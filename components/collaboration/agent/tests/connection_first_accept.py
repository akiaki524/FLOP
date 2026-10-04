"""Rootless Stage C1 -> C2 E2E with fake identity and blocked INET.

The fixture copies production code, rewrites only local paths/identity, and
injects a fake one-shot Transport adapter.  It never reads a Real seed, uses
sudo, or opens an INET socket.
"""
import hashlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from connection_real_activation import NODE, ROOT, RealFixture


BUILD_SNAPSHOT = r"""
import {official} from './src/collaboration_agent/pilot_protocol.mjs';
const {t,signing}=await official();
const payer=signing.signerFromSeed(Buffer.alloc(32,66));
const payee=signing.signerFromSeed(Buffer.alloc(32,65));
const now=Number(process.argv[1]);
function signed(frame,seq,timestampMs=now-100) {
 const line=typeof frame==='string'?frame:t.encodeFrame(frame), nonce=String(now-500+seq);
 return {room:t.OFFER_ROOM,seq,timestampMs,sender:payer.did,nonce,
  signature:payer.sign(signing.canonicalMessage(t.OFFER_ROOM,nonce,line)),line};
}
const offer=t.makeOffer({from:payer.did,role:'payer',amount:'1',asset:'PAPER',rails:['paper'],
 lock:'hash',expiresMs:now+600000,claimByMs:now+1800000,refundAfterMs:now+2400000,
 job:{proto:'a2a',id:'FIRST-REAL-ACCEPT-OFFLINE'}});
const alternate=t.makeOffer({from:payer.did,role:'payer',amount:'2',asset:'PAPER',rails:['paper'],
 lock:'hash',expiresMs:now+600000,claimByMs:now+1800000,refundAfterMs:now+2400000,
 job:{proto:'a2a',id:'ALTERNATE-FIRST-ACCEPT-OFFLINE'}});
const valid=signed(offer,1), malformed={...signed('hostile row',2),signature:'invalid'};
const alternateRecord=signed(alternate,2);
const observation={version:2,kind:'PROJECT_DID_PUBLIC_NONCE_OBSERVATION',did:payee.did,
 room:t.OFFER_ROOM,observedNonce:null,observedNone:true,verifiedAtMs:now,records:[],
 source:{url:'https://technocore.chat/r/tclk-offers/export',method:'GET',httpStatus:200,
 generation:1,rawBytes:4096,rawSha256:'a'.repeat(64),capturedAt:now},
 coverage:{complete:true,basis:'technocore-e4c4f73f3b28612d7161170b11e08e580b02123a',
  tailStart:0,tailBytes:4096,lineCount:1}};
process.stdout.write(JSON.stringify({offerId:offer.id,
 snapshot:{version:1,observation,records:[valid]},
 hostileSnapshot:{version:1,observation,records:[malformed,valid]},
 alternateOfferId:alternate.id,
 alternateSnapshot:{version:1,
  observation:{...observation,source:{...observation.source,rawSha256:'d'.repeat(64)}},
  records:[alternateRecord]}}));
"""


BUILD_VALID_HANDOFF = r"""
import {official} from './src/collaboration_agent/pilot_protocol.mjs';
import {validateAcceptHandoff} from './src/collaboration_agent/real_transport.mjs';
const chunks=[]; for await (const chunk of process.stdin) chunks.push(chunk);
const document=JSON.parse(Buffer.concat(chunks).toString('utf8'));
const {t,signing}=await official(), signer=signing.signerFromSeed(Buffer.alloc(32,65));
const s=document.packet.subject, line=document.packet.publicLine;
const record={room:s.room,seq:0,timestampMs:Date.now(),sender:signer.did,nonce:s.nonce,
 signature:signer.sign(signing.canonicalMessage(s.room,s.nonce,line)),line};
const approval={actionId:s.actionId,subjectDigest:document.packet.subjectDigest,
 approvalDigest:document.packet.approvalDigest,expiresAt:Date.now()+60000};
const value={record,packet:document.packet,approval,offerRecord:document.offerRecord};
validateAcceptHandoff(value,{t,signing,expectedDid:signer.did});
process.stdout.write(JSON.stringify(value));
"""


