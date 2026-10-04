"""Container-side soak worker. No shell, configurable URLs, or raw output.

The host grants one synchronous RPC per GET. A persistent worker holds the
Observer lock; watchdog uses a separate process/connection and exactly one GET.
Offline mode uses generated replies only, even when run as the host user.
"""

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error

if __package__ in (None, "") and Path(__file__).is_file():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from technocore_observer.cli import emit
from technocore_observer.http import SafeClient
from technocore_observer.observer import NETWORK_ERRORS, Observer, watchdog
from technocore_observer.protocol import Reply, decode_reply, json_dump, validate_envelope
from technocore_observer.storage import StateLock, Store, initialize, _verify

ROOM = "mb-047f3d88ef38"
MIB = 1024 * 1024


def private_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class FixtureClient:
    """No network-capable delegate exists in offline mode."""
    def config(self):
        return Reply(200, "application/json", b'{"version":"0.12.1","settings":{}}')

    def tail(self):
        return self.reply([100])

    def poll(self, since):
        return self.reply([since + 1])

    @staticmethod
    def reply(seqs, generation=2, held=True):
        obj = {"room": ROOM, "generation": generation, "count": len(seqs),
               "first_seq": seqs[0] if seqs else None,
               "last_seq": seqs[-1] if seqs else 100, "wait_held": held,
               "messages": [{"seq": n, "ts": "2026-09-06T00:00:00Z", "from": "fixture",
                             "text": "offline\n\u202e\x1b", "nonce": n} for n in seqs]}
        return Reply(200, "application/json", json_dump(obj).encode(), observed_at=time.time())


class MeasuredClient:
    def __init__(self, client):
        self.client, self.last = client, None

    def call(self, name, *args):
        start = time.monotonic()
        self.last = {"operation": name, "http_status": None, "valid_envelope": False}
        try:
            reply = getattr(self.client, name)(*args)
            self.last.update(http_status=reply.status, body_bytes=len(reply.body), truncated=reply.truncated)
            if reply.status == 200 and name != "config":
                try:
                    obj = validate_envelope(decode_reply(reply), ROOM)
                except Exception:
                    pass  # Observer records protocol evidence and applies its finite retry rule.
                else:
                    self.last.update(valid_envelope=True, count=obj["count"],
                                     wait_held=obj.get("wait_held"), generation=obj["generation"])
            return reply
        except NETWORK_ERRORS as exc:
            # Never render str/repr(exc) or a URL-bearing URLError.reason.
            # Preserve the original exception and the Observer's retry behavior.
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else None
            number = getattr(exc, "errno", None)
            if type(number) is not int and isinstance(reason, BaseException):
                number = getattr(reason, "errno", None)
            self.last.update(error_class=type(exc).__name__,
                             errno=number if type(number) is int else None,
                             reason_class=type(reason).__name__ if isinstance(reason, BaseException) else None)
            raise
        finally:
            self.last["elapsed_seconds"] = time.monotonic() - start

    def config(self): return self.call("config")
    def tail(self): return self.call("tail")
    def poll(self, since): return self.call("poll", since)


def memory_metrics():
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    rss = int(status["VmRSS"].split()[0]) * 1024
    current = Path("/sys/fs/cgroup/memory.current")
    events = Path("/sys/fs/cgroup/memory.events")
    if current.is_file() and events.is_file():
        counts = dict(line.split() for line in events.read_text().splitlines())
        return {"rss_bytes": rss, "container_memory_bytes": int(current.read_text()),
                "oom_count": int(counts.get("oom_kill", 0))}
    v1 = Path("/sys/fs/cgroup/memory")
    if (v1 / "memory.usage_in_bytes").is_file():
        counts = dict(line.split() for line in (v1 / "memory.oom_control").read_text().splitlines())
        return {"rss_bytes": rss, "container_memory_bytes": int((v1 / "memory.usage_in_bytes").read_text()),
                "oom_count": int(counts.get("oom_kill", 0))}
    return {"rss_bytes": rss, "container_memory_bytes": None, "oom_count": None}


