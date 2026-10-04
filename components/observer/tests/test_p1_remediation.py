"""P1 negatives: real SQLite, newest-N fixtures, explicit recovery, bounded IO."""
import contextlib
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import signal
import tempfile
import unittest
from unittest.mock import patch

import test_observer as fixtures
from test_observer import FakeClient, envelope, reply
from technocore_observer.cli import main
from technocore_observer.observer import Observer
from technocore_observer.protocol import MAX_BODY, ProtocolAnomaly, Reply, decode_reply
from technocore_observer import storage
from technocore_observer.storage import ObserverError, Store


def resync_crash(directory, point, token):
    def checkpoint(observed):
        if observed == point:
            os.kill(os.getpid(), signal.SIGKILL)
    with storage.StateLock(directory), contextlib.closing(Store(directory, "test-room", recovery=True)) as store:
        store.resync(201, 7, "crash review", token, checkpoint)


class P1Tests(unittest.TestCase):
    setUp = fixtures.DatabaseCase.setUp

    def poll(self, item):
        return Observer(self.store, FakeClient(item)).poll_once()

    def rows(self, table):
        return [tuple(row) for row in self.store.conn.execute("SELECT * FROM " + table)]

    def test_max_sqlite_seq_jump_never_commits_normal_poll(self):
        self.assertFalse(self.poll(reply(envelope([2**63 - 1])))[0])
        s = self.store.state()
        self.assertEqual((s["poll_seq"], s["resolved_seq"]), (100, 100))
        self.assertEqual(self.rows("messages"), [])
        self.assertEqual(self.rows("gaps"), [])
        self.assertEqual(s["status"], "ERROR")
        self.assertEqual(self.rows("evidence")[-1][-1], "FORWARD_JUMP_REQUIRES_RESYNC")
        client = FakeClient(reply(envelope([101])))
        with self.assertRaisesRegex(ObserverError, "HUMAN_REVIEW"):
            Observer(self.store, client).poll_once()
        self.assertEqual(client.cursors, [])
        with contextlib.closing(Store(self.directory, "test-room", readonly=True)) as reader:
            self.assertEqual(reader.state()["poll_seq"], 100)

    def test_jump_guard_also_runs_inside_save_transaction(self):
        with self.assertRaisesRegex(ProtocolAnomaly, "FORWARD_JUMP"):
            self.store.save(envelope([100 + storage.MAX_PREFIX_MISSING + 2]))
        self.assertEqual(self.rows("messages"), [])
        self.assertEqual(self.rows("gaps"), [])
        self.assertEqual(self.store.state()["poll_seq"], 100)

    def test_stale_save_cannot_fill_a_gap_and_move_cursor_backwards(self):
        self.poll(reply(envelope([200])))
        before = self.rows("gaps")
        with self.assertRaisesRegex(ProtocolAnomaly, "OLD_RECORD"):
            self.store.save(envelope([150, 151]))
        self.assertEqual(self.store.state()["poll_seq"], 200)
        self.assertEqual(self.rows("gaps"), before)

    def test_gap_budget_boundary_is_not_absolute_seq_limit(self):
        seq = 101 + storage.MAX_PREFIX_MISSING
        self.assertTrue(self.poll(reply(envelope([seq])))[0])
        plan = self.store.resync_plan(2**63 - 3, 7, "reviewed high absolute anchor")
        self.store.resync(2**63 - 3, 7, "reviewed high absolute anchor", plan["approval_token"])
        self.assertTrue(self.poll(reply(envelope([2**63 - 2, 2**63 - 1])))[0])
        self.assertEqual(self.store.state()["resolved_seq"], 2**63 - 1)

    def test_newest_n_does_not_imply_physical_loss(self):
        retained = list(range(101, 351))
        # Independent newest-first scan/take/reverse model supplied by upstream.
        chosen = list(reversed([seq for seq in reversed(retained) if seq > 100][:200]))
        self.poll(reply(envelope(chosen)))
        self.assertEqual(chosen, list(range(151, 351)))
        self.assertIn(101, retained)
        self.assertEqual(self.store.state()["resolved_seq"], 100)
        gap_event = self.store.conn.execute(
            "SELECT details_json FROM events WHERE event_type='GAP_DETECTED'").fetchone()[0]
        self.assertEqual(json.loads(gap_event)["physical_loss"], "UNKNOWN")
        self.assertIsNone(self.rows("gaps")[0][-1])

    def test_explicit_resync_preserves_old_epoch_and_progresses(self):
        self.poll(reply(envelope([200, 201])))
        old = {t: self.rows(t) for t in ("messages", "gaps", "evidence", "events")}
        plan = self.store.resync_plan(201, 7, "accept unobserved range; no loss claim")
        self.store.resync(201, 7, "accept unobserved range; no loss claim", plan["approval_token"])
        for table, rows in old.items():
            self.assertEqual(self.rows(table)[:len(rows)], rows)
        self.poll(reply(envelope([202, 203])))
        with contextlib.closing(Store(self.directory, "test-room", readonly=True)) as reader:
            s = reader.heartbeat()
            self.assertEqual((s["observer_epoch"], s["poll_seq"], s["resolved_seq"]), (2, 203, 203))
            self.assertEqual(s["open_gap_count"], 0)
            self.assertEqual(s["historical_open_gap_count"], 1)

    def test_generation_resync_and_sequence_overlap(self):
        self.poll(reply(envelope([101])))
        self.poll(reply(envelope([1], generation=8)))
        self.store.close()
        self.store = Store(self.directory, "test-room", recovery=True)
        self.addCleanup(self.store.close)
        plan = self.store.resync_plan(100, 8, "human approved new generation anchor")
        self.store.resync(100, 8, "human approved new generation anchor", plan["approval_token"])
        self.poll(reply(envelope([101], generation=8)))
        self.assertEqual(len(self.rows("messages")), 2)
        with contextlib.closing(Store(self.directory, "test-room", readonly=True)) as reader:
            self.assertEqual(reader.state()["resolved_seq"], 101)

    def test_resync_rejects_missing_stale_and_changed_approval(self):
        plan = self.store.resync_plan(100, 7, "review")
        for token in (None, "", "0" * 64):
            with self.assertRaisesRegex(ObserverError, "APPROVAL"):
                self.store.resync(100, 7, "review", token)
        with self.assertRaisesRegex(ObserverError, "APPROVAL"):
            self.store.resync(101, 7, "review", plan["approval_token"])
        self.poll(reply(envelope([101])))
        with self.assertRaisesRegex(ObserverError, "APPROVAL"):
            self.store.resync(100, 7, "review", plan["approval_token"])
        self.assertEqual(self.store.state()["observer_epoch"], 1)

    def test_resync_failure_rolls_back_history_and_state(self):
        plan = self.store.resync_plan(201, 7, "review")
        before = self.store.state()
        def fail(point):
            if point == "resync_before_commit":
                raise OSError("injected disk error")
        with self.assertRaises(OSError):
            self.store.resync(201, 7, "review", plan["approval_token"], fail)
        self.assertEqual(self.store.state(), before)
        self.assertEqual(self.rows("epoch_history"), [])

    def test_evidence_prefix_hash_metadata_are_preserved(self):
        body = b"a" * (storage.EVIDENCE_PREFIX_BYTES + 30)
        self.poll(Reply(503, "text/html", body, "120"))
        row = self.store.conn.execute("SELECT * FROM evidence ORDER BY evidence_id DESC").fetchone()
        meta = self.store.conn.execute("SELECT * FROM evidence_metadata ORDER BY evidence_id DESC").fetchone()
        self.assertEqual(len(row["body_bytes"]), storage.EVIDENCE_PREFIX_BYTES)
        self.assertEqual(row["body_sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(meta["received_bytes"], len(body))
        self.assertEqual(meta["hash_scope"], "COMPLETE_RECEIVED_BODY")
        self.assertEqual(meta["retry_after"], "120")
        self.assertEqual(row["http_status"], 503)

    def test_truncated_http_body_hash_is_explicitly_prefix(self):
        self.poll(Reply(200, "application/json", b"x" * MAX_BODY, truncated=True))
        row = self.store.conn.execute("SELECT * FROM evidence_metadata ORDER BY evidence_id DESC").fetchone()
        self.assertEqual(row["hash_scope"], "HTTP_PREFIX_ONLY")

    def test_evidence_budget_stops_without_rotation_or_cursor_damage(self):
        with patch.object(storage, "MAX_EVIDENCE_ROWS", 4):
            for _ in range(3):
                self.poll(Reply(503, "text/html", b"x" * 65536))
            before = self.rows("evidence")
            client = FakeClient(reply(envelope([101])))
            with self.assertRaisesRegex(ObserverError, "STORAGE_BUDGET"):
                Observer(self.store, client).poll_once()
            self.assertEqual(client.cursors, [])
            self.assertEqual(self.rows("evidence"), before)
            self.assertEqual(self.store.state()["poll_seq"], 100)
            plan = self.store.resync_plan(100, 7, "cannot reset budget")
            with self.assertRaisesRegex(ObserverError, "STORAGE_BUDGET"):
                self.store.resync(100, 7, "cannot reset budget", plan["approval_token"])

    def test_network_events_are_bounded_too(self):
        with patch.object(storage, "MAX_EVENT_ROWS", 3):
            for _ in range(3):
                self.poll_error()
            with self.assertRaisesRegex(ObserverError, "STORAGE_BUDGET"):
                self.poll_error()
        self.assertEqual(len(self.rows("events")), 3)

    def poll_error(self):
        return Observer(self.store, FakeClient(TimeoutError())).poll_once()

    def test_real_full_while_saving_evidence(self):
        pages = self.store.conn.execute("PRAGMA page_count").fetchone()[0]
        self.store.conn.execute(f"PRAGMA max_page_count={pages}")
        before = {t: self.rows(t) for t in ("evidence", "events", "messages", "gaps")}
        with self.assertRaises(sqlite3.OperationalError) as exc:
            self.poll(Reply(503, "text/html", b"x" * 65536))
        self.assertEqual(exc.exception.sqlite_errorcode, sqlite3.SQLITE_FULL)
        self.assertEqual(before, {t: self.rows(t) for t in before})
        self.assertEqual(self.store.state()["poll_seq"], 100)

    def test_low_disk_stops_before_network(self):
        client = FakeClient(reply(envelope([101])))
        with patch.object(storage.shutil, "disk_usage", return_value=type("Usage", (), {"free": 1})()):
            with self.assertRaisesRegex(ObserverError, "STORAGE_BUDGET"):
                Observer(self.store, client).poll_once()
        self.assertEqual(client.cursors, [])

    def test_config_check_also_stops_before_network_at_capacity(self):
        client = FakeClient(reply({"version": "0.12.1"}))
        with patch.object(storage, "MAX_EVIDENCE_ROWS", 1):
            with self.assertRaisesRegex(ObserverError, "STORAGE_BUDGET"):
                Observer(self.store, client).check_config()
        self.assertEqual(client.cursors, [])

    def test_wal_guard_stops_before_network(self):
        self.store.start_poll()
        client = FakeClient(reply(envelope([101])))
        with patch.object(storage, "MAX_WAL_BYTES", 0):
            with self.assertRaisesRegex(ObserverError, "STORAGE_BUDGET"):
                Observer(self.store, client).poll_once()
        self.assertEqual(client.cursors, [])

    def test_resync_sqlite_full_restores_old_epoch_and_evidence(self):
        plan = self.store.resync_plan(201, 7, "full during resync")
        old = self.store.state()
        evidence = self.rows("evidence")
        pages = self.store.conn.execute("PRAGMA page_count").fetchone()[0]
        self.store.conn.execute(f"PRAGMA max_page_count={pages}")
        def full(point):
            if point == "resync_history_inserted":
                # Real allocation failure inside the same resync transaction.
                self.store.conn.execute("""INSERT INTO evidence(room,observed_at,body_bytes,body_sha256,
                    truncated,anomaly_type) VALUES('test-room',1,zeroblob(?),'fixture',1,'TEST')""",
                    (65536,))
        with self.assertRaises(sqlite3.OperationalError) as exc:
            self.store.resync(201, 7, "full during resync", plan["approval_token"], full)
        self.assertEqual(exc.exception.sqlite_errorcode, sqlite3.SQLITE_FULL)
        self.assertEqual(self.store.state(), old)
        self.assertEqual(self.rows("evidence"), evidence)
        self.assertEqual(self.rows("epoch_history"), [])

    def test_memory_and_hostile_json_subprocess(self):
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("p1_memory_probe.py"))],
                                capture_output=True, text=True, timeout=60, check=True)
        report = json.loads(result.stdout)
        self.assertLess(report["peak_rss_bytes"], 192 * 1024 * 1024)
        self.assertTrue(all(case["passed"] for case in report["cases"]))

    def test_json_shape_limits_precede_object_expansion(self):
        for body in (b'{"x":[' + b"0," * 100000 + b"0]}",
                     b'{"x":' + b"[" * 40 + b"0" + b"]" * 40 + b"}"):
            with self.assertRaisesRegex(ProtocolAnomaly, "JSON_COMPLEXITY"):
                decode_reply(Reply(200, "application/json", body))
        self.assertEqual(self.store.state()["poll_seq"], 100)

    def test_resync_real_sigkill_before_and_after_commit(self):
        self.poll(reply(envelope([200, 201])))
        token = self.store.resync_plan(201, 7, "crash review")["approval_token"]
        self.store.close()
        self.lock.__exit__()
        ctx = multiprocessing.get_context("fork")
        for point in ("resync_history_inserted", "resync_before_commit", "resync_after_commit"):
            child = ctx.Process(target=resync_crash, args=(self.directory, point, token))
            child.start()
            child.join(10)
            if child.is_alive():
                child.kill()
                child.join()
                self.fail("resync child did not stop")
            self.assertEqual(child.exitcode, -signal.SIGKILL)
            with contextlib.closing(Store(self.directory, "test-room", readonly=True)) as reader:
                committed = point == "resync_after_commit"
                self.assertEqual(reader.state()["observer_epoch"], 2 if committed else 1)
                self.assertEqual(reader.conn.execute("SELECT count(*) FROM gaps").fetchone()[0], 1)

    def test_resync_cli_is_offline_and_requires_explicit_approval(self):
        self.store.close()
        self.lock.__exit__()
        base = ["--room", "test-room", "--state-dir", str(self.directory),
                "--anchor-seq", "100", "--generation", "7", "--reason", "operator review"]
        output = io.StringIO()
        with patch("technocore_observer.cli.SafeClient") as network, contextlib.redirect_stdout(output):
            self.assertEqual(main(["resync-plan"] + base), 0)
            token = json.loads(output.getvalue())["approval_token"]
            self.assertEqual(main(["resync"] + base + ["--approval", token]), 0)
            network.assert_not_called()

    def test_evidence_byte_budget_and_metadata_bounds(self):
        body = b"x" * 65536
        initial = len(self.rows("evidence")[0][6])
        with patch.object(storage, "MAX_EVIDENCE_BYTES", initial + 2 * storage.EVIDENCE_PREFIX_BYTES):
            self.poll(Reply(503, "x" * 20000, body, "9" * 20000))
            self.poll(Reply(429, "text/html", body))
            before = self.rows("evidence")
            with self.assertRaisesRegex(ObserverError, "STORAGE_BUDGET"):
                self.poll(Reply(503, "text/html", body))
        self.assertEqual(self.rows("evidence"), before)
        self.assertLessEqual(len(self.rows("evidence")[1][4]), 512)
        self.assertLessEqual(len(self.rows("evidence_metadata")[1][4]), 512)


