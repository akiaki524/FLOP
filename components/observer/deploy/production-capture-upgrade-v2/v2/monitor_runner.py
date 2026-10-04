"""Candidate Monitor evaluation with a closed V2 packet seam and exact-ID fatal stop."""
import argparse
import ast
import importlib
import json
import os
import tempfile
from pathlib import Path
import signal
import sys

# -I excludes script directory; bind only this root-owned staged package.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from v2.common import (Layout, Runner, V2Error, container_id, read_json, read_bytes,
                           require, sha256, docker_inspect, write_json_once, timer_state, sync_dir)
    from v2.preflight import require_host, v2_mount_check
    from v2.stop import stop_both, stopped, identity
else:
    from .common import (Layout, Runner, V2Error, container_id, read_json, read_bytes,
                         require, sha256, docker_inspect, write_json_once, timer_state, sync_dir)
    from .preflight import require_host, v2_mount_check
    from .stop import stop_both, stopped, identity

ALLOWED = frozenset({"E", "mounted_check", "read_json", "receipt", "require",
                     "require_host", "stop_roles", "sync_dir", "write_new"})


class StopSignal(BaseException):
    pass


def pair_already_stopped(runner, binding):
    """Read exact identities; a manual/intended stop is never a recovery request."""
    for role in ("capture", "archive"):
        cid = binding[role + "_id"]
        try:
            row = docker_inspect(runner, cid)
        except V2Error:
            return False  # Unknown is not stopped; normal evaluation tolerates Docker reads.
        if (row is None or not identity(row, cid=cid, role=role,
                image=binding["image"], attempt=binding["attempt"], release=binding["release"]) or
                row.get("State", {}).get("Running") is not False or
                row.get("State", {}).get("Pid") != 0):
            return False
    return True


