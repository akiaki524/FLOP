import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy'


def load(name):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load('wsl_crash_gate')
proof = load('wsl_crash_proof')
BOOT = '12345678-1234-1234-1234-123456789abc'


class GateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        kernel = root / 'kernel'
        kernel.mkdir()
        (kernel / 'random').mkdir()
        self.original = {'corePattern': '|/wsl-capture-crash %t %E %p %s', 'coreUsesPid': '1'}
        for name, value in {'osrelease': '6.1-microsoft-WSL2', 'random/boot_id': BOOT,
                            'core_pattern': self.original['corePattern'], 'core_uses_pid': '1'}.items():
            (kernel / name).write_text(value + '\n')
        for name, value in [('KERNEL', kernel), ('STATE', root / 'state.json')]:
            patcher = mock.patch.object(gate, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Fake root metadata ONLY inside this unit-test process. No /run or
        # actual sysctl/systemd writes; all state goes into TemporaryDirectory.
        real_fstat = os.fstat
        def fstat(fd):
            st = real_fstat(fd)
            return SimpleNamespace(st_mode=st.st_mode, st_uid=0, st_gid=0,
                                   st_nlink=st.st_nlink, st_size=st.st_size)
        for target, name, kwargs in [
            (gate.os, 'geteuid', {'return_value': 0}),
            (gate.os, 'fchown', {}), (gate.os, 'fstat', {'side_effect': fstat}),
            (gate, 'gate_lock', {'return_value': mock.MagicMock()}),
            (gate, 'stopped', {}),
        ]:
            p = mock.patch.object(target, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        # Regular fixture files need truncation; proc sysctl does not.
        self.real_write = gate.write_value
        def write(name, value):
            (gate.KERNEL / name).write_text(value + '\n')
            gate.need(gate.proc_value(name) == value, 'CRASH_GATE_READBACK_MISMATCH')
        p = mock.patch.object(gate, 'write_value', side_effect=write)
        p.start()
        self.addCleanup(p.stop)

    def test_original_is_saved_before_first_mutation(self):
        write = gate.write_value.side_effect
        def check(name, value):
            self.assertEqual(json.loads(gate.STATE.read_text())['original'], self.original)
            write(name, value)
        gate.write_value.side_effect = check
        gate.operate('arm')

    def test_symlink_and_invalid_prior_state_refused_without_sysctl_write(self):
        target = gate.STATE.with_name('unknown')
        target.write_text('{}')
        gate.STATE.symlink_to(target)
        with self.assertRaises(OSError):
            gate.operate('arm')
        gate.STATE.unlink()
        gate.STATE.write_text('{}')
        gate.STATE.chmod(0o600)
        with self.assertRaisesRegex(gate.GateError, 'STATE_INVALID'):
            gate.operate('arm')
        gate.write_value.assert_not_called()

    def test_partial_restore_refuses_retry_instead_of_overwriting(self):
        gate.operate('arm')
        def partial(name, value):
            if name == 'core_uses_pid':
                raise gate.GateError('CRASH_GATE_WRITE_FAILED')
            (gate.KERNEL / name).write_text(value + '\n')
        gate.write_value.side_effect = partial
        with self.assertRaisesRegex(gate.GateError, 'WRITE_FAILED'):
            gate.operate('disarm')
        gate.write_value.reset_mock()
        with self.assertRaisesRegex(gate.GateError, 'CURRENT_STATE_MISMATCH'):
            gate.operate('disarm')
        gate.write_value.assert_not_called()
        self.assertTrue(gate.STATE.exists())

    def test_transitions_never_expose_empty_pattern_with_pid_suffix(self):
        write = gate.write_value.side_effect
        seen = []
        def check(name, value):
            write(name, value)
            seen.append(gate.current())
        gate.write_value.side_effect = check
        gate.operate('arm')
        gate.operate('disarm')
        self.assertEqual(gate.current(), self.original)
        self.assertEqual([c for c in seen if c['corePattern'] == '' and c['coreUsesPid'] != '0'], [])
        self.assertEqual([c.args[0] for c in gate.write_value.call_args_list],
                         ['core_uses_pid', 'core_pattern', 'core_pattern', 'core_uses_pid'])

    def test_status_read_only_default_and_other_pipe_unsafe(self):
        for pattern in (self.original['corePattern'], '|/bin/false', 'core'):
            (gate.KERNEL / 'core_pattern').write_text(pattern + '\n')
            self.assertFalse(gate.status()['safe'])
        self.assertFalse(gate.STATE.exists())
        gate.stopped.assert_not_called()

    def test_arm_boot_bound_and_disarm_exact_restore(self):
        self.assertTrue(gate.operate('arm')['armed'])
        value = json.loads(gate.STATE.read_text())
        self.assertEqual(value['bootId'], BOOT)
        self.assertEqual(value['original'], self.original)
        self.assertEqual(gate.STATE.stat().st_mode & 0o777, 0o600)
        self.assertEqual(gate.current(), gate.TARGET)
        gate.operate('arm')  # exact state only, no lost original
        gate.operate('disarm')
        self.assertEqual(gate.current(), self.original)
        self.assertFalse(gate.STATE.exists())

    def test_non_wsl_does_not_mutate(self):
        (gate.KERNEL / 'osrelease').write_text('6.1-generic\n')
        self.assertTrue(gate.operate('arm')['safe'])
        gate.operate('disarm')
        gate.write_value.assert_not_called()
        gate.stopped.assert_not_called()
        self.assertFalse(gate.STATE.exists())

    def test_safe_never_depends_on_saved_file(self):
        gate.operate('arm')
        (gate.KERNEL / 'core_pattern').write_text('|/changed\n')
        self.assertFalse(gate.status()['safe'])
        with self.assertRaisesRegex(gate.GateError, 'WSL_CRASH_CAPTURE_UNSAFE'):
            gate.require_safe()

    def test_boot_mismatch_refuses_both_operations(self):
        gate.operate('arm')
        (gate.KERNEL / 'random/boot_id').write_text('aaaaaaaa-1234-1234-1234-123456789abc\n')
        gate.write_value.reset_mock()
        for command in ('arm', 'disarm'):
            with self.assertRaisesRegex(gate.GateError, 'BOOT_MISMATCH'):
                gate.operate(command)
        gate.write_value.assert_not_called()

    def test_external_admin_change_refused(self):
        gate.operate('arm')
        (gate.KERNEL / 'core_uses_pid').write_text('1\n')
        with self.assertRaisesRegex(gate.GateError, 'CURRENT_STATE_MISMATCH'):
            gate.operate('disarm')
        with self.assertRaisesRegex(gate.GateError, 'PRIOR_STATE_MISMATCH'):
            gate.operate('arm')
        self.assertTrue(gate.STATE.exists())

    def test_active_service_prevents_any_change(self):
        for command in ('arm', 'disarm'):
            gate.stopped.side_effect = gate.GateError('CRASH_GATE_SERVICE_NOT_STOPPED')
            with self.assertRaisesRegex(gate.GateError, 'SERVICE_NOT_STOPPED'):
                gate.operate(command)
        gate.write_value.assert_not_called()
        self.assertFalse(gate.STATE.exists())

    def test_readback_failure_keeps_original_and_stops(self):
        gate.write_value.side_effect = gate.GateError('CRASH_GATE_READBACK_MISMATCH')
        with self.assertRaisesRegex(gate.GateError, 'READBACK_MISMATCH'):
            gate.operate('arm')
        self.assertEqual(json.loads(gate.STATE.read_text())['original'], self.original)
        self.assertEqual(gate.write_value.call_count, 1)
        with self.assertRaisesRegex(gate.GateError, 'PRIOR_STATE_MISMATCH'):
            gate.operate('arm')

    def test_low_level_write_empty_uses_newline_and_checks_readback(self):
        with mock.patch.object(gate.os, 'open', return_value=19), \
             mock.patch.object(gate.os, 'close'), \
             mock.patch.object(gate.os, 'write', return_value=1) as write, \
             mock.patch.object(gate, 'proc_value', return_value='not-empty'):
            with self.assertRaisesRegex(gate.GateError, 'READBACK_MISMATCH'):
                self.real_write('core_pattern', '')
        write.assert_called_once_with(19, b'\n')

    def test_restore_failure_keeps_evidence(self):
        gate.operate('arm')
        gate.write_value.side_effect = gate.GateError('CRASH_GATE_READBACK_MISMATCH')
        with self.assertRaisesRegex(gate.GateError, 'READBACK_MISMATCH'):
            gate.operate('disarm')
        self.assertTrue(gate.STATE.exists())

    def test_untrusted_prior_file_refused(self):
        gate.operate('arm')
        gate.STATE.chmod(0o644)
        with self.assertRaisesRegex(gate.GateError, 'STATE_UNTRUSTED'):
            gate.operate('disarm')

    def test_missing_state_and_nonroot_refused(self):
        with self.assertRaisesRegex(gate.GateError, 'STATE_MISSING'):
            gate.operate('disarm')
        gate.os.geteuid.return_value = 1000
        with self.assertRaisesRegex(gate.GateError, 'ROOT_REQUIRED'):
            gate.operate('arm')

    def test_invalid_boot_and_unreadable_proc_fail_closed(self):
        (gate.KERNEL / 'random/boot_id').write_text('invalid\n')
        self.assertFalse(gate.status()['safe'])
        with self.assertRaisesRegex(gate.GateError, 'BOOT_INVALID'):
            gate.operate('arm')
        (gate.KERNEL / 'osrelease').unlink()
        self.assertFalse(gate.status()['safe'])


class ServiceChecks(unittest.TestCase):
    def test_loaded_inactive_and_notfound_are_allowed(self):
        for value in ('LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlPID=0\nJob=\n',
                      'LoadState=not-found\nActiveState=inactive\nJob=\n'):
            with mock.patch.object(gate.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=value)):
                gate.stopped()

    def test_active_transitioning_pid_pending_job_unknown_refused(self):
        good = 'LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlPID=0\nJob=\n'
        for value in ('', good.replace('inactive', 'active'), good.replace('inactive', 'activating'),
                      good.replace('MainPID=0', 'MainPID=23'), good.replace('Job=', 'Job=23')):
            with mock.patch.object(gate.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=value)):
                with self.assertRaisesRegex(gate.GateError, 'SERVICE_NOT_STOPPED'):
                    gate.stopped()


