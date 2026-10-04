"""Stateful regression for GC/reset and static operation failures."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from systemd_model import SystemdModel
spec = importlib.util.spec_from_file_location('setup', Path(__file__).resolve().parents[1] / 'deploy/connection/setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

class StateTest(unittest.TestCase):
    def test_old_unconditional_reset_reproduces_exit_one_after_gc(self):
        model = SystemdModel(['fixture.socket'])
        model(['systemctl', 'stop', 'fixture.socket'])
        with self.assertRaises(subprocess.CalledProcessError):
            model(['systemctl', 'reset-failed', 'fixture.socket'])

    def test_loaded_inactive_unloaded_and_failed(self):
        model = SystemdModel(['inactive.service', 'failed.service'])
        model.states['failed.service'] = ('loaded', 'failed')
        setup.reset_failed(['inactive.service', 'absent.socket', 'failed.service'], model)
        self.assertEqual([c for c in model.calls if c[1] == 'reset-failed'],
                         [['systemctl', 'reset-failed', 'failed.service']])

    def test_start_limit_reload_and_gc(self):
        model = SystemdModel(['fixture.service'])
        model.limited.add('fixture.service')
        with self.assertRaises(subprocess.CalledProcessError):
            model(['systemctl', 'start', 'fixture.service'])
        model(['systemctl', 'daemon-reload'])
        self.assertIn('fixture.service', model.limited)
        setup.reset_failed(['fixture.service'], model)
        model(['systemctl', 'start', 'fixture.service'])
        model(['systemctl', 'stop', 'fixture.service'])
        model(['systemctl', 'daemon-reload'])
        setup.reset_failed(['fixture.service'], model)
        self.assertEqual(model.reloads, 2)

    def test_unknown_state_fails_closed(self):
        model = SystemdModel()
        for state in [('error', 'inactive'), ('loaded', 'activating'), ('not-found', 'failed')]:
            model.states['fixture.service'] = state
            with self.assertRaisesRegex(RuntimeError, 'UNIT_STATE_UNSAFE'):
                setup.reset_failed(['fixture.service'], model)

    def test_static_failure_codes_hide_stderr(self):
        cases = [('daemon-reload', [], 'DAEMON_RELOAD_FAILED'),
                 ('stop', ['fixture.socket'], 'SOCKET_STOP_FAILED'),
                 ('start', ['fixture.service'], 'SERVICE_START_FAILED'),
                 ('reset-failed', ['collab-boundary-probe.service'], 'PROBE_RESET_FAILED_REFUSED')]
        for operation, units, code in cases:
            args = ['systemctl', operation] + units
            with patch.object(setup.subprocess, 'run', side_effect=subprocess.CalledProcessError(
                    1, args, stderr=b'SECRET_MUST_NOT_APPEAR')):
                with self.assertRaisesRegex(RuntimeError, '^' + code + '$'):
                    setup.run(args)

if __name__ == '__main__':
    unittest.main()
