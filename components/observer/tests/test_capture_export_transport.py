"""Offline export supervision: no HTTP requests or credentials."""

import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from technocore_full_capture.__main__ import ExportClient, RetryCapture
from technocore_full_capture.deadline import DeadlineClient, RequestFailure
from technocore_full_capture.production import TimedClient
from technocore_observer.protocol import ObserverError


def fake_download(path, before, **kwargs):
    Path(path).write_bytes(b'{"seq":1}\n')
    return before["generation"]


def stall_download(path, before, **kwargs):
    Path(path).write_bytes(b'{"seq":')
    time.sleep(60)


def local_io_failure(path, before, **kwargs):
    raise OSError("injected path must not escape")


class ExportTransportTests(unittest.TestCase):
    def test_export_uses_supervised_downloader_and_timing(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(ExportClient, "_download", side_effect=fake_download), \
                patch("socket.socket", side_effect=AssertionError("NO_NETWORK")):
            client = DeadlineClient("test-room", 2, context=multiprocessing.get_context("fork"))
            timed = TimedClient(client)
            path = Path(directory) / "export"
            self.assertEqual(json.loads(timed.export(path, 7).body), {"generation": 7})
            self.assertEqual(path.read_bytes(), b'{"seq":1}\n')
            # Export recovery has its own latency metric; it must not pollute
            # the normal GET latency histogram.
            self.assertEqual(sum(timed.histogram()["counts"]), 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(client.last_pid, 0)

    def test_stalled_export_is_killed_and_reaped(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(ExportClient, "_download", side_effect=stall_download), \
                patch("socket.socket", side_effect=AssertionError("NO_NETWORK")):
            client = DeadlineClient("test-room", 0.2, context=multiprocessing.get_context("fork"))
            start = time.monotonic()
            with self.assertRaisesRegex(RequestFailure, "TOTAL_REQUEST_DEADLINE"):
                client.export(Path(directory) / "export", 1)
            self.assertLess(time.monotonic() - start, 1)
            with self.assertRaises(ProcessLookupError):
                os.kill(client.last_pid, 0)

    def test_failure_metadata_is_fixed_and_rate_delay_is_numeric(self):
        for error, expected in ((ObserverError("GENERATION_CHANGE"), {"failure": "GENERATION_CHANGE"}),
                                (ObserverError("untrusted"), {"failure": "EXPORT_PROTOCOL_FAILURE"}),
                                (RetryCapture("HTTP_429", 12), {"failure": "HTTP_429", "delay": 12})):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory, \
                    patch.object(ExportClient, "_download", side_effect=error), \
                    patch("socket.socket", side_effect=AssertionError("NO_NETWORK")):
                client = DeadlineClient("test-room", 2, context=multiprocessing.get_context("fork"))
                self.assertEqual(json.loads(client.export(Path(directory) / "export", 1).body), expected)

    def test_local_write_failure_is_not_transport_failure(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(ExportClient, "_download", side_effect=local_io_failure), \
                patch("socket.socket", side_effect=AssertionError("NO_NETWORK")):
            client = DeadlineClient("test-room", 2, context=multiprocessing.get_context("fork"))
            value = json.loads(client.export(Path(directory) / "export", 1).body)
            self.assertEqual(value, {"failure": "LOCAL_IO_FAILURE"})

    def test_recovery_cap_is_forwarded_to_exclusive_download(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("socket.socket", side_effect=AssertionError("NO_NETWORK")):
            client = DeadlineClient("test-room", 2, context=multiprocessing.get_context("fork"))
            # fork gives the test a copy, so encode the assertion in the reply
            # path via a side effect that fails unless both constraints arrive.
            def strict(path, before, **kwargs):
                if kwargs != {"max_bytes": 65536, "exclusive": True}:
                    raise AssertionError("missing bounded exclusive flags")
                Path(path).write_bytes(b"")
                return before["generation"]
            with patch.object(ExportClient, "_download", side_effect=strict):
                value = json.loads(client.export(Path(directory) / "export", 1,
                                                 max_bytes=65536).body)
            self.assertEqual(value, {"generation": 1})


if __name__ == "__main__":
    unittest.main()