class CrashProofTests(unittest.TestCase):
    def test_synthetic_contract_restore_and_explicit_windows_gap(self):
        original = {'corePattern': '|/wsl-capture-crash', 'coreUsesPid': '1'}
        with mock.patch.object(proof.gate.os, 'geteuid', return_value=0), \
             mock.patch.object(proof.gate, 'is_wsl', return_value=True), \
             mock.patch.object(proof.gate, 'state_present', return_value=False), \
             mock.patch.object(proof.os.path, 'lexists', return_value=False), \
             mock.patch.object(proof.gate, 'boot_id', return_value=BOOT), \
             mock.patch.object(proof.gate, 'current', side_effect=[original, gate.TARGET, gate.TARGET, original]), \
             mock.patch.object(proof.gate, 'operate') as operation, \
             mock.patch.object(proof.gate, 'require_safe'), \
             mock.patch.object(proof.subprocess, 'run', return_value=SimpleNamespace(returncode=-6)) as run:
            result = proof.prove()
        self.assertEqual(operation.call_args_list, [mock.call('arm'), mock.call('disarm')])
        self.assertEqual(result['windowsCaptureObserved'], 'NOT_CHECKED')
        self.assertTrue(result['originalRestoredExactly'])
        self.assertIn('Human', proof.__doc__)
        self.assertIn('resource.RLIMIT_CORE', run.call_args.args[0][-1])
        self.assertIn('os.abort()', run.call_args.args[0][-1])


if __name__ == '__main__':
    unittest.main()
