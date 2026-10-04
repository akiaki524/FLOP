"""Bounded soak controller. Default is plan-only; --offline never uses network.

Live execution requires --execute --init-mode tail. No pulls or host mounts.
One persistent Observer process and separate watchdog processes share this
controller's sole request gate. No subprocess invokes a shell.
"""

import argparse
import ast
from collections import Counter
import contextlib
import hashlib
import io
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import time
import uuid
import zipfile

import run_container_smoke as smoke
import soak_payload as payload

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024
LIMITS = {"init": 1, "config": 1, "poll": 360, "watchdog": 12}
IDLE = "import time; time.sleep(4200)"
LAUNCH = "import sys; sys.path.insert(0, '/state/observer.zip'); import soak_payload; raise SystemExit(soak_payload.main())"


class Stop(Exception):
    pass


def plan():
    return {"mode": "PLAN_ONLY", "room": payload.ROOM, "baseline": "0.12.1",
            "origin": "https://technocore.chat", "method": "GET",
            "paths": ["/config", "/r/" + payload.ROOM], "max_seconds": 3600,
            "max_gets": 374, "role_limits": LIMITS, "min_pause_seconds": 2,
            "watchdog_interval_seconds": 300, "metrics_interval_seconds": 60,
            "metrics_sampling_policy": "60-second target; sample after current operation completes",
            "snapshot_interval_seconds": 300, "max_snapshots": 13,
            "memory_bytes": 256 * MIB, "tmpfs_bytes": 64 * MIB,
            "image": smoke.BASE_IMAGE, "pull": False, "bind_mounts": [],
            "network": "bridge for live; none for container preflight",
            "backup": "SQLite backup API; captured binary transport; no live DB copy",
            "duration_includes": "init and config; cleanup issues no GET"}


class Gate:
    def __init__(self, duration, clock=time.monotonic):
        if not math.isfinite(duration) or not 0 < duration <= 3600:
            raise Stop("INVALID_DURATION")
        self.clock, self.started = clock, clock()
        self.deadline = self.started + duration
        self.counts, self.not_before, self.inflight = Counter(), self.started, False
        self.stopped = False

    def begin(self, role):
        if self.stopped: raise Stop("ALREADY_STOPPED")
        if role not in LIMITS: raise Stop("BOUNDARY_VIOLATION")
        if self.inflight: raise Stop("CONCURRENT_REQUEST_REJECTED")
        if self.clock() >= self.deadline: raise Stop("TIME_LIMIT")
        if self.clock() < self.not_before: raise Stop("EARLY_REQUEST_REJECTED")
        if sum(self.counts.values()) >= 374 or self.counts[role] >= LIMITS[role]:
            raise Stop("REQUEST_LIMIT")
        self.counts[role] += 1  # Reserve before any IO, including failures/timeouts.
        self.inflight = True

    def finish(self, delay=0):
        self.inflight = False
        if type(delay) not in (int, float) or not math.isfinite(delay) or delay < 0:
            self.stopped = True
            raise Stop("INVALID_DELAY")
        self.not_before = self.clock() + max(2, delay)


def unpack_reply(data):
    if len(data) > 65536:
        raise Stop("RPC_TOO_LARGE")
    obj = json.loads(data)
    if not isinstance(obj, dict) or obj.get("ok") is not True or not isinstance(obj.get("result"), dict):
        raise Stop("WORKER_FAILURE")
    return obj["result"]


class RPC:
    def __init__(self, command, env):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=env, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.ready = self.receive(30)
        except BaseException:
            self.close()
            raise

    def receive(self, timeout):
        end, data = time.monotonic() + timeout, bytearray()
        while b"\n" not in data:
            remaining = end - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise Stop("RPC_DEADLINE")
            part = os.read(self.process.stdout.fileno(), 4096)
            if not part: raise Stop("WORKER_EXITED")
            data.extend(part)
            if len(data) > 65536: raise Stop("RPC_TOO_LARGE")
        if data.count(b"\n") != 1 or not data.endswith(b"\n"):
            raise Stop("RPC_FRAMING")
        return unpack_reply(data)

    def call(self, command, timeout=60):
        self.process.stdin.write(json.dumps({"command": command}).encode() + b"\n")
        self.process.stdin.flush()
        return self.receive(timeout)

    def close(self):
        if self.process.poll() is None:
            with contextlib.suppress(OSError):
                self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        self.selector.close()
        for stream in (self.process.stdin, self.process.stdout):
            with contextlib.suppress(OSError): stream.close()


