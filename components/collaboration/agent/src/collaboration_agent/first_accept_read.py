"""Bounded public acquisition for the first Real ACCEPT Human gate.

This module performs one fixed public GET and only normalizes public signed
record envelopes.  Signature, tclk frame, offer policy, freshness and candidate
validation remain in the pinned Node runtime.  ``PROFILE`` is a static
compatibility pin; only generation and HTTP framing are measured from the live
response.
"""

import json
import sys

from .material_fetch import transport
from .model import Invalid
from .real_nonce import (
    PROFILE,
    PROJECT_DID,
    ROOM,
    _DID,
    _nonce,
    _server_record,
    _timestamp_ms,
    acquire_export,
    parse_observation,
)


MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _normalize_record(raw_line):
    """Return one structurally usable public envelope, or None.

    Cryptographic validity and frame meaning are deliberately not decided here.
    """
    try:
        row = _server_record(raw_line)
        if row is None:
            return None
        seq = row.get("seq")
        sender = row.get("from")
        nonce = row.get("nonce")
        signature = row.get("sig")
        line = row.get("text")
        if not (type(seq) is int and 0 < seq <= MAX_SAFE_INTEGER
                and type(sender) is str and _DID.fullmatch(sender) is not None
                and type(nonce) is int and type(signature) is str
                and type(line) is str and len(line) <= 4096):
            return None
        return {
            "room": ROOM,
            "seq": seq,
            "timestampMs": _timestamp_ms(row.get("ts")),
            "sender": sender,
            "nonce": _nonce(nonce),
            "signature": signature,
            "line": line,
        }
    except Invalid:
        return None


def parse_records(raw):
    """Normalize the byte-bounded export in source order without truncation."""
    records = []
    for raw_line in raw.split(b"\n"):
        if not raw_line:
            continue
        record = _normalize_record(raw_line)
        if record is None:
            continue
        records.append(record)
    return records


def acquire_snapshot(*, send=transport, clock_ms=None,
                     expected_did=PROJECT_DID):
    """Acquire one export and return the bounded Stage C public snapshot.

    ``expected_did`` exists only for explicit offline fixture injection.  The
    production CLI exposes no identity override and always uses PROJECT_DID.
    """
    raw, captured_at_ms = acquire_export(send=send, clock_ms=clock_ms)
    observation = parse_observation(
        raw,
        expected_did,
        captured_at_ms=captured_at_ms,
        generation=1,
        profile=PROFILE,
    )
    return {"version": 1, "observation": observation,
            "records": parse_records(raw)}


def main(argv=None):
    try:
        if (sys.argv[1:] if argv is None else argv):
            raise Invalid("FIRST_ACCEPT_READ_ARGUMENTS_NOT_ALLOWED")
        packet = acquire_snapshot()
        print(json.dumps(packet, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":")))
        return 0
    except (Invalid, OSError, ValueError):
        # Never reflect public response bytes or exception internals.
        print(json.dumps({"status": "HUMAN_STOP",
                          "reason": "FIRST_ACCEPT_PUBLIC_READ_FAILED"},
                         separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