BUILD_PROJECT_NONCE = r"""
import {official} from './src/collaboration_agent/pilot_protocol.mjs';
const {t,signing}=await official(), signer=signing.signerFromSeed(Buffer.alloc(32,65));
const at=Number(process.argv[1]), nonce=process.argv[2], line='existing project record';
process.stdout.write(JSON.stringify({room:t.OFFER_ROOM,seq:2,timestampMs:at-100,
 sender:signer.did,nonce,signature:signer.sign(signing.canonicalMessage(t.OFFER_ROOM,nonce,line)),line}));
"""


FAKE_REQUEST = r"""
import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';
import {official} from '../src/collaboration_agent/pilot_protocol.mjs';
export async function fakeAcceptRequest(request) {
 const {t}=await official(), parts=request.path.split('/');
 assert.equal(request.hostname,'technocore.chat'); assert.equal(request.protocol,'https:');
 assert.equal(request.method,'GET'); assert.equal(request.agent,false); assert.equal(parts.length,8);
 assert.deepEqual(parts.slice(0,4),['','r','tclk-offers','say-signed']);
 const record={room:'tclk-offers',seq:3,timestampMs:Date.now(),sender:parts[4],
  signature:parts[5],nonce:parts[6],line:decodeURIComponent(parts[7])};
 assert.equal(t.decodeFrame(record.line).type,'accept');
 writeFileSync(MARKER,JSON.stringify({calls:1,record}),{flag:'wx',mode:0o600});
 return {statusCode:200,body:Buffer.from('unclassified offline response')};
}
"""


