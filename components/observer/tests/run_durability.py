"""Bounded, offline durability matrix. Default prints a plan; no live mode exists."""

import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

import container_isolation as isolation
from durability_diagnostics import OPTIONAL_CONTAINER_LABEL_KEYS, diagnostic
from durability_payload import INIT_POINTS, POINTS, ROOM, require, STRICT_PROFILE, LOCAL_PROFILE, preflight_policy

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "tests" / "durability_payload.py"
ENTRYPOINT = ["python3", "-I", "-B", "/app/tests/durability_payload.py", "--root", "/state", "--container"]
PURPOSE = "technocore-observer.durability"
BASE = "sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"


def private_write(path, data):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def manifest():
    paths = sorted((ROOT / "src" / "technocore_observer").glob("*.py")) + [
        PAYLOAD, ROOT / "tests" / "run_durability.py", ROOT / "tests" / "test_durability.py",
        ROOT / "tests" / "Containerfile.durability", ROOT / "tests" / "container_isolation.py",
        ROOT / "tests" / "durability_diagnostics.py"]
    require(all(p.is_file() and not p.is_symlink() for p in paths), "SOURCE_PATH_REJECTED")
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def container_args(image, name, volume, identity):
    return ["create", "--name=" + name, "--pull=never", "--network=none",
            "--user=65532:65532", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true", "--pids-limit=32", "--memory=256m",
            "--restart=no", "--interactive", "--log-driver=none",
            "--mount=type=volume,source=" + volume + ",target=/state",
            "--label=" + PURPOSE + "=" + identity,
            "--env=HOME=/nonexistent", "--env=PYTHONDONTWRITEBYTECODE=1",
            # Machine-independent host HOME path for the container isolation probe.
            "--env=OBSERVER_HOST_HOME=" + isolation.host_home(), image]


def volume_checks(config, volume, identity):
    return {"exact_volume": config.get("Name") == volume,
            "local_driver": config.get("Driver") == "local" and config.get("Scope") == "local",
            "no_driver_options": not config.get("Options"),
            "exact_labels": config.get("Labels") == {PURPOSE: identity}}


def configuration_checks(config, image, volume, identity, entrypoint=None):
    checks = isolation.configuration_checks(config, image)
    for name in ("no_bind_mounts", "only_state_tmpfs", "no_other_mounts", "expected_entrypoint"):
        checks.pop(name)
    host = config["HostConfig"]
    process = config["Config"]
    mounts = config.get("Mounts") or []
    declared = host.get("Mounts") or []
    labels = process.get("Labels")
    checks.update({
        "exact_state_volume": len(mounts) == 1 and mounts[0].get("Type") == "volume"
            and mounts[0].get("Name") == volume and mounts[0].get("Destination") == "/state"
            and mounts[0].get("Driver") == "local" and mounts[0].get("RW") is True,
        "exact_mount_declaration": len(declared) == 1 and declared[0].get("Type") == "volume"
            and declared[0].get("Source") == volume and declared[0].get("Target") == "/state"
            and not declared[0].get("VolumeOptions"),
        "no_additional_mounts": not host.get("Binds") and not host.get("VolumesFrom") and not host.get("Tmpfs"),
        "expected_entrypoint": process.get("Entrypoint") == (ENTRYPOINT if entrypoint is None else entrypoint) and not process.get("Cmd"),
        # Keep the report check name for compatibility. Only the reviewed
        # Desktop key is optional; its value is never interpreted or trusted.
        "exact_labels": isinstance(labels, dict) and PURPOSE in labels and labels[PURPOSE] == identity
            and set(labels) <= {PURPOSE} | OPTIONAL_CONTAINER_LABEL_KEYS,
        "no_container_logs": host.get("LogConfig", {}).get("Type") == "none",
        "no_links": not host.get("Links") and not host.get("ExtraHosts"),
    })
    return checks


