"""Offline behavior and real SQLite failure tests. No Technocore requests."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from technocore_observer.cli import emit, main
from technocore_observer.http import SafeClient, retry_after
from technocore_observer.observer import Observer, watchdog
from technocore_observer.protocol import (
    MAX_BODY, ObserverError, ProtocolAnomaly, Reply, decode_reply,
    json_dump, sanitize_for_display, validate_envelope, validate_room,
)
from technocore_observer.storage import StateLock, Store, initialize


def envelope(seqs=(), generation=7, **extra):
    seqs = list(seqs)
    result = {"room": "test-room", "count": len(seqs),
              "first_seq": seqs[0] if seqs else 0,
              "last_seq": seqs[-1] if seqs else 0,
              "generation": generation,
              "messages": [{"seq": seq, "ts": 1000, "from": "did:untrusted",
                            "text": "public message"} for seq in seqs],
              "wait_held": True}
    result.update(extra)
    return result


def reply(obj=None, status=200, content_type="application/json", **extra):
    body = json_dump(obj if obj is not None else envelope()).encode()
    return Reply(status, content_type, body, observed_at=time.time(), **extra)


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.cursors = []

    def poll(self, since):
        self.cursors.append(since)
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def tail(self):
        return self.poll(None)

    def config(self):
        return self.poll(None)


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        # All test databases remain inside this repository, including subprocesses.
        self.temp = tempfile.TemporaryDirectory(prefix=".observer-test-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.lock = StateLock(self.directory)
        self.lock.__enter__()
        self.addCleanup(self.lock.__exit__)
        anchor = envelope([100])
        initialize(self.directory, "test-room", anchor, reply(anchor))
        self.store = Store(self.directory, "test-room")
        self.addCleanup(self.store.close)

    def run_reply(self, obj):
        client = FakeClient(obj if isinstance(obj, Reply) else reply(obj))
        result = Observer(self.store, client).poll_once()
        return result

    def test_contiguous_and_empty(self):
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        self.assertTrue(self.run_reply(envelope([101, 102]))[0])
        self.run_reply(envelope([], last_seq=999))
        s = self.store.heartbeat()
        self.assertEqual((s["poll_seq"], s["resolved_seq"], s["status"]), (102, 102, "RUNNING"))
        for field in ("last_poll_at", "last_http_success_at", "last_valid_response_at", "last_message_saved_at"):
            self.assertIsNotNone(s[field])
        self.assertGreater(s["disk_free_bytes"], 0)

    def test_multiple_gaps(self):
        observer = Observer(self.store, FakeClient(reply(envelope(range(500, 700))), reply(envelope(range(900, 1100)))))
        observer.retention_seconds = 100
        observer.poll_once()
        observer.poll_once()
        s = self.store.heartbeat()
        self.assertEqual((s["poll_seq"], s["resolved_seq"], s["open_gap_count"], s["status"]),
                         (1099, 100, 2, "DEGRADED"))
        gaps = self.store.conn.execute("SELECT * FROM gaps ORDER BY gap_id").fetchall()
        self.assertEqual([(g["start_seq"], g["end_seq"]) for g in gaps], [(101, 499), (700, 899)])
        # A prefix gap is not physical retention loss; no TTL-derived deadline.
        self.assertIsNone(gaps[0]["recovery_deadline_hint"])

    def test_structural_anomalies_and_finite_retry(self):
        cases = [envelope([101, 101]), envelope([100]), envelope([99]),
                 envelope([101, 103]), envelope([102, 101]),
                 envelope([101], room="different"), envelope([101], count=2),
                 envelope([101], first_seq=99), envelope([101], last_seq=999),
                 envelope([101], generation=True), envelope([101], count=True),
                 envelope([101], messages=[{"seq": True}]),
                 envelope([101], messages=[{"seq": "101"}]),
                 envelope([101], messages=[None]), envelope([101], messages={}),
                 envelope([101], wait_held="false")]
        missing = envelope([101])
        del missing["generation"]
        cases.append(missing)
        for malformed in cases:
            with self.subTest(malformed=malformed):
                self.run_reply(malformed)
                self.assertEqual(self.store.state()["poll_seq"], 100)
                # A valid empty poll resets the counter and permits the next case.
                self.run_reply(envelope())
        client = FakeClient(*[reply(envelope([100])) for _ in range(3)])
        observer = Observer(self.store, client)
        self.assertTrue(observer.poll_once()[0])
        self.assertTrue(observer.poll_once()[0])
        self.assertFalse(observer.poll_once()[0])
        self.assertEqual(client.cursors, [100, 100, 100])
        self.assertEqual(self.store.state()["status"], "ERROR")
        self.assertEqual(self.store.state()["consecutive_protocol_anomalies"], 3)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        self.assertGreater(self.store.conn.execute("SELECT count(*) FROM evidence").fetchone()[0], 3)
        with self.assertRaisesRegex(ObserverError, "HUMAN_REVIEW"):
            Store(self.directory, "test-room")

    def test_invalid_wire_replies(self):
        cases = [Reply(200, "application/json", b"{garbage}"),
                 Reply(200, "application/json", b'{"room":'),
                 Reply(200, "application/json", b'\xff'),
                 Reply(200, "application/json", b'{"room":1,"room":2}'),
                 Reply(200, "application/json", b'{"x":NaN}'),
                 reply(envelope(), content_type="text/plain"),
                 reply(envelope(), content_type="text/html"),
                 Reply(200, "application/json", b"x" * MAX_BODY, truncated=True)]
        for item in cases:
            with self.subTest(content_type=item.content_type, length=len(item.body)):
                self.run_reply(item)
                s = self.store.state()
                self.assertEqual(s["poll_seq"], 100)
                self.assertEqual(s["consecutive_protocol_anomalies"], 1)
                self.assertIsNotNone(s["last_http_success_at"])
                self.run_reply(envelope())

    def test_generation_change_including_empty(self):
        for obj in (envelope([1], generation=8), envelope([], generation=8)):
            with self.subTest(empty=not obj["messages"]):
                # Use explicit test-only reset to exercise both replies.
                self.store.conn.execute("UPDATE state SET status='INITIALIZED'")
                self.assertFalse(self.run_reply(obj)[0])
                self.assertEqual(self.store.state()["status"], "NEEDS_RESYNC")
                self.assertEqual(self.store.state()["poll_seq"], 100)
                self.assertEqual(self.store.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
                with self.assertRaisesRegex(ObserverError, "HUMAN_REVIEW"):
                    Store(self.directory, "test-room")
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM events WHERE event_type='GENERATION_CHANGE'").fetchone()[0], 2)

    def test_tolerant_content_and_log_injection(self):
        text = "\x1b[31m\x00\u202e\n" + "x" * 10000 + "\u2028"
        records = [{"seq": 101}, {"seq": 102, "ts": {}, "text": [], "from": False},
                   {"seq": 103, "ts": 100, "from": "did:x", "text": text, "unknown": "https://evil.invalid/"}]
        self.run_reply(envelope([101, 102, 103], messages=records))
        rows = self.store.conn.execute("SELECT * FROM messages ORDER BY seq").fetchall()
        self.assertEqual([json.loads(row["raw_record_json"]) for row in rows], records)
        self.assertEqual(rows[2]["text_value"], text)
        self.assertEqual(set(json.loads(rows[0]["validation_flags"])), {"MISSING_TS", "MISSING_TEXT", "MISSING_FROM"})
        self.assertIn("NON_STRING_TEXT", json.loads(rows[1]["validation_flags"]))
        self.assertIn("INVALID_TS_TYPE", json.loads(rows[1]["validation_flags"]))
        self.assertIn("UNKNOWN_FIELDS", json.loads(rows[2]["validation_flags"]))
        self.assertEqual(rows[2]["trust"], "untrusted")
        self.assertIsNone(rows[2]["signature_verified"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            emit({"text": text})
        for char in ("\x1b", "\x00", "\u202e", "\u2028"):
            self.assertNotIn(char, output.getvalue())
        self.assertEqual(output.getvalue().count("\n"), 1)
        self.assertNotIn("\n", sanitize_for_display(text))

    def test_http_statuses(self):
        for status in (429, 500, 503):
            continuing, delay = self.run_reply(reply(status=status, retry_after="99999999"))
            self.assertTrue(continuing)
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, 600)
            self.assertEqual(self.store.state()["poll_seq"], 100)
        for status in (301, 302, 307, 308, 400, 404, 401, 204):
            self.store.conn.execute("UPDATE state SET status='INITIALIZED'")
            self.assertFalse(self.run_reply(reply(status=status))[0])
            self.assertEqual(self.store.state()["status"], "ERROR")

    def test_network_and_wait_slot_backoff(self):
        observer = Observer(self.store, FakeClient(TimeoutError(), ConnectionResetError()))
        for _ in range(2):
            continuing, delay = observer.poll_once()
            self.assertTrue(continuing)
            self.assertGreaterEqual(delay, 1)
            self.assertEqual(self.store.state()["poll_seq"], 100)
        continuing, delay = self.run_reply(envelope([], wait_held=False))
        self.assertGreaterEqual(delay, 10)
        self.assertEqual(self.store.state()["consecutive_failures"], 0)

    def test_unique_collision_rolls_back_response(self):
        self.run_reply(envelope([101]))
        # Deliberately corrupt only the test cursor after verification.
        self.store.conn.execute("UPDATE state SET poll_seq=100,resolved_seq=100")
        self.run_reply(envelope([101, 102]))
        self.assertEqual(self.store.state()["poll_seq"], 100)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.store.state()["consecutive_protocol_anomalies"], 1)

    def test_sqlite_runtime_settings(self):
        for pragma, expected in (("journal_mode", "wal"), ("synchronous", 2), ("foreign_keys", 1)):
            self.assertEqual(self.store.conn.execute("PRAGMA " + pragma).fetchone()[0], expected)

    def test_real_sqlite_full_rolls_back(self):
        pages = self.store.conn.execute("PRAGMA page_count").fetchone()[0]
        self.store.conn.execute(f"PRAGMA max_page_count={pages}")
        obj = envelope([101])
        obj["messages"][0]["text"] = "x" * 1000000
        with self.assertRaises(sqlite3.OperationalError) as caught:
            self.run_reply(obj)
        self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_FULL)
        self.assertEqual(self.store.state()["poll_seq"], 100)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_transaction_failure_rolls_back(self):
        def fail(point):
            if point == "before_commit":
                raise PermissionError("test write failure")
        with self.assertRaises(PermissionError):
            Observer(self.store, FakeClient(reply(envelope([200]))), fail).poll_once()
        self.assertEqual(self.store.state()["poll_seq"], 100)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM gaps").fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_config_version_drift_and_watchdog(self):
        observer = Observer(self.store, FakeClient(reply({"version": "0.13.0", "retention_seconds": 123})))
        self.assertEqual(observer.check_config()["event"], "VERSION_CHANGED")
        self.assertEqual(observer.retention_seconds, 123)
        self.assertEqual(watchdog(self.store, FakeClient(reply(envelope([101]))))["result"], "LAGGING")
        self.assertEqual(watchdog(self.store, FakeClient(reply(envelope([100]))))["result"], "OK")
        self.assertEqual(watchdog(self.store, FakeClient(reply(envelope([], generation=8))))["result"], "GENERATION_CHANGE")

    def test_invalid_state_metadata_and_room(self):
        with self.assertRaisesRegex(ObserverError, "ROOM_MISMATCH"):
            Store(self.directory, "wrong-room")
        self.store.conn.execute("PRAGMA application_id=1")
        with self.assertRaisesRegex(ObserverError, "APPLICATION_ID"):
            Store(self.directory, "test-room")

    def test_wrong_schema_and_logical_corruption(self):
        self.store.conn.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(ObserverError, "SCHEMA_VERSION"):
            Store(self.directory, "test-room")
        from technocore_observer.storage import SCHEMA_VERSION
        self.store.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.store.conn.execute("UPDATE state SET poll_seq=101,resolved_seq=101")
        with self.assertRaisesRegex(ObserverError, "CURSOR_MISMATCH"):
            Store(self.directory, "test-room")

    def test_terminal_gate_blocks_requests_in_same_process(self):
        client = FakeClient(reply(envelope([101])))
        observer = Observer(self.store, client)
        for status in ("ERROR", "NEEDS_RESYNC"):
            self.store.conn.execute("UPDATE state SET status=?", (status,))
            with self.assertRaisesRegex(ObserverError, "HUMAN_REVIEW"):
                observer.poll_once()
        self.assertEqual(client.cursors, [])

    def test_permissions_and_readonly_heartbeat(self):
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.directory / "state.sqlite").stat().st_mode & 0o777, 0o600)
        with contextlib.closing(Store(self.directory, "test-room", readonly=True)) as reader:
            self.assertEqual(reader.heartbeat()["poll_seq"], 100)
            with self.assertRaises(sqlite3.OperationalError):
                reader.conn.execute("UPDATE state SET status='ERROR'")
        with patch("technocore_observer.storage._connect", side_effect=PermissionError()):
            with self.assertRaises(PermissionError):
                Store(self.directory, "test-room")


class StandaloneTests(unittest.TestCase):
    def test_room_validation_and_allowlist(self):
        for room in ("a", "abc-123_x", "a" * 48):
            self.assertEqual(validate_room(room), room)
        for room in ("", "a" * 49, "A", "../keys", "a?write=1", "a\n", "a/b", "é", "-x"):
            with self.assertRaises(ObserverError):
                SafeClient(room)
        client = SafeClient("test-room")
        with self.assertRaisesRegex(ObserverError, "ENDPOINT_NOT_ALLOWED"):
            client._get("/r/test-room/export", {})
        with self.assertRaisesRegex(ObserverError, "QUERY_NOT_ALLOWED"):
            client._get("/r/test-room", {"format": "json", "limit": 1, "n": 123, "text": "do not write"})
        with self.assertRaisesRegex(ObserverError, "QUERY_NOT_ALLOWED"):
            client._get("/config", {"url": "https://evil.invalid"})

    def test_retry_after(self):
        self.assertEqual(retry_after("120"), (120, False))
        self.assertEqual(retry_after("9999999999"), (600, True))
        self.assertEqual(retry_after("Thu, 01 Jan 1970 00:01:00 GMT", now=0), (60, False))
        for value in (None, "-1", "nan", "1.2", "nonsense"):
            self.assertEqual(retry_after(value), (None, False))

    def test_empty_init_and_missing_run(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as root:
            directory = Path(root)
            with StateLock(directory):
                with self.assertRaisesRegex(ObserverError, "INIT_FAILED_EMPTY_ROOM"):
                    initialize(directory, "test-room", envelope(), reply())
            self.assertFalse((directory / "state.sqlite").exists())
            self.assertFalse((directory / "state.sqlite.init").exists())
            output = io.StringIO()
            with contextlib.redirect_stderr(output), patch.object(SafeClient, "poll") as network:
                self.assertEqual(main(["run", "--room", "test-room", "--state-dir", root]), 1)
                network.assert_not_called()

    def test_empty_nullable_cursors(self):
        validate_envelope(envelope([], first_seq=None, last_seq=None), "test-room")

    def test_stale_init_sidecar_is_preserved(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as root:
            directory = Path(root)
            stale = directory / "state.sqlite.init-wal"
            stale.write_bytes(b"stale evidence")
            with StateLock(directory), self.assertRaisesRegex(ObserverError, "INIT_PATH_ALREADY_EXISTS"):
                initialize(directory, "test-room", envelope([100]), reply(envelope([100])))
            self.assertEqual(stale.read_bytes(), b"stale evidence")
            self.assertFalse((directory / "state.sqlite").exists())

    def test_bad_database_and_missing_wal(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as root:
            directory = Path(root)
            with StateLock(directory):
                initialize(directory, "test-room", envelope([100]), reply(envelope([100])))
            self.assertFalse((directory / "state.sqlite-wal").exists())
            with contextlib.closing(Store(directory, "test-room")) as store:
                self.assertEqual(store.state()["poll_seq"], 100)
            # Damage only this disposable database, exercising quick_check failure.
            with (directory / "state.sqlite").open("r+b") as handle:
                handle.seek(100)
                handle.write(b"\xff" * 100)
            with self.assertRaises((ObserverError, sqlite3.DatabaseError)):
                Store(directory, "test-room")


if __name__ == "__main__":
    unittest.main()
