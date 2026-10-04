"""Filesystem/systemctl simulation for conservative DUMMY_OFFLINE smoke recovery.

No root, network, real credential, or host service is used.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from systemd_model import SystemdModel
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / 'deploy/connection'
sys.path.insert(0, str(DEPLOY))
SPEC = importlib.util.spec_from_file_location('connection_smoke_recover', DEPLOY / 'recover_smoke.py')
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo_source = root / 'repo/src/collaboration_agent'
        self.runtime = root / 'repo/runtime'
        self.dest = root / 'usr/local/lib/collab-connection'
        self.config = root / 'etc/collab-connection/config.json'
        self.units = root / 'etc/systemd/system'
        self.alt_units = [root / 'run/systemd/system', root / 'usr/lib/systemd/system']
        self.state = root / 'var/lib/collab-signer'
        self.setup_root = root / 'var/lib/collab-connection-setup'
        self.probe_install = root / 'usr/local/lib/collab-boundary-probe/probe.py'
        self.probe_unit = root / 'etc/systemd/system/collab-boundary-probe.service'
        for directory, mode in [(self.repo_source, 0o755), (self.runtime, 0o755),
                                (self.dest, 0o755), (self.config.parent, 0o755),
                                (self.units, 0o755), (self.state, 0o700),
                                (self.setup_root, 0o700), *[(p, 0o755) for p in self.alt_units]]:
            directory.mkdir(parents=True, mode=mode)
            directory.chmod(mode)
        self.probe_install.parent.mkdir(parents=True)
        self.probe_install.write_bytes((DEPLOY / 'probe.py').read_bytes())
        self.probe_install.chmod(0o644)
        self.probe_unit.write_bytes((DEPLOY / 'probe.service').read_bytes())
        self.probe_unit.chmod(0o644)
        self.old = b'export const version = "baseline";\n'
        self.new = b'export const version = "fixed";\n'
        runtime_data = b'pinned runtime\n'
        pin = {'repository': 'fixture', 'commit': 'a' * 40, 'version': '1',
               'dependencies': [], 'files': {'runtime.js': digest(runtime_data)}}
        pin_data = (json.dumps(pin) + '\n').encode()
        (self.repo_source / 'a.mjs').write_bytes(self.new)
        (self.repo_source / 'tclk_pin.json').write_bytes(pin_data)
        (self.runtime / 'runtime.js').write_bytes(runtime_data)
        installed_source = self.dest / 'src/collaboration_agent'
        installed_runtime = self.dest / '.local/batch16/official-runtime'
        installed_source.mkdir(parents=True)
        installed_runtime.mkdir(parents=True)
        (installed_source / 'a.mjs').write_bytes(self.old)
        (installed_source / 'tclk_pin.json').write_bytes(pin_data)
        (installed_runtime / 'runtime.js').write_bytes(runtime_data)
        self.node = root / 'usr/bin/node'
        self.node.parent.mkdir(parents=True)
        self.node.write_bytes(b'node fixture')
        self.node.chmod(0o755)
        (self.dest / 'node').write_bytes(self.node.read_bytes())
        (self.dest / 'node').chmod(0o755)
        self.baseline = {'a.mjs': digest(self.old), 'tclk_pin.json': digest(pin_data)}
        self.fingerprint = digest(b'D' * 32)
        self.config.write_text(json.dumps({'mode': 'DUMMY_OFFLINE', 'projectDid': recovery.PROJECT,
                                           'humanUid': 1000,
                                           'dummyCredentialSha256': self.fingerprint}) + '\n')
        self.config.chmod(0o644)
        (self.setup_root / 'before.json').write_text(json.dumps({
            'roles_existed': False, 'unit_existed': False, 'created_users': recovery.ROLES}))
        (self.setup_root / 'connection-before.json').write_text(json.dumps({
            'units_existed': False, 'deployment_existed': False,
            'human_uid': 1000, 'node_version': '22'}))
        (self.setup_root / 'dummy.cred').write_bytes(b'encrypted fixture')
        (self.setup_root / 'dummy.cred').chmod(0o600)
        (self.setup_root / 'result.json').write_text(json.dumps({
            'dummy_credential_sha256': self.fingerprint}))
        deployment = {'src/collaboration_agent/a.mjs': digest(self.old),
                      'src/collaboration_agent/tclk_pin.json': digest(pin_data)}
        (self.setup_root / 'deployment.json').write_text(json.dumps(deployment, indent=2) + '\n')
        self.accounts = {role: SimpleNamespace(pw_name=role, pw_uid=2000 + index,
                                                pw_gid=3000 + index,
                                                pw_shell='/usr/sbin/nologin')
                         for index, role in enumerate(recovery.ROLES)}
        self.groups = {role: SimpleNamespace(gr_gid=3000 + index, gr_mem=[])
                       for index, role in enumerate(recovery.ROLES)}
        self.calls = []
        self.systemd = SystemdModel(recovery.NAMES)
        patches = [
            patch.object(recovery, 'SOURCE', self.repo_source),
            patch.object(recovery, 'RUNTIME', self.runtime),
            patch.object(recovery, 'DEST', self.dest),
            patch.object(recovery, 'CONFIG', self.config),
            patch.object(recovery, 'UNITS', self.units),
            patch.object(recovery, 'ALT_UNITS', self.alt_units),
            patch.object(recovery, 'STATE', self.state),
            patch.object(recovery, 'ROOT', self.setup_root),
            patch.object(recovery, 'DEPLOYMENT', self.setup_root / 'deployment.json'),
            patch.object(recovery, 'PROBE_INSTALL', self.probe_install),
            patch.object(recovery, 'PROBE_UNIT', self.probe_unit),
            patch.object(recovery, 'BASELINE', self.baseline),
            patch.object(recovery.pwd, 'getpwnam', side_effect=self.accounts.__getitem__),
            patch.object(recovery.pwd, 'getpwall', return_value=list(self.accounts.values())),
            patch.object(recovery.grp, 'getgrnam', side_effect=self.groups.__getitem__),
            patch.object(recovery.grp, 'getgrall', return_value=list(self.groups.values())),
            patch.object(recovery, 'run', side_effect=self.fake_run),
            patch.object(recovery.setup, 'validate_result'),
            patch.object(recovery.setup, 'measure', side_effect=lambda fingerprint:
                         print(json.dumps({'external_writes': 0}))),
        ]
        original_trusted = recovery.trusted
        patches.append(patch.object(recovery, 'trusted',
                                    side_effect=lambda path, uid=0, **kw:
                                    original_trusted(path, uid=os.getuid(), **kw)))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        for name, data in recovery.render_units(1000).items():
            (self.units / name).write_bytes(data)
            (self.units / name).chmod(0o644)

    def fake_run(self, args, **kwargs):
        self.calls.append(list(args))
        if args[0] == 'systemd-creds':
            return SimpleNamespace(stdout=b'D' * 32)
        if args[0] == str(self.dest / 'node'):
            return SimpleNamespace(stdout=b'v22.99.0\n')
        if args[0] == 'systemctl' and (args[1] != 'show' or 'LoadState' in args):
            return self.systemd(args, **kwargs)
        if args[:2] == ['systemctl', 'show']:
            if args[2] == 'collab-boundary-probe.service':
                return SimpleNamespace(stdout=(f'FragmentPath={self.probe_unit}\nDropInPaths=\n').encode())
            return SimpleNamespace(stdout=(f'FragmentPath={self.units / args[2]}\nDropInPaths=\n').encode())
        return SimpleNamespace(stdout=b'')

    def diagnostic(self, **changes):
        value = {'schema': 1, 'phase': 'SOCKET_ACTIVATION',
                 'code': 'SOCKET_ACTIVATION_REQUIRED', 'invocationId': 'a' * 32}
        value.update(changes)
        path = self.state / 'startup-diagnostic.json'
        path.write_text(json.dumps(value) + '\n')
        path.chmod(0o600)
        return path

    def test_updates_only_reviewed_source_and_preserves_diagnostic(self):
        # The observed baseline failure creates this directory, then fails before
        # creating a lock or ledger file under Node's permission model.
        ledger = self.state / 'ledger'
        ledger.mkdir(mode=0o700)
        self.diagnostic()
        credential_before = (self.setup_root / 'dummy.cred').read_bytes()
        result = recovery.recover(self.node)
        self.assertEqual(result['updated_sources'], ['a.mjs'])
        self.assertTrue(result['evidence_preserved'])
        self.assertEqual((self.dest / 'src/collaboration_agent/a.mjs').read_bytes(), self.new)
        deployment = json.loads((self.setup_root / 'deployment.json').read_text())
        self.assertEqual(deployment['src/collaboration_agent/a.mjs'], digest(self.new))
        backups = list(self.setup_root.glob('smoke-recovery-*'))
        self.assertEqual(len(backups), 1)
        manifest = json.loads((backups[0] / 'manifest.json').read_text())
        archived = manifest[str(self.state / 'startup-diagnostic.json')]
        self.assertEqual((backups[0] / archived['backup']).read_bytes(),
                         (json.dumps({'schema': 1, 'phase': 'SOCKET_ACTIVATION',
                                      'code': 'SOCKET_ACTIVATION_REQUIRED',
                                      'invocationId': 'a' * 32}) + '\n').encode())
        self.assertFalse((self.state / 'startup-diagnostic.json').exists())
        self.assertTrue(ledger.is_dir())
        self.assertEqual(list(ledger.iterdir()), [])
        self.assertEqual((self.setup_root / 'dummy.cred').read_bytes(), credential_before)
        stop_socket = ['systemctl', 'stop'] + recovery.SOCKETS
        stop_service = ['systemctl', 'stop'] + recovery.SERVICES
        self.assertLess(self.calls.index(stop_socket), self.calls.index(stop_service))
        self.assertFalse(any(c[:2] == ['systemctl', 'reset-failed'] for c in self.calls))
        self.assertIn(['systemctl', 'start'] + recovery.SOCKETS, self.calls)
        recovery.setup.measure.assert_called_once_with(self.fingerprint)

    def populated(self):
        did = 'did:key:z6Mk' + 'A' * 44
        ledger = self.state / 'ledger'
        ledger.mkdir(mode=0o700)
        path = ledger / ('identity-' + digest(did.encode()) + '.json')
        path.write_text('{"fixture":"validated by signer"}')
        path.chmod(0o600)
        lock = path.with_suffix('.json.lock')
        lock.write_text(json.dumps({'pid': 99999999, 'did': did}))
        lock.chmod(0o600)
        return path, lock

    def test_post_smoke_resume_preserves_state_and_archives_dead_lock(self):
        state, lock = self.populated()
        original = state.read_bytes()
        with patch.object(recovery, 'resume_status', return_value=True):
            result = recovery.recover(self.node)
        self.assertEqual(result['state_mode'], 'reconciliation')
        self.assertIs(result['quota_consumed'], True)
        self.assertEqual(state.read_bytes(), original)
        self.assertFalse(lock.exists())
        archive = list(self.setup_root.glob('state-recovery-*'))[0]
        self.assertTrue((archive / 'dead-lock-0.json').exists())
        self.assertTrue((archive / 'before.json').exists())
        self.assertTrue((archive / 'after.json').exists())
        with patch.object(recovery, 'resume_status', return_value=True):
            self.assertTrue(recovery.recover(self.node)['recovered'])

    def test_live_owner_refused_without_moving_lock(self):
        state, lock = self.populated()
        with patch.object(recovery.os, 'kill', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'RECOVERY_OWNER_STILL_ALIVE'):
                recovery.recover(self.node)
        self.assertTrue(lock.exists())
        self.assertFalse(any(c[:2] == ['systemctl', 'start'] for c in self.calls))

    def test_invalid_runtime_state_keeps_ingress_closed_and_data(self):
        state, lock = self.populated()
        original = state.read_bytes()
        with patch.object(recovery, 'resume_status', side_effect=RuntimeError('RECOVERY_STATUS_INVALID')):
            with self.assertRaisesRegex(RuntimeError, 'RECOVERY_STATUS_INVALID'):
                recovery.recover(self.node)
        self.assertEqual(state.read_bytes(), original)
        self.assertEqual(self.calls[-2:], [['systemctl', 'stop'] + recovery.SOCKETS,
                                         ['systemctl', 'stop'] + recovery.SERVICES])

    def test_reset_failure_preserves_dead_locks_and_records_static_failure(self):
        state, lock = self.populated()
        original = state.read_bytes()
        real_run = self.fake_run
        def fail_reset(args, **kwargs):
            if args[:2] == ['systemctl', 'reset-failed']:
                raise RuntimeError('SERVICE_RESET_FAILED_REFUSED')
            result = real_run(args, **kwargs)
            if args == ['systemctl', 'stop'] + recovery.SERVICES:
                self.systemd.states['collab-signer.service'] = ('loaded', 'failed')
            return result
        with patch.object(recovery, 'run', side_effect=fail_reset):
            with self.assertRaisesRegex(RuntimeError, 'SERVICE_RESET_FAILED_REFUSED'):
                recovery.recover(self.node)
        self.assertTrue(lock.exists())
        self.assertEqual(state.read_bytes(), original)
        evidence = list(self.setup_root.glob('state-recovery-*'))[0]
        failure = json.loads((evidence / 'failure.json').read_text())
        self.assertEqual(failure['code'], 'SERVICE_RESET_FAILED_REFUSED')
        self.assertFalse(any(c[:2] == ['systemctl', 'start'] for c in self.calls))

    def test_real_mode_and_wrong_fingerprint_refused(self):
        original = json.loads(self.config.read_text())
        for value in [dict(original, mode='REAL'), dict(original, dummyCredentialSha256='0' * 64)]:
            self.config.write_text(json.dumps(value))
            self.assert_refused_without_stop('CONFIG_MISMATCH')

    def test_pending_or_orphan_custody_refused(self):
        custody = self.state / 'custody'
        custody.mkdir(mode=0o700)
        self.assert_refused_without_stop('RECOVERY_STATE_UNSAFE')

    def assert_refused_without_stop(self, code):
        original = (self.dest / 'src/collaboration_agent/a.mjs').read_bytes()
        with self.assertRaisesRegex(RuntimeError, code):
            recovery.recover(self.node)
        self.assertEqual((self.dest / 'src/collaboration_agent/a.mjs').read_bytes(), original)
        self.assertFalse(any(call[:2] == ['systemctl', 'stop'] for call in self.calls))
        self.assertFalse(list(self.setup_root.glob('smoke-recovery-*')))
        recovery.setup.measure.assert_not_called()

    def test_populated_signer_state_is_refused_and_preserved(self):
        ledger = self.state / 'ledger'
        ledger.mkdir()
        ledger.chmod(0o700)
        (ledger / 'identity.json').write_text('custody evidence')
        (ledger / 'identity.json').chmod(0o600)
        self.assert_refused_without_stop('RECOVERY_STATE_UNSAFE')
        self.assertEqual((ledger / 'identity.json').read_text(), 'custody evidence')

    def test_failed_boundary_remeasurement_keeps_ingress_closed(self):
        recovery.setup.measure.side_effect = RuntimeError('BOUNDARY_MEASUREMENT_FAILED')
        with self.assertRaisesRegex(RuntimeError, 'BOUNDARY_MEASUREMENT_FAILED'):
            recovery.recover(self.node)
        self.assertIn(['systemctl', 'stop'] + recovery.SOCKETS, self.calls)
        self.assertNotIn(['systemctl', 'start'] + recovery.SOCKETS, self.calls)

    def test_unclosed_diagnostic_vocabulary_is_refused(self):
        path = self.diagnostic(code='secret-looking arbitrary text')
        self.assert_refused_without_stop('INVALID_STARTUP_DIAGNOSTIC')
        self.assertTrue(path.exists())

    def test_unknown_installed_source_is_refused(self):
        (self.dest / 'src/collaboration_agent/a.mjs').write_bytes(b'unknown')
        self.assert_refused_without_stop('UNKNOWN_INSTALLED_SOURCE')

    def test_dropin_or_alternate_unit_is_refused(self):
        alternate = self.alt_units[0] / recovery.NAMES[0]
        alternate.write_text('override')
        self.assert_refused_without_stop('ALTERNATE_UNIT_REFUSED')

    def test_interrupted_forward_update_resumes_without_rollback(self):
        original_atomic = recovery.atomic_write
        failed = False

        def interrupt(path, data, mode):
            nonlocal failed
            if path == self.setup_root / 'deployment.json' and not failed:
                failed = True
                raise OSError('simulated interruption')
            return original_atomic(path, data, mode)

        with patch.object(recovery, 'atomic_write', side_effect=interrupt):
            with self.assertRaises(OSError):
                recovery.recover(self.node)
        self.assertEqual((self.dest / 'src/collaboration_agent/a.mjs').read_bytes(), self.new)
        stale = json.loads((self.setup_root / 'deployment.json').read_text())
        self.assertEqual(stale['src/collaboration_agent/a.mjs'], digest(self.old))
        result = recovery.recover(self.node)
        self.assertEqual(result['updated_sources'], [])
        current = json.loads((self.setup_root / 'deployment.json').read_text())
        self.assertEqual(current['src/collaboration_agent/a.mjs'], digest(self.new))
        recovery.setup.measure.assert_called_once_with(self.fingerprint)

    def test_sigkill_before_replace_preserves_temp_outside_deployment_and_resumes(self):
        target = self.dest / 'src/collaboration_agent/a.mjs'
        script = '''import importlib.util,os,pathlib,signal,sys
spec=importlib.util.spec_from_file_location('recovery',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.ROOT=pathlib.Path(sys.argv[2])
def killed_replace(source,target): os.kill(os.getpid(),signal.SIGKILL)
m.os.replace=killed_replace
m.atomic_write(pathlib.Path(sys.argv[3]),sys.stdin.buffer.read(),0o644)
'''
        child = subprocess.run([sys.executable, '-I', '-c', script,
                                str(DEPLOY / 'recover_smoke.py'), str(self.setup_root), str(target)],
                               input=self.new, capture_output=True)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertEqual(child.stdout + child.stderr, b'')
        self.assertEqual(target.read_bytes(), self.old)
        leftovers = list(self.setup_root.glob('.smoke-update-*'))
        self.assertEqual(len(leftovers), 1)
        self.assertEqual(leftovers[0].read_bytes(), self.new)
        self.assertFalse(list(self.dest.rglob('.smoke-update-*')))
        result = recovery.recover(self.node)
        self.assertTrue(result['recovered'])
        self.assertEqual(target.read_bytes(), self.new)
        self.assertTrue(leftovers[0].exists())  # preserve; never execute/delete unknown prior staging.

    def test_reverse_manifest_order_is_refused(self):
        manifest = json.loads((self.setup_root / 'deployment.json').read_text())
        manifest['src/collaboration_agent/a.mjs'] = digest(self.new)
        (self.setup_root / 'deployment.json').write_text(json.dumps(manifest))
        self.assert_refused_without_stop('DEPLOYMENT_MANIFEST_MISMATCH')


class HelperTest(unittest.TestCase):
    def test_trusted_rejects_symlink_and_writable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'target'
            target.write_text('x')
            link = root / 'link'
            link.symlink_to(target)
            with self.assertRaises(RuntimeError):
                recovery.trusted(link, uid=os.getuid())
            target.chmod(0o666)
            with self.assertRaises(RuntimeError):
                recovery.trusted(target, uid=os.getuid())


if __name__ == '__main__':
    unittest.main()
