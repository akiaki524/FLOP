"""Real empty-identity restart regressions; fake seed and rootless offline services only."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from connection_real_activation import NODE, ROOT, RealFixture


class EmptyIdentityRestartTest(unittest.TestCase):
    def stop_services(self, fixture):
        children = fixture.children
        fixture.children = []
        for _, child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for role, child in children:
            try:
                stdout, stderr = child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(stdout, b'', role + '_STDOUT_NOT_SILENT')
            self.assertEqual(stderr, b'', role + '_STDERR_NOT_SILENT')

    def identity_ledger(self, fixture):
        ledgers = list((fixture.state / 'ledger').glob('identity-*.json'))
        self.assertEqual(len(ledgers), 1)
        return ledgers[0]

    def assert_startup_rejected(self, fixture):
        with self.assertRaisesRegex(AssertionError, 'REAL_SERVICE_START_FAILED_signer'):
            fixture.startup()

    def add_distinct_identity_ledger(self, fixture):
        script = """
import {official} from './src/collaboration_agent/pilot_protocol.mjs';
import {AsyncPilotLedger} from './src/collaboration_agent/pilot_ledger.mjs';
const {signing}=await official();
const did=signing.signerFromSeed(Buffer.alloc(32,66)).did;
const ledger=await AsyncPilotLedger.open(did,{root:process.argv[1],create:true});
await ledger.close();
"""
        subprocess.run([NODE, '--input-type=module', '-e', script, str(fixture.state / 'ledger')],
                       cwd=ROOT, check=True, capture_output=True)

    def set_send_attempted(self, ledger, did):
        script = """
