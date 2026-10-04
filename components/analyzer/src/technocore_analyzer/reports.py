"""Situation Report, Report Candidates / Evidence Packets and notification events.

Canonical output is versioned JSON; the Japanese Markdown view is generated from it.
Nothing here sends, posts, files an Issue/Advisory, mails, or fetches a URL. Report
candidates are drafts for a Human; submission state is recorded only via the Human CLI.
"""

from __future__ import annotations

from .render import defang, redact_secrets, safe_md
from .util import SCHEMA_VERSION, digest

CHANNELS = {
    "ABUSE_CANDIDATE": {
        "suggested": "technocore-chat public Issue (abuse) — only if the content is safe to publish",
        "basis": "technocore-chat SECURITY.md separates abuse (public) from vulnerabilities (private) [SPEC S1]",
        "public": True},
    "SECURITY_FINDING_CANDIDATE": {
        "suggested": "private vulnerability channel named in technocore-chat SECURITY.md (never a public Issue)",
        "basis": "[SPEC S1]", "public": False},
    "PROTOCOL_INVALID": {
        "suggested": "NO_EXTERNAL_REPORT_SUGGESTED — a rejected frame shows the reader defended; "
                     "if an implementation ACCEPTED it, use tclk SECURITY.md private channel",
        "basis": "[SPEC S4]", "public": False},
    "PROTOCOL_FRAUD_CANDIDATE": {"suggested": "HUMAN_DECISION_REQUIRED (destination unknown; do not guess)",
                                 "basis": "SPEC §12", "public": False},
    "QUALITY_REPUTATION_CONCERN": {"suggested": "HUMAN_DECISION_REQUIRED", "basis": "SPEC §12", "public": False},
    "MARKET_AGENT_ANOMALY": {"suggested": "HUMAN_DECISION_REQUIRED", "basis": "SPEC §12", "public": False},
    "EVIDENCE_CONFLICT": {"suggested": "INTERNAL (Observer/Analyzer engineering Issue candidate)",
                          "basis": "own evidence pipeline", "public": False},
    "UNVERIFIED_CLAIM": {"suggested": "NO_EXTERNAL_REPORT_SUGGESTED", "basis": "claim tracking only", "public": False},
}
REPORTABLE = {"ABUSE_CANDIDATE", "SECURITY_FINDING_CANDIDATE", "PROTOCOL_FRAUD_CANDIDATE",
              "MARKET_AGENT_ANOMALY", "QUALITY_REPUTATION_CONCERN", "EVIDENCE_CONFLICT"}


def envelope(kind, run, body):
    return {"schema": f"{SCHEMA_VERSION}#{kind}", "result_id": f"{kind}-{run['run_id']}", "kind": kind,
            "generated_at": run["generated_at"], "data_reference_time": run["reference_time"],
            "as_of": run["as_of"], "scope": run["scope"], "inputs": run["inputs"],
            "profiles_rules_models": run["applied"], "analysis_mode": run["mode"],
            "run_mode": run.get("run_mode"),
            "processing_status": run["status"], "revision_of": run.get("previous_run"), **body}


def _public_str(value, redactions, limit=80):
    if not isinstance(value, str):
        return None
    clean, kinds = redact_secrets(value)
    redactions.extend(kinds)
    return defang(clean[:limit])


def _version_entries(finding, versions, redactions):
    """Both sides of every record conflict of this identifier, labelled and matched by digest.
    A side whose content is no longer held is shown by its digest only (never guessed)."""
    key = finding["coverage"]["key"]
    internal, public = [], []
    for n, pair in enumerate(finding["details"]["pairs"], start=1):
        for side in ("existing", "observed"):
            sha = pair[f"{side}_sha256"]
            v = versions.get((key, sha))
            tag = digest([key, side, sha])[:12]
            if v is None:
                internal.append({"conflict": n, "side": side, "record_sha256": sha, "available": False,
                                 "note": "content of this version is no longer held (e.g. re-read after rebuild)"})
                public.append({"ref_id": tag, "conflict": n, "side": side, "record_sha256": sha, "available": False})
                continue
            msg = v["message"]
            internal.append({"conflict": n, "side": side, "record_sha256": sha, "available": True,
                             "ref": v["ref"], "room": v["room"], "stream": v["stream"], "seq": v["seq"],
                             "ts": msg.get("ts"), "from": msg.get("from"), "signature": v["sig_status"],
                             "hash_scope": v["hash_scope"], "locator": v["locator"], "stored_message": msg})
            text = msg.get("text") if isinstance(msg.get("text"), str) else ""
            clean, kinds = redact_secrets(text)
            redactions.extend(kinds)
            public.append({"ref_id": tag, "conflict": n, "side": side, "record_sha256": sha, "available": True,
                           "room": v["room"], "seq": v["seq"], "ts": _public_str(msg.get("ts"), redactions),
                           "from": _public_str(msg.get("from"), redactions), "signature": v["sig_status"],
                           "text_excerpt": defang(clean[:500])})
    return internal, public


