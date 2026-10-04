"""Offline-only process/RPC fixture driver. Never used by production Observer."""

import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from technocore_observer.observer import Observer
from technocore_observer.protocol import ObserverError, Reply, content_values, json_dump
from technocore_observer.storage import StateLock, Store, _private, _verify, initialize

ROOM = "durability-fixture"
POINTS = ("http_received", "message_inserted", "gap_inserted",
          "before_cursor_update", "before_commit", "after_commit")
INIT_POINTS = ("init_temp_created", "init_schema", "init_state_inserted",
               "init_committed", "init_before_rename", "init_after_rename")
CORE = ("room", "observer_epoch", "server_generation", "init_anchor_seq",
        "poll_seq", "resolved_seq", "status")
FIXTURES = {"contiguous": (100, [101, 102, 103]), "gap": (103, [200, 201]),
            "resume": (201, [202, 203]), "second_gap": (203, [300, 301]),
            "crash_contiguous": (201, [202, 203]), "crash_gap": (201, [300, 301]),
            "empty": (None, []), "generation": (None, []), "anomaly": (None, [])}


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def digest(value):
    return hashlib.sha256(json_dump(value).encode()).hexdigest()


def source_hashes():
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "src" / "technocore_observer").glob("*.py")) + [Path(__file__).resolve()]
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def record(seq):
    return {"seq": seq, "ts": 1700000000, "from": "untrusted:fixture",
            "text": "fixture-%d\x1b[31m\x00\u202e\n" % seq, "fixture_extra": True}


def envelope(seqs, generation=2):
    return {"room": ROOM, "generation": generation, "count": len(seqs),
            "first_seq": seqs[0] if seqs else 0, "last_seq": seqs[-1] if seqs else 0,
            "messages": [record(seq) for seq in seqs], "wait_held": True}


def reply(obj):
    return Reply(200, "application/json", json_dump(obj).encode(), observed_at=time.time())


def emit(value):
    print(json.dumps(value, ensure_ascii=True, allow_nan=False), flush=True)


def barrier(point):
    emit({"barrier": point})
    require(sys.stdin.readline() == 'release\n', "BARRIER_RELEASE_REQUIRED")


def inventory(directory):
    result = {}
    for path in [directory] + sorted(directory.iterdir()):
        info = path.lstat()
        result["." if path == directory else path.name] = {
            "size": info.st_size, "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid, "gid": info.st_gid, "regular": stat.S_ISREG(info.st_mode),
            "directory": stat.S_ISDIR(info.st_mode), "symlink": stat.S_ISLNK(info.st_mode)}
    return result


def summarize(conn):
    state = dict(_verify(conn, ROOM))
    checks = {name: [row[0] for row in conn.execute("PRAGMA " + name)]
              for name in ("integrity_check", "quick_check")}
    require(all(value == ["ok"] for value in checks.values()), "SQLITE_INTEGRITY_FAILED")
    require(not conn.execute("PRAGMA foreign_key_check").fetchall(), "FOREIGN_KEYS_FAILED")
    checks.update({name: conn.execute("PRAGMA " + name).fetchone()[0]
                   for name in ("application_id", "user_version", "journal_mode", "synchronous", "foreign_keys")})
    messages = [dict(row) for row in conn.execute("SELECT * FROM messages ORDER BY seq")]
    gaps = [dict(row) for row in conn.execute("SELECT * FROM gaps ORDER BY start_seq")]
    for row in messages:
        expected = content_values(record(row["seq"]))
        require(all(row[key] == expected[key] for key in expected), "RAW_CONTENT_CHANGED")
        require(row["trust"] == "untrusted" and row["signature_verified"] is None
                and row["ingest_source"] == "poll", "METADATA_CHANGED")
    return {"state": state, "core": {key: state[key] for key in CORE},
            "messages": [{"seq": row["seq"], "row_sha256": digest(row)} for row in messages],
            "messages_sha256": digest(messages), "gaps": gaps, "gaps_sha256": digest(gaps),
            "events": [dict(row) for row in conn.execute(
                "SELECT event_type,evidence_id FROM events ORDER BY event_id")],
            "checks": checks}


