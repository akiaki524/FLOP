import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { canonicalApproval } from '../src/collaboration_agent/pilot_approval.mjs';
import { digest, objectDigest, official } from '../src/collaboration_agent/pilot_protocol.mjs';
import { buildAcceptRequest, createAcceptTransport,
  validateAcceptHandoff } from '../src/collaboration_agent/real_transport.mjs';

const { t, signing } = await official();
const temporary = [];
async function tempDir(prefix) {
  const path = await mkdtemp(join(tmpdir(), prefix)); temporary.push(path); return path;
}
const now = 1_800_000_000_000;
const payer = signing.signerFromSeed(Buffer.alloc(32, 0x31));
const payee = signing.signerFromSeed(Buffer.alloc(32, 0x32));
const hash = t.generateHashLock();
const offer = t.makeOffer({ from: payer.did, role: 'payer', lock: 'hash', amount: '1', asset: 'PAPER',
  rails: ['paper'], claimByMs: now + 600_000, refundAfterMs: now + 1_200_000,
  expiresMs: now + 300_000, job: { proto: 'a2a', id: 'real-transport-offline-test' }, nonce: '11'.repeat(8) });
const accept = t.makeAccept(offer, { from: payee.did, statement: hash.hash, nonce: '22'.repeat(8) });
function signed(frame, signer, nonce, seq, timestampMs) {
  const line = t.encodeFrame(frame), room = t.OFFER_ROOM;
  return { room, seq, timestampMs, sender: signer.did, nonce,
    signature: signer.sign(signing.canonicalMessage(room, nonce, line)), line };
}
const offerRecord = signed(offer, payer, '9007199254740993001', 7, now - 1000);
const record = signed(accept, payee, '9007199254740993002', 0, now);
const subject = { session: 'offline-e2e', actionId: 'first-real-paper-accept', type: 'ACCEPT', did: payee.did,
  room: t.OFFER_ROOM, offer: offer.id, contract: accept.contract, ref: offer.id, sourceDigest: 'a'.repeat(64),
  nonce: record.nonce, payloadDigest: digest(signing.canonicalMessage(record.room, record.nonce, record.line)) };
const action = { type: 'ACCEPT', subject, line: record.line };
const candidate = { offer, statement: accept.statement };
const packet = canonicalApproval(action, candidate, signing);
const approval = { actionId: subject.actionId, subjectDigest: objectDigest(subject),
  approvalDigest: packet.approvalDigest, expiresAt: now + 60_000 };
const handoff = { record, packet, approval, offerRecord };
const options = { t, signing, expectedDid: payee.did, nowMs: now };

assert.equal(validateAcceptHandoff(handoff, options).subject.nonce, '9007199254740993002');
const request = buildAcceptRequest(handoff, options);
assert.equal(request.hostname, 'technocore.chat');
assert.equal(request.method, 'GET');
assert.equal(request.agent, false);
assert(request.path.startsWith('/r/tclk-offers/say-signed/' + payee.did + '/'));
assert(!request.path.split('/')[4].includes('%3A'));
assert(request.path.endsWith('/' + encodeURIComponent(record.line)));
assert.equal(decodeURIComponent(request.path.split('/').at(-1)), record.line);
assert.equal(request.path.split('/')[2], 'tclk-offers');
assert.equal(request.path.split('/')[4], payee.did);
assert.equal(request.path.split('/')[5], record.signature);
assert.equal(request.path.split('/')[6], record.nonce);
assert(!request.path.includes('?') && !request.path.includes('#'));

// Path fields use the official raw spellings and reject delimiter injection.
for (const [field, value] of [['sender', payee.did + '/evil'],
  ['sender', payee.did + '?evil'], ['sender', payee.did + '#evil'],
  ['signature', record.signature + '='], ['signature', record.signature + '/evil'],
  ['signature', record.signature + '?evil'], ['signature', record.signature + '#evil'],
  ['nonce', record.nonce + '/evil'], ['nonce', record.nonce + '?evil'],
  ['nonce', record.nonce + '#evil']]) {
  const changed = structuredClone(handoff);
  changed.record[field] = value;
  assert.throws(() => buildAcceptRequest(changed, options));
}

