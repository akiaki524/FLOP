"""P2 size diagnostics through real DeadlineClient child/IPC, entirely offline."""
import io
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from technocore_full_capture.capture_first import FastCapture
from technocore_full_capture.deadline import DeadlineClient
from technocore_full_capture.spool import Spool, canonical
from test_capture_recovery import ImmediateBudget, page, reply


class Response:
    code = 200
    def __init__(self, length=None, size=0, body=None):
        self.headers = {'Content-Type': 'application/x-ndjson', 'X-Room-Generation': '1',
                        'Untrusted': 'must-not-persist-header'}
        if length is not None:
            self.headers['Content-Length'] = length
        self.remaining = size
        self.body = io.BytesIO(body) if body is not None else None
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read1(self, limit):
        if self.body is not None:
            return self.body.read(limit)
        count = min(limit, self.remaining)
        self.remaining -= count
        return b'x' * count


class ProductionExportClient:
    def __init__(self):
        self.transport = DeadlineClient('test-room', 3, context=multiprocessing.get_context('fork'))
        self.polls = [page(1, 10), page(211, 410), page(211, 410)]
    def poll(self, since): return reply(self.polls.pop(0))
    def export(self, path, generation, **kwargs):
        return self.transport.export(path, generation, **kwargs)


class SizeEvidenceTests(unittest.TestCase):
    def run_case(self, response, *, expected, length, received, io_failure=False):
        with tempfile.TemporaryDirectory() as directory:
            with Spool(directory, 'test-room', producer=True, create=True, min_free_bytes=0) as spool:
                worker = FastCapture(spool, ProductionExportClient(), ImmediateBudget())
                worker.step()
                # Only HTTP response production is mocked. Actual downloader,
                # child supervision, IPC and recovery failure mapping execute.
                with patch('urllib.request.OpenerDirector.open', return_value=response), \
                        patch('socket.socket', side_effect=AssertionError('NO_NETWORK')), \
                        patch.object(spool, 'recovery_limit', return_value=32768):
                    if io_failure:
                        with patch('technocore_full_capture.__main__.regular_open',
                                   side_effect=OSError(28, 'secret-local-path')):
                            worker.step()
                    else:
                        worker.step()
                self.assertEqual(worker.last.get('recovery_error'), expected)
                self.assertEqual(worker.last['recovery_export_limit_bytes'], 32768)
                self.assertEqual(worker.last['recovery_export_content_length_bytes'], length)
                self.assertEqual(worker.last['recovery_export_received_bytes'], received)
                self.assertEqual(spool.state()['gaps'], int(expected is not None))
                self.assertEqual(list(Path(directory).glob('.recovery-*')), [])
                evidence = json.dumps(worker.last) + json.dumps([tuple(row) for row in spool.conn.execute(
                    'SELECT code,body_sha256,hex(body_prefix) FROM failures')])
                for secret in ('must-not-persist-header', 'secret-local-path', 'secret-length', 'https://'):
                    self.assertNotIn(secret, evidence)
                if expected is not None:
                    self.assertEqual(spool.conn.execute('SELECT code FROM failures').fetchone()[0], expected)

    def test_content_length_over_cap_is_size_failure(self):
        self.run_case(Response(length='32769'), expected='RECOVERY_EXPORT_TOO_LARGE',
                      length=32769, received=0)

    def test_stream_over_cap_without_length_preserves_actual_bytes_only(self):
        self.run_case(Response(size=40000), expected='RECOVERY_EXPORT_TOO_LARGE',
                      length=None, received=32769)

    def test_malformed_length_remains_protocol_failure(self):
        self.run_case(Response(length='secret-length'), expected='RECOVERY_EXPORT_PROTOCOL_FAILURE',
                      length=None, received=0)

    def test_local_file_failure_is_distinct_and_sanitized(self):
        self.run_case(Response(length='100', size=100), expected='RECOVERY_LOCAL_IO_FAILURE',
                      length=100, received=0, io_failure=True)

    def test_success_reports_size_without_inventing_absent_length(self):
        body = b''.join((canonical(m)+'\n').encode() for m in page(1, 410)['messages'])
        self.run_case(Response(body=body), expected=None, length=None, received=len(body))

    def test_unrepresentable_length_is_size_failure_without_fabricated_value(self):
        self.run_case(Response(length='9'*1000), expected='RECOVERY_EXPORT_TOO_LARGE',
                      length=None, received=0)

    def test_existing_header_acceptance_bound_is_not_relaxed(self):
        self.run_case(Response(length='0'*20+'100'), expected='RECOVERY_EXPORT_PROTOCOL_FAILURE',
                      length=100, received=0)


if __name__ == '__main__':
    unittest.main()
