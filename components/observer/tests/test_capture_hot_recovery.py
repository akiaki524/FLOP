"""Offline hot-room recovery regressions; no network or production state IO."""

from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from technocore_full_capture.capture_first import FastCapture
from technocore_full_capture.deadline import RequestFailure
from technocore_full_capture.production import TimedClient
from technocore_full_capture.spool import Spool, canonical
from technocore_observer.protocol import Reply


def message(seq, *, text=None):
    return {"seq": seq, "text": text or "message-%d" % seq, "from": "fixture"}


def envelope(messages, generation=1):
    return {"room": "test-room", "generation": generation,
            "count": len(messages), "messages": messages,
            "first_seq": messages[0]["seq"] if messages else None,
            "last_seq": messages[-1]["seq"] if messages else None}


def reply(value, status=200):
    body = json.dumps(value, separators=(",", ":")).encode()
    return Reply(status, "application/json", body)


class ImmediateBudget:
    def __init__(self):
        self.requests = 0
        self.deferred = []

    @contextmanager
    def request(self, room):
        self.requests += 1
        yield

    def defer(self, seconds):
        self.deferred.append(seconds)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class HotRoomClient:
    """Newest-200 poll model with a 20-message/second hot room.

    The first poll establishes cursor 10. Later poll responses are the newest
    200 records, so a sufficiently long export can race the room head. Export
    duration advances the fake clock while the room continues to grow.
    """

    def __init__(self, clock, *, export_duration=0, export_outcome="success",
                 export_snapshot="completion", export_generation=1,
                 generation_after=None, overlap_conflict=False):
        self.clock = clock
        self.export_duration = export_duration
        self.export_outcome = export_outcome
        self.export_snapshot = export_snapshot
        self.export_generation = export_generation
        self.generation_after = generation_after
        self.overlap_conflict = overlap_conflict
        self.generation = 1
        self.records = [message(seq) for seq in range(1, 511)]
        self.poll_calls = []
        self.export_calls = []
        self._initial = True
        self._last_growth = clock()

    def _grow(self):
        elapsed = max(0, self.clock() - self._last_growth)
        count = int(elapsed * 20)
        if count:
            first = self.records[-1]["seq"] + 1
            self.records.extend(message(seq) for seq in range(first, first + count))
            self._last_growth += count / 20

    def poll(self, since):
        self._grow()
        self.poll_calls.append(since)
        if self._initial:
            self._initial = False
            selected = self.records[:10]
        else:
            selected = self.records[-200:]
            selected = [item for item in selected if item["seq"] > since]
        return reply(envelope(selected, self.generation))

    def export(self, path, generation, *, max_bytes=None):
        self._grow()
        self.export_calls.append({"path": Path(path), "generation": generation,
                                  "max_bytes": max_bytes, "start_head": self.records[-1]["seq"]})
        start_head = self.records[-1]["seq"]
        self.clock.advance(self.export_duration)
        self._grow()
        if self.export_outcome == "failure":
            raise RequestFailure("TOTAL_REQUEST_DEADLINE")
        if self.generation_after is not None:
            self.generation = self.generation_after
        if self.export_snapshot == "start":
            end = start_head
        else:
            end = self.records[-1]["seq"]
        records = [dict(item) for item in self.records if item["seq"] <= end]
        if self.overlap_conflict:
            for item in records:
                if item["seq"] == 311:
                    item["text"] = "changed-during-export"
        Path(path).write_bytes(b"".join(canonical(item).encode() + b"\n" for item in records))
        return reply({"generation": self.export_generation})


class HotRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="capture-hot-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def worker(self, client, clock):
        spool_path = self.root / "spool"
        spool_path.mkdir()
        spool = Spool(spool_path, "test-room", producer=True, create=True,
                      min_free_bytes=0)
        self.addCleanup(spool.close)
        worker = FastCapture(spool, client, ImmediateBudget(), clock=clock)
        return spool, worker

    def establish_cursor(self, worker):
        worker.step()

    def test_hot_room_export_five_seconds_success_uses_one_attempt(self):
        clock = FakeClock()
        client = HotRoomClient(clock, export_duration=5)
        spool, worker = self.worker(client, clock)
        self.establish_cursor(worker)

        worker.step()

        self.assertEqual(len(client.export_calls), 1)
        self.assertEqual(spool.state()["cursor"], 610)
        self.assertEqual(spool.state()["gaps"], 0)
        self.assertEqual(worker.recovery_metrics["recovery_attempts_process"], 1)
        self.assertEqual(worker.recovery_metrics["recovery_successes_process"], 1)
        self.assertEqual(worker.recovery_metrics["recovery_failures_process"], 0)
        self.assertGreaterEqual(worker.recovery_metrics["recovery_export_seconds"], 5)
        for _ in range(20):
            clock.advance(5)
            worker.step()
        self.assertEqual(len(client.export_calls), 1)
        self.assertEqual(spool.state()["gaps"], 0)
        self.assertEqual(spool.state()["cursor"], client.records[-1]["seq"])

    def test_hot_room_export_fifteen_seconds_success_covers_racing_head(self):
        clock = FakeClock()
        client = HotRoomClient(clock, export_duration=15)
        spool, worker = self.worker(client, clock)
        self.establish_cursor(worker)

        worker.step()

        self.assertEqual(len(client.export_calls), 1)
        self.assertEqual(spool.state()["cursor"], 810)
        # Ten records were committed by the bootstrap poll, followed by the
        # 800-record retained range through the racing head.
        self.assertEqual(spool.state()["messages"], 810)
        self.assertEqual(spool.state()["gaps"], 0)

        for _ in range(20):
            clock.advance(5)
            worker.step()
        self.assertEqual(len(client.export_calls), 1)
        self.assertEqual(spool.state()["gaps"], 0)
        self.assertEqual(spool.state()["cursor"], client.records[-1]["seq"])

    def test_hot_room_export_thirty_seconds_failure_enters_circuit_breaker(self):
        clock = FakeClock()
        client = HotRoomClient(clock, export_duration=30, export_outcome="failure")
        spool, worker = self.worker(client, clock)
        self.establish_cursor(worker)

        worker.step()
        self.assertEqual(len(client.export_calls), 1)
        self.assertGreater(client.export_calls[0]["start_head"] - 10, 200)
        self.assertEqual(worker.recovery_metrics["recovery_failures_process"], 1)
        self.assertEqual(spool.state()["gaps"], 1)

        # The failed export lost another 600 records of polling opportunity.
        # The next normal poll must record that real gap, then continue at the
        # normal cadence. Cooldown must not hide a gap or freeze capture.
        clock.advance(5)
        worker.step()
        self.assertEqual(worker.last["recovery_status"], "COOLDOWN")
        self.assertEqual(spool.state()["gaps"], 2)
        for _ in range(19):
            clock.advance(5)
            worker.step()
        self.assertEqual(len(client.export_calls), 1)
        self.assertEqual(worker.recovery_metrics["recovery_attempts_process"], 1)
        self.assertEqual(spool.state()["gaps"], 2)
        self.assertEqual(spool.state()["cursor"], client.records[-1]["seq"])
        gaps = [tuple(r) for r in spool.conn.execute("SELECT seq,end_seq FROM entries WHERE kind='GAP'")]
        self.assertEqual(gaps, [(11, 310), (511, 1010)])

    def test_recovery_export_and_after_poll_bridge_the_new_range(self):
        clock = FakeClock()
        # The export snapshot ends when the request starts. The after poll
        # overlaps its tail and proves the additional records through seq 610.
        client = HotRoomClient(clock, export_duration=5, export_snapshot="start")
        spool, worker = self.worker(client, clock)
        self.establish_cursor(worker)

        worker.step()

        self.assertEqual(spool.state()["cursor"], 610)
        self.assertEqual(spool.state()["gaps"], 0)
        self.assertEqual(client.poll_calls, [0, 10, 0])

    def test_breaker_can_retry_only_after_cooldown_on_another_real_gap(self):
        clock = FakeClock()
        client = HotRoomClient(clock, export_duration=30, export_outcome="failure")
        spool, worker = self.worker(client, clock)
        worker.step()
        worker.step()
        self.assertEqual(worker.recovery_status()["recovery_cooldown_remaining_seconds"], 120)
        clock.advance(120)
        worker.step()
        self.assertEqual(len(client.export_calls), 2)
        self.assertEqual(worker.recovery_metrics["recovery_failures_process"], 2)
        self.assertEqual(worker.recovery_status()["recovery_cooldown_remaining_seconds"], 120)
        self.assertEqual(spool.state()["gaps"], 2)

    def test_after_poll_hole_is_unrecoverable_and_fails_closed(self):
        clock = FakeClock()
        # Fifteen seconds creates 300 new records. A start-time export ending
        # at 510 and newest-200 after page beginning at 611 cannot prove
        # 511..610, so recovery must not commit a false contiguous range.
        client = HotRoomClient(clock, export_duration=15, export_snapshot="start")
        spool, worker = self.worker(client, clock)
        self.establish_cursor(worker)

        worker.step()

        self.assertEqual(spool.state()["cursor"], 510)
        self.assertEqual(spool.state()["gaps"], 1)
        self.assertEqual(worker.last["recovery_status"], "UNRECOVERABLE")
        self.assertEqual(worker.recovery_metrics["recovery_failures_process"], 1)

    def test_generation_change_during_recovery_is_unrecoverable(self):
        clock = FakeClock()
        client = HotRoomClient(clock, export_duration=5, generation_after=2)
        spool, worker = self.worker(client, clock)
        self.establish_cursor(worker)

        worker.step()

        self.assertEqual(worker.last["recovery_status"], "UNRECOVERABLE")
        self.assertEqual(worker.last["recovery_error"], "RECOVERY_GENERATION_MISMATCH")
        self.assertEqual(spool.state()["gaps"], 1)
        self.assertEqual(worker.recovery_metrics["recovery_successes_process"], 0)

    def test_overlapping_export_content_change_is_unrecoverable(self):
        clock = FakeClock()
        client = HotRoomClient(clock, export_duration=5, overlap_conflict=True)
        spool, worker = self.worker(client, clock)
        self.establish_cursor(worker)

        worker.step()

        self.assertEqual(worker.last["recovery_status"], "UNRECOVERABLE")
        self.assertEqual(worker.last["recovery_error"], "RECOVERY_CONTENT_MISMATCH")
        self.assertEqual(spool.state()["gaps"], 1)

    def test_normal_path_does_not_export_or_mix_export_into_poll_histogram(self):
        clock = FakeClock()
        client = HotRoomClient(clock, export_duration=15)
        timed = TimedClient(client)
        timed.poll(0)
        with tempfile.TemporaryDirectory(prefix="capture-hot-timed-") as directory:
            path = Path(directory) / "export.ndjson"
            timed.export(path, 1, max_bytes=1024 * 1024)
        self.assertEqual(sum(timed.histogram()["counts"]), 1)


if __name__ == "__main__":
    unittest.main()