function rejected(mutator, code) {
  const changed = structuredClone(handoff); mutator(changed);
  assert.throws(() => validateAcceptHandoff(changed, options), code ? { message: code } : undefined);
}
rejected(x => { x.record.room = 'other'; });
function typedHandoff(type) {
  const changedSubject = { ...subject, type };
  const changedPacket = canonicalApproval({ type, subject: changedSubject, line: record.line }, candidate, signing);
  return { ...handoff, packet: changedPacket, approval: { ...approval,
    subjectDigest: objectDigest(changedSubject), approvalDigest: changedPacket.approvalDigest } };
}
for (const type of ['REVEAL', 'DELIVERY_GCD', 'UNKNOWN']) {
  assert.throws(() => validateAcceptHandoff(typedHandoff(type), options), { message: 'ACCEPT_ONLY' });
}
rejected(x => { x.record.line = 'arbitrary signed message'; });
rejected(x => { delete x.approval; });
rejected(x => { delete x.record.signature; });
rejected(x => { x.url = 'https://other.invalid/say-signed'; });
// A valid Ed25519 signature alone is not a Stage 1 signing/transport capability.
const arbitraryLine = 'arbitrary correctly signed message';
const arbitraryRecord = { ...record, line: arbitraryLine,
  signature: payee.sign(signing.canonicalMessage(record.room, record.nonce, arbitraryLine)) };
const arbitrarySubject = { ...subject,
  payloadDigest: digest(signing.canonicalMessage(record.room, record.nonce, arbitraryLine)) };
const arbitraryPacket = canonicalApproval({ type: 'ACCEPT', subject: arbitrarySubject,
  line: arbitraryLine }, candidate, signing);
assert.throws(() => validateAcceptHandoff({ ...handoff, record: arbitraryRecord,
  packet: arbitraryPacket, approval: { ...approval, subjectDigest: objectDigest(arbitrarySubject),
    approvalDigest: arbitraryPacket.approvalDigest } }, options));
rejected(x => {
  x.record.nonce = '9007199254740993003';
  x.record.signature = payee.sign(signing.canonicalMessage(x.record.room, x.record.nonce, x.record.line));
}, 'SIGNED_RECORD_BINDING_MISMATCH');

rejected(x => { x.approval.subjectDigest = '0'.repeat(64); }, 'APPROVAL_PAYLOAD_MISMATCH');
rejected(x => { x.approval.approvalDigest = '0'.repeat(64); }, 'APPROVAL_PAYLOAD_MISMATCH');
rejected(x => { x.approval.expiresAt = now; }, 'APPROVAL_EXPIRED');
rejected(x => { x.record.nonce = '9007199254740993003'; }, 'INVALID_SIGNATURE');
function reboundPacket(mutator) {
  const changed = structuredClone(handoff);
  mutator(changed.packet);
  const { approvalDigest: ignored, ...view } = changed.packet;
  changed.packet.approvalDigest = objectDigest(view);
  changed.approval.approvalDigest = changed.packet.approvalDigest;
  return changed;
}
assert.throws(() => validateAcceptHandoff(reboundPacket(x => { x.statement = '0x' + '00'.repeat(32); }), options),
  { message: 'APPROVAL_PAYLOAD_MISMATCH' });
assert.throws(() => validateAcceptHandoff(reboundPacket(x => { x.deadlines.expiresMs++; }), options),
  { message: 'APPROVAL_PAYLOAD_MISMATCH' });
const valueOffer = { ...offer, asset: 'BTC' };
assert.throws(() => stage(valueOffer));
function stage(changedOffer) {
  const line = t.encodeFrame(changedOffer);
  const changed = structuredClone(handoff);
  changed.offerRecord = { ...offerRecord, line,
    signature: payer.sign(signing.canonicalMessage(t.OFFER_ROOM, offerRecord.nonce, line)) };
  return validateAcceptHandoff(changed, options);
}
function alternateHandoff({ asset = 'PAPER', rails = ['paper'], role = 'payer',
  claimByMs = now + 600_000, refundAfterMs = now + 1_200_000,
  expiresMs = now + 300_000 } = {}) {
  const alternateOffer = t.makeOffer({ from: payer.did, role, lock: 'hash', amount: '1', asset,
    rails, claimByMs, refundAfterMs, expiresMs, nonce: '33'.repeat(8) });
  const alternateAccept = t.makeAccept(alternateOffer, { from: payee.did, statement: hash.hash,
    nonce: '44'.repeat(8) });
  const alternateOfferRecord = signed(alternateOffer, payer, '9007199254740993010', 8, now - 500);
  const alternateRecord = signed(alternateAccept, payee, '9007199254740993011', 0, now);
  const alternateSubject = { ...subject, actionId: 'alternate-policy-action', offer: alternateOffer.id,
    contract: alternateAccept.contract, ref: alternateOffer.id, nonce: alternateRecord.nonce,
    payloadDigest: digest(signing.canonicalMessage(alternateRecord.room, alternateRecord.nonce,
      alternateRecord.line)) };
  const alternatePacket = canonicalApproval({ type: 'ACCEPT', subject: alternateSubject,
    line: alternateRecord.line }, { offer: alternateOffer, statement: alternateAccept.statement }, signing);
  return { record: alternateRecord, offerRecord: alternateOfferRecord, packet: alternatePacket,
    approval: { actionId: alternateSubject.actionId, subjectDigest: objectDigest(alternateSubject),
      approvalDigest: alternatePacket.approvalDigest, expiresAt: now + 60_000 } };
}
assert.throws(() => validateAcceptHandoff(alternateHandoff({ asset: 'BTC' }), options),
  { message: 'PAPER_ONLY' });
