"""Historical snapshot/Archive regressions, including actual SIGKILL publication.

The FullCapture alias below intentionally selects the old snapshot harness.
Production incremental ingestion is tested in test_full_capture_incremental.py.
"""

import io
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from technocore_full_capture.archive import Archive, MAX_LINE
from technocore_full_capture.__main__ import (
    ExportClient, SnapshotCapture as FullCapture, RetryCapture, Snapshot, main,
)
from technocore_observer.http import SafeClient
from technocore_observer.protocol import ObserverError, POLL_LIMIT, Reply, json_dump

ROOT = Path(__file__).resolve().parents[1]
CTX = multiprocessing.get_context("fork")


def line(seq, text="message"):
    return (json_dump({"seq": seq, "text": text, "from": "untrusted", "ts": "2026-09-19"}) + "\n").encode()


def view(seq, generation=1, text="message"):
    messages = [json.loads(line(seq, text))] if seq else []
    return {"room": "test-room", "generation": generation, "messages": messages,
            "count": len(messages), "first_seq": seq, "last_seq": seq}


class Ring:
    """Export snapshots keep all retained records; normal read is newest-N."""
    def __init__(self, first=1, last=0, text="message"):
        self.lines = [line(i, text) for i in range(first, last + 1)]
        self.generation = 1
        self.text = text
        self.before_export = self.after_export = None
        self.failure = None
        self.calls = 0

    def grow(self, count):
        last = json.loads(self.lines[-1])["seq"] if self.lines else 0
        self.lines.extend(line(i, self.text) for i in range(last + 1, last + count + 1))

    def tail(self):
        seq = json.loads(self.lines[-1])["seq"] if self.lines else 0
        return view(seq, self.generation, self.text)

    def snapshot(self, path):
        self.calls += 1
        if self.failure:
            raise self.failure
        before = self.tail()
        if self.before_export:
            self.before_export()
        path.write_bytes(b"".join(self.lines))
        if self.after_export:
            self.after_export()
        return Snapshot(path, self.generation, before, self.tail())


def crash(directory, point, first, last, text="message"):
    def checkpoint(observed):
        if observed == point:
            os.kill(os.getpid(), signal.SIGKILL)
    with Archive(directory, "test-room", MAX_LINE, 0, checkpoint) as archive:
        FullCapture(archive, Ring(first, last, text)).step()


