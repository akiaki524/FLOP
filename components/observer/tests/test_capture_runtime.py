"""Offline API model, cross-process admission and hard transport deadlines."""

from contextlib import contextmanager
import io
import json
import multiprocessing
import os
from pathlib import Path
import socket
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from test_capture_spool import page, producer
from technocore_full_capture.capture_first import FastCapture, main
from technocore_full_capture.deadline import DeadlineClient, RequestFailure, send_reply
from technocore_full_capture.pacing import BudgetPolicy, HostBudget, GracefulStop
from technocore_full_capture.scheduling import ReservationBudget
from technocore_full_capture.spool import Spool
from technocore_observer.http import SafeClient
from technocore_observer.protocol import ObserverError, Reply, MAX_BODY


class WindowAPI:
    """Newest-N reverse scan AND bounded server bytes; never oldest-after."""
    def __init__(self, last=0, byte_window=1024 * 1024):
        self.records = page(1, last)["messages"]
        self.byte_window, self.generation = byte_window, 1
        self.calls = []
        self.failure = None

    def grow(self, count):
        first = self.records[-1]["seq"] + 1 if self.records else 1
        self.records.extend(page(first, first + count - 1)["messages"])

    def poll(self, since):
        self.calls.append(since)
        if self.failure is not None:
            return self.failure
        selected, size = [], 0
        for message in reversed(self.records):
            if message["seq"] <= since:
                break
            cost = len(json.dumps(message).encode()) + 1
            if size + cost > self.byte_window:
                break
            selected.append(message)
            size += cost
            if len(selected) == 200:
                break
        selected.reverse()
        envelope = {"room": "test-room", "generation": self.generation, "count": len(selected),
                    "messages": selected, "first_seq": selected[0]["seq"] if selected else None,
                    "last_seq": selected[-1]["seq"] if selected else since}
        return Reply(200, "application/json", json.dumps(envelope).encode())


class ImmediateBudget:
    def __init__(self):
        self.requests, self.cooldown = 0, 0

    @contextmanager
    def request(self, room):
        self.requests += 1
        yield

    def defer(self, delay):
        self.cooldown = delay


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds
        return False

    def is_set(self):
        return False


def reply_transport(room, since, channel):
    send_reply(channel, Reply(200, "application/json", json.dumps(page(1, 2)).encode()))


def large_transport(room, since, channel):
    send_reply(channel, Reply(200, "application/json", b"x" * MAX_BODY))


def stalled_transport(room, since, channel):
    time.sleep(60)


def framed_trickle(room, since, channel):
    # A readable prefix must not make the parent's receive wait unboundedly.
    channel.sendall(struct.pack("!I", 1024))
    for _ in range(1024):
        channel.sendall(b" ")
        time.sleep(0.03)


def http_body_trickle(room, since, channel):
    class SlowResponse:
        code = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, count):
            # Every byte arrives before a socket inactivity timeout, while the
            # complete body read would take minutes.
            for _ in range(10000):
                time.sleep(0.01)
            return b"{}"

    class Opener:
        def open(self, request, timeout):
            assert request.full_url.startswith("https://technocore.chat/r/test-room?")
            assert request.get_method() == "GET" and request.data is None
            return SlowResponse()

    client = SafeClient(room)
    client._opener = Opener()
    send_reply(channel, client.poll(since))


def huge_frame(room, since, channel):
    channel.sendall(struct.pack("!I", MAX_BODY * 4))
    time.sleep(60)


