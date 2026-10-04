"""SQLite state, evidence, and inbox, guarded by a process-lifetime flock."""

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time

from .protocol import (POLL_LIMIT, ObserverError, ProtocolAnomaly, content_values,
                       json_dump, validate_envelope, validate_room)

APPLICATION_ID = 0x54434F42
SCHEMA_VERSION = 2
# Local acceptance policies and Production storage ceilings, never claims about
# server seq or retention.
MAX_PREFIX_MISSING = 10000
# Inclusive per-epoch human review budgets, not hostile-server/loss thresholds.
# 16 discontinuities are a bounded manual review list (<= 3,200 messages in
# gap-bearing limit=200 polls). 50,000 unobserved seq allows five maximal
# prefixes, then requires review. Large records, prior epochs and non-gap
# traffic still need independent global capacity monitoring.
MAX_EPOCH_OPEN_GAPS = 16
MAX_EPOCH_UNOBSERVED = 50000
EVIDENCE_PREFIX_BYTES = 16 * 1024
MAX_EVIDENCE_BYTES = 128 * 1024**2
MAX_EVIDENCE_ROWS = 65_536
MAX_EVENT_ROWS = 262_144
MAX_DB_BYTES = 10 * 1024**3
MAX_WAL_BYTES = 128 * 1024**2
MIN_DISK_FREE = 10 * 1024**3
STORAGE_WARNING_UTILIZATION = 0.8
RUNNABLE = {"INITIALIZED", "RUNNING", "DEGRADED"}
STATUSES = RUNNABLE | {"NEEDS_RESYNC", "ERROR"}


class GapReviewRequired(ObserverError):
    """Local observation budget, not a protocol violation or physical loss."""

LEGACY_SCHEMA = (
    """CREATE TABLE state (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        room TEXT NOT NULL,
        server_generation INTEGER NOT NULL CHECK(server_generation>=0),
        observer_epoch INTEGER NOT NULL CHECK(observer_epoch>=1),
        init_anchor_seq INTEGER NOT NULL CHECK(init_anchor_seq>=1),
        poll_seq INTEGER NOT NULL CHECK(poll_seq>=init_anchor_seq),
        resolved_seq INTEGER NOT NULL CHECK(resolved_seq>=init_anchor_seq AND resolved_seq<=poll_seq),
        status TEXT NOT NULL CHECK(status IN ('INITIALIZED','RUNNING','DEGRADED','NEEDS_RESYNC','ERROR')),
        last_poll_at REAL, last_http_success_at REAL,
        last_valid_response_at REAL, last_message_saved_at REAL,
        consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_failures>=0),
        consecutive_protocol_anomalies INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_protocol_anomalies>=0)
    )""",
    """CREATE TABLE messages (
        room TEXT NOT NULL, observer_epoch INTEGER NOT NULL,
        server_generation INTEGER NOT NULL, seq INTEGER NOT NULL CHECK(seq>=1),
        ts_value TEXT, from_value TEXT, text_value TEXT,
        raw_record_json TEXT NOT NULL, validation_flags TEXT NOT NULL,
        ingest_source TEXT NOT NULL CHECK(ingest_source='poll'),
        ingested_at REAL NOT NULL, trust TEXT NOT NULL DEFAULT 'untrusted' CHECK(trust='untrusted'),
        signature_verified INTEGER DEFAULT NULL CHECK(signature_verified IS NULL),
        text_sha256 TEXT,
        PRIMARY KEY(room,observer_epoch,server_generation,seq)
    )""",
    """CREATE TABLE gaps (
        gap_id INTEGER PRIMARY KEY, room TEXT NOT NULL,
        observer_epoch INTEGER NOT NULL, server_generation INTEGER NOT NULL,
        start_seq INTEGER NOT NULL, end_seq INTEGER NOT NULL CHECK(end_seq>=start_seq),
        detected_at REAL NOT NULL, status TEXT NOT NULL CHECK(status='OPEN'),
        recovery_deadline_hint REAL,
        UNIQUE(room,observer_epoch,server_generation,start_seq)
    )""",
    """CREATE TABLE evidence (
        evidence_id INTEGER PRIMARY KEY, room TEXT NOT NULL, request_since INTEGER,
        http_status INTEGER, content_type TEXT, observed_at REAL NOT NULL,
        body_bytes BLOB NOT NULL, body_sha256 TEXT NOT NULL,
        truncated INTEGER NOT NULL CHECK(truncated IN (0,1)),
        anomaly_type TEXT NOT NULL
    )""",
    """CREATE TABLE events (
        event_id INTEGER PRIMARY KEY, occurred_at REAL NOT NULL,
        event_type TEXT NOT NULL, details_json TEXT NOT NULL,
        evidence_id INTEGER REFERENCES evidence(evidence_id)
    )""",
)