class Session:
    def __init__(self, backend, command, name=None):
        self.backend = backend
        self.command = command
        self.name = name
        self.process = None
        self.last_case = "unopened"
        self.pending = b""
        self.start()

    def start(self):
        self.backend.bound()
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=self.backend.environment, bufsize=0)
        self.backend.sessions.append(self)
        self.pending = b""
        result = self.read()
        if "preflight" in result:
            policy = preflight_policy(result["preflight"], getattr(self.backend, "validation_profile", STRICT_PROFILE))
            self.backend.log.append({"container": self.name, **result, "host_preflight_policy": policy})
            require(result.get("preflight_policy") == policy, "PREFLIGHT_POLICY_MISMATCH")
            require(policy["accepted"], "CONTAINER_MOUNT_OR_PROCESS_BOUNDARY_REJECTED")
            result = self.read()
        elif isinstance(self.backend, DockerBackend):
            raise RuntimeError("CONTAINER_PREFLIGHT_MISSING")
        require(result.get("ready") is True, "DRIVER_NOT_READY")
        sources = manifest()
        expected_sources = {key: value for key, value in sources.items()
                            if key.startswith("src/") or key == "tests/durability_payload.py"}
        require(result.get("source_hashes") == expected_sources, "IMAGE_OR_PAYLOAD_SOURCE_MISMATCH")
        self.backend.log.append({"event": "process_ready", "container": self.name, **result})

    def read(self):
        deadline = min(time.monotonic() + 10, self.backend.deadline)
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in self.pending:
                remaining = deadline - time.monotonic()
                require(remaining > 0 and bool(selector.select(remaining)), "RPC_TIMEOUT")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                require(bool(chunk), "DRIVER_EOF")
                self.pending += chunk
                require(len(self.pending) <= 2 * 1024 * 1024, "RPC_OUTPUT_LIMIT")
        line, self.pending = self.pending.split(b"\n", 1)
        return json.loads(line)

    def send(self, case, op, **args):
        self.backend.bound()
        self.last_case = case
        request = {"case": case, "op": op, **args}
        self.process.stdin.write((json.dumps(request) + "\n").encode())
        self.process.stdin.flush()

    def rpc(self, case, op, ok=True, **args):
        self.send(case, op, **args)
        result = self.read()
        self.backend.log.append({"case": case, "operation": op, "arguments": args,
                                 **{k: v for k, v in result.items() if k != "backup_base64"}})
        require(result.get("ok") is ok, "UNEXPECTED_RPC_RESULT")
        if not ok:
            require(result.get("fixture_calls") == 0, "FAIL_CLOSED_FIXTURE_CALLED")
        return result

    def checkpoint(self, case, op, point, **args):
        self.send(case, op, point=point, **args)
        result = self.read()
        self.backend.log.append({"case": case, "operation": op, **result})
        require(result == {"barrier": point}, "CHECKPOINT_NOT_REACHED")

    def release(self):
        self.process.stdin.write(b"release\n")
        self.process.stdin.flush()
        require(self.read().get("ok") is True, "RELEASE_FAILED")

    def finish(self):
        if self.process.poll() is None:
            # Docker may keep container stdin open when an attached CLI detaches.
            # Explicit fixture RPC makes graceful process exit independent of EOF.
            self.rpc(self.last_case, "shutdown")
        code = self.process.wait(timeout=10)
        self.backend.log.append({"event": "graceful_exit", "container": self.name, "exit_code": code})
        require(code == 0, "GRACEFUL_EXIT_FAILED")
        self.dispose_pipes()

    def stop(self, sig=signal.SIGKILL):
        if self.name:
            self.backend.verify(self.name)
            self.backend.docker(["kill", "--signal=" + signal.Signals(sig).name, self.name])
        else:
            self.process.send_signal(sig)
        code = self.process.wait(timeout=10)
        require(code in ((128 + sig,) if self.name else (-sig,)), "WRONG_SIGNAL_EXIT")
        self.backend.log.append({"event": "signal_exit", "container": self.name,
                                 "signal": signal.Signals(sig).name, "exit_code": code})
        self.dispose_pipes()

    def dispose_pipes(self):
        for stream in (self.process.stdin, self.process.stdout):
            if stream and not stream.closed:
                stream.close()


