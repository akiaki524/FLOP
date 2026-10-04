"""Offline spool consumer: unchanged format=1 Archives plus a coverage manifest.

Durability order: shard/checkpoint -> manifest transaction -> spool consumer ack.
The manifest has indexed per-entry receipts, not a growing JSON list rewritten
on every page. Only one shard may be ahead of its manifest receipt. Recovery
compares its full payload hash and predecessor before adopting that publication.
"""

import fcntl
import hashlib
import json
import os
import sqlite3
import uuid

from .archive import Archive, DEFAULT_SHARD, ZERO_HASH, noop, regular_open, sync_directory
from .spool import canonical, checked_directory, digest, require

MANIFEST_ID = 0x5443414D


def validate_source(spool, manifest, *, full=False):
    """Reject stale/divergent restores before publication or acknowledgement.

    Startup checks the receipted tip and bounded pending plan. Explicit restore
    verification compares every receipt with the retained spool stream.
    """
    state = dict(manifest.execute("SELECT * FROM state WHERE singleton=1").fetchone())
    source = spool.state()
    require(state["spool_id"] == source["spool_id"] and state["room"] == spool.room,
            "MANIFEST_SOURCE_MISMATCH")
    require(state["through_entry"] <= source["high_entry"], "MANIFEST_AHEAD_OF_SOURCE")
    sql = "SELECT * FROM receipts ORDER BY entry_id" if full else "SELECT * FROM receipts WHERE entry_id=?"
    params = () if full else (state["through_entry"],)
    for receipt in manifest.execute(sql, params):
        rows, _ = spool.read(receipt["entry_id"] - 1, 1)
        require(rows and rows[0]["entry_id"] == receipt["entry_id"], "MANIFEST_SOURCE_MISSING")
        row, item = rows[0], json.loads(receipt["document"])
        require(all(item[key] == row[key] for key in
                    ("entry_id", "epoch", "batch_id", "kind", "seq", "end_seq", "batch")),
                "MANIFEST_SOURCE_DIVERGED")
        require(item["record_sha256"] == digest(row["payload"] + "\n") if row["kind"] == "MESSAGE"
                else item["evidence"] == json.loads(row["payload"]), "MANIFEST_SOURCE_DIVERGED")
    if state["through_entry"]:
        tip = manifest.execute("SELECT chain_hash FROM receipts WHERE entry_id=?",
                               (state["through_entry"],)).fetchone()
        require(tip is not None and tip[0] == state["receipt_hash"], "MANIFEST_RECEIPT_HASH")
    pending = manifest.execute("SELECT document,sha256 FROM pending WHERE singleton=1").fetchone()
    if pending is not None:
        require(digest(pending["document"]) == pending["sha256"], "MANIFEST_PENDING_HASH")
        planned = json.loads(pending["document"])
        require(0 < len(planned) <= 200 and planned[0]["entry_id"] == state["through_entry"] + 1,
                "MANIFEST_PENDING_SOURCE")
        rows, _ = spool.read(planned[0]["entry_id"] - 1, len(planned))
        require(rows == planned, "MANIFEST_PENDING_SOURCE")
    return state


