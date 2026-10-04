"""Offline 12-Room isolation and important-response evidence."""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import io
import os
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from technocore_full_capture.multi_room import (
    ROOMS, IMPORTANT, ReservedArchiveWorker, ReservedReservationBudget, RoomBudget, SignalStop,
    initialize, load, main, plan, run, status, storage_floor, validate,
)
from technocore_full_capture.archive import MAX_LINE
from technocore_full_capture.capture_first import FastCapture
from technocore_full_capture.pacing import GracefulStop
from technocore_full_capture.pacing import BudgetPolicy
from technocore_full_capture.scheduling import ReservationBudget
from technocore_full_capture.spool import Spool, canonical
from technocore_observer.protocol import MAX_BODY, ObserverError, Reply
from technocore_observer.http import SafeClient


class FakeClient:
    def __init__(self, room, seq=1):
        self.room, self.seq = room, seq

    def poll(self, since):
        messages = [{"seq": self.seq, "from": "test", "text": "raw evidence"}] if since < self.seq else []
        body = json.dumps({"room": self.room, "generation": 1, "count": len(messages),
                           "messages": messages, "first_seq": self.seq if messages else None,
                           "last_seq": self.seq if messages else since}, indent=2).encode()
        return Reply(200, "application/json", body)


class MultiRoomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        example = Path(__file__).resolve().parents[1] / "deploy" / "multi-room-example.json"
        self.config = load(example)
        # Offline-only allocation; the example deliberately has no Production inventory.
        self.config["budget"].update(external_rpm=180, capture_rpm=360, external_waiters=1)
        self.config["root"] = self.temp.name + "/new-runtime"
        self.config["global_min_free_bytes"] = 0
        self.root = Path(self.config["root"])
        self.root.mkdir()
        for room in self.config["rooms"]:
            if room["capture_owner"] == "local":
                room["max_db_bytes"] = 64 * 1024 * 1024
        validate(self.config)

    def test_selected_rooms_and_policy(self):
        self.assertEqual({r["room"] for r in self.config["rooms"]}, set(ROOMS))
        self.assertEqual({r["room"] for r in self.config["rooms"] if r["evidence"] == "important"}, IMPORTANT)
        bad = {**self.config, "rooms": self.config["rooms"][:-1]}
        with self.assertRaises(ObserverError):
            validate(bad)
        changed = self.config["rooms"][0]["max_rpm"]
        self.config["rooms"][0]["max_rpm"] = 500
        with self.assertRaises(ObserverError):
            validate(self.config)
        self.config["rooms"][0]["max_rpm"] = changed

    def test_trial_caps_exclude_external_lobby_and_show_room_ratios(self):
        example = Path(__file__).resolve().parents[1] / "deploy" / "multi-room-example.json"
        configured = load(example)
        caps = {r["room"]: r["max_db_bytes"] for r in configured["rooms"]}
        self.assertIsNone(caps["lobby"])
        self.assertEqual(caps["close1"], 4 * 1024**3)
        self.assertEqual(caps["tclk-offers"], caps["kibble"])
        self.assertEqual(caps["tclk-offers"], 2 * 1024**3)
        for name in ("events", "sub_economy", "zk-desk-a", "d-close1-price",
                     "d-close1-flow", "d-close1-positions", "d-close1-pnl", "d-close1-state"):
            self.assertEqual(caps[name], 256 * 1024**2)
        self.assertEqual(configured["global_min_free_bytes"], 10 * 1024**3)
        self.assertFalse(any(service.startswith("lobby:") for service in plan(configured)["services"]))

    def test_global_reserve_rejects_low_free_space_before_room_write(self):
        initialize(self.config)
        self.config["global_min_free_bytes"] = 10 * 1024**3
        floor = storage_floor(self.config)
        self.assertEqual(floor, 11 * 1024**3)
        real_statvfs = os.statvfs

        def low_free(path):
            info = real_statvfs(path)
            values = list(info)
            values[4] = (floor - 1) // info.f_frsize  # f_bavail
            return os.statvfs_result(values)

        before = status(self.config | {"global_min_free_bytes": 0}, "events")["state"]
        with patch("technocore_full_capture.multi_room.os.statvfs", side_effect=low_free):
            with self.assertRaisesRegex(ObserverError, "MULTI_GLOBAL_STORAGE_LOW"):
                run(self.config, "events", "capture", once=True, client=FakeClient("events"))
        self.assertEqual(status(self.config | {"global_min_free_bytes": 0}, "events")["state"], before)

    def test_lobby_has_no_new_store_or_archive_service(self):
        initialize(self.config)
        self.assertFalse((self.root / "lobby").exists())
        for role in ("capture", "archive"):
            with self.assertRaisesRegex(ObserverError, "MULTI_EXTERNAL_PRODUCER"):
                run(self.config, "lobby", role, once=True, client=FakeClient("lobby"))

    def test_shared_floor_guards_spool_recovery_archive_and_control(self):
        initialize(self.config)
        floor = 11 * 1024**3
        low = SimpleNamespace(f_bavail=floor - 1, f_frsize=1)
        room = "kibble"
        with Spool(self.root / room / "spool", room, min_free_bytes=floor) as spool:
            with patch("os.statvfs", return_value=low):
                with self.assertRaisesRegex(ObserverError, "SPOOL_STORAGE_LOW"):
                    spool.capacity()
                with self.assertRaisesRegex(ObserverError, "RECOVERY_CAPACITY_INSUFFICIENT"):
                    spool.recovery_limit()
                with self.assertRaisesRegex(ObserverError, "MANIFEST_STORAGE_LOW"):
                    ReservedArchiveWorker(spool, self.root / room / "archive", min_free_bytes=floor)
                b = self.config["budget"]
                policy = BudgetPolicy(b["read_limit_rpm"], b["external_rpm"],
                                      b["headroom_rpm"], b["capture_rpm"])
                with self.assertRaisesRegex(ObserverError, "CAPTURE_BUDGET_STORAGE_LOW"):
                    ReservedReservationBudget(self.root / "budget", policy, min_free_bytes=floor)
        with Spool(self.root / room / "spool", room, min_free_bytes=0) as spool:
            with ReservedArchiveWorker(spool, self.root / room / "archive") as archive:
                archive.reserve_floor = floor
                with patch("os.statvfs", return_value=low):
                    with self.assertRaisesRegex(ObserverError, "MANIFEST_STORAGE_LOW"):
                        archive.step()
        with ReservedReservationBudget(self.root / "budget", policy) as budget:
            budget.reserve_floor = floor
            with patch("os.statvfs", return_value=low):
                with self.assertRaisesRegex(ObserverError, "CAPTURE_BUDGET_STORAGE_LOW"):
                    with budget.transaction():
                        pass

    def test_trial_db_cap_can_be_raised_after_measurement(self):
        initialize(self.config)
        room = next(r for r in self.config["rooms"] if r["room"] == "events")
        room["max_db_bytes"] = 128 * 1024 * 1024
        result = run(self.config, "events", "capture", once=True, client=FakeClient("events"))
        self.assertEqual(result["state"]["cursor"], 1)
        with Spool(self.root / "events" / "spool", "events", max_db_bytes=room["max_db_bytes"],
                   min_free_bytes=0) as spool:
            page_size = spool.conn.execute("PRAGMA page_size").fetchone()[0]
            self.assertEqual(spool.conn.execute("PRAGMA max_page_count").fetchone()[0],
                             room["max_db_bytes"] // page_size)
            self.assertEqual(spool.state()["cursor"], 1)

    def test_uninventoried_budget_and_external_lobby_fail_closed(self):
        initialize(self.config)
        with self.assertRaisesRegex(ObserverError, "MULTI_EXTERNAL_PRODUCER"):
            run(self.config, "lobby", "capture", once=True, client=FakeClient("lobby"))
        self.config["budget"]["external_rpm"] = None
        with self.assertRaisesRegex(ObserverError, "MULTI_RUNTIME_INVENTORY_REQUIRED"):
            run(self.config, "events", "capture", once=True, client=FakeClient("events"))

    def test_intentional_stop_and_sqlite_failure_have_structured_results(self):
        output = io.StringIO()
        with patch("technocore_full_capture.multi_room.load", return_value=self.config), \
             patch("technocore_full_capture.multi_room.run",
                   side_effect=GracefulStop("CAPTURE_STOP_REQUESTED")), redirect_stdout(output):
            self.assertEqual(main(["run", "--config", "offline", "--room", "events", "--role", "capture"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "STOPPED")
        output = io.StringIO()
        with patch("technocore_full_capture.multi_room.load", return_value=self.config), \
             patch("technocore_full_capture.multi_room.run", side_effect=sqlite3.OperationalError(
                 "database or disk is full")), redirect_stdout(output):
            self.assertEqual(main(["run", "--config", "offline", "--room", "events", "--role", "capture"]), 2)
        self.assertEqual(json.loads(output.getvalue())["error"], "MULTI_SQLITE_FAILURE")
        self.assertNotIn("database or disk", output.getvalue())

    def test_recovery_slot_cannot_block_ordinary_capture(self):
        class ImmediateBudget:
            @contextmanager
            def request(self, room):
                yield
        budget = RoomBudget(ImmediateBudget(), self.root, "events", 2, 600, threading.Event())
        self.assertTrue(budget.recovery_available)
        with budget.recovery_request("events"):
            with budget.request("events"):
                pass
        one_slot = RoomBudget(ImmediateBudget(), self.root, "events", 1, 600, threading.Event())
        self.assertFalse(one_slot.recovery_available)

    def test_ordinary_slot_queue_admits_waiting_rooms_in_order(self):
        initialize(self.config)
        directory = self.root / "budget"
        b = self.config["budget"]
        policy = BudgetPolicy(b["read_limit_rpm"], b["external_rpm"],
                              b["headroom_rpm"], b["capture_rpm"])
        locks = [open(directory / f"waiter-{i}.lock", "a+b") for i in range(2)]
        for lock in locks:
            fcntl.flock(lock, fcntl.LOCK_EX)
        order = []

        def enter(room):
            with ReservationBudget(directory, policy) as shared:
                budget = RoomBudget(shared, directory, room, 3, 600, threading.Event())
                with budget.request(room):
                    order.append(room)

        def wait_for_ticket(room):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with sqlite3.connect(directory / "capture-budget.sqlite") as conn:
                    try:
                        if conn.execute("SELECT 1 FROM slot_queue WHERE room=?", (room,)).fetchone():
                            return
                    except sqlite3.OperationalError:
                        pass
                time.sleep(0.02)
            self.fail(f"slot ticket missing: {room}")

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(enter, "events")
                wait_for_ticket("events")
                second = pool.submit(enter, "kibble")
                wait_for_ticket("kibble")
                for lock in locks:
                    lock.close()
                first.result(timeout=5)
                second.result(timeout=5)
            self.assertEqual(order, ["events", "kibble"])
        finally:
            for lock in locks:
                if not lock.closed:
                    lock.close()

    def test_zero_wait_poll_is_available_for_multi_room_only(self):
        client = SafeClient("events", poll_wait=0)
        with patch.object(client, "_get", return_value="response") as get:
            self.assertEqual(client.poll(7), "response")
        self.assertEqual(get.call_args.args[1]["wait"], 0)
        self.assertEqual(SafeClient("events").poll_wait, 10)

    def test_important_recovery_keeps_trigger_and_confirmation_raw_distinct(self):
        initialize(self.config)
        name = "kibble"
        one = {"seq": 1, "from": "test", "text": "one"}
        two = {"seq": 2, "from": "test", "text": "two"}
        three = {"seq": 3, "from": "test", "text": "three"}

        def response(messages):
            return Reply(200, "application/json", json.dumps({
                "room": name, "generation": 1, "count": len(messages),
                "messages": messages, "first_seq": messages[0]["seq"],
                "last_seq": messages[-1]["seq"]}, indent=2).encode())

        class Client:
            def __init__(self):
                self.polls = [response([three]), response([three])]

            def poll(self, since):
                return self.polls.pop(0)

            def export(self, path, generation, *, max_bytes):
                Path(path).write_bytes(b"".join((canonical(row) + "\n").encode()
                                                for row in (one, two)))
                return Reply(200, "application/json", b'{"generation":1}')

        class Budget:
            @contextmanager
            def request(self, room):
                yield

            recovery_request = request

        with Spool(self.root / name / "spool", name, producer=True, raw_responses=True,
                   min_free_bytes=0, max_db_bytes=64 * 1024 * 1024) as spool:
            first = response([one])
            spool.ingest(json.loads(first.body), response_sha256=hashlib.sha256(first.body).hexdigest(),
                         raw_body=first.body)
            worker = FastCapture(spool, Client(), Budget())
            worker.step()
            self.assertEqual(spool.state()["cursor"], 3)
            batch = spool.conn.execute("SELECT * FROM batches ORDER BY batch_id DESC LIMIT 1").fetchone()
            rows = spool.conn.execute("SELECT source,body FROM response_raw WHERE batch_id=?",
                                      (batch["batch_id"],)).fetchall()
            self.assertEqual(batch["disposition"], "RECOVERED_EXPORT")
            self.assertEqual({row["source"] for row in rows}, {
                "RECOVERY_TRIGGER_POLL_HTTP_BODY", "RECOVERY_CONFIRMATION_POLL_HTTP_BODY"})
            self.assertNotEqual(batch["response_sha256"], hashlib.sha256(rows[0]["body"]).hexdigest())

    def test_independent_capture_archive_and_raw(self):
        initialize(self.config)
        important = "tclk-offers"
        standard = "events"
        result = run(self.config, important, "capture", once=True, client=FakeClient(important))
        self.assertEqual(result["state"]["cursor"], 1)
        run(self.config, standard, "capture", once=True, client=FakeClient(standard))
        run(self.config, important, "archive", once=True)
        with Spool(self.root / important / "spool", important, min_free_bytes=0) as spool:
            row = spool.conn.execute("SELECT batch_id,body,body_sha256,encoding FROM response_raw").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(hashlib.sha256(row["body"]).hexdigest(), row["body_sha256"])
            self.assertIn(b'\n  "room"', row["body"])
            self.assertEqual(row["encoding"], "HTTP_DECODED_BODY")
            normalized = spool.conn.execute("SELECT payload FROM entries WHERE kind='MESSAGE'").fetchone()[0]
            self.assertNotEqual(row["body"], normalized.encode())
            self.assertEqual(spool.conn.execute("PRAGMA user_version").fetchone()[0], 2)
        with self.assertRaisesRegex(ObserverError, "SPOOL_SCHEMA_MISMATCH"):
            with Spool(self.root / important / "spool", important, producer=True,
                       min_free_bytes=0, max_db_bytes=64 * 1024 * 1024):
                pass
        with Spool(self.root / standard / "spool", standard, min_free_bytes=0) as spool:
            self.assertEqual(spool.conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(spool.state()["cursor"], 1)
        self.assertEqual(status(self.config, important)["archive"]["messages"], 1)
        self.assertEqual(status(self.config, standard)["state"]["cursor"], 1)

    def test_room_failure_does_not_advance_another_room(self):
        initialize(self.config)
        run(self.config, "events", "capture", once=True, client=FakeClient("events"))
        failed = run(self.config, "kibble", "capture", once=True, client=FakeClient("wrong-room"))
        self.assertEqual(failed["capture"]["capture_status"], "RETRYING")
        self.assertEqual(status(self.config, "events")["state"]["cursor"], 1)
        self.assertEqual(status(self.config, "kibble")["state"]["cursor"], 0)

    def test_twelve_rooms_share_waiter_ceiling_and_advance_independently(self):
        initialize(self.config)
        guard = threading.Lock()
        counts = {"active": 0, "peak": 0}

        class TimedClient(FakeClient):
            def poll(self, since):
                with guard:
                    counts["active"] += 1
                    counts["peak"] = max(counts["peak"], counts["active"])
                try:
                    time.sleep(0.35)
                    return super().poll(since)
                finally:
                    with guard:
                        counts["active"] -= 1

        with ThreadPoolExecutor(max_workers=11) as pool:
            outcomes = list(pool.map(lambda name: run(self.config, name, "capture", once=True,
                                                       client=TimedClient(name)), ROOMS[1:]))
        self.assertEqual(len(outcomes), 11)
        self.assertTrue(all(item["state"]["cursor"] == 1 for item in outcomes))
        self.assertLessEqual(counts["peak"], 2)
        self.assertGreaterEqual(counts["peak"], 2)

    def test_raw_and_cursor_roll_back_together(self):
        initialize(self.config)
        name = "kibble"
        reply = FakeClient(name).poll(0)
        envelope = json.loads(reply.body)

        def fail(point):
            if point == "before_commit":
                raise RuntimeError("synthetic crash")

        with Spool(self.root / name / "spool", name, producer=True, raw_responses=True,
                   min_free_bytes=0, max_db_bytes=64 * 1024 * 1024, checkpoint=fail) as spool:
            with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
                spool.ingest(envelope, response_sha256=hashlib.sha256(reply.body).hexdigest(),
                             raw_body=reply.body)
            self.assertEqual(spool.state()["cursor"], 0)
            self.assertEqual(spool.conn.execute("SELECT count(*) FROM response_raw").fetchone()[0], 0)

    def test_room_spacing_starts_at_http_slot_acquisition(self):
        class ImmediateBudget:
            def __init__(self):
                self.grants = []

            @contextmanager
            def request(self, room):
                self.grants.append(time.monotonic())
                yield

        shared = ImmediateBudget()
        stop = threading.Event()
        budget = RoomBudget(shared, self.root, "events", 1, 600, stop)
        lock = open(self.root / "waiter-0.lock", "a+b")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            def two_requests():
                starts = []
                for _ in range(2):
                    with budget.request("events"):
                        starts.append(time.monotonic())
                return starts

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(two_requests)
                time.sleep(0.2)  # Longer than the configured 0.1-second Room spacing.
                self.assertEqual(shared.grants, [])  # No shared reservation before a slot exists.
                lock.close()
                starts = future.result(timeout=2)
            self.assertGreaterEqual(starts[1] - starts[0], 0.09)
        finally:
            if not lock.closed:
                lock.close()

    def test_signal_handler_only_sets_flag_and_interrupts_wait(self):
        stop = SignalStop()
        previous = signal.signal(signal.SIGUSR1, lambda *_: stop.set())
        timer = threading.Timer(0.03, lambda: os.kill(os.getpid(), signal.SIGUSR1))
        try:
            timer.start()
            started = time.monotonic()
            self.assertTrue(stop.wait(1))
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            timer.join()
            signal.signal(signal.SIGUSR1, previous)

    def test_important_recovery_reserves_raw_poll_bytes(self):
        initialize(self.config)
        name = "kibble"
        source = self.root / name / "spool"
        with Spool(source, name, producer=True, raw_responses=True, min_free_bytes=0,
                   max_db_bytes=64 * 1024 * 1024) as spool:
            first = FakeClient(name).poll(0)
            spool.ingest(json.loads(first.body), response_sha256=hashlib.sha256(first.body).hexdigest(),
                         raw_body=first.body)
            with tempfile.TemporaryDirectory(dir=source, prefix=".recovery-") as directory:
                path = Path(directory) / "normalized.ndjson"
                normalized = (canonical({"seq": 2, "from": "test", "text": "recovered"}) + "\n").encode()
                path.write_bytes(normalized)
                poll_body = b'{"raw":"' + b"x" * 4096 + b'"}'
                page_size = spool.conn.execute("PRAGMA page_size").fetchone()[0]
                old_reserve = len(normalized) * 2 + MAX_LINE + 4 * page_size
                before = spool.state()

                def capacity(reserve_bytes):
                    if reserve_bytes > old_reserve:
                        raise ObserverError("SPOOL_STORAGE_LOW")

                args = {"generation": 1, "request_epoch": 1, "request_since": 1,
                        "last_seq": 2, "count": 1,
                        "response_sha256": hashlib.sha256(normalized).hexdigest(),
                        "raw_body": poll_body, "confirmation_raw_body": b'{"confirm":true}'}
                with patch.object(spool, "capacity", side_effect=capacity) as guarded:
                    with self.assertRaisesRegex(ObserverError, "SPOOL_STORAGE_LOW"):
                        spool.ingest_recovery(path, **args)
                guarded.assert_called_once_with(old_reserve + len(poll_body) + len(args["confirmation_raw_body"]))
                self.assertEqual(spool.state(), before)
                self.assertEqual(spool.conn.execute("SELECT count(*) FROM response_raw").fetchone()[0], 1)
                spool.ingest_recovery(path, **args)
                self.assertEqual(spool.state()["cursor"], 2)
                rows = spool.conn.execute("SELECT source,body FROM response_raw WHERE batch_id=?",
                                          (spool.conn.execute("SELECT max(batch_id) FROM batches").fetchone()[0],)).fetchall()
                self.assertEqual({row["source"]: row["body"] for row in rows}, {
                    "RECOVERY_TRIGGER_POLL_HTTP_BODY": poll_body,
                    "RECOVERY_CONFIRMATION_POLL_HTTP_BODY": args["confirmation_raw_body"]})

    def test_important_recovery_checks_db_headroom_before_and_during_ingest(self):
        initialize(self.config)
        name = "kibble"
        source = self.root / name / "spool"
        with Spool(source, name, producer=True, raw_responses=True, min_free_bytes=0,
                   max_db_bytes=64 * 1024 * 1024) as spool:
            first = FakeClient(name).poll(0)
            spool.ingest(json.loads(first.body), response_sha256=hashlib.sha256(first.body).hexdigest(),
                         raw_body=first.body)
            page_size = spool.conn.execute("PRAGMA page_size").fetchone()[0]
            used = spool.conn.execute("PRAGMA page_count").fetchone()[0] * page_size
            configured_cap = spool.max_db_bytes
            page_cap = spool.conn.execute("PRAGMA max_page_count").fetchone()[0]
            try:
                # The second poll is not available at export start. The bound
                # must reserve both maximum-size raw bodies before export.
                spool.max_db_bytes = used + 2 * MAX_BODY
                with self.assertRaisesRegex(ObserverError, "RECOVERY_CAPACITY_INSUFFICIENT"):
                    spool.recovery_limit()

                with tempfile.TemporaryDirectory(dir=source, prefix=".recovery-") as directory:
                    path = Path(directory) / "normalized.ndjson"
                    normalized = (canonical({"seq": 2, "from": "test", "text": "two"}) + "\n").encode()
                    path.write_bytes(normalized)
                    trigger = b"x" * 4096
                    confirmation = b"y" * 4096
                    normalized_reserve = len(normalized) * 2 + MAX_LINE + 4 * page_size
                    spool.max_db_bytes = configured_cap
                    used_pages = spool.conn.execute("PRAGMA page_count").fetchone()[0]
                    tight_cap = used_pages + (normalized_reserve + page_size - 1) // page_size + 1
                    self.assertEqual(spool.conn.execute(
                        f"PRAGMA max_page_count={tight_cap}").fetchone()[0], tight_cap)
                    before = spool.state()
                    batches = spool.conn.execute("SELECT count(*) FROM batches").fetchone()[0]
                    raw_rows = spool.conn.execute("SELECT count(*) FROM response_raw").fetchone()[0]
                    messages = spool.conn.execute(
                        "SELECT count(*) FROM entries WHERE kind='MESSAGE'").fetchone()[0]
                    args = {"generation": 1, "request_epoch": 1, "request_since": 1,
                            "last_seq": 2, "count": 1,
                            "response_sha256": hashlib.sha256(normalized).hexdigest(),
                            "raw_body": trigger, "confirmation_raw_body": confirmation}
                    # Isolate the transaction-time guard after the separate
                    # pre-export guard has been exercised above.
                    with patch.object(spool, "recovery_limit", return_value=len(normalized)):
                        with self.assertRaisesRegex(ObserverError, "RECOVERY_DB_HEADROOM_INSUFFICIENT"):
                            spool.ingest_recovery(path, **args)
                    self.assertEqual(spool.state(), before)
                    self.assertEqual(spool.conn.execute("SELECT count(*) FROM batches").fetchone()[0], batches)
                    self.assertEqual(spool.conn.execute("SELECT count(*) FROM response_raw").fetchone()[0], raw_rows)
                    self.assertEqual(spool.conn.execute(
                        "SELECT count(*) FROM entries WHERE kind='MESSAGE'").fetchone()[0], messages)
                    self.assertFalse(spool.conn.in_transaction)
            finally:
                spool.conn.execute(f"PRAGMA max_page_count={page_cap}")
                spool.max_db_bytes = configured_cap


if __name__ == "__main__":
    unittest.main()