class LocalBackend:
    def __init__(self, directory):
        self.directory = directory
        self.state = directory / "persistent-state"
        self.state.mkdir(mode=0o700)
        self.deadline = time.monotonic() + 900
        self.sessions = []
        self.log = []
        self.environment = {"PATH": os.environ.get("PATH", os.defpath), "HOME": "/nonexistent"}

    def bound(self):
        require(time.monotonic() < self.deadline, "SUITE_TIME_LIMIT")

    def spawn(self):
        return Session(self, [sys.executable, "-I", "-B", str(PAYLOAD), "--root", str(self.state)])

    def recreate(self, session):
        return self.spawn()

    def cleanup(self, success):
        for session in self.sessions:
            if session.process.poll() is None:
                session.process.kill()
                session.process.wait(timeout=10)
            session.dispose_pipes()


class DockerBackend(LocalBackend):
    validation_profile = STRICT_PROFILE

    @property
    def entrypoint(self):
        require(self.validation_profile in (STRICT_PROFILE, LOCAL_PROFILE), "UNKNOWN_VALIDATION_PROFILE")
        return ENTRYPOINT if self.validation_profile == STRICT_PROFILE else ENTRYPOINT + ["--validation-profile", self.validation_profile]

    def __init__(self, directory, image, identity):
        # No host persistent-state directory: only the dedicated Docker volume.
        self.directory = directory
        self.deadline = time.monotonic() + 900
        self.sessions, self.log, self.names = [], [], []
        self.counter = 0
        self.image, self.identity = image, identity
        self.volume = "observer-durability-" + identity
        self.config = directory / "docker-client"
        self.config.mkdir(mode=0o700)
        self.environment = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(self.config)}
        self.prefix = ["docker", "--config=" + str(self.config), "--host=unix:///var/run/docker.sock"]
        self.volume_fingerprint = None

    def docker(self, args):
        self.bound()
        result = subprocess.run(self.prefix + args, capture_output=True, timeout=min(30, self.deadline - time.monotonic()),
                                env=self.environment, check=True)
        return result.stdout

    def prepare(self):
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", self.image), "LOCAL_IMAGE_ID_REQUIRED")
        config = json.loads(self.docker(["image", "inspect", self.image]))[0]
        require(config["Id"] == self.image and config["Config"].get("Entrypoint") == self.entrypoint,
                "IMAGE_CONFIGURATION_REJECTED")
        require(config["Config"].get("User") == "65532:65532" and not config["Config"].get("Volumes"),
                "IMAGE_RUNTIME_REJECTED")
        # Session.start checks hashes of actual immutable image code before any fixture init.
        self.docker(["volume", "create", "--driver=local", "--label=" + PURPOSE + "=" + self.identity, self.volume])
        self.verify_volume()

    def verify_volume(self):
        config = json.loads(self.docker(["volume", "inspect", self.volume]))[0]
        checks = volume_checks(config, self.volume, self.identity)
        require(all(checks.values()), "VOLUME_REJECTED")
        fingerprint = {key: config.get(key) for key in ("Name", "Driver", "Scope", "CreatedAt", "Labels", "Options")}
        if self.volume_fingerprint is not None:
            require(fingerprint == self.volume_fingerprint, "VOLUME_CHANGED")
        self.volume_fingerprint = fingerprint
        self.log.append({"event": "volume_verified", "volume": fingerprint, "checks": checks})

    def verify(self, name):
        require(name in self.names, "UNOWNED_CONTAINER")
        self.verify_volume()
        config = json.loads(self.docker(["container", "inspect", name]))[0]
        checks = configuration_checks(config, self.image, self.volume, self.identity, self.entrypoint)
        evidence = diagnostic(config, self.image, self.volume, self.identity, checks,
                              getattr(self, "verification_stage", "configuration"), name,
                              self.entrypoint, isolation.ALLOWED_ENV)
        self.log.append({"event": "container_configuration_checked", **evidence})
        self.diagnostic_count = getattr(self, "diagnostic_count", 0) + 1
        private_write(self.directory / ("configuration-%04d.json" % self.diagnostic_count),
                      json.dumps(evidence, ensure_ascii=True, indent=2).encode())
        require(all(checks.values()), "CONTAINER_CONFIGURATION_REJECTED")
        self.log.append({"event": "container_verified", "container": name, "id": config["Id"], "checks": checks})

    def spawn(self):
        self.counter += 1
        name = "observer-durability-" + self.identity + "-" + str(self.counter)
        self.docker(container_args(self.image, name, self.volume, self.identity))
        self.names.append(name)
        self.verify(name)
        return Session(self, self.prefix + ["start", "--attach", "--interactive", name], name)

    def recreate(self, session):
        self.verify(session.name)
        require(session.process.poll() is not None, "REMOVE_RUNNING_CONTAINER_REJECTED")
        self.preserve("before-remove-" + session.name)
        self.docker(["rm", session.name])  # Deliberately no -v, force, or prune.
        self.log.append({"event": "container_removed_volume_retained", "container": session.name})
        self.names.remove(session.name)
        # A monotonic suffix prevents reusing a removed container name.
        return self.spawn()

    def preserve(self, stage):
        private_write(self.directory / (stage + ".json"), json.dumps({
            "events": self.log, "volume": self.volume_fingerprint,
            "image_id": self.image, "containers": list(self.names)}, ensure_ascii=True, indent=2).encode())

    def restart(self, session):
        self.verify(session.name)
        require(session.process.poll() is not None, "RESTART_RUNNING_CONTAINER_REJECTED")
        session.start()
        self.log.append({"event": "same_container_started", "container": session.name})
        return session

    def cleanup(self, success):
        # Keep volume/state even on success. Never change settings to make a probe pass.
        self.verification_stage = "cleanup"
        for name in list(self.names):
            self.verify(name)
            config = json.loads(self.docker(["container", "inspect", name]))[0]
            if config["State"]["Running"]:
                self.docker(["stop", "--time=2", name])
            if success:
                self.preserve("before-cleanup-" + name)
                self.docker(["rm", name])
                self.names.remove(name)
        for session in self.sessions:
            if session.process.poll() is None:
                session.process.wait(timeout=10)
            session.dispose_pipes()