class ArchiveWorker:
    def __init__(self, spool, directory, *, name="archive", shard_bytes=DEFAULT_SHARD,
                 min_free_bytes=256 * 1024 * 1024, checkpoint=noop):
        self.spool, self.path, self.name = spool, checked_directory(directory), name
        self.shard_bytes, self.min_free_bytes, self.checkpoint = shard_bytes, min_free_bytes, checkpoint
        self.archive = self.active = self.conn = self.lock = None
        try:
            self.lock = regular_open(self.path / "archive-worker.lock", os.O_CREAT | os.O_WRONLY)
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            manifest = self.path / "manifest.sqlite"
            require(not (self.path / ".manifest.init").exists(), "MANIFEST_INCOMPLETE_INITIALIZATION")
            if not manifest.exists():
                self._initialize(manifest)
            with regular_open(manifest, os.O_RDONLY):
                pass
            self.conn = sqlite3.connect(manifest.as_uri() + "?mode=rw", uri=True, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA trusted_schema=OFF")
            self.conn.execute("PRAGMA synchronous=FULL")
            require(self.conn.execute("PRAGMA application_id").fetchone()[0] == MANIFEST_ID
                    and self.conn.execute("PRAGMA user_version").fetchone()[0] == 1, "MANIFEST_FORMAT")
            s = self.state()
            require(s["spool_id"] == spool.state()["spool_id"] and s["room"] == spool.room,
                    "MANIFEST_SOURCE_MISMATCH")
            validate_source(spool, self.conn)
            binding = str(self.path) + ":" + s["archive_id"]
            self.spool.register(name, binding)
            consumer = self.spool.consumer(name)
            require(consumer["ack_entry"] <= s["through_entry"], "MANIFEST_BEHIND_ACK")
            if consumer["ack_entry"]:
                receipt = self.conn.execute("SELECT chain_hash FROM receipts WHERE entry_id=?",
                                            (consumer["ack_entry"],)).fetchone()
                require(receipt is not None and consumer["receipt"] == receipt[0], "MANIFEST_ACK_RECEIPT_MISMATCH")
            # Bound startup to the active segment, not every archived message.
            if s["active_segment"] is not None:
                self._open_segment(s["active_segment"])
                seg = self.segment(s["active_segment"])
                if self.archive.state["shards"] == seg["shards"]:
                    require(self.archive.state["tip_sha256"] == seg["tip_sha256"], "MANIFEST_TIP_MISMATCH")
                else:
                    require(self.archive.state["shards"] == seg["shards"] + 1, "MANIFEST_UNEXPECTED_SUCCESSORS")
        except BaseException:
            self.close()
            raise

    def _initialize(self, manifest):
        require(not manifest.is_symlink(), "MANIFEST_UNSAFE_PATH")
        # A missing manifest in an existing archive is not a new empty archive.
        require(set(p.name for p in self.path.iterdir()) == {"archive-worker.lock"}, "MANIFEST_MISSING_OR_FOREIGN")
        temp = self.path / ".manifest.init"
        with regular_open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY):
            pass
        conn = sqlite3.connect(temp, isolation_level=None)
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"PRAGMA application_id={MANIFEST_ID}")
            conn.execute("PRAGMA user_version=1")
            conn.execute("""CREATE TABLE state (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                archive_id TEXT NOT NULL, spool_id TEXT NOT NULL, room TEXT NOT NULL,
                through_entry INTEGER NOT NULL DEFAULT 0, active_segment TEXT,
                receipt_hash TEXT NOT NULL, messages INTEGER NOT NULL DEFAULT 0,
                gaps INTEGER NOT NULL DEFAULT 0, unconfirmed INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("""CREATE TABLE segments (name TEXT PRIMARY KEY, epoch INTEGER NOT NULL,
                generation INTEGER NOT NULL, first_seq INTEGER NOT NULL, last_seq INTEGER NOT NULL,
                first_entry INTEGER NOT NULL, last_entry INTEGER NOT NULL,
                shards INTEGER NOT NULL, tip_sha256 TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE receipts (entry_id INTEGER PRIMARY KEY, document TEXT NOT NULL,
                chain_hash TEXT NOT NULL)""")
            conn.execute("CREATE TABLE pending (singleton INTEGER PRIMARY KEY CHECK(singleton=1), document TEXT NOT NULL, sha256 TEXT NOT NULL)")
            conn.execute("INSERT INTO state(singleton,archive_id,spool_id,room,receipt_hash) VALUES(1,?,?,?,?)",
                         (uuid.uuid4().hex, self.spool.state()["spool_id"], self.spool.room, ZERO_HASH))
            conn.execute("COMMIT")
        finally:
            conn.close()
        with regular_open(temp, os.O_RDONLY) as file:
            os.fsync(file.fileno())
        os.rename(temp, manifest)
        sync_directory(self.path)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.archive is not None:
            self.archive.__exit__()
            self.archive = None
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def state(self):
        return dict(self.conn.execute("SELECT * FROM state WHERE singleton=1").fetchone())

    def segment(self, name):
        row = self.conn.execute("SELECT * FROM segments WHERE name=?", (name,)).fetchone()
        return dict(row) if row is not None else None

    def _open_segment(self, name):
        require(name.startswith("segment-") and len(name) == 28 and name[8:].isascii()
                and name[8:].isdecimal(), "MANIFEST_BAD_SEGMENT")
        if self.active == name:
            return
        if self.archive is not None:
            self.archive.__exit__()
        self.archive = None
        path = self.path / name
        if not path.exists():
            require(self.segment(name) is None, "MANIFEST_SEGMENT_MISSING")
            path.mkdir(mode=0o700)
            sync_directory(self.path)
        self.archive = Archive(path, self.spool.room, self.shard_bytes, self.min_free_bytes, self.checkpoint)
        self.active = name

    def _publish(self, rows):
        first, last = rows[0], rows[-1]
        state = self.state()
        previous = self.segment(state["active_segment"]) if state["active_segment"] else None
        continuous = (previous is not None and previous["epoch"] == first["epoch"]
                      and first["seq"] == previous["last_seq"] + 1)
        name = previous["name"] if continuous else f"segment-{first['entry_id']:020d}"
        previous = previous if continuous else None
        self._open_segment(name)
        lines = [(row["payload"] + "\n").encode("ascii") for row in rows]
        old_count = previous["shards"] if previous else 0
        old_tip = previous["tip_sha256"] if previous else ZERO_HASH
        a = self.archive
        generation = first["batch"]["observed_generation"]
        if a.state["shards"] == old_count:
            require(a.state["tip_sha256"] == old_tip, "MANIFEST_TIP_MISMATCH")
            a.append(lines, generation)
        else:
            require(a.state["shards"] == old_count + 1, "MANIFEST_UNEXPECTED_SUCCESSORS")
            h, _ = a.inspect(a.name(old_count + 1))
            require(h["first_seq"] == first["seq"] and h["last_seq"] == last["seq"]
                    and h["generation"] == generation and h["previous_sha256"] == old_tip
                    and h["payload_sha256"] == hashlib.sha256(b"".join(lines)).hexdigest(),
                    "MANIFEST_REPLAY_MISMATCH")
        self.checkpoint("archive_published")
        return {"name": name, "epoch": first["epoch"], "generation": generation,
                "first_seq": previous["first_seq"] if previous else first["seq"],
                "last_seq": last["seq"], "first_entry": previous["first_entry"] if previous else first["entry_id"],
                "last_entry": last["entry_id"], "shards": a.state["shards"], "tip_sha256": a.state["tip_sha256"]}

    def _record(self, rows, segment=None):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            s = self.state()
            chain, messages, gaps, uncertain = s["receipt_hash"], 0, 0, 0
            for row in rows:
                require(row["entry_id"] == s["through_entry"] + 1, "MANIFEST_STREAM_HOLE")
                document = {key: row[key] for key in ("entry_id", "epoch", "batch_id", "kind", "seq", "end_seq", "batch")}
                if row["kind"] == "MESSAGE":
                    document.update(segment=segment["name"], record_sha256=digest(row["payload"] + "\n"))
                    messages += 1
                else:
                    document["evidence"] = json.loads(row["payload"])
                    gaps += row["kind"] in {"GAP", "BOOTSTRAP_UNOBSERVED_PREFIX"}
                    uncertain += row["kind"] in {"GENERATION_BOUNDARY", "BOUNDARY_OBSERVATION", "CONFLICT", "LATE_OBSERVATION"}
                raw = canonical(document)
                chain = digest(chain + raw)
                self.conn.execute("INSERT INTO receipts VALUES(?,?,?)", (row["entry_id"], raw, chain))
                s["through_entry"] = row["entry_id"]
            if segment is not None:
                self.conn.execute("INSERT OR REPLACE INTO segments VALUES(?,?,?,?,?,?,?,?,?)", tuple(segment.values()))
                self.conn.execute("DELETE FROM pending WHERE singleton=1")
            self.conn.execute("""UPDATE state SET through_entry=?,receipt_hash=?,
                active_segment=?,messages=messages+?,gaps=gaps+?,unconfirmed=unconfirmed+? WHERE singleton=1""",
                (s["through_entry"], chain, segment["name"] if segment else s["active_segment"], messages, gaps, uncertain))
            self.checkpoint("before_manifest_commit")
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        self.checkpoint("manifest_committed")

    def _ack(self):
        state = self.state()
        ack = self.spool.consumer(self.name)["ack_entry"]
        require(ack <= state["through_entry"], "MANIFEST_BEHIND_ACK")
        if ack < state["through_entry"]:
            self.checkpoint("before_consumer_ack")
            self.spool.ack(self.name, ack, state["through_entry"], state["receipt_hash"])
            self.checkpoint("after_consumer_ack")

    def step(self, limit=200):
        validate_source(self.spool, self.conn)
        pending = self.conn.execute("SELECT document,sha256 FROM pending WHERE singleton=1").fetchone()
        if pending is not None:
            require(digest(pending["document"]) == pending["sha256"], "MANIFEST_PENDING_HASH")
            planned = json.loads(pending["document"])
            self._record(planned, self._publish(planned))
        self._ack()
        rows, producer = self.spool.read(self.state()["through_entry"], limit)
        index = 0
        while index < len(rows):
            row = rows[index]
            if row["kind"] != "MESSAGE":
                self._record([row])
                index += 1
                continue
            group, size = [row], len(row["payload"]) + 1
            index += 1
            while index < len(rows):
                following = rows[index]
                added = len(following["payload"]) + 1
                if (following["kind"] != "MESSAGE" or following["epoch"] != row["epoch"]
                        or following["seq"] != group[-1]["seq"] + 1 or size + added > self.shard_bytes):
                    break
                group.append(following)
                size += added
                index += 1
            raw = canonical(group)
            self.conn.execute("INSERT INTO pending VALUES(1,?,?)", (raw, digest(raw)))
            self.checkpoint("publication_planned")
            segment = self._publish(group)
            self._record(group, segment)
        self._ack()
        return {**self.coverage(), "processed": len(rows), "producer_high_entry": producer["high_entry"],
                "caught_up": self.state()["through_entry"] == producer["high_entry"]}

    def coverage(self):
        s = self.state()
        status = ("PARTIAL_WITH_GAPS" if s["gaps"] else
                  "UNCONFIRMED" if s["unconfirmed"] or not s["messages"] else "COMPLETE")
        return {**s, "coverage_status": status,
                "scope": "observed local epochs and archived seq ranges through through_entry",
                "generation_binding": "OBSERVED_NOT_ATOMIC", "remote_tail": "UNKNOWN",
                "all_room_history_complete": False, "integrity_scope": "tip_on_open; full_verify_separate"}

    def verify(self):
        """Explicit offline full audit. Does not contact the producer or advance ack."""
        validate_source(self.spool, self.conn, full=True)
        require(self.conn.execute("SELECT 1 FROM pending LIMIT 1").fetchone() is None,
                "MANIFEST_PENDING_PUBLICATION")
        require(self.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok", "MANIFEST_INTEGRITY")
        chain, through, messages, gaps, uncertain = ZERO_HASH, 0, 0, 0, 0
        current_name = None
        stream = None
        archive = None
        def records(a):
            for index in range(1, a.state["shards"] + 1):
                with regular_open(a.name(index), os.O_RDONLY) as file:
                    file.readline()
                    yield from file
        try:
            for receipt in self.conn.execute("SELECT * FROM receipts ORDER BY entry_id"):
                require(receipt["entry_id"] == through + 1, "MANIFEST_STREAM_HOLE")
                chain = digest(chain + receipt["document"])
                require(chain == receipt["chain_hash"], "MANIFEST_RECEIPT_HASH")
                item = json.loads(receipt["document"])
                require(item["entry_id"] == receipt["entry_id"], "MANIFEST_RECEIPT_ID")
                if item["kind"] == "MESSAGE":
                    name = item["segment"]
                    if current_name != name:
                        if stream is not None:
                            require(next(stream, None) is None, "MANIFEST_UNRECEIPTED_RECORD")
                        if archive is not None and archive is not self.archive:
                            archive.__exit__()
                        archive = self.archive if name == self.active else Archive(
                            self.path / name, self.spool.room, self.shard_bytes, self.min_free_bytes)
                        result = archive.verify()
                        seg = self.segment(name)
                        require(seg is not None and result["tip_sha256"] == seg["tip_sha256"]
                                and result["cursor"] == seg["last_seq"] and result["generation"] == seg["generation"],
                                "MANIFEST_SEGMENT_MISMATCH")
                        stream, current_name = records(archive), name
                    raw = next(stream, None)
                    require(raw is not None and hashlib.sha256(raw).hexdigest() == item["record_sha256"]
                            and json.loads(raw)["seq"] == item["seq"], "MANIFEST_RECORD_MISMATCH")
                    messages += 1
                else:
                    gaps += item["kind"] in {"GAP", "BOOTSTRAP_UNOBSERVED_PREFIX"}
                    uncertain += item["kind"] in {"GENERATION_BOUNDARY", "BOUNDARY_OBSERVATION", "CONFLICT", "LATE_OBSERVATION"}
                through += 1
            if stream is not None:
                require(next(stream, None) is None, "MANIFEST_UNRECEIPTED_RECORD")
        finally:
            if stream is not None:
                stream.close()
            if archive is not None and archive is not self.archive:
                archive.__exit__()
        s = self.state()
        require((through, chain, messages, gaps, uncertain) ==
                (s["through_entry"], s["receipt_hash"], s["messages"], s["gaps"], s["unconfirmed"]),
                "MANIFEST_TOTAL_MISMATCH")
        return {**self.coverage(), "verified": True}