class Artifacts:
    def __init__(self, directory):
        self.directory = directory
        self.log_bytes = 0
        self.snapshots = 0

    def guard(self):
        if shutil.disk_usage(self.directory).free < 512 * MIB:
            raise Stop("HOST_DISK_RESERVE")
        size = sum(p.stat().st_size for p in self.directory.rglob("*") if p.is_file())
        if size >= 256 * MIB: raise Stop("ARTIFACT_LIMIT")
        return {"host_free_bytes": shutil.disk_usage(self.directory).free, "artifact_bytes": size}

    def log(self, item):
        data = json.dumps(item, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode() + b"\n"
        if self.log_bytes + len(data) > 8 * MIB: raise Stop("LOG_LIMIT")
        fd = os.open(self.directory / "metrics.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self.log_bytes += len(data)

    def save_snapshot(self, data, expected):
        if self.snapshots >= 13 or len(data) > 34 * MIB:
            raise Stop("SNAPSHOT_LIMIT")
        capacity = self.guard()
        if capacity["artifact_bytes"] + len(data) > 256 * MIB:
            raise Stop("ARTIFACT_LIMIT")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) != 2 or {e.filename for e in entries} != {"snapshot.sqlite", "snapshot.json"}:
                raise Stop("SNAPSHOT_NAMES_REJECTED")
            if any(e.file_size > 32 * MIB or e.is_dir() or ((e.external_attr >> 16) & 0o170000) == 0o120000 for e in entries):
                raise Stop("SNAPSHOT_TYPE_REJECTED")
            body = archive.read("snapshot.sqlite")
            meta = json.loads(archive.read("snapshot.json"))
        if meta != expected or hashlib.sha256(body).hexdigest() != meta["sha256"] or len(body) != meta["bytes"]:
            raise Stop("SNAPSHOT_HASH_MISMATCH")
        path = self.directory / ("snapshot-%02d.sqlite" % (self.snapshots + 1))
        payload.private_write(path, body)
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            state = dict(payload._verify(conn, payload.ROOM))
            if state != meta["state"]: raise Stop("SNAPSHOT_STATE_MISMATCH")
            counts = {name: conn.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                      for name in ("messages", "gaps", "events", "evidence")}
            counts["flagged_messages"] = conn.execute("SELECT COUNT(*) FROM messages WHERE validation_flags<>'[]'").fetchone()[0]
            if counts != meta["counts"]: raise Stop("SNAPSHOT_COUNTS_MISMATCH")
        payload.private_write(path.with_suffix(".json"), json.dumps(meta, ensure_ascii=True).encode())
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        self.snapshots += 1
        return {"snapshot": path.name, "sha256": meta["sha256"], "bytes": len(body)}


class Transport:
    """Local offline transport, also used unchanged underneath Docker transport."""
    def __init__(self, directory):
        self.directory = directory
        self.offline = True
        self.enforce_resources = False
        self.env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(directory)}
        self.rpc = None

    def command(self, mode):
        return [sys.executable, "-B", str(ROOT / "tests/soak_payload.py"), "--offline",
                "--directory", str(self.directory / "inbox"), "--mode", mode]

    def start(self):
        self.rpc = RPC(self.command("worker"), self.env)
        return self.rpc.ready

    def call(self, command, timeout=60): return self.rpc.call(command, timeout)

    def separate(self, mode, binary=False):
        result = subprocess.run(self.command(mode), env=self.env, capture_output=True, timeout=30 if mode == "export" else 60)
        if result.returncode: raise Stop("CHILD_FAILURE")
        return result.stdout if binary else unpack_reply(result.stdout)

    def watchdog(self): return self.separate("watchdog")
    def export(self): return self.separate("export", True)
    def close(self, remove=False):
        if self.rpc: self.rpc.close()