SCHEMA = LEGACY_SCHEMA + (
    """CREATE TABLE epoch_history (
        observer_epoch INTEGER PRIMARY KEY, room TEXT NOT NULL,
        server_generation INTEGER NOT NULL, snapshot_json TEXT NOT NULL,
        decision_json TEXT NOT NULL, approval_token TEXT NOT NULL,
        ended_at REAL NOT NULL
    )""",
    """CREATE TABLE evidence_metadata (
        evidence_id INTEGER PRIMARY KEY REFERENCES evidence(evidence_id),
        observer_epoch INTEGER NOT NULL, received_bytes INTEGER NOT NULL,
        hash_scope TEXT NOT NULL CHECK(hash_scope IN ('COMPLETE_RECEIVED_BODY','HTTP_PREFIX_ONLY')),
        retry_after TEXT, content_type_sha256 TEXT NOT NULL
    )""",
)


def noop_checkpoint(point):
    """Tests inject SIGKILL hooks here; there is no environment/CLI hook."""


def _private(path, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.geteuid():
        raise ObserverError("UNSAFE_STATE_PATH")
    if stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
        raise ObserverError("UNSAFE_STATE_PERMISSIONS")
    if not directory and info.st_nlink != 1:
        raise ObserverError("UNSAFE_STATE_HARDLINK")


class StateLock:
    def __init__(self, directory, create=False):
        self.directory = Path(directory).absolute()
        self.create = create
        self.fd = None

    def __enter__(self):
        if self.create:
            # No recursive creation in arbitrary parent paths.
            self.directory.mkdir(mode=0o700, exist_ok=True)
        if not self.directory.is_dir():
            raise ObserverError("STATE_DIRECTORY_MISSING")
        _private(self.directory, directory=True)
        lock = self.directory / "observer.lock"
        self.fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            _private(lock)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _connect(path, mode):
    conn = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True,
                           isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _verify(conn, room, version=SCHEMA_VERSION):
    if conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise ObserverError("INVALID_APPLICATION_ID")
    if conn.execute("PRAGMA user_version").fetchone()[0] != version:
        raise ObserverError("INVALID_SCHEMA_VERSION")
    if [row[0] for row in conn.execute("PRAGMA quick_check")] != ["ok"]:
        raise ObserverError("QUICK_CHECK_FAILED")
    # Reject additional tables, triggers, views, indexes, or changed schema.
    schema = {row[0]: row[1] for row in conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
    expected = {statement.split()[2]: statement for statement in
                (LEGACY_SCHEMA if version == 1 else SCHEMA)}
    if schema != expected:
        raise ObserverError("SCHEMA_MISMATCH")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ObserverError("FOREIGN_KEY_CHECK_FAILED")
    states = conn.execute("SELECT * FROM state").fetchall()
    if len(states) != 1 or states[0]["room"] != room:
        raise ObserverError("STATE_ROOM_MISMATCH")
    s = states[0]
    if s["singleton"] != 1 or s["status"] not in STATUSES:
        raise ObserverError("INVALID_STATE")
    for field in ("server_generation", "observer_epoch", "init_anchor_seq", "poll_seq",
                  "resolved_seq", "consecutive_failures", "consecutive_protocol_anomalies"):
        if type(s[field]) is not int:
            raise ObserverError("INVALID_STATE_INTEGER")
    epochs, decisions = [], []
    if version == SCHEMA_VERSION:
        for row in conn.execute("SELECT * FROM epoch_history ORDER BY observer_epoch"):
            old = json.loads(row["snapshot_json"])
            decision = json.loads(row["decision_json"])
            if (set(old) != set(s.keys()) or row["observer_epoch"] != len(epochs) + 1 or
                    old["observer_epoch"] != row["observer_epoch"] or
                    old["room"] != room or row["room"] != room or
                    old["server_generation"] != row["server_generation"] or
                    decision.get("before") != old or
                    decision.get("new_epoch") != old["observer_epoch"] + 1 or
                    hashlib.sha256(json_dump(decision).encode()).hexdigest() != row["approval_token"]):
                raise ObserverError("EPOCH_HISTORY_MISMATCH")
            epochs.append(old)
            decisions.append(decision)
    if s["observer_epoch"] != len(epochs) + 1:
        raise ObserverError("EPOCH_HISTORY_MISMATCH")
    epochs.append(dict(s))
    for decision, following in zip(decisions, epochs[1:]):
        if (decision["new_anchor"] != following["init_anchor_seq"] or
                decision["new_generation"] != following["server_generation"]):
            raise ObserverError("EPOCH_HISTORY_MISMATCH")
    for table, code in (("messages", "MESSAGE"), ("gaps", "GAP")):
        if conn.execute(f"SELECT 1 FROM {table} WHERE room<>? OR observer_epoch<1 OR observer_epoch>? LIMIT 1",
                        (room, s["observer_epoch"])).fetchone():
            raise ObserverError(code + "_IDENTITY_MISMATCH")
    for epoch in epochs:
        _verify_epoch(conn, epoch)
    return s


def _verify_epoch(conn, s):
    identity = (s["room"], s["observer_epoch"], s["server_generation"])
    for table, code in (("messages", "MESSAGE"), ("gaps", "GAP")):
        if conn.execute(f"SELECT 1 FROM {table} WHERE observer_epoch=? AND server_generation<>? LIMIT 1",
                        identity[1:]).fetchone():
            raise ObserverError(code + "_IDENTITY_MISMATCH")
    # Account for every position using disjoint message/gap intervals. This
    # detects logical cursor corruption which quick_check cannot detect.
    intervals = conn.execute("""SELECT seq AS start_seq, seq AS end_seq, 'message' AS kind
        FROM messages WHERE room=? AND observer_epoch=? AND server_generation=?
        UNION ALL SELECT start_seq,end_seq,'gap' FROM gaps
        WHERE room=? AND observer_epoch=? AND server_generation=? ORDER BY start_seq""", identity + identity)
    next_seq = s["init_anchor_seq"] + 1
    first_gap = None
    for interval in intervals:
        if interval["start_seq"] != next_seq:
            raise ObserverError("STATE_SEQUENCE_ACCOUNTING_FAILED")
        if interval["kind"] == "gap" and first_gap is None:
            first_gap = interval["start_seq"]
        next_seq = interval["end_seq"] + 1
    if next_seq != s["poll_seq"] + 1:
        raise ObserverError("STATE_CURSOR_MISMATCH")
    resolved = s["poll_seq"] if first_gap is None else first_gap - 1
    if s["resolved_seq"] != resolved:
        raise ObserverError("STATE_RESOLVED_MISMATCH")
    if s["status"] in RUNNABLE:
        if (first_gap is not None) != (s["status"] == "DEGRADED"):
            raise ObserverError("STATE_GAP_STATUS_MISMATCH")
        if s["consecutive_protocol_anomalies"] >= 3:
            raise ObserverError("STATE_ANOMALY_THRESHOLD")


def initialize(directory, room, envelope, reply, checkpoint=noop_checkpoint):
    """Caller holds StateLock. A failed init deliberately leaves stale evidence."""
    validate_room(room)
    directory = Path(directory).absolute()
    final = directory / "state.sqlite"
    temp = directory / "state.sqlite.init"
    if any(path.name.startswith("state.sqlite") for path in directory.iterdir()):
        raise ObserverError("INIT_PATH_ALREADY_EXISTS")
    if envelope["count"] == 0:
        raise ObserverError("INIT_FAILED_EMPTY_ROOM")
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    checkpoint("init_temp_created")
    conn = _connect(temp, "rw")
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        for statement in SCHEMA:
            conn.execute(statement)
            checkpoint("init_schema")
        anchor = envelope["messages"][-1]["seq"]
        conn.execute("""INSERT INTO state
            (singleton,room,server_generation,observer_epoch,init_anchor_seq,poll_seq,resolved_seq,status)
            VALUES(1,?,?,1,?,?,?,'INITIALIZED')""",
            (room, envelope["generation"], anchor, anchor, anchor))
        checkpoint("init_state_inserted")
        _evidence(conn, room, None, reply, "INIT_ANCHOR")
        conn.execute("COMMIT")
        checkpoint("init_committed")
    finally:
        conn.close()
    fd = os.open(temp, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    checkpoint("init_before_rename")
    # All supported writers hold this directory's flock; directory is private.
    if final.exists() or final.is_symlink():
        raise ObserverError("INIT_PATH_ALREADY_EXISTS")
    os.rename(temp, final)
    checkpoint("init_after_rename")
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _evidence(conn, room, since, reply, kind):
    usage = conn.execute("SELECT count(*),coalesce(sum(length(body_bytes)),0) FROM evidence").fetchone()
    body = reply.body[:EVIDENCE_PREFIX_BYTES]
    if usage[0] >= MAX_EVIDENCE_ROWS or usage[1] + len(body) > MAX_EVIDENCE_BYTES:
        raise ObserverError("STORAGE_BUDGET_EXHAUSTED")
    content_type = reply.content_type[:512]
    cursor = conn.execute("""INSERT INTO evidence
        (room,request_since,http_status,content_type,observed_at,body_bytes,body_sha256,truncated,anomaly_type)
        VALUES(?,?,?,?,?,?,?,?,?)""", (room, since, reply.status, content_type,
        reply.observed_at or time.time(), body, hashlib.sha256(reply.body).hexdigest(),
        int(reply.truncated or len(body) < len(reply.body)), kind[:128]))
    epoch = conn.execute("SELECT observer_epoch FROM state").fetchone()[0]
    conn.execute("INSERT INTO evidence_metadata VALUES(?,?,?,?,?,?)",
                 (cursor.lastrowid, epoch, len(reply.body),
                  "HTTP_PREFIX_ONLY" if reply.truncated else "COMPLETE_RECEIVED_BODY",
                  reply.retry_after[:512] if reply.retry_after is not None else None,
                  hashlib.sha256(reply.content_type.encode("utf-8", "surrogatepass")).hexdigest()))
    return cursor.lastrowid


class Store:
    def __init__(self, directory, room, readonly=False, busy_timeout=5000, recovery=False):
        self.directory = Path(directory).absolute()
        self.room = validate_room(room)
        path = self.directory / "state.sqlite"
        _private(self.directory, directory=True)
        if not path.exists():
            raise ObserverError("STATE_DB_MISSING")
        _private(path)
        if any(p.name.startswith("state.sqlite.init") for p in self.directory.iterdir()):
            raise ObserverError("STALE_INIT_FILE")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = self.directory / ("state.sqlite" + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                _private(sidecar)
        self.conn = _connect(path, "ro" if readonly else "rw")
        try:
            self.conn.execute("PRAGMA trusted_schema=OFF")
            self.conn.execute("PRAGMA foreign_keys=ON")
            if type(busy_timeout) is not int or not 0 <= busy_timeout <= 60000:
                raise ObserverError("INVALID_BUSY_TIMEOUT")
            self.conn.execute(f"PRAGMA busy_timeout={busy_timeout}")
            # A reader verifies a consistent WAL snapshot without taking flock.
            self.conn.execute("BEGIN")
            _verify(self.conn, room)
            self.conn.execute("COMMIT")
            if readonly:
                self.conn.execute("PRAGMA query_only=ON")
            else:
                if self.state()["status"] not in RUNNABLE and not recovery:
                    raise ObserverError("STATE_REQUIRES_HUMAN_REVIEW")
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=FULL")
                self.conn.execute("PRAGMA cache_size=-2048")
                self.conn.execute("PRAGMA wal_autocheckpoint=256")
                page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
                if self.conn.execute("PRAGMA page_count").fetchone()[0] * page_size > MAX_DB_BYTES:
                    raise ObserverError("STORAGE_BUDGET_EXHAUSTED")
                self.conn.execute(f"PRAGMA max_page_count={MAX_DB_BYTES // page_size}")
                settings = (self.conn.execute("PRAGMA journal_mode").fetchone()[0],
                            self.conn.execute("PRAGMA synchronous").fetchone()[0],
                            self.conn.execute("PRAGMA foreign_keys").fetchone()[0])
                if settings != ("wal", 2, 1):
                    raise ObserverError("SQLITE_CONFIGURATION_FAILED")
        except BaseException:
            self.conn.close()
            raise

    def close(self):
        self.conn.close()

    def resync_plan(self, anchor, generation, reason):
        """Offline human assertion, never a fabricated server/tail observation."""
        if (type(anchor) is not int or not 1 <= anchor <= 2**63 - 1 or
                type(generation) is not int or not 0 <= generation <= 2**63 - 1 or
                not isinstance(reason, str) or not reason.strip() or
                len(json_dump(reason)) > 1024):
            raise ObserverError("INVALID_RESYNC_DECISION")
        before = self.state()
        if before["observer_epoch"] >= 2**63 - 1:
            raise ObserverError("EPOCH_OUT_OF_RANGE")
        decision = {"action": "MANUAL_RESYNC_UNOBSERVED_NOT_RECOVERED", "before": before,
                    "new_anchor": anchor, "new_generation": generation,
                    "new_epoch": before["observer_epoch"] + 1, "reason": reason,
                    "anchor_source": "HUMAN_ASSERTED_NOT_LIVE_VERIFIED"}
        token = hashlib.sha256(json_dump(decision).encode()).hexdigest()
        return {"decision": decision, "approval_token": token}

    def resync(self, anchor, generation, reason, approval, checkpoint=noop_checkpoint):
        """Caller holds StateLock; exact review token is required on every resync."""
        with self.transaction():
            _verify(self.conn, self.room)
            plan = self.resync_plan(anchor, generation, reason)
            if not isinstance(approval, str) or approval != plan["approval_token"]:
                raise ObserverError("RESYNC_APPROVAL_MISMATCH")
            decision = plan["decision"]
            s = decision["before"]
            self.conn.execute("INSERT INTO epoch_history VALUES(?,?,?,?,?,?,?)",
                              (s["observer_epoch"], self.room, s["server_generation"],
                               json_dump(s), json_dump(decision), approval, time.time()))
            checkpoint("resync_history_inserted")
            self.conn.execute("""UPDATE state SET observer_epoch=?,server_generation=?,
                init_anchor_seq=?,poll_seq=?,resolved_seq=?,status='INITIALIZED',
                last_poll_at=NULL,last_http_success_at=NULL,last_valid_response_at=NULL,
                last_message_saved_at=NULL,consecutive_failures=0,consecutive_protocol_anomalies=0""",
                (decision["new_epoch"], generation, anchor, anchor, anchor))
            self.event("MANUAL_RESYNC", {"old_epoch": s["observer_epoch"],
                       "new_epoch": decision["new_epoch"], "approval_token": approval,
                       "old_gaps": "PRESERVED_OPEN_NOT_RECOVERED"})
            _verify(self.conn, self.room)
            checkpoint("resync_before_commit")
        checkpoint("resync_after_commit")

    @contextlib.contextmanager
    def transaction(self):
        self._capacity()
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

    def event(self, kind, details=None, evidence_id=None):
        encoded = json_dump(details or {})
        if len(encoded) > 4096 or len(kind) > 128:
            raise ObserverError("EVENT_METADATA_TOO_LARGE")
        if self.conn.execute("SELECT count(*) FROM events").fetchone()[0] >= MAX_EVENT_ROWS:
            raise ObserverError("STORAGE_BUDGET_EXHAUSTED")
        self.conn.execute("INSERT INTO events(occurred_at,event_type,details_json,evidence_id) VALUES(?,?,?,?)",
                          (time.time(), kind, encoded, evidence_id))

    def _capacity(self):
        if self.storage_budget()["state"] == "EXHAUSTED":
            # Preserve all evidence; cannot assume there is room to write ERROR.
            raise ObserverError("STORAGE_BUDGET_EXHAUSTED")

    def storage_budget(self):
        """Counts only. Caller may hold a read snapshot; file sizes are samples.

        Predictive capacity indicator, never a guarantee that an arbitrary next
        transaction fits. Freelist pages admit small writes but can be exhausted
        by a larger write. SQLITE_FULL is the authoritative write failure: it
        rolls back and exits through the CLI even if this remains WARNING/OK.
        No writes for warnings, and no reservation for an in-flight transaction.
        """
        evidence = self.conn.execute(
            "SELECT count(*),coalesce(sum(length(body_bytes)),0) FROM evidence").fetchone()
        page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
        db_bytes = self.conn.execute("PRAGMA page_count").fetchone()[0] * page_size
        reusable = self.conn.execute("PRAGMA freelist_count").fetchone()[0] * page_size
        wal = self.directory / "state.sqlite-wal"
        try:
            wal_bytes = wal.stat().st_size
        except FileNotFoundError:
            wal_bytes = 0
        pairs = {"evidence_rows": (evidence[0], MAX_EVIDENCE_ROWS),
                 "evidence_bytes": (evidence[1], MAX_EVIDENCE_BYTES),
                 "event_rows": (self.conn.execute("SELECT count(*) FROM events").fetchone()[0], MAX_EVENT_ROWS),
                 "db_bytes": (db_bytes, MAX_DB_BYTES), "wal_bytes": (wal_bytes, MAX_WAL_BYTES)}
        metrics = {key: {"used": used, "limit": limit, "remaining": max(0, limit - used),
                         "utilization": used / limit if limit else None}
                   for key, (used, limit) in pairs.items()}
        free = shutil.disk_usage(self.directory).free
        reasons = []
        for key in ("evidence_rows", "event_rows"):
            if metrics[key]["used"] >= metrics[key]["limit"]:
                reasons.append(key.upper())
        # Reserve one maximal evidence prefix before any ordinary transaction.
        if evidence[1] + EVIDENCE_PREFIX_BYTES > MAX_EVIDENCE_BYTES:
            reasons.append("EVIDENCE_BYTES")
        if db_bytes > MAX_DB_BYTES or db_bytes - reusable >= MAX_DB_BYTES:
            reasons.append("DB_BYTES")
        if wal_bytes > MAX_WAL_BYTES:
            reasons.append("WAL_BYTES")
        if free < MIN_DISK_FREE:
            reasons.append("DISK_FREE")
        warning = any(used >= limit * STORAGE_WARNING_UTILIZATION for used, limit in pairs.values())
        return {"state": "EXHAUSTED" if reasons else "WARNING" if warning else "OK",
                "warning_utilization": STORAGE_WARNING_UTILIZATION,
                "metrics": metrics, "exhausted_dimensions": reasons,
                "db_reusable_bytes": reusable,
                "disk_free_bytes": free, "min_disk_free_bytes": MIN_DISK_FREE,
                "disk_headroom_bytes": max(0, free - MIN_DISK_FREE),
                "next_evidence_reserve_bytes": EVIDENCE_PREFIX_BYTES}

    def epoch_gap_usage(self, epoch):
        row = self.conn.execute("""SELECT count(*),coalesce(sum(end_seq-start_seq+1),0)
            FROM gaps WHERE observer_epoch=? AND status='OPEN'""", (epoch,)).fetchone()
        return row[0], row[1]

    def start_poll(self):
        with self.transaction():
            self.conn.execute("UPDATE state SET last_poll_at=?", (time.time(),))

    def failure(self, kind, reply=None, protocol=False, terminal=False, delay_clamped=False):
        with self.transaction():
            s = self.state()
            evidence = None
            if reply is not None:
                evidence = _evidence(self.conn, self.room, s["poll_seq"], reply, kind)
            count = s["consecutive_protocol_anomalies"] + int(protocol)
            status = "ERROR" if terminal or count >= 3 else s["status"]
            self.conn.execute("""UPDATE state SET consecutive_failures=consecutive_failures+1,
                consecutive_protocol_anomalies=?,status=?,
                last_http_success_at=CASE WHEN ? THEN ? ELSE last_http_success_at END""",
                (count, status, bool(reply and reply.status == 200), time.time()))
            self.event(kind, {"retry_after_clamped": delay_clamped}, evidence)
        return status

    def generation_change(self, reply, observed):
        with self.transaction():
            s = self.state()
            evidence = _evidence(self.conn, self.room, s["poll_seq"], reply, "GENERATION_CHANGE")
            self.event("GENERATION_CHANGE", {"saved": s["server_generation"], "observed": observed}, evidence)
            self.conn.execute("UPDATE state SET status='NEEDS_RESYNC',last_http_success_at=?", (time.time(),))

    def save(self, envelope, checkpoint=noop_checkpoint):
        now = time.time()
        with self.transaction():
            s = self.state()
            if s["status"] not in RUNNABLE:
                raise ObserverError("STATE_REQUIRES_HUMAN_REVIEW")
            validate_envelope(envelope, self.room)
            messages = envelope["messages"]
            if envelope["generation"] != s["server_generation"]:
                raise ProtocolAnomaly("GENERATION_MISMATCH")
            if len(messages) > POLL_LIMIT:
                raise ProtocolAnomaly("RESPONSE_LIMIT_EXCEEDED")
            if messages and messages[0]["seq"] <= s["poll_seq"]:
                raise ProtocolAnomaly("OLD_RECORD_IN_NORMAL_POLL")
            if messages and messages[0]["seq"] - s["poll_seq"] - 1 > MAX_PREFIX_MISSING:
                raise ProtocolAnomaly("FORWARD_JUMP_REQUIRES_RESYNC")
            missing = messages[0]["seq"] - s["poll_seq"] - 1 if messages else 0
            if missing:
                count, unobserved = self.epoch_gap_usage(s["observer_epoch"])
                if count + 1 > MAX_EPOCH_OPEN_GAPS or unobserved + missing > MAX_EPOCH_UNOBSERVED:
                    # Reject before any inbox/gap/cursor mutation. Observer saves
                    # terminal review evidence after this transaction rolls back.
                    raise GapReviewRequired("EPOCH_GAP_BUDGET_REQUIRES_REVIEW")
            poll = s["poll_seq"]
            resolved = s["resolved_seq"]
            for message in messages:
                values = content_values(message)
                self.conn.execute("""INSERT INTO messages
                    (room,observer_epoch,server_generation,seq,ts_value,from_value,text_value,
                     raw_record_json,validation_flags,ingest_source,ingested_at,text_sha256)
                    VALUES(?,?,?,?,?,?,?,?,?,'poll',?,?)""",
                    (self.room,s["observer_epoch"],s["server_generation"],message["seq"],
                     values["ts_value"],values["from_value"],values["text_value"],
                     values["raw_record_json"],values["validation_flags"],now,values["text_sha256"]))
                checkpoint("message_inserted")
            if messages:
                first = messages[0]["seq"]
                if first > poll + 1:
                    # Newest-N selection/read window is not evidence of loss or a TTL.
                    hint = None
                    self.conn.execute("""INSERT INTO gaps
                        (room,observer_epoch,server_generation,start_seq,end_seq,detected_at,status,recovery_deadline_hint)
                        VALUES(?,?,?,?,?,?,'OPEN',?)""",
                        (self.room,s["observer_epoch"],s["server_generation"],poll+1,first-1,now,hint))
                    self.event("GAP_DETECTED", {"start_seq": poll+1, "end_seq": first-1,
                               "meaning": "UNOBSERVED_NORMAL_READ", "physical_loss": "UNKNOWN"})
                    checkpoint("gap_inserted")
                poll = messages[-1]["seq"]
            has_gap = self.conn.execute("SELECT 1 FROM gaps WHERE observer_epoch=? AND status='OPEN' LIMIT 1",
                                        (s["observer_epoch"],)).fetchone() is not None
            if not has_gap:
                resolved = poll
            status = "DEGRADED" if has_gap else "RUNNING"
            if status != s["status"]:
                self.event("STATE_TRANSITION", {"from": s["status"], "to": status})
            checkpoint("before_cursor_update")
            self.conn.execute("""UPDATE state SET poll_seq=?,resolved_seq=?,status=?,
                last_http_success_at=?,last_valid_response_at=?,
                last_message_saved_at=CASE WHEN ? THEN ? ELSE last_message_saved_at END,
                consecutive_failures=0,consecutive_protocol_anomalies=0""",
                (poll,resolved,status,now,now,bool(messages),now))
            checkpoint("before_commit")
        checkpoint("after_commit")

    def heartbeat(self):
        # Consistent state+gap snapshot even while a different process commits.
        self.conn.execute("BEGIN")
        try:
            s = self.state()
            gaps = self.conn.execute("SELECT count(*),min(recovery_deadline_hint) FROM gaps WHERE observer_epoch=? AND status='OPEN'",
                                     (s["observer_epoch"],)).fetchone()
            historical = self.conn.execute("SELECT count(*) FROM gaps WHERE observer_epoch<? AND status='OPEN'",
                                           (s["observer_epoch"],)).fetchone()[0]
            _, unobserved = self.epoch_gap_usage(s["observer_epoch"])
            budget = self.storage_budget()
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        s.pop("singleton")
        s["open_gap_count"] = gaps[0]
        s["historical_open_gap_count"] = historical
        s["cumulative_unobserved_seq_count"] = unobserved
        s["epoch_gap_budget"] = {"open_gap_limit": MAX_EPOCH_OPEN_GAPS,
                                 "unobserved_seq_limit": MAX_EPOCH_UNOBSERVED,
                                 "boundary": "INCLUSIVE"}
        s["storage_budget"] = budget
        # Persisted state may be stale if disk/full prevented writing ERROR.
        s["effective_status"] = "ERROR" if budget["state"] == "EXHAUSTED" else s["status"]
        s["human_review_required"] = s["effective_status"] not in RUNNABLE
        s["gap_meaning"] = "UNOBSERVED_NORMAL_READ_PHYSICAL_LOSS_UNKNOWN"
        s["oldest_gap_recovery_deadline_hint"] = gaps[1]
        s["disk_free_bytes"] = budget["disk_free_bytes"]
        return s


def migrate_v1(directory, room, approval=None, checkpoint=noop_checkpoint):
    """Explicit, transactional additive migration. Caller holds StateLock.

    Without approval returns the concrete plan; does not write the database.
    All v1 tables/rows (including large legacy evidence) remain unchanged.
    """
    directory = Path(directory).absolute()
    validate_room(room)
    _private(directory, directory=True)
    path = directory / "state.sqlite"
    _private(path)
    for item in directory.iterdir():
        if item.name.startswith("state.sqlite.init"):
            raise ObserverError("STALE_INIT_FILE")
        if item.name in ("state.sqlite-wal", "state.sqlite-shm", "state.sqlite-journal"):
            _private(item)
    conn = _connect(path, "ro" if approval is None else "rw")
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("BEGIN" if approval is None else "BEGIN IMMEDIATE")
        s = dict(_verify(conn, room, version=1))
        plan = {"action": "MIGRATE_V1_TO_V2_PRESERVE_ALL_ROWS", "state": s,
                "counts": {name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                           for name in ("messages", "gaps", "events", "evidence")}}
        token = hashlib.sha256(json_dump(plan).encode()).hexdigest()
        if approval is not None:
            if approval != token:
                raise ObserverError("MIGRATION_APPROVAL_MISMATCH")
            if shutil.disk_usage(directory).free < MIN_DISK_FREE:
                raise ObserverError("STORAGE_BUDGET_EXHAUSTED")
            for statement in SCHEMA[len(LEGACY_SCHEMA):]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            _verify(conn, room)
            checkpoint("migration_before_commit")
        conn.execute("COMMIT")
        return {"decision": plan, "approval_token": token, "applied": approval is not None}
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
