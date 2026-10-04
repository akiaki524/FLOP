"""Independent-review regressions: synthetic data, SQLite and local processes only."""

import ctypes
from functools import partial
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_capture_spool import page, producer, kill_archive
from test_capture_runtime import ImmediateBudget, WindowAPI
from technocore_full_capture.capture_first import FastCapture
from technocore_full_capture.deadline import DeadlineClient
from technocore_full_capture.pacing import BudgetPolicy, GracefulStop
from technocore_full_capture.scheduling import ReservationBudget
from technocore_full_capture.spool_archive import ArchiveWorker
from technocore_observer.protocol import MAX_BODY, ObserverError, Reply

CTX = multiprocessing.get_context("fork")
POLICY = BudgetPolicy(100, 30, 38, 32)


class AcceleratedClock:
    """Same monotonic clock on every process; 60 budget seconds = 3 real seconds."""
    def __call__(self):
        self.last = time.monotonic() * 20
        return self.last


def competing_capture(directory, room, start, report, rounds):
    clock = AcceleratedClock()
    with ReservationBudget(directory, POLICY, clock=clock, lease_seconds=120) as budget:
        report.send("ready")
        if not start.wait(10):
            raise AssertionError("start deadline")
        grants = []
        for _ in range(rounds):
            with budget.request(room):
                # Read the durable grant for this admission, before any later
                # call changes the clock. HTTP would run outside the transaction.
                grant = budget.conn.execute("SELECT ticket,started FROM grants WHERE started=?",
                                            (clock.last,)).fetchone()
                assert grant is not None
                grants.append(tuple(grant))
        report.send((grants, budget.contention_retries))
    report.close()


def report_and_stall(path, room, since, channel):
    Path(path).write_text(str(os.getpid()))
    time.sleep(60)


def transport_parent(path):
    DeadlineClient("test-room", 30, target=partial(report_and_stall, path)).poll(0)


def parent_death_supervisor(directory, report):
    # Adopt/reap the orphan in an isolated test process, independent of PID 1's
    # zombie handling. This changes no host configuration or signal permissions.
    assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    ready = Path(directory) / "transport-pid"
    parent = CTX.Process(target=transport_parent, args=(str(ready),))
    child_pid = None
    try:
        parent.start()
        expires = time.monotonic() + 8
        while time.monotonic() < expires:
            if ready.exists() and ready.read_text():
                child_pid = int(ready.read_text())
                break
            time.sleep(0.01)
        assert child_pid is not None, "transport never started"
        started = time.monotonic()
        parent.kill()
        parent.join(2)
        assert parent.exitcode == -signal.SIGKILL
        while time.monotonic() - started < 3:
            pid, status = os.waitpid(child_pid, os.WNOHANG)
            if pid:
                child_pid = None
                report.send((os.waitstatus_to_exitcode(status), time.monotonic() - started))
                return
            time.sleep(0.01)
        raise AssertionError("orphan transport survived parent SIGKILL")
    finally:
        if parent.is_alive():
            parent.kill()
            parent.join(2)
        if child_pid is not None:
            os.kill(child_pid, signal.SIGKILL)
            os.waitpid(child_pid, 0)
        report.close()


