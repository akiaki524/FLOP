"""Room-local durable observations. No networking, deletion, or archive work.

The stream is ordered by local entry_id, not remote seq. Message identities are
local epoch + seq; generation is an observed, non-atomic server claim. One
producer holds producer.lock; consumers use short independent transactions.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .archive import MAX_LINE, noop, record, regular_open, sync_directory
from technocore_observer.protocol import MAX_BODY, POLL_LIMIT, ObserverError, validate_envelope, validate_room

APPLICATION_ID = 0x54435350
SCHEMA_VERSION = 1
MAX_RECOVERY_BYTES = 32 * 1024 * 1024
MAX_RECOVERY_MESSAGES = 100_000
# A recovery shares a 256 MiB production cgroup with Python, SQLite and its
# page cache. Keep each raw/normalized artifact at or below 1/32 of that limit;
# the runtime derives a smaller cap when spool disk/DB/WAL headroom requires it.
MAX_RECOVERY_STAGING_BYTES = MAX_BODY * 2
MIN_RECOVERY_STAGING_BYTES = MAX_LINE * 2
CONSUMER = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}\Z")
SCHEMA = (
    """CREATE TABLE state (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        spool_id TEXT NOT NULL, room TEXT NOT NULL, epoch INTEGER NOT NULL,
        generation INTEGER, cursor INTEGER NOT NULL CHECK(cursor>=0),
        high_entry INTEGER NOT NULL DEFAULT 0, messages INTEGER NOT NULL DEFAULT 0,
        gaps INTEGER NOT NULL DEFAULT 0, conflicts INTEGER NOT NULL DEFAULT 0,
        last_response_at REAL, last_commit_at REAL, last_failure TEXT)""",
    """CREATE TABLE epochs (epoch INTEGER PRIMARY KEY, generation INTEGER NOT NULL,
        started_at REAL NOT NULL, binding TEXT NOT NULL,
        previous_tail TEXT NOT NULL)""",
    """CREATE TABLE batches (batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
        epoch INTEGER NOT NULL REFERENCES epochs(epoch), request_epoch INTEGER NOT NULL,
        request_since INTEGER NOT NULL, observed_generation INTEGER NOT NULL,
        received_at REAL NOT NULL, response_sha256 TEXT NOT NULL,
        response_count INTEGER NOT NULL, disposition TEXT NOT NULL)""",
    """CREATE TABLE entries (entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
        epoch INTEGER NOT NULL REFERENCES epochs(epoch), batch_id INTEGER NOT NULL REFERENCES batches(batch_id),
        kind TEXT NOT NULL, seq INTEGER, end_seq INTEGER, payload TEXT NOT NULL)""",
    """CREATE UNIQUE INDEX message_identity ON entries(epoch,seq) WHERE kind='MESSAGE'""",
    """CREATE TABLE consumers (name TEXT PRIMARY KEY, destination TEXT NOT NULL,
        required INTEGER NOT NULL CHECK(required IN (0,1)),
        ack_entry INTEGER NOT NULL DEFAULT 0 CHECK(ack_entry>=0), receipt TEXT,
        updated_at REAL NOT NULL)""",
    """CREATE TABLE failures (failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
        epoch INTEGER NOT NULL, request_since INTEGER NOT NULL, code TEXT NOT NULL,
        observed_at REAL NOT NULL, body_sha256 TEXT, body_prefix BLOB, truncated INTEGER NOT NULL)""",
)
RAW_SCHEMA = """CREATE TABLE response_raw (batch_id INTEGER NOT NULL REFERENCES batches(batch_id),
    source TEXT NOT NULL CHECK(source IN ('POLL_HTTP_BODY','RECOVERY_TRIGGER_POLL_HTTP_BODY',
        'RECOVERY_CONFIRMATION_POLL_HTTP_BODY')), body BLOB NOT NULL,
    body_sha256 TEXT NOT NULL, encoding TEXT NOT NULL CHECK(encoding='HTTP_DECODED_BODY'),
    PRIMARY KEY(batch_id,source))"""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def require(condition, code):
    if not condition:
        raise ObserverError(code)


def checked_directory(directory):
    path = Path(directory).absolute()
    require(path.is_dir() and not path.is_symlink(), "SPOOL_DIRECTORY_REQUIRED")
    return path


class Spool:
    def __init__(self, directory, room, *, producer=False, create=False,
                 min_free_bytes=256 * 1024 * 1024, max_db_bytes=1024 * 1024 * 1024,
                 max_wal_bytes=64 * 1024 * 1024, checkpoint=noop, raw_responses=False):
        self.path, self.room = checked_directory(directory), validate_room(room)
        self.producer, self.checkpoint = producer, checkpoint
        self.raw_responses = raw_responses
        self.min_free_bytes, self.max_db_bytes, self.max_wal_bytes = min_free_bytes, max_db_bytes, max_wal_bytes
        require(all(type(v) is int and v >= 0 for v in (min_free_bytes, max_db_bytes, max_wal_bytes))
                and max_db_bytes >= 1024 * 1024 and max_wal_bytes >= MAX_BODY, "SPOOL_INVALID_LIMIT")
        self.lock = self.conn = None
        try:
            if producer:
                self.lock = regular_open(self.path / "producer.lock", os.O_CREAT | os.O_WRONLY)
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            db = self.path / "spool.sqlite"
            require(not (self.path / ".spool.init").exists(), "SPOOL_INCOMPLETE_INITIALIZATION")
            if not db.exists():
                require(create and producer, "SPOOL_MISSING")
                self._initialize(db)
            for suffix in ("", "-wal", "-shm", "-journal"):
                file = Path(str(db) + suffix)
                if file.exists() or file.is_symlink():
                    with regular_open(file, os.O_RDONLY):
                        pass
            self.conn = sqlite3.connect(db.as_uri() + "?mode=rw", uri=True,
                                        isolation_level=None, timeout=0.25)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA trusted_schema=OFF")
            self.conn.execute("PRAGMA foreign_keys=ON")
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            require(self.conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
                    and version in (SCHEMA_VERSION, 2) and (not raw_responses or version == 2)
                    and (not producer or version != 2 or raw_responses),
                    "SPOOL_SCHEMA_MISMATCH")
            self.conn.execute("BEGIN")
            actual = {row[0]: row[1] for row in self.conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name<>'sqlite_sequence'")}
            schema = SCHEMA + ((RAW_SCHEMA,) if version == 2 else ())
            expected = {sql.split()[3] if sql.startswith("CREATE UNIQUE") else sql.split()[2]: sql for sql in schema}
            require(actual == expected, "SPOOL_SCHEMA_MISMATCH")
            state = self.state()
            require(state["room"] == self.room, "SPOOL_ROOM_MISMATCH")
            require(all(type(state[key]) is int and state[key] >= 0 for key in
                        ("epoch", "cursor", "high_entry", "messages", "gaps", "conflicts")), "SPOOL_BAD_STATE")
            require(self.conn.execute("SELECT coalesce(max(entry_id),0) FROM entries").fetchone()[0]
                    == state["high_entry"], "SPOOL_HIGH_ENTRY_MISMATCH")
            if state["epoch"]:
                epoch = self.conn.execute("SELECT generation FROM epochs WHERE epoch=?", (state["epoch"],)).fetchone()
                require(epoch is not None and epoch[0] == state["generation"], "SPOOL_EPOCH_MISMATCH")
            else:
                require(state["cursor"] == state["high_entry"] == 0 and state["generation"] is None,
                        "SPOOL_BAD_STATE")
            if state["cursor"]:
                tip = self.conn.execute("SELECT payload FROM entries WHERE epoch=? AND seq=? AND kind='MESSAGE'",
                                        (state["epoch"], state["cursor"])).fetchone()
                require(tip is not None and json.loads(tip[0])["seq"] == state["cursor"], "SPOOL_CURSOR_WITHOUT_MESSAGE")
            self.conn.execute("COMMIT")
            require(self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal", "SPOOL_WAL_REQUIRED")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA wal_autocheckpoint=256")
            self.conn.execute("PRAGMA cache_size=-2048")
            pages = max_db_bytes // self.conn.execute("PRAGMA page_size").fetchone()[0]
            require(self.conn.execute(f"PRAGMA max_page_count={pages}").fetchone()[0] == pages,
                    "SPOOL_DB_LIMIT_EXCEEDED")
            require(self.conn.execute("PRAGMA synchronous").fetchone()[0] == 2, "SPOOL_FULL_SYNC_REQUIRED")
        except BaseException:
            self.close()
            raise

    def _initialize(self, db):
        require(not db.is_symlink(), "SPOOL_UNSAFE_PATH")
        require(set(p.name for p in self.path.iterdir()) == {"producer.lock"}, "SPOOL_FOREIGN_DIRECTORY")
        temp = self.path / ".spool.init"
        with regular_open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL):
            pass
        conn = sqlite3.connect(temp, isolation_level=None)
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
            conn.execute(f"PRAGMA user_version={2 if self.raw_responses else SCHEMA_VERSION}")
            for sql in SCHEMA + ((RAW_SCHEMA,) if self.raw_responses else ()):
                conn.execute(sql)
            conn.execute("INSERT INTO state(singleton,spool_id,room,epoch,cursor) VALUES(1,?,?,0,0)",
                         (uuid.uuid4().hex, self.room))
            conn.execute("COMMIT")
        finally:
            conn.close()
        with regular_open(temp, os.O_RDONLY) as file:
            os.fsync(file.fileno())
        os.rename(temp, db)
        sync_directory(self.path)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def capacity(self, reserve_bytes=MAX_BODY * 2):
        info = os.statvfs(self.path)
        wal = self.path / "spool.sqlite-wal"
        require(info.f_bavail * info.f_frsize >= self.min_free_bytes + reserve_bytes,
                "SPOOL_STORAGE_LOW")
        if wal.exists() and wal.stat().st_size > self.max_wal_bytes:
            # Historical allocation is not live pressure. TRUNCATE reclaims it
            # only when SQLite can safely checkpoint every frame and release
            # readers. The connection's 250 ms busy timeout bounds this probe.
            # A pinned reader leaves the guard active; never remove WAL files.
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        require(not wal.exists() or wal.stat().st_size <= self.max_wal_bytes, "SPOOL_WAL_LIMIT")

    def recovery_limit(self):
        """Return a conservative export/staging cap derived from live storage.

        Recovery is exceptional and uses the spool volume, never container
        /tmp. Quartering each live budget leaves room for the downloaded file,
        normalized staging, SQLite pages/WAL and concurrent filesystem use.
        """
        info = os.statvfs(self.path)
        free_headroom = max(0, info.f_bavail * info.f_frsize - self.min_free_bytes)
        page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
        # The confirmation poll has not arrived yet. Reserve both maximum raw
        # responses before starting an export for an important Room.
        raw_allowance = 2 * MAX_BODY + 4 * page_size if self.raw_responses else 0
        db_headroom = max(0, self._db_headroom_bytes() - raw_allowance - MAX_LINE)
        wal = self.path / "spool.sqlite-wal"
        wal_bytes = 0
        if wal.exists() or wal.is_symlink():
            with regular_open(wal, os.O_RDONLY) as file:
                wal_bytes = os.fstat(file.fileno()).st_size
        wal_headroom = max(0, self.max_wal_bytes - wal_bytes)
        limit = min(MAX_RECOVERY_BYTES, MAX_RECOVERY_STAGING_BYTES,
                    wal_headroom // 4, db_headroom // 4, free_headroom // 4)
        require(limit >= MIN_RECOVERY_STAGING_BYTES, "RECOVERY_CAPACITY_INSUFFICIENT")
        return limit

    def _db_headroom_bytes(self):
        page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
        used = self.conn.execute("PRAGMA page_count").fetchone()[0] * page_size
        page_cap = self.conn.execute("PRAGMA max_page_count").fetchone()[0] * page_size
        return max(0, min(self.max_db_bytes, page_cap) - used)

    @contextmanager
    def transaction(self, reserve_bytes=MAX_BODY * 2):
        self.capacity(reserve_bytes)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def state(self):
        return dict(self.conn.execute("SELECT * FROM state WHERE singleton=1").fetchone())

    def _entry(self, epoch, batch, kind, payload, seq=None, end=None):
        return self.conn.execute("INSERT INTO entries(epoch,batch_id,kind,seq,end_seq,payload) VALUES(?,?,?,?,?,?)",
                                 (epoch, batch, kind, seq, end, canonical(payload))).lastrowid

    def ingest(self, envelope, *, request_epoch=None, request_since=None, response_sha256=None,
               recovered=False, raw_body=None):
        require(self.producer, "SPOOL_PRODUCER_REQUIRED")
        require(not self.raw_responses or raw_body is not None, "SPOOL_RAW_RESPONSE_REQUIRED")
        if raw_body is not None:
            require(self.raw_responses and isinstance(raw_body, bytes) and len(raw_body) <= MAX_BODY
                    and response_sha256 == hashlib.sha256(raw_body).hexdigest(), "SPOOL_RAW_RESPONSE_INVALID")
        validate_envelope(envelope, self.room)
        messages = envelope["messages"]
        require(len(messages) <= (MAX_RECOVERY_MESSAGES if recovered else POLL_LIMIT), "SPOOL_PAGE_LIMIT")
        encoded = [canonical(message) for message in messages]
        require(all(len(raw) + 1 <= MAX_LINE for raw in encoded), "SPOOL_RECORD_LIMIT")
        require(sum(map(len, encoded)) <= (MAX_RECOVERY_BYTES if recovered else MAX_BODY),
                "SPOOL_NORMALIZED_PAGE_LIMIT")
        observed = envelope["generation"]
        now = time.time()
        reserve = MAX_RECOVERY_BYTES * 2 if recovered else MAX_BODY * (3 if raw_body is not None else 2)
        with self.transaction(reserve):
            s = self.state()
            epoch_before = s["epoch"] if request_epoch is None else request_epoch
            since = s["cursor"] if request_since is None else request_since
            require(epoch_before == s["epoch"] and since == s["cursor"], "SPOOL_STALE_REQUEST")
            changed = s["generation"] is not None and observed != s["generation"]
            if recovered:
                require(observed == s["generation"] and messages and
                        messages[0]["seq"] == s["cursor"] + 1, "SPOOL_STALE_RECOVERY")
            new_epoch = s["generation"] is None or changed
            epoch = s["epoch"] + int(new_epoch)
            cursor = 0 if new_epoch else s["cursor"]
            if new_epoch:
                self.conn.execute("INSERT INTO epochs VALUES(?,?,?,?,?)", (
                    epoch, observed, now, "OBSERVED_NOT_ATOMIC", "UNKNOWN" if changed else "NOT_OBSERVED"))
            batch = self.conn.execute("""INSERT INTO batches(epoch,request_epoch,request_since,
                observed_generation,received_at,response_sha256,response_count,disposition)
                VALUES(?,?,?,?,?,?,?,?)""", (epoch, epoch_before, since, observed, now,
                response_sha256 or digest(canonical(envelope)), len(messages),
                "RECOVERED_EXPORT" if recovered else "BOUNDARY_UNCONFIRMED" if changed else "OBSERVED")).lastrowid
            if raw_body is not None:
                self.conn.execute("INSERT INTO response_raw VALUES(?,?,?,?,?)",
                                  (batch, "POLL_HTTP_BODY", raw_body, response_sha256, "HTTP_DECODED_BODY"))
            if new_epoch:
                self._entry(epoch, batch, "GENERATION_BOUNDARY" if changed else "EPOCH_START", {
                    "observed_generation": observed, "previous_epoch": s["epoch"],
                    "previous_cursor": s["cursor"], "binding": "OBSERVED_NOT_ATOMIC",
                    "previous_tail": "UNKNOWN", "request_since": since})
            saved = gaps = conflicts = 0
            if changed:
                # Preserve every received value, but do not assign these values
                # as proven messages of the new generation or inherit old since.
                self._entry(epoch, batch, "BOUNDARY_OBSERVATION", envelope)
            else:
                for message, raw in zip(messages, encoded):
                    seq = message["seq"]
                    existing = self.conn.execute(
                        "SELECT payload FROM entries WHERE epoch=? AND seq=? AND kind='MESSAGE'",
                        (epoch, seq)).fetchone()
                    if existing is not None:
                        if existing[0] != raw:
                            self._entry(epoch, batch, "CONFLICT", {
                                "seq": seq, "existing_sha256": digest(existing[0]),
                                "received_sha256": digest(raw), "received": message}, seq)
                            conflicts += 1
                        continue
                    if seq <= cursor:
                        # A late backfill is evidence, never silently closes a gap.
                        self._entry(epoch, batch, "LATE_OBSERVATION", message, seq)
                        continue
                    if seq > cursor + 1:
                        self._entry(epoch, batch, "BOOTSTRAP_UNOBSERVED_PREFIX" if cursor == 0 else "GAP", {
                            "meaning": "UNOBSERVED_NORMAL_READ", "physical_loss": "UNKNOWN"}, cursor + 1, seq - 1)
                        gaps += 1
                        self.checkpoint("gap_inserted")
                    self._entry(epoch, batch, "MESSAGE", message, seq)
                    saved += 1
                    cursor = seq
                    self.checkpoint("message_inserted")
                if not messages:
                    self._entry(epoch, batch, "EMPTY_OBSERVATION", {
                        "echoed_last_seq": envelope["last_seq"], "high_water_proven": False})
            self.checkpoint("before_cursor_update")
            high = self.conn.execute("SELECT coalesce(max(entry_id),0) FROM entries").fetchone()[0]
            self.conn.execute("""UPDATE state SET epoch=?,generation=?,cursor=?,high_entry=?,
                messages=messages+?,gaps=gaps+?,conflicts=conflicts+?,last_response_at=?,last_commit_at=?,last_failure=NULL
                WHERE singleton=1""", (epoch, observed, cursor, high, saved, gaps, conflicts, now, now))
            self.checkpoint("before_commit")
        self.checkpoint("after_commit")
        return {"saved": saved, "gaps": gaps, "conflicts": conflicts,
                "generation_changed": changed, "cursor": cursor, "batch_id": batch}

    def ingest_recovery(self, path, *, generation, request_epoch, request_since,
                        last_seq, count, response_sha256, raw_body=None,
                        confirmation_raw_body=None):
        """Atomically ingest canonical contiguous NDJSON without materializing it.

        The caller stages a fully selected recovery range below the spool in a
        dedicated temporary directory. Validation and inserts share one SQLite
        transaction, so malformed/truncated/stale input leaves no partial batch.
        """
        require(self.producer, "SPOOL_PRODUCER_REQUIRED")
        require(not self.raw_responses or raw_body is not None and confirmation_raw_body is not None,
                "SPOOL_RAW_RESPONSE_REQUIRED")
        if raw_body is not None or confirmation_raw_body is not None:
            require(self.raw_responses and isinstance(raw_body, bytes) and len(raw_body) <= MAX_BODY
                    and isinstance(confirmation_raw_body, bytes) and len(confirmation_raw_body) <= MAX_BODY,
                    "SPOOL_RAW_RESPONSE_INVALID")
        require(all(type(v) is int and 0 <= v <= 2**63 - 1 for v in
                    (generation, request_epoch, request_since, last_seq, count)),
                "SPOOL_INVALID_RECOVERY")
        require(0 < count <= MAX_RECOVERY_MESSAGES and last_seq > request_since
                and count == last_seq - request_since, "SPOOL_INVALID_RECOVERY")
        require(isinstance(response_sha256, str) and len(response_sha256) == 64
                and all(c in "0123456789abcdef" for c in response_sha256),
                "SPOOL_INVALID_RECOVERY")
        path = Path(path).absolute()
        parent = path.parent
        require(parent.parent == self.path and parent.name.startswith(".recovery-")
                and parent.is_dir() and not parent.is_symlink(), "SPOOL_UNSAFE_RECOVERY_PATH")
        limit = self.recovery_limit()
        size = path.stat().st_size
        require(size <= limit, "RECOVERY_NORMALIZED_LIMIT")
        now = time.time()
        hasher = hashlib.sha256()
        seen = saved = total = 0
        page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
        reserve = size * 2 + MAX_LINE + 4 * page_size
        if raw_body is not None:
            reserve += len(raw_body) + len(confirmation_raw_body)
        with self.transaction(reserve):
            # Check again under the SQLite writer lock. Consumer bookkeeping
            # may have used pages since the earlier export preflight.
            require(self._db_headroom_bytes() >= reserve, "RECOVERY_DB_HEADROOM_INSUFFICIENT")
            s = self.state()
            require(s["epoch"] == request_epoch and s["cursor"] == request_since,
                    "SPOOL_STALE_REQUEST")
            require(s["generation"] == generation, "SPOOL_STALE_RECOVERY")
            batch = self.conn.execute("""INSERT INTO batches(epoch,request_epoch,request_since,
                observed_generation,received_at,response_sha256,response_count,disposition)
                VALUES(?,?,?,?,?,?,?,?)""", (s["epoch"], request_epoch, request_since,
                generation, now, response_sha256, count, "RECOVERED_EXPORT")).lastrowid
            if raw_body is not None:
                self.conn.execute("INSERT INTO response_raw VALUES(?,?,?,?,?)",
                                  (batch, "RECOVERY_TRIGGER_POLL_HTTP_BODY", raw_body,
                                   hashlib.sha256(raw_body).hexdigest(), "HTTP_DECODED_BODY"))
                self.conn.execute("INSERT INTO response_raw VALUES(?,?,?,?,?)",
                                  (batch, "RECOVERY_CONFIRMATION_POLL_HTTP_BODY", confirmation_raw_body,
                                   hashlib.sha256(confirmation_raw_body).hexdigest(),
                                   "HTTP_DECODED_BODY"))
            with regular_open(path, os.O_RDONLY) as file:
                for raw in iter(lambda: file.readline(MAX_LINE + 1), b""):
                    total += len(raw)
                    require(total <= limit, "RECOVERY_NORMALIZED_LIMIT")
                    message = record(raw)
                    encoded = (canonical(message) + "\n").encode("ascii")
                    require(raw == encoded, "RECOVERY_NONCANONICAL_RECORD")
                    seen += 1
                    require(seen <= count and message["seq"] == request_since + seen,
                            "RECOVERY_SEQUENCE_MISMATCH")
                    existing = self.conn.execute(
                        "SELECT payload FROM entries WHERE epoch=? AND seq=? AND kind='MESSAGE'",
                        (s["epoch"], message["seq"])).fetchone()
                    require(existing is None, "SPOOL_STALE_RECOVERY")
                    self._entry(s["epoch"], batch, "MESSAGE", message, message["seq"])
                    saved += 1
                    hasher.update(raw)
                    self.checkpoint("message_inserted")
            require(seen == count and request_since + seen == last_seq,
                    "RECOVERY_SEQUENCE_MISMATCH")
            require(hasher.hexdigest() == response_sha256, "RECOVERY_HASH_MISMATCH")
            self.checkpoint("before_cursor_update")
            high = self.conn.execute("SELECT coalesce(max(entry_id),0) FROM entries").fetchone()[0]
            self.conn.execute("""UPDATE state SET cursor=?,high_entry=?,messages=messages+?,
                last_response_at=?,last_commit_at=?,last_failure=NULL WHERE singleton=1""",
                              (last_seq, high, saved, now, now))
            self.checkpoint("before_commit")
        self.checkpoint("after_commit")
        return {"saved": saved, "gaps": 0, "conflicts": 0,
                "generation_changed": False, "cursor": last_seq, "batch_id": batch}

    def failure(self, code, reply=None):
        require(self.producer and isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", code) is not None,
                "SPOOL_INVALID_FAILURE")
        body = reply.body if reply is not None else b""
        with self.transaction():
            s = self.state()
            self.conn.execute("""INSERT INTO failures(epoch,request_since,code,observed_at,body_sha256,body_prefix,truncated)
                VALUES(?,?,?,?,?,?,?)""", (s["epoch"], s["cursor"], code, time.time(),
                hashlib.sha256(body).hexdigest() if reply is not None else None,
                body[:16384], int(len(body) > 16384 or bool(reply and reply.truncated))))
            self.conn.execute("UPDATE state SET last_failure=? WHERE singleton=1", (code,))

    def register(self, name, destination, *, required=True):
        require(isinstance(name, str) and CONSUMER.fullmatch(name) is not None, "SPOOL_BAD_CONSUMER")
        require(isinstance(destination, str) and 1 <= len(destination) <= 4096, "SPOOL_BAD_DESTINATION")
        with self.transaction():
            self.conn.execute("INSERT OR IGNORE INTO consumers(name,destination,required,updated_at) VALUES(?,?,?,?)",
                              (name, destination, int(required), time.time()))
            row = self.consumer(name)
            require(row["destination"] == destination and row["required"] == int(required), "SPOOL_CONSUMER_BINDING")
        return row

    def consumer(self, name):
        row = self.conn.execute("SELECT * FROM consumers WHERE name=?", (name,)).fetchone()
        require(row is not None, "SPOOL_CONSUMER_MISSING")
        return dict(row)

    def read(self, after, limit=200):
        require(type(after) is int and after >= 0 and type(limit) is int and 1 <= limit <= 200,
                "SPOOL_INVALID_READ")
        self.conn.execute("BEGIN")
        try:
            s = self.state()
            rows, size = [], 0
            for row in self.conn.execute(
                    "SELECT * FROM entries WHERE entry_id>? ORDER BY entry_id LIMIT ?", (after, limit)):
                if rows and size + len(row["payload"]) > MAX_BODY:
                    break
                item = dict(row)
                item["batch"] = dict(self.conn.execute("SELECT * FROM batches WHERE batch_id=?",
                                                       (row["batch_id"],)).fetchone())
                rows.append(item)
                size += len(row["payload"])
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return rows, s

    def ack(self, name, expected, through, receipt):
        require(type(through) is int and expected <= through and isinstance(receipt, str)
                and len(receipt) == 64 and all(c in "0123456789abcdef" for c in receipt), "SPOOL_BAD_ACK")
        with self.transaction():
            require(through <= self.state()["high_entry"], "SPOOL_ACK_AHEAD")
            result = self.conn.execute("""UPDATE consumers SET ack_entry=?,receipt=?,updated_at=?
                WHERE name=? AND ack_entry=?""", (through, receipt, time.time(), name, expected))
            require(result.rowcount == 1, "SPOOL_ACK_RACE")

    def retention_watermark(self):
        # Informational only. This batch deliberately has NO delete/compact API.
        # A required consumer must be registered before enabling future retention.
        row = self.conn.execute("SELECT count(*),min(ack_entry) FROM consumers WHERE required=1").fetchone()
        return {"deletion_enabled": False, "required_consumers": row[0],
                "processed_through": row[1] if row[0] else 0}


class LocalConsumer:
    """At-least-once local stream. Caller persists output BEFORE acknowledge.

    Empty reads mean caught up to an observed local watermark, never a remote
    completeness claim. There is no HTTP client or fallback in this API.
    """
    def __init__(self, spool, name, destination):
        self.spool, self.name = spool, name
        spool.register(name, destination)
        self.pending = None

    def poll(self, limit=200):
        offset = self.spool.consumer(self.name)["ack_entry"]
        rows, state = self.spool.read(offset, limit)
        self.pending = (offset, rows[-1]["entry_id"] if rows else offset)
        return rows, {"producer_high_entry": state["high_entry"],
                      "last_commit_at": state["last_commit_at"],
                      "caught_up": self.pending[1] == state["high_entry"],
                      "remote_completeness": "UNKNOWN"}

    def acknowledge(self, durable_receipt):
        require(self.pending is not None, "SPOOL_NOT_DELIVERED")
        self.spool.ack(self.name, *self.pending, durable_receipt)
        self.pending = None
