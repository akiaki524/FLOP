"""One-shot bounded ACTIVATE, short smoke, and V2-owned rollback."""
import json
from pathlib import Path
import signal
import time

from .artifact import verify_staged_copy
from .common import (ROLES, V2Error, command, docker_inspect, effective_argv,
                     exec_fingerprint, read_bytes, read_json,
                     require, sha256, timer_state, unit_state, write_json_once, write_once)
from .preflight import fresh_before_activation, old_writer_check
from .stage import candidate_packet, verify_writer
from .stop import identity, stop_both, stopped

SMOKE_MIN_SECONDS = 240      # Candidate capture evaluation grace is 180 seconds.
SMOKE_MIN_SAMPLES = 3
SMOKE_MAX_SECONDS = 600
STOP_TIMEOUT = 40
MONITOR_STOP_GRACE = 300     # Two serial stops: each 30s inspect + 55s stop + 30s verify, plus margin.
WRITER_READY_SECONDS = 15
WRITER_READY_POLL_SECONDS = 1


class ActivationSignal(BaseException):
    pass


def switch_text(layout, attempt, staged, role):
    root = layout.release(attempt)
    ids = staged["container_ids"]
    if role in ROLES:
        cid = ids[role]
        return ("[Service]\nExecStartPre=\n"
                f"ExecStartPre=/usr/bin/python3 -I -B {root}/v2/writer_precheck.py {attempt} {role} {cid}\n"
                "ExecStart=\n" f"ExecStart=/usr/bin/docker start --attach {cid}\n"
                "ExecStartPost=\nExecStop=\n"
                f"ExecStop=/usr/bin/docker stop --time {STOP_TIMEOUT} {cid}\n"
                "ExecStopPost=\n")
    require(role == "monitor", "UNIT_ROLE")
    return ("[Service]\nExecStartPre=\nExecStartPost=\nExecStart=\n"
            f"ExecStart=/usr/bin/python3 -I -B {root}/v2/monitor_runner.py"
            f" --attempt {attempt} --capture-id {ids['capture']} --archive-id {ids['archive']}"
            f" --image {staged['image_id']} --release {staged['release_commit']}\n"
            "ExecStop=\nExecStopPost=\n"
            f"KillMode=process\nTimeoutStartSec={MONITOR_STOP_GRACE}s\n"
            f"TimeoutStopSec={MONITOR_STOP_GRACE}s\n")


def switch_bytes(layout, attempt, staged):
    return {role: switch_text(layout, attempt, staged, role).encode()
            for role in (*ROLES, "monitor")}


def _expected(row, *, staged, role):
    return (row is not None and identity(row, cid=staged["container_ids"][role],
            image=staged["image_id"], attempt=staged["attempt_id"],
            role=role, release=staged["release_commit"]))


def verify_staged(runner, layout, attempt, staged):
    require(staged.get("attempt_id") == attempt and
            set(staged.get("container_ids", {})) == set(ROLES), "STAGED_EVIDENCE")
    source = layout.release(attempt) / "source"
    attempt_record = read_json(layout.state(attempt) / "attempt.json")
    verify_staged_copy(layout.release(attempt), attempt_record["manifest_sha256"])
    packet = candidate_packet(source)
    network = staged["baseline"]["network"]["network"]
    for role in ROLES:
        cid = staged["container_ids"][role]
        verify_writer(packet, docker_inspect(runner, cid), cid=cid,
                      image=staged["image_id"], attempt=attempt, role=role,
                      release=staged["release_commit"], network=network)
    require(not (layout.state(attempt) / "terminal.json").exists(), "ATTEMPT_TERMINAL")
    require(not (layout.state(attempt) / "activation-begin.json").exists(), "ACTIVATE_ALREADY_STARTED")