assert.throws(() => validateAcceptHandoff(alternateHandoff({ rails: ['paper', 'memory'] }), options),
  { message: 'PAPER_ONLY' });
assert.throws(() => validateAcceptHandoff(alternateHandoff({ role: 'payee' }), options),
  { message: 'PAPER_ONLY' });
assert.throws(() => validateAcceptHandoff(alternateHandoff({ expiresMs: now + 60_000 }), options),
  { message: 'ACCEPT_DEADLINE' });
assert.throws(() => validateAcceptHandoff(alternateHandoff({ claimByMs: now + 120_000 }), options),
  { message: 'CLAIM_DEADLINE' });
assert.throws(() => validateAcceptHandoff(alternateHandoff({ claimByMs: now + 600_000,
  refundAfterMs: now + 659_999 }), options), { message: 'REFUND_MARGIN' });
assert.throws(() => validateAcceptHandoff(handoff, { ...options, expectedDid: payer.did }),
  { message: 'WRONG_DID' });

const disabledDir = await tempDir('real-transport-disabled-');
let attempts = 0;
const disabled = createAcceptTransport({ t, signing, expectedDid: payee.did, stateDirectory: disabledDir,
  requestImpl: async () => { attempts++; } });
await assert.rejects(disabled.send(handoff, { nowMs: now }), { message: 'EXTERNAL_WRITE_DISABLED' });
assert.equal(attempts, 0);

const stateDir = await tempDir('real-transport-state-');
const seen = [];
const transport = createAcceptTransport({ t, signing, expectedDid: payee.did,
  stateDirectory: stateDir, writeEnabled: true, requestImpl: async req => {
  seen.push(req); return { statusCode: 302, headers: { location: 'https://evil.invalid/' } };
} });
process.env.HTTPS_PROXY = 'http://127.0.0.1:9';
const result = await transport.send(handoff, { nowMs: now });
delete process.env.HTTPS_PROXY;
assert.deepEqual(result, { status: 'AMBIGUOUS', retry: false,
  next: 'READ_ONLY_RECONCILIATION', envelopeDigest: request.envelopeDigest });
assert.equal(seen.length, 1);
assert.equal(seen[0].hostname, 'technocore.chat');
assert.equal(Object.hasOwn(seen[0], 'proxy'), false);
await assert.rejects(transport.send(handoff, { nowMs: now }), { message: 'NO_RETRY' });
assert.equal(seen.length, 1);
const restarted = createAcceptTransport({ t, signing, expectedDid: payee.did,
  stateDirectory: stateDir, writeEnabled: true, requestImpl: async () => { attempts++; } });
await assert.rejects(restarted.send(handoff, { nowMs: now }), { message: 'NO_RETRY' });
assert.equal(attempts, 0);
await assert.rejects(restarted.send(alternateHandoff(), { nowMs: now }), { message: 'NO_RETRY' });
const receiptNames = await readdir(stateDir);
assert.deepEqual(receiptNames, ['first-real-accept.json']);
const receipt = await readFile(join(stateDir, receiptNames[0]), 'utf8');
assert(!receipt.includes(record.signature));
assert(!receipt.includes('/say-signed/'));
assert.equal(JSON.parse(receipt).state, 'SEND_ATTEMPTED');