class DockerTransport(Transport):
    def __init__(self, directory, offline):
        super().__init__(directory)
        self.offline = offline
        self.enforce_resources = True
        self.name = "observer-soak-" + uuid.uuid4().hex[:12]
        config = directory / "docker-client"
        config.mkdir(mode=0o700)
        self.env["HOME"] = str(config)
        self.docker = ["docker", "--config=" + str(config), "--host=unix:///var/run/docker.sock"]
        self.created = False

    def run(self, args, **kwargs):
        result = subprocess.run(self.docker + args, env=self.env, capture_output=True,
                                timeout=kwargs.pop("timeout", 30), **kwargs)
        if result.returncode: raise Stop("DOCKER_OPERATION_FAILED")
        return result.stdout

    def command(self, mode):
        return self.docker + ["exec"] + (["--interactive"] if mode == "worker" else []) + ["--user=65532:65532", self.name,
                             "python3", "-I", "-B", "-c", LAUNCH, "--mode", mode] + (["--offline"] if self.offline else [])

    def start(self):
        image = self.run(["image", "inspect", "--format", "{{.Id}}", smoke.BASE_IMAGE]).decode().strip()
        if image != smoke.BASE_IMAGE: raise Stop("LOCAL_IMAGE_MISMATCH")
        args = smoke.container_command(image, self.name, "offline" if self.offline else "live")[len(smoke.DOCKER):]
        args[-1] = IDLE
        args[args.index("--label=technocore-observer.purpose=offline-isolation-check")] = "--label=technocore-observer.purpose=bounded-soak"
        self.run(args)
        self.created = True
        config = json.loads(self.run(["container", "inspect", self.name]))[0]
        checks = smoke.inspect_checks(config, image, "offline" if self.offline else "live")
        checks["expected_entrypoint"] = config["Config"].get("Entrypoint") == ["python3"] and config["Config"].get("Cmd") == ["-I", "-B", "-c", IDLE]
        payload.private_write(self.directory / "configuration.json", json.dumps(checks).encode())
        if not all(checks.values()): raise Stop("BOUNDARY_VIOLATION")
        self.run(["start", self.name])
        self.run(["exec", "--interactive", "--user=65532:65532", self.name, "python3", "-I", "-B", "-c", smoke.IMPORT_CODE],
                 input=(self.directory / "observer.zip").read_bytes())
        return super().start()

    def close(self, remove=False):
        try:
            super().close()
        finally:
            if self.created:
                try:
                    state = json.loads(self.run(["container", "inspect", "--format", "{{json .State}}", self.name], timeout=10))
                    payload.private_write(self.directory / "container-final-state.json", json.dumps({
                        "container": self.name, "oom_killed": state.get("OOMKilled"),
                        "running_before_stop": state.get("Running"),
                        "exit_code_before_stop": state.get("ExitCode")}).encode())
                    if state.get("OOMKilled"): raise Stop("OOM")
                finally:
                    self.run(["stop", "--time=5", self.name], timeout=10)
                # Failed/incomplete runs keep their stopped container for review.
                if remove: self.run(["rm", self.name], timeout=10)


