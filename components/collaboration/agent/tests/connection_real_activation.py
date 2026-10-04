"""Offline Real-preparation E2E: fake seed, copied DID anchor, no INET sockets.

The production deployment exposes neither the test DID override nor adapter
injection. This fixture changes only its temporary code copy; no host units run.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from connection_offline import OfflineFixture, ROOT, NODE, WRAPPER

PROJECT = 'did:key:z6MkjQVeF7TCAC6H6LKQnYFannWhsfWzzHhDDr3UhbZgHSTL'

class RealFixture(OfflineFixture):
    def __init__(self, root, enabled=True):
        super().__init__(root)
        result = subprocess.run([NODE, '--input-type=module', '-e',
            "import {official} from './src/collaboration_agent/pilot_protocol.mjs';"
            "const {signing}=await official();process.stdout.write(signing.signerFromSeed(Buffer.alloc(32,65)).did);"],
            cwd=ROOT, capture_output=True, check=True)
        self.did = result.stdout.decode()
        for path in self.source.glob('*.mjs'):
            path.write_text(path.read_text().replace(PROJECT, self.did))
        for filename in ('connection_runtime.mjs', 'connection_services.mjs'):
            path = self.source / filename
            path.write_text(path.read_text().replace(
                '/etc/collab-connection/first-accept.json', str(self.config / 'first-accept.json')))
        (self.credentials / 'dummy').rename(self.credentials / 'project-seed')
        (self.config / 'config.json').write_text(json.dumps({'mode': 'REAL_ACCEPT_PREPARATION',
            'projectDid': self.did, 'externalWriteEnabled': enabled}))
        self.transport_state = self.root / 'transport-state'; self.transport_state.mkdir(mode=0o700)
        services = self.source / 'connection_services.mjs'
        text = "import {fakeAcceptRequest} from '../../tests/fake_real_request.mjs';\n" + services.read_text()
        text = text.replace("stateDirectory: '/var/lib/collab-transport'",
            "requestImpl: fakeAcceptRequest, stateDirectory: " + json.dumps(str(self.transport_state)))
        text = text.replace("const directory = '/var/lib/collab-transport'",
            "const directory = " + json.dumps(str(self.transport_state)))
        services.write_text(text)
        (self.tests / 'fake_real_request.mjs').write_text('''
import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';
import {official} from '../src/collaboration_agent/pilot_protocol.mjs';
export async function fakeAcceptRequest(request) {
 const {t}=await official();
 assert.equal(request.hostname,'technocore.chat'); assert.equal(request.protocol,'https:');
 assert.equal(request.method,'GET'); assert.equal(request.agent,false);
 const parts=request.path.split('/');
 assert.deepEqual(parts.slice(0,4),['','r','tclk-offers','say-signed']);
 assert.equal(parts.length,8);
 assert.equal(decodeURIComponent(parts[6]),'9007199254740993124');
 const frame=t.decodeFrame(decodeURIComponent(parts[7])); assert.equal(frame.type,'accept');
 writeFileSync(MARKER,JSON.stringify({calls:1,nonce:parts[6],action:frame.type}),{flag:'wx',mode:0o600});
 return {statusCode:200,body:Buffer.from('unclassified offline response')};
}
'''.replace('MARKER', json.dumps(str(self.transport_state / 'fake-attempt.json'))))
        smoke = (ROOT / 'tests/real_activation_smoke.mjs').read_text()
        smoke = smoke.replace('.local/batch17a/candidate.json', str(ROOT / '.local/batch17a/candidate.json'))
        smoke = smoke.replace('/run/collab-connection/', str(self.root) + '/')
        (self.tests / 'real_activation_smoke.mjs').write_text(smoke)

    def launch(self, role):
        names = {'signer': ['gate', 'worker'], 'worker': ['worker-control'],
                 'approval': ['human'], 'transport': ['transport']}[role]
        passed = [os.dup(self.listeners[name].fileno()) for name in names]
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}; env.update({'LISTEN_FDS': str(len(names)),
            'LISTEN_FDNAMES': ':'.join(names), 'CREDENTIALS_DIRECTORY': str(self.credentials)})
        fs = ['--allow-fs-read=' + str(self.deploy), '--allow-fs-read=' + str(self.config)]
        if role == 'signer':
            fs += ['--allow-fs-read=' + str(self.credentials), '--allow-fs-read=' + str(self.state),
                   '--allow-fs-write=' + str(self.state)]
        if role == 'transport':
            fs += ['--allow-fs-read=' + str(self.transport_state), '--allow-fs-write=' + str(self.transport_state)]
        entry = [str(self.source / 'pilot_signer.mjs'), '--offline-connection'] if role == 'signer' else [
            str(self.source / 'connection_services.mjs'), role]
        # Every role, including fake Transport, is denied AF_INET/AF_INET6 by seccomp.
        argv = [sys.executable, '-I', '-c', WRAPPER, str(len(passed)), '1', *map(str, passed), NODE,
            '--permission', '--disable-sigusr1', '--disallow-code-generation-from-strings', *fs, *entry]
        child = subprocess.Popen(argv, cwd=self.deploy, env=env, pass_fds=tuple(passed),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for fd in passed: os.close(fd)
        self.children.append((role, child)); return child

    def run_smoke(self, mode='enabled'):
        result = subprocess.run([NODE, str(self.tests / 'real_activation_smoke.mjs'), mode],
            cwd=self.deploy, capture_output=True, timeout=30)
        try: value = json.loads(result.stdout)
        except Exception: value = {'passed': -1, 'failed': 1}
        if result.returncode or value['failed'] or result.stderr:
            raise AssertionError('REAL_OFFLINE_E2E_FAILED_AFTER_' + str(value['passed']))
        return value

    def startup(self):
        for role in ('transport', 'signer', 'approval', 'worker'): self.launch(role)
        time.sleep(.5)
        for role, child in self.children:
            if child.poll() is not None:
                raise AssertionError('REAL_SERVICE_START_FAILED_' + role)
            proc = Path('/proc') / str(child.pid)
            metadata = (proc / 'environ').read_bytes() + (proc / 'cmdline').read_bytes()
            if any(s in metadata for s in (self.seed, self.seed.hex().encode())):
                raise AssertionError('FAKE_SECRET_EXPOSED')

class RealActivationTest(unittest.TestCase):
    def test_credential_does_not_enable_write(self):
        with tempfile.TemporaryDirectory(prefix='real-preparation-') as root:
            f = RealFixture(root, enabled=False)
            try:
                f.startup(); self.assertGreater(f.run_smoke('disabled')['passed'], 15)
                self.assertFalse(list(f.transport_state.iterdir()))
            finally: f.close()

    def test_approved_accept_ambiguous_restart_no_resend_and_exact_nonce(self):
        with tempfile.TemporaryDirectory(prefix='real-preparation-') as root:
            f = RealFixture(root)
            try:
                f.startup(); self.assertGreaterEqual(f.run_smoke()['passed'], 20)
                marker = f.transport_state / 'fake-attempt.json'
                self.assertEqual(json.loads(marker.read_text()), {'calls': 1, 'nonce': '9007199254740993124', 'action': 'accept'})
                custody = f.state / 'real-accept-custody'
                before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in custody.iterdir()
                          if p.name != 'connection-store.lock'}
                ledger = next((f.state / 'ledger').glob('identity-*.json'))
                value = json.loads(ledger.read_text())['value']
                self.assertEqual(value['actions'][0]['status'], 'AMBIGUOUS')
                self.assertIsNotNone(value['actions'][0]['approval'])
                signer = next(c for r, c in f.children if r == 'signer')
                signer.kill(); signer.wait(timeout=5)
                # Test-only recovery: verify dead owner, remove just its locks, retain all state.
                for lock in (Path(str(ledger) + '.lock'), custody / 'connection-store.lock'):
                    self.assertEqual(json.loads(lock.read_text())['pid'], signer.pid)
                    lock.unlink()
                    fd = os.open(lock.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try: os.fsync(fd)
                    finally: os.close(fd)
                f.launch('signer'); time.sleep(.4)
                self.assertGreaterEqual(f.run_smoke('recovery')['passed'], 5)
                after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in custody.iterdir()
                         if p.name != 'connection-store.lock'}
                self.assertEqual(before, after)
                self.assertEqual(json.loads(marker.read_text())['calls'], 1)
            finally: f.close()

    def test_public_reconciliation_preserves_stop(self):
        with tempfile.TemporaryDirectory(prefix='real-preparation-') as root:
            f = RealFixture(root)
            try:
                f.startup(); self.assertGreater(f.run_smoke('reconcile')['passed'], 20)
                ledger = next((f.state / 'ledger').glob('identity-*.json'))
                self.assertEqual(json.loads(ledger.read_text())['value']['actions'][0]['status'], 'RECONCILED')
            finally: f.close()

if __name__ == '__main__': unittest.main()
