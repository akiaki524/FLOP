"""Deterministic permanent features: Radar, Opportunity, Anomaly, Actor, Coverage.

Everything here is code: counts, signatures, hashes, syntax, protocol state and coverage.
Natural-language judgement is left to the optional semantic layer (semantic.py).
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import re

from . import findings as F
from . import signature as S
from . import tclk
from .util import canonical, digest, ms_to_iso, normalize_for_similarity, parse_rfc3339_ms

HOUR_MS = 3600 * 1000

OPPORTUNITY_RULES = (
    ("job", re.compile(r"\b(?:hiring|bounty|paid (?:task|work|gig)|job|gig|contract work|will pay|rfp|request for proposals?)\b|募集|依頼|報酬|仕事", re.I)),
    ("review", re.compile(r"\b(?:review (?:wanted|needed|requested)|needs? (?:a )?review|audit(?:or)? (?:wanted|needed)|code review)\b|レビュー(?:募集|依頼)|監査依頼", re.I)),
    ("collaboration", re.compile(r"\b(?:looking for (?:collaborators|partners|agents)|seeking (?:collaborators|partners)|collaborat(?:e|ion|ors)|co-build|partner with)\b|共同開発|協業|コラボ", re.I)),
    ("contribution", re.compile(r"\b(?:good first issue|contributors? welcome|testers? wanted|pull requests? welcome)\b|テスター募集|貢献募集", re.I)),
    ("help_request", re.compile(r"\b(?:help wanted|need help|needs help|can (?:anyone|someone) help|looking for help)\b|助けて|手伝って|支援募集", re.I)),
)
REWARD = re.compile(r"(?:\$\s?\d[\d,.]*|\b\d[\d,.]*\s?(?:usd|usdc|flop|polf|eth|sol|btc|sats)\b|報酬|謝礼)", re.I)
DEADLINE = re.compile(r"\b(?:by|before|until|deadline|due)\b[^.\n]{0,40}|\d{4}-\d{2}-\d{2}|締切|期限|まで", re.I)
ROOM_REF = re.compile(r"/r/([a-z0-9][a-z0-9_-]{0,47})\b")


def ts_ms(rec):
    return parse_rfc3339_ms(rec["message"].get("ts"))


def is_protocol_text(text):
    if not isinstance(text, str):
        return False
    if tclk.line_version(text):
        return True
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            return isinstance(json.loads(stripped), dict)
        except ValueError:
            return False
    return False


def unique_messages(records):
    """Collapse the same stored message seen via several sources: same room, generation,
    seq and identical content. Anything less certain is kept separate."""
    groups = {}
    for rec in records:
        identity = (rec["room"], rec["generation"], rec["seq"], digest(rec["message"]))
        if identity in groups:
            groups[identity]["also_refs"].append(rec["ref"])
        else:
            groups[identity] = {**rec, "also_refs": []}
    return list(groups.values())


def sender_view(rec):
    did = S.attributable_did(rec["sig_status"], rec["message"])
    if did:
        return {"kind": "did", "did": did, "signature": S.VALID}
    raw = rec["message"].get("from")
    return {"kind": "unverified", "claimed_from": raw if isinstance(raw, str) else None,
            "signature": rec["sig_status"],
            "note": "not attributed; unsigned nickname or unverifiable signature"}


# ── Observed Surface Radar ──────────────────────────────────────────────────────────

def radar(messages, rooms_config, source_statuses, as_of, window_ms):
    start, prev_start = as_of - window_ms, as_of - 2 * window_ms
    first_seen_did = {}
    for rec in messages:
        did = S.attributable_did(rec["sig_status"], rec["message"])
        t = ts_ms(rec)
        if did and t is not None and (did not in first_seen_did or t < first_seen_did[did]):
            first_seen_did[did] = t
    rooms = defaultdict(list)
    for rec in messages:
        rooms[rec["room"]].append(rec)
    room_sources = defaultdict(list)
    for sid, status in source_statuses.items():
        room_sources[status["room"]].append(sid)
    names = sorted(set(rooms) | set(rooms_config) | set(room_sources))
    out = []
    for room in names:
        items = rooms.get(room, [])
        stamps = [ts_ms(r) for r in items]
        known = [t for t in stamps if t is not None]
        in_window = [r for r, t in zip(items, stamps) if t is not None and start <= t < as_of]
        previous = [r for r, t in zip(items, stamps) if t is not None and prev_start <= t < start]
        sig = defaultdict(int)
        for r in in_window:
            sig[r["sig_status"]] += 1
        new_dids = sorted({r["message"]["from"] for r in in_window
                           if r["sig_status"] == S.VALID and start <= first_seen_did.get(r["message"]["from"], -1) < as_of})
        statuses = [source_statuses[s] for s in room_sources.get(room, [])]
        if not statuses:
            capture_state = "NOT_INGESTED (no Analyzer source configured)"
        elif all(s["status"] == "UNAVAILABLE" for s in statuses):
            capture_state = "SOURCE_UNAVAILABLE (medium or path not connected; not deletion)"
        elif all(s["status"] == "READ" for s in statuses):
            capture_state = "READ"
        elif all(s["status"] in ("UNAVAILABLE", "FAILED") for s in statuses):
            capture_state = "SOURCE_FAILED (ingestion failed or medium not connected; not the room's state)"
        else:  # any DEFERRED / PARTIAL / FAILED / UNAVAILABLE next to a source that read
            capture_state = "PARTIALLY_READ"
        if in_window:
            activity = "ACTIVE_IN_WINDOW"
        elif capture_state.startswith("READ") or capture_state == "PARTIALLY_READ":
            activity = "NO_RECORDS_IN_WINDOW (quiet room vs capture stop is not distinguishable from Archive alone)"
        else:
            activity = "UNKNOWN (" + capture_state.split(" ")[0] + ")"
        protocol = defaultdict(int)
        for r in in_window:
            text = r["message"].get("text")
            if tclk.line_version(text):
                protocol[tclk.line_version(text)] += 1
            elif is_protocol_text(text):
                protocol["json-object"] += 1
        out.append({
            "room": room,
            "configured": room in rooms_config,
            "role": rooms_config.get(room, {}).get("role"),
            "sources": room_sources.get(room, []),
            "capture_state": capture_state,
            "activity": activity,
            "window": {"start": ms_to_iso(start), "end": ms_to_iso(as_of)},
            "records_in_window": len(in_window),
            "records_previous_window": len(previous),
            "change": len(in_window) - len(previous),
            "signature_breakdown_in_window": dict(sig),
            "verified_signed": sig.get(S.VALID, 0),
            "unsigned": sig.get(S.UNSIGNED, 0),
            "new_verified_dids_first_observed_in_window": new_dids,
            "protocol_candidates_in_window": dict(protocol),
            "records_total_observed": len(items),
            "records_without_valid_time": len(items) - len(known),
            "observed_period": {"first": ms_to_iso(min(known)) if known else None,
                                "last": ms_to_iso(max(known)) if known else None},
            "note": "first observed != DID creation or first participation; counts cover observed sources only",
        })
    return out


# ── Opportunity Radar ───────────────────────────────────────────────────────────────

def opportunities(messages, tclk_result, interests, as_of, window_ms, closed_rooms=()):
    out = []
    include = [w.casefold() for w in interests.get("keywords", [])]
    exclude = [w.casefold() for w in interests.get("exclude", [])]
    for view in tclk_result["contracts"]:
        state = view["protocol_status"]
        expired = view["deadline_state"]["offer_expired"]
        if state == "proposed" and not expired:
            observed = "NO_ACCEPT_OBSERVED (not proof the offer is still open)"
        elif state == "proposed":
            observed = "EXPIRED_BY_OFFER_DEADLINE"
        else:
            observed = f"NOT_OPEN (protocol status {state} observed)"
        haystack = canonical({"asset": view["asset"], "rails": view["rails"], "job": view["job"]}).casefold()
        out.append({
            "opportunity_id": "O-" + digest(["tclk", view["offer_id"]])[:16],
            "kind": "tclk_offer",
            "information_class": "Observed+Derived",
            "evidence_refs": [view["offer_ref"]],
            "requester": {"kind": "did", "did": view["offerer_did"], "role": view["role_of_offerer"]},
            "request": {"amount": view["amount"], "asset": view["asset"], "lock": view["lock"],
                        "rails": view["rails"], "job": view["job"]},
            "deadlines_ms": view["deadlines_ms"],
            "observed_status": observed,
            "reward_statement": "amount stated in a signed offer; not a payment guarantee",
            "missing_information": [x for x in (
                "settlement rail not verified",
                view["value_backing_note"],
                "deal room not observed" if view["deal_room"] and not view["deal_room_observed"] else None,
                "job content outside tclk (see job binding)" if view["job"] else "work content not stated in frame",
            ) if x],
            "relevance": _relevance(haystack, include, exclude),
            "next_checks": ["read offer terms against Human-set capability/risk policy",
                            "verify rail and counterparty independently before any commitment (separate authority)"],
            "actionable": state == "proposed" and not expired,
        })
    start = as_of - window_ms
    for rec in messages:
        text = rec["message"].get("text")
        t = ts_ms(rec)
        if (not isinstance(text, str) or is_protocol_text(text) or t is None or not start <= t < as_of
                or rec["room"] in closed_rooms):
            continue
        matches = [(name, m.group(0)) for name, rx in OPPORTUNITY_RULES for m in [rx.search(text)] if m]
        if not matches:
            continue
        lowered = text.casefold()
        if any(word in lowered for word in exclude):
            continue
        reward = REWARD.search(text)
        deadline = DEADLINE.search(text)
        sender = sender_view(rec)
        out.append({
            "opportunity_id": "O-" + digest(["text", rec["ref"]])[:16],
            "kind": "text_candidate",
            "information_class": "Observed+Derived(keyword rule)",
            "evidence_refs": [rec["ref"]] + rec.get("also_refs", []),
            "room": rec["room"], "observed_at": rec["message"].get("ts"),
            "requester": sender,
            "categories": sorted({name for name, _ in matches}),
            "matched_terms": sorted({m for _, m in matches}),
            "text_excerpt": text[:280],
            "reward_mentioned": reward.group(0) if reward else None,
            "reward_statement": "mentioned in text only; not a payment guarantee" if reward else None,
            "deadline_mentioned": deadline.group(0) if deadline else None,
            "observed_status": "UNKNOWN (free text has no acceptance tracking)",
            "missing_information": [x for x in (
                None if sender["kind"] == "did" else "requester identity unverified",
                None if reward else "no reward/fee stated",
                None if deadline else "no deadline stated",
                "required capabilities not structured") if x],
            "relevance": _relevance(lowered, include, exclude),
            "next_checks": ["read the original record", "semantic classification pending or not run"],
            "actionable": None,
        })
    return out


def _relevance(haystack, include, exclude):
    hits = sorted({w for w in include if w and w in haystack})
    return {"matched_interests": hits,
            "basis": "Human-configured keywords only" if include else "no Human interests configured"}


# ── Change / Anomaly Detection ──────────────────────────────────────────────────────

RULES = {
    "ANOM-RATE-001": {"id": "ANOM-RATE-001", "version": "1",
                      "condition": "verified DID's records in window >= min_count and >= ratio x (baseline mean + 1)",
                      "scope": "per room, per signature-valid DID",
                      "required_evidence": "baseline windows fully inside the room's observed period",
                      "benign": ["event start or deadline rush", "legitimate automated agent schedule",
                                 "catch-up after the agent's own downtime", "observer backfill of a quiet period"]},
    "ANOM-DUP-001": {"id": "ANOM-DUP-001", "version": "1",
                     "condition": "same normalized free text >= min_repeats times in one room within window",
                     "scope": "per room; protocol frames and JSON objects excluded",
                     "required_evidence": "the repeated records",
                     "benign": ["standard notice or template", "legitimate retry/resend", "event announcement",
                                "several agents using the same public template"]},
    "ANOM-XROOM-001": {"id": "ANOM-XROOM-001", "version": "1",
                       "condition": "same normalized free text in >= min_rooms observed rooms within window",
                       "scope": "observed rooms only",
                       "required_evidence": "one record per room",
                       "benign": ["cross-posted announcement", "shared template", "bridge/relay agent"]},
    "SEC-MB-UNSIGNED-001": {"id": "SEC-MB-UNSIGNED-001", "version": "1",
                            "condition": "a stored record without signature material in an 'mb-' (signed-only) room",
                            "scope": "rooms whose name starts with 'mb-'",
                            "required_evidence": "the stored record and its room",
                            "benign": ["venue room-class rule differs in the live version",
                                       "record stored before the room class applied",
                                       "Observer/Analyzer mis-assigned the room"]},
    "SEC-SIG-STORED-001": {"id": "SEC-SIG-STORED-001", "version": "1",
                           "condition": "a stored record with well-formed signature material that does not verify",
                           "scope": "all observed rooms",
                           "required_evidence": "the stored record (room, nonce, text, sig, from)",
                           "benign": ["nonce posted with leading zeros but stored as integer (signed text differs)",
                                      "text changed by storage/serialization in Observer or Analyzer",
                                      "Analyzer verifier bug"]},
    "EVID-CONFLICT-001": {"id": "EVID-CONFLICT-001", "version": "1",
                          "condition": "same source/stream/seq identifier observed with different content",
                          "scope": "per source", "required_evidence": "both stored versions",
                          "benign": ["epoch boundary not separated", "source-side re-serialization change"]},
    "EVID-INTEGRITY-001": {"id": "EVID-INTEGRITY-001", "version": "1",
                           "condition": "a published archive unit failed hash/sequence verification or changed after it was read",
                           "scope": "per archive unit", "required_evidence": "unit digest history",
                           "benign": ["partial copy on transfer medium", "reading a unit during migration"]},
    "EVID-SOURCE-CONFLICT-001": {"id": "EVID-SOURCE-CONFLICT-001", "version": "1",
                                 "condition": "Observer recorded a CONFLICT for a seq",
                                 "scope": "manifest receipts", "required_evidence": "Observer conflict receipt",
                                 "benign": ["server-side edit or re-serialization", "generation boundary ambiguity"]},
    "TCLK-INVALID-001": {"id": "TCLK-INVALID-001", "version": tclk.PROFILE_VERSION,
                         "condition": "signature-valid tclk/1 frame rejected by decoder, room binding or state guard",
                         "scope": "tclk-offers and derived deal rooms",
                         "required_evidence": "the frame record and the contract history it was judged against",
                         "benign": ["race: another accept applied first", "client bug", "late frame after deadline",
                                    "rejection shows the conforming reader defended correctly"]},
    "TCLK-CLAIM-001": {"id": "TCLK-CLAIM-001", "version": tclk.PROFILE_VERSION,
                       "condition": "a lock/terminal state rests on chat frames whose rail facts were not checked",
                       "scope": "contracts reaching locked or later", "required_evidence": "lock frame",
                       "benign": ["rail was checked by a party outside Analyzer"]},
}


def anomaly_findings(messages, rules_config, as_of, window_ms, observed_since):
    out, notes = [], []
    start = as_of - window_ms
    rate = rules_config.get("rate", {})
    min_count, ratio, baseline_windows = rate.get("min_count", 20), rate.get("ratio", 5.0), rate.get("baseline_windows", 7)
    dup = rules_config.get("duplicate", {})
    min_repeats, min_chars = dup.get("min_repeats", 5), dup.get("min_chars", 16)
    min_rooms = rules_config.get("cross_room", {}).get("min_rooms", 3)
    per_sender = defaultdict(lambda: defaultdict(list))
    texts = defaultdict(list)
    for rec in messages:
        t = ts_ms(rec)
        if t is None:
            continue
        did = S.attributable_did(rec["sig_status"], rec["message"])
        if did:
            per_sender[(rec["room"], did)][(as_of - t - 1) // window_ms if t < as_of else -1].append(rec)
        text = rec["message"].get("text")
        if start <= t < as_of and isinstance(text, str) and not is_protocol_text(text):
            norm = normalize_for_similarity(text)
            if len(norm) >= min_chars:
                texts[norm].append(rec)
    baseline_start = start - baseline_windows * window_ms
    for (room, did), buckets in per_sender.items():
        current = buckets.get(0, [])
        if len(current) < min_count:
            continue
        since = observed_since.get(room)
        if since is None or since > baseline_start:
            notes.append({"rule": "ANOM-RATE-001", "room": room, "did": did,
                          "note": "BASELINE_INSUFFICIENT: observed history shorter than baseline; no finding"})
            continue
        mean = sum(len(buckets.get(i, [])) for i in range(1, baseline_windows + 1)) / baseline_windows
        if len(current) < ratio * (mean + 1):
            continue
        out.append(F.make(
            category="ABUSE_CANDIDATE", rule=RULES["ANOM-RATE-001"], subject=f"rate:{room}:{did}:{start // window_ms}",
            claim=f"signature-valid DID posted {len(current)} records in one window vs baseline mean {mean:.1f}",
            expected="activity near the DID's own observed baseline", observed=f"{len(current)} records in window",
            evidence_refs=[r["ref"] for r in current], severity="INFO", detection_method="DETERMINISTIC_RULE",
            coverage={"room": room, "window": [ms_to_iso(start), ms_to_iso(as_of)],
                      "baseline_windows": baseline_windows, "observed_sources_only": True},
            alternatives=RULES["ANOM-RATE-001"]["benign"],
            evidence_facets=F.facets(direct="records observed", signature="VALID for every counted record",
                                     coverage="observed rooms only", unverified=["intent", "operator identity"]),
            details={"did": did, "count": len(current), "baseline_mean": mean, "ratio_config": ratio}))
    by_room_text = defaultdict(list)
    for norm, recs in texts.items():
        rooms = sorted({r["room"] for r in recs})
        for room in rooms:
            in_room = [r for r in recs if r["room"] == room]
            if len(in_room) >= min_repeats:
                by_room_text[room].append((norm, in_room))
        if len(rooms) >= min_rooms:
            out.append(F.make(
                category="ABUSE_CANDIDATE", rule=RULES["ANOM-XROOM-001"],
                subject=f"xroom:{digest(norm)[:16]}:{start // window_ms}",
                claim=f"identical free text observed in {len(rooms)} rooms within one window",
                expected="room-specific content", observed=f"rooms: {', '.join(rooms)}",
                evidence_refs=[r["ref"] for r in recs], severity="INFO", detection_method="DETERMINISTIC_RULE",
                coverage={"rooms": rooms, "window": [ms_to_iso(start), ms_to_iso(as_of)], "observed_sources_only": True},
                alternatives=RULES["ANOM-XROOM-001"]["benign"],
                evidence_facets=F.facets(direct="records observed", signature=_sig_mix(recs), coverage="observed rooms only",
                                         unverified=["whether senders are the same operator"]),
                details={"text_sha256": digest(norm), "senders": _senders(recs)}))
    for room, items in by_room_text.items():
        for norm, recs in items:
            out.append(F.make(
                category="ABUSE_CANDIDATE", rule=RULES["ANOM-DUP-001"],
                subject=f"dup:{room}:{digest(norm)[:16]}:{start // window_ms}",
                claim=f"identical free text repeated {len(recs)} times in {room} within one window",
                expected="non-repetitive posts", observed=f"{len(recs)} repeats",
                evidence_refs=[r["ref"] for r in recs], severity="INFO", detection_method="DETERMINISTIC_RULE",
                coverage={"room": room, "window": [ms_to_iso(start), ms_to_iso(as_of)], "observed_sources_only": True},
                alternatives=RULES["ANOM-DUP-001"]["benign"],
                evidence_facets=F.facets(direct="records observed", signature=_sig_mix(recs), coverage="observed rooms only",
                                         unverified=["multiple DIDs are not assumed to be one operator"]),
                details={"text_sha256": digest(norm), "senders": _senders(recs)}))
    return out, notes


def _sig_mix(recs):
    counts = defaultdict(int)
    for r in recs:
        counts[r["sig_status"]] += 1
    return dict(counts)


def _senders(recs):
    return {"verified_dids": sorted({r["message"]["from"] for r in recs if r["sig_status"] == S.VALID}),
            "unverified_claimed_from": sorted({str(r["message"].get("from")) for r in recs if r["sig_status"] != S.VALID})}


def security_findings(messages):
    """One finding per rule and position (room/generation/seq). Distinct contents at one position
    (kept apart by unique_messages) are merged into that finding with all their refs, never
    dropped: the finding ID stays position-based, so it does not depend on which copy came first."""
    groups = defaultdict(list)
    for rec in messages:
        # A signed-only room should hold no record lacking valid signature material: unsigned, a
        # did-shaped sender without sig/nonce, or partial/malformed material (INVALID has its own rule).
        if rec["room"].startswith("mb-") and rec["sig_status"] in (S.UNSIGNED, S.MATERIAL_ABSENT, S.MALFORMED):
            groups[("mb-unsigned", rec["room"], rec["generation"], rec["seq"])].append(rec)
        if rec["sig_status"] == S.INVALID:
            groups[("stored-invalid-sig", rec["room"], rec["generation"], rec["seq"])].append(rec)
    out = []
    for key in sorted(groups, key=str):
        recs = sorted(groups[key], key=lambda r: digest(r["message"]))
        f = _security_finding(key[0], recs[0])
        if len(recs) > 1:
            f = _merge_security(f, key[0], recs)
        out.append(f)
    return out


def _merge_security(f, kind, recs):
    """Several distinct contents at one position: keep every ref and say so explicitly."""
    refs = [ref for r in recs for ref in [r["ref"]] + r.get("also_refs", [])]
    statuses = sorted({r["sig_status"] for r in recs})
    details = {**f["details"], "distinct_contents_at_position": len(recs),
               "content_sha256": sorted(digest(r["message"]) for r in recs)}
    if kind == "stored-invalid-sig":
        details["claimed_did_not_attributed"] = sorted({str(r["message"].get("from")) for r in recs})
        observed = f"{len(recs)} distinct stored records at this position do not verify"
    else:
        observed = f"{len(recs)} distinct stored records at this position have no usable signature material ({', '.join(statuses)})"
    return F.make(category=f["category"], rule=f["rule"], subject=f["subject"], claim=f["claim"],
                  expected=f["expected_behavior"], observed=observed, evidence_refs=refs, severity=f["severity"],
                  detection_method=f["detection_method"], coverage=f["coverage"],
                  alternatives=f["alternative_explanations"],
                  evidence_facets={**f["evidence"], "coverage": "one position, several distinct contents"},
                  details=details)


def _security_finding(kind, rec):
    msg = rec["message"]
    if kind == "mb-unsigned":
        return F.make(
            category="SECURITY_FINDING_CANDIDATE", rule=RULES["SEC-MB-UNSIGNED-001"],
            subject=f"mb-unsigned:{rec['room']}:{rec['generation']}:{rec['seq']}",
            claim="a record without signature material is stored in a signed-only (mb-) room",
            expected="the venue refuses unsigned writes to mb- rooms",
            observed=f"stored record has no usable signature material ({rec['sig_status']})", evidence_refs=[rec["ref"]] + rec.get("also_refs", []),
            severity="UNKNOWN", detection_method="DETERMINISTIC_RULE",
            coverage={"room": rec["room"], "seq": rec["seq"]}, alternatives=RULES["SEC-MB-UNSIGNED-001"]["benign"],
            evidence_facets=F.facets(direct="stored record", signature=rec["sig_status"], coverage="single record",
                                     unverified=["live venue room-class rules", "whether the record was accepted by the venue as shown"]),
            details={"note": "candidate only; a malformed post alone is not a vulnerability"})
    # kind == "stored-invalid-sig"
    return F.make(
        category="SECURITY_FINDING_CANDIDATE", rule=RULES["SEC-SIG-STORED-001"],
        subject=f"stored-invalid-sig:{rec['room']}:{rec['generation']}:{rec['seq']}",
        claim="a stored signed-lane record does not verify over room|nonce|text",
        expected="the venue verifies signed writes before storing them",
        observed="signature does not verify against the claimed did:key",
        evidence_refs=[rec["ref"]] + rec.get("also_refs", []), severity="UNKNOWN",
        detection_method="DETERMINISTIC_RULE", coverage={"room": rec["room"], "seq": rec["seq"]},
        alternatives=RULES["SEC-SIG-STORED-001"]["benign"],
        evidence_facets=F.facets(direct="stored record", signature="INVALID", coverage="single record",
                                 unverified=["exact posted nonce text", "HTTP raw bytes (not in Archive)"]),
        details={"claimed_did_not_attributed": msg.get("from"),
                 "note": "not evidence that the claimed DID forged anything"})


def replay_integrity_inputs(conflicts, unit_changes, units, coverage_items, as_of_ms):
    """Evidence-integrity facts usable by a historical replay at `as_of_ms`.

    Record conflicts and unit changes carry the Analyzer's own detection time and are kept only
    if detected at or before as-of. Quarantined units and Observer CONFLICT coverage carry no
    placeable time here (unit/coverage rows hold the latest read only), so they are never assumed
    to have existed at as-of: they are excluded and counted for a Coverage Advisory instead.
    """
    placed = ([c for c in conflicts if c["detected_at"] * 1000 <= as_of_ms],
              [u for u in unit_changes if u["detected_at"] * 1000 <= as_of_ms],
              [], [c for c in coverage_items if c["kind"] != "CONFLICT"])
    unplaced = {"quarantined_units": sum(u["status"] == "QUARANTINED" for u in units),
                "source_conflict_receipts": sum(c["kind"] == "CONFLICT" for c in coverage_items)}
    return placed, (unplaced if any(unplaced.values()) else None)


def evidence_findings(conflicts, unit_changes, units, coverage_items):
    out = []
    by_key = defaultdict(list)
    for c in conflicts:
        by_key[c["key"]].append(c)
    for key in sorted(by_key):
        # One finding per identifier; every (existing, observed) digest pair is kept, so each side of
        # each conflict can be shown in the evidence packet (report_candidate resolves the contents).
        pairs = sorted({(c["existing_sha256"], c["observed_sha256"]) for c in by_key[key]})
        out.append(F.make(
            category="EVIDENCE_CONFLICT", rule=RULES["EVID-CONFLICT-001"], subject=f"record-conflict:{key}",
            claim="the same record identifier was read with different content",
            expected="one content per identifier",
            observed="; ".join(f"{e[:12]} vs {o[:12]}" for e, o in pairs),
            evidence_refs=[c["observed_ref"] for c in by_key[key]], severity="UNKNOWN",
            detection_method="DETERMINISTIC_RULE", coverage={"key": key},
            alternatives=RULES["EVID-CONFLICT-001"]["benign"],
            evidence_facets=F.facets(direct="both digests stored; each side's content as retained (evidence packet)",
                                     signature="n/a", coverage="single identifier"),
            details={"pairs": [{"existing_sha256": e, "observed_sha256": o} for e, o in pairs]}))
    by_unit = defaultdict(list)
    for u in unit_changes:
        by_unit[(u["source_id"], u["unit"])].append(u)
    for (sid, unit) in sorted(by_unit):
        changes = by_unit[(sid, unit)]
        u = changes[0]
        details = ({"previous_sha256": u["previous_sha256"], "observed_sha256": u["observed_sha256"]}
                   if len(changes) == 1 else
                   {"changes": [{"previous_sha256": c["previous_sha256"], "observed_sha256": c["observed_sha256"]}
                                for c in changes]})  # in detection order (change_id)
        # Structured identity: report candidates never re-split "<source_id>:<unit>" (IDs may contain ":").
        details.update(source_id=sid, unit=unit)
        out.append(F.make(
            category="EVIDENCE_CONFLICT", rule=RULES["EVID-INTEGRITY-001"], subject=f"unit-changed:{u['source_id']}:{u['unit']}",
            claim="a published archive unit changed after it was read", expected="published shards are immutable",
            observed="digest changed" if len(changes) == 1 else f"digest changed {len(changes)} times",
            evidence_refs=[f"{u['source_id']}:{u['unit']}"], severity="UNKNOWN",
            detection_method="DETERMINISTIC_RULE", coverage={"unit": u["unit"]},
            alternatives=RULES["EVID-INTEGRITY-001"]["benign"],
            evidence_facets=F.facets(direct="digest history", signature="n/a", coverage="single unit"),
            details=details))
    for u in units:
        if u["status"] == "QUARANTINED":
            out.append(F.make(
                category="EVIDENCE_CONFLICT", rule=RULES["EVID-INTEGRITY-001"], subject=f"unit-quarantined:{u['source_id']}:{u['unit']}",
                claim="an archive unit failed verification and was quarantined", expected="header/payload/chain hashes agree",
                observed=u["detail"] or "verification failed", evidence_refs=[f"{u['source_id']}:{u['unit']}"],
                severity="UNKNOWN", detection_method="DETERMINISTIC_RULE", coverage={"unit": u["unit"]},
                alternatives=RULES["EVID-INTEGRITY-001"]["benign"],
                evidence_facets=F.facets(direct="unit bytes", signature="n/a", coverage="unit excluded from analysis"),
                details={"reason": u["detail"], "source_id": u["source_id"], "unit": u["unit"]}))
    for item in coverage_items:
        if item["kind"] == "CONFLICT":
            out.append(F.make(
                category="EVIDENCE_CONFLICT", rule=RULES["EVID-SOURCE-CONFLICT-001"],
                subject=f"source-conflict:{item['source_id']}:{item['ref']}",
                claim="Observer recorded conflicting content for one seq", expected="one content per seq",
                observed="Observer CONFLICT receipt", evidence_refs=[f"{item['source_id']}:{item['ref']}"],
                severity="UNKNOWN", detection_method="SOURCE_RECORDED", coverage={"room": item["room"], "seq": item["start_seq"]},
                alternatives=RULES["EVID-SOURCE-CONFLICT-001"]["benign"],
                evidence_facets=F.facets(direct="Observer receipt", signature="n/a", coverage="single seq"),
                details={"detail": item["detail"].get("evidence"), "source_id": item["source_id"],
                         "receipt_ref": item["ref"]}))
    return out


def tclk_findings(tclk_result):
    out = []
    groups = defaultdict(list)
    for v in tclk_result["records"]:
        if v["protocol_invalid"]:
            groups[(v["contract"] or v["ref"], v["reason"], v["attributable_did"])].append(v)
    for (subject, reason, did), items in groups.items():
        out.append(F.make(
            category="PROTOCOL_INVALID", rule=RULES["TCLK-INVALID-001"], subject=f"tclk:{subject}:{reason}:{did}",
            claim=f"signature-valid tclk/1 frame(s) rejected: {reason}",
            expected="frames conform to tclk/1 decoding, room binding and state guards",
            observed=f"{len(items)} rejected frame(s); protocol state unchanged",
            evidence_refs=[v["ref"] for v in items], severity="INFO", detection_method="PROTOCOL_PROFILE",
            coverage={"contract": subject if subject.startswith("0x") else None,
                      "judged_against": "observed tclk-offers and deal-room records only"},
            alternatives=RULES["TCLK-INVALID-001"]["benign"],
            evidence_facets=F.facets(direct="signed frame record(s)", signature="VALID",
                                     coverage="prerequisite history observed for this judgement",
                                     unverified=["sender intent", "whether any client accepted the frame"]),
            details={"attributable_did": did,
                     "note": "a rejected frame is not a vulnerability; conforming readers refuse it"}))
    for view in tclk_result["contracts"]:
        if view["protocol_status"] in ("locked", "claimed", "refunded"):
            out.append(F.make(
                category="UNVERIFIED_CLAIM", rule=RULES["TCLK-CLAIM-001"], subject=f"tclk-rail:{view['contract_id']}",
                claim=f"lock on rail {view['rail']} ref {view['rail_ref']} is asserted by a chat frame only",
                expected="rail facts checked independently", observed=f"protocol status {view['protocol_status']}; rail NOT_CHECKED",
                evidence_refs=[s["ref"] for s in view["steps"] if s["type"] == "lock"], severity="INFO",
                detection_method="PROTOCOL_PROFILE", coverage={"contract": view["contract_id"]},
                alternatives=RULES["TCLK-CLAIM-001"]["benign"],
                evidence_facets=F.facets(direct="lock frame", signature="VALID", coverage="chat transcript only",
                                         unverified=["escrow existence/amount/payee", "delivery", "quality", "settlement"]),
                details={"value_backing_note": view["value_backing_note"]}))
    return out


# ── Actor Activity History / Profile View ───────────────────────────────────────────

def actors(messages, tclk_result, finding_list):
    profiles, aliases, unattributed = {}, defaultdict(lambda: {"count": 0, "rooms": set()}), defaultdict(int)
    for rec in messages:
        msg = rec["message"]
        did = S.attributable_did(rec["sig_status"], msg)
        t = msg.get("ts")
        if did:
            p = profiles.setdefault(did, {"did": did, "records": 0, "rooms": set(), "first_observed": t,
                                          "last_observed": t, "offers": 0, "contracts": [],
                                          "rejected_frames_attributed": 0, "findings": []})
            p["records"] += 1
            p["rooms"].add(rec["room"])
            tm = parse_rfc3339_ms(t)
            if tm is not None:
                if parse_rfc3339_ms(p["first_observed"]) is None or tm < parse_rfc3339_ms(p["first_observed"]):
                    p["first_observed"] = t
                if parse_rfc3339_ms(p["last_observed"]) is None or tm > parse_rfc3339_ms(p["last_observed"]):
                    p["last_observed"] = t
        elif rec["sig_status"] == S.UNSIGNED and isinstance(msg.get("from"), str):
            a = aliases[(rec["room"], msg["from"])]
            a["count"] += 1
        elif isinstance(msg.get("from"), str):
            unattributed[msg["from"]] += 1
    for view in tclk_result["contracts"]:
        for did in {view["offerer_did"], view["payer_did"], view["payee_did"]} - {None}:
            if did in profiles:
                profiles[did]["contracts"].append({"contract": view["contract_id"], "offer": view["offer_id"],
                                                   "observed_status": view["protocol_status"]})
        if view["offerer_did"] in profiles:
            profiles[view["offerer_did"]]["offers"] += 1
    for v in tclk_result["records"]:
        if v["verdict"] == tclk.REJECTED and v["attributable_did"] in profiles:
            profiles[v["attributable_did"]]["rejected_frames_attributed"] += 1
    for f in finding_list:
        for did in set(_dids_in(f)) & set(profiles):
            profiles[did]["findings"].append(f["finding_id"])
    out = []
    for p in profiles.values():
        terminal = [c for c in p["contracts"] if c["observed_status"] in tclk.TERMINAL]
        out.append({**p, "rooms": sorted(p["rooms"]),
                    "contracts_terminal_observed": len(terminal),
                    "contracts_unconfirmed_outcome": len(p["contracts"]) - len(terminal),
                    "scope_note": ("observed sources only; first/last observed are not creation or join times; "
                                   "no reputation score, fraud probability or personality assessment is produced; "
                                   "unconfirmed outcomes are not failures")})
    return {"dids": sorted(out, key=lambda p: p["did"]),
            "unsigned_aliases": [{"room": room, "nickname": nick, "records": v["count"],
                                  "note": "display alias only; not the same actor across records"}
                                 for (room, nick), v in sorted(aliases.items())],
            "claimed_but_unverified_from": [{"claimed_from": k, "records": n,
                                             "note": "signature invalid/absent/unverifiable; not attributed"}
                                            for k, n in sorted(unattributed.items())]}


def _dids_in(finding):
    details = finding.get("details", {})
    dids = []
    for key in ("did", "attributable_did"):
        if isinstance(details.get(key), str):
            dids.append(details[key])
    dids.extend(details.get("senders", {}).get("verified_dids", []))
    return dids


# ── Coverage Advisor ────────────────────────────────────────────────────────────────

def coverage_advisories(source_statuses, coverage_items, messages, tclk_result, rooms_config, streams,
                        verifier_available):
    out = []
    for sid, st in sorted(source_statuses.items()):
        if st["status"] != "READ":
            out.append({"kind": "SOURCE_" + st["status"], "source": sid, "room": st["room"], "reason": st.get("reason"),
                        "cannot_judge": "activity, gaps and protocol state for this source during this run",
                        "not_implied": "deletion, loss or fraud",
                        "request_candidate": "connect/restore the medium or provide a consistent snapshot"})
        if st["kind"] == "full-capture-archive" and not st.get("manifest_read"):
            out.append({"kind": "GAP_EVIDENCE_NOT_READ", "source": sid, "room": st["room"],
                        "cannot_judge": "Observer-recorded GAP/CONFLICT/late observations; epoch numbers",
                        "request_candidate": "enable read_manifest for a transferred or quiescent archive"})
    late = defaultdict(list)
    for item in coverage_items:
        if item["kind"] == "LATE_OBSERVATION":
            late[(item["source_id"], item["stream"])].append(item["start_seq"])
    for item in coverage_items:
        if item["kind"] in ("GAP", "BOOTSTRAP_UNOBSERVED_PREFIX"):
            # Distinct sequences only: a repeated late receipt for one seq recovers nothing more.
            recovered = sorted({s for s in late[(item["source_id"], item["stream"])]
                               if s is not None and item["start_seq"] is not None and item["end_seq"] is not None
                               and item["start_seq"] <= s <= item["end_seq"]})
            size = (item["end_seq"] - item["start_seq"] + 1) if item["start_seq"] is not None and item["end_seq"] is not None else None
            out.append({"kind": item["kind"], "source": item["source_id"], "room": item["room"], "stream": item["stream"],
                        "seq_range": [item["start_seq"], item["end_seq"]], "unobserved_seq_count": size,
                        "late_observed_seq": recovered,
                        "resolution": ("PARTIALLY_RECOVERED" if recovered and len(recovered) < (size or 0)
                                       else "RECOVERED_BY_LATE_OBSERVATION" if recovered else "OPEN"),
                        "cannot_judge": "activity and protocol steps inside the range",
                        "not_implied": "physical loss or absence of messages"})
        elif item["kind"] in ("GENERATION_BOUNDARY", "BOUNDARY_OBSERVATION", "SHARD_PENDING_CHECKPOINT"):
            out.append({"kind": item["kind"], "source": item["source_id"], "room": item["room"],
                        "stream": item["stream"], "ref": item["ref"],
                        "cannot_judge": "continuity across this boundary" if "BOUNDARY" in item["kind"] else
                        "records in the pending shard until its checkpoint is published"})
    malformed = Counter((item["source_id"], item["room"], item["detail"].get("table"))
                        for item in coverage_items if item["kind"] == "ROW_MALFORMED")
    for (sid, room, table), count in sorted(malformed.items(), key=str):
        out.append({"kind": "ROW_MALFORMED", "source": sid, "room": room, "table": table, "rows": count,
                    "cannot_judge": "the content of these rows (not valid Observer Evidence; excluded, not repaired)",
                    "not_implied": "tampering or fraud: the cause is unknown"})
    by_source = defaultdict(list)
    for s in streams:
        by_source[s["source"]].append(s)
    for sid, items in by_source.items():
        items.sort(key=lambda s: tclk.stream_key(s["stream"]))
        for prev, nxt in zip(items, items[1:]):
            if prev.get("cursor") is not None and nxt.get("anchor_seq") is not None:
                out.append({"kind": "STREAM_DISCONTINUITY", "source": sid, "streams": [prev["stream"], nxt["stream"]],
                            "previous_last_seq": prev["cursor"], "next_first_seq": (nxt["anchor_seq"] or 0) + 1,
                            "generations": [prev.get("generation"), nxt.get("generation")],
                            "cannot_judge": "whether this is a gap or an epoch/generation boundary without manifest evidence"})
    for room, cfg in sorted(rooms_config.items()):
        if not any(st["room"] == room for st in source_statuses.values()):
            out.append({"kind": "ROOM_NOT_INGESTED", "room": room, "role": cfg.get("role"),
                        "cannot_judge": "anything about this room", "request_candidate": "add a read-only source for it"})
    out.extend(tclk_result["coverage"])
    if not verifier_available:
        out.append({"kind": "SIGNATURE_VERIFIER_UNAVAILABLE",
                    "cannot_judge": "signature validity, DID attribution, tclk state",
                    "request_candidate": "install the optional 'cryptography' package"})
    absent = sum(1 for r in messages if r["sig_status"] == S.MATERIAL_ABSENT)
    if absent:
        out.append({"kind": "SIGNATURE_MATERIAL_ABSENT", "records": absent,
                    "cannot_judge": "attribution of did-shaped senders without stored sig/nonce"})
    observed = {r["room"] for r in messages} | set(rooms_config)
    discovered = defaultdict(list)
    for rec in messages:
        if rec["room"] != "events" or not isinstance(rec["message"].get("text"), str):
            continue
        for name in ROOM_REF.findall(rec["message"]["text"]):
            if name not in observed:
                discovered[name].append(rec["ref"])
    for name, refs in sorted(discovered.items()):
        out.append({"kind": "ROOM_DISCOVERED_NOT_OBSERVED", "room": name, "evidence_refs": refs[:5],
                    "method": "heuristic '/r/<room>' mention in events",
                    "request_candidate": "Human decides whether Observer should add it; Analyzer never adds sources"})
    return out