def hold_request(directory, room, channel):
    with ReservationBudget(directory, BudgetPolicy(600, 120, 360, 120)) as budget:
        with budget.request(room):
            channel.send(time.monotonic())
            channel.recv()
    channel.close()


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_one_get_latest_n_and_future_capture(self):
        api, budget = WindowAPI(100), ImmediateBudget()
        with producer(self.root) as spool:
            worker = FastCapture(spool, api, budget)
            worker.step()
            api.grow(400)
            self.assertEqual(worker.step(), 0)
            self.assertEqual(api.calls, [0, 100])
            self.assertEqual(budget.requests, 2)
            gap = spool.conn.execute("SELECT seq,end_seq FROM entries WHERE kind='GAP'").fetchone()
            self.assertEqual(tuple(gap), (101, 300))
            self.assertEqual(spool.state()["cursor"], 500)
            api.grow(20)
            worker.step()
            self.assertEqual(spool.state()["cursor"], 520)

    def test_byte_window_under_200_is_still_a_gap(self):
        api = WindowAPI(100)
        with producer(self.root) as spool:
            worker = FastCapture(spool, api, ImmediateBudget())
            worker.step()
            api.grow(20)
            api.byte_window = sum(len(json.dumps(m).encode()) + 1 for m in api.records[-5:])
            worker.step()
            self.assertEqual(spool.state()["cursor"], 120)
            self.assertEqual(spool.state()["messages"], 105)
            self.assertEqual(tuple(spool.conn.execute("SELECT seq,end_seq FROM entries WHERE kind='GAP'").fetchone()), (101, 115))

    def test_generation_resets_request_cursor_even_when_response_empty(self):
        api = WindowAPI(100)
        with producer(self.root) as spool:
            worker = FastCapture(spool, api, ImmediateBudget())
            worker.step()
            api.generation = 2
            api.records = page(1, 4)["messages"]
            self.assertEqual(worker.step(), 0)
            self.assertEqual(spool.state()["cursor"], 0)
            worker.step()
            self.assertEqual(api.calls, [0, 100, 0])
            self.assertEqual(spool.state()["cursor"], 4)

    def test_retries_and_protocol_evidence_keep_cursor(self):
        api, budget = WindowAPI(3), ImmediateBudget()
        with producer(self.root) as spool:
            worker = FastCapture(spool, api, budget)
            worker.step()
            for reply in (Reply(503, "text/plain", b"untrusted"),
                          Reply(429, "text/plain", b"untrusted", retry_after="13"),
                          Reply(200, "application/json", b"{")):
                api.failure = reply
                self.assertGreaterEqual(worker.step(), 1)
                self.assertEqual(spool.state()["cursor"], 3)
            self.assertGreaterEqual(budget.cooldown, 13)
            api.failure = None
            api.grow(2)
            worker.step()
            self.assertEqual(spool.state()["cursor"], 5)
            self.assertIsNone(spool.state()["last_failure"])
            self.assertEqual(spool.conn.execute("SELECT count(*) FROM failures").fetchone()[0], 3)

    def test_transport_failure_and_local_storage_failure_are_distinct(self):
        with producer(self.root) as spool:
            api = WindowAPI(2)
            worker = FastCapture(spool, api, ImmediateBudget())
            with patch.object(api, "poll", side_effect=RequestFailure("TOTAL_REQUEST_DEADLINE")):
                worker.step()
            self.assertEqual(spool.state()["cursor"], 0)
            with patch.object(spool, "capacity", side_effect=ObserverError("SPOOL_STORAGE_LOW")):
                with self.assertRaisesRegex(ObserverError, "STORAGE_LOW"):
                    worker.step()
            self.assertEqual(api.calls, [])

    def test_full_replay_does_not_spin_and_no_export_capability(self):
        api = WindowAPI(200)
        with producer(self.root) as spool:
            worker = FastCapture(spool, api, ImmediateBudget())
            self.assertEqual(worker.step(), 0)
            api.failure = Reply(200, "application/json", json.dumps(page(1, 200)).encode())
            self.assertEqual(worker.step(), 1)
            self.assertEqual(spool.state()["messages"], 200)

    def test_cli_capture_offline_injection_and_read_status(self):
        budget = self.root / "budget"
        source = self.root / "spool"
        budget.mkdir()
        source.mkdir()
        args = ["capture", "--room", "test-room", "--spool-dir", str(source), "--budget-dir", str(budget),
                "--read-limit-rpm", "600", "--observer-reserve-rpm", "120", "--headroom-rpm", "360",
                "--capture-rpm", "120", "--min-free-bytes", "0", "--once"]
        with patch("technocore_full_capture.capture_first.DeadlineClient", return_value=WindowAPI(10)), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(args), 0)
            self.assertEqual(json.loads(output.getvalue())["producer"]["cursor"], 10)
        for command in ("status", "read"):
            with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                    patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(main([command, "--room", "test-room", "--spool-dir", str(source), "--min-free-bytes", "0"]), 0)


