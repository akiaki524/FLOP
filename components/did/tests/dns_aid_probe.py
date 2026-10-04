"""Resolve this domain's DNS for AI Discovery records and report what is actually served.

Not a pytest module (the filename keeps it out of collection): it queries the public DNS,
so it asserts nothing about a build — it tells you what a validator standing outside the
zone can see right now.

    python tests/dns_aid_probe.py technocore.chat

Queries over DNS-over-HTTPS rather than the resolver library, because that is the transport
DNS-AID validators use and it is the one whose answers include the `AD` flag verbatim. It
also sidesteps a local dig too old to render SVCB presentation format: DiG 9.10 prints an
unrelated RRset for `-t HTTPS` rather than admitting it does not know the type.

Two names are expected to be *absent*, and the probe says so rather than staying quiet.
`_a2a._agents` and `_mcp._agents` are omitted on purpose: the HTTP origin implements neither
protocol, and the MCP wrapper is a stdio distribution with no hosted endpoint for a service
binding to point at. Finding records there means somebody published a claim the origin cannot
answer, which is why it is reported as a finding rather than as a bonus.

Absence is only meaningful if the lookup succeeded, so two things are checked per name rather
than inferred. First the RCODE: DoH reports SERVFAIL and REFUSED inside an HTTP 200 body, and
treating those as "no records" would let a broken resolver read as the very absence this probe
wants to confirm. Only NOERROR and NXDOMAIN are answers; anything else moves to the next
resolver. Second the `AD` flag, per response — an authenticated *denial* is what proves a name
is unpublished, and a signed apex says nothing about a name that lives under an insecure
delegation, so the DS lookup cannot stand in for it.

A miss right after publishing is usually cache, not a failed write: the SOA carries a 1800s
negative TTL, so a resolver that answered NODATA before publication keeps doing so. Confirm
against the authoritative servers before believing this probe:

    dig -t TYPE64 _index._agents.<domain> @cloe.ns.cloudflare.com
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

# Cloudflare first, Google as the fallback, matching what the readiness scanners do. A
# resolver-level failure on one is not evidence about the zone, so the probe tries both
# before calling a name missing.
RESOLVERS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)

# NOERROR covers both records and NODATA; NXDOMAIN is a real answer too — the name is
# genuinely not there. Every other RCODE (SERVFAIL, REFUSED, FORMERR) is the resolver
# failing to answer, which is not evidence about the zone either way.
NOERROR, NXDOMAIN = 0, 3
ANSWERED = frozenset({NOERROR, NXDOMAIN})

SVCB, TXT, DS = 64, 16, 43

# What each name is for, and whether it is meant to exist. The absent ones are listed so a
# false claim published later shows up as a finding instead of going unnoticed.
NAMES = (
    ("_index._agents", SVCB, True, "well-known entry point"),
    ("_index._agents", TXT, True, "index hint (not draft-defined)"),
    ("_chat._agents", SVCB, True, "the service itself"),
    ("_a2a._agents", SVCB, False, "omitted — no A2A at this origin"),
    ("_mcp._agents", SVCB, False, "omitted — the MCP wrapper is stdio, not hosted"),
)


def query(name: str, rrtype: int) -> dict | None:
    """One DoH lookup with `do=1`, so the answer carries the resolver's DNSSEC verdict.

    Returns None when no resolver produced an answer — distinct from an answer that
    contains no records, which is a fact about the zone rather than about the lookup.
    """
    params = urllib.parse.urlencode({"name": name, "type": rrtype, "do": "1"})
    for base in RESOLVERS:
        # Both URLs are literals in RESOLVERS; nothing here takes a caller-supplied scheme.
        req = urllib.request.Request(f"{base}?{params}", headers={"Accept": "application/dns-json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            continue
        # A DoH server reports SERVFAIL/REFUSED with HTTP 200 and an RCODE in the body, so
        # the status has to be read before the payload counts as an answer.
        if payload.get("Status") in ANSWERED:
            return payload
    return None


def answers(payload: dict | None, rrtype: int) -> list[str]:
    """The record data for the type asked about; CNAMEs and other chaff are dropped."""
    if not payload:
        return []
    return [a["data"] for a in payload.get("Answer", []) if a.get("type") == rrtype]


def main(domain: str) -> int:
    print(f"DNS-AID under _agents.{domain}\n")
    problems = 0
    answered = 0
    unauthenticated: list[str] = []

    for label, rrtype, expected, note in NAMES:
        payload = query(f"{label}.{domain}", rrtype)
        found = answers(payload, rrtype)
        kind = {SVCB: "SVCB", TXT: "TXT"}[rrtype]
        # True for a signed record set and for a signed denial alike; both are things a
        # validating consumer is entitled to act on, and neither follows from the apex.
        authentic = bool(payload and payload.get("AD"))

        if payload is None:
            verdict, problems = "RESOLVER FAILED", problems + 1
        elif bool(found) == expected:
            verdict = "ok"
        else:
            verdict, problems = ("MISSING" if expected else "UNEXPECTED"), problems + 1

        if payload is not None:
            answered += 1
            if not authentic:
                unauthenticated.append(f"{kind} {label}")

        flag = "AD=yes" if authentic else "AD=NO " if payload else "      "
        print(f"  {kind:5} {label:14} {verdict:15} {flag}  ({note})")
        for record in found:
            print(f"        {record}")

    # The chain of trust, which is a registrar action and not a zone edit: the zone can be
    # signed — RRSIGs present — while the parent holds no DS, and then every answer above
    # is unauthenticated however well-formed it looks. Reported alongside the per-name AD
    # flags rather than instead of them: the DS proves the delegation is signed, not that
    # the names under it are.
    ds = query(domain, DS)
    signed = bool(answers(ds, DS))
    if not answered:
        authenticity = "no answers to judge"
    else:
        authenticity = "NO" if unauthenticated else "yes"
    print(f"\n  DNSSEC  DS at parent: {'yes' if signed else 'NO'}", end="")
    print(f"   discovery answers authenticated: {authenticity}")
    if not signed:
        print("        no DS — the parent does not vouch for the zone's signing key")
        problems += 1
    if unauthenticated:
        print(f"        unauthenticated: {', '.join(unauthenticated)}")
        problems += 1

    print(f"\n{'all as documented' if not problems else f'{problems} finding(s)'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "technocore.chat"))