def _integrity_entries(finding):
    """Non-record Evidence-integrity facts (unit digest history, unit quarantine, Observer CONFLICT
    receipt) as the finding holds them. They are not records, so they are never looked up as
    record refs; bytes the Analyzer does not keep are said to be absent, not "unavailable records"."""
    kind = finding["subject"].split(":", 1)[0]
    ref = finding["evidence_refs"][0]
    details, tag = finding["details"], digest(ref)[:12]
    # Identity comes from the finding's structured fields, never from splitting the ref string:
    # source IDs may legally contain ":" (or "|"), so a delimiter split would misattribute them.
    source_id = details.get("source_id")
    locus = details.get("receipt_ref") if kind == "source-conflict" else details.get("unit")
    if kind == "unit-changed":
        changes = details.get("changes") or [{"previous_sha256": details["previous_sha256"],
                                              "observed_sha256": details["observed_sha256"]}]
        changes = [{"order": n, **c} for n, c in enumerate(changes, start=1)]  # detection order
        internal = {"evidence_kind": "UNIT_DIGEST_HISTORY", "source_id": source_id, "unit": locus,
                    "digest_changes": changes, "record_evidence": False,
                    "unit_bytes_retained": False, "note": "digests of a published archive unit; not a record"}
        public = {"ref_id": tag, "evidence_kind": "UNIT_DIGEST_HISTORY", "digest_changes": changes}
    elif kind == "unit-quarantined":
        reason = details.get("reason") or ""
        internal = {"evidence_kind": "UNIT_QUARANTINE", "source_id": source_id, "unit": locus,
                    "reason": reason, "record_evidence": False, "unit_bytes_retained": False,
                    "note": "the unit failed verification and was excluded; its bytes are not kept by the Analyzer"}
        public = {"ref_id": tag, "evidence_kind": "UNIT_QUARANTINE", "reason_code": reason.split(":", 1)[0]}
    else:  # source-conflict
        internal = {"evidence_kind": "OBSERVER_CONFLICT_RECEIPT", "source_id": source_id, "receipt_ref": locus,
                    "room": finding["coverage"].get("room"), "seq": finding["coverage"].get("seq"),
                    "receipt_evidence": details.get("detail"), "record_evidence": False,
                    "note": "recorded by the Observer; the conflicting records themselves are not in this packet"}
        public = {"ref_id": tag, "evidence_kind": "OBSERVER_CONFLICT_RECEIPT",
                  "room": finding["coverage"].get("room"), "seq": finding["coverage"].get("seq")}
    return [internal], [public]


RERUN = "re-run the rule/profile version named above on the same inputs"


def _evidence_kind(finding):
    rule = finding["rule"]["id"]
    if rule == "EVID-CONFLICT-001":
        return "RECORD_CONFLICT"
    if rule == "EVID-SOURCE-CONFLICT-001":
        return "OBSERVER_CONFLICT_RECEIPT"
    if rule == "EVID-INTEGRITY-001":
        return "UNIT_DIGEST_HISTORY" if finding["subject"].startswith("unit-changed:") else "UNIT_QUARANTINE"
    return "RECORD"