class BudgetTests(unittest.TestCase):
    def test_fifo_full_page_requeue_rate_and_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            clients = [ReservationBudget(directory, BudgetPolicy(600, 120, 360, 120),
                                         clock=clock, boot="test", stop=clock) for _ in range(3)]
            try:
                names = ("high", "low-a", "low-b")
                starts = []
                for _ in range(1000):
                    # high polls admission more often; cannot jump the queue.
                    for index in (0, 0, 0, 1, 2):
                        if clients[index].attempt(names[index]):
                            starts.append((clock(), index))
                    clock.wait(0.05)
                self.assertGreater(len(starts), 60)
                self.assertEqual([index for _, index in starts[:12]], [0, 1, 2] * 4)
                self.assertTrue(all(b[0] - a[0] >= 0.4999 for a, b in zip(starts, starts[1:])))
                for timestamp, _ in starts:
                    self.assertLessEqual(sum(timestamp <= t < timestamp + 60 for t, _ in starts), 120)
                for index in range(3):
                    times = [t for t, room in starts if room == index]
                    self.assertLess(max(b - a for a, b in zip(times, times[1:])), 1.8)
                clients[0].defer(13)
                until = clock() + 13
                while clock() < until:
                    self.assertFalse(clients[1].attempt("low-a"))
                    self.assertFalse(clients[2].attempt("low-b"))
                    clock.wait(0.05)
            finally:
                for client in clients:
                    client.close()

    def test_dead_ticket_expires_restart_no_refund_and_policy_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            policy = BudgetPolicy(600, 120, 360, 120)
            with ReservationBudget(directory, policy, clock=clock, boot="test", stop=clock) as dead:
                self.assertFalse(dead.attempt("dead"))
            with ReservationBudget(directory, policy, clock=clock, boot="test", stop=clock) as live:
                self.assertFalse(live.attempt("live"))
                for _ in range(39):
                    clock.wait(0.05)
                    self.assertFalse(live.attempt("live"))
                clock.wait(0.1)
                self.assertTrue(live.attempt("live"))
            with ReservationBudget(directory, policy, clock=clock, boot="test", stop=clock) as restarted:
                self.assertFalse(restarted.attempt("live"))
                clock.wait(0.5)
                self.assertTrue(restarted.attempt("live"))
            with self.assertRaisesRegex(ObserverError, "POLICY_MISMATCH"):
                ReservationBudget(directory, BudgetPolicy(600, 120, 360, 100), clock=clock, boot="test")
            with ReservationBudget(directory, policy, clock=clock, boot="new-boot", stop=clock) as rebooted:
                self.assertFalse(rebooted.attempt("live"))

    def test_http_body_does_not_hold_shared_lock_across_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = multiprocessing.get_context("fork")
            parent_a, child_a = ctx.Pipe()
            parent_b, child_b = ctx.Pipe()
            a = ctx.Process(target=hold_request, args=(directory, "slow", child_a))
            b = ctx.Process(target=hold_request, args=(directory, "fast", child_b))
            try:
                a.start()
                self.assertTrue(parent_a.poll(5))
                first = parent_a.recv()
                b.start()
                self.assertTrue(parent_b.poll(5))
                second = parent_b.recv()
                self.assertGreaterEqual(second - first, 0.49)
                self.assertLess(second - first, 2)
                # a still owns its request context while b has already started.
                parent_a.send("finish")
                parent_b.send("finish")
                a.join(5)
                b.join(5)
                self.assertEqual((a.exitcode, b.exitcode), (0, 0))
            finally:
                for process in (a, b):
                    if process.pid is not None and process.is_alive():
                        process.kill()
                        process.join()
                for channel in (parent_a, parent_b, child_a, child_b):
                    channel.close()

    def test_no_legacy_budget_mixing(self):
        policy = BudgetPolicy(600, 120, 360, 120)
        with tempfile.TemporaryDirectory() as directory:
            with ReservationBudget(directory, policy):
                with self.assertRaisesRegex(GracefulStop, "MIXING"):
                    HostBudget(directory, policy)
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            legacy = HostBudget(directory, policy, clock, clock, "boot")
            with legacy.request():
                pass
            with self.assertRaisesRegex(ObserverError, "MIXING"):
                ReservationBudget(directory, policy)


class DeadlineTests(unittest.TestCase):
    def test_bounded_response_and_reaped_worker(self):
        client = DeadlineClient("test-room", 3, target=reply_transport)
        self.assertEqual(json.loads(client.poll(0).body)["count"], 2)
        with self.assertRaises(ProcessLookupError):
            os.kill(client.last_pid, 0)

    def test_large_frame_is_received_incrementally(self):
        client = DeadlineClient("test-room", 3, target=large_transport)
        self.assertEqual(len(client.poll(0).body), MAX_BODY)

    def test_dns_header_stall_http_trickle_and_partial_ipc_frame_deadlines(self):
        for target in (stalled_transport, http_body_trickle, framed_trickle):
            with self.subTest(target=target.__name__):
                client = DeadlineClient("test-room", 0.4, target=target)
                started = time.monotonic()
                with self.assertRaisesRegex(RequestFailure, "TOTAL_REQUEST_DEADLINE"):
                    client.poll(0)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 1.1)  # 0.4 deadline + <=0.5 reap + scheduling margin
                with self.assertRaises(ProcessLookupError):
                    os.kill(client.last_pid, 0)

    def test_oversize_ipc_prefix_rejected_without_body_allocation(self):
        client = DeadlineClient("test-room", 3, target=huge_frame)
        with self.assertRaisesRegex(RequestFailure, "FRAME_LIMIT"):
            client.poll(0)


if __name__ == "__main__":
    unittest.main()