def same(before, after):
    for key in ("state", "messages_sha256", "gaps_sha256", "events"):
        require(before[key] == after[key], "RESTART_CHANGED_" + key.upper())


def preserved(before, after):
    require(after["messages"][:len(before["messages"])] == before["messages"], "SAVED_ROWS_CHANGED")
    require(after["gaps"][:len(before["gaps"])] == before["gaps"], "SAVED_GAPS_CHANGED")


def expected(summary, poll, resolved, status, messages, gaps):
    core = summary["core"]
    require(core == {"room": ROOM, "observer_epoch": 1, "server_generation": 2,
                     "init_anchor_seq": 100, "poll_seq": poll, "resolved_seq": resolved, "status": status},
            "UNEXPECTED_STATE")
    require(len(summary["messages"]) == messages and
            [(g["start_seq"], g["end_seq"], g["status"]) for g in summary["gaps"]] ==
            [(start, end, "OPEN") for start, end in gaps], "UNEXPECTED_ACCOUNTING")
    require(tuple(summary["checks"][key] for key in ("journal_mode", "synchronous", "foreign_keys")) ==
            ("wal", 2, 1), "RUNTIME_PRAGMAS")


def check_permissions(files):
    for name, value in files.items():
        require(value["mode"] == (0o700 if value["directory"] else 0o600)
                and not value["symlink"], "PRIVATE_PERMISSIONS_FAILED")
        require(value["uid"] == files["."]["uid"], "OWNER_MISMATCH")