import {readPublicState, atomicPublicState} from './src/collaboration_agent/pilot_ledger.mjs';
const path=process.argv[1], did=process.argv[2];
const value=readPublicState(path,did);
value.actions[0].status='SEND_ATTEMPTED';
value.actions[0].envelopeDigest='a'.repeat(64);
value.revision++;
atomicPublicState(path,value);
"""
        subprocess.run([NODE, '--input-type=module', '-e', script, str(ledger), did],
                       cwd=ROOT, check=True, capture_output=True)

    def test_gate_b_identity_restart_can_admit_and_prepare_without_duplicate_identity(self):
        with tempfile.TemporaryDirectory(prefix='real-empty-restart-') as root:
            f = RealFixture(root, enabled=False)
            try:
                f.startup()
                ledger = self.identity_ledger(f)
                initial = json.loads(ledger.read_text())['value']
                self.assertEqual((initial['did'], initial['quotaConsumed'], initial['admission'],
                                  initial['state'], initial['actions']),
                                 (f.did, False, None, None, []))
                self.stop_services(f)
                # Gate B cleanup removes only the delivered credential. A
                # distinct DUMMY identity ledger may coexist and is not prior
                # state for this fixed Project DID.
                credential = f.credentials / 'project-seed'
                credential.unlink()
                self.add_distinct_identity_ledger(f)
                self.assertEqual(len(list((f.state / 'ledger').glob('identity-*.json'))), 2)
                credential.write_bytes(f.seed)

                f.startup()
                identity = f.run_smoke('identity-only')
                after_restart = json.loads(ledger.read_text())['value']
                self.assertEqual(identity['stage'], 'IDENTITY')
                self.assertEqual(after_restart['revision'], initial['revision'] + 1)
                self.assertFalse(after_restart['quotaConsumed'])
                self.assertIsNone(after_restart['admission'])
                self.assertEqual(after_restart['actions'], [])
                result = f.run_smoke('prepare-only')
                current = json.loads(ledger.read_text())['value']
                self.assertEqual(result['stage'], 'PREPARED')
                self.assertEqual(result['did'], f.did)
                self.assertEqual(current['did'], initial['did'])
                # Restart appends START, then ADMITTED and PREPARED. A duplicate
                # IDENTITY would make this four revisions instead of three.
                self.assertEqual(current['revision'], initial['revision'] + 3)
                self.assertTrue(current['quotaConsumed'])
                self.assertEqual(len(current['actions']), 1)
                self.assertEqual(current['actions'][0]['status'], 'PREPARED')
                self.assertFalse(list(f.transport_state.iterdir()))
            finally:
                f.close()

    def test_admitted_ledger_remains_reconciliation_only(self):
        with tempfile.TemporaryDirectory(prefix='real-admitted-restart-') as root:
            f = RealFixture(root, enabled=False)
            try:
                f.startup()
                self.assertEqual(f.run_smoke('admit-only')['stage'], 'ADMITTED')
                self.stop_services(f)
                f.startup()
                self.assertGreaterEqual(f.run_smoke('recovery-admitted')['passed'], 4)
            finally:
                f.close()

    def test_prepared_action_remains_reconciliation_only(self):
        with tempfile.TemporaryDirectory(prefix='real-prepared-restart-') as root:
            f = RealFixture(root, enabled=False)
            try:
                f.startup()
                self.assertEqual(f.run_smoke('prepare-only')['stage'], 'PREPARED')
                self.stop_services(f)
                f.startup()
                self.assertGreaterEqual(f.run_smoke('recovery-prepared')['passed'], 4)
                self.assertFalse(list(f.transport_state.iterdir()))
            finally:
                f.close()

    def test_send_attempted_prior_never_resends(self):
        with tempfile.TemporaryDirectory(prefix='real-send-attempted-restart-') as root:
            f = RealFixture(root, enabled=False)
            try:
                f.startup()
                self.assertEqual(f.run_smoke('prepare-only')['stage'], 'PREPARED')
                ledger = self.identity_ledger(f)
                self.stop_services(f)
                custody = f.state / 'real-accept-custody'
                before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in custody.iterdir() if path.name != 'connection-store.lock'}
                self.set_send_attempted(ledger, f.did)
                f.startup()
                self.assertGreaterEqual(f.run_smoke('recovery-send-attempted')['passed'], 4)
                after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in custody.iterdir() if path.name != 'connection-store.lock'}
                self.assertEqual(after, before)
                self.assertFalse(list(f.transport_state.iterdir()))
            finally:
                f.close()

    def test_empty_ledger_with_custody_or_dangling_custody_is_rejected(self):
        for dangling in (False, True):
            with self.subTest(dangling=dangling), tempfile.TemporaryDirectory(prefix='real-custody-window-') as root:
                f = RealFixture(root, enabled=False)
                try:
                    f.startup()
                    self.stop_services(f)
                    custody = f.state / 'real-accept-custody'
                    if dangling:
                        os.symlink(f.state / 'missing-custody-target', custody)
                    else:
                        custody.mkdir(mode=0o700)
                    self.assert_startup_rejected(f)
                finally:
                    f.close()

    def test_corrupt_empty_ledger_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='real-corrupt-ledger-') as root:
            f = RealFixture(root, enabled=False)
            try:
                f.startup()
                ledger = self.identity_ledger(f)
                self.stop_services(f)
                raw = json.loads(ledger.read_text())
                raw['checksum'] = '0' * 64
                ledger.write_text(json.dumps(raw) + '\n')
                self.assert_startup_rejected(f)
            finally:
                f.close()

    def test_unknown_identity_artifact_rejects_startup(self):
        with tempfile.TemporaryDirectory(prefix='real-unknown-ledger-') as root:
            f = RealFixture(root, enabled=False)
            try:
                f.startup()
                ledger = self.identity_ledger(f)
                self.stop_services(f)
                Path(str(ledger) + '.pending-stale').write_text('uncertain\n')
                self.assert_startup_rejected(f)
            finally:
                f.close()

    def test_transport_receipt_prevents_empty_identity_reuse(self):
        with tempfile.TemporaryDirectory(prefix='real-transport-receipt-') as root:
            f = RealFixture(root, enabled=False)
            try:
                f.startup()
                self.stop_services(f)
                (f.transport_state / 'unknown-receipt.json').write_text('{}\n')
                f.startup()
                self.assertGreaterEqual(f.run_smoke('recovery-empty')['passed'], 3)
            finally:
                f.close()

    def test_no_ledger_transport_receipt_and_stale_pending_fail_closed(self):
        cases = ('receipt', 'pending')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix='real-prior-artifact-') as root:
                f = RealFixture(root, enabled=False)
                try:
                    if case == 'receipt':
                        (f.transport_state / 'unknown-receipt.json').write_text('{}\n')
                    else:
                        ledger_root = f.state / 'ledger'
                        ledger_root.mkdir(mode=0o700)
                        name = 'identity-' + hashlib.sha256(f.did.encode()).hexdigest() + '.json.pending-stale'
                        (ledger_root / name).write_text('uncertain\n')
                    self.assert_startup_rejected(f)
                finally:
                    f.close()


if __name__ == '__main__':
    unittest.main()
