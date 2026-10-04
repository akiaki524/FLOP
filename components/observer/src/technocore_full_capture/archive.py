"""Full Capture's application-append-only hash-linked JSONL shards.

One exclusive writer per pre-created directory. Publication order is shard fsync,
rename, directory fsync, checkpoint fsync/rename/directory fsync. At most one
published successor can precede its checkpoint. Unpublished .pending files are
scratch space, never archives. No committed data is truncated, deleted or repaired.
"""

import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import time

from technocore_observer.protocol import ObserverError, Reply, decode_reply, json_dump, validate_room

MAX_LINE = 256 * 1024
MAX_SHARD = 64 * 1024 * 1024
DEFAULT_SHARD = 4 * 1024 * 1024
HEADER_LIMIT = 4096
ZERO_HASH = "0" * 64
SHARD_NAME = re.compile(r"shard-([0-9]{12})\.jsonl\Z")


def noop(point):
    pass


def fail(code):
    raise ObserverError(code)


def record(raw):
    if not raw.endswith(b"\n") or len(raw) > MAX_LINE:
        fail("CAPTURE_PARTIAL_OR_OVERSIZE_RECORD")
    obj = decode_reply(Reply(200, "application/json", raw))
    seq = obj.get("seq")
    if type(seq) is not int or not 1 <= seq <= 2**63 - 1:
        fail("CAPTURE_INVALID_SEQ")
    return obj


