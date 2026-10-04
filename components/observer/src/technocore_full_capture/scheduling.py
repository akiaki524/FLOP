"""Durable FIFO request-start reservations; no lock spans HTTP or sleeps.

One outstanding ticket per room. A full-page producer rejoins at the back.
Expired tickets bound dead-producer head-of-line blocking. Starts, including
failed requests, consume a rolling 60 second budget and spacing; no refunds.
All processes sharing an egress allocation MUST share this directory/policy.
"""

from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .archive import regular_open, sync_directory
from .pacing import GracefulStop, select_budget_mode
from .spool import canonical, checked_directory, require
from technocore_observer.protocol import validate_room


class ReservationBudget:
    def __init__(self, directory, policy, *, stop=None, clock=time.monotonic,
                 boot=None, lease_seconds=2.0, production_binding=None):
        self.path, self.policy = checked_directory(directory), policy
        self.stop = stop if stop is not None else threading.Event()
        self.clock, self.owner = clock, uuid.uuid4().hex
        self.boot = boot or Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.lease = lease_seconds
        self.limit = math.floor(policy.capture_rpm)
        require(self.limit >= 1 and math.isfinite(lease_seconds) and lease_seconds >= 0.2,
                "CAPTURE_INVALID_RESERVATION_POLICY")
        self.spacing = 60.0 / self.limit
        self.requests = 0
        self.wait_seconds = 0.0
        self.contention_retries = 0
        self.conn = None
        select_budget_mode(self.path, "reservation", production_binding)
        with regular_open(self.path / "admission.lock", os.O_CREAT | os.O_WRONLY) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            database = self.path / "capture-budget.sqlite"
            if not database.exists():
                with regular_open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY):
                    pass
            with regular_open(database, os.O_RDONLY):
                pass
            self.conn = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True,
                                        isolation_level=None, timeout=0.25)
            self.conn.row_factory = sqlite3.Row
            try:
                require(self._execute_wait("PRAGMA journal_mode=WAL").fetchone()[0] == "wal",
                        "CAPTURE_BUDGET_WAL_REQUIRED")
                self.conn.execute("PRAGMA synchronous=FULL")
                self.conn.execute("PRAGMA trusted_schema=OFF")
                with self.transaction():
                    self.conn.execute("""CREATE TABLE IF NOT EXISTS policy (singleton INTEGER PRIMARY KEY,
                        document TEXT NOT NULL, boot TEXT NOT NULL, next_start REAL NOT NULL,
                        cooldown REAL NOT NULL, last_clock REAL NOT NULL, next_ticket INTEGER NOT NULL)""")
                    self.conn.execute("""CREATE TABLE IF NOT EXISTS tickets (room TEXT PRIMARY KEY,
                        ticket INTEGER UNIQUE NOT NULL, owner TEXT NOT NULL, expires REAL NOT NULL)""")
                    self.conn.execute("CREATE TABLE IF NOT EXISTS grants (ticket INTEGER PRIMARY KEY, started REAL NOT NULL)")
                    self.conn.execute("CREATE INDEX IF NOT EXISTS grants_time ON grants(started)")
                    now = self.clock()
                    document = canonical({"version": 1, "allocation": asdict(policy), "lease_seconds": self.lease})
                    self.conn.execute("INSERT OR IGNORE INTO policy VALUES(1,?,?,?,?,?,1)",
                                      (document, self.boot, now + self.spacing, 0, now))
                    s = self.conn.execute("SELECT * FROM policy WHERE singleton=1").fetchone()
                    require(s["document"] == document, "CAPTURE_BUDGET_POLICY_MISMATCH")
                    if s["boot"] != self.boot:
                        pause = max(60.0, s["cooldown"] - s["last_clock"])
                        self.conn.execute("UPDATE policy SET boot=?,next_start=?,cooldown=?,last_clock=? WHERE singleton=1",
                                          (self.boot, now + pause, now + pause, now))
                        self.conn.execute("DELETE FROM tickets")
                        self.conn.execute("DELETE FROM grants")
                sync_directory(self.path)
            except BaseException:
                self.close()
                raise

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def transaction(self):
        self._execute_wait("BEGIN IMMEDIATE")
        try:
            yield
            self._execute_wait("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def _execute_wait(self, statement):
        # Each SQLite busy wait is bounded by connect(timeout=0.25). Treat only
        # BUSY/LOCKED (including extended codes) as scheduler contention. WAL
        # keeps readers from blocking writes after BEGIN IMMEDIATE succeeds.
        # Retry the lock acquisition/commit, never replay a granted transaction.
        while True:
            if self.stop.is_set():
                raise GracefulStop("CAPTURE_STOP_REQUESTED")
            started = time.monotonic()
            try:
                return self.conn.execute(statement)
            except sqlite3.OperationalError as exc:
                if getattr(exc, "sqlite_errorcode", 0) & 0xff not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                self.contention_retries += 1
                self.wait_seconds += time.monotonic() - started
                if self.stop.wait(0.05):
                    raise GracefulStop("CAPTURE_STOP_REQUESTED") from exc
                self.wait_seconds += 0.05

    def attempt(self, room):
        """One admission decision; database contention waits cooperatively."""
        validate_room(room)
        with self.transaction():
            now = self.clock()
            s = self.conn.execute("SELECT * FROM policy WHERE singleton=1").fetchone()
            require(s["boot"] == self.boot and now >= s["last_clock"], "CAPTURE_BUDGET_CLOCK")
            self.conn.execute("DELETE FROM tickets WHERE expires<=?", (now,))
            self.conn.execute("DELETE FROM grants WHERE started<=?", (now - 60.0,))
            ticket = self.conn.execute("SELECT * FROM tickets WHERE room=?", (room,)).fetchone()
            if ticket is None:
                require(self.conn.execute("SELECT count(*) FROM tickets").fetchone()[0] < 64,
                        "CAPTURE_TOO_MANY_WAITERS")
                self.conn.execute("INSERT INTO tickets VALUES(?,?,?,?)", (room, s["next_ticket"], self.owner, now + self.lease))
                self.conn.execute("UPDATE policy SET next_ticket=next_ticket+1 WHERE singleton=1")
                ticket_number = s["next_ticket"]
            else:
                require(ticket["owner"] == self.owner, "CAPTURE_ROOM_ALREADY_WAITING")
                ticket_number = ticket["ticket"]
                self.conn.execute("UPDATE tickets SET expires=? WHERE room=?", (now + self.lease, room))
            first = self.conn.execute("SELECT ticket FROM tickets ORDER BY ticket LIMIT 1").fetchone()[0]
            count, oldest = self.conn.execute("SELECT count(*),min(started) FROM grants").fetchone()
            earliest = max(s["next_start"], s["cooldown"], oldest + 60.0 if count >= self.limit else now)
            granted = ticket_number == first and now >= earliest
            if granted:
                self.conn.execute("INSERT INTO grants VALUES(?,?)", (ticket_number, now))
                self.conn.execute("DELETE FROM tickets WHERE room=?", (room,))
                self.conn.execute("UPDATE policy SET next_start=? WHERE singleton=1", (now + self.spacing,))
            self.conn.execute("UPDATE policy SET last_clock=? WHERE singleton=1", (now,))
        if granted:
            self.requests += 1
        return granted

    def acquire(self, room):
        while not self.stop.is_set():
            if self.attempt(room):
                return
            wait = min(0.05, self.lease / 4)
            if self.stop.wait(wait):
                break
            self.wait_seconds += wait
        # Pending tickets expire by lease, including cancellation during a busy
        # wait. Do not require another contended transaction just to stop.
        # Granted tickets were already removed atomically; grants stay charged.
        raise GracefulStop("CAPTURE_STOP_REQUESTED")

    def defer(self, seconds):
        require(math.isfinite(seconds) and 0 <= seconds <= 600, "CAPTURE_INVALID_COOLDOWN")
        with self.transaction():
            now = self.clock()
            self.conn.execute("UPDATE policy SET cooldown=max(cooldown,?),last_clock=? WHERE singleton=1",
                              (now + seconds, now))

    @contextmanager
    def request(self, room):
        self.acquire(room)
        yield  # Deliberately no SQLite transaction/flock spans the HTTP call.