class FirstAcceptFixture(RealFixture):
    def __init__(self, root):
        super().__init__(root, enabled=True)
        self.now_ms = int(time.time() * 1000)
        self.clock = self.config / 'clock.json'
        self.set_clock(self.now_ms)
        (self.tests / 'test_clock.mjs').write_text(
            "import {readFileSync} from 'node:fs';\n"
            "const path=" + json.dumps(str(self.clock)) + ";\n"
            "Date.now=()=>JSON.parse(readFileSync(path,'utf8')).nowMs;\n")
        preload = "import '../../tests/test_clock.mjs';\n"
        for filename in ('pilot_signer.mjs', 'connection_services.mjs'):
            path = self.source / filename
            path.write_text(preload + path.read_text())
        client_dir = self.deploy / 'deploy/connection'
        client_dir.mkdir(parents=True)
        client = client_dir / 'gate_c_client.mjs'
        shutil.copyfile(ROOT / 'deploy/connection/gate_c_client.mjs', client)
        client.write_text(preload + client.read_text().replace(
            '/run/collab-connection/', str(self.root) + '/'))
        self.client = client
        (self.tests / 'fake_real_request.mjs').write_text(
            FAKE_REQUEST.replace('MARKER', json.dumps(str(self.transport_state / 'fake-attempt.json'))))

    def set_clock(self, now_ms):
        self.now_ms = now_ms
        self.clock.write_text(json.dumps({'nowMs': now_ms}))

    def client_call(self, command, value):
        result = subprocess.run([NODE, str(self.client), command], cwd=self.deploy,
                                input=json.dumps(value).encode(), capture_output=True, timeout=30)
        self.assert_silent_secret(result)
        return result.returncode, json.loads(result.stdout)

    def assert_silent_secret(self, result):
        if result.stderr:
            raise AssertionError('GATE_C_CLIENT_STDERR')
        for secret in (self.seed, self.seed.hex().encode()):
            if secret in result.stdout or secret in result.stderr:
                raise AssertionError('FAKE_SEED_EXPOSED')

    def snapshot(self, at=None):
        now = self.now_ms if at is None else at
        result = subprocess.run([NODE, '--input-type=module', '-e', BUILD_SNAPSHOT, str(now)],
                                cwd=self.deploy, capture_output=True, check=True)
        return json.loads(result.stdout)

    def freeze_c1(self):
        source = self.snapshot()
        code, found = self.client_call('candidates', source['hostileSnapshot'])
        if code or [row['offerId'] for row in found] != [source['offerId']]:
            raise AssertionError('C1_CANDIDATE_FILTER_FAILED')
        code, refused = self.client_call('freeze',
            {'snapshot': source['snapshot'], 'selectedOfferId': '0x' + '00' * 32})
        if code == 0 or refused != {'status': 'HUMAN_STOP', 'retry': False}:
            raise AssertionError('C1_AUTOMATIC_SELECTION')
        code, frozen = self.client_call('freeze',
            {'snapshot': source['snapshot'], 'selectedOfferId': source['offerId']})
        if code:
            raise AssertionError('C1_FREEZE_FAILED')
        return source, frozen

    def activate(self, frozen):
        document, preimage = frozen['document'], frozen['preimage']
        if not isinstance(preimage, str) or not preimage.startswith('0x') or len(preimage) != 66:
            raise AssertionError('C1_PREIMAGE_FORMAT')
        (self.config / 'first-accept.json').write_text(json.dumps(document))
        (self.credentials / 'accept-preimage').write_bytes(bytes.fromhex(preimage[2:]))
        (self.config / 'config.json').write_text(json.dumps({'mode': 'REAL_ACCEPT_PREPARATION',
            'projectDid': self.did, 'externalWriteEnabled': True,
            'firstAcceptDigest': document['digest']}))
        self.startup()
        for _, child in self.children:
            status = (Path('/proc') / str(child.pid) / 'status').read_text()
            if 'NoNewPrivs:\t1' not in status or 'Seccomp:\t2' not in status:
                raise AssertionError('PROCESS_BOUNDARY_MISSING')
            metadata = ((Path('/proc') / str(child.pid) / 'environ').read_bytes()
                        + (Path('/proc') / str(child.pid) / 'cmdline').read_bytes())
            if bytes.fromhex(preimage[2:]) in metadata or preimage[2:].encode() in metadata:
                raise AssertionError('FAKE_PREIMAGE_EXPOSED')

    def execute(self, document, final_snapshot, confirmation=None):
        return self.client_call('execute', {'document': document,
            'confirmation': confirmation or 'SEND ACCEPT ' + document['digest'],
            'finalSnapshot': final_snapshot})

    def public_snapshot(self, document, record=None, at=None):
        now = self.now_ms if at is None else at
        observation = json.loads(json.dumps(document['observation']))
        observation['verifiedAtMs'] = now
        observation['source']['capturedAt'] = now
        observation['source']['rawSha256'] = 'c' * 64
        observation['records'] = [] if record is None else [record]
        observation['observedNonce'] = None if record is None else record['nonce']
        observation['observedNone'] = record is None
        observation['coverage']['lineCount'] = max(1, len(observation['records']))
        records = [document['offerRecord']]
        if record is not None:
            records.append(record)
        return {'version': 1, 'observation': observation, 'records': records}

    def project_nonce_record(self, nonce):
        result = subprocess.run([NODE, '--input-type=module', '-e', BUILD_PROJECT_NONCE,
                                 str(self.now_ms), nonce], cwd=self.deploy,
                                capture_output=True, check=True)
        return json.loads(result.stdout)

    def stop_services(self):
        children, self.children = self.children, []
        for _, child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for role, child in children:
            try:
                stdout, stderr = child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                stdout, stderr = child.communicate(timeout=5)
            if stdout or stderr:
                raise AssertionError(role + '_SERVICE_OUTPUT_NOT_SILENT')

    def socket_request(self, channel, method, value):
        return self.raw_socket_request(channel, {'method': method, 'value': value})

    def raw_socket_request(self, channel, request):
        client = socket.socket(socket.AF_UNIX)
        client.settimeout(5)
        client.connect(str(self.paths[channel]))
        client.sendall((json.dumps(request) + '\n').encode())
        client.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        client.close()
        return json.loads(b''.join(chunks))