def save_terminal_status(path, evidence):
    """Atomically update current status; never overwrite first terminal Evidence."""
    fd, pending = tempfile.mkstemp(prefix=".monitor-terminal-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(evidence, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


def exact_stop_confirmed(results):
    return (isinstance(results, dict) and set(results) == {"capture", "archive"}
            and stopped(results))


def terminal_result(runner, layout, binding, evidence, *, stop_confirmed=False):
    """Persist stop outcome before quiescing only a confirmed stopped pair."""
    evidence = {**evidence, "binding": binding, "verdict": "FAIL",
                "stop_confirmed": stop_confirmed, "retry_required": not stop_confirmed,
                "timer_quiesced": False}
    path = layout.state(binding["attempt"]) / "monitor-terminal.json"
    latest = path.with_name("monitor-terminal-latest.json")
    try:
        if not path.exists():
            write_json_once(path, evidence)
        else:
            require(read_json(path).get("binding") == binding, "MONITOR_TERMINAL_BINDING")
        save_terminal_status(latest, evidence)
        evidence["terminal_evidence"] = "PRESERVED"
    except Exception:
        evidence["terminal_evidence"] = "SAVE_FAILED"
        print(json.dumps(evidence, sort_keys=True))
        return 2  # Keep scheduling available when durable Evidence is unconfirmed.
    if stop_confirmed:
        # Fixed V2 scheduler only. No reset-failed, restart or arbitrary unit input.
        try:
            result = runner.run(["systemctl", "stop", "technocore-capture-monitor.timer"])
            evidence["timer_quiesced"] = result.returncode == 0 and timer_state(runner) == "inactive"
        except Exception:
            evidence["timer_quiesced"] = False
        try:
            save_terminal_status(latest, evidence)
        except Exception:
            evidence["terminal_evidence"] = "STATUS_SAVE_FAILED"
    print(json.dumps(evidence, sort_keys=True))
    return 2


def packet_attributes(source):
    tree = ast.parse(source)
    return frozenset(node.attr for node in ast.walk(tree)
                     if isinstance(node, ast.Attribute) and
                     isinstance(node.value, ast.Name) and node.value.id == "packet")


def load_candidate(layout, attempt):
    release = layout.release(attempt)
    helper = release / "source/deploy/production-capture-human"
    manifest = read_json(release / "release-manifest.json")
    for name in ("monitor", "packet", "policy", "readonly"):
        relative = "deploy/production-capture-human/" + name + ".py"
        require(sha256(read_bytes(helper / (name + ".py"))) == manifest.get(relative),
                "STAGED_CANDIDATE_HASH")
    require(packet_attributes((helper / "monitor.py").read_text()) == ALLOWED,
            "MONITOR_PACKET_SURFACE_CHANGED")
    sys.path.insert(0, str(helper))
    packet = importlib.import_module("packet")
    monitor = importlib.import_module("monitor")
    for name in ("packet", "policy", "readonly", "monitor"):
        module = sys.modules.get(name)
        require(module is not None and Path(module.__file__).resolve() ==
                (helper / (name + ".py")).resolve(), "CANDIDATE_IMPORT_PATH")
    return packet, monitor


class PacketSeam:
    """Nine attributes, no __getattr__ delegate into V1 packet authority."""
    __slots__ = ("E", "mounted_check", "read_json", "receipt", "require",
                 "require_host", "stop_roles", "sync_dir", "write_new", "stop_attempted",
                 "stop_results")

    def __init__(self, packet, *, layout, attempt, runner, binding):
        self.E = layout.state(attempt)             # V2 checkpoint reference, never V1 /etc.
        self.read_json = packet.read_json
        self.write_new = packet.write_new
        self.sync_dir = packet.sync_dir
        self.require = packet.require
        self.require_host = require_host            # V2-owned root/host gate.
        self.mounted_check = lambda: v2_mount_check(runner, layout)
        self.stop_attempted = False
        self.stop_results = None

        def receipt(name):
            require(name == "60-started.json", "MONITOR_RECEIPT_NAME")
            started = read_json(layout.state(attempt) / "activation-started.json")
            require(type(started.get("start_at")) in (int, float) and
                    type(started.get("observation_checkpoint_at")) in (int, float),
                    "MONITOR_START_EVIDENCE")
            return started

        def stop_roles(roles):
            require(roles == ("capture", "archive"), "MONITOR_STOP_ROLES")
            self.stop_attempted = True
            self.stop_results = stop_both(runner, **binding)
            require(stopped(self.stop_results), "MONITOR_EXACT_STOP_FAILED")

        self.receipt = receipt
        self.stop_roles = stop_roles


def run_bound(*, attempt, capture_id, archive_id, image, release, runner=None, layout=None):
    # Parse/bind exact IDs before reading mutable Attempt files.
    container_id(capture_id)
    container_id(archive_id)
    require(isinstance(image, str) and image.startswith("sha256:") and len(image) == 71,
            "MONITOR_IMAGE_ID")
    require(isinstance(release, str) and len(release) == 40, "MONITOR_RELEASE_ID")
    runner, layout = runner or Runner(), layout or Layout()
    binding = {"attempt": attempt, "release": release, "image": image,
               "capture_id": capture_id, "archive_id": archive_id}
    seam = None
    previous_handler = signal.getsignal(signal.SIGTERM)
    emergency_in_progress = False

    def on_term(_signum, _frame):
        if emergency_in_progress or (seam is not None and seam.stop_attempted):
            return  # Let a bounded exact-stop sequence finish within TimeoutStopSec.
        raise StopSignal()

    signal.signal(signal.SIGTERM, on_term)
    try:
        terminal = layout.state(attempt) / "monitor-terminal.json"
        if terminal.exists():
            saved = read_json(terminal)
            require(saved.get("binding") == binding, "MONITOR_TERMINAL_BINDING")
            emergency_in_progress = True
            # A receipt proves fatal history, not current writer state. Recheck
            # exact identities; retry only bound IDs until stop is confirmed.
            already_stopped = pair_already_stopped(runner, binding)
            results = ({"capture": "ALREADY_STOPPED", "archive": "ALREADY_STOPPED"}
                       if already_stopped else stop_both(runner, **binding))
            return terminal_result(runner, layout, binding, {
                **saved, "stop_results": results, "stop_attempted": not already_stopped,
                "retry_suppressed": already_stopped},
                stop_confirmed=exact_stop_confirmed(results))
        if pair_already_stopped(runner, binding):
            # Stopped state alone is not fatal history. Keep observing so a
            # legitimate restart of this exact pair can resume normal sampling.
            print(json.dumps({
                "binding": binding, "verdict": "UNKNOWN",
                "writers_state": "STOPPED", "stop_reason": "UNKNOWN_NOT_RECOVERY",
                "stop_attempted": False, "timer_quiesced": False}, sort_keys=True))
            return 2
        packet, monitor = load_candidate(layout, attempt)
        seam = PacketSeam(packet, layout=layout, attempt=attempt, runner=runner, binding=binding)
        staged = read_json(layout.state(attempt) / "staged.json")
        require(staged.get("container_ids") == {"capture": capture_id, "archive": archive_id}
                and staged.get("image_id") == image and staged.get("release_commit") == release,
                "MONITOR_BINDING_CHANGED")
        monitor.packet = seam
        monitor.DIRECTORY = layout.observation(attempt)
        monitor.GAP_BASELINE = staged["metrics_baseline"]["gaps"]
        monitor.notification_event = lambda _report: {"status": "not_enqueued_outside_v2"}
        result = monitor.tick()
        if result["failures"]:
            emergency_in_progress = True
            return terminal_result(runner, layout, binding, {
                "verdict": "FAIL", "error": "MONITOR_PROTECTIVE_STOP",
                "failures": result["failures"], "stop_results": seam.stop_results},
                stop_confirmed=exact_stop_confirmed(seam.stop_results))
        return 0
    except BaseException:
        # Attempt at most once per invocation; later invocations retry unconfirmed stops.
        if seam is None or not seam.stop_attempted:
            emergency_in_progress = True
            results = stop_both(runner, **binding)
            code = "MONITOR_FAILED_PRESERVE_STATE"
        else:
            results = seam.stop_results
            code = "MONITOR_EXACT_STOP_FAILED"
        return terminal_result(runner, layout, binding, {
            "verdict": "FAIL", "stop_results": results, "error": code},
            stop_confirmed=exact_stop_confirmed(results))
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="V2 bounded Candidate Monitor runner")
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--capture-id", required=True)
    parser.add_argument("--archive-id", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--release", required=True)
    args = parser.parse_args(argv)
    return run_bound(attempt=args.attempt, capture_id=args.capture_id,
                     archive_id=args.archive_id, image=args.image, release=args.release)


if __name__ == "__main__":
    raise SystemExit(main())
