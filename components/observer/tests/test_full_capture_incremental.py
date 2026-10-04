"""Offline incremental conformance: latest-N selection is NOT forward paging.

Model selection follows pinned store.py: reverse scan, exclusive since, limit,
then reverse output. A >200 pending backlog must fail, not be relabelled a pass
for catch-up. No fixture invents an oldest-200 upstream interface.
"""

import io
import json
import os
from pathlib import Path
import signal
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_full_capture import Archive, CTX, Response, line
from test_full_capture_review import VirtualTime
from technocore_full_capture.__main__ import FullCapture, ReadClient, main
from technocore_full_capture.pacing import BudgetPolicy, HostBudget
from technocore_observer.protocol import ObserverError
from full_capture_container_check import build_context


class LatestWindow:
    def __init__(self, count=0, text="message"):
        self.records = [json.loads(line(i, text)) for i in range(1, count + 1)]
        self.generation = 1 if count else 0
        self.requests = []
        self.before_page = self.after_page = None
        self.transform = None
        self.fail_at = None
        self.failure = 503
        self.clock = lambda: 0

    def grow(self, count, text="message"):
        last = self.records[-1]["seq"] if self.records else 0
        self.records.extend(json.loads(line(i, text)) for i in range(last + 1, last + count + 1))
        self.generation = max(1, self.generation)

    def open(self, request, timeout):
        self.requests.append((self.clock(), request.full_url))
        assert request.get_method() == "GET" and request.data is None
        url = urlsplit(request.full_url)
        assert url.scheme == "https" and url.netloc == "technocore.chat"
        assert url.path == "/r/test-room"  # export is forbidden in this model
        if len(self.requests) == self.fail_at:
            if isinstance(self.failure, Exception):
                raise self.failure
            return Response(b"untrusted error", self.failure, **{"Retry-After": "13"})
        query = parse_qs(url.query)
        since = int(query["since"][0]) if "since" in query else None
        if since is not None:
            assert int(query["limit"][0]) == 200 and query["wait"] == ["10"]
            if self.before_page:
                self.before_page()
        selected = []
        for message in reversed(self.records):
            if since is not None and message["seq"] <= since:
                break
            selected.append(message.copy())
            if len(selected) >= int(query["limit"][0]):
                break
        selected.reverse()
        view = {"room": "test-room", "generation": self.generation,
                "messages": selected, "count": len(selected),
                "first_seq": selected[0]["seq"] if selected else None,
                "last_seq": selected[-1]["seq"] if selected else (since or 0)}
        if since is not None:
            if self.transform:
                view = self.transform(view)
            if self.after_page:
                self.after_page()
        return Response(json.dumps(view).encode(), **{"Content-Type": "application/json"})


def client_for(model, budget=None):
    client = ReadClient("test-room", budget)
    client.tail_client._opener = model
    return client