class Controller:
    def __init__(self, transport, artifacts, duration=3600, clock=time.monotonic, sleep=time.sleep,
                 wall_clock=time.time):
        self.transport, self.artifacts = transport, artifacts
        self.clock, self.sleep = clock, sleep
        self.wall_clock = wall_clock
        self.timing_origin = clock()
        self.phase_times = {name: None for name in (
            "controller_started", "request_window_started", "last_request_dispatched",
            "last_request_rpc_finished", "request_dispatch_closed", "final_backup_completed",
            "cleanup_completed", "cleanup_attempt_finished")}
        self.sample_times = []
        self.duration = duration
        self.gate = None
        self.warning = set()
        self.http = Counter()
        self.held = Counter()
        self.samples = []
        self.initialized = False
        self.last_heartbeat = None
        self.high_memory = 0
        self.snapshot_counts = None

    def mark(self, phase):
        # Wall time is diagnostic only; elapsed times use the existing host clock.
        self.phase_times[phase] = {"unix_seconds": self.wall_clock(),
                                   "controller_elapsed_seconds": self.clock() - self.timing_origin}

    def request(self, role):
        try:
            return self._request(role)
        except BaseException:
            self.gate.stopped = True
            raise

    def _request(self, role):
        self.artifacts.guard()
        self.gate.begin(role)
        result = None
        self.artifacts.log({"kind": "request_reserved", "role": role, "counts": dict(self.gate.counts),
                            "elapsed_seconds": self.clock() - self.gate.started})
        try:
            self.mark("last_request_dispatched")
            result = self.transport.watchdog() if role == "watchdog" else self.transport.call(role)
        finally:
            self.gate.finish((result or {}).get("delay", 0))
            self.mark("last_request_rpc_finished")
        self.artifacts.log({"kind": role, "result": result})
        http = result.get("http") or {}
        status = http.get("http_status")
        self.http[str(status)] += 1
        if status != 200: self.warning.add("HTTP_FAILURE")
        hb = result.get("heartbeat")
        if hb:
            self.last_heartbeat = hb
            if hb["status"] in ("ERROR", "NEEDS_RESYNC") or result.get("continuing") is False:
                raise Stop("TERMINAL_STATE")
            if hb["open_gap_count"]: self.warning.add("OPEN_GAP")
            if hb["consecutive_protocol_anomalies"]: self.warning.add("PROTOCOL_ANOMALY")
        if role == "init":
            self.initialized = True
            if result.get("pragmas") != {"journal_mode": "wal", "synchronous": 2, "foreign_keys": 1}:
                raise Stop("SQLITE_CONFIGURATION")
        if role == "config":
            config = result["config"]
            if config["event"] == "VERSION_CHANGED": raise Stop("VERSION_CHANGED")
            if config["deployment_config"]["validation_flags"]: self.warning.add("CONFIG_FLAGS")
        if role == "watchdog":
            outcome = result["watchdog"]["result"]
            if outcome in ("GENERATION_CHANGE", "OBSERVER_STOPPED"): raise Stop(outcome)
            if outcome == "LAGGING": self.warning.add("WATCHDOG_LAGGING")
        if role == "poll" and hb and not hb["consecutive_failures"]:
            self.held["valid_polls"] += 1
            self.held[str(http.get("wait_held"))] += 1
        return result

    def sample(self):
        metrics = self.transport.call("metrics")
        host = self.artifacts.guard()
        sampled_at = self.clock()
        interval = sampled_at - self.sample_times[-1] if self.sample_times else None
        self.sample_times.append(sampled_at)
        self.artifacts.log({"kind": "resources", "elapsed_seconds": sampled_at - self.gate.started,
                            "sample_interval_seconds": interval,
                            **metrics, **host})
        self.samples.append((sampled_at - self.gate.started, metrics["rss_bytes"]))
        if self.transport.enforce_resources:
            memory, oom = metrics["container_memory_bytes"], metrics["oom_count"]
            if memory is None or oom is None: raise Stop("MEMORY_MEASUREMENT_UNAVAILABLE")
            if oom: raise Stop("OOM")
            if metrics["disk_free_bytes"] < 48 * MIB: raise Stop("DISK_RESERVE")
            high = max(memory, metrics["rss_bytes"]) >= 192 * MIB
            self.high_memory = self.high_memory + 1 if high else 0
            if self.high_memory >= 2: raise Stop("MEMORY_LIMIT")
        hb = metrics.get("heartbeat")
        if hb:
            self.last_heartbeat = hb
            if hb["status"] in ("ERROR", "NEEDS_RESYNC"): raise Stop("TERMINAL_STATE")
            if hb["last_valid_response_at"]:
                age = time.time() - hb["last_valid_response_at"]
                if age > 120: self.warning.add("VALID_RESPONSE_STALE")
                if age > 600: raise Stop("VALID_RESPONSE_STALL")

    def backup(self):
        meta = self.transport.call("snapshot", timeout=30)
        saved = self.artifacts.save_snapshot(self.transport.export(), meta)
        self.snapshot_counts = meta["counts"]
        if meta["counts"]["flagged_messages"]: self.warning.add("CONTENT_FLAGS")
        self.artifacts.log({"kind": "backup", **saved})
        self.transport.call("ack_snapshot", timeout=10)

    def run(self):
        self.timing_origin = self.clock()
        self.mark("controller_started")
        report = {"status": "STOPPED", "final_snapshot_saved": False, "offline": self.transport.offline}
        try:
            self.artifacts.guard()
            ready = self.transport.start()
            if not ready.get("ready") or not all(ready.get("checks", {}).values()):
                raise Stop("BOUNDARY_VIOLATION")
            self.artifacts.log({"kind": "preflight", **ready})
            self.gate = Gate(self.duration, self.clock)
            self.mark("request_window_started")
            self.sample()  # Resource availability is checked before the first GET.
            self.request("init")
            self.wait_until(self.gate.not_before)
            self.request("config")
            next_sample = self.clock()
            next_watchdog = next_backup = self.gate.started + 300
            while self.clock() < self.gate.deadline:
                now = self.clock()
                if now >= next_sample:
                    self.sample()
                    next_sample = self.clock() + 60
                if now >= next_backup and self.artifacts.snapshots < 12:
                    self.backup()
                    next_backup = self.clock() + 300 if self.artifacts.snapshots < 12 else float("inf")
                if self.clock() >= self.gate.not_before:
                    if self.clock() >= next_watchdog and self.gate.counts["watchdog"] < 12:
                        self.request("watchdog")
                        next_watchdog = self.clock() + 300
                    else:
                        self.request("poll")
                self.wait_until(min(self.gate.not_before, next_sample, next_backup, self.gate.deadline))
            report["reason"] = "TIME_LIMIT"
            report["status"] = "WARN" if self.warning else "PASS"
            if self.transport.offline: report["status"] = "OFFLINE_PASS"
            elif self.duration < 3600: report["status"] = "PARTIAL"
        except Exception as exc:
            report["reason"] = str(exc) if type(exc) is Stop else type(exc).__name__
        finally:
            if self.gate:
                self.gate.stopped = True
                self.mark("request_dispatch_closed")
            # No GET can occur in cleanup. Snapshot uses SQLite backup, not cp.
            if self.initialized:
                try:
                    self.backup()
                    report["final_snapshot_saved"] = True
                    self.mark("final_backup_completed")
                except Exception as exc:
                    report["status"] = "STOPPED"
                    report["backup_error"] = type(exc).__name__
            early = [v for t,v in self.samples if 60 <= t <= 960]
            late = [v for t,v in self.samples if t >= 2700]
            if early and late and statistics.median(late) - statistics.median(early) > 32 * MIB:
                self.warning.add("RSS_GROWTH")
                if report["status"] == "PASS": report["status"] = "WARN"
            report.update(request_count=sum(self.gate.counts.values()) if self.gate else 0,
                          role_counts=dict(self.gate.counts) if self.gate else {},
                          http_status_counts=dict(self.http), wait_held_counts=dict(self.held),
                          warnings=sorted(self.warning), heartbeat=self.last_heartbeat,
                          snapshots=self.artifacts.snapshots,
                          snapshot_counts=self.snapshot_counts,
                          resource_sampling={"target_interval_seconds": 60,
                              "sample_count": len(self.sample_times),
                              "max_observed_interval_seconds": max(
                                  (b - a for a, b in zip(self.sample_times, self.sample_times[1:])), default=None)},
                          elapsed_seconds=self.clock() - self.gate.started if self.gate else 0)
            if report["status"] == "PASS" and self.warning: report["status"] = "WARN"
            denominator = self.held["True"] + self.held["False"]
            report["wait_held_false_rate"] = self.held["False"] / denominator if denominator else None
            try:
                self.transport.close(remove=report["final_snapshot_saved"] and report["status"] in ("PASS", "WARN", "OFFLINE_PASS"))
                self.mark("cleanup_completed")
            except Exception as exc:
                report["status"] = "STOPPED"
                report["cleanup_error"] = type(exc).__name__
            self.mark("cleanup_attempt_finished")
            report["phase_times"] = self.phase_times
            # Keep legacy elapsed_seconds (through backup, excluding cleanup).
            report["total_elapsed_seconds"] = self.clock() - self.timing_origin
            payload.private_write(self.artifacts.directory / "report.json", json.dumps(report, ensure_ascii=True, allow_nan=False).encode())
        return report

    def wait_until(self, deadline):
        # Short host waits keep cancellation responsive; no catch-up bursts.
        while self.clock() < min(deadline, self.gate.deadline):
            self.sleep(min(1, min(deadline, self.gate.deadline) - self.clock()))


