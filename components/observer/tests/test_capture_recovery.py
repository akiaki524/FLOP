"""Offline retained-export recovery checks; no network or production state IO."""

from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from technocore_full_capture.capture_first import FastCapture
from technocore_full_capture.deadline import RequestFailure
from technocore_full_capture.pacing import GracefulStop
from technocore_full_capture.recovery import recover
from technocore_full_capture.spool import Spool, canonical
from technocore_observer.protocol import ObserverError, Reply


def page(first, last, generation=1):
    messages = [{"seq": seq, "text": "message-%d" % seq, "from": "fixture"}
                for seq in range(first, last + 1)]
    return {"room": "test-room", "generation": generation, "count": len(messages),
            "messages": messages,
            "first_seq": first if messages else None,
            "last_seq": last if messages else None}


def reply(value, status=200):
    if isinstance(value, bytes):
        body = value
        content_type = "application/json"
    else:
        body = json.dumps(value, separators=(",", ":")).encode()
        content_type = "application/json"
    return Reply(status, content_type, body)


class ImmediateBudget:
    def __init__(self):
        self.requests = 0
        self.deferred = []

    @contextmanager
    def request(self, room):
        self.requests += 1
        yield

    def defer(self, delay):
        self.deferred.append(delay)


class StopOnRequestBudget(ImmediateBudget):
    def __init__(self, stop_on):
        super().__init__()
        self.stop_on = stop_on

    @contextmanager
    def request(self, room):
        self.requests += 1
        if self.requests == self.stop_on:
            raise GracefulStop("CAPTURE_STOP_REQUESTED")
        yield


