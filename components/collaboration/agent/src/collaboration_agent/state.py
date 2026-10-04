"""Local SQLite ledger, process exclusion, persistent policy and hash-linked audit."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from .model import (Invalid, check, digest, encode, fingerprint, regular,
                    validate_outcome, validate_task)
from .solvers import REGISTRY

SCHEMA = """
CREATE TABLE policy (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
CREATE TABLE identities (task_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
CREATE TABLE runs (
 fingerprint TEXT PRIMARY KEY, task TEXT NOT NULL, task_digest TEXT NOT NULL,
 solver TEXT, started INTEGER NOT NULL, charged INTEGER NOT NULL,
 result TEXT, result_digest TEXT
);
CREATE INDEX rate_window ON runs(charged, started);
CREATE TABLE events (
 seq INTEGER PRIMARY KEY, at INTEGER NOT NULL, kind TEXT NOT NULL,
 body TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL
);
PRAGMA user_version=1;
"""
MAX_RUNS = 10_000
MAX_EVENTS = 50_000


class Busy(Invalid):
    pass


def default_policy(hour=60, day=500):
    for value in (hour, day):
        check(type(value) is int and 0 <= value <= MAX_RUNS, "invalid_rate_limit")
    return {"per_hour": hour, "per_day": day, "concurrency": 1,
            "families": {name: {"enabled": True, "suspended": False, "reason": "initial"}
                         for name in REGISTRY}}


class Store:
    def __init__(self, root, *, create=False, hour=60, day=500, clock=None):
        self.root = Path(root).absolute()
        self.db = None
        self.lock_fd = None
        self.clock = clock or (lambda: int(time.time()))
        check(self.root == self.root.resolve(), "symlink_state_root")
        if create:
            default_policy(hour, day)
            self.root.mkdir(parents=True, exist_ok=False)
        check(self.root.is_dir(), "state_not_initialized")
        try:
            lock = self.root / "agent.lock"
            if not create:
                regular(lock)
            self.lock_fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW |
                                   (os.O_CREAT | os.O_EXCL if create else 0), 0o600)
            info = os.fstat(self.lock_fd)
            check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "invalid_lock")
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Busy("concurrency_limit") from exc
            db_path = self.root / "agent.sqlite"
            if not create:
                regular(db_path)
            for name in ("agent.sqlite-journal", "agent.sqlite-wal", "agent.sqlite-shm"):
                sidecar = self.root / name
                if os.path.lexists(sidecar):
                    regular(sidecar)
            self.db = sqlite3.connect(db_path, timeout=0, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA journal_mode=DELETE")
            if create:
                self.db.executescript(SCHEMA)
                with self.transaction():
                    policy = default_policy(hour, day)
                    self.db.execute("INSERT INTO policy VALUES (1, ?)", (encode(policy).decode(),))
                    self.event("policy", policy)
            self.verify()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def now(self):
        value = self.clock()
        check(type(value) is int and value >= 0, "invalid_clock")
        return value

    def event(self, kind, body):
        tail = self.db.execute("SELECT seq, at, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq = tail["seq"] + 1 if tail else 1
        check(seq <= MAX_EVENTS, "audit_capacity_reached")
        previous = tail["hash"] if tail else "0" * 64
        at = max(self.now(), tail["at"] if tail else 0)
        value = {"seq": seq, "at": at, "kind": kind, "body": body, "previous": previous}
        self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                        (seq, at, kind, encode(body).decode(), previous, digest(value)))

    def policy(self):
        return json.loads(self.db.execute("SELECT body FROM policy WHERE id=1").fetchone()[0])

    def change_family(self, family, action, reason):
        check(family in REGISTRY, "unknown_family")
        check(action in ("enable", "disable", "suspend", "resume"), "invalid_policy_action")
        check(type(reason) is str and 1 <= len(reason) <= 300, "policy_reason_required")
        policy = self.policy()
        check(family in policy["families"], "family_not_configured")
        member = policy["families"][family]
        if action in ("enable", "disable"):
            member["enabled"] = action == "enable"
        else:
            member["suspended"] = action == "suspend"
        member["reason"] = reason
        with self.transaction():
            self.db.execute("UPDATE policy SET body=? WHERE id=1", (encode(policy).decode(),))
            self.event("policy", policy)
        return policy

    def verify(self):
        check(self.db.execute("PRAGMA user_version").fetchone()[0] == 1, "state_version")
        check(self.db.execute("PRAGMA quick_check").fetchone()[0] == "ok", "state_corrupt")
        previous = "0" * 64
        identities, starts, finishes = {}, {}, {}
        policy = None
        last_at = 0
        for expected_seq, row in enumerate(self.db.execute("SELECT * FROM events ORDER BY seq"), 1):
            body = json.loads(row["body"])
            value = {"seq": row["seq"], "at": row["at"], "kind": row["kind"],
                     "body": body, "previous": row["previous"]}
            check(row["seq"] == expected_seq and row["previous"] == previous
                  and digest(value) == row["hash"] and row["at"] >= last_at, "audit_corrupt")
            previous, last_at = row["hash"], row["at"]
            if row["kind"] == "policy":
                policy = body
            elif row["kind"] == "identity":
                check(body["task_id"] not in identities, "identity_rebound")
                identities[body["task_id"]] = body["fingerprint"]
            elif row["kind"] == "started":
                check(body["fingerprint"] not in starts, "duplicate_execution")
                starts[body["fingerprint"]] = body
            elif row["kind"] == "finished":
                check(body["fingerprint"] in starts and body["fingerprint"] not in finishes,
                      "invalid_finish")
                finishes[body["fingerprint"]] = body["result_digest"]
        check(policy is not None and policy == self.policy(), "policy_audit_mismatch")
        actual_ids = dict(self.db.execute("SELECT task_id, fingerprint FROM identities"))
        check(actual_ids == identities, "identity_audit_mismatch")
        count = 0
        for row in self.db.execute("SELECT * FROM runs"):
            count += 1
            task = validate_task(json.loads(row["task"]))
            fp = row["fingerprint"]
            check(fingerprint(task) == fp and digest(task) == row["task_digest"], "task_corrupt")
            check(identities.get(task["task_id"]) == fp, "unbound_task")
            admission = {k: row[k] for k in ("fingerprint", "task_digest", "solver", "started", "charged")}
            check(starts.get(fp) == admission, "admission_corrupt")
            if row["result"] is None:
                check(fp not in finishes and row["result_digest"] is None, "missing_result")
            else:
                result = json.loads(row["result"])
                validate_outcome(result["outcome"], task)
                check(result["task_digest"] == digest(task) and result["fingerprint"] == fp
                      and result["task_id"] == task["task_id"] and result["solver"] == row["solver"]
                      and result["dry_run"] is False, "result_binding_mismatch")
                check(digest(result) == row["result_digest"] == finishes.get(fp), "result_corrupt")
        check(count == len(starts), "missing_run")
        return {"runs": count, "events": self.db.execute("SELECT count(*) FROM events").fetchone()[0],
                "audit_head": previous, "policy": policy}