def kill_incremental(directory, point):
    def checkpoint(observed):
        if observed == point:
            os.kill(os.getpid(), signal.SIGKILL)
    with Archive(directory, "test-room", min_free_bytes=0, checkpoint=checkpoint) as archive:
        FullCapture(archive, client_for(LatestWindow(200))).step()


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = self.temp.name

    def archive(self, **kwargs):
        return Archive(self.directory, "test-room", min_free_bytes=0, **kwargs)

    def test_backlog_under_and_exactly_200(self):
        for count in (1, 199, 200):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                model = LatestWindow(count)
                with Archive(directory, "test-room", min_free_bytes=0) as archive:
                    worker = FullCapture(archive, client_for(model))
                    self.assertEqual(worker.step(), 5)
                    self.assertEqual(archive.verify()["message_count"], count)
                    self.assertEqual(archive.state["anchor_seq"], 0)
                    self.assertEqual(worker.step(), 5)
                    self.assertEqual(archive.state["shards"], 1)
                self.assertEqual(len(model.requests), 6)

    def test_pending_backlog_over_200_cannot_page_on_official_contract(self):
        for backlog in (201, 800):
            with self.subTest(backlog=backlog), tempfile.TemporaryDirectory() as directory:
                model = LatestWindow(1)
                with Archive(directory, "test-room", min_free_bytes=0) as archive:
                    worker = FullCapture(archive, client_for(model))
                    worker.step()
                    before = archive.state.copy()
                    model.grow(backlog)
                    with self.assertRaisesRegex(ObserverError, "GAP_REQUIRES_REVIEW"):
                        worker.step()
                    self.assertEqual(archive.state["cursor"], 1)
                    self.assertEqual(archive.state["tip_sha256"], before["tip_sha256"])
                    self.assertEqual(archive.state["status"], "BLOCKED")
                    with self.assertRaisesRegex(ObserverError, "CAPTURE_REVIEW_REQUIRED"):
                        worker.step()
                    self.assertEqual(len(model.requests), 6)

    def test_new_archive_does_not_silently_anchor_at_newest_window(self):
        with self.archive() as archive:
            with self.assertRaisesRegex(ObserverError, "GAP_REQUIRES_REVIEW"):
                FullCapture(archive, client_for(LatestWindow(201))).step()
            self.assertIsNone(archive.state["cursor"])
            self.assertEqual(archive.state["shards"], 0)

    def test_multiple_reads_and_arrival_during_fetch(self):
        model = LatestWindow(150)
        model.before_page = lambda: model.grow(20)
        model.after_page = lambda: model.grow(100)
        with self.archive() as archive:
            worker = FullCapture(archive, client_for(model))
            self.assertEqual(worker.step(), 5)
            self.assertEqual(archive.state["cursor"], 170)
            model.before_page = model.after_page = None
            worker.step()
            self.assertEqual(archive.state["cursor"], 270)
            for _ in range(4):
                model.grow(200)
                self.assertEqual(worker.step(), 5)
            self.assertEqual(archive.verify()["message_count"], 1070)

    def test_burst_during_fetch_exceeds_window_blocks(self):
        model = LatestWindow(1)
        with self.archive() as archive:
            worker = FullCapture(archive, client_for(model))
            worker.step()
            model.before_page = lambda: model.grow(201)
            with self.assertRaisesRegex(ObserverError, "GAP_REQUIRES_REVIEW"):
                worker.step()
            self.assertEqual(archive.state["cursor"], 1)

    def test_compaction_internal_hole_duplicate_and_generation(self):
        for kind, error in (("compaction", "GAP_REQUIRES_REVIEW"),
                            ("hole", "INTERNAL_SEQ_HOLE"),
                            ("duplicate", "PAGE_DUPLICATE_BOUNDARY"),
                            ("generation", "GENERATION_CHANGE"),
                            ("generation_during", "GENERATION_CHANGE")):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                model = LatestWindow(10)
                with Archive(directory, "test-room", min_free_bytes=0) as archive:
                    worker = FullCapture(archive, client_for(model))
                    worker.step()
                    model.grow(10)
                    if kind == "compaction":
                        model.records = model.records[15:]
                    elif kind == "hole":
                        model.records = [m for m in model.records if m["seq"] != 15]
                    elif kind == "generation":
                        model.generation = 2
                    elif kind == "generation_during":
                        model.after_page = lambda: setattr(model, "generation", 2)
                    else:
                        def duplicate(view):
                            view["messages"].insert(0, model.records[9])
                            view.update(first_seq=10, count=11)
                            return view
                        model.transform = duplicate
                    with self.assertRaisesRegex(ObserverError, error):
                        worker.step()
                    self.assertEqual(archive.state["cursor"], 10)

    def test_empty_echo_is_not_evidence_of_no_loss(self):
        model = LatestWindow(10)
        with self.archive() as archive:
            worker = FullCapture(archive, client_for(model))
            worker.step()
            model.records = []  # all records expired; poll still echoes since=10
            with self.assertRaisesRegex(ObserverError, "EMPTY_PAGE_UNCONFIRMED"):
                worker.step()
            self.assertEqual(archive.state["cursor"], 10)

    def test_never_created_waits_but_reaped_start_is_unconfirmed(self):
        model = LatestWindow()
        with self.archive() as archive:
            worker = FullCapture(archive, client_for(model))
            worker.step()
            self.assertEqual(archive.state["status"], "WAITING")
            model.generation = 1
            with self.assertRaisesRegex(ObserverError, "EMPTY_PAGE_UNCONFIRMED"):
                worker.step()

    def test_retry_at_each_read_stage_preserves_cursor_and_archive(self):
        for failure in (429, 503, TimeoutError("no remote text")):
            for stage in (1, 2, 3):
                with self.subTest(failure=failure, stage=stage), tempfile.TemporaryDirectory() as directory:
                    model = LatestWindow(10)
                    with Archive(directory, "test-room", min_free_bytes=0) as archive:
                        worker = FullCapture(archive, client_for(model))
                        worker.step()
                        before = archive.state.copy()
                        model.grow(10)
                        model.fail_at, model.failure = 3 + stage, failure
                        self.assertGreaterEqual(worker.step(), 5)
                        for key in ("cursor", "tip_sha256", "shards", "message_count"):
                            self.assertEqual(archive.state[key], before[key])
                        self.assertEqual(archive.state["status"], "RETRY")
                        model.fail_at = None
                        worker.step()
                        self.assertEqual(archive.verify()["cursor"], 20)

    def test_real_incremental_client_shared_budget_and_positive_cycle_floor(self):
        with tempfile.TemporaryDirectory() as shared, self.archive() as archive:
            clock = VirtualTime()
            policy = BudgetPolicy(600, 120, 360, 120)
            budget = HostBudget(shared, policy, clock, clock.clock, "boot")
            model = LatestWindow(1)
            model.clock = clock.clock
            model.after_page = lambda: model.grow(1)
            worker = FullCapture(archive, client_for(model, budget))
            for _ in range(20):
                delay = worker.step()
                self.assertEqual(delay, 5)
                clock.wait(delay)
            self.assertEqual(archive.state["cursor"], 20)
            times = [t for t, url in model.requests]
            self.assertEqual(budget.requests, 60)
            self.assertTrue(all(b - a >= 0.5 for a, b in zip(times, times[1:])))
            model.failure, model.fail_at = 429, 61
            self.assertGreaterEqual(worker.step(), 13)
            other_model = LatestWindow(1)
            other_model.clock = clock.clock
            other_budget = HostBudget(shared, policy, clock, clock.clock, "boot")
            client_for(other_model, other_budget).tail()
            self.assertGreaterEqual(other_model.requests[0][0] - model.requests[-1][0], 13)

    def test_crash_publication_before_cursor_and_recovery(self):
        for point in ("page_validated", "shard_fsynced", "shard_published",
                      "shard_directory_fsynced", "before_checkpoint", "after_checkpoint"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                process = CTX.Process(target=kill_incremental, args=(directory, point))
                process.start()
                process.join(15)
                if process.is_alive():
                    process.kill()
                    process.join()
                    self.fail("crash point not reached")
                self.assertEqual(process.exitcode, -signal.SIGKILL)
                with Archive(directory, "test-room", min_free_bytes=0) as archive:
                    FullCapture(archive, client_for(LatestWindow(200))).step()
                    self.assertEqual(archive.verify()["message_count"], 200)
                    self.assertEqual(archive.state["shards"], 1)

    def test_long_term_over_16_mib_and_old_raw_archive_resume(self):
        model = LatestWindow(1)
        with self.archive(shard_bytes=1024 * 1024) as archive:
            # Existing byte-exact shard can have different JSON whitespace.
            archive.append([b'{ "seq": 1, "text": "message", "from": "untrusted", "ts": "2026-09-19" }\n'], 1)
            worker = FullCapture(archive, client_for(model))
            for _ in range(25):
                model.grow(200, "x" * 4096)
                model.records = model.records[-200:]
                worker.step()
            result = archive.verify()
            self.assertEqual(result["message_count"], 5001)
            self.assertGreater(result["payload_bytes"], 16 * 1024 * 1024)
        with self.archive() as archive:
            self.assertEqual(archive.verify()["cursor"], 5001)

    def test_untrusted_values_are_data_and_malformed_page_never_partly_publishes(self):
        model = LatestWindow(2)
        model.records[0].update(text="$(touch pwned)", room="evil", generation=99,
                                url="https://evil.test", archive_dir="/elsewhere")
        with self.archive() as archive:
            FullCapture(archive, client_for(model)).step()
            self.assertEqual(archive.state["generation"], 1)
            with archive.name(1).open("rb") as shard:
                shard.readline()
                self.assertEqual(json.loads(shard.readline()), model.records[0])
            model.grow(2)
            model.records[-1]["seq"] = 5
            with self.assertRaisesRegex(ObserverError, "INTERNAL_SEQ_HOLE"):
                FullCapture(archive, client_for(model)).step()
            self.assertEqual(archive.state["cursor"], 2)

    def test_cli_run_constructs_only_read_client(self):
        class Done(Exception):
            pass
        args = ["run", "--room", "test-room", "--archive-dir", self.directory]
        with patch("technocore_full_capture.__main__.configured_budget"), \
                patch("technocore_full_capture.__main__.ReadClient", side_effect=Done), \
                patch("technocore_full_capture.__main__.ExportClient", side_effect=AssertionError("export")):
            with self.assertRaises(Done):
                main(args)

    def test_container_context_has_all_copy_inputs_without_root_ignore_or_secrets(self):
        with tarfile.open(fileobj=io.BytesIO(build_context()), mode="r") as context:
            names = context.getnames()
            self.assertEqual(len(names), 14)
            self.assertTrue(all(m.isfile() for m in context.getmembers()))
            self.assertIn("src/technocore_full_capture/__main__.py", names)
            self.assertIn("src/technocore_full_capture/pacing.py", names)
            self.assertIn("src/technocore_full_capture/capture_first.py", names)
            self.assertNotIn(".dockerignore", names)
            self.assertTrue(all(n == "Dockerfile" or n.startswith("src/") for n in names))
            for row in context.extractfile("Dockerfile").read().decode().splitlines():
                if row.startswith("COPY "):
                    for source in row.split()[1:-1]:
                        self.assertTrue(source in names or any(n.startswith(source) for n in names))


if __name__ == "__main__":
    unittest.main()