def verify_effective(runner, layout, attempt, staged, baseline_dropins):
    root = layout.release(attempt)
    for role in (*ROLES, "monitor"):
        row = unit_state(runner, role)
        require(row["Restart"] == "no", "EFFECTIVE_RESTART_OR_DROPIN")
        require(row["DropInPaths"].split() ==
                [*baseline_dropins[role], str(layout.switch(role, attempt))],
                "EFFECTIVE_DROPIN_PRECEDENCE")
        require("tc-cap-loop-01-lobby-" not in " ".join(row.values()) and
                "technocore-capture-standing" not in " ".join(row.values()),
                "LEGACY_EFFECTIVE_COMMAND")
        if role in ROLES:
            cid = staged["container_ids"][role]
            require(effective_argv(row["ExecStart"]) ==
                    [f"/usr/bin/docker start --attach {cid}"] and
                    effective_argv(row["ExecStop"]) ==
                    [f"/usr/bin/docker stop --time {STOP_TIMEOUT} {cid}"] and
                    effective_argv(row["ExecStartPre"]) ==
                    [f"/usr/bin/python3 -I -B {root}/v2/writer_precheck.py {attempt} {role} {cid}"] and
                    not effective_argv(row["ExecStartPost"]) and
                    not effective_argv(row["ExecStopPost"]),
                    "EFFECTIVE_WRITER_COMMAND")
        else:
            require(all(not effective_argv(row[key]) for key in
                        ("ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost")),
                    "EFFECTIVE_MONITOR_AUX_COMMAND")
            require(row["KillMode"] == "process" and
                    row["TimeoutStartUSec"] in ("5min", "300s") and
                    row["TimeoutStopUSec"] in ("5min", "300s"),
                    "EFFECTIVE_MONITOR_TIMEOUT_OR_KILLMODE")
            require(effective_argv(row["ExecStart"]) == [
                f"/usr/bin/python3 -I -B {root}/v2/monitor_runner.py"
                f" --attempt {attempt} --capture-id {staged['container_ids']['capture']}"
                f" --archive-id {staged['container_ids']['archive']}"
                f" --image {staged['image_id']} --release {staged['release_commit']}"],
                "EFFECTIVE_MONITOR_COMMAND")


def _running(runner, staged, role):
    row = docker_inspect(runner, staged["container_ids"][role])
    require(_expected(row, staged=staged, role=role), "RUNNING_TARGET_MISMATCH")
    state = row.get("State", {})
    require(state.get("Running") is True and state.get("OOMKilled") is not True and
            isinstance(state.get("Pid"), int) and state["Pid"] > 0,
            "WRITER_NOT_HEALTHY")
    return row


def wait_writer_ready(runner, staged, role, samples, *, monotonic=time.monotonic,
                      sleep=time.sleep):
    """Wait briefly for an exact Writer ID to become running; retain safe diagnostics."""
    start = monotonic()
    deadline = start + WRITER_READY_SECONDS
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            samples.append({"elapsed_seconds": round(monotonic() - start, 3),
                            "deadline_exceeded": True})
            raise V2Error("WRITER_NOT_HEALTHY")
        try:
            row = docker_inspect(runner, staged["container_ids"][role], timeout=remaining)
        except V2Error as exc:
            samples.append({"elapsed_seconds": round(monotonic() - start, 3),
                            "inspect_error": exc.code})
            raise
        if not _expected(row, staged=staged, role=role):
            samples.append({"elapsed_seconds": round(monotonic() - start, 3),
                            "target": "MISMATCH_OR_ABSENT"})
            raise V2Error("RUNNING_TARGET_MISMATCH")
        state = row.get("State", {})
        sample = {"elapsed_seconds": round(monotonic() - start, 3),
                  "running": state.get("Running"), "pid": state.get("Pid"),
                  "status": state.get("Status"), "oom_killed": state.get("OOMKilled"),
                  "exit_code": state.get("ExitCode")}
        samples.append(sample)
        if (sample["running"] is True and sample["oom_killed"] is not True and
                type(sample["pid"]) is int and sample["pid"] > 0):
            return row
        remaining = deadline - monotonic()
        if (sample["oom_killed"] is True or sample["status"] in ("exited", "dead") or
                remaining <= 0):
            raise V2Error("WRITER_NOT_HEALTHY")
        sleep(min(WRITER_READY_POLL_SECONDS, remaining))


def _smoke_sample(runner, layout, attempt, staged, started_at, now):
    for role in ROLES:
        _running(runner, staged, role)
    obs = layout.observation(attempt) / "latest.json"
    if not obs.exists():
        return False
    report = read_json(obs)
    if report.get("stop_applied") or report.get("failures") or report.get("verdict") == "FAIL":
        raise V2Error("SMOKE_MONITOR_FATAL")
    observed = report.get("observed_at")
    if report.get("samples", 0) < SMOKE_MIN_SAMPLES or type(observed) not in (int, float):
        return False
    require(started_at + 180 <= observed <= now and now - observed <= 120,
            "SMOKE_MONITOR_STALE")
    baseline = staged["metrics_baseline"]
    metrics = {}
    for role in ROLES:
        metrics[role] = read_json(layout.shared_root / "control/registry" /
                                  ("lobby." + role + ".metrics.json"))
        stamp = metrics[role].get("observed_at")
        require(type(stamp) in (int, float) and started_at < stamp <= now and
                now - stamp <= 120, "SMOKE_METRICS_STALE")
    capture, archive = metrics["capture"], metrics["archive"]
    return (capture.get("producer", {}).get("messages", -1) > baseline["messages"] and
            archive.get("processed_through", -1) > baseline["archive_processed_through"])