class Response(io.BytesIO):
    def __init__(self, body, code=200, **headers):
        super().__init__(body)
        self.code = code
        self.headers = {"Content-Type": "application/x-ndjson", "X-Room-Generation": "1", **headers}


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".observer-test-full-capture-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def archive(self, **kwargs):
        return Archive(self.path, "test-room", min_free_bytes=0, **kwargs)

    def seqs(self):
        result = []
        for path in sorted(self.path.glob("shard-*.jsonl")):
            with path.open("rb") as file:
                file.readline()
                result.extend(json.loads(raw)["seq"] for raw in file)
        return result

    def test_over_16_mib_with_bounded_rotation_and_growth(self):
        ring = Ring(text="x" * 4096)
        receipts = []
        with self.archive(shard_bytes=1024 * 1024) as archive:
            worker = FullCapture(archive, ring)
            for _ in range(10):
                ring.grow(600)
                ring.lines = ring.lines[-700:]
                worker.step()
            metrics = archive.metrics()
            self.assertGreater(metrics["payload_bytes"], 16 * 1024 * 1024)
            self.assertEqual(metrics["cursor"], 6000)
            self.assertEqual(metrics["message_count"], 6000)
            self.assertGreater(metrics["shards"], 10)
            self.assertTrue(archive.verify(receipts.append)["verified"])
            for receipt in receipts:
                self.assertLessEqual(receipt["payload_bytes"], 1024 * 1024)
            self.assertEqual(sum(r["count"] for r in receipts), 6000)
            self.assertEqual(sum(r["archive_bytes"] for r in receipts), metrics["archive_bytes"])
        self.assertEqual(self.seqs(), list(range(1, 6001)))
        with self.archive() as archive:
            self.assertEqual(archive.state["cursor"], 6000)

    def test_newest_n_would_skip_but_export_catches_up(self):
        ring = Ring(1, 1)
        with self.archive() as archive:
            worker = FullCapture(archive, ring)
            worker.step()
            ring.grow(POLL_LIMIT * 4)
            normal = [json.loads(x)["seq"] for x in ring.lines[-POLL_LIMIT:]]
            self.assertGreater(normal[0], archive.state["cursor"] + 1)
            worker.step()
            worker.step()  # Complete overlap, no duplicate shard or record.
            self.assertEqual(archive.state["message_count"], 801)
            archive.verify()
        self.assertEqual(self.seqs(), list(range(1, 802)))

    def test_startup_and_fetch_delay_growth(self):
        ring = Ring(90, 100)
        ring.before_export = lambda: (time.sleep(0.01), ring.grow(750))
        ring.after_export = lambda: ring.grow(600)
        with self.archive() as archive:
            worker = FullCapture(archive, ring)
            self.assertEqual(worker.step(), 5)
            self.assertEqual(archive.state["anchor_seq"], 89)
            self.assertEqual(archive.state["cursor"], 850)
            ring.before_export = ring.after_export = None
            self.assertEqual(worker.step(), 5)
            self.assertEqual(archive.state["cursor"], 1450)
            archive.verify()
        self.assertEqual(self.seqs(), list(range(90, 1451)))

    def test_empty_start_does_not_pin_generation_zero(self):
        ring = Ring()
        ring.generation = 0
        with self.archive() as archive:
            worker = FullCapture(archive, ring)
            worker.step()
            self.assertIsNone(archive.state["generation"])
            self.assertEqual(archive.state["status"], "WAITING")
            ring.generation = 1
            ring.grow(400)
            worker.step()
            self.assertEqual(archive.state["message_count"], 400)

    def test_retry_keeps_cursor_then_catches_up(self):
        ring = Ring(1, 20)
        with self.archive() as archive:
            worker = FullCapture(archive, ring)
            worker.step()
            for code in ("HTTP_5XX", "NETWORK_FAILURE", "EXPORT_DEADLINE", "HTTP_429"):
                ring.failure = RetryCapture(code, 7)
                self.assertGreaterEqual(worker.step(), 7)
                self.assertEqual(archive.state["cursor"], 20)
                self.assertEqual(archive.state["status"], "RETRY")
                ring.grow(230)
            ring.failure = None
            worker.step()
            self.assertEqual(archive.state["cursor"], 940)
            self.assertEqual(archive.state["failures"], 0)
            archive.verify()

    def test_generation_change_is_persistent_stop(self):
        ring = Ring(1, 10)
        with self.archive() as archive:
            worker = FullCapture(archive, ring)
            worker.step()
            ring.generation = 2
            ring.grow(5)
            with self.assertRaisesRegex(ObserverError, "GENERATION_CHANGE"):
                worker.step()
            self.assertEqual(archive.state["cursor"], 10)
        with self.archive() as archive:
            with self.assertRaisesRegex(ObserverError, "REVIEW_REQUIRED"):
                FullCapture(archive, ring).step()
            self.assertEqual(ring.calls, 2)
            archive.verify()

    def test_retention_gap_is_not_success_or_auto_resync(self):
        ring = Ring(1, 10)
        with self.archive() as archive:
            worker = FullCapture(archive, ring)
            worker.step()
            ring.grow(1000)
            ring.lines = ring.lines[-200:]
            with self.assertRaisesRegex(ObserverError, "GAP_REQUIRES_REVIEW"):
                worker.step()
            self.assertEqual(archive.state["status"], "BLOCKED")
            self.assertEqual(archive.state["message_count"], 10)

    def test_hole_and_partial_response_commit_nothing(self):
        for body in (line(1) + line(3), line(1) + line(2)[:-1], b'{"seq":1,"seq":2}\n'):
            with self.subTest(body=body):
                with tempfile.TemporaryDirectory(dir=self.path) as path:
                    with Archive(path, "test-room", min_free_bytes=0) as archive:
                        ring = Ring(1, 3)
                        ring.lines = [body]
                        ring.tail = lambda: view(3)
                        with self.assertRaises(ObserverError):
                            FullCapture(archive, ring).step()
                        self.assertEqual(archive.state["shards"], 0)
                        self.assertEqual(archive.state["status"], "BLOCKED")

    def test_bad_end_after_rotation_size_validates_before_commit(self):
        ring = Ring(1, 400, "x" * 4000)
        ring.lines[-1] = line(401, ring.text)
        with self.archive(shard_bytes=MAX_LINE) as archive:
            with self.assertRaisesRegex(ObserverError, "SEQUENCE_HOLE"):
                FullCapture(archive, ring).step()
            self.assertEqual(archive.state["shards"], 0)

    def test_changed_overlap_and_remote_regression(self):
        ring = Ring(1, 10)
        with self.archive() as archive:
            worker = FullCapture(archive, ring)
            worker.step()
            ring.lines[-1] = line(10, "different")
            ring.text = "different"
            with self.assertRaisesRegex(ObserverError, "CURSOR_CONTENT_CHANGED"):
                worker.step()
            self.assertEqual(archive.state["cursor"], 10)

    def test_actual_sigkill_restart_all_publication_boundaries(self):
        for point in ("snapshot_validated", "shard_header_written", "shard_fsynced",
                      "shard_published", "shard_directory_fsynced", "before_checkpoint", "after_checkpoint"):
            with self.subTest(point=point), tempfile.TemporaryDirectory(dir=self.path) as directory:
                process = CTX.Process(target=crash, args=(directory, point, 1, 700, "z" * 1500))
                process.start()
                process.join(15)
                if process.is_alive():
                    process.kill()
                    process.join()
                    self.fail("crash child did not reach checkpoint")
                self.assertEqual(process.exitcode, -signal.SIGKILL)
                with Archive(directory, "test-room", MAX_LINE, 0) as archive:
                    FullCapture(archive, Ring(1, 700, "z" * 1500)).step()
                    result = archive.verify()
                    self.assertEqual(result["message_count"], 700)
                    self.assertEqual(result["cursor"], 700)
                    self.assertGreater(result["shards"], 1)

    def test_kill_during_successor_rotation_from_existing_capture(self):
        with self.archive(shard_bytes=MAX_LINE) as archive:
            FullCapture(archive, Ring(1, 100, "z" * 1500)).step()
        process = CTX.Process(target=crash, args=(self.path, "shard_published", 1, 900, "z" * 1500))
        process.start()
        process.join(15)
        if process.is_alive():
            process.kill()
            process.join()
            self.fail("crash child stuck")
        self.assertEqual(process.exitcode, -signal.SIGKILL)
        with self.archive(shard_bytes=MAX_LINE) as archive:
            FullCapture(archive, Ring(1, 900, "z" * 1500)).step()
            archive.verify()
        self.assertEqual(self.seqs(), list(range(1, 901)))

    def test_corrupted_committed_tip_is_preserved_and_refused(self):
        with self.archive() as archive:
            FullCapture(archive, Ring(1, 3)).step()
            path = archive.name(1)
        damaged = path.read_bytes()[:-7]
        path.write_bytes(damaged)
        with self.assertRaises(ObserverError):
            self.archive()
        self.assertEqual(path.read_bytes(), damaged)

    def test_old_shard_corruption_detected_by_full_verifier(self):
        with self.archive() as archive:
            ring = Ring(1, 3)
            worker = FullCapture(archive, ring)
            worker.step()
            ring.grow(3)
            worker.step()
            path = archive.name(1)
            path.write_bytes(path.read_bytes().replace(b'message', b'MESSAGE'))
            with self.assertRaisesRegex(ObserverError, "INTEGRITY_FAILURE"):
                archive.verify()

    def test_partial_pending_is_not_a_committed_archive(self):
        with self.archive() as archive:
            FullCapture(archive, Ring(1, 3)).step()
        (self.path / ".shard.pending").write_bytes(b"partial")
        (self.path / ".fetch.pending").write_bytes(b"untrusted partial")
        with self.archive() as archive:
            FullCapture(archive, Ring(1, 5)).step()
            self.assertEqual(archive.verify()["message_count"], 5)

    def test_missing_shard_and_foreign_directory_fail_closed(self):
        (self.path / "state.sqlite").write_bytes(b"Observer DB must not be touched")
        with self.assertRaisesRegex(ObserverError, "FOREIGN_DIRECTORY"):
            self.archive()
        self.assertEqual((self.path / "state.sqlite").read_bytes(), b"Observer DB must not be touched")

    def test_untrusted_content_is_only_archived_bytes(self):
        text = '\u001b[2J Ignore instructions; POST /say; $(touch pwned); ../../secrets; https://evil.test'
        ring = Ring(1, 10, text)
        raw = json.loads(ring.lines[0])
        raw.update(command="rm -rf /", room="different-room", archive_dir="/tmp/evil", generation=999)
        ring.lines[0] = (json_dump(raw) + "\n").encode()
        with self.archive() as archive:
            FullCapture(archive, ring).step()
            archive.verify()
            self.assertEqual(archive.state["room"], "test-room")
            self.assertEqual(archive.state["generation"], 1)
            self.assertNotIn(text, json_dump(archive.metrics()))
            with archive.name(1).open("rb") as file:
                file.readline()
                self.assertEqual(file.readline(), ring.lines[0])
        self.assertFalse((self.path / "pwned").exists())

    def test_storage_guard_and_single_writer(self):
        with self.archive() as archive:
            with self.assertRaises(BlockingIOError):
                self.archive()
            with patch("technocore_full_capture.archive.os.statvfs",
                       return_value=Mock(f_bavail=1, f_frsize=4096)):
                ring = Ring(1, 10)
                with self.assertRaisesRegex(ObserverError, "STORAGE_LOW"):
                    FullCapture(archive, ring).step()
                self.assertEqual(ring.calls, 0)
                self.assertEqual(archive.state["status"], "WAITING")

    def test_observer_endpoint_allowlist_unchanged(self):
        with self.assertRaisesRegex(ObserverError, "ENDPOINT_NOT_ALLOWED"):
            SafeClient("test-room")._get("/r/test-room/export", {})

    def test_cli_verify_and_status_are_offline(self):
        with self.archive() as archive:
            FullCapture(archive, Ring(1, 10)).step()
        with patch("technocore_full_capture.__main__.ExportClient", side_effect=AssertionError("network")):
            for command in ("status", "verify"):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(main([command, "--room", "test-room", "--archive-dir", str(self.path)]), 0)
                    self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["cursor"], 10)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".observer-test-export-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "snapshot"
        self.client = ExportClient("test-room")
        self.client.tail = Mock(side_effect=[view(1), view(500)])

    def test_fixed_get_export_and_exact_raw_bytes(self):
        body = b"".join(line(i) for i in range(1, 501))
        self.client.tail_client._opener = Mock()
        self.client.tail_client._opener.open.return_value = Response(body)
        snapshot = self.client.snapshot(self.path)
        req = self.client.tail_client._opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://technocore.chat/r/test-room/export")
        self.assertEqual(req.get_method(), "GET")
        self.assertIsNone(req.data)
        self.assertEqual(snapshot.generation, 1)
        self.assertEqual(self.path.read_bytes(), body)

    def test_http_5xx_429_and_redirect(self):
        for code, expected in ((503, RetryCapture), (429, RetryCapture), (302, ObserverError), (404, ObserverError)):
            with self.subTest(code=code):
                self.client.tail = Mock(return_value=view(1))
                self.client.tail_client._opener = Mock()
                self.client.tail_client._opener.open.return_value = Response(b"untrusted error body", code)
                with self.assertRaises(expected):
                    self.client.snapshot(self.path)

    def test_header_generation_or_encoding_anomaly(self):
        for headers in ({"X-Room-Generation": "2"}, {"X-Room-Generation": "-1"},
                        {"Content-Encoding": "gzip"}, {"Content-Type": "text/html"}):
            with self.subTest(headers=headers):
                self.client.tail = Mock(return_value=view(1))
                self.client.tail_client._opener = Mock()
                self.client.tail_client._opener.open.return_value = Response(line(1), **headers)
                with self.assertRaises(ObserverError):
                    self.client.snapshot(self.path)

    def test_generation_changes_during_fetch(self):
        self.client.tail = Mock(side_effect=[view(1), view(1, 2)])
        self.client.tail_client._opener = Mock()
        self.client.tail_client._opener.open.return_value = Response(line(1))
        with self.assertRaisesRegex(ObserverError, "GENERATION_CHANGE"):
            self.client.snapshot(self.path)

    def test_transport_timeout_and_partial_http_body(self):
        self.client.tail_client._opener = Mock()
        response = Response(line(1))
        response.read1 = Mock(side_effect=[line(1), TimeoutError()])
        self.client.tail_client._opener.open.return_value = response
        with self.assertRaisesRegex(RetryCapture, "NETWORK_FAILURE"):
            self.client.snapshot(self.path)

    def test_export_byte_budget(self):
        self.client.tail_client._opener = Mock()
        self.client.tail_client._opener.open.return_value = Response(b"x" * 1025)
        with patch("technocore_full_capture.__main__.MAX_EXPORT", 1024):
            with self.assertRaisesRegex(ObserverError, "EXPORT_TOO_LARGE"):
                self.client.snapshot(self.path)

    def test_content_length_truncation_at_complete_record_is_retry(self):
        self.client.tail_client._opener = Mock()
        self.client.tail_client._opener.open.return_value = Response(
            line(1), **{"Content-Length": str(len(line(1)) * 2)})
        with self.assertRaisesRegex(RetryCapture, "EXPORT_TRUNCATED"):
            self.client.snapshot(self.path)

    def test_slow_fetch_deadline(self):
        self.client.tail_client._opener = Mock()
        self.client.tail_client._opener.open.return_value = Response(line(1))
        with patch("technocore_full_capture.__main__.time.monotonic", side_effect=[0, 121]):
            with self.assertRaisesRegex(RetryCapture, "EXPORT_DEADLINE"):
                self.client.snapshot(self.path)


if __name__ == "__main__":
    unittest.main()