class BudgetContentionTests(unittest.TestCase):
    def test_16_and_64_processes_fifo_rolling_cap_spacing_and_cooldown(self):
        for count, rounds in ((16, 3), (64, 2)):
            with self.subTest(processes=count), tempfile.TemporaryDirectory() as directory:
                start = CTX.Event()
                channels, children = [], []
                blocker = None
                began = time.monotonic()
                deadline = began + 45
                try:
                    for index in range(count):
                        parent, child = CTX.Pipe()
                        process = CTX.Process(target=competing_capture,
                                              args=(directory, f"room-{index}", start, child, rounds))
                        process.start()
                        child.close()
                        channels.append(parent)
                        children.append(process)
                    for channel in channels:
                        self.assertTrue(channel.poll(max(0, deadline - time.monotonic())))
                        self.assertEqual(channel.recv(), "ready")
                    with ReservationBudget(directory, POLICY, clock=AcceleratedClock(), lease_seconds=120) as budget:
                        budget.defer(30)
                        cooldown = budget.conn.execute("SELECT cooldown FROM policy").fetchone()[0]
                    blocker = sqlite3.connect(Path(directory) / "capture-budget.sqlite", isolation_level=None)
                    blocker.execute("BEGIN IMMEDIATE")
                    start.set()
                    # Exceed the old 250 ms timeout deterministically.
                    time.sleep(0.8)
                    blocker.execute("ROLLBACK")
                    grants, retries = [], 0
                    for channel in channels:
                        self.assertTrue(channel.poll(max(0, deadline - time.monotonic())))
                        admitted, busy = channel.recv()
                        grants.extend(admitted)
                        retries += busy
                    for child in children:
                        child.join(max(0, deadline - time.monotonic()))
                        self.assertEqual(child.exitcode, 0)
                    self.assertLess(time.monotonic() - began, 45)
                    self.assertGreater(retries, 0)
                    self.assertEqual(len(grants), count * rounds)
                    grants.sort(key=lambda item: item[1])
                    tickets, times = zip(*grants)
                    self.assertEqual(list(tickets), sorted(set(tickets)))
                    self.assertGreaterEqual(times[0], cooldown)
                    self.assertTrue(all(b - a >= 60 / POLICY.capture_rpm - 1e-6
                                        for a, b in zip(times, times[1:])))
                    self.assertGreater(times[-1] - times[0], 60)
                    for timestamp in times:
                        self.assertLessEqual(sum(timestamp - 60 < value <= timestamp for value in times), 32)
                finally:
                    if blocker is not None:
                        blocker.close()
                    for child in children:
                        if child.is_alive():
                            child.kill()
                        child.join(2)
                    for channel in channels:
                        channel.close()

    def test_busy_startup_and_cooldown_retry_without_weakening_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            with ReservationBudget(directory, POLICY):
                pass
            ready = threading.Event()
            def hold_writer():
                with sqlite3.connect(Path(directory) / "capture-budget.sqlite") as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    ready.set()
                    time.sleep(0.8)
            for operation in ("startup", "cooldown"):
                with self.subTest(operation=operation):
                    ready.clear()
                    budget = None if operation == "startup" else ReservationBudget(directory, POLICY)
                    holder = threading.Thread(target=hold_writer)
                    holder.start()
                    self.assertTrue(ready.wait(2))
                    try:
                        if budget is None:
                            budget = ReservationBudget(directory, POLICY)
                        else:
                            budget.defer(13)
                            self.assertGreater(budget.conn.execute("SELECT cooldown FROM policy").fetchone()[0],
                                               time.monotonic() + 12)
                        self.assertGreater(budget.contention_retries, 0)
                    finally:
                        holder.join(2)
                        if budget is not None:
                            budget.close()

    def test_busy_wait_cancels_and_nonbusy_errors_propagate(self):
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event()
            with ReservationBudget(directory, POLICY, stop=stop) as budget:
                with sqlite3.connect(Path(directory) / "capture-budget.sqlite") as blocker:
                    blocker.execute("BEGIN IMMEDIATE")
                    timer = threading.Timer(0.4, stop.set)
                    timer.start()
                    began = time.monotonic()
                    try:
                        with self.assertRaisesRegex(GracefulStop, "STOP_REQUESTED"):
                            budget.acquire("cancelled")
                        self.assertLess(time.monotonic() - began, 1)
                        self.assertEqual(budget.requests, 0)
                    finally:
                        timer.cancel()
                        timer.join()
                stop.clear()
                budget.conn.execute("PRAGMA query_only=ON")
                with self.assertRaises(sqlite3.OperationalError) as error:
                    budget.attempt("readonly")
                self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_READONLY)


class CaptureFindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.archive = self.root / "spool", self.root / "archive"
        self.source.mkdir()
        self.archive.mkdir()

    def test_pinned_reader_wal_guard_recovers_same_capture_without_cursor_loss(self):
        with producer(self.source, max_wal_bytes=MAX_BODY) as spool:
            spool.ingest(page(1, 1))
            reader = sqlite3.connect(self.source / "spool.sqlite", isolation_level=None)
            self.addCleanup(reader.close)
            reader.execute("BEGIN")
            reader.execute("SELECT cursor FROM state").fetchone()
            wal = self.source / "spool.sqlite-wal"
            for seq in range(2, 50):
                value = page(seq, seq)
                value["messages"][0]["text"] = "x" * 200000
                spool.ingest(value)
                if wal.stat().st_size > MAX_BODY:
                    break
            self.assertGreater(wal.stat().st_size, MAX_BODY)
            before = spool.state()
            with self.assertRaisesRegex(ObserverError, "SPOOL_WAL_LIMIT"):
                spool.ingest(page(seq + 1, seq + 1))
            self.assertEqual(spool.state(), before)
            api, budget = WindowAPI(seq + 1), ImmediateBudget()
            worker = FastCapture(spool, api, budget)
            for _ in range(2):
                began = time.monotonic()
                self.assertGreaterEqual(worker.step(), 1)
                self.assertLess(time.monotonic() - began, 1)
                self.assertEqual(worker.last["error"], "SPOOL_WAL_LIMIT")
                self.assertEqual(spool.state(), before)
            self.assertEqual((api.calls, budget.requests), ([], 0))
            reader.execute("ROLLBACK")
            # No restart, deletion, external checkpoint or manual size change.
            worker.step()
            self.assertEqual(worker.last["capture_status"], "RUNNING")
            self.assertEqual(api.calls, [seq])
            self.assertEqual(spool.state()["cursor"], seq + 1)
            self.assertEqual(spool.state()["messages"], seq + 1)
            self.assertLessEqual(wal.stat().st_size, MAX_BODY)
            with ArchiveWorker(spool, self.archive, min_free_bytes=0) as archive:
                while archive.step()["processed"]:
                    pass
                self.assertEqual(archive.verify()["messages"], seq + 1)

    def test_wal_pressure_after_get_and_during_failure_evidence_does_not_stop(self):
        with producer(self.source) as spool:
            worker = FastCapture(spool, WindowAPI(2), ImmediateBudget())
            for reply in (None, Reply(503, "text/plain", b"offline")):
                worker.client.failure = reply
                with patch.object(spool, "capacity", side_effect=[None, ObserverError("SPOOL_WAL_LIMIT")]):
                    self.assertGreaterEqual(worker.step(), 1)
                    self.assertEqual(spool.state()["cursor"], 0)
                    self.assertEqual(worker.last["error"], "SPOOL_WAL_LIMIT")
            worker.client.failure = None
            worker.step()
            self.assertEqual(spool.state()["cursor"], 2)

    def test_generation_metadata_records_races_preserve_observations_and_resync(self):
        for scenario in ("old_records_new_generation", "new_records_old_generation", "empty_new_generation"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, destination = root / "spool", root / "archive"
                source.mkdir()
                destination.mkdir()
                with producer(source) as spool:
                    api = WindowAPI(3)
                    worker = FastCapture(spool, api, ImmediateBudget())
                    worker.step()
                    if scenario == "new_records_old_generation":
                        stale = page(4, 5, generation=1)
                        api.failure = Reply(200, "application/json", json.dumps(stale).encode())
                        worker.step()
                        # Indistinguishable from normal continuation until the
                        # metadata changes. Preserve the actual observation,
                        # never retrospectively assert atomic attribution.
                        self.assertEqual(spool.state()["cursor"], 5)
                        self.assertEqual(worker.last["generation_binding"], "OBSERVED_NOT_ATOMIC")
                    changed = (page(1, 3, generation=2) if scenario == "old_records_new_generation"
                               else page(generation=2, since=spool.state()["cursor"]))
                    prior = spool.state()
                    api.failure = Reply(200, "application/json", json.dumps(changed).encode())
                    self.assertEqual(worker.step(), 0)
                    self.assertEqual(spool.state()["cursor"], 0)
                    self.assertEqual(spool.state()["messages"], prior["messages"])
                    boundary = spool.conn.execute("SELECT payload FROM entries WHERE kind='BOUNDARY_OBSERVATION'").fetchone()
                    self.assertEqual(json.loads(boundary[0]), changed)
                    epoch = spool.conn.execute("SELECT binding,previous_tail FROM epochs WHERE epoch=2").fetchone()
                    self.assertEqual(tuple(epoch), ("OBSERVED_NOT_ATOMIC", "UNKNOWN"))
                    with ArchiveWorker(spool, destination, min_free_bytes=0) as archive:
                        archive.step()
                        self.assertEqual(archive.verify()["coverage_status"], "UNCONFIRMED")
                        # Recreated Room preserves its high-water mark. It can
                        # expose old retained records too; attribution stays an
                        # observation, and local epochs must remain distinct.
                        stable = page(1, 7, generation=2)
                        api.failure = Reply(200, "application/json", json.dumps(stable).encode())
                        worker.step()
                        self.assertEqual(api.calls[-1], 0)
                        self.assertEqual(spool.state()["cursor"], 7)
                        archive.step()
                        result = archive.verify()
                        self.assertEqual(result["coverage_status"], "UNCONFIRMED")
                        self.assertFalse(result["all_room_history_complete"])
                        self.assertEqual(result["generation_binding"], "OBSERVED_NOT_ATOMIC")
                        self.assertEqual([tuple(row) for row in archive.conn.execute(
                            "SELECT epoch,generation FROM segments ORDER BY first_entry")], [(1, 1), (2, 2)])
                        old = spool.conn.execute("SELECT count(*) FROM entries WHERE kind='MESSAGE' AND epoch=1").fetchone()[0]
                        self.assertEqual(old, prior["messages"])

    def test_in_page_hole_is_durable_protocol_failure_with_bounded_backoff(self):
        with producer(self.source) as spool:
            api, budget = WindowAPI(2), ImmediateBudget()
            worker = FastCapture(spool, api, budget)
            worker.step()
            malformed = page(3, 5)
            malformed["messages"].pop(1)
            malformed["count"] = 2
            body = json.dumps(malformed).encode()
            api.failure = Reply(200, "application/json", body)
            before = spool.state()
            delays = [worker.step() for _ in range(10)]
            self.assertTrue(all(1 <= delay <= 60 for delay in delays))
            self.assertEqual(delays[-1], 60)
            for key in ("cursor", "epoch", "messages", "gaps", "high_entry"):
                self.assertEqual(spool.state()[key], before[key])
            failures = spool.conn.execute("SELECT code,request_since,body_sha256,body_prefix FROM failures").fetchall()
            self.assertEqual(len(failures), 10)
            for failure in failures:
                self.assertEqual(tuple(failure), ("INTERNAL_SEQ_HOLE", 2, hashlib.sha256(body).hexdigest(), body))
        with producer(self.source) as spool:
            self.assertEqual(spool.state()["last_failure"], "INTERNAL_SEQ_HOLE")
            api.failure = None
            api.grow(3)
            FastCapture(spool, api, budget).step()
            self.assertEqual(spool.state()["cursor"], 5)

    def test_second_and_third_segment_publish_crash_recovery(self):
        for segment in (2, 3):
            for point in ("publication_planned", "shard_fsynced", "shard_published", "before_checkpoint",
                          "archive_published", "before_manifest_commit", "manifest_committed",
                          "before_consumer_ack", "after_consumer_ack"):
                with self.subTest(segment=segment, point=point), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source, destination = root / "spool", root / "archive"
                    source.mkdir()
                    destination.mkdir()
                    with producer(source) as spool:
                        with ArchiveWorker(spool, destination, min_free_bytes=0) as archive:
                            for previous in range(1, segment):
                                first = 1 + (previous - 1) * 20
                                spool.ingest(page(first, first + 9))
                                archive.step()
                            old_segments = [dict(row) for row in archive.conn.execute("SELECT * FROM segments ORDER BY name")]
                            first = 1 + (segment - 1) * 20
                            # Receipt the GAP separately, so manifest crash
                            # hooks below belong to the MESSAGE publication.
                            spool.ingest(page(first, first + 9))
                            archive.step(limit=1)
                    child = CTX.Process(target=kill_archive, args=(source, destination, point))
                    child.start()
                    child.join(8)
                    if child.is_alive():
                        child.kill()
                        child.join(2)
                        self.fail("archive crash hook not reached")
                    self.assertEqual(child.exitcode, -signal.SIGKILL)
                    with producer(source) as spool:
                        spool.ingest(page(first + 10, first + 14))
                        with ArchiveWorker(spool, destination, min_free_bytes=0) as archive:
                            for _ in range(30):
                                if not archive.step(limit=3)["processed"]:
                                    break
                            else:
                                self.fail("archive recovery did not finish")
                            result = archive.verify()
                            self.assertEqual(result["messages"], segment * 10 + 5)
                            self.assertEqual(result["gaps"], segment - 1)
                            segments = [dict(row) for row in archive.conn.execute("SELECT * FROM segments ORDER BY name")]
                            self.assertEqual(len(segments), segment)
                            self.assertEqual(segments[:-1], old_segments)
                            self.assertEqual(spool.consumer("archive")["ack_entry"], spool.state()["high_entry"])

    def test_parent_sigkill_kills_and_reaps_deadline_transport_child(self):
        parent, child = CTX.Pipe()
        supervisor = CTX.Process(target=parent_death_supervisor, args=(str(self.root), child))
        try:
            supervisor.start()
            child.close()
            self.assertTrue(parent.poll(12))
            exitcode, elapsed = parent.recv()
            self.assertEqual(exitcode, -signal.SIGKILL)
            self.assertLess(elapsed, 3)  # Much less than the 30 second child timer.
            supervisor.join(3)
            self.assertEqual(supervisor.exitcode, 0)
        finally:
            if supervisor.is_alive():
                supervisor.kill()
            supervisor.join(2)
            parent.close()


if __name__ == "__main__":
    unittest.main()