def short_smoke(runner, layout, attempt, staged, started_at, *, clock=time.time, sleep=time.sleep):
    """At least 240s / 3 Monitor samples, with fresh exact-ID and progress checks."""
    deadline = started_at + SMOKE_MAX_SECONDS
    while clock() <= deadline:
        if clock() - started_at >= SMOKE_MIN_SECONDS and _smoke_sample(
                runner, layout, attempt, staged, started_at, clock()):
            return {"duration_seconds": clock() - started_at,
                    "min_samples": SMOKE_MIN_SAMPLES, "verdict": "PASS"}
        sleep(30)
    raise V2Error("SHORT_SMOKE_TIMEOUT")


def writer_units_inactive_without_jobs(runner, *, allow_failed=False):
    """A stopped Docker ID alone cannot prove that a systemd start job is gone."""
    units = {role: "technocore-capture-" + role + ".service" for role in ROLES}
    output = command(runner, ["systemctl", "list-jobs", "--no-legend", "--plain"],
                     "WRITER_JOBS_SHOW")
    for line in output.splitlines():
        parts = line.split()
        if not parts or line.strip() in ("No jobs running.", "0 jobs listed."):
            continue
        require(len(parts) >= 4 and parts[0].isdigit(), "WRITER_JOBS_FORMAT")
        if parts[1] in units.values():
            return False
    allowed = ("inactive", "failed") if allow_failed else ("inactive",)
    return all(unit_state(runner, role)["ActiveState"] in allowed for role in ROLES)


def quiesce_writer_units(runner, layout, attempt, staged, begin, *, start_attempted):
    """Stop V2-bound units before exact-ID stop; never run an unverified old ExecStop."""
    results = {"timer": "UNKNOWN", **{role: "NOT_STARTED" for role in ROLES}}
    try:
        command(runner, ["systemctl", "stop", "technocore-capture-monitor.timer"],
                "WRITER_QUIESCE_TIMER")
        results["timer"] = ("INACTIVE" if timer_state(runner) == "inactive" else "NOT_INACTIVE")
    except (V2Error, OSError, KeyError):
        results["timer"] = "UNKNOWN"
    if start_attempted:
        try:
            for role, digest in begin["switch_sha256"].items():
                require(sha256(read_bytes(layout.switch(role, attempt))) == digest,
                        "WRITER_SWITCH_CHANGED")
            verify_effective(runner, layout, attempt, staged, begin["baseline_dropins"])
        except (V2Error, OSError, KeyError):
            results["effective"] = "UNVERIFIED"
            return results, False
        for role in ROLES:
            try:
                command(runner, ["systemctl", "stop", "technocore-capture-" + role + ".service"],
                        "WRITER_UNIT_STOP", timeout=120)
                results[role] = "STOP_COMMAND_OK"
            except (V2Error, OSError):
                results[role] = "STOP_COMMAND_FAILED"
    try:
        settled = writer_units_inactive_without_jobs(runner, allow_failed=True)
    except (V2Error, OSError, KeyError):
        settled = False
    results["settled"] = settled
    return results, (results["timer"] == "INACTIVE" and settled and
                     (not start_attempted or all(results[role] == "STOP_COMMAND_OK"
                                                 for role in ROLES)))


