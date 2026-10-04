"""P1 resource regressions: private spool staging and bounded-memory recovery."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_capture_recovery import ExportFixture, ImmediateBudget, page, reply
from technocore_full_capture.capture_first import FastCapture
from technocore_full_capture.recovery import recover
from technocore_full_capture.spool import Spool, canonical
from technocore_observer.protocol import ObserverError


class ResourceTests(unittest.TestCase):
    def test_spool_staging_ignores_default_tmpfs_and_cleans_success_failure(self):
        for malformed in (False, True):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with Spool(root, 'test-room', producer=True, create=True, min_free_bytes=0) as spool:
                    api = ExportFixture([page(1, 10), page(211, 410), page(211, 410)],
                                        page(1, 410)['messages'],
                                        export_body=b'bad\n' if malformed else None)
                    worker = FastCapture(spool, api, ImmediateBudget())
                    worker.step()
                    with patch('tempfile.gettempdir', side_effect=AssertionError('tmpfs forbidden')):
                        worker.step()
                    export_path = api.export_calls[0][0]
                    self.assertEqual(export_path.parent.parent, root)
                    self.assertTrue(export_path.parent.name.startswith('.recovery-'))
                    self.assertFalse(export_path.parent.exists())
                    self.assertEqual(list(root.glob('.recovery-*')), [])
                    self.assertEqual(spool.state()['gaps'], int(malformed))

    def test_secure_new_directory_does_not_follow_old_symlink(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            with Spool(root, 'test-room', producer=True, create=True, min_free_bytes=0) as spool:
                (root / '.recovery-old').symlink_to(outside, target_is_directory=True)
                api = ExportFixture([page(1, 10), page(211, 410), page(211, 410)], page(1, 410)['messages'])
                worker = FastCapture(spool, api, ImmediateBudget())
                worker.step(); worker.step()
                self.assertEqual(list(Path(outside).iterdir()), [])
                self.assertTrue((root / '.recovery-old').is_symlink())
                self.assertEqual(spool.state()['gaps'], 0)

    def test_maximum_export_boundary_and_oversize_rejected(self):
        messages = page(1, 410)['messages']
        raw = b''.join((canonical(m)+'\n').encode() for m in messages)
        for extra in (b'', b'\n'):
            with self.subTest(oversize=bool(extra)), tempfile.TemporaryDirectory() as directory:
                with Spool(directory, 'test-room', producer=True, create=True, min_free_bytes=0) as spool:
                    api = ExportFixture([page(1, 10), page(211, 410), page(211, 410)], export_body=raw+extra)
                    worker = FastCapture(spool, api, ImmediateBudget())
                    worker.step()
                    with patch.object(spool, 'recovery_limit', return_value=len(raw)):
                        worker.step()
                    self.assertEqual(spool.state()['gaps'], int(bool(extra)))
                    if extra:
                        self.assertEqual(worker.last['recovery_error'], 'RECOVERY_EXPORT_TOO_LARGE')

    def test_local_staging_write_failure_is_not_transport_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with Spool(directory, 'test-room', producer=True, create=True, min_free_bytes=0) as spool:
                api = ExportFixture([page(1, 10), page(211, 410), page(211, 410)], page(1, 410)['messages'])
                worker = FastCapture(spool, api, ImmediateBudget())
                worker.step()
                with patch('technocore_full_capture.recovery.regular_open', side_effect=OSError(28, 'not persisted')):
                    worker.step()
                self.assertEqual(worker.last['recovery_error'], 'RECOVERY_LOCAL_IO_FAILURE')
                self.assertEqual(list(Path(directory).glob('.recovery-*')), [])
                self.assertEqual(spool.state()['gaps'], 1)

    def test_streaming_near_cap_memory_measurement_in_fresh_process(self):
        root = Path(__file__).resolve().parents[1]
        environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                       'PYTHONPATH': str(root/'src') + os.pathsep + str(root/'tests')}
        child = subprocess.run([sys.executable, '-B', '-c',
                                'from test_capture_recovery_resources import measure_memory; measure_memory()'],
                               capture_output=True, text=True, env=environment, timeout=60)
        self.assertEqual(child.returncode, 0, child.stderr)
        result = json.loads(child.stdout)
        self.assertGreater(result['export_bytes'], 7 * 1024**2)
        self.assertEqual(result['saved'], 4096)
        self.assertLess(result['python_peak_bytes'], 16 * 1024**2)
        self.assertLess(result['rss_peak_bytes'], 128 * 1024**2)
        print('RECOVERY_MEMORY_MEASUREMENT ' + json.dumps(result, sort_keys=True))

    def test_insufficient_capacity_does_not_start_export_or_hide_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            with Spool(directory, 'test-room', producer=True, create=True, min_free_bytes=0) as spool:
                api = ExportFixture([page(1, 10), page(211, 410)], page(1, 410)['messages'])
                worker = FastCapture(spool, api, ImmediateBudget())
                worker.step()
                with patch.object(spool, 'recovery_limit', side_effect=ObserverError('RECOVERY_CAPACITY_INSUFFICIENT')):
                    worker.step()
                self.assertEqual(api.export_calls, [])
                self.assertEqual(worker.last['recovery_error'], 'RECOVERY_CAPACITY_INSUFFICIENT')
                self.assertEqual(spool.state()['gaps'], 1)
                self.assertEqual(list(Path(directory).glob('.recovery-*')), [])


def measure_memory():
    import resource
    import tracemalloc
    def message(seq):
        return {'seq': seq, 'text': 'x' * 1850}
    def tail():
        messages = [message(i) for i in range(3897, 4097)]
        return {'room': 'test-room', 'generation': 1, 'count': 200,
                'first_seq': 3897, 'last_seq': 4096, 'messages': messages}
    class API:
        size = 0
        def poll(self, since):
            return reply(tail())
        def export(self, path, generation, *, max_bytes):
            with open(path, 'wb') as file:
                for i in range(1, 4097):
                    raw = (canonical(message(i))+'\n').encode()
                    self.size += len(raw)
                    file.write(raw)
            assert self.size <= max_bytes
            return reply({'generation': generation})
    with tempfile.TemporaryDirectory() as directory:
        with Spool(directory, 'test-room', producer=True, create=True, min_free_bytes=0) as spool:
            spool.ingest(page(1, 1))
            api = API()
            tracemalloc.start()
            worker = FastCapture(spool, api, ImmediateBudget())
            worker.step()
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert spool.state()['gaps'] == 0
            print(json.dumps({'export_bytes': api.size, 'saved': spool.state()['messages'],
                              'python_peak_bytes': peak,
                              'rss_peak_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}))


if __name__ == '__main__':
    unittest.main()