class Matrix:
    def __init__(self, backend):
        self.backend = backend
        self.cases = []

    def mark(self, case):
        self.cases.append({"case": case, "status": "PASS"})

    def seed(self, case):
        session = self.backend.spawn()
        session.rpc(case, "init")
        initial = session.rpc(case, "open")
        expected(initial["summary"], 100, 100, "INITIALIZED", 0, [])
        check_permissions(initial["files"])
        first = session.rpc(case, "poll", fixture="contiguous")
        require(first["first_since"] == 100, "INIT_SINCE")
        expected(first["summary"], 103, 103, "RUNNING", 3, [])
        gap = session.rpc(case, "poll", fixture="gap")
        require(gap["first_since"] == 103, "GAP_SINCE")
        expected(gap["summary"], 201, 103, "DEGRADED", 5, [(104, 199)])
        return session, gap["summary"]

    def reopen(self, session, case):
        files = session.rpc(case, "files")
        require(files["connection_open"] is False, "POST_KILL_INVENTORY_ORDER")
        check_permissions(files["files"])
        opened = session.rpc(case, "open")
        check_permissions(opened["files"])
        return opened["summary"]

    def backup(self, session, case, before):
        result = session.rpc(case, "snapshot")
        same(before, result["summary"])
        check_permissions(result["files"])
        data = base64.b64decode(result["backup_base64"], validate=True)
        require(hashlib.sha256(data).hexdigest() == result["backup_sha256"], "BACKUP_TRANSFER_HASH")
        path = self.backend.directory / (case + "-backup.sqlite")
        private_write(path, data)  # Only the backup artifact, never the live state DB.
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            require(conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "EXPORTED_BACKUP_INTEGRITY")

    def normal(self):
        case = "graceful-recreate"
        session, before = self.seed(case)
        session.finish()
        session = self.backend.recreate(session)
        same(before, self.reopen(session, case))
        resumed = session.rpc(case, "poll", fixture="resume")
        require(resumed["first_since"] == 201, "RESUME_SINCE")
        expected(resumed["summary"], 203, 103, "DEGRADED", 7, [(104, 199)])
        preserved(before, resumed["summary"])
        second = session.rpc(case, "poll", fixture="second_gap")
        require(second["first_since"] == 203, "SECOND_GAP_SINCE")
        expected(second["summary"], 301, 103, "DEGRADED", 9, [(104, 199), (204, 299)])
        preserved(resumed["summary"], second["summary"])
        self.backup(session, case, second["summary"])
        session.finish()
        session = self.backend.recreate(session)
        same(second["summary"], self.reopen(session, case))
        session.finish()
        self.mark(case)

    def crash(self, fixture, point, sig=signal.SIGKILL):
        case = fixture.replace("_", "-") + "-" + point.replace("_", "-")
        session, before = self.seed(case)
        session.checkpoint(case, "hold" if point == "idle" else "poll", point, fixture=fixture)
        session.stop(sig)
        if isinstance(self.backend, DockerBackend):
            # Same-container restart AND removal/recreation use the original volume.
            session = self.backend.restart(session)
        else:
            session = self.backend.recreate(session)
        after = self.reopen(session, case)
        observed = self.backend.log[-2]["files"]
        if point in POINTS or point == "fixture_wait":
            require(observed.get("state.sqlite-wal", {}).get("size", 0) > 0, "POST_KILL_WAL_NOT_OBSERVED")
        committed = point == "after_commit"
        gap = fixture == "crash_gap"
        poll = (301 if gap else 203) if committed else 201
        gaps = [(104, 199)] + ([(202, 299)] if committed and gap else [])
        expected(after, poll, 103, "DEGRADED", 7 if committed else 5, gaps)
        preserved(before, after)
        if not committed:
            require(before["core"] == after["core"] and before["messages_sha256"] == after["messages_sha256"]
                    and before["gaps_sha256"] == after["gaps_sha256"], "PARTIAL_COMMIT")
        resumed = session.rpc(case, "poll", fixture="empty" if committed else fixture)
        require(resumed["first_since"] == poll, "POST_CRASH_SINCE")
        if fixture in ("crash_gap", "crash_contiguous"):
            expected(resumed["summary"], 301 if gap else 203, 103, "DEGRADED", 7,
                     [(104, 199)] + ([(202, 299)] if gap else []))
        session.finish()
        session = self.backend.recreate(session)
        same(resumed["summary"], self.reopen(session, case))
        session.finish()
        self.mark(case)

    def init_crash(self, point):
        case = point.replace("_", "-")
        session = self.backend.spawn()
        session.checkpoint(case, "init", point)
        session.stop()
        session = self.backend.recreate(session)
        files = session.rpc(case, "files")["files"]
        if point == "init_after_rename":
            require("state.sqlite" in files and "state.sqlite.init" not in files, "INIT_PUBLICATION")
            result = session.rpc(case, "open")
            expected(result["summary"], 100, 100, "INITIALIZED", 0, [])
            session.rpc(case, "close")
        else:
            require("state.sqlite" not in files and "state.sqlite.init" in files, "INIT_PARTIAL_FINAL")
            refused = session.rpc(case, "open", ok=False)
            require(refused.get("error_code") == "STATE_DB_MISSING", "INIT_RESUME_WRONG_FAILURE")
        refused = session.rpc(case, "init", ok=False)
        require(refused.get("error_code") == "INIT_PATH_ALREADY_EXISTS", "INIT_RETRY_WRONG_FAILURE")
        session.finish()
        self.mark(case)

    def terminal(self, fixture, status, repetitions):
        case = "terminal-" + fixture
        session, before = self.seed(case)
        for _ in range(repetitions):
            result = session.rpc(case, "poll", fixture=fixture)
        after = result["summary"]
        require(not result["continuing"] and after["core"]["status"] == status, "TERMINAL_STATUS")
        preserved(before, after)
        require(after["core"]["poll_seq"] == 201, "TERMINAL_CURSOR_ADVANCED")
        session.finish()
        session = self.backend.recreate(session)
        session.rpc(case, "files")
        refused = session.rpc(case, "open", ok=False)
        require(refused.get("error_code") == "STATE_REQUIRES_HUMAN_REVIEW", "TERMINAL_WRONG_FAILURE")
        same(after, session.rpc(case, "peek")["summary"])
        session.finish()
        self.mark(case)

    def fail_closed(self):
        for fault in ("missing", "wrong-room", "application_id", "user_version", "corrupt",
                      "accounting", "directory_mode", "db_mode"):
            case = "fail-" + fault.replace("_", "-")
            session = self.backend.spawn()
            if fault != "missing":
                session.rpc(case, "init")
                if fault != "wrong-room":
                    session.rpc(case, "damage", kind=fault)
            session.finish()
            session = self.backend.recreate(session)
            refused = session.rpc(case, "open", ok=False, room="other-room" if fault == "wrong-room" else ROOM)
            expected_code = {"missing": "STATE_DIRECTORY_MISSING", "wrong-room": "STATE_ROOM_MISMATCH",
                             "application_id": "INVALID_APPLICATION_ID", "user_version": "INVALID_SCHEMA_VERSION",
                             "accounting": "STATE_CURSOR_MISMATCH", "directory_mode": "UNSAFE_STATE_PERMISSIONS",
                             "db_mode": "UNSAFE_STATE_PERMISSIONS"}.get(fault)
            require(refused.get("error_code") == expected_code if expected_code else
                    refused["error_class"] == "DatabaseError", "STATE_WRONG_FAILURE")
            session.finish()
            self.mark(case)

    def lock(self):
        case = "single-writer"
        first, before = self.seed(case)
        first.checkpoint(case, "hold", "idle")
        second = self.backend.spawn()
        result = second.rpc(case, "open", ok=False)
        require(result["error_class"] == "BlockingIOError", "SECOND_WRITER_NOT_LOCKED_OUT")
        second.finish()
        first.release()
        result = first.rpc(case, "poll", fixture="resume")
        require(result["first_since"] == 201, "LOCK_HOLDER_CURSOR")
        preserved(before, result["summary"])
        first.finish()
        self.mark(case)

    def container_stop_start(self):
        if not isinstance(self.backend, DockerBackend):
            return
        case = "container-stop-start"
        session, before = self.seed(case)
        session.checkpoint(case, "hold", "idle")
        self.backend.verify(session.name)
        self.backend.docker(["stop", "--time=2", session.name])
        require(session.process.wait(timeout=10) in (137, 143), "CONTAINER_STOP_EXIT")
        session.dispose_pipes()
        self.backend.restart(session)
        same(before, self.reopen(session, case))
        session.finish()
        self.mark(case)

    def run(self):
        self.normal()
        for fixture in ("crash_contiguous", "crash_gap"):
            for point in POINTS:
                if point == "gap_inserted" and fixture == "crash_contiguous":
                    continue
                self.crash(fixture, point)
        for point in ("idle", "fixture_wait"):
            self.crash("crash_contiguous", point, signal.SIGTERM)
        for point in INIT_POINTS:
            self.init_crash(point)
        self.terminal("generation", "NEEDS_RESYNC", 1)
        self.terminal("anomaly", "ERROR", 3)
        self.fail_closed()
        self.lock()
        self.container_stop_start()
        return self.cases