class FirstAcceptE2ETest(unittest.TestCase):
    def test_admitted_stop_restarts_reconciliation_only_without_dispatch(self):
        with tempfile.TemporaryDirectory(prefix='first-accept-recovery-bound-') as root:
            f = FirstAcceptFixture(root)
            try:
                _, frozen = f.freeze_c1()
                document, subject = frozen['document'], frozen['document']['packet']['subject']
                f.activate(frozen)
                admitted = f.socket_request('gate', 'admit', {
                    'mode': 'REAL_ACCEPT_PREPARATION', 'expectedDid': subject['did'],
                    'offerRecord': document['offerRecord'],
                    'sourceDigest': subject['sourceDigest'], 'family': 'paper.accept-only',
                    'answer': '', 'noValue': 'EXPLICIT_PAPER_NO_VALUE',
                    'heartbeatRequired': False,
                    'finalSnapshot': f.public_snapshot(document)})
                self.assertTrue(admitted['ok'])
                self.assertEqual(admitted['result']['contract'], subject['contract'])
                self.assertEqual(f.socket_request('gate', 'approvalPreview',
                    {'actionId': subject['actionId']}),
                    {'ok': False, 'code': 'CONNECTION_REQUEST_DENIED'})
                self.assertEqual(f.socket_request('worker', 'sign', {
                    'actionId': subject['actionId'], 'subjectDigest': document['packet']['subjectDigest']}),
                    {'ok': False, 'code': 'CONNECTION_REQUEST_DENIED'})
                self.assertTrue(f.socket_request('gate', 'stop', {})['result']['stopped'])

                custody = f.state / 'real-accept-custody'
                before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in custody.iterdir() if path.name != 'connection-store.lock'}
                f.stop_services()
                (f.config / 'config.json').write_text(json.dumps({
                    'mode': 'REAL_ACCEPT_PREPARATION', 'projectDid': f.did,
                    'externalWriteEnabled': False}))
                f.startup()
                status = f.socket_request('worker', 'status', {})['result']
                self.assertTrue(status['stopped'])
                self.assertTrue(status['reconciliationOnly'])
                self.assertEqual(status['contract'], subject['contract'])
                rejected = f.socket_request('gate', 'admit', {
                    'mode': 'REAL_ACCEPT_PREPARATION', 'expectedDid': subject['did'],
                    'offerRecord': document['offerRecord'],
                    'sourceDigest': subject['sourceDigest'], 'family': 'paper.accept-only',
                    'answer': '', 'noValue': 'EXPLICIT_PAPER_NO_VALUE',
                    'heartbeatRequired': False})
                self.assertEqual(rejected, {'ok': False, 'code': 'CONNECTION_REQUEST_DENIED'})
                after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in custody.iterdir() if path.name != 'connection-store.lock'}
                self.assertEqual(after, before)
                self.assertFalse(list(f.transport_state.iterdir()))
            finally:
                f.close()

    def test_more_than_thirty_seconds_after_admit_still_uses_protocol_deadline(self):
        with tempfile.TemporaryDirectory(prefix='first-accept-post-admit-clock-') as root:
            f = FirstAcceptFixture(root)
            try:
                _, frozen = f.freeze_c1()
                document = frozen['document']
                f.activate(frozen)
                code, armed = f.client_call('arm', {'document': document})
                self.assertEqual(code, 0)
                self.assertTrue(armed['armed'])
                subject = document['packet']['subject']
                admitted = f.socket_request('gate', 'admit', {
                    'mode': 'REAL_ACCEPT_PREPARATION', 'expectedDid': subject['did'],
                    'offerRecord': document['offerRecord'],
                    'sourceDigest': subject['sourceDigest'], 'family': 'paper.accept-only',
                    'answer': '', 'noValue': 'EXPLICIT_PAPER_NO_VALUE',
                    'heartbeatRequired': False,
                    'finalSnapshot': f.public_snapshot(document)})
                self.assertTrue(admitted['ok'])
                self.assertEqual(admitted['result']['contract'], subject['contract'])

                f.set_clock(f.now_ms + 31_001)
                prepared = f.socket_request('worker', 'prepare', {'type': 'ACCEPT',
                    'did': subject['did'], 'room': subject['room'], 'offer': subject['offer'],
                    'contract': subject['contract'], 'ref': subject['ref'],
                    'sourceDigest': subject['sourceDigest']})
                self.assertTrue(prepared['ok'])
                packet = f.socket_request('gate', 'approvalPreview',
                                          {'actionId': subject['actionId']})['result']
                expiry = min(packet['deadlines']['expiresMs'] - 60_000,
                             packet['deadlines']['claimByMs'] - 120_000)
                approved = f.socket_request('gate', 'approve', {
                    'actionId': subject['actionId'], 'subjectDigest': packet['subjectDigest'],
                    'approvalDigest': packet['approvalDigest'], 'expiresAt': expiry})
                self.assertTrue(approved['ok'])
                token = {'actionId': subject['actionId'], 'subjectDigest': packet['subjectDigest']}
                self.assertTrue(f.socket_request('worker', 'sign', token)['ok'])
                self.assertTrue(f.socket_request('worker', 'prepareWrite', token)['ok'])
                sent = f.socket_request('gate', 'sendAccept', token)
                self.assertTrue(sent['ok'])
                self.assertEqual((sent['result']['status'], sent['result']['stopped'],
                                  sent['result']['retry']), ('AMBIGUOUS', True, False))
                marker = json.loads((f.transport_state / 'fake-attempt.json').read_text())
                self.assertEqual(marker['calls'], 1)
                self.assertEqual(marker['record']['timestampMs'], f.now_ms)
            finally:
                f.close()

    def test_runtime_and_transport_reject_alternate_valid_packet_without_state_change(self):
        with tempfile.TemporaryDirectory(prefix='first-accept-binding-') as root:
            f = FirstAcceptFixture(root)
            try:
                source, frozen = f.freeze_c1()
                code, alternate = f.client_call('freeze', {'snapshot': source['alternateSnapshot'],
                    'selectedOfferId': source['alternateOfferId']})
                self.assertEqual(code, 0)
                self.assertNotEqual(alternate['document']['digest'], frozen['document']['digest'])
                self.assertNotEqual(alternate['document']['packet']['subject']['sourceDigest'],
                                    frozen['document']['packet']['subject']['sourceDigest'])
                handoff = subprocess.run([NODE, '--input-type=module', '-e', BUILD_VALID_HANDOFF],
                    cwd=f.deploy, input=json.dumps(alternate['document']).encode(),
                    capture_output=True, check=True)
                alternate_handoff = json.loads(handoff.stdout)

                f.activate(frozen)
                subject = alternate['document']['packet']['subject']
                final_snapshot = f.public_snapshot(frozen['document'])
                denied = f.socket_request('gate', 'admit', {
                    'mode': 'REAL_ACCEPT_PREPARATION', 'expectedDid': subject['did'],
                    'offerRecord': alternate['document']['offerRecord'],
                    'sourceDigest': subject['sourceDigest'], 'family': 'paper.accept-only',
                    'answer': '', 'noValue': 'EXPLICIT_PAPER_NO_VALUE',
                    'heartbeatRequired': False, 'finalSnapshot': final_snapshot})
                self.assertEqual(denied, {'ok': False, 'code': 'CONNECTION_REQUEST_DENIED'})
                status = f.socket_request('worker', 'status', {})['result']
                self.assertFalse(status['stopped'])
                self.assertIsNone(status['contract'])
                self.assertEqual(status['actions'], [])

                transport_denied = f.raw_socket_request('transport',
                    {'operation': 'validateAccept', 'value': alternate_handoff})
                self.assertEqual(transport_denied,
                                 {'ok': False, 'code': 'CONNECTION_REQUEST_DENIED'})
                ledger = next((f.state / 'ledger').glob('identity-*.json'))
                durable = json.loads(ledger.read_text())['value']
                self.assertFalse(durable['quotaConsumed'])
                self.assertIsNone(durable['admission'])
                self.assertIsNone(durable['state'])
                self.assertEqual(durable['actions'], [])
                self.assertFalse((f.state / 'real-accept-custody').exists())
                self.assertFalse(list(f.transport_state.iterdir()))
            finally:
                f.close()

    def test_historical_c1_to_fresh_c2_sends_once_reconciles_and_remains_stopped(self):
        with tempfile.TemporaryDirectory(prefix='first-accept-e2e-') as root:
            f = FirstAcceptFixture(root)
            try:
                seed_path = f.credentials / 'project-seed'
                seed_path.unlink()  # C1 has no seed and does not start services.
                source, frozen = f.freeze_c1()
                self.assertFalse(seed_path.exists())
                self.assertFalse((f.state / 'ledger').exists())
                self.assertFalse(list(f.transport_state.iterdir()))
                self.assertEqual(frozen['document']['offerRecord'], source['snapshot']['records'][0])

                f.set_clock(f.now_ms + 61_000)
                seed_path.write_bytes(f.seed)
                f.activate(frozen)
                code, armed = f.client_call('arm', {'document': frozen['document']})
                self.assertEqual(code, 0)
                self.assertEqual(armed, {'armed': True, 'packetDigest': frozen['document']['digest']})
                final_snapshot = f.public_snapshot(frozen['document'])
                code, sent = f.execute(frozen['document'], final_snapshot)
                self.assertEqual(code, 0)
                self.assertEqual((sent['status'], sent['stopped'], sent['retry']),
                                 ('AMBIGUOUS', True, False))
                marker = json.loads((f.transport_state / 'fake-attempt.json').read_text())
                self.assertEqual(marker['calls'], 1)
                self.assertEqual(marker['record']['line'], frozen['document']['packet']['publicLine'])
                receipt = json.loads((f.transport_state / 'first-real-accept.json').read_text())
                self.assertEqual(receipt['state'], 'SEND_ATTEMPTED')
                self.assertEqual(receipt['envelopeDigest'], sent['envelopeDigest'])

                second_code, second = f.execute(frozen['document'], final_snapshot)
                self.assertNotEqual(second_code, 0)
                self.assertEqual(second, {'status': 'HUMAN_STOP', 'retry': False})
                self.assertEqual(json.loads((f.transport_state / 'fake-attempt.json').read_text())['calls'], 1)
                status = f.socket_request('worker', 'status', {})
                self.assertTrue(status['ok'])
                action = status['result']['actions'][0]
                for forbidden in ('DELIVERY_GCD', 'REVEAL'):
                    denied = f.socket_request('worker', 'prepare', {'type': forbidden, 'did': f.did,
                        'room': 'tclk-forbidden', 'offer': frozen['document']['packet']['subject']['offer'],
                        'contract': frozen['document']['packet']['subject']['contract'],
                        'ref': None, 'sourceDigest': frozen['document']['packet']['subject']['sourceDigest'],
                        **({'answer': 'forbidden'} if forbidden == 'DELIVERY_GCD' else {})})
                    self.assertEqual(denied, {'ok': False, 'code': 'CONNECTION_REQUEST_DENIED'})

                public = f.public_snapshot(frozen['document'], marker['record'])
                code, reconciled = f.client_call('match', {'document': frozen['document'],
                    'snapshot': public, 'envelopeDigest': sent['envelopeDigest']})
                self.assertEqual(code, 0)
                self.assertEqual((reconciled['status'], reconciled['stopped'], reconciled['retry']),
                                 ('RECONCILED_PRESENT', True, False))
                final = f.socket_request('worker', 'status', {})['result']
                self.assertTrue(final['stopped'])
                self.assertEqual(final['actions'][0]['status'], 'RECONCILED')
                ledger = next((f.state / 'ledger').glob('identity-*.json'))
                durable = json.loads(ledger.read_text())['value']
                self.assertTrue(durable['quotaConsumed'])
                self.assertEqual(durable['actions'][0]['status'], 'RECONCILED')
                self.assertEqual(action['attemptCount'], 1)
                self.assertTrue((f.state / 'real-accept-custody').is_dir())
            finally:
                f.close()

    def test_wrong_confirmation_tamper_and_bad_final_observations_create_no_durable_action(self):
        with tempfile.TemporaryDirectory(prefix='first-accept-refusal-') as root:
            f = FirstAcceptFixture(root)
            try:
                _, frozen = f.freeze_c1()
                document = frozen['document']
                final_snapshot = f.public_snapshot(document)
                code, result = f.execute(document, final_snapshot, 'SEND ACCEPT wrong')
                self.assertNotEqual(code, 0)
                self.assertEqual(result, {'status': 'HUMAN_STOP', 'retry': False})
                tampered = json.loads(json.dumps(document))
                tampered['offerRecord']['seq'] += 1
                code, result = f.execute(tampered, final_snapshot)
                self.assertNotEqual(code, 0)
                self.assertEqual(result, {'status': 'HUMAN_STOP', 'retry': False})

                f.activate(frozen)
                changed = f.public_snapshot(document, f.project_nonce_record(str(f.now_ms + 10)))
                expired = f.public_snapshot(document, at=f.now_ms - 31_000)
                incompatible = f.public_snapshot(document)
                incompatible['observation']['coverage']['basis'] = 'incompatible-profile'
                for bad in (changed, expired, incompatible):
                    code, result = f.execute(document, bad)
                    self.assertNotEqual(code, 0)
                    self.assertEqual(result, {'status': 'RETURN_C1', 'retry': False})
                status = f.socket_request('worker', 'status', {})['result']
                self.assertFalse(status['stopped'])
                self.assertIsNone(status['contract'])
                self.assertEqual(status['actions'], [])
                ledger = next((f.state / 'ledger').glob('identity-*.json'))
                durable = json.loads(ledger.read_text())['value']
                self.assertFalse(durable['quotaConsumed'])
                self.assertIsNone(durable['admission'])
                self.assertEqual(durable['actions'], [])
                self.assertFalse((f.state / 'real-accept-custody').exists())
                self.assertFalse(list(f.transport_state.iterdir()))
            finally:
                f.close()

    def test_altered_public_record_does_not_reconcile_or_retry(self):
        with tempfile.TemporaryDirectory(prefix='first-accept-mismatch-') as root:
            f = FirstAcceptFixture(root)
            try:
                _, frozen = f.freeze_c1()
                f.activate(frozen)
                code, sent = f.execute(frozen['document'], f.public_snapshot(frozen['document']))
                self.assertEqual(code, 0)
                marker = json.loads((f.transport_state / 'fake-attempt.json').read_text())
                altered = dict(marker['record'])
                altered['line'] += ' '
                public = f.public_snapshot(frozen['document'])
                public['records'] = [altered]
                code, result = f.client_call('match', {'document': frozen['document'],
                    'snapshot': public, 'envelopeDigest': sent['envelopeDigest']})
                self.assertEqual(code, 0)
                self.assertEqual(result, {'status': 'AMBIGUOUS', 'stopped': True, 'retry': False})
                current = f.socket_request('worker', 'status', {})['result']
                self.assertTrue(current['stopped'])
                self.assertEqual(current['actions'][0]['status'], 'AMBIGUOUS')
                self.assertEqual(json.loads((f.transport_state / 'fake-attempt.json').read_text())['calls'], 1)
            finally:
                f.close()


if __name__ == '__main__':
    unittest.main()