class FixtureClient:
    def __init__(self, saved_since, fixture, checkpoint):
        self.saved_since = saved_since
        self.fixture = fixture
        self.checkpoint = checkpoint
        self.calls = []

    def poll(self, since):
        require(since == self.saved_since, "RESUME_CURSOR_MISMATCH")
        require(not self.calls, "FIXTURE_CALL_LIMIT")
        expected, seqs = FIXTURES[self.fixture]
        require(expected is None or since == expected, "FIXTURE_STAGE_MISMATCH")
        self.calls.append(since)
        self.checkpoint("fixture_wait")
        obj = envelope(seqs, 3 if self.fixture == "generation" else 2)
        if self.fixture == "anomaly":
            obj["count"] = 1
        return reply(obj)

    def tail(self):
        raise RuntimeError("RESUME_TAIL_FORBIDDEN")

    def config(self):
        raise RuntimeError("CONFIG_FORBIDDEN")


def mount_checks(text):
    matches = [line.split() for line in text.splitlines() if len(line.split()) > 6
               and line.split()[4] == "/state"]
    require(len(matches) == 1, "STATE_MOUNT_COUNT")
    options = set(matches[0][5].split(","))
    return {flag: flag in options for flag in ("rw", "noexec", "nosuid", "nodev")}


STRICT_PROFILE = "strict"
LOCAL_PROFILE = "local-docker-desktop"
MOUNT_HARDENING = frozenset({"state_mount_noexec", "state_mount_nosuid", "state_mount_nodev"})
CORE_CHECKS = frozenset({"nonroot_uid_gid", "no_capabilities", "no_new_privileges", "seccomp",
    "loopback_only", "no_host_home", "no_windows_mount", "no_control_socket", "state_mount_rw",
    "root_mount_readonly", "only_state_persistent_writable"})


def preflight_policy(checks, profile=STRICT_PROFILE):
    """Only the explicit local profile can waive the three observed mount flags."""
    require(profile in (STRICT_PROFILE, LOCAL_PROFILE), "UNKNOWN_VALIDATION_PROFILE")
    shape_ok = (isinstance(checks, dict) and set(checks) == CORE_CHECKS | MOUNT_HARDENING
                and all(type(value) is bool for value in checks.values()))
    failed = sorted(key for key in CORE_CHECKS if not isinstance(checks, dict) or checks.get(key) is not True)
    hardening = {"mount_hardening_" + flag: (
        "PENDING" if not isinstance(checks, dict) or type(checks.get("state_mount_" + flag)) is not bool
        else "PASS" if checks["state_mount_" + flag] else "WARN") for flag in ("noexec", "nosuid", "nodev")}
    warnings = sorted(key for key in MOUNT_HARDENING
                      if isinstance(checks, dict) and checks.get(key) is False)
    if not shape_ok:
        failed.append("preflight_shape")
    if profile == STRICT_PROFILE:
        failed.extend(warnings)
    return {"validation_profile": profile, "accepted": not failed,
            "isolation_boundary": "PASS" if shape_ok and not any(checks[k] is not True for k in CORE_CHECKS) else "FAIL",
            "failed_checks": sorted(failed), "hardening_warnings": warnings, **hardening}


