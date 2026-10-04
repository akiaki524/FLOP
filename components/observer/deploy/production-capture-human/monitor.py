"""Fixed local monitor: no HTTP, no recovery, never stop existing Observers."""

import errno
import json
import os
from pathlib import Path
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import packet
from policy import OBSERVERS, ROOT, digest
from readonly import Reader, decoded, freshness, supervisor_health

DIRECTORY = Path(ROOT) / "control/observation"
NOTIFICATION_DIRECTORY = Path(ROOT) / "control/notification/outbox"
NOTIFICATION_FAILED_DIRECTORY = Path(ROOT) / "control/notification/failed"
NOTIFICATION_SENT_DIRECTORY = Path(ROOT) / "control/notification/sent"
NOTIFICATION_EVIDENCE_MAX_BYTES = 64 * 1024
NOTIFICATION_MAX_TRANSIENT_ATTEMPTS = 5
NOTIFICATION_FINDING_PREFIX = "STOP_NOTIFICATION_UNDELIVERED:"
HISTORY_LIMIT = 16 * 1024**2
HISTORY_RETENTION_SECONDS = 10 * 24 * 60 * 60
HISTORY_BACKUP_INTERVAL_SECONDS = 7 * 24 * 60 * 60
HISTORY_FINDING_PREFIX = "MONITOR_HISTORY_UNAVAILABLE:"
OBSERVER_FINDING_PREFIXES = (
    "OBSERVER_HEALTH_OR_FRESHNESS:",
    "OBSERVER_BASELINE_DEGRADED:",
)
OBSERVER_FINDINGS = {"OBSERVER_SUPERVISOR_FAILED"}
MATERIAL_GAP_GROWTH = 10
MATERIAL_GAP_STREAK = 2
GAP_BASELINE = None


def observer_finding(value):
    return value in OBSERVER_FINDINGS or value.startswith(OBSERVER_FINDING_PREFIXES)


