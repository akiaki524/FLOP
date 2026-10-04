"""Host orchestration tests; no services, real credentials or external writes."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]

def module(name):
    spec = importlib.util.spec_from_file_location(name, REPO / 'deploy/connection' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

class CheckpointTest(unittest.TestCase):
    def test_each_step_requires_positive_typed_evidence_even_with_exit_zero(self):
        checkpoint = module('host_checkpoint')
        for kind, expected in checkpoint.RESULT_CHECKS.items():
            with self.subTest(kind=kind):
                def invoke(value):
                    with patch.object(checkpoint.subprocess, 'run', return_value=SimpleNamespace(
                            stdout=json.dumps(value).encode(), returncode=0)):
                        return checkpoint.call(['dummy-fixture'], kind=kind)
                self.assertEqual(invoke(expected), expected)
                for invalid in [{}, {'passed': False}, dict(expected, passed=False)]:
                    with self.assertRaisesRegex(RuntimeError, 'STEP_RESULT_INVALID'):
                        invoke(invalid)
                for key in expected:
                    missing = dict(expected)
                    del missing[key]
                    with self.assertRaisesRegex(RuntimeError, 'STEP_RESULT_INVALID'):
                        invoke(missing)

    def test_nested_denial_or_boolean_integer_substitution_cannot_pass(self):
        checkpoint = module('host_checkpoint')
        original = checkpoint.RESULT_CHECKS['permissions']
        for key, value in [('no_new_privileges', False), ('uid_matches', 1)]:
            result = json.loads(json.dumps(original))
            result['services']['signer'][key] = value
            with patch.object(checkpoint.subprocess, 'run', return_value=SimpleNamespace(
                    stdout=json.dumps(result).encode(), returncode=0)), self.assertRaisesRegex(
                        RuntimeError, 'STEP_RESULT_INVALID'):
                checkpoint.call(['dummy-fixture'], kind='permissions')

    def test_success_exit_with_invalid_json_cannot_pass(self):
        checkpoint = module('host_checkpoint')
        for output in [b'not json', b'{}\n{}', b'[]']:
            with self.subTest(output=output), patch.object(checkpoint.subprocess, 'run',
                    return_value=SimpleNamespace(stdout=output, returncode=0)), self.assertRaises(RuntimeError):
                checkpoint.call(['dummy-fixture'])

    def test_installed_smoke_routes_to_preserving_update(self):
        checkpoint = module('host_checkpoint')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def call(args, cwd=None, kind=None):
                calls.append(args)
                return {'passed': True}
            with patch.object(checkpoint, 'STATE', root), patch.object(checkpoint, 'DEPLOYMENT', root), \
                    patch.object(checkpoint, 'call', side_effect=call), \
                    patch.object(checkpoint.os, 'geteuid', return_value=0), \
                    patch.object(checkpoint.pwd, 'getpwuid', return_value=SimpleNamespace(pw_name='human')), \
                    patch.dict(os.environ, {'SUDO_UID': '1000'}), \
                    patch.object(sys, 'argv', ['checkpoint', sys.executable]), \
                    patch.object(checkpoint.subprocess, 'run'), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(checkpoint.main(), 0)
            self.assertTrue(calls[0][2].endswith('recover_smoke.py'))
            self.assertFalse(any('setup.py' in arg or 'install.py' in arg for call in calls for arg in call))
            self.assertTrue(json.loads(output.getvalue())['passed'])
            self.assertEqual(len(calls), 5)

    def test_initialized_checkpoint_never_replays_fresh_smoke(self):
        checkpoint = module('host_checkpoint')
        for consumed in (True, False):
            with self.subTest(consumed=consumed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                kinds = []
                def call(args, cwd=None, kind=None):
                    kinds.append(kind)
                    if kind == 'recover_smoke':
                        return {'state_mode': 'reconciliation', 'quota_consumed': consumed}
                    return {'passed': True}
                with patch.object(checkpoint, 'STATE', root), patch.object(checkpoint, 'DEPLOYMENT', root), \
                        patch.object(checkpoint, 'call', side_effect=call), \
                        patch.object(checkpoint.os, 'geteuid', return_value=0), \
                        patch.object(checkpoint.pwd, 'getpwuid', return_value=SimpleNamespace(pw_name='human')), \
                        patch.dict(os.environ, {'SUDO_UID': '1000'}), \
                        patch.object(sys, 'argv', ['checkpoint', sys.executable]), \
                        patch.object(checkpoint.subprocess, 'run'), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(checkpoint.main(), 0 if consumed else 1)
                self.assertNotIn('smoke', kinds)
                if consumed:
                    self.assertIn('permissions', kinds)
                    self.assertIn('crash_recover', kinds)
                else:
                    self.assertEqual(kinds, ['recover_smoke'])
                    self.assertEqual(json.loads(output.getvalue())['failure_code'],
                                     'RECOVERY_UNADMITTED_PRESERVED')

    def test_diagnostic_never_reflects_arbitrary_error_or_stale_data(self):
        checkpoint = module('host_checkpoint')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'diagnostic.json'
            path.write_text(json.dumps({'schema': 1, 'phase': 'CREDENTIAL', 'code': 'ERR_ACCESS_DENIED',
                                        'extra': 'DO_NOT_REFLECT'}))
            path.chmod(0o600)
            with patch.object(checkpoint, 'Path', return_value=path), \
                    patch.object(checkpoint.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid())):
                value = checkpoint.signer_diagnostic(0)
                self.assertEqual(value, {'available': True, 'phase': 'CREDENTIAL', 'code': 'ERR_ACCESS_DENIED'})
                self.assertEqual(checkpoint.signer_diagnostic(path.stat().st_mtime_ns + 1), {'available': False})
                path.write_text(json.dumps({'schema': 1, 'phase': 'CREDENTIAL', 'code': 'DO_NOT_REFLECT'}))
                self.assertEqual(checkpoint.signer_diagnostic(0), {'available': False})
                path.chmod(0o644)
                self.assertEqual(checkpoint.signer_diagnostic(0), {'available': False})

class CrashTest(unittest.TestCase):
    def exercise_injection(self, kill_code, states, succeeds=True, query_code=0):
        verify = module('verify')
        clock = [0.0]
        observed = iter(states)
        last = states[-1]
        def run(args, **kwargs):
            nonlocal last
            self.assertGreater(kwargs['timeout'], 0)
            self.assertLessEqual(kwargs['timeout'], 5)
            self.assertFalse(kwargs['check'])
            if args[1] == 'kill':
                self.assertEqual(args, ['systemctl', 'kill', '--signal=SIGKILL',
                                       '--kill-whom=main', 'collab-signer.service'])
                if isinstance(kill_code, Exception):
                    raise kill_code
                return SimpleNamespace(returncode=kill_code)
            last = next(observed, last)
            if isinstance(last, Exception):
                raise last
            return SimpleNamespace(returncode=query_code, stdout=last)
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(verify, 'run', side_effect=run) as runner, \
                patch.object(verify.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(verify.time, 'sleep', side_effect=sleep):
            if succeeds:
                verify.inject_crash(12345)
            else:
                with self.assertRaisesRegex(RuntimeError, '^SERVICE_KILL_FAILED$'):
                    verify.inject_crash(12345)
        self.assertLessEqual(clock[0], 5)
        self.assertLessEqual(runner.call_count, 53)
        return clock[0]

    KILLED = b'ExecMainPID=12345\nExecMainCode=2\nExecMainStatus=9\nActiveState=failed\n'
    ALIVE = b'ExecMainPID=12345\nExecMainCode=0\nExecMainStatus=0\nActiveState=active\n'

    def test_nonzero_kill_with_observed_sigkill_succeeds(self):
        self.exercise_injection(1, [self.KILLED])

    def test_kill_command_error_still_requires_observed_termination(self):
        self.exercise_injection(RuntimeError('SERVICE_KILL_FAILED'), [self.KILLED])
        self.exercise_injection(RuntimeError('SERVICE_KILL_FAILED'), [self.ALIVE], False)

    def test_nonzero_kill_with_live_process_fails(self):
        self.assertEqual(self.exercise_injection(1, [self.ALIVE], False), 5)

    def test_zero_kill_with_live_process_fails(self):
        self.assertEqual(self.exercise_injection(0, [self.ALIVE], False), 5)

    def test_delayed_sigkill_and_inactive_state_succeed(self):
        for code in (0, 1):
            with self.subTest(code=code):
                elapsed = self.exercise_injection(code, [self.ALIVE,
                    self.KILLED.replace(b'failed', b'inactive')])
                self.assertGreater(elapsed, 0)

    def test_every_required_property_and_query_success_are_required(self):
        for old, new in [(b'12345', b'54321'), (b'Code=2', b'Code=3'),
                         (b'Status=9', b'Status=15'), (b'failed', b'active')]:
            with self.subTest(property=old):
                self.exercise_injection(0, [self.KILLED.replace(old, new)], False)
        for state in (b'', b'not properties', b'\xff', RuntimeError('SERVICE_STATE_QUERY_FAILED')):
            with self.subTest(state=repr(state)):
                self.exercise_injection(0, [state], False)
        self.exercise_injection(0, [self.KILLED], False, query_code=1)

    def test_crash_stops_activation_before_lock_removal_and_uses_ipc_readiness(self):
        verify = module('verify')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'custody').mkdir()
            (root / 'ledger').mkdir()
            (root / 'custody/connection-pointer.json').write_text('{}')
            (root / 'custody/connection-state.enc').write_bytes(b'dummy ciphertext fixture')
            (root / 'custody/connection-store.lock').write_text('{"pid":12345}')
            (root / 'ledger/identity-fixture.json.lock').write_text('{"pid":12345}')
            config = root / 'config.json'
            import hashlib
            config.write_text(json.dumps({'mode': 'DUMMY_OFFLINE', 'dummyCredentialSha256': hashlib.sha256(b'D' * 32).hexdigest()}))
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                if args[0] == 'systemd-creds':
                    return SimpleNamespace(stdout=b'D' * 32)
                if args[1] == 'stop':
                    self.assertTrue((root / 'custody/connection-store.lock').exists())
                if 'ExecMainPID' in args:
                    return SimpleNamespace(returncode=0, stdout=b'ExecMainPID=12345\nExecMainCode=2\nExecMainStatus=9\nActiveState=failed\n')
                if 'LoadState' in args:
                    return SimpleNamespace(stdout=b'LoadState=not-found\nActiveState=inactive\n')
                return SimpleNamespace(stdout=b'12345')
            def ready():
                self.assertFalse((root / 'custody/connection-store.lock').exists())
                self.assertTrue(any(c[1] == 'kill' for c in calls))
            def recover(node):
                (root / 'custody/connection-store.lock').unlink()
            with patch.object(verify.recovery, 'recover', side_effect=recover), patch.object(verify.setup, 'trusted', side_effect=lambda p, **kw: p), patch.object(verify, 'ROOT', root), patch.object(verify, 'Path', return_value=config), \
                    patch.object(verify, 'run', side_effect=run), \
                    patch.object(verify, 'recovered_status', side_effect=ready) as readiness, \
                    patch.object(verify.os, 'geteuid', return_value=0), \
                    patch.object(verify.os, 'kill', side_effect=ProcessLookupError), \
                    patch.object(sys, 'argv', ['verify', 'crash-recover']), redirect_stdout(io.StringIO()) as output:
                verify.main()
            self.assertEqual(calls[2][1:3], ['kill', '--signal=SIGKILL'])
            readiness.assert_called_once()
            self.assertTrue(json.loads(output.getvalue())['custody_nonce_unchanged'])

if __name__ == '__main__':
    unittest.main()
