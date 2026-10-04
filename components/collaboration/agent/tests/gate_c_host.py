"""Offline host-boundary tests for Gate C; no sudo, seed, service or network."""

import importlib.util
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]


def load_gate_c():
    spec = importlib.util.spec_from_file_location(
        'gate_c_host_under_test', REPO / 'deploy/connection/gate_c.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GateCHostContractTest(unittest.TestCase):
    ATTEMPT = '12345678-1234-4123-8123-123456789abc'
    DIGEST = 'a' * 64
    ENVELOPE = 'b' * 64

    def setUp(self):
        self.gate = load_gate_c()
        required = ('c1', 'c2', 'helper', 'encrypt_preimage')
        missing = [name for name in required if not hasattr(self.gate, name)]
        if missing:
            self.skipTest('Gate C implementation pending: ' + ','.join(missing))
        self.temp = tempfile.TemporaryDirectory(prefix='gate-c-host-test-')
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def observation(self, nonce='7', verified=1_800_000_000_000):
        return {
            'did': self.gate.r.PROJECT, 'room': 'tclk-offers',
            'observedNonce': nonce, 'observedNone': nonce is None,
            'verifiedAtMs': verified,
            'source': {'generation': 1, 'rawSha256': 'c' * 64},
            'coverage': {'basis': 'fixed-profile'},
        }

    def document(self):
        return {'version': 1, 'kind': 'FIRST_PAPER_ACCEPT',
                'digest': self.DIGEST, 'observation': self.observation(),
                'packet': {'approvalDigest': 'd' * 64}}

    def snapshot(self, nonce='7'):
        return {'version': 1, 'observation': self.observation(nonce), 'records': []}

    def test_c1_stops_after_public_artifact_without_handoff_or_host_activation(self):
        candidate = {'offerId': 'offer-1'}
        document = self.document()
        helper_calls = []

        def helper(mode, value):
            helper_calls.append((mode, value))
            if mode == 'candidates':
                return [candidate]
            if mode == 'freeze':
                return {'document': document, 'preimage': '0x' + '11' * 32}
            if mode == 'validate':
                return {'document': document}
            self.fail('unexpected helper mode ' + mode)

        encrypted = []
        with patch.object(self.gate, 'ROOT', self.root / 'gate-c'), \
             patch.object(self.gate, 'baseline_preflight', return_value=1000), \
             patch.object(self.gate, 'acquire_snapshot', side_effect=[self.snapshot(), self.snapshot()]), \
             patch.object(self.gate, 'helper', side_effect=helper), \
             patch.object(self.gate, 'tty_prompt', return_value='offer-1'), \
             patch.object(self.gate, 'encrypt_preimage',
                          side_effect=lambda value, path: encrypted.append((value, path))), \
             patch.object(self.gate, 'handoff_seed') as handoff, \
             patch.object(self.gate, 'apply_changes') as apply_changes, \
             patch.object(self.gate.r, 'trusted', side_effect=lambda path, **_kw: Path(path)), \
             patch.object(self.gate.r, 'atomic_write',
                          side_effect=lambda path, data, mode: Path(path).write_bytes(bytes(data))), \
             patch.object(self.gate.r.setup, 'sync_dir'):
            result = self.gate.c1()

        self.assertEqual(result['stage'], 'C1_STOPPED')
        self.assertEqual(result['externalWrites'], 0)
        self.assertEqual([call[0] for call in helper_calls],
                         ['candidates', 'freeze', 'validate'])
        # The Node adapter's candidates mode accepts the snapshot itself; it
        # has no wrapper field or alternate acquisition source.
        self.assertEqual(helper_calls[0][1], self.snapshot())
        self.assertEqual(len(encrypted), 1)
        handoff.assert_not_called()
        apply_changes.assert_not_called()

    def test_final_revalidate_returns_to_c1_without_admission_and_cleans_up(self):
        attempt = self.root / self.ATTEMPT
        attempt.mkdir()
        document = self.document()
        fake_config = self.root / 'live-config.json'
        calls = []

        def helper(mode, _value):
            calls.append(mode)
            if mode == 'arm':
                return {'armed': True, 'packetDigest': self.DIGEST}
            if mode == 'revalidate':
                raise self.gate.ReturnC1('RETURN_C1')
            self.fail('unexpected helper mode ' + mode)

        with patch.object(self.gate, 'baseline_preflight', return_value=1000), \
             patch.object(self.gate, 'load_artifact', return_value=(attempt, document)), \
             patch.object(self.gate, 'prepare', return_value=[(fake_config, b'config')]), \
             patch.object(self.gate, 'exclusive_marker'), \
             patch.object(self.gate, 'apply_changes'), \
             patch.object(self.gate, 'handoff_seed') as handoff, \
             patch.object(self.gate, 'tty_prompt', return_value='SEND ACCEPT ' + self.DIGEST), \
             patch.object(self.gate, 'acquire_snapshot', return_value=self.snapshot()), \
             patch.object(self.gate, 'helper', side_effect=helper), \
             patch.object(self.gate, 'rollback') as rollback, \
             patch.object(self.gate, 'run', return_value=SimpleNamespace(stdout=b'')), \
             patch.object(self.gate.r, 'CONFIG', fake_config), \
             patch.object(self.gate.r, 'atomic_write'), \
             patch.object(self.gate.r.setup, 'reset_failed'), \
             patch.object(self.gate, 'save'):
            result = self.gate.c2(self.ATTEMPT)
        self.assertEqual(result['stage'], 'RETURN_C1')
        self.assertEqual(result['externalWrites'], 0)
        self.assertTrue(result['credentialHandoff'])
        self.assertEqual(calls, ['arm', 'revalidate'])
        handoff.assert_called_once_with()
        rollback.assert_called_once_with(self.ATTEMPT)

    def test_loading_artifact_uses_historical_validation(self):
        gate_root = self.root / 'gate-c'
        attempt = gate_root / self.ATTEMPT
        attempt.mkdir(parents=True)
        document = self.document()
        (attempt / 'packet.json').write_text(json.dumps(document))
        (attempt / 'accept-preimage.cred').write_bytes(b'encrypted')
        with patch.object(self.gate, 'ROOT', gate_root), \
             patch.object(self.gate.r, 'trusted', side_effect=lambda path, **_kw: Path(path)), \
             patch.object(self.gate, 'helper',
                          return_value={'document': document}) as helper:
            loaded_attempt, loaded = self.gate.load_artifact(self.ATTEMPT)
        self.assertEqual(loaded_attempt, attempt)
        self.assertEqual(loaded, document)
        helper.assert_called_once_with('validate', {'document': document})

    def test_real_gate_b_dummy_config_has_no_external_write_field(self):
        setup_root = self.root / 'setup'
        gate_b = setup_root / 'gate-b'
        gate_b.mkdir(parents=True)
        (gate_b / 'result.json').write_text(json.dumps({
            'passed': True, 'rollback': 'DUMMY_CONFIG_ALL_STOPPED'}))
        config = self.root / 'config.json'
        config.write_text(json.dumps({'mode': 'DUMMY_OFFLINE', 'humanUid': 1000,
                                      'dummyCredentialSha256': 'a' * 64}))
        credential = setup_root / 'project-seed.cred'
        public_packet = self.root / 'first-accept.json'
        dropins = [self.root / 'signer.d/gate-c.conf',
                   self.root / 'transport.d/gate-c.conf']
        trusted = lambda path, **_kw: Path(path)
        with patch.dict(self.gate.os.environ, {'SUDO_UID': '1000'}, clear=False), \
             patch.object(self.gate.r, 'ROOT', setup_root), \
             patch.object(self.gate.r, 'CONFIG', config), \
             patch.object(self.gate, 'CREDENTIAL', credential), \
             patch.object(self.gate, 'PUBLIC_PACKET', public_packet), \
             patch.object(self.gate, 'DROPINS', dropins), \
             patch.object(self.gate.r, 'trusted', side_effect=trusted), \
             patch.object(self.gate, 'stopped', return_value=True), \
             patch.object(self.gate, 'no_active_attempt'), \
             patch.object(self.gate, 'deployment_preflight'), \
             patch.object(self.gate, 'safe_empty_real_state'), \
             patch.object(self.gate.r, 'account_preflight'), \
             patch.object(self.gate.r, 'config_preflight'), \
             patch.object(self.gate.r, 'unit_preflight'), \
             patch.object(self.gate, 'run',
                          return_value=SimpleNamespace(stdout=b'v22.0.0\n')):
            self.assertEqual(self.gate.baseline_preflight(), 1000)

    def test_true_write_policy_stops_and_removes_seed_immediately(self):
        config = self.root / 'config.json'
        config.write_text(json.dumps({'mode': 'REAL_ACCEPT_PREPARATION',
                                      'externalWriteEnabled': True}))
        stopped = []
        removed = []
        with patch.object(self.gate.r, 'CONFIG', config), \
             patch.object(self.gate.r, 'trusted', side_effect=lambda path, **_kw: Path(path)), \
             patch.object(self.gate, 'stop', side_effect=lambda: stopped.append(True)), \
             patch.object(self.gate, 'remove_seed', side_effect=lambda: removed.append(True)):
            with self.assertRaisesRegex(RuntimeError, 'EXTERNAL_WRITE_ENABLED_STOPPED'):
                self.gate.baseline_preflight()
        self.assertEqual(stopped, [True])
        self.assertEqual(removed, [True])

    def test_gate_b_checkpoint_allows_only_empty_project_ledger(self):
        state = self.root / 'signer-state'
        ledger = state / 'ledger'
        ledger.mkdir(parents=True, mode=0o700)
        setup_root = self.root / 'setup'
        gate_b = setup_root / 'gate-b'
        gate_b.mkdir(parents=True)
        dummy = ledger / ('identity-' + 'd' * 64 + '.json')
        dummy.write_bytes(b'unchanged dummy ledger')
        project = ledger / ('identity-' + self.gate.r.sha(
            self.gate.r.PROJECT.encode()) + '.json')
        value = {'version': 1, 'mode': 'TEST_EPHEMERAL',
                 'did': self.gate.r.PROJECT, 'revision': 1,
                 'quotaConsumed': False, 'admission': None,
                 'state': None, 'actions': []}
        project.write_text(json.dumps({'value': value,
                                       'checksum': self.gate._json_sha(value)},
                                      separators=(',', ':')))
        before = {'ledger/' + dummy.name: hashlib.sha256(
            dummy.read_bytes()).hexdigest()}
        (gate_b / 'state-before.json').write_text(json.dumps(before))

        def hashes():
            return {str(path.relative_to(state)): hashlib.sha256(
                    path.read_bytes()).hexdigest()
                    for path in state.rglob('*') if path.is_file()}

        real_lexists = self.gate.os.path.lexists
        def lexists(path):
            if str(path) == '/var/lib/collab-transport':
                return False
            return real_lexists(path)

        gate_c_root = setup_root / 'gate-c'
        prior = gate_c_root / self.ATTEMPT
        prior.mkdir(parents=True)
        marker = prior / 'c2-attempt.json'
        marker.write_text('{"retry":false}')
        (prior / 'rollback.json').write_text(json.dumps({
            'passed': True, 'finalState': 'DUMMY_CONFIG_ALL_STOPPED'}))
        evidence_before = {p.name: p.read_bytes() for p in prior.iterdir()}
        account = SimpleNamespace(pw_uid=1234)
        with patch.object(self.gate, 'ROOT', gate_c_root), \
             patch.object(self.gate.r, 'STATE', state), \
             patch.object(self.gate.r, 'ROOT', setup_root), \
             patch.object(self.gate.r, 'trusted', side_effect=lambda path, **_kw: Path(path)), \
             patch.object(self.gate.g, 'state_hashes', side_effect=hashes), \
             patch.object(self.gate.pwd, 'getpwnam', return_value=account), \
             patch.object(self.gate.os.path, 'lexists', side_effect=lexists):
            # Completed pre-commit evidence does not override actual empty state.
            self.gate.no_active_attempt()
            self.assertEqual(self.gate.safe_empty_real_state(), value)
            self.assertEqual({p.name: p.read_bytes() for p in prior.iterdir()}, evidence_before)
            admitted = {**value, 'quotaConsumed': True, 'admission': {'contract': 'pending'}}
            project.write_text(json.dumps({'value': admitted,
                'checksum': self.gate._json_sha(admitted)}, separators=(',', ':')))
            self.gate.no_active_attempt()  # marker/rollback unchanged; not the authority
            with self.assertRaisesRegex(RuntimeError, 'REAL_LEDGER_NOT_EMPTY'):
                self.gate.safe_empty_real_state()
            project.write_text(json.dumps({'value': value,
                'checksum': self.gate._json_sha(value)}, separators=(',', ':')))
            self.assertEqual({p.name: p.read_bytes() for p in prior.iterdir()}, evidence_before)
            orphan = state / 'real-accept-custody'
            orphan.mkdir()
            with self.assertRaisesRegex(RuntimeError, 'REAL_STATE_UNCERTAIN|REAL_CUSTODY_EXISTS'):
                self.gate.safe_empty_real_state()
            orphan.rmdir()
            extra = ledger / ('identity-' + 'e' * 64 + '.json')
            extra.write_bytes(b'unknown identity')
            with self.assertRaisesRegex(RuntimeError, 'REAL_STATE_UNCERTAIN'):
                self.gate.safe_empty_real_state()

    def test_exact_approval_mismatch_after_arm_cleans_up_without_public_read(self):
        attempt = self.root / self.ATTEMPT
        attempt.mkdir()
        document = self.document()
        fake_config = self.root / 'live-config.json'
        with patch.object(self.gate, 'baseline_preflight', return_value=1000), \
             patch.object(self.gate, 'load_artifact', return_value=(attempt, document)), \
             patch.object(self.gate, 'prepare', return_value=[(fake_config, b'config')]), \
             patch.object(self.gate, 'apply_changes'), \
             patch.object(self.gate, 'tty_prompt', return_value='SEND ACCEPT wrong'), \
             patch.object(self.gate, 'handoff_seed') as handoff, \
             patch.object(self.gate, 'exclusive_marker') as marker, \
             patch.object(self.gate, 'acquire_snapshot') as acquire, \
             patch.object(self.gate, 'helper', return_value={
                 'armed': True, 'packetDigest': self.DIGEST}), \
             patch.object(self.gate, 'rollback') as rollback, \
             patch.object(self.gate, 'run', return_value=SimpleNamespace(stdout=b'')), \
             patch.object(self.gate.r, 'CONFIG', fake_config), \
             patch.object(self.gate.r, 'atomic_write'), \
             patch.object(self.gate.r.setup, 'reset_failed'), \
             patch.object(self.gate, 'save'):
            result = self.gate.c2(self.ATTEMPT)
        self.assertFalse(result['passed'])
        self.assertEqual(result['externalWrites'], 0)
        handoff.assert_called_once_with()
        marker.assert_called_once_with(attempt, document)
        acquire.assert_not_called()
        rollback.assert_called_once_with(self.ATTEMPT)

    def test_preadmission_arm_failure_cleans_up_and_remains_provably_no_write(self):
        attempt = self.root / self.ATTEMPT
        attempt.mkdir()
        document = self.document()
        fake_config = self.root / 'live-config.json'
        with patch.object(self.gate, 'baseline_preflight', return_value=1000), \
             patch.object(self.gate, 'load_artifact', return_value=(attempt, document)), \
             patch.object(self.gate, 'prepare', return_value=[(fake_config, b'config')]), \
             patch.object(self.gate, 'exclusive_marker'), \
             patch.object(self.gate, 'apply_changes'), \
             patch.object(self.gate, 'handoff_seed'), \
             patch.object(self.gate, 'tty_prompt') as prompt, \
             patch.object(self.gate, 'acquire_snapshot') as acquire, \
             patch.object(self.gate, 'helper', side_effect=RuntimeError('arm failed')), \
             patch.object(self.gate, 'rollback') as rollback, \
             patch.object(self.gate, 'run', return_value=SimpleNamespace(stdout=b'')), \
             patch.object(self.gate.r, 'CONFIG', fake_config), \
             patch.object(self.gate.r, 'atomic_write'), \
             patch.object(self.gate.r.setup, 'reset_failed'), \
             patch.object(self.gate, 'save'):
            result = self.gate.c2(self.ATTEMPT)
        self.assertEqual(result['status'], 'HUMAN_STOP')
        self.assertEqual(result['externalWrites'], 0)
        prompt.assert_not_called()
        acquire.assert_not_called()
        rollback.assert_called_once_with(self.ATTEMPT)

    def test_success_calls_execute_once_then_match_and_always_rolls_back(self):
        attempt = self.root / self.ATTEMPT
        attempt.mkdir()
        document = self.document()
        calls = []
        events = []

        def helper(mode, value):
            events.append(mode)
            calls.append((mode, value))
            if mode == 'arm':
                return {'armed': True, 'packetDigest': self.DIGEST}
            if mode == 'revalidate':
                return {'validated': True}
            if mode == 'execute':
                return {'status': 'AMBIGUOUS', 'stopped': True, 'retry': False,
                        'envelopeDigest': self.ENVELOPE,
                        'packetDigest': self.DIGEST}
            if mode == 'match':
                return {'status': 'RECONCILED_PRESENT', 'stopped': True,
                        'retry': False}
            self.fail('unexpected helper mode ' + mode)

        fake_r = self.gate.r
        fake_config = self.root / 'live-config.json'
        changes = [(fake_config, b'config')]
        final_snapshot = self.snapshot('8')
        reconciliation = self.snapshot('9')

        def prompt(*_args):
            events.append('prompt')
            return 'SEND ACCEPT ' + self.DIGEST

        snapshots = iter((final_snapshot, reconciliation))
        def acquire():
            events.append('acquire')
            return next(snapshots)

        with patch.object(self.gate, 'baseline_preflight', return_value=1000), \
             patch.object(self.gate, 'load_artifact', return_value=(attempt, document)), \
             patch.object(self.gate, 'tty_prompt', side_effect=prompt), \
             patch.object(self.gate, 'prepare', return_value=changes), \
             patch.object(self.gate, 'exclusive_marker'), \
             patch.object(self.gate, 'apply_changes'), \
             patch.object(self.gate, 'handoff_seed'), \
             patch.object(self.gate, 'acquire_snapshot', side_effect=acquire), \
             patch.object(self.gate, 'helper', side_effect=helper), \
             patch.object(self.gate, 'rollback') as rollback, \
             patch.object(self.gate, 'run', return_value=SimpleNamespace(stdout=b'')), \
             patch.object(fake_r, 'CONFIG', fake_config), \
             patch.object(fake_r, 'atomic_write'), \
             patch.object(fake_r.setup, 'reset_failed'), \
             patch.object(self.gate, 'save'):
            result = self.gate.c2(self.ATTEMPT)
        self.assertTrue(result['passed'])
        self.assertEqual(result['status'], 'RECONCILED_PRESENT')
        self.assertEqual(events, ['arm', 'prompt', 'acquire', 'revalidate',
                                  'execute', 'acquire', 'match'])
        self.assertEqual(calls[2][1]['finalSnapshot'], final_snapshot)
        rollback.assert_called_once_with(self.ATTEMPT)

    def test_lost_execute_response_reports_unknown_and_cleans_up(self):
        attempt = self.root / self.ATTEMPT
        attempt.mkdir()
        document = self.document()
        fake_config = self.root / 'live-config.json'

        def helper(mode, _value):
            if mode == 'arm':
                return {'armed': True, 'packetDigest': self.DIGEST}
            if mode == 'revalidate':
                return {'validated': True}
            if mode == 'execute':
                raise RuntimeError('lost response')
            self.fail('unexpected helper mode ' + mode)

        with patch.object(self.gate, 'baseline_preflight', return_value=1000), \
             patch.object(self.gate, 'load_artifact', return_value=(attempt, document)), \
             patch.object(self.gate, 'tty_prompt', return_value='SEND ACCEPT ' + self.DIGEST), \
             patch.object(self.gate, 'prepare', return_value=[(fake_config, b'config')]), \
             patch.object(self.gate, 'exclusive_marker'), \
             patch.object(self.gate, 'apply_changes'), \
             patch.object(self.gate, 'handoff_seed'), \
             patch.object(self.gate, 'acquire_snapshot', return_value=self.snapshot()), \
             patch.object(self.gate, 'helper', side_effect=helper), \
             patch.object(self.gate, 'rollback') as rollback, \
             patch.object(self.gate, 'run', return_value=SimpleNamespace(stdout=b'')), \
             patch.object(self.gate.r, 'CONFIG', fake_config), \
             patch.object(self.gate.r, 'atomic_write'), \
             patch.object(self.gate.r.setup, 'reset_failed'), \
             patch.object(self.gate, 'save'):
            result = self.gate.c2(self.ATTEMPT)
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'AMBIGUOUS')
        self.assertEqual(result['externalWrites'], 'UNKNOWN')
        rollback.assert_called_once_with(self.ATTEMPT)

    def test_execute_proved_preadmission_return_c1_is_not_ambiguous(self):
        attempt = self.root / self.ATTEMPT
        attempt.mkdir()
        document = self.document()
        fake_config = self.root / 'live-config.json'

        def helper(mode, _value):
            if mode == 'arm':
                return {'armed': True, 'packetDigest': self.DIGEST}
            if mode == 'revalidate':
                return {'validated': True}
            if mode == 'execute':
                raise self.gate.ReturnC1('RETURN_C1')
            self.fail('unexpected helper mode ' + mode)

        with patch.object(self.gate, 'baseline_preflight', return_value=1000), \
             patch.object(self.gate, 'load_artifact', return_value=(attempt, document)), \
             patch.object(self.gate, 'tty_prompt', return_value='SEND ACCEPT ' + self.DIGEST), \
             patch.object(self.gate, 'prepare', return_value=[(fake_config, b'config')]), \
             patch.object(self.gate, 'exclusive_marker'), \
             patch.object(self.gate, 'apply_changes'), \
             patch.object(self.gate, 'handoff_seed'), \
             patch.object(self.gate, 'acquire_snapshot', return_value=self.snapshot()), \
             patch.object(self.gate, 'helper', side_effect=helper), \
             patch.object(self.gate, 'rollback') as rollback, \
             patch.object(self.gate, 'run', return_value=SimpleNamespace(stdout=b'')), \
             patch.object(self.gate.r, 'CONFIG', fake_config), \
             patch.object(self.gate.r, 'atomic_write'), \
             patch.object(self.gate.r.setup, 'reset_failed'), \
             patch.object(self.gate, 'save'):
            result = self.gate.c2(self.ATTEMPT)
        self.assertEqual(result['stage'], 'RETURN_C1')
        self.assertEqual(result['status'], 'RETURN_C1')
        self.assertEqual(result['externalWrites'], 0)
        rollback.assert_called_once_with(self.ATTEMPT)

    def test_exclusive_attempt_marker_refuses_any_retry(self):
        attempt = self.root / self.ATTEMPT
        attempt.mkdir()
        document = self.document()
        with patch.object(self.gate.r.setup, 'sync_dir'):
            self.gate.exclusive_marker(attempt, document)
            with self.assertRaisesRegex(RuntimeError, 'C2_ALREADY_ATTEMPTED'):
                self.gate.exclusive_marker(attempt, document)
        marker = json.loads((attempt / 'c2-attempt.json').read_text())
        self.assertEqual(marker, {'version': 1, 'packetDigest': self.DIGEST,
                                  'retry': False})

    def test_rolled_back_marker_allows_new_packet_but_unfinished_refuses(self):
        gate_root = self.root / 'gate-c'
        first = gate_root / self.ATTEMPT
        second = gate_root / '22345678-1234-4123-8123-123456789abc'
        first.mkdir(parents=True)
        second.mkdir()
        trusted = lambda path, **_kw: Path(path)
        with patch.object(self.gate, 'ROOT', gate_root), \
             patch.object(self.gate.r, 'trusted', side_effect=trusted):
            (first / 'c2-attempt.json').write_text('{}')
            (first / 'rollback.json').write_text(json.dumps({
                'passed': True, 'finalState': 'DUMMY_CONFIG_ALL_STOPPED'}))
            self.gate.no_active_attempt()
            (first / 'rollback.json').unlink()
            with self.assertRaisesRegex(RuntimeError, 'UNFINISHED_C2_TRANSACTION'):
                self.gate.no_active_attempt()
            (first / 'c2-attempt.json').unlink()
            (first / 'transaction.json').write_text('{}')
            with self.assertRaisesRegex(RuntimeError, 'UNFINISHED_C2_TRANSACTION'):
                self.gate.no_active_attempt()

    def test_rollback_restores_write_off_before_unrelated_source_conflict(self):
        gate_root = self.root / 'gate-c'
        attempt = gate_root / self.ATTEMPT
        attempt.mkdir(parents=True, mode=0o700)
        destination = self.root / 'dest'
        source_dir = destination / 'src/collaboration_agent'
        source_dir.mkdir(parents=True)
        source = source_dir / 'connection_runtime.mjs'
        source.write_bytes(b'unexpected operator edit')
        config = self.root / 'config.json'
        before_config = b'{"mode":"DUMMY_OFFLINE"}\n'
        after_config = b'{"mode":"REAL_ACCEPT_PREPARATION","externalWriteEnabled":true}\n'
        config.write_bytes(after_config)
        (attempt / '0.before').write_bytes(b'old source')
        (attempt / '1.before').write_bytes(before_config)
        transaction = {'entries': [
            {'path': str(source), 'backup': '0.before',
             'before': self.gate.r.sha(b'old source'),
             'after': self.gate.r.sha(b'new source')},
            {'path': str(config), 'backup': '1.before',
             'before': self.gate.r.sha(before_config),
             'after': self.gate.r.sha(after_config)},
        ]}
        (attempt / 'transaction.json').write_text(json.dumps(transaction))
        public_packet = self.root / 'first-accept.json'
        credential = self.root / 'project-seed.cred'
        dropins = [self.root / 'signer.d/gate-c.conf',
                   self.root / 'transport.d/gate-c.conf']

        def atomic_write(path, data, mode):
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes(data))

        with patch.object(self.gate, 'ROOT', gate_root), \
             patch.object(self.gate.r, 'DEST', destination), \
             patch.object(self.gate.r, 'CONFIG', config), \
             patch.object(self.gate, 'PUBLIC_PACKET', public_packet), \
             patch.object(self.gate, 'CREDENTIAL', credential), \
             patch.object(self.gate, 'DROPINS', dropins), \
             patch.object(self.gate.r, 'trusted', side_effect=lambda path, **_kw: Path(path)), \
             patch.object(self.gate.r, 'atomic_write', side_effect=atomic_write), \
             patch.object(self.gate, 'stop'), \
             patch.object(self.gate, 'run'), \
             patch.object(self.gate.r.setup, 'sync_dir'), \
             patch.object(self.gate.r.setup, 'reset_failed'):
            with self.assertRaisesRegex(RuntimeError, 'ROLLBACK_FILE_CHANGED'):
                self.gate.rollback(self.ATTEMPT)
        self.assertEqual(config.read_bytes(), before_config)

    def test_helper_passes_packet_on_stdin_not_argv_or_environment(self):
        document = self.document()
        completed = SimpleNamespace(returncode=0,
                                    stdout=json.dumps({'document': document}).encode())
        with patch.object(self.gate.subprocess, 'run', return_value=completed) as run:
            self.gate.helper('validate', {'document': document})
        args, kwargs = run.call_args
        self.assertEqual(args[0][-1], 'validate')
        self.assertNotIn(self.DIGEST, ' '.join(args[0]))
        self.assertNotIn(self.DIGEST, json.dumps(kwargs['env']))
        self.assertIn(self.DIGEST.encode(), kwargs['input'])

    def test_helper_parses_only_typed_preadmission_return_c1(self):
        returned = SimpleNamespace(returncode=1, stdout=json.dumps({
            'status': 'RETURN_C1', 'retry': False}).encode())
        with patch.object(self.gate.subprocess, 'run', return_value=returned):
            with self.assertRaises(self.gate.ReturnC1):
                self.gate.helper('revalidate', {
                    'document': self.document(), 'snapshot': self.snapshot()})
            with self.assertRaisesRegex(RuntimeError, 'GATE_C_HELPER_FAILED'):
                self.gate.helper('arm', {'document': self.document()})

    def test_preimage_encryption_uses_raw_stdin_only(self):
        destination = self.root / 'preimage.cred'
        completed = SimpleNamespace(returncode=0, stdout=b'encrypted')
        writes = []
        with patch.object(self.gate.subprocess, 'run', return_value=completed) as run, \
             patch.object(self.gate.r, 'atomic_write',
                          side_effect=lambda path, data, mode: writes.append((path, bytes(data), mode))):
            self.gate.encrypt_preimage('0x' + '22' * 32, destination)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ['/usr/bin/systemd-creds', 'encrypt', '--with-key=host',
                                   '--name=accept-preimage', '-', '-'])
        self.assertEqual(bytes(kwargs['input']), b'\0' * 32)  # erased after subprocess return
        self.assertNotIn('22' * 32, ' '.join(args[0]))
        self.assertEqual(writes, [(destination, b'encrypted', 0o600)])


if __name__ == '__main__':
    unittest.main()
