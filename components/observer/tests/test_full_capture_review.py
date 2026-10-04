"""Independent review regressions; no live traffic."""

import tempfile
import errno
import io
import json
import multiprocessing
import os
from pathlib import Path
import threading
import time
import sys
import unittest
from unittest.mock import Mock, patch

from test_full_capture import Archive, FullCapture, Ring, Response, view, line
from technocore_full_capture.__main__ import ExportClient, RetryCapture, Snapshot, main
from technocore_full_capture.pacing import BudgetPolicy, GracefulStop, HostBudget
from technocore_full_capture.probe import observe, main as probe_main, UNVERIFIED
from technocore_observer.protocol import ObserverError, Reply
from full_capture_container_check import configuration_checks, PAYLOAD


class VirtualTime:
    def __init__(self):
        self.now = 1000.0

    def clock(self):
        return self.now

    def wait(self, delay):
        assert delay >= 0
        self.now += delay
        return False

    def is_set(self):
        return False


def process_requests(directory, queue):
    budget = HostBudget(directory, BudgetPolicy(1200, 300, 300, 600))
    for _ in range(3):
        with budget.request():
            queue.put(time.monotonic())


class ModelOpener:
    """Actual ExportClient GET path and budget, independent remote ring model."""
    def __init__(self, clock):
        self.ring = Ring(1, 1)
        self.clock = clock
        self.requests = []
        self.growing = True
        self.failure = None
        self.failure_at = None

    def open(self, request, timeout):
        self.requests.append((self.clock(), request.full_url))
        if self.failure and (self.failure_at is None or len(self.requests) == self.failure_at):
            code, self.failure = self.failure, None
            return Response(b"", code, **{"Retry-After": "13"})
        if request.full_url.endswith("/export"):
            response = Response(b"".join(self.ring.lines))
            if self.growing:
                self.ring.grow(1)
            return response
        return Response(json.dumps(self.ring.tail()).encode(), **{"Content-Type": "application/json"})