def regular_open(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        fail("CAPTURE_NOT_REGULAR_FILE")
    return os.fdopen(fd, "rb" if flags == os.O_RDONLY else "wb")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Archive:
    def __init__(self, directory, room, shard_bytes=DEFAULT_SHARD,
                 min_free_bytes=256 * 1024 * 1024, checkpoint=noop):
        self.path = Path(directory)
        self.room = validate_room(room)
        if (type(shard_bytes) is not int or not MAX_LINE <= shard_bytes <= MAX_SHARD
                or type(min_free_bytes) is not int or min_free_bytes < 0):
            fail("CAPTURE_INVALID_STORAGE_LIMIT")
        if not self.path.is_dir() or self.path.is_symlink():
            fail("CAPTURE_DIRECTORY_REQUIRED")
        self.shard_bytes = shard_bytes
        self.min_free_bytes = min_free_bytes
        self.checkpoint = checkpoint
        self.lock = regular_open(self.path / "capture.lock", os.O_CREAT | os.O_WRONLY)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            self._open()
            self.startup_seconds = time.monotonic() - started
        except BaseException:
            self.lock.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.lock.close()

    def _empty(self):
        return {"format": 1, "room": self.room, "generation": None,
                "anchor_seq": None, "cursor": None, "cursor_sha256": None,
                "shards": 0, "tip_sha256": ZERO_HASH, "message_count": 0,
                "archive_bytes": 0, "payload_bytes": 0, "created_at": time.time(),
                "updated_at": None, "last_success_at": None, "status": "WAITING",
                "error": None, "failures": 0}

    def name(self, index):
        if not 1 <= index <= 999999999999:
            fail("CAPTURE_SHARD_INDEX_EXHAUSTED")
        return self.path / f"shard-{index:012d}.jsonl"

    def _open(self):
        state_path = self.path / "capture.json"
        names = set()
        # Only file numbers, not message bodies, are kept during startup.
        for entry in self.path.iterdir():
            match = SHARD_NAME.fullmatch(entry.name)
            if match:
                names.add(int(match[1]))
            elif entry.name not in {"capture.json", "capture.lock", ".state.pending",
                                    ".shard.pending", ".fetch.pending"}:
                fail("CAPTURE_FOREIGN_DIRECTORY_ENTRY")
        if state_path.exists():
            with regular_open(state_path, os.O_RDONLY) as file:
                raw = file.read(HEADER_LIMIT + 1)
            if len(raw) > HEADER_LIMIT:
                fail("CAPTURE_BAD_CHECKPOINT")
            try:
                self.state = decode_reply(Reply(200, "application/json", raw))
                s = self.state
                if (set(s) != set(self._empty()) or s["format"] != 1
                        or s["room"] != self.room or s["status"] not in
                        {"WAITING", "RUNNING", "RETRY", "BLOCKED"}):
                    fail("CAPTURE_BAD_CHECKPOINT")
                for key in ("shards", "message_count", "archive_bytes", "payload_bytes", "failures"):
                    if type(s[key]) is not int or s[key] < 0:
                        fail("CAPTURE_BAD_CHECKPOINT")
                if s["shards"]:
                    for key in ("generation", "anchor_seq", "cursor"):
                        if type(s[key]) is not int or not 0 <= s[key] <= 2**63 - 1:
                            fail("CAPTURE_BAD_CHECKPOINT")
                    if s["message_count"] != s["cursor"] - s["anchor_seq"]:
                        fail("CAPTURE_BAD_CHECKPOINT")
                elif any(s[k] is not None for k in ("generation", "anchor_seq", "cursor")):
                    fail("CAPTURE_BAD_CHECKPOINT")
            except (KeyError, TypeError, ValueError) as exc:
                raise ObserverError("CAPTURE_BAD_CHECKPOINT") from exc
        else:
            if names:
                fail("CAPTURE_CHECKPOINT_MISSING")
            self.state = self._empty()
            self.save_state()
        n = self.state["shards"]
        if (len(names) not in (n, n + 1) or (names and
                (min(names) != 1 or max(names) != len(names)))):
            fail("CAPTURE_ARCHIVE_SET_MISMATCH")
        if n:
            header, digest = self.inspect(self.name(n))
            s = self.state
            if (digest != s["tip_sha256"] or header["last_seq"] != s["cursor"]
                    or header["last_sha256"] != s["cursor_sha256"]
                    or header["generation"] != s["generation"]
                    or header["index"] != n or header["anchor_seq"] != s["anchor_seq"]):
                fail("CAPTURE_TIP_CHECKPOINT_MISMATCH")
        if len(names) == n + 1:
            # Complete publication whose checkpoint was interrupted. This does
            # not repair anything: validate the sole successor before adoption.
            if self.state["status"] == "BLOCKED":
                fail("CAPTURE_BLOCKED_SUCCESSOR")
            header, digest = self.inspect(self.name(n + 1))
            self._adopt(header, digest)
            self.save_state()

    def capacity(self, needed=0):
        info = os.statvfs(self.path)
        free = info.f_bavail * info.f_frsize
        if free < self.min_free_bytes + needed:
            fail("CAPTURE_STORAGE_LOW")
        return free

    def save_state(self):
        raw = (json_dump(self.state) + "\n").encode("ascii")
        if len(raw) > HEADER_LIMIT:
            fail("CAPTURE_CHECKPOINT_OVERSIZE")
        with regular_open(self.path / ".state.pending", os.O_WRONLY | os.O_CREAT | os.O_TRUNC) as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.replace(self.path / ".state.pending", self.path / "capture.json")
        sync_directory(self.path)

    def block(self, code):
        # Codes are local constants, never remote exception text or message text.
        self.state.update(status="BLOCKED", error=code, updated_at=time.time())
        self.save_state()

    def inspect(self, path):
        size = path.stat(follow_symlinks=False).st_size
        if size > MAX_SHARD + HEADER_LIMIT:
            fail("CAPTURE_ARCHIVE_OVERSIZE")
        with regular_open(path, os.O_RDONLY) as file:
            raw = file.readline(HEADER_LIMIT + 1)
            if len(raw) > HEADER_LIMIT or not raw.endswith(b"\n"):
                fail("CAPTURE_BAD_ARCHIVE_HEADER")
            h = decode_reply(Reply(200, "application/json", raw))
            required = {"format", "room", "index", "generation", "anchor_seq", "first_seq",
                        "last_seq", "count", "payload_bytes", "payload_sha256",
                        "previous_sha256", "last_sha256", "captured_at"}
            if set(h) != required or h["format"] != 1 or h["room"] != self.room:
                fail("CAPTURE_BAD_ARCHIVE_HEADER")
            for key in ("index", "generation", "anchor_seq", "first_seq", "last_seq", "count", "payload_bytes"):
                if type(h[key]) is not int or not 0 <= h[key] <= 2**63 - 1:
                    fail("CAPTURE_BAD_ARCHIVE_HEADER")
            digest, payload = hashlib.sha256(raw), hashlib.sha256()
            count = total = 0
            last_hash = None
            for line in iter(lambda: file.readline(MAX_LINE + 1), b""):
                obj = record(line)
                if obj["seq"] != h["first_seq"] + count:
                    fail("CAPTURE_ARCHIVE_SEQUENCE_MISMATCH")
                count += 1
                total += len(line)
                digest.update(line)
                payload.update(line)
                last_hash = hashlib.sha256(line).hexdigest()
            if (not count or h["count"] != count or h["payload_bytes"] != total
                    or h["last_seq"] != h["first_seq"] + count - 1
                    or h["payload_sha256"] != payload.hexdigest()
                    or h["last_sha256"] != last_hash):
                fail("CAPTURE_ARCHIVE_INTEGRITY_FAILURE")
            return h, digest.hexdigest()

    def _adopt(self, h, digest):
        s = self.state
        expected = s["cursor"] + 1 if s["cursor"] is not None else h["anchor_seq"] + 1
        if (h["index"] != s["shards"] + 1 or h["previous_sha256"] != s["tip_sha256"]
                or h["first_seq"] != expected
                or (s["generation"] is not None and h["generation"] != s["generation"])
                or (s["anchor_seq"] is not None and h["anchor_seq"] != s["anchor_seq"])):
            fail("CAPTURE_ARCHIVE_CHAIN_FAILURE")
        s.update(generation=h["generation"], anchor_seq=h["anchor_seq"], cursor=h["last_seq"],
                 cursor_sha256=h["last_sha256"], shards=h["index"], tip_sha256=digest,
                 message_count=s["message_count"] + h["count"],
                 archive_bytes=s["archive_bytes"] + self.name(h["index"]).stat().st_size,
                 payload_bytes=s["payload_bytes"] + h["payload_bytes"],
                 updated_at=time.time(), status="RUNNING", error=None, failures=0)

    def append(self, lines, generation):
        if not lines:
            return
        if self.state["status"] == "BLOCKED":
            fail("CAPTURE_REVIEW_REQUIRED")
        records = [record(line) for line in lines]
        first, last = records[0]["seq"], records[-1]["seq"]
        if (type(generation) is not int or not 0 <= generation <= 2**63 - 1
                or any(obj["seq"] != first + i for i, obj in enumerate(records))):
            fail("CAPTURE_INVALID_BATCH")
        payload = b"".join(lines)
        if len(payload) > self.shard_bytes:
            fail("CAPTURE_SHARD_BUDGET_EXCEEDED")
        s = self.state
        if ((s["cursor"] is not None and first != s["cursor"] + 1)
                or (s["generation"] is not None and generation != s["generation"])):
            fail("CAPTURE_BATCH_DISCONTINUITY")
        self.capacity(len(payload) + HEADER_LIMIT * 2)
        h = {"format": 1, "room": self.room, "index": s["shards"] + 1,
             "generation": generation, "anchor_seq": s["anchor_seq"] if s["shards"] else first - 1,
             "first_seq": first, "last_seq": last, "count": len(lines),
             "payload_bytes": len(payload), "payload_sha256": hashlib.sha256(payload).hexdigest(),
             "previous_sha256": s["tip_sha256"], "last_sha256": hashlib.sha256(lines[-1]).hexdigest(),
             "captured_at": time.time()}
        header = (json_dump(h) + "\n").encode("ascii")
        target = self.name(h["index"])
        if target.exists() or target.is_symlink():
            fail("CAPTURE_SHARD_ALREADY_EXISTS")
        with regular_open(self.path / ".shard.pending", os.O_WRONLY | os.O_CREAT | os.O_TRUNC) as file:
            file.write(header)
            self.checkpoint("shard_header_written")
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        self.checkpoint("shard_fsynced")
        os.replace(self.path / ".shard.pending", target)
        self.checkpoint("shard_published")
        sync_directory(self.path)
        self.checkpoint("shard_directory_fsynced")
        self._adopt(h, hashlib.sha256(header + payload).hexdigest())
        self.checkpoint("before_checkpoint")
        self.save_state()
        self.checkpoint("after_checkpoint")

    def verify(self, emit=noop):
        """Full offline scan, constant message memory; emits one receipt per shard."""
        started = time.monotonic()
        try:
            result = self._verify(emit)
        except ObserverError as exc:
            self.block(str(exc))
            raise
        return {**result, "verification_seconds": time.monotonic() - started}

    def _verify(self, emit):
        previous, cursor, count, total, payload = ZERO_HASH, self.state["anchor_seq"], 0, 0, 0
        for i in range(1, self.state["shards"] + 1):
            h, digest = self.inspect(self.name(i))
            if (h["index"] != i or h["previous_sha256"] != previous
                    or h["generation"] != self.state["generation"]
                    or h["anchor_seq"] != self.state["anchor_seq"] or h["first_seq"] != cursor + 1):
                fail("CAPTURE_ARCHIVE_CHAIN_FAILURE")
            size = self.name(i).stat().st_size
            emit({**h, "sha256": digest, "archive_bytes": size})
            previous, cursor = digest, h["last_seq"]
            count += h["count"]
            total += size
            payload += h["payload_bytes"]
        s = self.state
        if (previous, cursor, count, total, payload) != (
                s["tip_sha256"], s["cursor"], s["message_count"], s["archive_bytes"], s["payload_bytes"]):
            fail("CAPTURE_CHECKPOINT_TOTAL_MISMATCH")
        return {"verified": True, **s}

    def metrics(self):
        info = os.statvfs(self.path)
        return {**self.state, "filesystem_free_bytes": info.f_bavail * info.f_frsize,
                "filesystem_free_inodes": info.f_favail,
                "startup_seconds": self.startup_seconds,
                "mean_shard_payload_bytes": self.state["payload_bytes"] / max(1, self.state["shards"]),
                "integrity_scope": "tip_on_open; full_history_requires_verify"}