def plan():
    return {"scope": "offline fixtures only; no live mode", "external_requests": 0,
            "max_seconds": 900, "barrier_timeout_seconds": 10, "docker_operation_timeout_seconds": 30,
            "base_image": BASE, "container_image": "explicit local sha256 ID; no pulls",
            "local_command": ["python3", "-B", "tests/run_durability.py", "--execute-local"],
            "docker_command": ["python3", "-B", "tests/run_durability.py", "--execute-docker", "--image-id", "<local-image-id>"],
            "container_create": container_args("<local-image-id>", "<test-container>", "<test-volume>", "<test-id>"),
            "volume": "dedicated local named volume, no driver options; retained on all outcomes",
            "mount_gate": "rw/noexec/nosuid/nodev required; standard volume may fail; no workaround",
            "backup": "SQLite backup API, never cp live DB; never restore to prove persistence",
            "docker_build_executed": False, "live_executed": False}


def execute(mode, image=None):
    os.umask(0o077)
    identity = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    directory = ROOT / "docs" / ("durability-results-" + mode + "-" + identity)
    print(json.dumps({"create_path": str(directory)}), flush=True)
    directory.mkdir(mode=0o700)
    sources = manifest()
    private_write(directory / "source-hashes.json", json.dumps(sources, indent=2).encode())
    private_write(directory / "plan.json", json.dumps(plan(), indent=2).encode())
    backend = DockerBackend(directory, image, identity) if mode == "docker" else LocalBackend(directory)
    matrix = Matrix(backend)
    started = time.monotonic()
    report = {"status": "INCOMPLETE", "mode": mode, "external_requests": 0,
              "technocore_baseline": "0.12.1", "observer_spec": "0.3",
              "container_durability": "PENDING", "cases": matrix.cases,
              "image_id": image, "events": backend.log}
    try:
        if mode == "docker":
            backend.prepare()
        matrix.run()
        require(manifest() == sources, "SOURCE_CHANGED_DURING_SUITE")
        report["status"] = "OFFLINE_PROCESS_PASS" if mode == "local" else "OFFLINE_CONTAINER_PASS"
        if mode == "docker":
            report["container_durability"] = "PASS"
    except Exception as exc:
        report["status"] = "STOPPED"
        report["error_class"] = type(exc).__name__
        # Only our fixed internal codes; never CalledProcessError command/output.
        if type(exc) is RuntimeError and re.fullmatch(r"[A-Z_]+", str(exc)):
            report["error_code"] = str(exc)
    finally:
        report["volume"] = getattr(backend, "volume_fingerprint", None)
        private_write(directory / "before-cleanup-report.json", json.dumps(report, ensure_ascii=True, indent=2).encode())
        try:
            backend.cleanup(report["status"].endswith("_PASS"))
        except Exception as exc:
            report["status"] = "STOPPED"
            report["cleanup_error_class"] = type(exc).__name__
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["volume"] = getattr(backend, "volume_fingerprint", None)
        private_write(directory / "report.json", json.dumps(report, ensure_ascii=True, indent=2).encode())
    print(json.dumps({"status": report["status"], "case_count": len(matrix.cases),
                      "external_requests": 0, "artifacts": str(directory)}), flush=True)
    return 0 if report["status"].endswith("_PASS") else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute-local", action="store_true")
    mode.add_argument("--execute-docker", action="store_true")
    parser.add_argument("--image-id")
    args = parser.parse_args()
    if args.execute_docker:
        require(args.image_id is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", args.image_id), "LOCAL_IMAGE_ID_REQUIRED")
        return execute("docker", args.image_id)
    if args.execute_local:
        return execute("local")
    print(json.dumps(plan(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