class MigrationTests(unittest.TestCase):
    @contextlib.contextmanager
    def legacy(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as root:
            path = Path(root) / "state.sqlite"
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            conn = sqlite3.connect(path)
            for statement in storage.LEGACY_SCHEMA:
                conn.execute(statement)
            conn.execute(f"PRAGMA application_id={storage.APPLICATION_ID}")
            conn.execute("PRAGMA user_version=1")
            conn.execute("""INSERT INTO state(singleton,room,server_generation,observer_epoch,
                init_anchor_seq,poll_seq,resolved_seq,status) VALUES(1,'test-room',7,1,100,200,100,'DEGRADED')""")
            conn.execute("""INSERT INTO messages(room,observer_epoch,server_generation,seq,
                raw_record_json,validation_flags,ingest_source,ingested_at)
                VALUES('test-room',1,7,200,'{"seq":200}','[]','poll',1)""")
            conn.execute("""INSERT INTO gaps(room,observer_epoch,server_generation,start_seq,end_seq,
                detected_at,status) VALUES('test-room',1,7,101,199,1,'OPEN')""")
            conn.execute("""INSERT INTO evidence(room,observed_at,body_bytes,body_sha256,truncated,anomaly_type)
                VALUES('test-room',1,?,?,0,'LEGACY_EVIDENCE')""", (b"old bytes", hashlib.sha256(b"old bytes").hexdigest()))
            conn.commit()
            old = {t: conn.execute("SELECT * FROM " + t).fetchall()
                   for t in ("state", "messages", "gaps", "events", "evidence")}
            conn.close()
            with storage.StateLock(root):
                yield root, old

    def test_migrate_preserves_all_legacy_rows_then_resync(self):
        with self.legacy() as (root, old):
            with self.assertRaisesRegex(ObserverError, "SCHEMA_VERSION"):
                Store(root, "test-room")
            plan = storage.migrate_v1(root, "test-room")
            with self.assertRaisesRegex(ObserverError, "APPROVAL"):
                storage.migrate_v1(root, "test-room", "wrong")
            storage.migrate_v1(root, "test-room", plan["approval_token"])
            with contextlib.closing(Store(root, "test-room")) as store:
                for table, rows in old.items():
                    self.assertEqual([tuple(r) for r in store.conn.execute("SELECT * FROM " + table)], rows)
                plan = store.resync_plan(200, 7, "review migrated gap")
                store.resync(200, 7, "review migrated gap", plan["approval_token"])
                Observer(store, FakeClient(reply(envelope([201])))).poll_once()
                self.assertEqual(store.state()["resolved_seq"], 201)

    def test_migration_failure_preserves_v1(self):
        with self.legacy() as (root, old):
            plan = storage.migrate_v1(root, "test-room")
            def fail(point):
                raise OSError("migration interrupted")
            with self.assertRaises(OSError):
                storage.migrate_v1(root, "test-room", plan["approval_token"], fail)
            conn = sqlite3.connect(Path(root) / "state.sqlite")
            try:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
                for table, rows in old.items():
                    self.assertEqual(conn.execute("SELECT * FROM " + table).fetchall(), rows)
            finally:
                conn.close()
