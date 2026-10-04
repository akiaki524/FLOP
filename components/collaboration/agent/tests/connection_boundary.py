"""Offline boundary/recovery tests. Kernel seccomp is real; systemd calls are mocked.

Run separately so the historical 108-test regression keeps its original count.
No root, network, real credentials, service changes, or new dependencies.
"""
import errno
import hashlib
import importlib.util
import json
import io
from contextlib import redirect_stdout
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('connection_setup', REPO / 'deploy/connection/setup.py')
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class KernelProbeTest(unittest.TestCase):
    def invoke(self, kill=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dummy = os.urandom(32)
            (root / 'dummy').write_bytes(dummy)
            # libseccomp installs a real kernel filter in this child only. The old
            # SIGSYS action is reproduced without producing a core dump.
            code = r'''
import ctypes, errno, os, pathlib, resource, runpy, sys
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
lib = ctypes.CDLL('libseccomp.so.2', use_errno=True)
lib.seccomp_init.argtypes = [ctypes.c_uint32]
lib.seccomp_init.restype = ctypes.c_void_p
lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
lib.seccomp_load.argtypes = [ctypes.c_void_p]
lib.seccomp_release.argtypes = [ctypes.c_void_p]
ctx = lib.seccomp_init(0x7fff0000)
assert ctx
for name in [b'clone', b'clone3', b'fork', b'vfork']:
    num = lib.seccomp_syscall_resolve_name(name)
    assert num >= 0
    action = 0x80000000 if sys.argv[3] == 'kill' else 0x00050000 | errno.EPERM
    assert lib.seccomp_rule_add(ctx, action, num, 0) == 0
assert lib.seccomp_rule_add(ctx, 0x00050000 | errno.EAFNOSUPPORT,
                           lib.seccomp_syscall_resolve_name(b'socket'), 0) == 0
assert lib.seccomp_load(ctx) == 0
lib.seccomp_release(ctx)
runpy.run_path(sys.argv[1])['main'](pathlib.Path(sys.argv[2]))
'''
            output = root / 'result.json'
            env = dict(os.environ, CREDENTIALS_DIRECTORY=tmp, INVOCATION_ID='kernel-test')
            proc = subprocess.run([sys.executable, '-I', '-c', code,
                                   str(REPO / 'deploy/connection/probe.py'), str(output),
                                   'kill' if kill else 'errno'], env=env, capture_output=True)
            if kill:
                self.assertEqual(proc.returncode, -signal.SIGSYS)
                self.assertFalse(output.exists())
                return
            self.assertEqual(proc.returncode, 0, 'seccomp child failed')
            result = json.loads(output.read_text())
            for name in ['credential_read', 'fork_denied', 'inet_denied', 'no_new_privileges',
                         'secret_absent_env', 'secret_absent_argv']:
                self.assertIs(result[name], True, name)
            self.assertEqual(result['fork_errno'], errno.EPERM)
            self.assertEqual(result['uid'], os.getuid())
            self.assertEqual(result['dummy_credential_sha256'], hashlib.sha256(dummy).hexdigest())
            self.assertNotIn(dummy, output.read_bytes())

    def test_sigsys_reproduces_lost_result(self):
        self.invoke(kill=True)

    def test_eperm_denies_fork_and_persists_measurements(self):
        self.invoke()


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.real_path = Path
        def mapped(value):
            value = str(value)
            if value.startswith(('/var/', '/etc/', '/run/', '/usr/local/', '/usr/lib/')):
                return self.root / value.lstrip('/')
            return Path(value)
        self.mapped = mapped
        self.state = mapped('/var/lib/collab-connection-setup')
        self.unit = mapped('/etc/systemd/system/collab-boundary-probe.service')
        self.install = mapped('/usr/local/lib/collab-boundary-probe')
        self.output_dir = mapped('/var/lib/collab-boundary-probe')
        for directory, mode in [(self.state, 0o700), (self.install, 0o755), (self.output_dir, 0o700), (self.unit.parent, 0o755)]:
            directory.mkdir(parents=True, mode=mode)
        (self.state / 'before.json').write_text(json.dumps({'roles_existed': False, 'unit_existed': False, 'created_users': setup.ROLES}))
        (self.state / 'dummy.cred').write_bytes(b'encrypted-dummy-fixture')
        (self.state / 'dummy.cred').chmod(0o600)
        self.legacy_probe = subprocess.check_output(['git', 'show', '905e61a:deploy/connection/probe.py'], cwd=REPO)
        unit = (REPO / 'deploy/connection/probe.service').read_bytes().replace(b':EPERM', b'')
        self.unit.write_bytes(unit)
        (self.install / 'probe.py').write_bytes(self.legacy_probe)
        self.accounts = {r: SimpleNamespace(pw_name=r, pw_uid=2000+i, pw_gid=3000+i, pw_shell='/usr/sbin/nologin') for i, r in enumerate(setup.ROLES)}
        self.groups = {r: SimpleNamespace(gr_gid=3000+i, gr_mem=[]) for i, r in enumerate(setup.ROLES)}
        self.original_trusted = setup.trusted
        # Filesystem mode/symlink logic remains real. Test fixtures cannot chown;
        # ownership enforcement is separately tested with the unmodified helper.
        def trusted(path, uid=0, **kwargs):
            return self.original_trusted(path, uid=os.getuid(), **kwargs)
        patches = [patch.object(setup, 'Path', mapped), patch.object(setup, 'ROOT', self.state),
                   patch.object(setup, 'UNIT', self.unit), patch.object(setup, 'trusted', trusted),
                   patch.object(setup.pwd, 'getpwnam', side_effect=self.accounts.__getitem__),
                   patch.object(setup.pwd, 'getpwall', return_value=list(self.accounts.values())),
                   patch.object(setup.grp, 'getgrnam', side_effect=self.groups.__getitem__),
                   patch.object(setup.grp, 'getgrall', return_value=list(self.groups.values())),
                   patch.object(setup, 'run', side_effect=self.fake_run)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.calls = []

    def fake_run(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == 'systemd-creds':
            return SimpleNamespace(stdout=b'D' * 32)
        if args[:2] == ['systemctl', 'show']:
            return SimpleNamespace(stdout=('FragmentPath=' + str(self.unit) + '\nDropInPaths=\nLoadState=loaded\nActiveState=failed\n').encode())
        return SimpleNamespace(stdout=b'')

    def test_known_partial_setup_preserved_then_migrated(self):
        before = (self.state / 'before.json').read_bytes()
        with patch.object(setup, 'measure') as measure:
            setup.recover()
            measure.assert_called_once_with(hashlib.sha256(b'D' * 32).hexdigest())
        self.assertEqual((self.state / 'before.json').read_bytes(), before)
        self.assertEqual((self.state / 'dummy.cred').read_bytes(), b'encrypted-dummy-fixture')
        backups = list(self.state.glob('probe-recovery-*'))
        self.assertEqual(len(backups), 1)
        manifest = json.loads((backups[0] / 'manifest.json').read_text())
        for path, item in manifest.items():
            self.assertEqual(hashlib.sha256((backups[0] / item['backup']).read_bytes()).hexdigest(), item['sha256'])
        self.assertIn(b'clone:EPERM', self.unit.read_bytes())
        self.assertEqual((backups[0] / manifest[str(self.install / 'probe.py')]['backup']).read_bytes(), self.legacy_probe)

    def test_interrupted_migration_resumes_without_regeneration(self):
        (self.install / 'probe.py').write_bytes((REPO / 'deploy/connection/probe.py').read_bytes())
        with patch.object(setup, 'measure', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                setup.recover()
        with patch.object(setup, 'measure'):
            setup.recover()
        self.assertEqual(len(list(self.state.glob('probe-recovery-*'))), 2)
        self.assertFalse(any(c[0] == 'useradd' or 'encrypt' in c for c in self.calls))

    def assert_refused(self):
        original = self.unit.read_bytes()
        with patch.object(setup, 'measure') as measure:
            with self.assertRaises((RuntimeError, FileNotFoundError)):
                setup.recover()
            measure.assert_not_called()
        self.assertEqual(self.unit.read_bytes(), original)
        self.assertFalse(list(self.state.glob('probe-recovery-*')))

    def test_stale_output_archived_and_cannot_pass(self):
        output = self.output_dir / 'result.json'
        output.write_text('{"old": true}')
        with self.assertRaises(FileNotFoundError):
            setup.measure('unused')
        self.assertFalse(output.exists())
        archived = list(self.state.glob('probe-result-before-*'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_text(), '{"old": true}')

    def test_measure_skips_reset_after_probe_gc(self):
        from systemd_model import SystemdModel
        model = SystemdModel()
        with patch.object(setup, 'run', side_effect=model):
            # The fake start creates no result; reaching this file read proves
            # that unloaded reset did not abort the measurement.
            with self.assertRaises(FileNotFoundError):
                setup.measure('unused')
        self.assertFalse(any(c[1] == 'reset-failed' for c in model.calls))
        self.assertTrue(any(c[1] == 'start' for c in model.calls))

    def test_unknown_unit_refused(self):
        self.unit.write_bytes(self.unit.read_bytes() + b'# unknown\n')
        self.assert_refused()

    def test_unknown_probe_refused(self):
        (self.install / 'probe.py').write_text('unknown')
        self.assert_refused()

    def test_missing_provenance_refused(self):
        (self.state / 'before.json').write_text('{}')
        self.assert_refused()

    def test_downstream_installation_refused(self):
        self.mapped('/etc/collab-connection').mkdir()
        self.assert_refused()

    def test_symlink_refused(self):
        self.unit.unlink()
        self.unit.symlink_to(REPO / 'deploy/connection/probe.service')
        self.assert_refused()

    def test_uid_alias_refused(self):
        alias = SimpleNamespace(pw_name='alias', pw_uid=2002, pw_gid=9999)
        with patch.object(setup.pwd, 'getpwall', return_value=list(self.accounts.values()) + [alias]):
            self.assert_refused()

    def test_bad_dummy_size_refused_before_update(self):
        original_run = self.fake_run
        def bad_dummy(args, **kwargs):
            if args[0] == 'systemd-creds':
                return SimpleNamespace(stdout=b'wrong')
            return original_run(args, **kwargs)
        with patch.object(setup, 'run', side_effect=bad_dummy):
            self.assert_refused()

    def test_group_membership_refused(self):
        self.groups['collab-signer'].gr_mem = ['unexpected']
        self.assert_refused()

    def test_override_refused(self):
        with patch.object(setup, 'run', return_value=SimpleNamespace(stdout=('FragmentPath=' + str(self.unit) + '\nDropInPaths=/override\nActiveState=failed\n').encode())):
            self.assert_refused()

    def test_ownership_enforced(self):
        with self.assertRaises(RuntimeError):
            self.original_trusted(self.unit, uid=os.getuid() + 1)

    def test_writable_mode_refused(self):
        self.unit.chmod(0o666)
        self.assert_refused()


class ResultTest(unittest.TestCase):
    def test_stale_or_false_measurements_refused(self):
        result = dict.fromkeys(['credential_read', 'inet_denied', 'fork_denied', 'no_new_privileges', 'secret_absent_env', 'secret_absent_argv'], True)
        result.update(schema=2, fork_errno=errno.EPERM, uid=2002, invocation_id='a' * 32,
                      cross_uid={r: True for r in setup.ROLES if r != 'collab-signer'})
        with patch.object(setup.pwd, 'getpwnam', side_effect=lambda r: SimpleNamespace(pw_uid=2000 + setup.ROLES.index(r))), patch.object(setup, 'run', return_value=SimpleNamespace(stdout=b'fresh\n')):
            setup.validate_result(result)
            for key, value in [('invocation_id', 'old'), ('fork_errno', errno.EAGAIN), ('uid', 0), ('inet_denied', False), ('cross_uid', {})]:
                with self.subTest(key=key), self.assertRaises(RuntimeError):
                    setup.validate_result(dict(result, **{key: value}))


class CheckpointTest(unittest.TestCase):
    def test_partial_setup_routes_to_recovery_and_preserves_old_report(self):
        spec = importlib.util.spec_from_file_location('checkpoint', REPO / 'deploy/connection/host_checkpoint.py')
        checkpoint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checkpoint)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            old = state / 'host-checkpoint.json'
            old.write_text('historical failure')
            (state / 'result.json').write_text('{"forged_cached_pass":true}')
            calls = []
            def call(args, cwd=None, kind=None):
                calls.append(args)
                if args[2].endswith('install.py'):
                    raise RuntimeError('INSTALL_FIXTURE_STOP')
                return {'fresh_setup': True}
            with patch.object(checkpoint, 'DEPLOYMENT', state / 'absent-deployment'), patch.object(checkpoint, 'STATE', state), patch.object(checkpoint, 'call', side_effect=call), patch.object(checkpoint.os, 'geteuid', return_value=0), patch.object(checkpoint.pwd, 'getpwuid', return_value=SimpleNamespace(pw_name='human')), patch.dict(os.environ, {'SUDO_UID': '1000'}), patch.object(sys, 'argv', ['checkpoint', sys.executable]), patch.object(checkpoint.subprocess, 'run') as save, redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(checkpoint.main(), 1)
            result = json.loads(stdout.getvalue())
            self.assertEqual(calls[0][-1], '--recover-probe')
            self.assertEqual(result['setup'], {'fresh_setup': True})
            self.assertEqual(result['failed_step'], 'install')
            self.assertEqual(result['failure_code'], 'INSTALL_FIXTURE_STOP')
            self.assertEqual(old.read_text(), 'historical failure')
            self.assertEqual(len(list(state.glob('host-checkpoint-*.json'))), 1)
            self.assertIn('host-checkpoint-', result['evidence_file'])
            self.assertEqual(save.call_count, 1)


if __name__ == '__main__':
    unittest.main()