def snapshot(conn, directory):
    """One pending snapshot; host must durably receive it before acknowledgement."""
    target = directory / "snapshot.sqlite"
    private_write(target, b"")
    deadline = time.monotonic() + 20
    def progress(status, remaining, total):
        if time.monotonic() >= deadline:
            raise TimeoutError("BACKUP_DEADLINE")
    with contextlib.closing(sqlite3.connect(target)) as dest:
        conn.backup(dest, pages=128, progress=progress, sleep=0.01)
        dest.row_factory = sqlite3.Row
        state = dict(_verify(dest, ROOM))
        counts = {name: dest.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                  for name in ("messages", "gaps", "events", "evidence")}
        counts["flagged_messages"] = dest.execute("SELECT COUNT(*) FROM messages WHERE validation_flags<>'[]'").fetchone()[0]
    with target.open("rb") as stream:
        os.fsync(stream.fileno())
    meta = {"sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "bytes": target.stat().st_size, "state": state, "counts": counts}
    private_write(directory / "snapshot.json", json_dump(meta).encode())
    return meta


def export_snapshot(directory):
    import zipfile
    with zipfile.ZipFile(sys.stdout.buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in ("snapshot.sqlite", "snapshot.json"):
            path = directory / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * MIB:
                raise RuntimeError("SNAPSHOT_PATH_REJECTED")
            archive.write(path, name)


class Worker:
    def __init__(self, directory, client, offline=False):
        self.directory, self.client, self.offline = directory, MeasuredClient(client), offline
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(StateLock(directory, create=True))
        self.store = self.observer = None
        self.counts = {"init": 0, "config": 0, "poll": 0}

    def close(self): self.stack.close()

    def metrics(self):
        vfs = os.statvfs(self.directory)
        return {**memory_metrics(), "disk_free_bytes": vfs.f_bavail * vfs.f_frsize,
                "files": {name: (self.directory / name).stat().st_size if (self.directory / name).exists() else 0
                          for name in ("state.sqlite", "state.sqlite-wal", "state.sqlite-shm")},
                "heartbeat": self.store.heartbeat() if self.store else None}

    def guard(self):
        if self.store and self.store.state()["status"] in ("ERROR", "NEEDS_RESYNC"):
            raise RuntimeError("TERMINAL_STATE")
        if not self.offline:
            if os.getuid() != 65532 or os.getgid() != 65532:
                raise RuntimeError("BOUNDARY_VIOLATION")
            if self.metrics()["disk_free_bytes"] < 48 * MIB:
                raise RuntimeError("DISK_RESERVE")

    def handle(self, command):
        if command in self.counts:
            self.guard()
            cap = 360 if command == "poll" else 1
            if self.counts[command] >= cap:
                raise RuntimeError("ROLE_BUDGET")
            self.counts[command] += 1
        if command == "init":
            if self.store or any(p.name.startswith("state.sqlite") for p in self.directory.iterdir()):
                raise RuntimeError("EXPLICIT_FRESH_INIT_REQUIRED")
            reply = self.client.tail()
            if reply.status != 200:
                raise RuntimeError("INIT_HTTP_FAILURE")
            obj = validate_envelope(decode_reply(reply), ROOM)
            if obj["count"] != 1:
                raise RuntimeError("INIT_REQUIRES_ONE_MESSAGE")
            initialize(self.directory, ROOM, obj, reply)
            self.store = self.stack.enter_context(contextlib.closing(Store(self.directory, ROOM)))
            self.observer = Observer(self.store, self.client)
            return {"heartbeat": self.store.heartbeat(), "pragmas": {
                name: self.store.conn.execute("PRAGMA " + name).fetchone()[0]
                for name in ("journal_mode", "synchronous", "foreign_keys")}, "http": self.client.last}
        if command in ("config", "poll", "snapshot") and self.store is None:
            raise RuntimeError("NOT_INITIALIZED")
        if command == "config":
            return {"config": self.observer.check_config(), "http": self.client.last}
        if command == "poll":
            continuing, delay = self.observer.poll_once()
            return {"continuing": continuing, "delay": delay, "http": self.client.last,
                    "heartbeat": self.store.heartbeat()}
        if command == "metrics": return self.metrics()
        if command == "snapshot": return snapshot(self.store.conn, self.directory)
        if command == "ack_snapshot":
            # Only our own generated, successfully exported snapshot is removed.
            for name in ("snapshot.sqlite", "snapshot.json"):
                (self.directory / name).unlink()
            return {"acknowledged": True}
        raise RuntimeError("COMMAND_NOT_ALLOWED")


def _main():
    p = argparse.ArgumentParser()
    p.add_argument("--offline", action="store_true")
    p.add_argument("--directory", type=Path, default=Path("/state/inbox"))
    p.add_argument("--mode", choices=("worker", "watchdog", "export"), default="worker")
    args = p.parse_args()
    if not args.offline:
        if os.getuid() != 65532 or os.getgid() != 65532 or args.directory != Path("/state/inbox"):
            emit({"ok": False, "error": "ISOLATED_CONTAINER_REQUIRED"})
            return 1
        from live_smoke import process_checks
        checks = process_checks(False)
        if not all(checks.values()):
            emit({"ok": False, "error": "BOUNDARY_VIOLATION", "checks": checks})
            return 1
    else:
        checks = {"offline_fixture_no_network": True}
        if os.getuid() == 65532 and args.directory == Path("/state/inbox"):
            from live_smoke import process_checks
            checks.update(process_checks(True))
            if not all(checks.values()):
                emit({"ok": False, "error": "BOUNDARY_VIOLATION", "checks": checks})
                return 1
    if args.mode == "export":
        export_snapshot(args.directory)
        return 0
    client = FixtureClient() if args.offline else SafeClient(ROOM)
    if args.mode == "watchdog":
        try:
            with contextlib.closing(Store(args.directory, ROOM, readonly=True)) as store:
                # Refuse the GET as well if a terminal state is already visible.
                if store.state()["status"] in ("ERROR", "NEEDS_RESYNC"):
                    raise RuntimeError("TERMINAL_STATE")
                measured = MeasuredClient(client)
                result = watchdog(store, measured)
                emit({"ok": True, "result": {"watchdog": result, "http": measured.last}})
            return 0
        except Exception as exc:
            emit({"ok": False, "error": type(exc).__name__})
            return 1
    with contextlib.closing(Worker(args.directory, client, args.offline)) as worker:
        emit({"ok": True, "result": {"ready": True, "checks": checks}})
        while True:
            line = sys.stdin.buffer.readline(4097)
            if not line:
                break
            try:
                if len(line) > 4096:
                    raise RuntimeError("COMMAND_TOO_LARGE")
                obj = json.loads(line)
                if not isinstance(obj, dict) or set(obj) != {"command"} or not isinstance(obj["command"], str):
                    raise RuntimeError("INVALID_COMMAND")
                if obj["command"] == "stop":
                    break
                result = worker.handle(obj["command"])
                emit({"ok": True, "result": result})
            except Exception as exc:
                # Any disk/config/boundary exception ends the worker immediately.
                # No raw exception formatting: it may contain message content.
                emit({"ok": False, "error": type(exc).__name__})
                return 1
    return 0


def main():
    try:
        return _main()
    except Exception as exc:
        emit({"ok": False, "error": type(exc).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