for (const failure of [new Error('timeout'), new Error('reset')]) {
  const dir = await tempDir('real-transport-error-');
  let count = 0;
  const service = createAcceptTransport({ t, signing, expectedDid: payee.did,
    stateDirectory: dir, writeEnabled: true, requestImpl: async () => {
    count++; throw failure;
  } });
  assert.equal((await service.send(handoff, { nowMs: now })).status, 'AMBIGUOUS');
  assert.equal(count, 1);
}

function fakeHttps(chunks, statusCode = 200, error = null) {
  return { request(requestOptions, callback) {
    assert.equal(requestOptions.agent, false);
    assert.equal(requestOptions.hostname, 'technocore.chat');
    const req = new EventEmitter(); req.end = () => queueMicrotask(() => {
      if (chunks === null) return;
      if (error) { req.emit('error', error); return; }
      const response = new EventEmitter(); response.statusCode = statusCode; response.destroy = () => {};
      callback(response); for (const chunk of chunks) response.emit('data', Buffer.from(chunk)); response.emit('end');
    }); req.destroy = () => {}; return req;
  } };
}
for (const [chunks, statusCode, cap, error] of [
  [['ok'], 200, 16, null], [['failure'], 500, 16, null], [['redirect'], 302, 16, null],
  [['12345'], 200, 4, null],
  [[], 0, 16, new Error('reset')], [null, 0, 16, null],
]) {
  const dir = await tempDir('real-transport-adapter-');
  const service = createAcceptTransport({ t, signing, expectedDid: payee.did, stateDirectory: dir,
    writeEnabled: true, httpsModule: fakeHttps(chunks, statusCode, error), timeoutMs: 100,
    maxResponseBytes: cap });
  const outcome = await service.send(handoff, { nowMs: now });
  assert.equal(outcome.status, 'AMBIGUOUS');
  assert.equal(outcome.retry, false);
}

const dnsDir = await tempDir('real-transport-dns-');
let dnsCalls = 0;
const dnsService = createAcceptTransport({ t, signing, expectedDid: payee.did,
  stateDirectory: dnsDir, writeEnabled: true, httpsModule: { request() {
    dnsCalls++; throw new Error('ENOTFOUND');
  } } });
assert.equal((await dnsService.send(handoff, { nowMs: now })).status, 'AMBIGUOUS');
assert.equal(dnsCalls, 1);

const expiryDir = await tempDir('real-transport-expiry-');
let expiryAttempts = 0;
let clockCall = 0;
const expiryHandoff = structuredClone(handoff);
expiryHandoff.approval.expiresAt = now + 1;
const expiryService = createAcceptTransport({ t, signing, expectedDid: payee.did,
  stateDirectory: expiryDir, writeEnabled: true, clock: () => [100, 102][clockCall++] ?? 102,
  requestImpl: async () => { expiryAttempts++; } });
await assert.rejects(expiryService.send(expiryHandoff, { nowMs: now }), { message: 'APPROVAL_EXPIRED' });
assert.equal(expiryAttempts, 0);

const deadlineDir = await tempDir('real-transport-deadline-');
let deadlineAttempts = 0;
const deadlineService = createAcceptTransport({ t, signing, expectedDid: payee.did,
  stateDirectory: deadlineDir, writeEnabled: true,
  requestImpl: async () => { deadlineAttempts++; } });
await assert.rejects(deadlineService.send(alternateHandoff({ expiresMs: now + 60_000 }),
  { nowMs: now }), { message: 'ACCEPT_DEADLINE' });
assert.equal(deadlineAttempts, 0);

const elapsedDeadlineDir = await tempDir('real-transport-elapsed-deadline-');
let elapsedDeadlineAttempts = 0, elapsedClockCall = 0;
const elapsedDeadlineService = createAcceptTransport({ t, signing, expectedDid: payee.did,
  stateDirectory: elapsedDeadlineDir, writeEnabled: true,
  clock: () => [100, 103][elapsedClockCall++] ?? 103,
  requestImpl: async () => { elapsedDeadlineAttempts++; } });
await assert.rejects(elapsedDeadlineService.send(alternateHandoff({ expiresMs: now + 60_002 }),
  { nowMs: now }), { message: 'ACCEPT_DEADLINE' });
assert.equal(elapsedDeadlineAttempts, 0);

await Promise.all(temporary.map(path => rm(path, { recursive: true, force: true })));

console.log(JSON.stringify({ passed: true, networkAttempts: seen.length, retryCount: 0,
  nonce: record.nonce, externalNetwork: false }));
