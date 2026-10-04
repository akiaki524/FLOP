"""One finite Analyzer run: ingest → verify → analyse → persist → report.

Progress is Analyzer-owned and independent of the Observer. Units (shards/snapshots) are
tracked by digest, not by a max seq, so late small seqs, new segments and changed units
are all detected. A run is SUCCEEDED/PARTIAL only after its results are committed; an
interrupted run (its process gone, run lock free) becomes FAILED on the next start; an overlapping run
is refused (ANALYZER_RUN_IN_PROGRESS) and touches nothing. Output files become ``latest`` only after
that commit.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import secrets
import shutil
import time

from . import analysis, events, evidence, reports, semantic, signature, tclk
from .store import Store, claim_output_dir, private_umask
from .util import (ANALYZER_VERSION, SCHEMA_VERSION, canonical, digest, ms_to_iso, now_ms)

HOUR_MS = 3600 * 1000


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=True, indent=1, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def _publish_latest(out_root, value):
    """Atomically repoint latest.json. Until os.replace succeeds the previous pointer (a committed
    run) is untouched, so a failure here leaves it valid while the caller fails this run. The
    replace is the publication point: nothing after it can fail the run."""
    data = (json.dumps(value, ensure_ascii=True, indent=1, sort_keys=True) + "\n").encode("ascii")
    tmp = out_root / f".latest.json.tmp-{value['run_id']}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, out_root / "latest.json")
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:  # durability of the rename only; either pointer names a committed run
        dfd = os.open(out_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def ingest(store, config, run_id):
    budget = config["limits"]["max_new_records_per_run"]
    statuses, streams, new_total = {}, [], 0
    for source in config["resolved_sources"]:
        sid = source["id"]
        doc = {"source_id": sid, "kind": source["kind"], "room": source["room"],
               "provenance": source.get("provenance", "captured"), "status": "READ", "reason": None,
               "units": Counter(), "records_new": 0, "records_same": 0, "records_conflict": 0,
               "manifest_read": False, "latest_captured_at": None}
        previous = store.source_statuses().get(sid)
        if previous is not None and (previous.get("room"), previous.get("kind")) != (source["room"], source["kind"]):
            # The ID was repointed to another room/kind: its cached rows are not this source's.
            with store.transaction():
                store.reset_source(sid)
            doc["identity_reset"] = {"previous_room": previous.get("room"), "previous_kind": previous.get("kind")}
        try:
            read = evidence.adapter_for(source).read(store.known_units(sid), store.source_progress(sid))
        except Exception as exc:  # one source never stops the others
            doc.update(status="FAILED", reason="ADAPTER_ERROR:" + type(exc).__name__)
            statuses[sid] = doc
            with store.transaction():
                store.put_source_status(sid, {**doc, "units": {}}, run_id)
            continue
        doc.update(status=read.status, reason=read.reason, latest_captured_at=read.latest_captured_at)
        with store.transaction():
            for unit in read.units:
                complete = True
                for rec in unit.records:
                    # The budget counts records that would be NEW, not the size of the unit: a large
                    # already-read snapshot must still admit a few new records each run.
                    if new_total >= budget and not store.has_record(rec.key):
                        complete = False
                        break
                    status, detail = signature.verify_record(rec.room, rec.message)
                    outcome = store.ingest_record(rec, status, detail, run_id)
                    doc["records_" + outcome.lower()] += 1
                    new_total += outcome == "NEW"
                if not complete:
                    # Records ingested so far stay (they are SAME next run); the unit is not marked
                    # read, so the rest is picked up by a later run instead of being dropped.
                    doc["units"]["DEFERRED_BUDGET"] += 1
                    doc.update(status="PARTIAL", reason="DEFERRED_BY_RECORD_BUDGET")
                    continue
                store.record_unit(sid, unit, run_id)
                doc["units"][unit.status] += 1
            if read.manifest_quarantined:
                store.drop_manifest_coverage(sid)
            store.drop_pending_checkpoint(sid, [s["stream"] for s in read.streams])
            for item in read.coverage:
                store.put_coverage(item, run_id)
            if read.manifest is not None:
                doc["manifest_read"] = True
                doc["archive_id"] = read.manifest.get("archive_id")
                store.put_progress(sid, read.manifest["_through_entry_read"], read.manifest["_chain_hash_read"], run_id)
            doc["units"] = dict(doc["units"])
            store.put_source_status(sid, doc, run_id)
        statuses[sid] = doc
        for s in read.streams:
            if not doc["manifest_read"]:
                streams.append({**s, "source": sid})
    with store.transaction():
        rechecked = store.recheck_signatures(signature.verify_record, statuses)
    return statuses, streams, rechecked


def _semantic(store, config, run_id, messages, text_opps, as_of, window_ms, out_dir):
    llm = config["llm"]
    runtime = semantic.runtime_for(llm)
    pre = runtime.preflight()
    allowed = set(llm.get("allowed_rooms", []))
    by_ref = {m["ref"]: m for m in messages}
    matched_refs = {o["evidence_refs"][0] for o in text_opps}
    candidates = [by_ref[r] for r in sorted(matched_refs) if r in by_ref and by_ref[r]["room"] in allowed]
    start = as_of - window_ms
    pool = [m for m in messages if m["room"] in allowed and m["ref"] not in matched_refs
            and isinstance(m["message"].get("text"), str) and not analysis.is_protocol_text(m["message"]["text"])
            and (analysis.ts_ms(m) or -1) >= start and (analysis.ts_ms(m) or 0) < as_of]
    # Deterministic sample so repeated runs reuse the same bundle (and its result).
    sample = sorted(pool, key=lambda m: digest([m["ref"], start // window_ms]))[:llm.get("sample_size", 20)]
    context = {"interests": config["interests"], "analysis_window": [ms_to_iso(start), ms_to_iso(as_of)]}
    plans = []
    if candidates:
        plans.append(("opportunity_classification", candidates, as_of + window_ms, "TIME_SENSITIVE", 5))
    if sample:
        plans.append(("missed_expression_digest", sample, None, "HISTORICAL", 1))
    summary = {"runtime": runtime.name, "preflight": pre, "tasks": [], "excluded_rooms_policy":
               "only llm.allowed_rooms are ever bundled; private/deal rooms are excluded unless listed",
               "sampling": {"keyword_matched": len(candidates), "unmatched_pool": len(pool), "sampled": len(sample),
                            "unanalysed_remainder": max(0, len(pool) - len(sample))}}
    day_start = time.time() - (time.time() % 86400)
    per_run = 0
    stop_reason = None
    for purpose, items, expires, retain, priority in plans:
        bundle, transforms = semantic.build_bundle(purpose, items, context, llm["bundle_limits"])
        # Runtime is part of task identity: an offline fixture result/attempt is not a
        # real-runtime result/attempt, even when the Evidence bundle is byte-identical.
        task_id = "T-" + digest([runtime.name, bundle["bundle_sha256"]])[:20]
        _write(out_dir / "semantic-bundles" / f"{task_id}.json", {"bundle": bundle, "processing_record": transforms})
        status = store.put_task({"task_id": task_id, "kind": purpose, "purpose_key": purpose,
                                 "bundle_sha256": bundle["bundle_sha256"], "priority": priority,
                                 "retain_class": retain, "expires_ms": expires, "status": "PENDING"}, run_id)
        entry = {"task_id": task_id, "purpose": purpose, "items": len(bundle["items"])}
        attempts = len(store.results(task_id, include_quarantined=True, runtime=runtime.name))
        if status in ("COMPLETED",):
            entry["status"] = "REUSED_EXISTING_RESULT"
        elif expires is not None and expires <= now_ms() and retain == "TIME_SENSITIVE":
            store.set_task_status(task_id, "EXPIRED", "time-sensitive task is not re-run after its window")
            entry["status"] = "EXPIRED"
        elif not pre["can_run"]:
            store.set_task_status(task_id, "DEFERRED", pre["status"])
            entry["status"] = "DEFERRED:" + pre["status"]
        elif stop_reason:
            store.set_task_status(task_id, "DEFERRED", stop_reason)
            entry["status"] = "DEFERRED:" + stop_reason
        elif attempts >= llm["max_attempts_per_task"]:
            store.set_task_status(task_id, "GAVE_UP", "attempt cap reached; no infinite retry")
            entry["status"] = "GAVE_UP"
        elif (per_run >= llm["max_invocations_per_run"]
              or (runtime.name != "fixture" and store.invocations_since(day_start, runtime.name) >= llm["max_invocations_per_day"])):
            store.set_task_status(task_id, "DEFERRED", "LOCAL_INVOCATION_BUDGET")
            entry["status"] = "DEFERRED:LOCAL_INVOCATION_BUDGET"
        else:
            per_run += 1
            try:
                result = runtime.run(bundle)
            except Exception:  # a runtime fault never stops the deterministic analysis
                result = {"outcome": "RESULT_UNKNOWN", "started": time.time(), "finished": time.time(), "output": None}
            outcome, output = result["outcome"], result.get("output")
            validation = {"outcome": outcome}
            quarantined = False
            if outcome == "COMPLETED":
                try:
                    ok, validation = semantic.validate_output(bundle, output)
                except Exception:  # hostile shapes (NaN, odd types) are a schema failure, not a crash
                    ok, validation = False, {"outcome": "SCHEMA_MISMATCH", "problems": ["unvalidatable output"]}
                outcome = "COMPLETED" if ok else validation["outcome"]
                quarantined = outcome == "QUARANTINED_SECRET_SHAPED_OUTPUT"
            store.add_result(task_id, runtime.name, result["started"], result.get("finished"), outcome,
                             json.dumps(output, ensure_ascii=True) if output is not None else None,
                             validation, quarantined)
            store.set_task_status(task_id, "COMPLETED" if outcome == "COMPLETED" else "FAILED:" + outcome)
            entry["status"] = outcome
            if outcome in ("AUTH_FAILED", "QUOTA_EXHAUSTED", "RESULT_UNKNOWN", "TIMEOUT", "RUNTIME_UNAVAILABLE",
                           "BLOCKED_PREFLIGHT", "AUTH_ROUTE_UNVERIFIED"):
                stop_reason = outcome  # no retry storm; deterministic results are unaffected
        summary["tasks"].append(entry)
    # Only results of the bundles selected in THIS run (same items, context and interests,
    # because the task id is the bundle digest). Older results stay in history only.
    inferred = defaultdict(list)
    current_tasks = {t["task_id"] for t in summary["tasks"]}
    for task in store.tasks("COMPLETED"):
        if task["task_id"] not in current_tasks:
            continue
        for res in store.results(task["task_id"], runtime=runtime.name):
            if res["outcome"] != "COMPLETED" or not res["output"]:
                continue
            for item in json.loads(res["output"])["items"]:
                inferred[item["evidence_id"]].append({**item, "information_class": "Inferred",
                                                      "task_id": task["task_id"], "runtime": res["runtime"]})
    summary["status"] = ("NOT_RUN (" + pre["status"] + ")" if not pre["can_run"] else
                         "RAN" if any(t.get("status") == "COMPLETED" for t in summary["tasks"]) else
                         "NO_NEW_INFERENCE")
    summary["pending_tasks"] = len(store.tasks("DEFERRED")) + len(store.tasks("PENDING"))
    return summary, inferred


def run(config, *, reference_time_ms=None, as_of_ms=None):
    with private_umask():
        store = Store(config["state_db"])
        try:
            # Before recovery: a RUNNING row is only "interrupted" if no live run holds the lock.
            store.acquire_run_lock()
            return _run(store, config, reference_time_ms, as_of_ms)
        finally:
            store.close()


def _run(store, config, reference_time_ms, as_of_ms):
    store.recover_interrupted()
    current = now_ms()
    as_of = as_of_ms if as_of_ms is not None else current
    reference = reference_time_ms if reference_time_ms is not None else as_of
    window_ms = int(config["window_hours"] * HOUR_MS)
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3)
    # A historical replay (--as-of) is a read-only reconstruction: it shares the Evidence cache
    # but never reads or writes the live Finding lifecycle (revisions, candidates, notifications,
    # failed-run surfacing). Live runs never see replay runs as their predecessor either.
    replay = as_of_ms is not None
    mode = "replay" if replay else "current"
    previous = store.last_run(mode=mode)
    store.start_run(run_id, mode, reference, as_of, config["config_sha256"])
    out_root = Path(config["output_dir"])
    tmp = out_root / "runs" / f".tmp-{run_id}"
    replaced = False
    try:
        claim_output_dir(out_root)  # ours (marker / empty / legacy layout) before any chmod
        nice = config["limits"].get("nice")
        if nice:
            try:
                os.nice(nice)  # Observer capture keeps priority on a shared host
            except OSError:
                pass
        statuses, streams, rechecked = ingest(store, config, run_id)
        active_source_ids = set(statuses)
        # The state DB is historical. Current views must never analyze records cached for a
        # source that has since been removed/renamed from resolved_sources.
        records = [r for r in store.records() if r["source_id"] in active_source_ids]
        active_record_keys = {r["key"] for r in records}
        active_conflicts = [c for c in store.conflicts() if c["key"] in active_record_keys]
        active_unit_changes = [u for u in store.unit_changes() if u["source_id"] in active_source_ids]
        active_units = [u for u in store.units() if u["source_id"] in active_source_ids]
        active_coverage = [c for c in store.coverage_items() if c["source_id"] in active_source_ids]
        messages = analysis.unique_messages(records)
        replay_unplaced = []
        if as_of_ms is not None:
            # Historical replay cannot place missing/malformed timestamps on its timeline.
            # Exclude them rather than silently treating later-ingested evidence as already present.
            replay_unplaced = [m for m in messages if analysis.ts_ms(m) is None]
            visible = []
            for message in messages:
                stamp = analysis.ts_ms(message)
                if stamp is not None and stamp <= as_of:
                    visible.append(message)
            messages = visible
        observed_rooms = {m["room"] for m in messages}
        tclk_result = tclk.analyze(messages, observed_rooms, as_of, as_of if as_of_ms is not None else None)
        event_results = events.run_profiles(config["event_profiles"], messages, as_of)
        closed = {room for e in event_results for room in e.get("closed_rooms_for_opportunities", [])}
        opps = analysis.opportunities(messages, tclk_result, config["interests"], as_of, window_ms, closed)
        observed_since = {}
        for rec in messages:
            t = analysis.ts_ms(rec)
            if t is not None and (rec["room"] not in observed_since or t < observed_since[rec["room"]]):
                observed_since[rec["room"]] = t
        anomalies, anomaly_notes = analysis.anomaly_findings(messages, config["rules"], as_of, window_ms, observed_since)
        integrity = (active_conflicts, active_unit_changes, active_units, active_coverage)
        integrity_unplaced = None
        if replay:
            integrity, integrity_unplaced = analysis.replay_integrity_inputs(*integrity, as_of)
        new_findings = (anomalies + analysis.security_findings(messages) + analysis.tclk_findings(tclk_result)
                        + analysis.evidence_findings(*integrity)
                        + [f for e in event_results for f in e.get("findings", [])])
        unique = {f["finding_id"]: f for f in new_findings}
        changes = []
        if replay:
            # Run-local derivation only: no revision, no management binding, not persisted.
            all_findings = [{**unique[k], "lifecycle": "REPLAY_DERIVED",
                             "replay": {"as_of": ms_to_iso(as_of), "persisted": False,
                                        "note": "reconstructed from Evidence placeable at as-of; not a live "
                                                "finding revision and carries no review/report/resolution state"}}
                            for k in sorted(unique)]
        else:
            with store.transaction():
                for f in unique.values():
                    change, rev = store.upsert_finding(f, run_id)
                    changes.append({"finding_id": f["finding_id"], "change": change, "revision": rev,
                                    "category": f["category"], "severity": f["severity"], "claim": f["claim"]})
                for fid in store.mark_not_reproduced(set(unique), run_id):
                    changes.append({"finding_id": fid, "change": "NO_LONGER_DETECTED", "category": None,
                                    "severity": None, "claim": "no longer reproduced (history kept)"})
            # Findings first recorded by a failed run were never shown in a successful output: surface
            # them once now (a later committed run then treats them as ordinary UNCHANGED).
            seen_now = {c["finding_id"] for c in changes if c["change"] != "UNCHANGED"}
            for item in store.unsurfaced_findings():
                if item["finding_id"] not in seen_now:
                    changes = [c for c in changes if c["finding_id"] != item["finding_id"]] + [item]
            all_findings = store.findings()
        text_opps = [o for o in opps if o["kind"] == "text_candidate"]
        sem_summary, inferred = _semantic(store, config, run_id, messages, text_opps, as_of, window_ms, tmp)
        for o in opps:
            o["semantic"] = [x for ref in o["evidence_refs"] for x in inferred.get(ref, [])] or None
        actor_view = analysis.actors(messages, tclk_result, all_findings)
        # Stored Coverage items (manifest GAP/boundary/late observation, pending checkpoint) carry
        # no time of their own (receipts have none; rows hold only the latest read), so a replay
        # cannot tell whether they existed at as-of. They are never assumed to have: excluded and
        # counted below. Live runs use them as before.
        replay_coverage_unplaced = Counter(c["kind"] for c in active_coverage if c["kind"] != "CONFLICT") \
            if replay else None
        coverage = analysis.coverage_advisories(statuses, [] if replay else active_coverage, messages,
                                                tclk_result, config["rooms"], streams, signature.HAVE_CRYPTO)
        if replay_coverage_unplaced:
            coverage.append({"kind": "REPLAY_COVERAGE_UNPLACED", "items_by_kind": dict(replay_coverage_unplaced),
                             "cannot_judge": "which stored Coverage items (gaps, boundaries, late observations, "
                                             "pending checkpoints) already existed at as-of (historical placement "
                                             "unavailable); excluded, so their absence here is not evidence of none",
                             "request_candidate": "read Coverage from a live run"})
        if replay_unplaced:
            coverage.append({"kind": "REPLAY_TIME_UNPLACED", "records": len(replay_unplaced),
                             "cannot_judge": "records with missing/malformed ts are excluded from historical replay",
                             "request_candidate": "use a source timestamp that can be placed on the replay timeline"})
        if integrity_unplaced:
            coverage.append({"kind": "REPLAY_INTEGRITY_UNPLACED", **integrity_unplaced,
                             "cannot_judge": "whether these evidence-integrity facts already existed at as-of "
                                             "(historical placement unavailable); excluded from replay findings",
                             "request_candidate": "evaluate them in a live run"})
        coverage += [{"kind": "BASELINE_INSUFFICIENT", **n} for n in anomaly_notes]
        coverage += [{"kind": "EVENT_OFFICIAL_KEY_UNCONFIRMED", "profile": e["profile_id"],
                      "cannot_judge": "which posts are official referee evidence; sweep coverage is INCONCLUSIVE",
                      "request_candidate": "Human sets official_dids from the official launch record"}
                     for e in event_results if (e.get("official_keys") or {}).get("status") == "OFFICIAL_KEY_UNCONFIRMED"]
        radar = analysis.radar(messages, config["rooms"], statuses, as_of, window_ms)
        input_sha = digest(sorted((u["source_id"], u["unit"], u["sha256"]) for u in active_units))
        partial = (any(s["status"] != "READ" for s in statuses.values())
                   or any(e.get("status") == "FAILED" for e in event_results))
        run_status = "PARTIAL" if partial else "SUCCEEDED"
        analysis_mode = "DETERMINISTIC_ONLY" if sem_summary["status"].startswith("NOT_RUN") else \
            f"DETERMINISTIC+SEMANTIC({sem_summary['runtime']})"
        meta = {"run_id": run_id, "generated_at": ms_to_iso(current), "reference_time": ms_to_iso(reference),
                "as_of": ms_to_iso(as_of), "status": run_status, "mode": analysis_mode, "run_mode": mode,
                "previous_run": previous["run_id"] if previous else None,
                "scope": {"sources": sorted(statuses), "rooms": sorted(observed_rooms | set(config["rooms"])),
                          "window_hours": config["window_hours"],
                          "note": "observed sources and periods only"},
                "inputs": {"input_units_sha256": input_sha, "records_cached": len(records),
                           "unique_messages": len(messages), "signatures_rechecked": rechecked},
                "applied": {"analyzer": ANALYZER_VERSION, "schema": SCHEMA_VERSION, "tclk_profile": tclk.PROFILE_VERSION,
                            "rules": {k: v["version"] for k, v in analysis.RULES.items()},
                            "event_profiles": [(e.get("profile_id"), e.get("profile_version")) for e in event_results],
                            "signature_verifier": signature.verifier_name(),
                            "llm_runtime": sem_summary["runtime"], "config_sha256": config["config_sha256"]}}
        records_by_ref = {r["ref"]: r for r in records}
        # Both stored versions of each conflicting identifier, keyed by (record key, digest): the
        # current record and every conflicting observation kept in record_conflicts.
        room_of = {s["id"]: s["room"] for s in config["resolved_sources"]}
        versions = {(r["key"], r["record_sha256"]): r for r in records}
        for c in active_conflicts:
            sid, stream, seq = c["key"].rsplit("|", 2)
            existing = versions.get((c["key"], c["existing_sha256"]))
            room = room_of.get(sid)
            versions.setdefault((c["key"], c["observed_sha256"]), {
                "ref": c["observed_ref"], "room": room, "stream": stream, "seq": int(seq) if seq.isdigit() else seq,
                "message": c["observed_message"], "locator": None,
                "hash_scope": existing["hash_scope"] if existing else None,
                "sig_status": signature.verify_record(room, c["observed_message"])[0] if room else "UNCHECKED"})
        candidates_written, candidate_rows = [], []
        # A candidate is needed for every reportable finding whose CURRENT revision has none
        # committed: new/updated ones, and ones whose creating run failed before committing.
        # Replay findings have no persisted revision to bind a candidate to: none are written.
        needed = {c["finding_id"] for c in changes if c["change"] in ("NEW", "UPDATED")}
        if not replay:
            needed |= store.missing_candidates(reports.REPORTABLE)
        for f in all_findings:
            if f["category"] in reports.REPORTABLE and f["finding_id"] in needed \
                    and f.get("lifecycle") != "NO_LONGER_DETECTED":
                internal, public = reports.report_candidate(f, records_by_ref, meta, versions)
                base = tmp / "report-candidates" / f"{f['finding_id']}-r{f['revision']}"
                _write(base.with_suffix(".internal.json"), internal)
                _write(base.with_suffix(".public.json"), public)
                _write(base.with_suffix(".ja.md"), reports.candidate_md(public))
                # DB rows are written only in the commit transaction below, after the output
                # directory is in place, so a failed run never leaves a submittable digest.
                candidate_rows += [(f["finding_id"], f["revision"], "internal", internal["candidate_sha256"]),
                                   (f["finding_id"], f["revision"], "public", public["candidate_sha256"])]
                candidates_written.append(f["finding_id"])
        notices, suppression = reports.notification_events(changes, meta)
        tclk_summary = {"contracts": len(tclk_result["contracts"]),
                        "status_counts": dict(Counter(c["protocol_status"] for c in tclk_result["contracts"])),
                        "verdict_counts": dict(Counter(v["verdict"] for v in tclk_result["records"]))}
        review_queue = [c for c in changes if c["change"] != "UNCHANGED"]
        situation = reports.envelope("situation-report", meta, {
            "sources": list(statuses.values()), "radar": radar, "tclk": tclk_summary,
            "opportunities": sorted(opps, key=lambda o: (not o["actionable"], o["kind"], o["opportunity_id"])),
            "review_queue": review_queue,
            "unreviewed_total": None if replay else sum(
                1 for f in all_findings if f["management"]["human_review"]["decision"]
                in ("UNREVIEWED", "STALE_RE_REVIEW_REQUIRED")),
            "findings_scope": ("REPLAY_DERIVED: findings reconstructed at as-of; the live finding lifecycle "
                               "was neither read nor changed" if replay else "LIVE"),
            "coverage": coverage, "events": [{k: v for k, v in e.items() if k != "findings"} for e in event_results],
            "semantic": sem_summary, "report_candidates_written": candidates_written,
            "limitations": [
                "Archive lines are canonical re-serialized JSON, not HTTP raw bytes; byte-exact receipt is not claimed.",
                "Protocol status and actor history cover observed rooms/sources only.",
                "Rail/settlement, delivery and quality are never inferred from chat frames.",
                "Semantic (LLM) results are Inferred and never change protocol state or Human-owned status.",
                "Short offline tests do not establish long-run stability or zero false positives."]})
        _write(tmp / "situation.json", situation)
        _write(tmp / "situation.ja.md", reports.situation_md(situation))
        _write(tmp / "findings.json", reports.envelope("findings", meta, {"findings": all_findings, "changes": changes}))
        _write(tmp / "opportunities.json", reports.envelope("opportunities", meta, {"opportunities": situation["opportunities"]}))
        _write(tmp / "tclk.json", reports.envelope("protocol-tracker", meta, tclk_result))
        _write(tmp / "actors.json", reports.envelope("actor-profiles", meta, actor_view))
        _write(tmp / "coverage.json", reports.envelope("coverage-advisory", meta, {"advisories": coverage}))
        _write(tmp / "notifications.jsonl", "".join(canonical(n) + "\n" for n in notices))
        _write(tmp / "notification-suppression.json", suppression)
        final = out_root / "runs" / run_id
        os.replace(tmp, final)
        replaced = True
        summary = {"status": run_status, "findings_changed": len(review_queue), "opportunities": len(opps),
                   "contracts": tclk_summary["contracts"], "coverage_advisories": len(coverage),
                   "semantic": sem_summary["status"], "records_cached": len(records)}
        with store.transaction():
            for row in candidate_rows:
                store.record_candidate(*row, run_id)
            store.finish_run(run_id, run_status, input_sha, str(final), summary)
        _publish_latest(out_root, {"run_id": run_id, "status": run_status, "path": f"runs/{run_id}"})
        return {"run_id": run_id, **summary, "output": str(final)}
    except BaseException as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        if replaced:
            # The run is recorded FAILED below, so its output directory must not outlive it
            # (this also covers a failure after the DB commit, e.g. writing latest.json).
            shutil.rmtree(out_root / "runs" / run_id, ignore_errors=True)
        store.finish_run(run_id, "FAILED", None, None, {}, type(exc).__name__)
        raise