def archive_sources(directory):
    hashes = {}
    with zipfile.ZipFile(directory / "observer.zip", "x") as archive:
        files = smoke.source_files() + [(ROOT / "tests/soak_payload.py", "soak_payload.py")]
        for path, name in files:
            if path.is_symlink(): raise Stop("SOURCE_PATH_REJECTED")
            data = path.read_bytes()
            ast.parse(data)
            archive.writestr(name, data)
            hashes[name] = hashlib.sha256(data).hexdigest()
    (directory / "observer.zip").chmod(0o644)
    payload.private_write(directory / "source-hashes.json", json.dumps(hashes).encode())
    host_hashes = {name: hashlib.sha256((ROOT / "tests" / name).read_bytes()).hexdigest()
                   for name in ("run_container_soak.py", "run_container_smoke.py", "container_isolation.py")}
    payload.private_write(directory / "host-source-hashes.json", json.dumps(host_hashes).encode())


def new_directory(label, duration=3600):
    directory = ROOT / "docs" / ("soak-results-" + label + "-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    print(json.dumps({"create_path": str(directory)}), flush=True)
    directory.mkdir(mode=0o700)
    payload.private_write(directory / "execution-plan.json", json.dumps({**plan(), "mode": label,
                          "requested_seconds": duration, "explicit_init_mode": "tail"}).encode())
    archive_sources(directory)
    return directory


def cancel(signum, frame):
    raise Stop("CANCELLED")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--execute", action="store_true")
    group.add_argument("--offline", action="store_true")
    group.add_argument("--container-offline", action="store_true")
    p.add_argument("--init-mode", choices=("tail",))
    p.add_argument("--duration-seconds", type=float, default=3600)
    args = p.parse_args()
    if not args.execute and not args.offline and not args.container_offline:
        print(json.dumps(plan(), indent=2))
        return 0
    if args.init_mode != "tail" or not math.isfinite(args.duration_seconds) or not 0 < args.duration_seconds <= 3600:
        print(json.dumps({"status": "STOPPED", "reason": "EXPLICIT_TAIL_AND_BOUNDED_DURATION_REQUIRED"}))
        return 1
    previous_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        signal.signal(signal.SIGINT, cancel)
        signal.signal(signal.SIGTERM, cancel)
        if args.execute:
            directory = new_directory("preflight", 6)
            preflight = Controller(DockerTransport(directory, True), Artifacts(directory), duration=6).run()
            if preflight["status"] != "OFFLINE_PASS": raise Stop("OFFLINE_PREFLIGHT_FAILED")
        directory = new_directory("offline" if args.offline or args.container_offline else "live", args.duration_seconds)
        transport = Transport(directory) if args.offline else DockerTransport(directory, args.container_offline)
        report = Controller(transport, Artifacts(directory), args.duration_seconds).run()
        print(json.dumps({"status": report["status"], "request_count": report["request_count"],
                          "external_requests": 0 if args.offline or args.container_offline else report["request_count"],
                          "artifacts": str(directory)}, ensure_ascii=True))
        return 0 if report["status"] in ("PASS", "WARN", "OFFLINE_PASS") else 1
    except Exception as exc:
        print(json.dumps({"status": "STOPPED", "error_class": type(exc).__name__}), file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous_signals.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
