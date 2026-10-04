"""The Analyzer's own SQLite store. Never an Observer file.

Two kinds of data live here and are treated differently:

* derived, rebuildable: ingested record cache, unit digests, coverage items, run
  outputs. ``rebuild`` may drop and re-derive them from the Evidence sources.
* preserved history that Evidence cannot reproduce: finding revisions, Human reviews,
  report submission events, resolutions, LLM task/results. These are append-only and
  never deleted by ``rebuild`` or by any automatic path.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .util import AnalyzerError, canonical, digest

APPLICATION_ID = 0x54414E41  # "TANA"
VERSION = 3
SQLITE_MAGIC = b"SQLite format 3\x00"

DERIVED = (
    """CREATE TABLE records (key TEXT PRIMARY KEY, source_id TEXT NOT NULL, stream TEXT NOT NULL,
        room TEXT NOT NULL, seq INTEGER, epoch INTEGER, generation INTEGER, ref TEXT NOT NULL,
        locator TEXT NOT NULL, message TEXT NOT NULL, record_sha256 TEXT NOT NULL,
        hash_scope TEXT NOT NULL, format_version TEXT NOT NULL, provenance TEXT NOT NULL,
        captured_at REAL, quality TEXT NOT NULL, sig_status TEXT NOT NULL, sig_detail TEXT,
        first_run TEXT NOT NULL, last_run TEXT NOT NULL)""",
    "CREATE INDEX records_room ON records(room, stream, seq)",
    """CREATE TABLE record_conflicts (conflict_id INTEGER PRIMARY KEY, key TEXT NOT NULL,
        existing_sha256 TEXT NOT NULL, observed_sha256 TEXT NOT NULL, observed_ref TEXT NOT NULL,
        observed_message TEXT NOT NULL, run_id TEXT NOT NULL, detected_at REAL NOT NULL,
        UNIQUE(key, observed_sha256))""",
    """CREATE TABLE units (source_id TEXT NOT NULL, unit TEXT NOT NULL, sha256 TEXT, status TEXT NOT NULL,
        detail TEXT, run_id TEXT NOT NULL, PRIMARY KEY(source_id, unit))""",
    """CREATE TABLE unit_changes (change_id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, unit TEXT NOT NULL,
        previous_sha256 TEXT, observed_sha256 TEXT, run_id TEXT NOT NULL, detected_at REAL NOT NULL)""",
    """CREATE TABLE coverage_items (key TEXT PRIMARY KEY, source_id TEXT NOT NULL, room TEXT NOT NULL,
        stream TEXT NOT NULL, kind TEXT NOT NULL, start_seq INTEGER, end_seq INTEGER,
        detail TEXT NOT NULL, ref TEXT NOT NULL, run_id TEXT NOT NULL)""",
    """CREATE TABLE source_progress (source_id TEXT PRIMARY KEY, through_entry INTEGER NOT NULL,
        chain_hash TEXT, run_id TEXT NOT NULL)""",
    """CREATE TABLE source_status (source_id TEXT PRIMARY KEY, document TEXT NOT NULL, run_id TEXT NOT NULL)""",
)
PRESERVED = (
    """CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
        status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCEEDED','PARTIAL','FAILED','DEFERRED')),
        mode TEXT NOT NULL, reference_time_ms INTEGER NOT NULL, as_of_ms INTEGER NOT NULL,
        config_sha256 TEXT NOT NULL, input_sha256 TEXT, output_dir TEXT, summary TEXT, error TEXT)""",
    """CREATE TABLE findings (finding_id TEXT PRIMARY KEY, category TEXT NOT NULL, rule_id TEXT NOT NULL,
        subject TEXT NOT NULL, first_run TEXT NOT NULL, latest_revision INTEGER NOT NULL)""",
    """CREATE TABLE finding_revisions (finding_id TEXT NOT NULL, revision INTEGER NOT NULL,
        run_id TEXT NOT NULL, change TEXT NOT NULL, document TEXT NOT NULL, document_sha256 TEXT NOT NULL,
        created_at REAL NOT NULL, PRIMARY KEY(finding_id, revision))""",
    """CREATE TABLE human_reviews (review_id INTEGER PRIMARY KEY, finding_id TEXT NOT NULL,
        revision INTEGER NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('CONFIRMED','REJECTED','UNREVIEWED')),
        note TEXT, reviewer TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin='HUMAN_CLI'),
        recorded_at REAL NOT NULL)""",
    """CREATE TABLE report_events (event_id INTEGER PRIMARY KEY, finding_id TEXT NOT NULL,
        finding_revision INTEGER, candidate_sha256 TEXT, state TEXT NOT NULL CHECK(state IN
        ('NOT_SUBMITTED','SUBMITTED','DELIVERY_UNKNOWN','ACKNOWLEDGED')),
        channel TEXT, detail TEXT, source_ref TEXT, actor TEXT NOT NULL,
        origin TEXT NOT NULL CHECK(origin='HUMAN_CLI'), recorded_at REAL NOT NULL)""",
    """CREATE TABLE resolutions (resolution_id INTEGER PRIMARY KEY, finding_id TEXT NOT NULL,
        finding_revision INTEGER, resolution TEXT NOT NULL CHECK(resolution IN
        ('OPEN','FIXED','FALSE_POSITIVE','DUPLICATE','NOT_A_BUG','UNKNOWN')),
        source_ref TEXT, note TEXT, actor TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin='HUMAN_CLI'),
        recorded_at REAL NOT NULL)""",
    """CREATE TABLE report_candidates (finding_id TEXT NOT NULL, revision INTEGER NOT NULL,
        variant TEXT NOT NULL CHECK(variant IN ('internal','public')), candidate_sha256 TEXT NOT NULL,
        run_id TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(finding_id, revision, variant))""",
    """CREATE TABLE semantic_tasks (task_id TEXT PRIMARY KEY, kind TEXT NOT NULL, purpose_key TEXT NOT NULL,
        bundle_sha256 TEXT NOT NULL, priority INTEGER NOT NULL, retain_class TEXT NOT NULL,
        expires_ms INTEGER, status TEXT NOT NULL, created_run TEXT NOT NULL, updated_at REAL NOT NULL,
        detail TEXT)""",
    """CREATE TABLE semantic_results (result_id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
        runtime TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL, outcome TEXT NOT NULL,
        output TEXT, validation TEXT NOT NULL, quarantined INTEGER NOT NULL)""",
)


def _immutable_unit(unit):
    """Only published Archive shards are immutable (`<stream>/shard-NNNNNNNNNNNN.jsonl`)."""
    name = unit.rsplit("/", 1)[-1]
    return name.startswith("shard-") and name.endswith(".jsonl")


def _require_identity(value):
    """Human-owned audit rows never store a blank or whitespace-only identity."""
    if not isinstance(value, str) or not value.strip():
        raise AnalyzerError("BLANK_HUMAN_IDENTITY")


@contextlib.contextmanager
def private_umask():
    """Analyzer state/output may hold observed text: files 0600, directories 0700."""
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _header(path):
    """Identify a SQLite file from its 100-byte header without opening it via SQLite
    (opening may change journal mode or create -wal/-shm next to a foreign file)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AnalyzerError("ANALYZER_STORE_NOT_REGULAR_FILE")
        raw = os.read(fd, 100)
    finally:
        os.close(fd)
    if len(raw) < 100 or raw[:16] != SQLITE_MAGIC:
        raise AnalyzerError("ANALYZER_STORE_NOT_OWN_DATABASE")
    return int.from_bytes(raw[68:72], "big"), int.from_bytes(raw[60:64], "big")


