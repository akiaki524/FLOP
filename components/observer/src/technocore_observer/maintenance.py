"""Explicit offline retention under StateLock; never called by the poll worker.

Only evidence_metadata, evidence and events can be deleted. A verified SQLite
backup matching every current row and an exact human review token are required.
Backups remain in the private state directory, never exported or overwritten.
"""

import contextlib
import hashlib
import math
import os
import re
import shutil
import time

from . import storage
from .protocol import ObserverError, json_dump

TABLES = tuple(statement.split()[2] for statement in storage.SCHEMA)
EVIDENCE_SELECTION = """observed_at < ? AND anomaly_type <> 'INIT_ANCHOR'
    AND NOT EXISTS (SELECT 1 FROM events
        WHERE events.evidence_id=evidence.evidence_id AND occurred_at >= ?)"""


def _backup_path(store, name, existing=False):
    # No arbitrary path, source alias, sidecar, symlink or overwrite option.
    if not isinstance(name, str) or not re.fullmatch(r"review-[a-z0-9-]{1,64}\.sqlite", name):
        raise ObserverError("INVALID_BACKUP_NAME")
    path = store.directory / name
    if existing:
        if not path.exists() and not path.is_symlink():
            raise ObserverError("BACKUP_MISSING")
        storage._private(path)
    elif path.exists() or path.is_symlink():
        raise ObserverError("BACKUP_ALREADY_EXISTS")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = store.directory / (name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise ObserverError("BACKUP_SIDECAR_PRESENT")
    return path


def _integrity(conn, room):
    storage._verify(conn, room)
    if [row[0] for row in conn.execute("PRAGMA integrity_check")] != ["ok"]:
        raise ObserverError("BACKUP_INTEGRITY_FAILED")


def _fingerprint(conn):
    """Stream typed values; no raw row/body data is returned or logged."""
    digest = hashlib.sha256()
    for table in TABLES:
        digest.update(table.encode("ascii"))
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            digest.update(b"row:")
            for value in row:
                kind = type(value).__name__.encode("ascii")
                data = value if isinstance(value, bytes) else str(value).encode("utf-8")
                digest.update(kind + b":" + str(len(data)).encode("ascii") + b":")
                digest.update(data)
    return digest.hexdigest()


def backup(store, name):
    """Explicit backup API command. Caller holds StateLock; worker is stopped."""
    path = _backup_path(store, name)
    required = store.conn.execute("PRAGMA page_count").fetchone()[0] * store.conn.execute("PRAGMA page_size").fetchone()[0]
    if shutil.disk_usage(store.directory).free < required + storage.MIN_DISK_FREE:
        raise ObserverError("BACKUP_DISK_HEADROOM_REQUIRED")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    # Failure deliberately leaves the partial file; no automatic cleanup.
    with contextlib.closing(storage._connect(path, "rw")) as dest:
        dest.execute("PRAGMA trusted_schema=OFF")
        dest.execute("PRAGMA synchronous=FULL")
        store.conn.backup(dest)
        # A standalone backup must not need a companion WAL.
        dest.execute("PRAGMA journal_mode=DELETE")
        _integrity(dest, store.room)
        fingerprint = _fingerprint(dest)
        if fingerprint != _fingerprint(store.conn):
            raise ObserverError("BACKUP_SNAPSHOT_MISMATCH")
    for target, flags in ((path, os.O_RDONLY | os.O_NOFOLLOW),
                          (store.directory, os.O_RDONLY | os.O_DIRECTORY)):
        fd = os.open(target, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return {"action": "SQLITE_BACKUP", "backup_name": name,
            "snapshot_sha256": fingerprint, "integrity": "ok"}


def _summary(conn, table, key, timestamp, predicate, params):
    row = conn.execute(f"SELECT count(*),min({key}),max({key}),min({timestamp}),max({timestamp}) "
                       f"FROM {table} WHERE {predicate}", params).fetchone()
    return dict(zip(("count", "min_id", "max_id", "min_time", "max_time"), row))


def retain(store, name, before, approval=None, checkpoint=storage.noop_checkpoint):
    """Plan first; apply only matching approval, backup and unchanged snapshot.

    Bypasses ordinary ingestion capacity only for these fixed DELETE statements.
    Freelist pages can be reused; this command does not VACUUM, change budgets,
    clear terminal state, resync, delete backups, or touch protected tables.
    """
    if type(before) not in (int, float) or not math.isfinite(before) or not 0 <= before <= time.time():
        raise ObserverError("INVALID_RETENTION_CUTOFF")
    path = _backup_path(store, name, existing=True)
    store.conn.execute("BEGIN" if approval is None else "BEGIN IMMEDIATE")
    try:
        _integrity(store.conn, store.room)
        current = _fingerprint(store.conn)
        with contextlib.closing(storage._connect(path, "ro")) as saved:
            saved.execute("PRAGMA trusted_schema=OFF")
            saved.execute("PRAGMA query_only=ON")
            _integrity(saved, store.room)
            if _fingerprint(saved) != current:
                raise ObserverError("BACKUP_SNAPSHOT_MISMATCH")
        params = (before, before)
        evidence = _summary(store.conn, "evidence", "evidence_id", "observed_at", EVIDENCE_SELECTION, params)
        events = _summary(store.conn, "events", "event_id", "occurred_at", "occurred_at < ?", (before,))
        metadata_count = store.conn.execute(
            "SELECT count(*) FROM evidence_metadata WHERE evidence_id IN "
            "(SELECT evidence_id FROM evidence WHERE " + EVIDENCE_SELECTION + ")", params).fetchone()[0]
        decision = {"action": "MANUAL_RETAIN_EVIDENCE_EVENTS", "backup_name": name,
                    "snapshot_sha256": current, "before_unix": float(before),
                    "boundary": "STRICTLY_OLDER_THAN_CUTOFF",
                    "delete": {"events": events, "evidence": evidence,
                               "evidence_metadata": {"count": metadata_count}},
                    "preserve": {table: store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                                 for table in ("state", "messages", "gaps", "epoch_history")},
                    "retained_evidence": "INIT_ANCHOR_AND_EVIDENCE_REFERENCED_BY_RETAINED_EVENTS"}
        token = hashlib.sha256(json_dump(decision).encode()).hexdigest()
        if approval is not None:
            if not isinstance(approval, str) or approval != token:
                raise ObserverError("RETENTION_APPROVAL_MISMATCH")
            if shutil.disk_usage(store.directory).free < storage.MIN_DISK_FREE:
                raise ObserverError("RETENTION_DISK_HEADROOM_REQUIRED")
            # FK-safe ordering; retained events keep their referenced evidence.
            store.conn.execute("DELETE FROM events WHERE occurred_at < ?", (before,))
            store.conn.execute("DELETE FROM evidence_metadata WHERE evidence_id IN "
                               "(SELECT evidence_id FROM evidence WHERE " + EVIDENCE_SELECTION + ")", params)
            store.conn.execute("DELETE FROM evidence WHERE " + EVIDENCE_SELECTION, params)
            _integrity(store.conn, store.room)
            checkpoint("retention_before_commit")
        store.conn.execute("COMMIT")
        return {"decision": decision, "approval_token": token, "applied": approval is not None,
                "integrity": "ok"}
    except BaseException:
        if store.conn.in_transaction:
            store.conn.execute("ROLLBACK")
        raise