def notification_metadata(path, *, directory_fd=None):
    """Read a bounded local receipt and retain only its non-secret fields."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory_fd)
        row = os.fstat(fd)
        if not stat.S_ISREG(row.st_mode) or row.st_size > NOTIFICATION_EVIDENCE_MAX_BYTES:
            return None
        raw = os.read(fd, NOTIFICATION_EVIDENCE_MAX_BYTES + 1)
        if len(raw) > NOTIFICATION_EVIDENCE_MAX_BYTES:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None
        # Do not return unknown fields: old or malformed evidence may contain
        # response text or other data that must never reach Monitor output.
        return {key: value.get(key) for key in (
            "event_id", "status", "terminal", "attempts", "capture_affected")}
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def terminal_notification_count(failed_directory=None, sent_directory=None):
    """Count terminal undelivered events without inspecting event or webhook data."""
    failed_directory = (NOTIFICATION_FAILED_DIRECTORY if failed_directory is None
                        else Path(failed_directory))
    sent_directory = (NOTIFICATION_SENT_DIRECTORY if sent_directory is None
                      else Path(sent_directory))
    count = 0
    failed_fd = sent_fd = None
    try:
        failed_fd = os.open(failed_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            sent_fd = os.open(sent_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            sent_fd = None
        with os.scandir(failed_fd) as entries:
            for entry in entries:
                name = entry.name
                if not name.endswith(".json"):
                    continue
                event_id = name[:-5]
                if (len(event_id) != 16 or name != event_id + ".json" or
                        any(char not in "0123456789abcdef" for char in event_id)):
                    continue
                failed = notification_metadata(name, directory_fd=failed_fd)
                if (failed is None or failed.get("event_id") != event_id or
                        failed.get("status") != "failed"):
                    continue
                sent = (notification_metadata(name, directory_fd=sent_fd)
                        if sent_fd is not None else None)
                if (sent is not None and sent.get("event_id") == event_id and
                        sent.get("status") == "sent"):
                    continue
                attempts = failed.get("attempts")
                # Legacy evidence predates the explicit terminal field. Reaching
                # the bounded retry limit proves exhaustion, but says nothing
                # about the missing HTTP status and is not inferred here.
                legacy_exhausted = (failed.get("terminal") is None and
                                    type(attempts) is int and
                                    attempts >= NOTIFICATION_MAX_TRANSIENT_ATTEMPTS)
                if failed.get("terminal") is True or legacy_exhausted:
                    count += 1
        return count
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return 0
    finally:
        for fd in (sent_fd, failed_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def gap_growth(current, previous, now):
    """Measure new gaps in one uninterrupted run of increasing samples."""
    prior = previous.get("metrics", {}).get("capture", {}).get("producer", {}).get("gaps")
    if not previous.get("samples"):
        prior = GAP_BASELINE
    if type(prior) not in (int, float):
        return {"previous_gaps": None, "current_gaps": current, "delta": None,
                "streak": 0, "streak_gaps": 0}
    delta = current - prior
    if delta <= 0:
        return {"previous_gaps": prior, "current_gaps": current, "delta": delta,
                "streak": 0, "streak_gaps": 0}
    old = previous.get("gap_growth", {})
    previous_at = previous.get("observed_at")
    continuing = ((old.get("delta") or 0) > 0 and
                  type(previous_at) in (int, float) and 0 <= now - previous_at <= 180)
    return {"previous_gaps": prior, "current_gaps": current, "delta": delta,
            "streak": old.get("streak", 0) + 1 if continuing else 1,
            "streak_gaps": old.get("streak_gaps", 0) + delta if continuing else delta}


def observers(now=None):
    now = time.time() if now is None else now
    reader = Reader()
    supervisor = supervisor_health(reader)
    result = {}
    for name in OBSERVERS:
        state = decoded(reader.command(["docker", "inspect", "--format", "{{json .State}}", name]), {})
        logs = reader.command(["docker", "logs", "--timestamps", "--since", "10m", "--tail", "1000", name], optional=True)
        result[name] = {"running": state.get("Running") is True,
                        "oom_killed": state.get("OOMKilled") is True,
                        "freshness": supervisor["freshness"].copy(),
                        "container_log_freshness": freshness(logs or "", now)}
    return {"containers": result, **supervisor,
            "read_errors": reader.errors}


def metrics(role):
    p = Path(ROOT) / "control/registry" / ("lobby." + role + ".metrics.json")
    try:
        x = packet.read_json(p)
    except (OSError, ValueError):
        return {}
    result = {k: x.get(k) for k in (
        "observed_at", "sampled_at", "process_start_count", "archive_lag_entries", "archive_lag_seconds",
        "last_successful_response", "planning_runway_seconds", "db_bytes", "wal_bytes", "free_inodes",
        "disk_free_bytes", "requests_process", "http_429_process", "deadline_failures_process",
        "get_latency_seconds", "budget_wait_seconds_process") if type(x.get(k)) in (int, float)}
    result["service_status"] = x.get("service_status") if x.get("service_status") in (
        "STARTING", "RUNNING", "STOPPED") else "UNKNOWN"
    result["producer"] = {k: v if type(v) in (int, float) else None
                          for k in ("messages", "gaps", "cursor", "high_entry")
                          for v in (x.get("producer", {}).get(k),)}
    return result


def evaluate(sample, start_at):
    failures, findings = [], []
    now = sample["observed_at"]
    o = sample["observers"]
    for name, row in o["containers"].items():
        if not row["running"] or row["oom_killed"] or row["freshness"]["verdict"] != "PASS":
            findings.append("OBSERVER_HEALTH_OR_FRESHNESS:" + name)
    if (o["timer"].get("ActiveState") != "active" or o["service"].get("Result") != "success"
            or o["service"].get("ExecMainStatus") != "0"):
        findings.append("OBSERVER_SUPERVISOR_FAILED")
    if set(o["health"]) != {"rules", "results"}:
        findings.append("SUPERVISOR_DETAIL_NOT_OBSERVED")
    for name, row in o["health"].items():
        if row != {"health": "OK", "lag_streak": 0, "action": "none"}:
            findings.append("OBSERVER_BASELINE_DEGRADED:" + name)
    if now - start_at >= 180:
        for role, row in sample["metrics"].items():
            if row.get("service_status") != "RUNNING" or not 0 <= now - row.get("observed_at", 0) <= 120:
                failures.append("CAPTURE_SERVICE_OR_METRICS_STALE:" + role)
            if row.get("planning_runway_seconds", 0) < 7200:
                failures.append("RESOURCE_RUNWAY_LOW:" + role)
        capture = sample["metrics"]["capture"]
        if now - capture.get("last_successful_response", 0) > 180:
            failures.append("CAPTURE_RESPONSE_STALE")
        gap_count = capture.get("producer", {}).get("gaps") or 0
        if gap_count:
            findings.append("CAPTURE_GAPS_RECORDED")
        growth = sample.get("gap_growth", {})
        delta = growth.get("delta")
        if type(delta) in (int, float) and delta > 0:
            findings.append("CAPTURE_GAP_GROWTH:+" + str(delta))
        if ((type(delta) in (int, float) and delta >= MATERIAL_GAP_GROWTH) or
                (growth.get("streak", 0) >= MATERIAL_GAP_STREAK and
                 growth.get("streak_gaps", 0) >= MATERIAL_GAP_GROWTH)):
            failures.append("MATERIAL_CAPTURE_GAPS")
        lag = capture.get("archive_lag_seconds", 0)
        if lag > 60:
            findings.append("ARCHIVE_LAG_OVER_60S")
        if lag > 300:
            failures.append("ARCHIVE_LAG_OVER_300S")
        for field in ("http_429_process", "deadline_failures_process"):
            if capture.get(field, 0): findings.append(field.upper())
        if capture.get("get_latency_seconds", 0) > 10:
            findings.append("GET_LATENCY_OVER_10S")
    return failures, findings


def replace_json(path, obj):
    pending = path.with_suffix(path.suffix + ".pending")
    packet.write_new(pending, json.dumps(obj, sort_keys=True) + "\n", 0o600)
    os.replace(pending, path)
    packet.sync_dir(path.parent)


def append_history(report):
    # Preserve every generation until verified external backup can authorize
    # deletion. The 16 MiB limit bounds each file, not unbacked historical total.
    history = DIRECTORY / "history-current.jsonl"
    previous = DIRECTORY / "history-previous.jsonl"
    raw = (json.dumps(report, sort_keys=True) + "\n").encode()
    if len(raw) > HISTORY_LIMIT:
        return "MONITOR_HISTORY_UNAVAILABLE:SAMPLE_TOO_LARGE"
    for path in (history, previous):
        packet.require(not path.is_symlink() and
                       (not path.exists() or stat.S_ISREG(path.stat().st_mode)),
                       "MONITOR_HISTORY_FILE_TYPE")
    current_size = history.stat().st_size if history.exists() else 0
    if history.exists() and previous.exists() and os.path.samefile(history, previous):
        # Complete an interrupted rotation; the sealed previous retains all bytes.
        history.unlink()
        packet.sync_dir(DIRECTORY)
        current_size = 0
    if current_size + len(raw) > HISTORY_LIMIT:
        if previous.exists():
            sealed = DIRECTORY / ("history-sealed-" + str(time.time_ns()) + ".jsonl")
            # No overwrite, unlink or truncate of historical bytes: both moves
            # publish a durable hard link before removing the old name.
            os.link(previous, sealed)
            packet.sync_dir(DIRECTORY)
            previous.unlink()
            packet.sync_dir(DIRECTORY)
        os.link(history, previous)
        packet.sync_dir(DIRECTORY)
        history.unlink()  # All original bytes remain in the sealed previous file.
        packet.sync_dir(DIRECTORY)
    fd = os.open(history, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def save_evaluation(report, started):
    """Persist the evaluated state before any fail-closed stop is applied."""
    # Attempt activation is a conservative age reference for normal samples,
    # not proof of a backed-up range. No backup receipt is accepted in this fix.
    age = report["observed_at"] - started["start_at"]
    report["history_retention"] = {
        "retention_seconds": HISTORY_RETENTION_SECONDS,
        "backup_interval_seconds": HISTORY_BACKUP_INTERVAL_SECONDS,
        "age_reference_at": started["start_at"],
        "verified_backup": False, "deletion_enabled": False}
    if age > HISTORY_RETENTION_SECONDS:
        report["findings"] = sorted(set(report["findings"] + ["MONITOR_HISTORY_RETENTION_BACKUP_REQUIRED"]))
    elif age >= HISTORY_BACKUP_INTERVAL_SECONDS:
        report["findings"] = sorted(set(report["findings"] + ["MONITOR_HISTORY_BACKUP_REQUIRED"]))
    if report["findings"] and not report["failures"]:
        report["verdict"] = "PASS WITH FINDINGS"
    try:
        warning = append_history(report)
    except OSError as exc:
        # Only local access failures are auxiliary. ENOSPC, EDQUOT, EIO,
        # EROFS and unknown errors still take the protective fatal path.
        if exc.errno not in (errno.EACCES, errno.EPERM):
            raise
        warning = HISTORY_FINDING_PREFIX + errno.errorcode[exc.errno]
    if warning:
        report["findings"] = sorted(set(report["findings"] + [warning]))
        if not report["failures"]:
            report["verdict"] = "PASS WITH FINDINGS"
    replace_json(DIRECTORY / "latest.json", report)
    checkpoint = DIRECTORY / "checkpoint-24h.json"
    now = report["observed_at"]
    if now >= started["observation_checkpoint_at"] and not checkpoint.exists():
        packet.write_new(checkpoint, json.dumps({
            "start_at": started["start_at"], "checkpoint_at": started["observation_checkpoint_at"],
            "evaluated_at": now, "verdict": report["verdict"], "samples": report["samples"],
            "failures": report["failures"], "findings": report["findings"],
            "stop_required": report["stop_required"], "stop_applied": report["stop_applied"],
            "latest_sample_sha256": digest(report), "permission_expired": False,
            "continued": not (report["stop_required"] or report["stop_applied"]),
            "observer_baseline_reference": str(packet.E / "observer-baseline.json"),
            "scope": "sampled local metrics and observer health; not proof of gap-free remote history"
        }, sort_keys=True) + "\n", 0o600)


def notification_event(report):
    """Atomically enqueue one secret-free event for one successful stop transition."""
    failures = sorted(report["failures"])
    fingerprint = digest({"failures": failures})
    identity = {"failure_fingerprint": fingerprint, "observed_at": report["observed_at"],
                "samples": report["samples"]}
    event_id = digest(identity)[:16]
    event = {"version": 1, "kind": "CAPTURE_STOP", "event_id": event_id,
             "failure_fingerprint": fingerprint, "failures": failures,
             "samples": report["samples"], "observed_at": report["observed_at"],
             "stop_applied": True, "state_evidence_preserved": True}
    NOTIFICATION_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = NOTIFICATION_DIRECTORY / (event_id + ".json")
    if path.exists():
        return {"status": "duplicate", "event_id": event_id}
    pending = NOTIFICATION_DIRECTORY / ("." + event_id + ".pending-" + str(os.getpid()))
    try:
        packet.write_new(pending, json.dumps(event, sort_keys=True) + "\n", 0o600)
        # write_new fsyncs the file and directory; replace + final dir fsync
        # makes the visible .json publication atomic for the notifier.
        if path.exists():
            return {"status": "duplicate", "event_id": event_id}
        os.replace(pending, path)
        packet.sync_dir(NOTIFICATION_DIRECTORY)
    finally:
        try:
            pending.unlink()
        except FileNotFoundError:
            pass
    return {"status": "queued", "event_id": event_id}


def tick():
    packet.require_host()
    packet.mounted_check()
    started = packet.receipt("60-started.json")
    latest = DIRECTORY / "latest.json"
    previous = packet.read_json(latest) if latest.exists() else {
        "samples": 0, "failures": [], "findings": [], "stop_applied": False}
    sample = {"observers": observers(),
              "metrics": {role: metrics(role) for role in ("capture", "archive")}}
    # A writer may publish during Observer collection or between metrics reads.
    # Compare every collected timestamp with a clock read AFTER collection.
    now = time.time()
    sample["observed_at"] = now
    gap_count = sample["metrics"]["capture"].get("producer", {}).get("gaps") or 0
    sample["gap_growth"] = gap_growth(gap_count, previous, now)
    failures, findings = evaluate(sample, started["start_at"])
    terminal_notifications = terminal_notification_count()
    if terminal_notifications:
        findings.append(NOTIFICATION_FINDING_PREFIX + str(terminal_notifications))
    if not previous["samples"] and now - started["start_at"] > 180:
        findings.append("MONITOR_START_DELAY")
    if previous.get("observed_at") and now - previous["observed_at"] > 180:
        findings.append("MONITOR_SAMPLING_GAP")
    if now >= started["observation_checkpoint_at"] and previous["samples"] + 1 < 1296:
        findings.append("INSUFFICIENT_CHECKPOINT_COVERAGE")
    # Observer health remains durable evidence, but it is not a fatal predicate
    # for the independently healthy Capture/Archive path. Normalize reports
    # written by the previous policy without weakening any Capture-local gate.
    previous_observer = [x for x in previous["failures"] if observer_finding(x)]
    previous_fatal = [x for x in previous["failures"] if not observer_finding(x)]
    failures = sorted(set(previous_fatal + failures))
    # Notification delivery is current status, so replace an older count on
    # every sample instead of preserving a stale diagnostic forever.
    # History warnings stay latched as Evidence of missed samples, even when
    # the next append succeeds. They do not require a writer stop.
    previous_findings = [x for x in previous["findings"]
                         if not x.startswith(NOTIFICATION_FINDING_PREFIX)]
    findings = sorted(set(previous_findings + previous_observer + findings))
    stopped = previous["stop_applied"]
    stop_required = bool(failures and not stopped)
    report = {**sample, "samples": previous["samples"] + 1, "failures": failures,
              "findings": findings, "stop_required": stop_required, "stop_applied": stopped,
              "verdict": "FAIL" if failures else "PASS WITH FINDINGS" if findings else "PASS"}
    # Ordering invariant: evaluation -> durable Evidence -> stop -> outbox.
    save_evaluation(report, started)
    findings = report["findings"]
    event = None
    if stop_required:
        # These names are exclusively this package's Capture/Archive services.
        packet.stop_roles(("capture", "archive"))
        stopped = True
        report = {**report, "stop_required": False, "stop_applied": True}
        replace_json(latest, report)
        try:
            event = notification_event(report)
        except Exception:
            # Notification storage is deliberately outside Capture health and
            # stop success.  Do not turn alerting failure into monitor failure.
            event = {"status": "queue_failed"}
    print(json.dumps({"observed_at": now, "verdict": report["verdict"], "samples": report["samples"],
                      "failures": failures, "findings": findings, "stop_applied": stopped,
                      "notification_event": event}))
    return report


if __name__ == "__main__":
    try:
        tick()
    except Exception:
        # Fail closed for loss of monitoring/evidence capacity. Never restore,
        # reinitialize, change identities, or modify an Observer.
        try:
            packet.require_host()
            packet.stop_roles(("capture", "archive"))
        finally:
            print('{"verdict":"FAIL","error":"MONITOR_FAILED_PRESERVE_STATE"}')
        raise SystemExit(2)