def _verification_steps(finding):
    """Steps a Human can actually perform for the kind of Evidence this finding holds."""
    kind = _evidence_kind(finding)
    if kind == "RECORD_CONFLICT":
        return ["for each conflict, compare the existing and observed record_sha256 with the stored content of "
                "that side in the internal packet (sides are matched by digest)",
                "re-read the current record by its locator and compare record_sha256; a side marked "
                "available=false is known by its digest only",
                "re-verify signatures over room|nonce|text only for sides whose stored message carries signature "
                "material", RERUN]
    if kind == "UNIT_DIGEST_HISTORY":
        return ["compare the unit's digest history (previous -> observed, in detection order) with the Analyzer "
                "unit change log and, if the unit is still readable, its current SHA-256",
                "published archive units are immutable by the Observer contract; no record or signature is "
                "involved in this finding", RERUN]
    if kind == "UNIT_QUARANTINE":
        return ["re-run the Archive verification (header, payload, chain) on the named unit and compare the "
                "failure reason; the Analyzer keeps no bytes of a quarantined unit",
                "no record of this unit was admitted as Evidence; there is nothing to re-read by record locator",
                RERUN]
    if kind == "OBSERVER_CONFLICT_RECEIPT":
        return ["read the named receipt from the Observer manifest (read-only) and compare room, seq and the "
                "recorded evidence metadata",
                "the conflict is recorded by the Observer; the receipt is not a signed record and carries no "
                "signature to re-verify", RERUN]
    return ["re-read each referenced record from the Observer Archive by locator and compare record_sha256",
            "re-verify signatures over room|nonce|text with the exact stored values", RERUN]


def _machine_verified(finding):
    return {"RECORD": "evidence refs, hashes, signature status, rule result",
            "RECORD_CONFLICT": "record key, both digests of each conflict, stored contents where held, signature "
                               "status per held side, rule result",
            "UNIT_DIGEST_HISTORY": "unit identity, digest history, rule result (no record or signature involved)",
            "UNIT_QUARANTINE": "unit identity, quarantine reason, rule result (no record or signature involved)",
            "OBSERVER_CONFLICT_RECEIPT": "receipt identity, room/seq and receipt metadata as recorded by the "
                                         "Observer, rule result (no signature involved)"}[_evidence_kind(finding)]


def report_candidate(finding, records_by_ref, run, versions=None):
    """Internal Evidence Packet + public-safe derivative. Never sent anywhere."""
    channel = CHANNELS[finding["category"]]
    evidence, public_evidence, redactions = [], [], []
    conflict_sides = finding["rule"]["id"] == "EVID-CONFLICT-001" and "pairs" in finding.get("details", {})
    integrity = finding["rule"]["id"] in ("EVID-INTEGRITY-001", "EVID-SOURCE-CONFLICT-001")
    if conflict_sides:
        evidence, public_evidence = _version_entries(finding, versions or {}, redactions)
    elif integrity:
        evidence, public_evidence = _integrity_entries(finding)
    for ref in ([] if conflict_sides or integrity else finding["evidence_refs"][:20]):
        rec = records_by_ref.get(ref)
        if rec is None:
            evidence.append({"ref": ref, "available": False})
            public_evidence.append({"ref_id": digest(ref)[:12], "available": False})
            continue
        msg = rec["message"]
        evidence.append({"ref": ref, "room": rec["room"], "seq": rec["seq"], "ts": msg.get("ts"),
                         "from": msg.get("from"), "signature": rec["sig_status"],
                         "record_sha256": rec["record_sha256"], "hash_scope": rec["hash_scope"],
                         "locator": rec["locator"]})
        text = msg.get("text") if isinstance(msg.get("text"), str) else ""
        clean, kinds = redact_secrets(text)
        redactions.extend(kinds)
        # Every Evidence-derived string in the public copy is redacted and defanged, not only text.
        public_evidence.append({"ref_id": digest(ref)[:12], "room": rec["room"], "seq": rec["seq"],
                                "ts": _public_str(msg.get("ts"), redactions),
                                "from": _public_str(msg.get("from"), redactions),
                                "signature": rec["sig_status"], "record_sha256": rec["record_sha256"],
                                "text_excerpt": defang(clean[:500])})
    body = {
        "finding_id": finding["finding_id"], "finding_revision": finding.get("revision"),
        "category": finding["category"], "severity": finding["severity"],
        "target_and_impact": finding["subject"], "claim": finding["claim"],
        "expected_behavior": finding["expected_behavior"], "observed_behavior": finding["observed_behavior"],
        "timeline": sorted((e for e in evidence if isinstance(e.get("ts"), str) and e["ts"]),
                           key=lambda e: e["ts"]),  # stored ts may be any JSON type; only text is ordered
        "spec_basis": finding["rule"], "verification_steps": _verification_steps(finding),
        "unverified_points": finding["evidence"]["unverified_points"],
        "alternative_explanations": finding["alternative_explanations"],
        "duplicate_and_known_spec_check": "NOT_DONE_AUTOMATICALLY — Human checks existing Issues/Advisories and "
                                          "documented known behaviour (nickname self-claim, untrusted content, retention)",
        "suggested_channel": channel,
        "confirm_before_submission": ["channel is still current", "target version", "public-safety of every field"],
        "machine_verified_part": _machine_verified(finding),
        "llm_drafted_part": None,
        "submission": "NOT_SUBMITTED — Analyzer never sends; approval must bind recipient, visibility, body and attachment versions",
    }
    internal = {**body, "evidence_packet": evidence}
    public_body = {k: v for k, v in body.items() if k not in ("timeline",)}
    if finding["category"] == "EVIDENCE_CONFLICT":
        # Evidence-conflict subjects contain Analyzer source IDs / unit locators. Keep the
        # internal target exact, but expose only a stable opaque target in the public derivative.
        public_body["target_and_impact"] = f"evidence-integrity:{digest(finding['subject'])[:12]}"
    public = {**public_body,
              "evidence_packet": public_evidence,
              "public_copy": {"derived_from": "internal packet", "redactions": sorted(set(redactions)),
                              "removed": ["locators and source ids", "host paths", "non-public refs"],
                              "urls": "defanged, never fetched",
                              "verification_limits": "redacted spans cannot be re-verified from the public copy"}}
    internal["candidate_sha256"] = digest(internal)
    public["candidate_sha256"] = digest(public)
    return internal, public