class ExportFixture:
    """A deterministic poll/export fixture with no HTTP implementation."""

    def __init__(self, polls, export_messages=None, *, export_generation=1,
                 export_status=200, export_error=None, export_body=None,
                 export_reply_body=None):
        self.polls = list(polls)
        self.export_messages = export_messages
        self.export_generation = export_generation
        self.export_status = export_status
        self.export_error = export_error
        self.export_body = export_body
        self.export_reply_body = export_reply_body
        self.poll_calls = []
        self.export_calls = []

    def poll(self, since):
        self.poll_calls.append(since)
        if not self.polls:
            raise AssertionError("unexpected poll")
        value = self.polls.pop(0)
        return value if isinstance(value, Reply) else reply(value)

    def export(self, path, generation, *, max_bytes=None):
        self.export_calls.append((Path(path), generation))
        if self.export_error is not None:
            raise self.export_error
        if self.export_body is not None:
            Path(path).write_bytes(self.export_body)
        elif self.export_messages is not None:
            Path(path).write_bytes(b"".join(canonical(message).encode() + b"\n"
                                             for message in self.export_messages))
        return (Reply(self.export_status, "application/json", self.export_reply_body)
                if self.export_reply_body is not None
                else reply({"generation": self.export_generation}, self.export_status))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="capture-recovery-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def open_spool(self):
        (self.root / "spool").mkdir()
        return Spool(self.root / "spool", "test-room", producer=True, create=True,
                     min_free_bytes=0)

    def run_worker(self, client):
        spool = self.open_spool()
        self.addCleanup(spool.close)
        worker = FastCapture(spool, client, ImmediateBudget())
        return spool, worker

    def test_more_than_200_advance_is_recovered_from_retained_export(self):
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410)),
                                reply(page(211, 410))],
                               page(1, 410)["messages"])
        spool, worker = self.run_worker(client)

        worker.step()
        worker.step()

        self.assertEqual(spool.state()["cursor"], 410)
        self.assertEqual(spool.state()["messages"], 410)
        self.assertEqual(spool.state()["gaps"], 0)
        self.assertEqual(len(client.export_calls), 1)
        self.assertEqual(client.poll_calls, [0, 10, 0])

    def test_http_5xx_then_more_than_200_advance_recovers(self):
        client = ExportFixture([reply(page(1, 10)), Reply(503, "text/plain", b"failure"),
                                reply(page(211, 410)), reply(page(211, 410))],
                               page(1, 410)["messages"])
        spool, worker = self.run_worker(client)
        worker.step()
        worker.step()
        self.assertEqual(spool.state()["cursor"], 10)
        worker.step()
        self.assertEqual(spool.state()["cursor"], 410)
        self.assertEqual(spool.state()["gaps"], 0)
        self.assertEqual(len(client.export_calls), 1)
        self.assertEqual(spool.conn.execute("SELECT code FROM failures").fetchall()[0][0],
                         "HTTP_5XX")

    def test_invalid_json_then_more_than_200_advance_recovers(self):
        client = ExportFixture([reply(page(1, 10)), Reply(200, "application/json", b"{"),
                                reply(page(211, 410)), reply(page(211, 410))],
                               page(1, 410)["messages"])
        spool, worker = self.run_worker(client)
        worker.step()
        worker.step()
        self.assertEqual(spool.state()["cursor"], 10)
        worker.step()
        self.assertEqual(spool.state()["cursor"], 410)
        self.assertEqual(spool.state()["gaps"], 0)
        self.assertEqual(len(client.export_calls), 1)
        self.assertEqual(spool.conn.execute("SELECT code FROM failures").fetchall()[0][0],
                         "INVALID_JSON")

    def test_fully_recoverable_range_has_no_gap_evidence(self):
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410)),
                                reply(page(211, 410))],
                               page(11, 410)["messages"])
        spool, worker = self.run_worker(client)
        worker.step()
        result = worker.step()
        self.assertEqual(result, 0.0)
        self.assertEqual(spool.conn.execute("SELECT count(*) FROM entries WHERE kind='GAP'").fetchone()[0], 0)
        self.assertEqual(spool.state()["cursor"], 410)

    def assert_unrecoverable_gap(self, client, error_code=None):
        spool, worker = self.run_worker(client)
        worker.step()
        before_entries = spool.conn.execute("SELECT count(*) FROM entries").fetchone()[0]
        worker.step()
        self.assertEqual(spool.state()["cursor"], 410)
        self.assertEqual(spool.state()["gaps"], 1)
        gap = spool.conn.execute("SELECT seq,end_seq FROM entries WHERE kind='GAP'").fetchone()
        self.assertEqual(tuple(gap), (11, 210))
        if error_code:
            self.assertEqual(spool.conn.execute(
                "SELECT code FROM failures ORDER BY failure_id DESC LIMIT 1").fetchone()[0],
                             error_code)
        # No partially read export records were committed into the spool.
        self.assertEqual(spool.conn.execute("SELECT count(*) FROM entries").fetchone()[0],
                         before_entries + 201)

    def test_missing_range_already_outside_retention_is_unrecoverable(self):
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410)),
                                reply(page(211, 410))],
                               page(101, 410)["messages"])
        self.assert_unrecoverable_gap(client, "RECOVERY_OUTSIDE_RETENTION")

    def test_export_timeout_is_unrecoverable_and_fails_closed(self):
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410))],
                               export_error=RequestFailure("TOTAL_REQUEST_DEADLINE"))
        self.assert_unrecoverable_gap(client, "RECOVERY_EXPORT_TIMEOUT")

    def test_export_http_failure_is_unrecoverable_and_fails_closed(self):
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410))],
                               export_messages=page(1, 410)["messages"], export_status=503)
        self.assert_unrecoverable_gap(client, "RECOVERY_EXPORT_HTTP_FAILURE")

    def test_malformed_export_is_unrecoverable_and_fails_closed(self):
        body = canonical(page(1, 100)["messages"][0]).encode() + b"\nnot-json\n"
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410)),
                                reply(page(211, 410))],
                               export_body=body)
        self.assert_unrecoverable_gap(client, "INVALID_JSON")

    def test_generation_change_during_recovery_is_unrecoverable(self):
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410)),
                                reply(page(211, 410, generation=2))],
                               export_messages=page(1, 410)["messages"])
        self.assert_unrecoverable_gap(client, "RECOVERY_GENERATION_MISMATCH")

    def test_export_generation_header_mismatch_is_unrecoverable(self):
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410))],
                               export_messages=page(1, 410)["messages"],
                               export_generation=2)
        self.assert_unrecoverable_gap(client, "RECOVERY_GENERATION_MISMATCH")

    def test_overlapping_export_content_mismatch_is_unrecoverable(self):
        messages = page(1, 410)["messages"]
        messages[210] = {"seq": 211, "text": "changed", "from": "fixture"}
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410)),
                                reply(page(211, 410))], export_messages=messages)
        self.assert_unrecoverable_gap(client, "RECOVERY_CONTENT_MISMATCH")

    def test_duplicate_export_record_is_unrecoverable(self):
        messages = page(1, 410)["messages"]
        body = b"".join(canonical(message).encode() + b"\n" for message in messages[:410])
        body += canonical(messages[-1]).encode() + b"\n"
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410)),
                                reply(page(211, 410))],
                               export_body=body)
        self.assert_unrecoverable_gap(client, "RECOVERY_EXPORT_SEQUENCE_HOLE")

    def test_export_internal_sequence_hole_is_unrecoverable(self):
        messages = page(1, 410)["messages"]
        body = b"".join(canonical(message).encode() + b"\n"
                         for message in messages if message["seq"] != 100)
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410)),
                                reply(page(211, 410))], export_body=body)
        self.assert_unrecoverable_gap(client, "RECOVERY_EXPORT_SEQUENCE_HOLE")

    def test_export_trailing_partial_record_is_unrecoverable(self):
        body = b"".join(canonical(message).encode() + b"\n"
                         for message in page(1, 410)["messages"])
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410)),
                                reply(page(211, 410))],
                               export_body=body + b'{"seq":411}')
        self.assert_unrecoverable_gap(client, "CAPTURE_PARTIAL_OR_OVERSIZE_RECORD")

    def test_export_429_defers_budget_and_fails_closed(self):
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410))],
                               export_body=b"", export_status=200,
                               export_reply_body=b'{"failure":"HTTP_429","delay":17}')
        # ExportClient encodes retryable export failures in its bounded reply.
        spool, worker = self.run_worker(client)
        worker.step()
        worker.step()
        self.assertEqual(worker.budget.deferred, [17])
        self.assertEqual(spool.state()["cursor"], 410)
        self.assertEqual(spool.state()["gaps"], 1)

    def test_replay_after_recovery_is_late_observation_without_export_or_new_gap(self):
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410)),
                                reply(page(211, 410)), reply(page(211, 410))],
                               page(1, 410)["messages"])
        spool, worker = self.run_worker(client)
        worker.step()
        worker.step()
        self.assertEqual(spool.state()["cursor"], 410)
        worker.step()
        self.assertEqual(spool.state()["cursor"], 410)
        self.assertEqual(spool.state()["gaps"], 0)
        self.assertEqual(len(client.export_calls), 1)
        # Existing MESSAGE rows are idempotent replays; they are not rewritten
        # as GAP/LATE_OBSERVATION evidence.
        self.assertEqual(spool.conn.execute("SELECT count(*) FROM entries WHERE kind='LATE_OBSERVATION'").fetchone()[0], 0)

    def test_no_gap_normal_path_does_not_call_export(self):
        client = ExportFixture([reply(page(1, 10)), reply(page(11, 20))],
                               page(1, 20)["messages"])
        spool, worker = self.run_worker(client)
        worker.step()
        worker.step()
        self.assertEqual(spool.state()["cursor"], 20)
        self.assertEqual(client.export_calls, [])
        self.assertEqual(spool.state()["gaps"], 0)

    def test_historical_gap_evidence_is_preserved_after_later_recovery(self):
        client = ExportFixture([reply(page(421, 620)), reply(page(421, 620))],
                               page(221, 620)["messages"])
        spool, worker = self.run_worker(client)
        spool.ingest(page(1, 10))
        spool.ingest(page(21, 220))
        historical = tuple(spool.conn.execute(
            "SELECT seq,end_seq FROM entries WHERE kind='GAP'").fetchone())
        worker.step()
        self.assertEqual(spool.state()["cursor"], 620)
        self.assertEqual(tuple(spool.conn.execute(
            "SELECT seq,end_seq FROM entries WHERE kind='GAP'").fetchone()), historical)
        self.assertEqual(spool.state()["gaps"], 1)

    def test_recovery_failure_does_not_commit_partial_export_records(self):
        prefix = b"".join(canonical(message).encode() + b"\n"
                           for message in page(1, 100)["messages"])
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410)),
                                reply(page(211, 410))],
                               export_body=prefix + b"not-json\n")
        spool, worker = self.run_worker(client)
        worker.step()
        worker.step()
        self.assertEqual(spool.state()["messages"], 210)
        self.assertEqual(spool.state()["cursor"], 410)
        self.assertEqual(spool.conn.execute("SELECT count(*) FROM entries WHERE kind='MESSAGE'").fetchone()[0], 210)
        self.assertEqual(spool.conn.execute("SELECT count(*) FROM entries WHERE kind='GAP'").fetchone()[0], 1)

    def test_recovery_request_stop_is_rethrown_without_spool_mutation(self):
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410))],
                               export_error=RequestFailure("CAPTURE_STOP_REQUESTED"))
        spool, worker = self.run_worker(client)
        worker.step()
        before = spool.state()
        with self.assertRaisesRegex(GracefulStop, "CAPTURE_STOP_REQUESTED"):
            worker.step()
        self.assertEqual(spool.state(), before)
        self.assertEqual(spool.conn.execute("SELECT count(*) FROM entries WHERE kind='GAP'").fetchone()[0], 0)

    def test_recovery_budget_stop_is_rethrown_without_spool_mutation(self):
        client = ExportFixture([reply(page(1, 10)), reply(page(211, 410))],
                               export_messages=page(1, 410)["messages"])
        spool = self.open_spool()
        self.addCleanup(spool.close)
        budget = StopOnRequestBudget(3)
        worker = FastCapture(spool, client, budget)
        worker.step()
        before = spool.state()
        with self.assertRaisesRegex(GracefulStop, "CAPTURE_STOP_REQUESTED"):
            worker.step()
        self.assertEqual(spool.state(), before)
        self.assertEqual(spool.conn.execute("SELECT count(*) FROM entries WHERE kind='GAP'").fetchone()[0], 0)

    def test_spool_transaction_rolls_back_partial_recovery_batch(self):
        with self.open_spool() as spool:
            spool.ingest(page(1, 10))
            before = spool.state()
            inserted = 0

            def abort(point):
                nonlocal inserted
                if point == "message_inserted":
                    inserted += 1
                    if inserted == 250:
                        raise OSError("injected local write failure")

            spool.checkpoint = abort
            with self.assertRaisesRegex(OSError, "injected local write failure"):
                spool.ingest(page(11, 410), recovered=True,
                             request_epoch=before["epoch"], request_since=10)
            self.assertEqual(inserted, 250)
            self.assertEqual(spool.state(), before)
            self.assertEqual(spool.conn.execute("SELECT count(*) FROM entries WHERE kind='MESSAGE'").fetchone()[0], 10)
            self.assertEqual(spool.conn.execute("SELECT count(*) FROM batches").fetchone()[0], 1)
            self.assertEqual(spool.conn.execute(
                "SELECT count(*) FROM entries WHERE kind='GAP'").fetchone()[0], 0)

    def test_direct_recovery_failure_leaves_spool_state_unchanged(self):
        client = ExportFixture([reply(page(211, 410))], export_body=b"not-json\n")
        with self.open_spool() as spool:
            spool.ingest(page(1, 10))
            state_before = spool.state()
            entries_before = [tuple(row) for row in spool.conn.execute(
                "SELECT kind,seq,end_seq,payload FROM entries ORDER BY entry_id")]
            with self.assertRaisesRegex(ObserverError, "INVALID_JSON"):
                with recover(client, ImmediateBudget(), "test-room", state_before, page(211, 410),
                             spool=spool, clock=time.monotonic, telemetry={}):
                    self.fail("malformed export accepted")
            self.assertEqual(spool.state(), state_before)
            self.assertEqual([tuple(row) for row in spool.conn.execute(
                "SELECT kind,seq,end_seq,payload FROM entries ORDER BY entry_id")], entries_before)


if __name__ == "__main__":
    unittest.main()