def secure_path(path, mode):
    """Bring one path we have established to be Analyzer-owned to ``mode``. Refuse symlinks,
    paths owned by someone else, and multiply-linked files (a hardlink would carry the chmod to
    another directory's inode). Source Evidence paths never pass through here."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
        raise AnalyzerError("ANALYZER_PATH_NOT_OWNED")
    if not stat.S_ISDIR(info.st_mode) and info.st_nlink != 1:
        raise AnalyzerError("ANALYZER_PATH_HARDLINKED")
    if stat.S_IMODE(info.st_mode) != mode:
        os.chmod(path, mode)


OUTPUT_MARKER = ".technocore-analyzer-output"
LEGACY_RUN_NAME = re.compile(r"(?:\.tmp-)?\d{8}T\d{6}Z-[0-9a-f]{6}\Z")


def claim_output_dir(root):
    """Establish that ``root`` is Analyzer's before anything below it is changed.

    New directory: create it private with a marker. Existing directory: proceed only if it
    carries our marker, or is empty, or has exactly the layout earlier Analyzer versions wrote
    (`latest.json` + `runs/<run id>`); then adopt it (write the marker) and correct modes.
    Anything else is not ours: refuse, change nothing."""
    root = Path(root)
    if not root.exists() and not root.is_symlink():
        root.mkdir(parents=True, mode=0o700)
    elif root.is_symlink() or not root.is_dir():
        raise AnalyzerError("ANALYZER_OUTPUT_DIR_UNSAFE")
    info = os.lstat(root)
    if info.st_uid != os.geteuid():
        raise AnalyzerError("ANALYZER_PATH_NOT_OWNED")
    marker = root / OUTPUT_MARKER
    if not marker.exists():
        names = {p.name for p in root.iterdir()}
        runs = root / "runs"
        legacy = names <= {"latest.json", "runs"} and (
            "runs" not in names or (runs.is_dir() and not runs.is_symlink()
                                    and all(LEGACY_RUN_NAME.fullmatch(p.name) for p in runs.iterdir())))
        if not legacy:
            raise AnalyzerError("ANALYZER_OUTPUT_DIR_NOT_ANALYZER_OWNED")
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    secure_path(root, 0o700)
    secure_path(marker, 0o600)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames:
            secure_path(Path(dirpath) / name, 0o700)
        for name in filenames:
            secure_path(Path(dirpath) / name, 0o600)


def prepare_state_dir(path, db_name, identified):
    """Tighten an existing state directory only when it is demonstrably ours: it holds an
    Analyzer DB that passed the header check (and nothing foreign), or it is empty. A
    directory that is already private is left alone."""
    if not path.exists():
        return
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
        raise AnalyzerError("ANALYZER_PATH_NOT_OWNED")
    if not stat.S_IMODE(info.st_mode) & 0o077:
        return
    entries = [p.name for p in path.iterdir()]
    if entries and not (identified and all(n.startswith(db_name) for n in entries)):
        raise AnalyzerError("ANALYZER_STATE_DIR_SHARED_AND_UNSAFE")
    secure_path(path, 0o700)


class Store:
    def __init__(self, path):
        self._lock_fd = None
        self.path = Path(path)
        if self.path.is_symlink():
            raise AnalyzerError("ANALYZER_STORE_SYMLINK")
        with private_umask():
            identified = False
            # Before any open/migration: a DB (or sidecar) that is hardlinked elsewhere, e.g. into an
            # Evidence tree, shares its inode there, so writing it would modify that other path.
            for suffix in ("", "-wal", "-shm", "-journal"):
                sidecar = Path(str(self.path) + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    info = os.lstat(sidecar)
                    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
                        raise AnalyzerError("ANALYZER_PATH_NOT_OWNED")
                    if info.st_nlink != 1:
                        raise AnalyzerError("ANALYZER_PATH_HARDLINKED")
            if self.path.exists() and self.path.stat().st_size == 0:
                # Our own creation interrupted before the schema commit: nothing to protect.
                new, version = True, VERSION
            elif self.path.exists():
                application_id, version = _header(self.path)
                if application_id != APPLICATION_ID or version not in (1, 2, VERSION):
                    # Checked before any chmod/PRAGMA/journal change: a foreign DB is never touched.
                    raise AnalyzerError("ANALYZER_STORE_FORMAT_MISMATCH")
                new, identified = False, True
            else:
                new, version = True, VERSION
            prepare_state_dir(self.path.parent, self.path.name, identified)
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600))
            self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            if new:
                self.conn.execute("BEGIN IMMEDIATE")
                for statement in DERIVED + PRESERVED:
                    self.conn.execute(statement)
                self.conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
                self.conn.execute(f"PRAGMA user_version={VERSION}")
                self.conn.execute("COMMIT")
            else:
                self._migrate(version)
            self.secure_files()

    def acquire_run_lock(self):
        """One analysis run per state DB. A non-blocking flock on `<db>.lock` is held until this
        Store closes; the kernel drops it when the process dies, so a RUNNING row is "interrupted"
        only when nobody holds the lock. A second run is refused before it changes anything
        (no recovery, no run row), which also serialises the real-runtime invocation budget."""
        path = Path(str(self.path) + ".lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise AnalyzerError("ANALYZER_PATH_NOT_OWNED")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AnalyzerError("ANALYZER_RUN_IN_PROGRESS") from None
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd = fd

    def secure_files(self):
        """Analyzer-owned files only: the state DB and its sidecars. Never Source paths."""
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(self.path) + suffix)
            if path.exists() or path.is_symlink():
                secure_path(path, 0o600)

    def _migrate(self, version):
        """v1 → v2 binds report events to a finding revision and records candidate digests;
        v2 → v3 binds resolutions to a finding revision. History rows are kept: a legacy row
        gets the revision that was current when it was recorded (from revision timestamps)."""
        if version == VERSION:
            return
        self.conn.execute("BEGIN IMMEDIATE")
        if version == 1:
            self.conn.execute("ALTER TABLE report_events ADD COLUMN finding_revision INTEGER")
            self.conn.execute(PRESERVED[-3])
        self.conn.execute("ALTER TABLE resolutions ADD COLUMN finding_revision INTEGER")
        for table, stamp in (("resolutions", "recorded_at"), ("report_events", "recorded_at")):
            self.conn.execute(
                f"UPDATE {table} SET finding_revision = COALESCE((SELECT MAX(r.revision) FROM finding_revisions r "
                f"WHERE r.finding_id={table}.finding_id AND r.created_at <= {table}.{stamp}), 1) "
                f"WHERE finding_revision IS NULL")
        self.conn.execute(f"PRAGMA user_version={VERSION}")
        self.conn.execute("COMMIT")

    def close(self):
        self.conn.close()
        fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            os.close(fd)  # releases the run lock

    def transaction(self):
        return _Tx(self.conn)

    # ── runs ──
    def recover_interrupted(self):
        """A run that never committed is FAILED, never silently completed."""
        self.conn.execute("UPDATE runs SET status='FAILED', error='INTERRUPTED_BEFORE_COMMIT', "
                          "finished_at=? WHERE status='RUNNING'", (time.time(),))

    def start_run(self, run_id, mode, reference_ms, as_of_ms, config_sha):
        self.conn.execute("INSERT INTO runs(run_id,started_at,status,mode,reference_time_ms,as_of_ms,"
                          "config_sha256) VALUES(?,?,?,?,?,?,?)",
                          (run_id, time.time(), "RUNNING", mode, reference_ms, as_of_ms, config_sha))

    def finish_run(self, run_id, status, input_sha, output_dir, summary, error=None):
        if status == "FAILED":
            # A failed run backs no candidate, even one committed just before a later step failed;
            # dropping its rows also frees (finding, revision, variant) for the next run to record.
            self.conn.execute("DELETE FROM report_candidates WHERE run_id=?", (run_id,))
        self.conn.execute("UPDATE runs SET status=?,finished_at=?,input_sha256=?,output_dir=?,summary=?,error=? "
                          "WHERE run_id=?", (status, time.time(), input_sha, output_dir,
                                             canonical(summary), error, run_id))

    def last_run(self, status=("SUCCEEDED", "PARTIAL"), mode=None):
        marks = ",".join("?" * len(status))
        where, args = f"status IN ({marks})", list(status)
        if mode is not None:
            where, args = where + " AND mode=?", args + [mode]
        row = self.conn.execute(f"SELECT * FROM runs WHERE {where} ORDER BY started_at DESC LIMIT 1",
                                args).fetchone()
        return dict(row) if row else None

    # ── ingestion ──
    def known_units(self, source_id):
        return {row["unit"]: row["sha256"] for row in
                self.conn.execute("SELECT unit,sha256 FROM units WHERE source_id=? AND status IN ('READ','SKIPPED_UNCHANGED')",
                                  (source_id,))}

    def source_progress(self, source_id):
        row = self.conn.execute("SELECT * FROM source_progress WHERE source_id=?", (source_id,)).fetchone()
        return dict(row) if row else None

    def has_record(self, key):
        return self.conn.execute("SELECT 1 FROM records WHERE key=?", (key,)).fetchone() is not None

    def record_unit(self, source_id, unit, run_id):
        previous = self.conn.execute("SELECT sha256 FROM units WHERE source_id=? AND unit=?",
                                     (source_id, unit.unit)).fetchone()
        if (previous is not None and previous[0] and unit.sha256 and previous[0] != unit.sha256
                and _immutable_unit(unit.unit)):
            # A published shard must never change; keep both facts. manifest.sqlite and Observer
            # state snapshots legitimately change between runs and are not tracked this way.
            self.conn.execute("INSERT INTO unit_changes(source_id,unit,previous_sha256,observed_sha256,run_id,detected_at) "
                              "VALUES(?,?,?,?,?,?)", (source_id, unit.unit, previous[0], unit.sha256, run_id, time.time()))
        if unit.status == "SKIPPED_UNCHANGED":
            self.conn.execute("UPDATE units SET run_id=? WHERE source_id=? AND unit=?", (run_id, source_id, unit.unit))
            return
        self.conn.execute("INSERT OR REPLACE INTO units VALUES(?,?,?,?,?,?)",
                          (source_id, unit.unit, unit.sha256, unit.status, unit.detail, run_id))

    def ingest_record(self, rec, sig_status, sig_detail, run_id):
        """Return NEW / SAME / CONFLICT. Same key + other content never overwrites."""
        row = self.conn.execute("SELECT record_sha256 FROM records WHERE key=?", (rec.key,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec.key, rec.source_id, rec.stream, rec.room, rec.seq, rec.epoch, rec.generation, rec.ref,
                 canonical(rec.locator), json.dumps(rec.message, ensure_ascii=True, allow_nan=False),
                 rec.record_sha256, rec.hash_scope, rec.format_version, rec.provenance, rec.captured_at,
                 canonical(rec.quality), sig_status, sig_detail, run_id, run_id))
            return "NEW"
        if row[0] == rec.record_sha256:
            self.conn.execute("UPDATE records SET last_run=? WHERE key=?", (run_id, rec.key))
            return "SAME"
        self.conn.execute("INSERT OR IGNORE INTO record_conflicts(key,existing_sha256,observed_sha256,observed_ref,"
                          "observed_message,run_id,detected_at) VALUES(?,?,?,?,?,?,?)",
                          (rec.key, row[0], rec.record_sha256, rec.ref,
                           json.dumps(rec.message, ensure_ascii=True, allow_nan=False), run_id, time.time()))
        return "CONFLICT"

    def recheck_signatures(self, verify, source_ids=None):
        changed = 0
        sql = "SELECT key,room,message FROM records WHERE sig_status='VERIFIER_UNAVAILABLE'"
        args = []
        if source_ids is not None:
            ids = sorted(set(source_ids))
            if not ids:
                return 0
            sql += " AND source_id IN (" + ",".join("?" for _ in ids) + ")"
            args.extend(ids)
        for row in self.conn.execute(sql, args).fetchall():
            status, detail = verify(row["room"], json.loads(row["message"]))
            if status != "VERIFIER_UNAVAILABLE":
                self.conn.execute("UPDATE records SET sig_status=?,sig_detail=? WHERE key=?", (status, detail, row["key"]))
                changed += 1
        return changed

    def put_coverage(self, item, run_id):
        self.conn.execute("INSERT OR REPLACE INTO coverage_items VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (item.key, item.source_id, item.room, item.stream, item.kind, item.start_seq,
                           item.end_seq, canonical(item.detail), item.ref, run_id))

    def reset_source(self, source_id):
        """A source ID that now names a different room/kind: everything cached under the old
        identity is dropped (rebuildable Evidence cache; findings/human history are untouched).
        That includes the integrity facts derived from that identity's reads: its unit changes
        (they carry source_id) and the record conflicts of its record keys (they carry only the
        key, whose owner prefix identifies the source). Other sources' rows are untouched."""
        # A record key is `<source_id>|<stream>|<seq>` (streams and seqs never contain "|"), so the
        # owner is recoverable from the key itself, also after `rebuild` has dropped the records.
        keys = [k for (k,) in self.conn.execute("SELECT DISTINCT key FROM record_conflicts")
                if k.rsplit("|", 2)[0] == source_id]
        self.conn.executemany("DELETE FROM record_conflicts WHERE key=?", [(k,) for k in keys])
        self.conn.execute("DELETE FROM unit_changes WHERE source_id=?", (source_id,))
        for table in ("records", "units", "coverage_items", "source_progress", "source_status"):
            self.conn.execute(f"DELETE FROM {table} WHERE source_id=?", (source_id,))

    def drop_pending_checkpoint(self, source_id, streams):
        """SHARD_PENDING_CHECKPOINT is transient: it holds only while the successor shard is
        still outside capture.json. Streams whose checkpoint was read this run re-emit it if
        it still holds; streams not read keep their last known item."""
        self.conn.executemany(
            "DELETE FROM coverage_items WHERE source_id=? AND stream=? AND kind='SHARD_PENDING_CHECKPOINT'",
            [(source_id, stream) for stream in streams])

    def drop_manifest_coverage(self, source_id):
        """Items imported from a manifest that no longer verifies are not current Evidence.
        Its epoch numbers are withdrawn from cached records too; the records themselves (the
        Evidence text, hashes, refs) are kept, and `rebuild` re-derives epochs from a good manifest."""
        self.conn.execute("DELETE FROM coverage_items WHERE source_id=? AND ref LIKE 'manifest.sqlite#%'", (source_id,))
        self.conn.execute("DELETE FROM source_progress WHERE source_id=?", (source_id,))
        self.conn.execute("UPDATE records SET epoch=NULL WHERE source_id=?", (source_id,))

    def put_progress(self, source_id, through, chain, run_id):
        self.conn.execute("INSERT OR REPLACE INTO source_progress VALUES(?,?,?,?)", (source_id, through, chain, run_id))

    def put_source_status(self, source_id, document, run_id):
        self.conn.execute("INSERT OR REPLACE INTO source_status VALUES(?,?,?)", (source_id, canonical(document), run_id))

    # ── reads for analysis ──
    def records(self):
        out = []
        for row in self.conn.execute("SELECT * FROM records ORDER BY room, stream, seq"):
            item = dict(row)
            item["message"] = json.loads(item["message"])
            item["locator"] = json.loads(item["locator"])
            item["quality"] = json.loads(item["quality"])
            out.append(item)
        return out

    def conflicts(self):
        out = []
        for row in self.conn.execute("SELECT conflict_id,key,existing_sha256,observed_sha256,observed_ref,"
                                     "observed_message,run_id,detected_at FROM record_conflicts ORDER BY conflict_id"):
            item = dict(row)
            item["observed_message"] = json.loads(item["observed_message"])
            out.append(item)
        return out

    def unit_changes(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM unit_changes ORDER BY change_id")
                if _immutable_unit(r["unit"])]

    def units(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM units ORDER BY source_id, unit")]

    def coverage_items(self):
        out = []
        for row in self.conn.execute("SELECT * FROM coverage_items ORDER BY source_id, key"):
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            out.append(item)
        return out

    def source_statuses(self):
        return {r["source_id"]: json.loads(r["document"]) for r in self.conn.execute("SELECT * FROM source_status")}

    # ── findings (append-only revisions) ──
    def upsert_finding(self, finding, run_id):
        """Add a revision only when the evidence-bearing document changed."""
        body = {k: v for k, v in finding.items() if k not in ("generated_run",)}
        sha = digest(body)
        row = self.conn.execute("SELECT latest_revision FROM findings WHERE finding_id=?",
                                (finding["finding_id"],)).fetchone()
        if row is None:
            self.conn.execute("INSERT INTO findings VALUES(?,?,?,?,?,?)",
                              (finding["finding_id"], finding["category"], finding["rule"]["id"],
                               finding["subject"], run_id, 1))
            self._revision(finding["finding_id"], 1, run_id, "NEW", body, sha)
            return "NEW", 1
        latest = row[0]
        prev = self.conn.execute("SELECT document_sha256 FROM finding_revisions WHERE finding_id=? AND revision=?",
                                 (finding["finding_id"], latest)).fetchone()[0]
        if prev == sha:
            return "UNCHANGED", latest
        self._revision(finding["finding_id"], latest + 1, run_id, "UPDATED", body, sha)
        self.conn.execute("UPDATE findings SET latest_revision=? WHERE finding_id=?", (latest + 1, finding["finding_id"]))
        return "UPDATED", latest + 1

    def mark_not_reproduced(self, active_ids, run_id):
        changed = []
        for row in self.conn.execute("SELECT finding_id, latest_revision FROM findings").fetchall():
            if row["finding_id"] in active_ids:
                continue
            latest = self.conn.execute("SELECT change, document FROM finding_revisions WHERE finding_id=? AND revision=?",
                                       (row["finding_id"], row["latest_revision"])).fetchone()
            if latest["change"] == "NO_LONGER_DETECTED":
                continue
            body = json.loads(latest["document"])
            body["lifecycle"] = "NO_LONGER_DETECTED"
            body["lifecycle_note"] = ("not reproduced from current Evidence/rules; earlier revisions are kept. "
                                      "This is not a resolution and not a false-positive decision.")
            rev = row["latest_revision"] + 1
            self._revision(row["finding_id"], rev, run_id, "NO_LONGER_DETECTED", body, digest(body))
            self.conn.execute("UPDATE findings SET latest_revision=? WHERE finding_id=?", (rev, row["finding_id"]))
            changed.append(row["finding_id"])
        return changed

    def _revision(self, finding_id, revision, run_id, change, body, sha):
        self.conn.execute("INSERT INTO finding_revisions VALUES(?,?,?,?,?,?,?)",
                          (finding_id, revision, run_id, change, canonical(body), sha, time.time()))

    def findings(self):
        out = []
        for row in self.conn.execute("SELECT f.finding_id, f.latest_revision, f.first_run, r.change, r.document, r.run_id "
                                     "FROM findings f JOIN finding_revisions r ON r.finding_id=f.finding_id "
                                     "AND r.revision=f.latest_revision ORDER BY f.finding_id"):
            doc = json.loads(row["document"])
            doc.update(revision=row["latest_revision"], last_change=row["change"],
                       first_run=row["first_run"], revision_run=row["run_id"])
            doc["management"] = self.management(row["finding_id"])
            out.append(doc)
        return out

    def finding_history(self, finding_id):
        revisions = [dict(r) for r in self.conn.execute(
            "SELECT revision, run_id, change, document_sha256, created_at FROM finding_revisions WHERE finding_id=? "
            "ORDER BY revision", (finding_id,))]
        return {"revisions": revisions,
                "reviews": [dict(r) for r in self.conn.execute("SELECT * FROM human_reviews WHERE finding_id=? ORDER BY review_id", (finding_id,))],
                "report_events": [dict(r) for r in self.conn.execute("SELECT * FROM report_events WHERE finding_id=? ORDER BY event_id", (finding_id,))],
                "resolutions": [dict(r) for r in self.conn.execute("SELECT * FROM resolutions WHERE finding_id=? ORDER BY resolution_id", (finding_id,))]}

    def management(self, finding_id):
        """Human-owned state, bound to the finding revision it was given for.

        Only HUMAN_CLI rows exist; LLM paths never write these tables. A review or report
        event recorded for an older revision is never shown as the state of the current one.
        """
        current = self.conn.execute("SELECT latest_revision FROM findings WHERE finding_id=?",
                                    (finding_id,)).fetchone()[0]
        review = self.conn.execute("SELECT decision, revision, reviewer, recorded_at FROM human_reviews WHERE finding_id=? "
                                   "ORDER BY review_id DESC LIMIT 1", (finding_id,)).fetchone()
        report = self.conn.execute("SELECT state, channel, finding_revision, candidate_sha256, recorded_at FROM report_events "
                                   "WHERE finding_id=? ORDER BY event_id DESC LIMIT 1", (finding_id,)).fetchone()
        resolution = self.conn.execute("SELECT resolution, finding_revision, source_ref, recorded_at FROM resolutions "
                                       "WHERE finding_id=? ORDER BY resolution_id DESC LIMIT 1", (finding_id,)).fetchone()
        if review is None:
            review_view = {"decision": "UNREVIEWED", "current_revision": current}
        elif review["revision"] != current:
            review_view = {"decision": "STALE_RE_REVIEW_REQUIRED", "current_revision": current,
                           "previous": dict(review),
                           "note": "the finding changed after this review; the old decision does not apply"}
        else:
            review_view = {**dict(review), "current_revision": current}
        if report is None:
            report_view = {"state": "NOT_SUBMITTED", "current_revision": current}
        elif report["finding_revision"] != current:
            report_view = {"state": "PREVIOUS_REVISION_ONLY", "current_revision": current, "previous": dict(report),
                           "note": "report events belong to an earlier revision/candidate; the revised "
                                   "candidate is NOT_SUBMITTED and inherits no approval"}
        else:
            report_view = {**dict(report), "current_revision": current}
        if resolution is None:
            resolution_view = {"resolution": "OPEN", "current_revision": current}
        elif resolution["finding_revision"] != current:
            resolution_view = {"resolution": "REASSESSMENT_REQUIRED", "current_revision": current,
                               "previous": dict(resolution),
                               "note": "the finding changed after this resolution; it is not the state of the "
                                       "current revision"}
        else:
            resolution_view = {**dict(resolution), "current_revision": current}
        return {"human_review": review_view, "report": report_view, "resolution": resolution_view}

    def add_review(self, finding_id, decision, note, reviewer, revision=None):
        _require_identity(reviewer)
        row = self.conn.execute("SELECT latest_revision FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
        if row is None:
            raise AnalyzerError("UNKNOWN_FINDING")
        if revision is not None and revision != row[0]:
            raise AnalyzerError("REVIEW_REVISION_IS_NOT_CURRENT")
        self.conn.execute("INSERT INTO human_reviews(finding_id,revision,decision,note,reviewer,origin,recorded_at) "
                          "VALUES(?,?,?,?,?,'HUMAN_CLI',?)", (finding_id, row[0], decision, note, reviewer, time.time()))

    def record_candidate(self, finding_id, revision, variant, candidate_sha, run_id):
        """Called only inside the transaction that marks the run SUCCEEDED/PARTIAL, after its
        output directory was moved into place. A failed run therefore leaves no candidate row."""
        self.conn.execute("INSERT OR IGNORE INTO report_candidates VALUES(?,?,?,?,?,?)",
                          (finding_id, revision, variant, candidate_sha, run_id, time.time()))

    def missing_candidates(self, categories):
        """Findings whose CURRENT revision has no committed candidate (e.g. the run that created
        the revision failed): they are regenerated by the next successful run."""
        marks = ",".join("?" * len(categories))
        return {r[0] for r in self.conn.execute(
            f"SELECT f.finding_id FROM findings f WHERE f.category IN ({marks}) AND NOT EXISTS ("
            "SELECT 1 FROM report_candidates c JOIN runs r ON r.run_id=c.run_id "
            "WHERE c.finding_id=f.finding_id AND c.revision=f.latest_revision AND c.variant='public' "
            "AND r.status IN ('SUCCEEDED','PARTIAL'))", tuple(categories))}

    def unsurfaced_findings(self):
        """Findings whose CURRENT revision was created by a FAILED run and that no committed run
        started since has reported on. The next successful live run surfaces each of them once
        (a replay run never reports on the live lifecycle, so it does not count)."""
        out = []
        for row in self.conn.execute(
                "SELECT v.finding_id, v.revision, v.document FROM findings f "
                "JOIN finding_revisions v ON v.finding_id=f.finding_id AND v.revision=f.latest_revision "
                "JOIN runs a ON a.run_id=v.run_id WHERE a.status='FAILED' AND NOT EXISTS ("
                "SELECT 1 FROM runs b WHERE b.status IN ('SUCCEEDED','PARTIAL') AND b.mode='current' "
                "AND b.started_at > v.created_at)"):
            doc = json.loads(row["document"])
            out.append({"finding_id": row["finding_id"], "change": "RECOVERED_FROM_FAILED_RUN",
                        "revision": row["revision"], "category": doc.get("category"),
                        "severity": doc.get("severity"), "claim": doc.get("claim")})
        return out

    def _valid_candidate(self, finding_id, revision, candidate_sha):
        """A candidate is submittable only if it belongs to a committed (SUCCEEDED/PARTIAL) run and
        its persisted file still hashes to the recorded digest."""
        row = self.conn.execute(
            "SELECT c.variant, r.output_dir FROM report_candidates c JOIN runs r ON r.run_id=c.run_id "
            "WHERE c.finding_id=? AND c.revision=? AND c.candidate_sha256=? AND r.status IN ('SUCCEEDED','PARTIAL') "
            "AND r.output_dir IS NOT NULL", (finding_id, revision, candidate_sha)).fetchone()
        if row is None:
            return "CANDIDATE_NOT_FROM_A_COMMITTED_RUN"
        path = Path(row["output_dir"]) / "report-candidates" / f"{finding_id}-r{revision}.{row['variant']}.json"
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            recorded = body.pop("candidate_sha256")
        except (OSError, ValueError, KeyError, AttributeError):
            return "CANDIDATE_FILE_UNAVAILABLE"
        if recorded != candidate_sha or digest(body) != candidate_sha:
            return "CANDIDATE_FILE_DIGEST_MISMATCH"
        return None

    def add_report_event(self, finding_id, state, channel, detail, source_ref, actor, candidate_sha, revision=None):
        _require_identity(actor)
        row = self.conn.execute("SELECT latest_revision FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
        if row is None:
            raise AnalyzerError("UNKNOWN_FINDING")
        revision = row[0] if revision is None else revision
        if revision != row[0]:
            raise AnalyzerError("REPORT_REVISION_IS_NOT_CURRENT")
        if state != "NOT_SUBMITTED":
            # Submission facts bind to one exact candidate generated for this revision.
            if not candidate_sha:
                raise AnalyzerError("CANDIDATE_SHA256_REQUIRED")
            problem = self._valid_candidate(finding_id, revision, candidate_sha)
            if problem:
                raise AnalyzerError(problem)
        self.conn.execute("INSERT INTO report_events(finding_id,finding_revision,candidate_sha256,state,channel,detail,"
                          "source_ref,actor,origin,recorded_at) VALUES(?,?,?,?,?,?,?,?,'HUMAN_CLI',?)",
                          (finding_id, revision, candidate_sha, state, channel, detail, source_ref, actor, time.time()))

    def add_resolution(self, finding_id, resolution, source_ref, note, actor, revision=None):
        _require_identity(actor)
        row = self.conn.execute("SELECT latest_revision FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
        if row is None:
            raise AnalyzerError("UNKNOWN_FINDING")
        if revision is not None and revision != row[0]:
            raise AnalyzerError("RESOLUTION_REVISION_IS_NOT_CURRENT")
        self.conn.execute("INSERT INTO resolutions(finding_id,finding_revision,resolution,source_ref,note,actor,origin,recorded_at) "
                          "VALUES(?,?,?,?,?,?,'HUMAN_CLI',?)",
                          (finding_id, row[0], resolution, source_ref, note, actor, time.time()))

    # ── semantic tasks ──
    def put_task(self, task, run_id):
        row = self.conn.execute("SELECT status FROM semantic_tasks WHERE task_id=?", (task["task_id"],)).fetchone()
        if row is not None:
            return row[0]
        self.conn.execute("INSERT INTO semantic_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (task["task_id"], task["kind"], task["purpose_key"], task["bundle_sha256"],
                           task["priority"], task["retain_class"], task.get("expires_ms"), task["status"],
                           run_id, time.time(), task.get("detail")))
        return task["status"]

    def set_task_status(self, task_id, status, detail=None):
        self.conn.execute("UPDATE semantic_tasks SET status=?, detail=?, updated_at=? WHERE task_id=?",
                          (status, detail, time.time(), task_id))

    def tasks(self, status=None):
        sql, args = "SELECT * FROM semantic_tasks", ()
        if status:
            sql, args = sql + " WHERE status=?", (status,)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY priority DESC, task_id", args)]

    def add_result(self, task_id, runtime, started, finished, outcome, output, validation, quarantined):
        self.conn.execute("INSERT INTO semantic_results(task_id,runtime,started_at,finished_at,outcome,output,validation,quarantined) "
                          "VALUES(?,?,?,?,?,?,?,?)", (task_id, runtime, started, finished, outcome, output,
                                                     canonical(validation), int(quarantined)))

    def results(self, task_id=None, include_quarantined=False, runtime=None):
        sql = "SELECT * FROM semantic_results WHERE 1=1"
        args = []
        if task_id:
            sql += " AND task_id=?"
            args.append(task_id)
        if runtime is not None:
            sql += " AND runtime=?"
            args.append(runtime)
        if not include_quarantined:
            sql += " AND quarantined=0"
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY result_id", args)]

    def invocations_since(self, since, runtime):
        """Invocations of ONE runtime: the daily budget of a real runtime is not charged for
        offline fixture evaluations (or any other runtime) sharing the same state DB."""
        return self.conn.execute("SELECT count(*) FROM semantic_results WHERE runtime=? AND started_at>=?",
                                 (runtime, since)).fetchone()[0]

    def rebuild_derived(self):
        """Drop rebuildable caches only. Preserved history is untouched."""
        self.conn.execute("BEGIN IMMEDIATE")
        # record_conflicts / unit_changes stay: the other side may no longer be readable.
        # source_status stays too: it is the source's identity binding (room/kind), which the next run
        # needs to tell whether an ID was repointed and those preserved facts must be withdrawn. It
        # is rewritten by every run, so nothing current is inferred from it.
        for table in ("records", "units", "coverage_items", "source_progress"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.execute("COMMIT")


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
