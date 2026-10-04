"""Capture-first storage tests: only synthetic local data, real crash boundaries."""

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from technocore_full_capture.spool import Spool, LocalConsumer, canonical
from technocore_full_capture.spool_archive import ArchiveWorker
from technocore_observer.protocol import ObserverError

CTX = multiprocessing.get_context("fork")


def page(first=1, last=0, generation=1, since=0):
    messages = [{"seq": seq, "text": "message", "from": "untrusted"} for seq in range(first, last + 1)]
    return {"room": "test-room", "generation": generation, "count": len(messages), "messages": messages,
            "first_seq": first if messages else None, "last_seq": last if messages else since}


def producer(directory, **kwargs):
    return Spool(directory, "test-room", producer=True, create=True, min_free_bytes=0, **kwargs)


def kill_capture(directory, point):
    def checkpoint(observed):
        if observed == point:
            os.kill(os.getpid(), signal.SIGKILL)
    with producer(directory, checkpoint=checkpoint) as spool:
        spool.ingest(page(301, 500))


def kill_archive(directory, destination, point):
    def checkpoint(observed):
        if observed == point:
            os.kill(os.getpid(), signal.SIGKILL)
    with Spool(directory, "test-room", min_free_bytes=0) as spool:
        with ArchiveWorker(spool, destination, min_free_bytes=0, checkpoint=checkpoint) as worker:
            worker.step()


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "spool"
        self.archive = self.root / "archive"
        self.source.mkdir()
        self.archive.mkdir()

    def kinds(self, spool):
        return [row[0] for row in spool.conn.execute("SELECT kind FROM entries ORDER BY entry_id")]

    def test_latest_n_gap_future_and_bootstrap(self):
        with producer(self.source) as spool:
            spool.ingest(page(1, 100))
            result = spool.ingest(page(301, 500))
            self.assertEqual((result["saved"], result["cursor"], result["gaps"]), (200, 500, 1))
            row = spool.conn.execute("SELECT * FROM entries WHERE kind='GAP'").fetchone()
            self.assertEqual((row["seq"], row["end_seq"]), (101, 300))
            spool.ingest(page(501, 510))
            self.assertEqual(spool.state()["cursor"], 510)
            self.assertEqual(spool.state()["messages"], 310)
            with ArchiveWorker(spool, self.archive, min_free_bytes=0) as worker:
                while worker.step()["processed"]:
                    pass
                result = worker.verify()
                self.assertEqual(result["coverage_status"], "PARTIAL_WITH_GAPS")
                self.assertEqual(result["messages"], 310)
                self.assertEqual(worker.conn.execute("SELECT count(*) FROM segments").fetchone()[0], 2)

    def test_bootstrap_missing_prefix(self):
        with producer(self.source) as spool:
            spool.ingest(page(301, 500))
            gap = spool.conn.execute("SELECT * FROM entries WHERE kind='BOOTSTRAP_UNOBSERVED_PREFIX'").fetchone()
            self.assertEqual((gap["seq"], gap["end_seq"]), (1, 300))
            with ArchiveWorker(spool, self.archive, min_free_bytes=0) as worker:
                while worker.step()["processed"]:
                    pass
                self.assertEqual(worker.verify()["coverage_status"], "PARTIAL_WITH_GAPS")
                self.assertFalse(worker.coverage()["all_room_history_complete"])

    def test_duplicate_canonical_identity_conflict_and_late_observation(self):
        with producer(self.source) as spool:
            spool.ingest(page(1, 2))
            replay = page(1, 2)
            replay["messages"] = [dict(reversed(list(m.items()))) for m in replay["messages"]]
            self.assertEqual(spool.ingest(replay)["saved"], 0)
            replay["messages"][0]["text"] = "different"
            self.assertEqual(spool.ingest(replay)["conflicts"], 1)
            self.assertEqual(spool.state()["messages"], 2)
            self.assertIn("CONFLICT", self.kinds(spool))
            original = spool.conn.execute("SELECT payload FROM entries WHERE kind='MESSAGE' AND seq=1").fetchone()[0]
            self.assertEqual(json.loads(original)["text"], "message")
            spool.ingest(page(5, 6))
            spool.ingest(page(3, 4))
            self.assertEqual(spool.state()["gaps"], 1)
            self.assertEqual(spool.state()["cursor"], 6)
            self.assertIn("LATE_OBSERVATION", self.kinds(spool))
            with ArchiveWorker(spool, self.archive, min_free_bytes=0) as worker:
                worker.step()
                self.assertEqual(worker.verify()["coverage_status"], "PARTIAL_WITH_GAPS")

    def test_generation_boundary_preserves_page_and_rebases_next_read(self):
        with producer(self.source) as spool:
            spool.ingest(page(1, 100))
            result = spool.ingest(page(101, 105, generation=2))
            self.assertTrue(result["generation_changed"])
            self.assertEqual(spool.state()["cursor"], 0)
            self.assertEqual(spool.state()["epoch"], 2)
            uncertain = spool.conn.execute("SELECT payload FROM entries WHERE kind='BOUNDARY_OBSERVATION'").fetchone()[0]
            self.assertEqual(json.loads(uncertain)["messages"], page(101, 105, 2)["messages"])
            spool.ingest(page(1, 2, generation=2), request_epoch=2, request_since=0)
            self.assertEqual(spool.state()["cursor"], 2)
            with ArchiveWorker(spool, self.archive, min_free_bytes=0) as worker:
                worker.step()
                self.assertEqual(worker.verify()["coverage_status"], "UNCONFIRMED")
                self.assertEqual(worker.state()["messages"], 102)

    def test_empty_echo_never_advances_or_certifies_history(self):
        with producer(self.source) as spool:
            spool.ingest(page(since=999))
            self.assertEqual(spool.state()["cursor"], 0)
            with ArchiveWorker(spool, self.archive, min_free_bytes=0) as worker:
                worker.step()
                self.assertEqual(worker.verify()["coverage_status"], "UNCONFIRMED")
                spool.ingest(page(1, 3))
                worker.step()
                result = worker.verify()
                self.assertEqual(result["coverage_status"], "COMPLETE")
                self.assertEqual(result["remote_tail"], "UNKNOWN")
                self.assertFalse(result["all_room_history_complete"])
            spool.ingest(page(since=999))
            self.assertEqual(spool.state()["cursor"], 3)

    def test_process_kill_message_gap_cursor_transaction(self):
        for point in ("gap_inserted", "message_inserted", "before_cursor_update", "before_commit", "after_commit"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                with producer(directory) as spool:
                    spool.ingest(page(1, 100))
                child = CTX.Process(target=kill_capture, args=(directory, point))
                child.start()
                child.join(10)
                if child.is_alive():
                    child.kill()
                    child.join()
                self.assertEqual(child.exitcode, -signal.SIGKILL)
                with producer(directory) as spool:
                    committed = point == "after_commit"
                    self.assertEqual(spool.state()["cursor"], 500 if committed else 100)
                    self.assertEqual(spool.state()["messages"], 300 if committed else 100)
                    self.assertEqual(spool.state()["gaps"], int(committed))
                    spool.ingest(page(301, 500))
                    self.assertEqual(spool.state()["messages"], 300)
                    self.assertEqual(spool.state()["gaps"], 1)

    def test_sqlite_full_rolls_back_messages_gap_and_cursor(self):
        with producer(self.source) as spool:
            spool.ingest(page(1, 100))
            spool.conn.execute("""CREATE TEMP TRIGGER fail_cursor BEFORE UPDATE OF cursor ON state
                BEGIN SELECT RAISE(ABORT, 'synthetic SQLITE_FULL'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                spool.ingest(page(301, 500))
            self.assertEqual(spool.state()["cursor"], 100)
            self.assertEqual(spool.state()["messages"], 100)
            self.assertEqual(spool.state()["gaps"], 0)
            spool.conn.execute("DROP TRIGGER fail_cursor")
            spool.ingest(page(301, 500))
            self.assertEqual(spool.state()["cursor"], 500)

    def test_actual_sqlite_page_limit_and_disk_guard(self):
        with producer(self.source) as spool:
            spool.ingest(page(1, 2))
            pages = spool.conn.execute("PRAGMA page_count").fetchone()[0]
            spool.conn.execute(f"PRAGMA max_page_count={pages}")
            large = page(3, 100)
            for message in large["messages"]:
                message["text"] = "x" * 4096
            with self.assertRaises(sqlite3.OperationalError):
                spool.ingest(large)
            self.assertEqual(spool.state()["cursor"], 2)
            self.assertEqual(spool.state()["messages"], 2)
            with patch.object(spool, "capacity", side_effect=ObserverError("SPOOL_STORAGE_LOW")):
                with self.assertRaisesRegex(ObserverError, "STORAGE_LOW"):
                    spool.ingest(page(3, 4))
            self.assertEqual(spool.state()["cursor"], 2)

    def test_archive_publish_ack_crashes_and_backlog_growth(self):
        for point in ("publication_planned", "shard_fsynced", "shard_published", "before_checkpoint",
                      "archive_published", "before_manifest_commit", "manifest_committed",
                      "before_consumer_ack", "after_consumer_ack"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, destination = root / "spool", root / "archive"
                source.mkdir()
                destination.mkdir()
                with producer(source) as spool:
                    spool.ingest(page(1, 10))
                child = CTX.Process(target=kill_archive, args=(source, destination, point))
                child.start()
                child.join(10)
                if child.is_alive():
                    child.kill()
                    child.join()
                self.assertEqual(child.exitcode, -signal.SIGKILL)
                # More messages and a different reader limit must not change the
                # identity of a publication that crashed before its receipt.
                with producer(source) as spool:
                    spool.ingest(page(11, 100))
                    with ArchiveWorker(spool, destination, min_free_bytes=0) as worker:
                        while worker.step(limit=37)["processed"]:
                            pass
                        self.assertEqual(worker.verify()["messages"], 100)
                        self.assertEqual(worker.conn.execute("SELECT count(*) FROM segments").fetchone()[0], 1)
                        self.assertEqual(spool.consumer("archive")["ack_entry"], spool.state()["high_entry"])

    def test_consumer_outage_and_local_replay_without_network(self):
        with producer(self.source) as spool:
            local = LocalConsumer(spool, "observer", "offline-output-v1")
            for first in range(1, 1001, 100):
                spool.ingest(page(first, first + 99))
            self.assertEqual(spool.state()["messages"], 1000)
            with patch("socket.socket", side_effect=AssertionError("network forbidden")):
                with ArchiveWorker(spool, self.archive, min_free_bytes=0) as worker:
                    while worker.step()["processed"]:
                        pass
                    self.assertEqual(worker.verify()["messages"], 1000)
                self.assertEqual(spool.retention_watermark()["processed_through"], 0)
                rows, state = local.poll()
                restarted = LocalConsumer(spool, "observer", "offline-output-v1")
                replay, _ = restarted.poll()
                self.assertEqual(rows, replay)
                while replay:
                    restarted.acknowledge(hashlib.sha256(canonical(replay).encode()).hexdigest())
                    replay, state = restarted.poll()
                self.assertTrue(state["caught_up"])
                self.assertEqual(state["remote_completeness"], "UNKNOWN")
                self.assertFalse(spool.retention_watermark()["deletion_enabled"])

    def test_read_snapshot_never_exposes_uncommitted_producer_rows(self):
        seen = []
        def checkpoint(point):
            if point == "before_commit":
                rows, state = reader.read(0)
                seen.append((len(rows), state["cursor"]))
        with producer(self.source) as spool:
            with Spool(self.source, "test-room", min_free_bytes=0) as reader:
                spool.checkpoint = checkpoint
                spool.ingest(page(1, 5))
                self.assertEqual(seen, [(0, 0)])
                self.assertEqual(reader.read(0)[1]["cursor"], 5)

    def test_indexed_reads_and_restart_no_history_scan(self):
        with producer(self.source) as spool:
            for first in range(1, 10001, 200):
                spool.ingest(page(first, first + 199))
            plan = spool.conn.execute("EXPLAIN QUERY PLAN SELECT * FROM entries WHERE entry_id>? ORDER BY entry_id LIMIT ?",
                                      (9000, 20)).fetchall()
            self.assertTrue(any("SEARCH entries USING INTEGER PRIMARY KEY" in row[3] for row in plan))
            identity_plan = spool.conn.execute("EXPLAIN QUERY PLAN SELECT payload FROM entries WHERE epoch=? AND seq=? AND kind='MESSAGE'",
                                               (1, 9000)).fetchall()
            self.assertTrue(any("message_identity" in row[3] for row in identity_plan))
            statements = []
            spool.conn.set_trace_callback(statements.append)
            rows, _ = spool.read(9000, 20)
            self.assertEqual(len(rows), 20)
            self.assertFalse(any("OFFSET" in sql.upper() or "COUNT(*)" in sql.upper() for sql in statements))
        with producer(self.source) as spool:
            self.assertEqual(spool.state()["cursor"], 10000)

    def test_single_producer_and_consumer_destination_binding(self):
        with producer(self.source) as spool:
            with self.assertRaises(BlockingIOError):
                producer(self.source)
            spool.register("archive", "first")
            with self.assertRaisesRegex(ObserverError, "BINDING"):
                spool.register("archive", "second")

    def test_restart_rejects_cursor_without_durable_message(self):
        with producer(self.source) as spool:
            spool.ingest(page(1, 2))
            spool.conn.execute("UPDATE state SET cursor=3")
        with self.assertRaisesRegex(ObserverError, "CURSOR_WITHOUT_MESSAGE"):
            producer(self.source)

    def test_source_directory_cannot_be_an_observer_directory(self):
        (self.source / "state.sqlite").write_bytes(b"unrelated observer fixture")
        with self.assertRaisesRegex(ObserverError, "FOREIGN_DIRECTORY"):
            producer(self.source)
        self.assertFalse((self.source / "spool.sqlite").exists())

    def test_archive_receipt_corruption_fails_full_verification(self):
        with producer(self.source) as spool:
            spool.ingest(page(1, 2))
            with ArchiveWorker(spool, self.archive, min_free_bytes=0) as worker:
                worker.step()
                worker.conn.execute("UPDATE receipts SET chain_hash=? WHERE entry_id=1", ("0" * 64,))
                with self.assertRaisesRegex(ObserverError, "RECEIPT_HASH"):
                    worker.verify()

    def test_archive_corruption_rejected_without_advancing_ack(self):
        with producer(self.source) as spool:
            spool.ingest(page(1, 3))
            with ArchiveWorker(spool, self.archive, min_free_bytes=0) as worker:
                worker.step()
                shard = worker.archive.name(1)
                before = spool.consumer("archive")["ack_entry"]
            shard.write_bytes(shard.read_bytes().replace(b"message", b"MESSAGE"))
            with self.assertRaises(ObserverError):
                ArchiveWorker(spool, self.archive, min_free_bytes=0)
            self.assertEqual(spool.consumer("archive")["ack_entry"], before)

    def test_untrusted_fields_stay_data(self):
        with producer(self.source) as spool:
            value = page(1, 1)
            value["messages"][0].update(room="evil", generation=999, path="../../escape", command="POST /say")
            spool.ingest(value)
            with ArchiveWorker(spool, self.archive, min_free_bytes=0) as worker:
                worker.step()
                self.assertEqual(worker.verify()["room"], "test-room")
                self.assertEqual(worker.archive.state["generation"], 1)


if __name__ == "__main__":
    unittest.main()