def rollback_switch(runner, layout, attempt, begin, *, stop_results, unit_stop_ok=True):
    """Only remove this Attempt's identical switch files after exact writers stop."""
    if not stopped(stop_results) or not unit_stop_ok:
        return "RECONCILIATION_REQUIRED"
    try:
        for role, expected in begin["switch_sha256"].items():
            path = layout.switch(role, attempt)
            if (path.exists() or path.is_symlink()) and sha256(read_bytes(path)) != expected:
                return "RECONCILIATION_REQUIRED"
        command(runner, ["systemctl", "stop", "technocore-capture-monitor.timer"],
                "ROLLBACK_TIMER_QUIESCE")
        require(timer_state(runner) == "inactive", "ROLLBACK_TIMER_NOT_INACTIVE")
        require(unit_state(runner, "monitor")["ActiveState"] == "inactive",
                "ROLLBACK_MONITOR_NOT_INACTIVE")
        require(writer_units_inactive_without_jobs(runner, allow_failed=True),
                "ROLLBACK_WRITER_JOB_OR_UNIT")
        for role in ROLES:
            unit = "technocore-capture-" + role + ".service"
            if unit_state(runner, role)["ActiveState"] == "failed":
                command(runner, ["systemctl", "reset-failed", unit], "ROLLBACK_RESET_FAILED")
        require(writer_units_inactive_without_jobs(runner), "ROLLBACK_WRITER_JOB_OR_UNIT")
        for role in begin["switch_sha256"]:
            path = layout.switch(role, attempt)
            if path.exists() or path.is_symlink():
                path.unlink()
        command(runner, ["systemctl", "daemon-reload"], "ROLLBACK_RELOAD")
        for role, baseline in begin["baseline_units"].items():
            actual = unit_state(runner, role)
            for key in ("ExecStartPre", "ExecStart", "ExecStartPost", "ExecStop",
                        "ExecStopPost"):
                require(exec_fingerprint(actual[key]) == baseline[key], "ROLLBACK_BASELINE_MISMATCH")
            require(sha256(actual["DropInPaths"].encode()) == baseline["DropInPaths"],
                    "ROLLBACK_BASELINE_MISMATCH")
            require(actual["DropInPaths"].split() == baseline["DropInPathsList"],
                    "ROLLBACK_DROPIN_LIST_MISMATCH")
            for key in ("Restart", "KillMode", "TimeoutStartUSec", "TimeoutStopUSec"):
                require(actual[key] == baseline[key], "ROLLBACK_UNIT_PROPERTY_MISMATCH")
        if begin["baseline_timer"] == "active":
            command(runner, ["systemctl", "start", "technocore-capture-monitor.timer"],
                    "ROLLBACK_TIMER")
        require(timer_state(runner) == begin["baseline_timer"], "ROLLBACK_TIMER_STATE")
        require(writer_units_inactive_without_jobs(runner), "ROLLBACK_WRITER_JOB_OR_UNIT")
    except (V2Error, OSError, KeyError):
        return "RECONCILIATION_REQUIRED"
    return "ROLLED_BACK_STOPPED_BASELINE"


