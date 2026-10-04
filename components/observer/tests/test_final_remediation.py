"""Pre-VPS final remediation: disposable real SQLite, no live requests."""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_observer as fixtures
from test_observer import FakeClient, envelope, reply
from technocore_observer import storage
from technocore_observer.cli import main
from technocore_observer.observer import Observer
from technocore_observer.protocol import ObserverError, Reply
from technocore_observer.storage import Store


class FinalTests(unittest.TestCase):
    setUp = fixtures.DatabaseCase.setUp

    def poll(self, seqs):
        return Observer(self.store, FakeClient(reply(envelope(seqs)))).poll_once()

    def rows(self, table):
        return [tuple(row) for row in self.store.conn.execute("SELECT * FROM " + table)]

    def assert_gap_stop_and_resync(self, seq):
        prior = {t: self.rows(t) for t in ("messages", "gaps", "evidence", "evidence_metadata")}
        before = self.store.state()
        self.assertFalse(self.poll([seq])[0])
        after = self.store.state()
        self.assertEqual(after["status"], "ERROR")
        for field in ("poll_seq", "resolved_seq", "observer_epoch"):
            self.assertEqual(after[field], before[field])
        for table in ("messages", "gaps"):
            self.assertEqual(self.rows(table), prior[table])
        for table in ("evidence", "evidence_metadata"):
            self.assertEqual(self.rows(table)[:len(prior[table])], prior[table])
        self.assertEqual(self.rows("evidence")[-1][-1], "EPOCH_GAP_BUDGET_REQUIRES_REVIEW")
        self.assertEqual(after["consecutive_protocol_anomalies"], 0)
        self.store.close()
        with self.assertRaisesRegex(ObserverError, "HUMAN_REVIEW"):
            Store(self.directory, "test-room")
        self.store = Store(self.directory, "test-room", recovery=True)
        self.addCleanup(self.store.close)
        self.assertTrue(self.store.heartbeat()["human_review_required"])
        evidence = self.rows("evidence")
        plan = self.store.resync_plan(before["poll_seq"], 7, "reviewed unobserved amount")
        self.store.resync(before["poll_seq"], 7, "reviewed unobserved amount", plan["approval_token"])
        self.assertEqual(self.store.state()["observer_epoch"], 2)
        self.assertEqual(self.rows("gaps"), prior["gaps"])
        self.assertEqual(self.rows("evidence"), evidence)
        self.assertEqual(self.store.heartbeat()["cumulative_unobserved_seq_count"], 0)
        self.assertTrue(self.poll([before["poll_seq"] + 2])[0])

    def test_cumulative_seq_below_equal_and_over(self):
        for _ in range(4):
            self.assertTrue(self.poll([self.store.state()["poll_seq"] + 10001])[0])
        self.assertTrue(self.poll([self.store.state()["poll_seq"] + 10000])[0])
        self.assertEqual(self.store.heartbeat()["cumulative_unobserved_seq_count"], 49999)
        self.assertTrue(self.poll([self.store.state()["poll_seq"] + 2])[0])
        self.assertEqual(self.store.heartbeat()["cumulative_unobserved_seq_count"], 50000)
        self.assert_gap_stop_and_resync(self.store.state()["poll_seq"] + 2)

    def test_gap_count_below_equal_and_over(self):
        for _ in range(storage.MAX_EPOCH_OPEN_GAPS - 1):
            self.assertTrue(self.poll([self.store.state()["poll_seq"] + 2])[0])
        self.assertEqual(self.store.heartbeat()["open_gap_count"], storage.MAX_EPOCH_OPEN_GAPS - 1)
        self.assertTrue(self.poll([self.store.state()["poll_seq"] + 2])[0])
        self.assertEqual(self.store.heartbeat()["open_gap_count"], storage.MAX_EPOCH_OPEN_GAPS)
        # At the limit, contiguous and empty polls remain valid.
        self.assertTrue(self.poll([self.store.state()["poll_seq"] + 1])[0])
        self.assertTrue(self.poll([])[0])
        self.assert_gap_stop_and_resync(self.store.state()["poll_seq"] + 2)

    def test_budget_telemetry_and_persistent_exhaustion(self):
        with patch.object(storage, "MAX_EVIDENCE_ROWS", 1):
            with self.assertRaisesRegex(ObserverError, "STORAGE_BUDGET_EXHAUSTED"):
                self.poll([101])
            h = self.store.heartbeat()
            self.assertEqual(h["storage_budget"]["state"], "EXHAUSTED")
            self.assertTrue(h["human_review_required"])
            self.assertEqual(h["effective_status"], "ERROR")
            self.store.close()
            self.store = Store(self.directory, "test-room")
            self.addCleanup(self.store.close)
            self.assertEqual(self.store.heartbeat()["storage_budget"]["state"], "EXHAUSTED")
            client = FakeClient(reply(envelope([101])))
            with self.assertRaisesRegex(ObserverError, "STORAGE_BUDGET"):
                Observer(self.store, client).poll_once()
            self.assertEqual(client.cursors, [])
            plan = self.store.resync_plan(100, 7, "no bypass")
            with self.assertRaisesRegex(ObserverError, "STORAGE_BUDGET"):
                self.store.resync(100, 7, "no bypass", plan["approval_token"])

    def test_warning_boundary_is_readonly_and_does_not_stop_poll(self):
        with patch.object(storage, "MAX_EVENT_ROWS", 10):
            for _ in range(7):
                self.store.event("FIXTURE")
            self.assertEqual(self.store.heartbeat()["storage_budget"]["state"], "OK")
            self.store.event("FIXTURE")
            old = self.rows("events")
            h = self.store.heartbeat()
            self.assertEqual(h["storage_budget"]["state"], "WARNING")
            self.assertEqual(h["storage_budget"]["metrics"]["event_rows"],
                             {"used": 8, "limit": 10, "remaining": 2, "utilization": 0.8})
            self.assertEqual(self.rows("events"), old)
            self.assertTrue(self.poll([101])[0])
            self.assertTrue(self.poll([102])[0])
            self.store.event("FIXTURE")
            old = {t: self.rows(t) for t in ("messages", "gaps", "evidence", "events")}
            before = self.store.state()
            with self.assertRaisesRegex(ObserverError, "STORAGE_BUDGET"):
                self.poll([104])
            self.assertEqual(old, {t: self.rows(t) for t in old})
            self.assertEqual(before, self.store.state())

    def test_heartbeat_contains_only_budget_counts_not_content(self):
        marker = "SECRET_BODY_NOT_FOR_HEARTBEAT"
        obj = envelope([102])
        obj["messages"][0]["text"] = marker
        Observer(self.store, FakeClient(reply(obj), Reply(503, marker, marker.encode(), marker))).poll_once()
        Observer(self.store, FakeClient(Reply(503, marker, marker.encode(), marker))).poll_once()
        h = self.store.heartbeat()
        self.assertNotIn(marker, json.dumps(h))
        for metric in ("evidence_rows", "evidence_bytes", "event_rows", "db_bytes", "wal_bytes"):
            self.assertGreaterEqual(h["storage_budget"]["metrics"][metric]["used"], 0)
            self.assertIn("remaining", h["storage_budget"]["metrics"][metric])

    def test_real_sqlite_full_rolls_back_gap_and_cursor(self):
        self.poll([102])
        old = {t: self.rows(t) for t in ("messages", "gaps", "evidence", "events")}
        pages = self.store.conn.execute("PRAGMA page_count").fetchone()[0]
        self.store.conn.execute(f"PRAGMA max_page_count={pages}")
        obj = envelope([104])
        obj["messages"][0]["text"] = "x" * 1000000
        with self.assertRaises(sqlite3.OperationalError) as exc:
            Observer(self.store, FakeClient(reply(obj))).poll_once()
        self.assertEqual(exc.exception.sqlite_errorcode, sqlite3.SQLITE_FULL)
        self.assertFalse(self.store.conn.in_transaction)
        self.assertEqual(old, {t: self.rows(t) for t in old})
        self.assertEqual((self.store.state()["poll_seq"], self.store.state()["resolved_seq"]), (102, 100))

    def test_machine_readable_stderr_for_budget_and_full(self):
        from technocore_observer import cli
        for exc, reason in ((ObserverError("STORAGE_BUDGET_EXHAUSTED"), "STORAGE_BUDGET_EXHAUSTED"),
                            (sqlite3.OperationalError("raw must not leak"), "DATABASE_FAILURE")):
            if isinstance(exc, sqlite3.Error):
                exc.sqlite_errorname = "SQLITE_FULL"
            output = io.StringIO()
            with patch.object(cli, "_execute", side_effect=exc), contextlib.redirect_stderr(output):
                self.assertEqual(main(["heartbeat", "--room", "test-room"]), 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["error"], reason)
            self.assertTrue(result["human_review_required"])
            self.assertNotIn("raw must not leak", output.getvalue())

    def prepare_retention(self):
        from technocore_observer.maintenance import backup, retain
        self.poll([102])
        plan = self.store.resync_plan(102, 7, "preserve history during retention")
        self.store.resync(102, 7, "preserve history during retention", plan["approval_token"])
        Observer(self.store, FakeClient(Reply(503, "text/plain", b"private evidence body"))).poll_once()
        # Only this fixture's evidence/events are made old; init anchor retained.
        self.store.conn.execute("UPDATE events SET occurred_at=1")
        self.store.conn.execute("UPDATE evidence SET observed_at=1")
        backup(self.store, "review-one.sqlite")
        return retain, retain(self.store, "review-one.sqlite", 2)

    def test_supported_recovery_preserves_protected_tables_and_integrity(self):
        retain, plan = self.prepare_retention()
        before = {t: self.rows(t) for t in ("messages", "gaps", "state", "epoch_history")}
        self.assertGreater(plan["decision"]["delete"]["events"]["count"], 0)
        self.assertEqual(plan["decision"]["delete"]["evidence"]["count"], 1)
        self.assertNotIn("private evidence body", json.dumps(plan))
        with patch.object(storage, "MAX_EVENT_ROWS", len(self.rows("events"))):
            self.assertEqual(self.store.heartbeat()["storage_budget"]["state"], "EXHAUSTED")
            result = retain(self.store, "review-one.sqlite", 2, plan["approval_token"])
            self.assertTrue(result["applied"])
            self.assertNotEqual(self.store.heartbeat()["storage_budget"]["state"], "EXHAUSTED")
        self.assertEqual(before, {t: self.rows(t) for t in before})
        self.assertEqual(len(self.rows("evidence")), 1)
        self.assertEqual(len(self.rows("evidence_metadata")), 1)
        self.assertEqual(self.store.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertIsNone(self.store.conn.execute("PRAGMA foreign_key_check").fetchone())
        self.store.close()
        self.store = Store(self.directory, "test-room")
        self.addCleanup(self.store.close)
        self.assertTrue(self.poll([103])[0])

    def test_retention_dry_run_and_invalid_approval_do_not_write(self):
        retain, plan = self.prepare_retention()
        old = {t: self.rows(t) for t in ("evidence", "evidence_metadata", "events", "state")}
        for approval in ("", "wrong"):
            with self.assertRaisesRegex(ObserverError, "APPROVAL"):
                retain(self.store, "review-one.sqlite", 2, approval)
        self.assertEqual(plan, retain(self.store, "review-one.sqlite", 2))
        with self.assertRaisesRegex(ObserverError, "APPROVAL"):
            retain(self.store, "review-one.sqlite", 3, plan["approval_token"])
        self.assertEqual(old, {t: self.rows(t) for t in old})

    def test_retention_requires_unchanged_verified_backup(self):
        retain, plan = self.prepare_retention()
        # Same row counts/IDs but changed content must also invalidate approval.
        self.store.conn.execute("UPDATE evidence SET body_bytes=x'78' WHERE anomaly_type='HTTP_5XX'")
        with self.assertRaisesRegex(ObserverError, "BACKUP_SNAPSHOT_MISMATCH"):
            retain(self.store, "review-one.sqlite", 2, plan["approval_token"])

    def prepare_full_fixture(self):
        for _ in range(8):
            Observer(self.store, FakeClient(Reply(503, "text/plain", b"x" * 16384))).poll_once()
        self.store.conn.execute("UPDATE evidence SET observed_at=1")
        self.store.conn.execute("UPDATE events SET occurred_at=1")
        pages = self.store.conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = self.store.conn.execute("PRAGMA page_size").fetchone()[0]
        self.store.conn.execute(f"PRAGMA max_page_count={pages}")
        return pages, page_size

    def test_real_full_recovery_reuses_pages_without_increasing_db_budget(self):
        from technocore_observer.maintenance import backup, retain
        pages, page_size = self.prepare_full_fixture()
        # Small first record is inserted before the second exhausts allocation.
        obj = envelope([102, 103])
        obj["messages"][1]["text"] = "allocation exceeds all fixture pages" * 30000
        prior = {t: self.rows(t) for t in ("messages", "gaps", "evidence", "evidence_metadata", "events", "epoch_history")}
        cursors = (self.store.state()["poll_seq"], self.store.state()["resolved_seq"])
        with patch.object(storage, "MAX_DB_BYTES", pages * page_size):
            before = self.store.heartbeat()["storage_budget"]
            self.assertEqual(before["state"], "WARNING")
            self.assertEqual(before["metrics"]["db_bytes"]["utilization"], 1.0)
            self.assertGreater(before["db_reusable_bytes"], 0)
            self.assertLess(before["db_reusable_bytes"], len(obj["messages"][1]["text"]))
            with self.assertRaises(sqlite3.OperationalError) as exc:
                Observer(self.store, FakeClient(reply(obj))).poll_once()
            self.assertEqual(exc.exception.sqlite_errorcode, sqlite3.SQLITE_FULL)
            self.assertFalse(self.store.conn.in_transaction)
            self.assertEqual(prior, {t: self.rows(t) for t in prior})
            self.assertEqual(cursors, (self.store.state()["poll_seq"], self.store.state()["resolved_seq"]))
            after = self.store.heartbeat()["storage_budget"]
            # A predictive utilization sample is not a transaction-size oracle.
            # The failed write is authoritative even when rollback restores free
            # pages and the indicator remains WARNING (not EXHAUSTED).
            self.assertEqual(after["state"], "WARNING")
            self.assertEqual(after["db_reusable_bytes"], before["db_reusable_bytes"])
            saved = backup(self.store, "review-full.sqlite")
            self.assertEqual(saved["integrity"], "ok")
            plan = retain(self.store, "review-full.sqlite", 2)
            self.assertEqual(plan["decision"]["delete"]["evidence"]["count"], 8)
            self.assertEqual(plan["decision"]["delete"]["events"]["count"], 8)
            applied = retain(self.store, "review-full.sqlite", 2, plan["approval_token"])
            self.assertEqual(applied["integrity"], "ok")
            recovered = self.store.heartbeat()["storage_budget"]
            self.assertGreater(recovered["db_reusable_bytes"], before["db_reusable_bytes"])
            for table in ("messages", "gaps", "epoch_history"):
                self.assertEqual(self.rows(table), prior[table])
            self.assertEqual(len(self.rows("evidence")), 1)  # INIT_ANCHOR retained
            self.assertIsNone(self.store.conn.execute("PRAGMA foreign_key_check").fetchone())
            self.store.close()
            self.store = Store(self.directory, "test-room")
            self.addCleanup(self.store.close)
            self.assertTrue(self.poll([101])[0])
            self.assertEqual(self.store.conn.execute("PRAGMA max_page_count").fetchone()[0], pages)
            self.assertEqual(self.store.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual((self.store.state()["poll_seq"], self.store.state()["resolved_seq"]), (101, 101))
            self.full_recovery_measurement = {
                "sqlite_version": sqlite3.sqlite_version, "page_size": page_size, "max_page_count": pages,
                "db_budget_bytes": pages * page_size, "before": before, "after_full": after,
                "after_retention": recovered, "sqlite_errorname": exc.exception.sqlite_errorname,
                "rollback": True, "partial_commits": False, "backup_integrity": saved["integrity"],
                "retention": applied, "restart_poll_seq": 101, "restart_resolved_seq": 101}

    def test_real_full_cli_exits_nonzero_after_warning_without_partial_commit(self):
        pages, page_size = self.prepare_full_fixture()
        prior = {t: self.rows(t) for t in ("messages", "gaps", "evidence", "evidence_metadata", "events", "epoch_history")}
        cursors = (self.store.state()["poll_seq"], self.store.state()["resolved_seq"])
        self.store.close()
        self.lock.__exit__()
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()),
                                 "--sqlite-full-cli-fixture", str(self.directory), str(pages * page_size)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        sample = json.loads(result.stdout)
        self.assertEqual(sample["storage_budget"]["state"], "WARNING")
        error = json.loads(result.stderr)
        self.assertEqual(error, {"error": "DATABASE_FAILURE", "class": "OperationalError",
                                 "sqlite_code": "SQLITE_FULL", "human_review_required": True})
        self.assertNotIn("allocation exceeds", result.stdout + result.stderr)
        with patch.object(storage, "MAX_DB_BYTES", pages * page_size):
            self.store = Store(self.directory, "test-room", readonly=True)
            self.addCleanup(self.store.close)
            self.assertEqual(prior, {t: self.rows(t) for t in prior})
            self.assertEqual(cursors, (self.store.state()["poll_seq"], self.store.state()["resolved_seq"]))
            self.assertEqual(self.store.heartbeat()["storage_budget"]["state"], "WARNING")
            self.assertEqual(self.store.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.full_cli_measurement = {"returncode": result.returncode, "before_failure": sample,
                                     "stderr": error, "partial_commits": False,
                                     "poll_seq": cursors[0], "resolved_seq": cursors[1]}

    def test_retention_cutoff_boundary_and_invalid_cutoffs(self):
        from technocore_observer.maintenance import backup, retain
        Observer(self.store, FakeClient(Reply(503, "text/plain", b"boundary"))).poll_once()
        self.store.conn.execute("UPDATE evidence SET observed_at=2")
        self.store.conn.execute("UPDATE events SET occurred_at=2")
        backup(self.store, "review-boundary.sqlite")
        for cutoff in (True, -1, float("nan"), float("inf"), 10**20):
            with self.assertRaisesRegex(ObserverError, "INVALID_RETENTION_CUTOFF"):
                retain(self.store, "review-boundary.sqlite", cutoff)
        plan = retain(self.store, "review-boundary.sqlite", 2)
        self.assertEqual(plan["decision"]["delete"]["evidence"]["count"], 0)
        self.assertEqual(plan["decision"]["delete"]["events"]["count"], 0)
        plan = retain(self.store, "review-boundary.sqlite", 3)
        self.assertEqual(plan["decision"]["delete"]["evidence"]["count"], 1)
        self.assertEqual(plan["decision"]["delete"]["events"]["count"], 1)

    def test_retention_rejects_missing_unsafe_or_corrupt_backup(self):
        from technocore_observer.maintenance import backup, retain
        for name in ("../outside.sqlite", "state.sqlite", "/tmp/backup.sqlite"):
            with self.assertRaisesRegex(ObserverError, "BACKUP_NAME"):
                backup(self.store, name)
        with self.assertRaisesRegex(ObserverError, "BACKUP_MISSING"):
            retain(self.store, "review-absent.sqlite", 2)
        backup(self.store, "review-one.sqlite")
        with self.assertRaisesRegex(ObserverError, "BACKUP_ALREADY_EXISTS"):
            backup(self.store, "review-one.sqlite")
        with (self.directory / "review-one.sqlite").open("r+b") as handle:
            handle.write(b"broken")
        with self.assertRaises((ObserverError, sqlite3.DatabaseError)):
            retain(self.store, "review-one.sqlite", 2)

    def test_retention_transaction_failure_rolls_back_all_deletions(self):
        retain, plan = self.prepare_retention()
        before = {t: self.rows(t) for t in ("evidence", "evidence_metadata", "events", "state", "messages", "gaps", "epoch_history")}
        def fail(point):
            if point == "retention_before_commit":
                raise OSError("injected storage failure")
        with self.assertRaises(OSError):
            retain(self.store, "review-one.sqlite", 2, plan["approval_token"], checkpoint=fail)
        self.assertEqual(before, {t: self.rows(t) for t in before})

    def test_retention_keeps_evidence_referenced_by_newer_events(self):
        from technocore_observer.maintenance import backup, retain
        Observer(self.store, FakeClient(Reply(503, "text/plain", b"keep"))).poll_once()
        self.store.conn.execute("UPDATE evidence SET observed_at=1")
        backup(self.store, "review-one.sqlite")
        plan = retain(self.store, "review-one.sqlite", 2)
        self.assertEqual(plan["decision"]["delete"]["evidence"]["count"], 0)
        self.assertEqual(plan["decision"]["delete"]["events"]["count"], 0)

    def test_retention_cli_is_offline_and_requires_lock(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(main(["retention-backup", "--room", "test-room", "--state-dir", str(self.directory),
                                   "--backup-name", "review-cli.sqlite"]), 1)
        self.assertIn("OBSERVER_ALREADY_LOCKED", out.getvalue())
        self.store.close()
        self.lock.__exit__()
        from technocore_observer.cli import SafeClient
        args = ["--room", "test-room", "--state-dir", str(self.directory), "--backup-name", "review-cli.sqlite"]
        with patch.object(SafeClient, "_get", side_effect=AssertionError("network forbidden")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["retention-backup"] + args), 0)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["retention-plan"] + args + ["--before-unix", "2"]), 0)
            token = json.loads(output.getvalue())["approval_token"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["retention-apply"] + args + ["--before-unix", "2", "--approval", token]), 0)


def sqlite_full_cli_fixture(directory, budget):
    """Only the HTTP boundary is faked; real run/Store/SQLite/CLI exit handling."""
    from technocore_observer import cli
    obj = envelope([102, 103])
    obj["messages"][1]["text"] = "allocation exceeds all fixture pages" * 30000

    class FullClient(FakeClient):
        def poll(self, since):
            with contextlib.closing(Store(directory, "test-room", readonly=True)) as reader:
                cli.emit(reader.heartbeat())
            return super().poll(since)

    with patch.object(storage, "MAX_DB_BYTES", budget), patch.object(cli, "SafeClient", return_value=FullClient(reply(obj))):
        return main(["run", "--room", "test-room", "--state-dir", directory])


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--sqlite-full-cli-fixture":
        raise SystemExit(sqlite_full_cli_fixture(sys.argv[2], int(sys.argv[3])))
    unittest.main()