def candidate_md(candidate):
    lines = [f"# 報告案（未送信）: {safe_md(candidate['finding_id'])} rev {candidate.get('finding_revision')}", "",
             "> 自動送信・Issue作成・Advisory提出は行いません。Humanの確認と別承認が必要です。", "",
             f"- 分類: `{candidate['category']}` / Severity(影響): `{candidate['severity']}`",
             f"- 主張: {safe_md(candidate['claim'])}",
             f"- 期待挙動: {safe_md(candidate['expected_behavior'])}",
             f"- 観測挙動: {safe_md(candidate['observed_behavior'])}",
             f"- 推奨窓口（要再確認）: {safe_md(candidate['suggested_channel']['suggested'])}",
             f"- 重複・既知仕様の確認: {safe_md(candidate['duplicate_and_known_spec_check'])}", "",
             "## 正常な代替説明"] + [f"- {safe_md(a)}" for a in candidate["alternative_explanations"]] + [
             "", "## 未確認点"] + [f"- {safe_md(u)}" for u in candidate["unverified_points"]] + [
             "", "## 証拠（抜粋）"]
    for e in candidate["evidence_packet"]:
        if e.get("evidence_kind"):  # non-record integrity evidence: say what it is, never a fake record line
            extra = ("; ".join(f"{c['order']}: {c['previous_sha256'][:16]}… → {c['observed_sha256'][:16]}…"
                               for c in e.get("digest_changes", []))
                     or safe_md(e.get("reason_code") or f"room={e.get('room')} seq={e.get('seq')}"))
            lines.append(f"- `{safe_md(e.get('ref_id'), 120)}` {e['evidence_kind']} — {extra}")
            continue
        lines.append(f"- `{safe_md(e.get('ref') or e.get('ref_id'), 120)}` room={safe_md(e.get('room'))} "
                     f"seq={e.get('seq')} sig={e.get('signature')} sha256={str(e.get('record_sha256'))[:16]}…")
        if e.get("text_excerpt"):
            lines.append(f"  - 本文抜粋: {safe_md(e['text_excerpt'])}")
    lines += ["", "## 検証手順"] + [f"1. {safe_md(s)}" for s in candidate["verification_steps"]]
    lines += ["", f"- 機械検証済みの範囲: {safe_md(candidate['machine_verified_part'])}"]
    return "\n".join(lines) + "\n"


def notification_events(finding_changes, run):
    """Minimal events for an *approved* Notifier. Analyzer holds no webhook and delivers nothing."""
    events, suppressed = [], 0
    for change in finding_changes:
        if change["change"] in ("NEW", "UPDATED", "NO_LONGER_DETECTED", "RECOVERED_FROM_FAILED_RUN"):
            events.append({"schema": f"{SCHEMA_VERSION}#notification", "run_id": run["run_id"],
                           "event": f"FINDING_{change['change']}", "finding_id": change["finding_id"],
                           "category": change["category"], "severity": change["severity"],
                           "human_action": "review in Analyzer (analyzer findings show)"})
        else:
            suppressed += 1
    return events, {"suppressed_unchanged": suppressed, "reason": "same state is not re-notified"}