def activate(runner, layout, *, attempt, human_approved=False,
             clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
    require(human_approved, "HUMAN_ACTIVATION_GATE")
    state = layout.state(attempt)
    staged = read_json(state / "staged.json")
    require(not (state / "terminal.json").exists(), "ATTEMPT_TERMINAL")
    fresh_before_activation(runner, layout, layout.release(attempt) / "source", attempt, staged)
    verify_staged(runner, layout, attempt, staged)
    require(unit_state(runner, "monitor")["ActiveState"] == "inactive", "MONITOR_SERVICE_ACTIVE")
    baseline_dropins = {}
    for role in (*ROLES, "monitor"):
        current = unit_state(runner, role)
        allowed = staged["baseline"]["units"][role]["DropInPathsList"]
        require(sha256(current["DropInPaths"].encode()) ==
                staged["baseline"]["units"][role]["DropInPaths"] and
                current["DropInPaths"].split() == allowed, "BASELINE_DROPIN_CHANGED")
        baseline_dropins[role] = allowed
    expected = switch_bytes(layout, attempt, staged)
    begin = {"attempt_id": attempt, "activation_at": clock(),
             "container_ids": staged["container_ids"],
             "switch_sha256": {role: sha256(data) for role, data in expected.items()},
             "baseline_units": staged["baseline"]["units"],
             "baseline_dropins": baseline_dropins,
             "baseline_timer": staged["baseline"]["timer"]}
    previous_handlers = {signum: signal.getsignal(signum)
                         for signum in (signal.SIGTERM, signal.SIGHUP)}
    cleanup_in_progress = False
    writer_start_attempted = False
    readiness = {}

    def on_term(_signum, _frame):
        if not cleanup_in_progress:
            raise ActivationSignal()

    for signum in previous_handlers:
        signal.signal(signum, on_term)
    try:
        write_json_once(state / "activation-begin.json", begin)
        command(runner, ["systemctl", "stop", "technocore-capture-monitor.timer"],
                "TIMER_QUIESCE")
        require(timer_state(runner) == "inactive", "TIMER_NOT_QUIESCED")
        require(unit_state(runner, "monitor")["ActiveState"] == "inactive",
                "MONITOR_SERVICE_NOT_INACTIVE")
        for role, data in expected.items():
            path = layout.switch(role, attempt)
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            write_once(path, data, mode=0o644)
        command(runner, ["systemctl", "daemon-reload"], "ACTIVATION_RELOAD")
        verify_effective(runner, layout, attempt, staged, baseline_dropins)
        started_at = clock()
        write_json_once(state / "activation-started.json", {"start_at": started_at,
            "observation_checkpoint_at": started_at + 86400})
        writer_start_attempted = True
        command(runner, ["systemctl", "start", "technocore-capture-capture.service"],
                "CAPTURE_START_FAILED", timeout=120)
        readiness["capture"] = []
        wait_writer_ready(runner, staged, "capture", readiness["capture"],
                          monotonic=monotonic, sleep=sleep)
        command(runner, ["systemctl", "start", "technocore-capture-archive.service"],
                "ARCHIVE_START_FAILED", timeout=120)
        readiness["archive"] = []
        wait_writer_ready(runner, staged, "archive", readiness["archive"],
                          monotonic=monotonic, sleep=sleep)
        command(runner, ["systemctl", "start", "technocore-capture-monitor.timer"],
                "MONITOR_TIMER_START_FAILED")
        command(runner, ["systemctl", "start", "technocore-capture-monitor.service"],
                "MONITOR_START_FAILED", timeout=300)
        smoke = short_smoke(runner, layout, attempt, staged, started_at, clock=clock, sleep=sleep)
        write_json_once(state / "terminal.json", {"outcome": "ACCEPTED", "primary_failure": None,
            "short_smoke": smoke, "stop_required": False, "rollback_required": False,
            "final_observed_state": "V2_RUNNING"})
        return "ACCEPTED"
    except BaseException as exc:
        cleanup_in_progress = True
        primary = (exc.code if isinstance(exc, V2Error) else
                   "ACTIVATION_SIGNAL" if isinstance(exc, (ActivationSignal, KeyboardInterrupt))
                   else "ACTIVATION_UNEXPECTED")
        unit_stop_results, unit_stop_ok = quiesce_writer_units(
            runner, layout, attempt, staged, begin, start_attempted=writer_start_attempted)
        stop_results = stop_both(runner, attempt=attempt, release=staged["release_commit"],
            image=staged["image_id"], capture_id=staged["container_ids"]["capture"],
            archive_id=staged["container_ids"]["archive"])
        rollback_result = rollback_switch(runner, layout, attempt, begin,
                                          stop_results=stop_results, unit_stop_ok=unit_stop_ok)
        write_json_once(state / "terminal.json", {"outcome": "ACTIVATION_FAILED_ROLLED_BACK"
            if rollback_result == "ROLLED_BACK_STOPPED_BASELINE" else "RECONCILIATION_REQUIRED",
            "primary_failure": primary, "stop_required": True, "stop_results": stop_results,
            "writer_readiness": readiness,
            "unit_stop_results": unit_stop_results, "stop_applied": stopped(stop_results), "rollback_required": True,
            "rollback_result": rollback_result,
            "final_observed_state": "STOPPED_BASELINE" if rollback_result == "ROLLED_BACK_STOPPED_BASELINE"
                                    else "UNKNOWN_RECONCILE"})
        raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def explicit_rollback(runner, layout, *, attempt, human_approved=False):
    require(human_approved, "HUMAN_ROLLBACK_GATE")
    from .preflight import require_host
    require_host()
    state = layout.state(attempt)
    require(not (state / "rollback.json").exists(), "ROLLBACK_ALREADY_ATTEMPTED")
    staged = read_json(state / "staged.json")
    begin = read_json(state / "activation-begin.json")
    unit_stop_results, unit_stop_ok = quiesce_writer_units(
        runner, layout, attempt, staged, begin, start_attempted=True)
    results = stop_both(runner, attempt=attempt, release=staged["release_commit"],
        image=staged["image_id"], capture_id=staged["container_ids"]["capture"],
        archive_id=staged["container_ids"]["archive"])
    outcome = rollback_switch(runner, layout, attempt, begin,
                              stop_results=results, unit_stop_ok=unit_stop_ok)
    write_json_once(state / "rollback.json", {"outcome": outcome, "stop_results": results,
                                               "unit_stop_results": unit_stop_results})
    return outcome