def container_probe():
    # Public paths only: never try to open real key material.
    process = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()
                   if ":" in line)
    # The host passes its real absolute HOME path; a missing or relative value
    # fails no_host_home closed rather than passing it unconditionally.
    host_home = os.environ.get("OBSERVER_HOST_HOME", "")
    checks = {"nonroot_uid_gid": os.getuid() == os.getgid() == 65532,
              "no_capabilities": all(int(process[name].strip(), 16) == 0 for name in
                  ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")),
              "no_new_privileges": process["NoNewPrivs"].strip() == "1",
              "seccomp": process["Seccomp"].strip() == "2",
              "loopback_only": set(name for _, name in socket.if_nameindex()) <= {"lo"},
              "no_host_home": host_home.startswith("/") and host_home != "/"
                  and not os.path.lexists(host_home),
              "no_windows_mount": not os.path.lexists("/mnt/c"),
              "no_control_socket": not any(os.path.lexists(p) for p in
                  ("/var/run/docker.sock", "/run/docker.sock", "/run/desktop/docker.sock"))}
    mountinfo = Path("/proc/self/mountinfo").read_text()
    checks.update({"state_mount_" + k: v for k, v in mount_checks(mountinfo).items()})
    checks.update(persistent_mount_checks(mountinfo))
    _private(Path("/state"), directory=True)
    return checks


def persistent_mount_checks(text):
    mounts = []
    for line in text.splitlines():
        left, right = line.split(" - ", 1)
        fields = left.split()
        mounts.append((fields[4], set(fields[5].split(",")), right.split()[0]))
    roots = [options for path, options, _ in mounts if path == "/"]
    ephemeral = {"proc", "sysfs", "tmpfs", "devpts", "mqueue", "cgroup", "cgroup2"}
    return {"root_mount_readonly": len(roots) == 1 and "ro" in roots[0],
            "only_state_persistent_writable": all("rw" not in options or path == "/state"
                or filesystem in ephemeral for path, options, filesystem in mounts)}


class Driver:
    def __init__(self, root):
        self.root = root
        self.store = None
        self.lock = None
        self.directory = None
        self.calls = []

    def close(self):
        if self.store is not None:
            self.store.close()
            self.store = None
        if self.lock is not None:
            self.lock.__exit__()
            self.lock = None

    def execute(self, request):
        case = request["case"]
        require(isinstance(case, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", case), "CASE_REJECTED")
        directory = self.root / case
        operation = request["op"]
        target = request.get("point")
        require(target is None or target in POINTS + INIT_POINTS + ("idle", "fixture_wait"), "POINT_REJECTED")

        def checkpoint(point):
            if target == point:
                barrier(point)

        if operation == "init":
            require(self.store is None, "ALREADY_OPEN")
            with StateLock(directory, create=True):
                obj = envelope([100])
                initialize(directory, ROOM, obj, reply(obj), checkpoint)
            return {"initialized": True}
        if operation == "files":
            # Must be requested BEFORE any connection opens the post-kill DB.
            return {"files": inventory(directory), "observed_at": time.time(), "connection_open": self.store is not None}
        if operation == "open":
            require(self.store is None, "ALREADY_OPEN")
            self.lock = StateLock(directory)
            self.lock.__enter__()
            try:
                self.store = Store(directory, request.get("room", ROOM))
                self.directory = directory
                return {"summary": summarize(self.store.conn), "files": inventory(directory)}
            except BaseException:
                self.close()
                raise
        if operation == "peek":
            require(self.store is None, "ALREADY_OPEN")
            with contextlib.closing(Store(directory, ROOM, readonly=True)) as store:
                return {"summary": summarize(store.conn)}
        if operation == "close":
            self.close()
            return {"closed": True}
        if operation == "shutdown":
            self.close()
            return {"shutdown": True}
        if operation == "damage":
            # Only independent disposable fixture DBs. No general SQL/path API.
            require(self.store is None, "DAMAGE_REQUIRES_CLOSED_FIXTURE")
            _private(directory, directory=True)
            path = directory / "state.sqlite"
            _private(path)
            kind = request["kind"]
            if kind in ("application_id", "user_version", "accounting"):
                with contextlib.closing(sqlite3.connect(path)) as conn:
                    conn.execute({"application_id": "PRAGMA application_id=1",
                                  "user_version": "PRAGMA user_version=99",
                                  "accounting": "UPDATE state SET poll_seq=101,resolved_seq=101"}[kind])
                    conn.commit()
            elif kind == "corrupt":
                with path.open("r+b") as handle:
                    handle.seek(100)
                    handle.write(b"\xff" * 100)
                    handle.flush()
                    os.fsync(handle.fileno())
            elif kind in ("directory_mode", "db_mode"):
                (directory if kind == "directory_mode" else path).chmod(0o750 if kind == "directory_mode" else 0o640)
            else:
                raise RuntimeError("DAMAGE_KIND_REJECTED")
            return {"fault_injected": kind}
        require(self.store is not None and directory == self.directory, "OPEN_REQUIRED")
        if operation == "poll":
            client = FixtureClient(self.store.state()["poll_seq"], request["fixture"], checkpoint)
            self.calls = client.calls
            continuing, _ = Observer(self.store, client, checkpoint).poll_once()
            return {"summary": summarize(self.store.conn), "first_since": client.calls[0],
                    "fixture_calls": len(client.calls), "continuing": continuing}
        if operation == "hold":
            barrier("idle")
            return {"released": True}
        if operation == "snapshot":
            path = directory / "snapshot.sqlite"
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as destination:
                destination.row_factory = sqlite3.Row
                destination.execute("PRAGMA foreign_keys=ON")
                destination.execute("PRAGMA synchronous=FULL")
                self.store.conn.backup(destination)
                summary = summarize(destination)
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
                data = handle.read()
            require(len(data) <= 1024 * 1024, "BACKUP_SIZE_LIMIT")
            return {"backup_method": "sqlite3.Connection.backup", "summary": summary,
                    "backup_sha256": hashlib.sha256(data).hexdigest(),
                    "backup_base64": base64.b64encode(data).decode(), "files": inventory(directory)}
        raise RuntimeError("OPERATION_REJECTED")


def deny_network(event, args):
    if event in ("socket.__new__", "socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system"):
        raise RuntimeError("OFFLINE_BOUNDARY_VIOLATION")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--container", action="store_true")
    parser.add_argument("--validation-profile", choices=(STRICT_PROFILE, LOCAL_PROFILE), default=STRICT_PROFILE)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.resolve()
    require(not args.root.is_symlink(), "ROOT_SYMLINK_REJECTED")
    if args.container:
        require(root == Path("/state"), "CONTAINER_ROOT_REJECTED")
        checks = container_probe()
        policy = preflight_policy(checks, args.validation_profile)
        emit({"preflight": checks, "preflight_policy": policy, "external_requests": 0})
        require(policy["accepted"], "CONTAINER_BOUNDARY_REJECTED")
    else:
        require(root.is_relative_to(Path(__file__).resolve().parents[1]), "REPOSITORY_ROOT_REQUIRED")
    _private(root, directory=True)
    sys.addaudithook(deny_network)
    signal.alarm(900)
    emit({"ready": True, "external_requests": 0, "pid": os.getpid(), "source_hashes": source_hashes()})
    driver = Driver(root)
    try:
        for line in sys.stdin:
            require(len(line) <= 4096, "RPC_SIZE_LIMIT")
            driver.calls = []
            try:
                request = json.loads(line)
                result = driver.execute(request)
                emit({"ok": True, **result})
                if request["op"] == "shutdown":
                    return 0
            except Exception as exc:
                # Never format exceptions, raw records, or external strings.
                error = {"ok": False, "error_class": type(exc).__name__, "fixture_calls": len(driver.calls)}
                if isinstance(exc, (ObserverError, RuntimeError)) and re.fullmatch(r"[A-Z_]+", str(exc)):
                    error["error_code"] = str(exc)
                emit(error)
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        emit({"ok": False, "error_class": type(exc).__name__, "stage": "startup"})
        raise SystemExit(1)