def situation_md(rep):
    L = [f"# Technocore Analyzer 状況レポート", "",
         f"- 生成: {rep['generated_at']} / 基準時刻(as-of): {rep['as_of']} / データ基準: {rep['data_reference_time']}",
         f"- 解析mode: `{rep['analysis_mode']}` / 処理状態: `{rep['processing_status']}`",
         f"- 対象: 観測済みSourceと期間のみ（全Technocore・全FLOP活動・Actorの全履歴ではありません）", "",
         "## Source"]
    for s in rep["sources"]:
        L.append(f"- `{safe_md(s['source_id'])}` room={safe_md(s['room'])} kind={s['kind']} "
                 f"status=`{s['status']}` {safe_md(s.get('reason') or '')}")
    L += ["", "## Observed Surface Radar", "",
          "| Room | 取込状態 | 活動 | 窓内件数 | 前窓 | 検証済署名 | 未署名 | 新規DID(初観測) |",
          "|---|---|---|---|---|---|---|---|"]
    for r in rep["radar"]:
        L.append(f"| {safe_md(r['room'])} | {safe_md(r['capture_state'], 40)} | {safe_md(r['activity'], 40)} | "
                 f"{r['records_in_window']} | {r['records_previous_window']} | {r['verified_signed']} | "
                 f"{r['unsigned']} | {len(r['new_verified_dids_first_observed_in_window'])} |")
    t = rep["tclk"]
    L += ["", "## tclk/1 Protocol Tracker", "",
          f"- 観測contract数: {t['contracts']} / 状態内訳: {safe_md(t['status_counts'])}",
          f"- record判定内訳: {safe_md(t['verdict_counts'])}",
          "- 状態は署名検証済みframeのみから復元。rail・納品・決済は未確認（receipt/heartbeatは証拠に格上げしない）"]
    L += ["", f"## Opportunity（{len(rep['opportunities'])}件、上位10件）"]
    for o in rep["opportunities"][:10]:
        who = o["requester"].get("did") or f"未検証: {o['requester'].get('claimed_from')}"
        L.append(f"- [{o['kind']}] {safe_md(o.get('text_excerpt') or o.get('request'), 160)} — {safe_md(who, 80)} "
                 f"— 状態: {safe_md(o['observed_status'], 80)}")
    L += ["", "## 要確認（新規・更新・非再現のFinding）"]
    if rep.get("run_mode") == "replay":
        L.append("- 履歴再生（replay）: Findingはas-of時点のrun-local再構成（findings.json）で、live Findingの"
                 "revision・Review・報告・解決は読まず変更もしません")
    for f in rep["review_queue"]:
        L.append(f"- `{f['finding_id']}` {f['change']} `{f['category']}` severity={f['severity']} "
                 f"— {safe_md(f['claim'], 160)}")
    if not rep["review_queue"]:
        L.append("- なし")
    L += ["", f"## Coverage Advisory（{len(rep['coverage'])}件）"]
    for c in rep["coverage"][:40]:
        L.append(f"- `{c['kind']}` {safe_md(c.get('room') or c.get('source') or '', 60)} — 判断不能: "
                 f"{safe_md(c.get('cannot_judge') or c.get('limits') or '', 120)}")
    L += ["", "## Event Profile"]
    for e in rep["events"]:
        L.append(f"- {safe_md(e.get('profile_id'))}: {safe_md(e.get('phase') or e.get('status'))} / "
                 f"再計算: {safe_md((e.get('recalculation') or {}).get('status'))} / 公式鍵: "
                 f"{safe_md((e.get('official_keys') or {}).get('status'))}")
    s = rep["semantic"]
    L += ["", "## 意味解析（LLM）",
          f"- runtime: `{s['runtime']}` / 状態: {safe_md(s['status'])} / 未実施task: {s['pending_tasks']}",
          "- LLM未実施の場合も、上記の決定論的結果・Finding・報告案は有効です。意味解析の未実施範囲はここに示します。",
          "", "## 限界", *[f"- {safe_md(x, 300)}" for x in rep["limitations"]]]
    return "\n".join(L) + "\n"