class ReviewRegressionTests(unittest.TestCase):
    def test_continuously_growing_export_has_no_zero_delay(self):
        with tempfile.TemporaryDirectory() as directory:
            ring = Ring(1, 1)
            ring.after_export = lambda: ring.grow(1)
            with Archive(directory, "test-room", min_free_bytes=0) as archive:
                worker = FullCapture(archive, ring, interval=5)
                delays = [worker.step() for _ in range(20)]
                self.assertEqual(archive.state["cursor"], 20)
                self.assertEqual(delays, [5] * 20)
                ring.after_export = None
                worker.step()
                self.assertEqual(archive.verify()["cursor"], 21)

    def test_paced_real_client_high_rate_429_then_catch_up(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as shared:
            clock = VirtualTime()
            policy = BudgetPolicy(120, 50, 10, 60)
            budget = HostBudget(shared, policy, clock, clock.clock, "test-boot")
            client = ExportClient("test-room", budget)
            opener = ModelOpener(clock.clock)
            client.tail_client._opener = opener
            with Archive(directory, "test-room", min_free_bytes=0) as archive:
                worker = FullCapture(archive, client, interval=5)
                cycle_starts = []
                for _ in range(20):
                    cycle_starts.append(clock.clock())
                    clock.wait(worker.step())
                self.assertEqual(archive.state["cursor"], 20)
                self.assertEqual(len(opener.requests), 60)
                self.assertTrue(all(b - a >= 5 for a, b in zip(cycle_starts, cycle_starts[1:])))
                times = [t for t, _ in opener.requests]
                self.assertTrue(all(b - a >= 1 for a, b in zip(times, times[1:])))
                # Per-room cycle ceiling: at most 3 * (1 + window / interval).
                for start in times:
                    self.assertLessEqual(sum(start <= t < start + 60 for t in times), 36)
                opener.failure = 429
                before = archive.state["cursor_sha256"]
                delay = worker.step()
                self.assertGreaterEqual(delay, 13)
                self.assertEqual(archive.state["cursor"], 20)
                self.assertEqual(archive.state["cursor_sha256"], before)
                self.assertEqual(archive.state["status"], "RETRY")
                # A second process/client obeys the shared 429 cooldown.
                other = HostBudget(shared, policy, clock, clock.clock, "test-boot")
                with other.request():
                    self.assertGreaterEqual(clock.clock() - opener.requests[-1][0], 13)
                clock.wait(delay)
                opener.growing = False
                worker.step()
                self.assertEqual(archive.verify()["cursor"], 21)

    def test_two_processes_share_total_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("fork")
            queue = context.Queue()
            children = [context.Process(target=process_requests, args=(directory, queue)) for _ in range(2)]
            for child in children:
                child.start()
            for child in children:
                child.join(10)
                if child.is_alive():
                    child.kill()
                    child.join()
                self.assertEqual(child.exitcode, 0)
            times = sorted(queue.get(timeout=1) for _ in range(6))
            queue.close()
            self.assertTrue(all(b - a >= 0.099 for a, b in zip(times, times[1:])), times)

    def test_429_at_every_snapshot_stage_keeps_cursor_then_recovers(self):
        for stage in (1, 2, 3):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory, \
                    tempfile.TemporaryDirectory() as shared:
                clock = VirtualTime()
                budget = HostBudget(shared, BudgetPolicy(120, 50, 10, 60), clock, clock.clock, "boot")
                client = ExportClient("test-room", budget)
                opener = ModelOpener(clock.clock)
                opener.growing = False
                opener.ring.grow(5)
                opener.failure, opener.failure_at = 429, stage
                client.tail_client._opener = opener
                with Archive(directory, "test-room", min_free_bytes=0) as archive:
                    FullCapture(archive, Ring(1, 1)).step()
                    worker = FullCapture(archive, client)
                    before = archive.state["cursor_sha256"]
                    delay = worker.step()
                    self.assertGreaterEqual(delay, 13)
                    self.assertEqual(archive.state["cursor"], 1)
                    self.assertEqual(archive.state["cursor_sha256"], before)
                    self.assertEqual(archive.state["shards"], 1)
                    clock.wait(delay)
                    worker.step()
                    self.assertEqual(archive.verify()["cursor"], 6)

    def test_budget_finalization_failure_cannot_mask_integrity_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = VirtualTime()
            budget = HostBudget(directory, BudgetPolicy(120, 50, 10, 60), clock, clock.clock, "boot")
            with budget.request():
                pass
            with patch.object(budget, "_save", side_effect=[None, GracefulStop("CAPTURE_BUDGET_IO_FAILURE")]):
                with self.assertRaisesRegex(ObserverError, "GENERATION_CHANGE"):
                    with budget.request():
                        raise ObserverError("GENERATION_CHANGE")

    def test_budget_restart_policy_mismatch_corruption_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = VirtualTime()
            policy = BudgetPolicy(120, 50, 10, 60)
            def budget(selected=policy):
                return HostBudget(directory, selected, clock, clock.clock, "boot")
            with budget().request():
                first = clock.clock()
            with budget().request():
                self.assertGreaterEqual(clock.clock() - first, 1)
            with self.assertRaisesRegex(GracefulStop, "MISMATCH"):
                with budget(BudgetPolicy(120, 50, 10, 30)).request():
                    self.fail("mismatched policy sent request")
            (Path(directory) / "budget.json").write_bytes(b"torn")
            with self.assertRaisesRegex(GracefulStop, "MISMATCH"):
                with budget().request():
                    self.fail("corrupt budget sent request")
            stop = threading.Event()
            stop.set()
            with self.assertRaisesRegex(GracefulStop, "STOP_REQUESTED"):
                with HostBudget(directory, policy, stop).request():
                    self.fail("stopped process sent request")

    def test_budget_rejects_missing_headroom_and_oversubscription(self):
        for values in ((600, 200, 0, 300), (600, 200, 100, 301), (float("nan"), 1, 1, 1),
                       (600, 0, 100, 200), (600, 200, 100, float("inf"))):
            with self.subTest(values=values), self.assertRaises(GracefulStop):
                BudgetPolicy(*values)
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["run", "--room", "test-room", "--archive-dir", directory]), 2)
            self.assertEqual(json.loads(output.getvalue())["error"], "CAPTURE_READ_BUDGET_REQUIRED")

    def test_storage_preflight_stops_without_touching_checkpoint_then_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            with Archive(directory, "test-room", min_free_bytes=0) as archive:
                ring = Ring(1, 5)
                worker = FullCapture(archive, ring)
                worker.step()
                checkpoint = (Path(directory) / "capture.json").read_bytes()
                ring.grow(5)
                with patch.object(archive, "capacity", side_effect=ObserverError("CAPTURE_STORAGE_LOW")):
                    with self.assertRaises(GracefulStop):
                        worker.step()
                self.assertEqual((Path(directory) / "capture.json").read_bytes(), checkpoint)
                self.assertEqual(ring.calls, 1)
            with Archive(directory, "test-room", min_free_bytes=0) as archive:
                FullCapture(archive, ring).step()
                self.assertEqual(archive.verify()["cursor"], 10)

    def test_enospc_after_publish_keeps_recoverable_successor(self):
        with tempfile.TemporaryDirectory() as directory:
            def checkpoint(point):
                if point == "shard_published":
                    raise OSError(errno.ENOSPC, "synthetic")
            with Archive(directory, "test-room", min_free_bytes=0, checkpoint=checkpoint) as archive:
                with self.assertRaisesRegex(GracefulStop, "STORAGE_EXHAUSTED"):
                    FullCapture(archive, Ring(1, 5)).step()
                self.assertEqual(archive.state["shards"], 0)
            with Archive(directory, "test-room", min_free_bytes=0) as archive:
                self.assertEqual(archive.verify()["cursor"], 5)

    def test_malformed_tail_retries_but_malformed_snapshot_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            with Archive(directory, "test-room", min_free_bytes=0) as archive:
                FullCapture(archive, Ring(1, 5)).step()
                client = ExportClient("test-room")
                client.tail_client.tail = Mock(return_value=Reply(200, "application/json", b"{"))
                worker = FullCapture(archive, client)
                self.assertGreaterEqual(worker.step(), 5)
                self.assertEqual(archive.state["error"], "TAIL_INVALID_JSON")
                self.assertEqual(archive.state["cursor"], 5)
                worker.client = Ring(1, 6)
                worker.step()
                self.assertEqual(archive.verify()["cursor"], 6)
                worker.client.lines[-1] = b"{\n"
                worker.client.tail = lambda: view(6)
                with self.assertRaises(ObserverError):
                    worker.step()
                self.assertEqual(archive.state["status"], "BLOCKED")

    def test_confirmed_empty_cursor_safe_unknown_or_newer_head_blocks(self):
        for remote in (5, 6, None):
            with self.subTest(remote=remote), tempfile.TemporaryDirectory() as directory:
                with Archive(directory, "test-room", min_free_bytes=0) as archive:
                    FullCapture(archive, Ring(1, 5)).step()
                    ring = Ring()
                    empty = {**view(0), "first_seq": remote, "last_seq": remote}
                    ring.tail = lambda: empty
                    worker = FullCapture(archive, ring)
                    if remote == 5:
                        worker.step()
                        self.assertEqual(archive.state["cursor"], 5)
                        worker.client = Ring(6, 7)
                        worker.step()
                        self.assertEqual(archive.verify()["cursor"], 7)
                    else:
                        with self.assertRaisesRegex(ObserverError, "EMPTY_SNAPSHOT_UNCONFIRMED"):
                            worker.step()
                        self.assertEqual(archive.state["status"], "BLOCKED")

    def test_probe_observes_metadata_no_live_claims_and_detects_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            ring = Ring(1, 5)
            ring.room = "test-room"
            result, state = observe(ring, Path(directory) / "fetch")
            self.assertEqual(result["canonical_tail_matches"], 1)
            self.assertEqual(result["observed_max_line_bytes"], len(line(5)))
            self.assertEqual(result["compaction_cause"], UNVERIFIED)
            ring.grow(10)
            ring.lines = ring.lines[-2:]
            with self.assertRaisesRegex(ObserverError, "GAP_REQUIRES_REVIEW"):
                observe(ring, Path(directory) / "fetch", state)

    def test_probe_six_get_limit_and_no_write_requests(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as shared:
            clock = VirtualTime()
            budget = HostBudget(shared, BudgetPolicy(120, 50, 10, 60), clock, clock.clock, "boot")
            opener = ModelOpener(clock.clock)
            with patch("technocore_full_capture.probe.configured_budget", return_value=budget), \
                    patch("technocore_observer.http.urllib.request.build_opener", return_value=opener), \
                    patch("technocore_full_capture.probe.threading.Event", return_value=clock), \
                    patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(probe_main(["--room", "test-room", "--scratch-dir", directory]), 0)
            report = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(report["requests"], 6)
            self.assertEqual(report["write_requests"], 0)
            self.assertEqual(len(opener.requests), 6)

    def test_old_corruption_verify_is_persistent_review_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            with Archive(directory, "test-room", min_free_bytes=0) as archive:
                ring = Ring(1, 1)
                worker = FullCapture(archive, ring)
                worker.step()
                ring.grow(1)
                worker.step()
                old = archive.name(1)
                old.write_bytes(old.read_bytes().replace(b"message", b"MESSAGE"))
                with self.assertRaises(ObserverError):
                    archive.verify()
            with Archive(directory, "test-room", min_free_bytes=0) as archive:
                with self.assertRaisesRegex(ObserverError, "REVIEW_REQUIRED"):
                    FullCapture(archive, ring).step()

    def test_container_check_rejects_weakened_isolation(self):
        import ast
        import copy
        ast.parse(PAYLOAD)
        config = {"Config": {"User": "1000:1000"}, "HostConfig": {
            "ReadonlyRootfs": True, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
            "NetworkMode": "none", "RestartPolicy": {"Name": "no"}, "Privileged": False},
            "Mounts": [{"Type": "bind", "Source": "/dedicated", "Destination": "/capture", "RW": True}]}
        self.assertTrue(all(configuration_checks(config, Path("/dedicated"), "1000:1000").values()))
        for field, value in (("ReadonlyRootfs", False), ("CapDrop", []), ("SecurityOpt", []),
                             ("NetworkMode", "host"), ("RestartPolicy", {"Name": "always"}),
                             ("Privileged", True)):
            bad = copy.deepcopy(config)
            bad["HostConfig"][field] = value
            self.assertFalse(all(configuration_checks(bad, Path("/dedicated"), "1000:1000").values()))
        bad = copy.deepcopy(config)
        bad["Mounts"].append({"Type": "bind", "Source": "/", "Destination": "/host", "RW": True})
        self.assertFalse(all(configuration_checks(bad, Path("/dedicated"), "1000:1000").values()))


def benchmark():
    results = []
    with tempfile.TemporaryDirectory() as directory:
        previous = 0
        for count in (32, 256, 1024):
            with Archive(directory, "test-room", min_free_bytes=0) as archive:
                for seq in range(previous + 1, count + 1):
                    archive.append([line(seq)], 1)
            with Archive(directory, "test-room", min_free_bytes=0) as archive:
                verified = archive.verify()
                metrics = archive.metrics()
                results.append({"shards": count, "startup_seconds": metrics["startup_seconds"],
                                "verification_seconds": verified["verification_seconds"],
                                "mean_shard_payload_bytes": metrics["mean_shard_payload_bytes"],
                                "archive_bytes": metrics["archive_bytes"],
                                "allocated_shard_bytes": sum(archive.name(i).stat().st_blocks * 512
                                                             for i in range(1, count + 1))})
            previous = count
    print(json.dumps({"scope": "local synthetic warm-cache, one small record per shard; not production forecast",
                      "measurements": results}, indent=2))


if __name__ == "__main__":
    if sys.argv[1:] == ["--benchmark"]:
        benchmark()
    else:
        unittest.main()
