// Integration tests never save seeds, preimages, signed REVEAL bodies or IPC transcripts.
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import fs, { readFileSync, mkdtempSync, mkdirSync, writeFileSync, existsSync } from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { fork } from 'node:child_process';
import { once } from 'node:events';
import { resolve } from 'node:path';
import { launchTestPilot } from '../src/collaboration_agent/pilot_client.mjs';
import { ROOT, official, blockNetwork, digest, objectDigest, envelopeDigest, outputGuard, testNonce } from '../src/collaboration_agent/pilot_protocol.mjs';
import { PilotLedger, ledgerPath, readPublicState, atomicPublicState } from '../src/collaboration_agent/pilot_ledger.mjs';
import { verifyApprovalView, canonicalApproval } from '../src/collaboration_agent/pilot_approval.mjs';

blockNetwork();
const { t, signing } = await official();
const OUT = resolve(ROOT, '.local/batch17a1');
mkdirSync(OUT, { recursive: true });
const candidate = JSON.parse(readFileSync(resolve(ROOT, '.local/batch17a/candidate.json'), 'utf8'));
assert.equal(candidate.independent_verification.valid, true);
const results = [], failures = [];
const checks = [];
const test = (name, fn) => checks.push({ name, fn });
const replaceOffer = (offer, changes) => {
  const { id, ...fields } = offer;
  return t.makeOffer({ ...fields, ...changes });
};
const denied = async (promise, code) => {
  let caught = false;
  try { await promise; } catch (error) { caught = true; assert.equal(error.message, code); }
  assert(caught, 'expected fail-closed rejection');
};

async function fixture(fn, changeAdmission = null) {
  const directory = mkdtempSync(resolve(OUT, 'case-'));
  const journal = resolve(directory, 'journal.jsonl');
  const p = await launchTestPilot(journal);
  const payerSeed = randomBytes(32), payer = signing.signerFromSeed(payerSeed);
  let nonce = BigInt(Date.now()) * 1000n;
  const record = (frame, signer, room, seq, timestampMs = Date.now()) => {
    const line = typeof frame === 'string' ? frame : t.encodeFrame(frame);
    const n = String(++nonce);
    return { room, seq, timestampMs, sender: signer.did, nonce: n,
      signature: signer.sign(signing.canonicalMessage(room, n, line)), line };
  };
  const at = Date.now();
  const offer = t.makeOffer({ from: payer.did, role: 'payer', amount: '1', asset: 'PAPER',
    rails: ['paper'], lock: 'hash', expiresMs: at + 600000, claimByMs: at + 1800000,
    refundAfterMs: at + 2400000,
    job: { proto: 'a2a', id: 'TEST-batch17a-gcd', context: 'TEST GCD bundle sha256=' + candidate.source_digest } });
  const admission = { mode: 'TEST_EPHEMERAL', expectedDid: p.info.did,
    offerRecord: record(offer, payer, t.OFFER_ROOM, 1, at), sourceDigest: candidate.source_digest,
    family: 'math.gcd_lcm', answer: candidate.answer, noValue: 'EXPLICIT_TEST_NO_VALUE', heartbeatRequired: true };
  const f = { p, payer, record, offer, admission, journal, directory, meta: null, heartbeatDone: false };
  f.request = type => ({ type, did: p.info.did, room: type === 'ACCEPT' ? t.OFFER_ROOM : f.meta.room,
    offer: f.offer.id, contract: f.meta.contract, ref: type === 'ACCEPT' ? f.offer.id : type === 'REVEAL' ? f.meta.contract : null,
    sourceDigest: candidate.source_digest, ...(type === 'DELIVERY_GCD' ? { answer: candidate.answer } : {}) });
  f.prepare = async type => {
    const prepared = await p.worker.request('prepare', f.request(type));
    if (type === 'ACCEPT') f.acceptFrame = t.decodeFrame(prepared.preview);
    return prepared;
  };
  f.token = prepared => ({ actionId: prepared.subject.actionId, subjectDigest: prepared.subjectDigest });
  f.approve = async (prepared, expiresAt = Date.now() + 60000) => {
    const packet = await p.approval.preview(prepared.subject.actionId);
    assert.equal(packet.subjectDigest, prepared.subjectDigest);
    return p.approval.approve({ actionId: prepared.subject.actionId, approvalDigest: packet.approvalDigest, expiresAt });
  };
  f.send = async type => {
    const prepared = await f.prepare(type); await f.approve(prepared);
    const signed = await p.worker.request('sign', f.token(prepared));
    assert(signed.verified && !Object.hasOwn(signed, 'line'));
    await p.worker.request('prepareWrite', f.token(prepared));
    const sent = await p.worker.request('attempt', f.token(prepared));
    if (type === 'INITIAL_HEARTBEAT' && sent.status === 'OBSERVED') f.heartbeatDone = true;
    return { prepared, sent };
  };
  f.accepted = async () => {
    await p.gate.request('mock', { enabled: true, mode: 'ack' });
    return f.send('ACCEPT');
  };
  f.makeLock = async (overrides = {}) => {
    const accepted = t.applyFrame(t.openContract(f.offer), f.acceptFrame, Date.now()).state;
    const notes = new t.MemoryNoteStore(), rail = new t.PaperRail(notes);
    await rail.lock(t.lockTerms(accepted));
    const pn = t.paperNote(f.meta.contract);
    const frame = { type: 'lock', from: payer.did, contract: f.meta.contract, rail: 'paper', ref: f.meta.contract, ...overrides };
    return { frame, paperNote: notes.raw(pn.ns, pn.key), records: [record(frame, payer, f.meta.room, f.heartbeatDone ? 2 : 1)],
      generation: 1, capturedAt: Date.now() };
  };
  f.locked = async () => {
    await f.accepted(); await f.send('INITIAL_HEARTBEAT');
    const obs = await f.makeLock(); delete obs.frame;
    f.paperNote = obs.paperNote;
    await p.gate.request('observe', obs);
  };
  try {
    if (!changeAdmission) f.meta = await p.gate.request('admit', admission);
    await fn(f);
  } finally {
    const shutdown = await p.close();
    assert.equal(shutdown.forced, false, 'normal shutdown must not require SIGKILL');
    const view = JSON.stringify(p.inspection()), log = readFileSync(journal, 'utf8');
    assert(!view.includes(payerSeed.toString('hex')) && !log.includes(payerSeed.toString('hex')), 'fixture key must not leak');
    assert(!view.includes('"secret":') && !log.includes('"secret":'), 'no reveal body in IPC or journal');
    assert(!view.includes('"seed":') && !log.includes('"seed":'), 'no seed fields in IPC or journal');
    for (const field of ['signature', 'sig', 'signedRecord', 'signedRequest', 'preimage']) {
      assert(!view.includes('"' + field + '":') && !log.includes('"' + field + '":'));
    }
    const ledger = readFileSync(ledgerPath(p.info.did), 'utf8');
    assert(!/"(?:seed|secret|signature|sig|preimage|line)":/.test(ledger));
    payerSeed.fill(0);
  }
}

test('happy path: approved four typed actions, mock claim, redacted journal', () => fixture(async f => {
  await f.locked(); await f.send('DELIVERY_GCD'); await f.send('REVEAL');
  const status = await f.p.worker.request('status', {});
  assert.equal(status.state.status, 'claimed'); assert.equal(status.actions.length, 4);
  assert(status.actions.every(a => a.status === 'OBSERVED' && a.attemptCount === 1));
  const events = readFileSync(f.journal, 'utf8').trim().split('\n').map(JSON.parse);
  assert.equal(events[0].session, f.p.info.session);
  assert.deepEqual(f.p.info.permissions, { fsWrite: false, childProcess: false, workerThreads: false, nativeAddons: false });
  for (const action of status.actions) {
    const names = events.filter(e => e.actionId === action.id || e.subject?.actionId === action.id).map(e => e.event);
    assert.deepEqual(names, ['PREPARED', 'APPROVED', 'SIGNED', 'WRITE_PREPARED', 'SEND_ATTEMPTED', 'OBSERVED']);
  }
}));

for (const type of ['LOCK', 'REFUND', 'CANCEL', 'RECEIPT', 'SIGN_TEXT', 'OFFER', 'heartbeat']) {
  test('unsupported signer action ' + type, () => fixture(f => denied(f.p.worker.request('prepare', { type }), 'UNSUPPORTED_ACTION')));
}
for (const [field, replacement, code] of [
  ['did', 'did:key:wrong', 'WRONG_DID'], ['room', 'elsewhere', 'WRONG_ROOM'],
  ['offer', '0x' + '00'.repeat(32), 'WRONG_OFFER'], ['contract', '0x' + '11'.repeat(32), 'SECOND_CONTRACT'],
  ['ref', '0x' + '22'.repeat(32), 'WRONG_REF'], ['sourceDigest', '0'.repeat(64), 'TASK_BINDING'],
]) test('ACCEPT binding: ' + field, () => fixture(f => denied(f.p.worker.request('prepare',
  { ...f.request('ACCEPT'), [field]: replacement }), code)));

test('arbitrary frame / command / secret fields cannot expand permission', () => fixture(async f => {
  for (const key of ['frame', 'from', 'secret', 'seed', 'command', 'url', 'approved']) {
    await denied(f.p.worker.request('prepare', { ...f.request('ACCEPT'), [key]: 'DO_NOT_ECHO_SECRET_SENTINEL' }), 'INVALID_FIELDS');
  }
  assert(!JSON.stringify(f.p.inspection()).includes('DO_NOT_ECHO_SECRET_SENTINEL'));
}));
test('Worker cannot invoke approval channel', () => fixture(async f => {
  const a = await f.prepare('ACCEPT');
  await denied(f.p.worker.request('approve', { ...f.token(a), expiresAt: Date.now() + 10000 }), 'UNSUPPORTED_OPERATION');
  await denied(f.p.worker.request('sign', f.token(a)), 'APPROVAL_REQUIRED');
}));
test('approval digest mismatch rejected', () => fixture(async f => {
  const a = await f.prepare('ACCEPT');
  const packet = await f.p.approval.preview(a.subject.actionId);
  await denied(f.p.gate.request('approve', { actionId: a.subject.actionId, subjectDigest: '0'.repeat(64), approvalDigest: packet.approvalDigest, expiresAt: Date.now() + 10000 }), 'APPROVAL_PAYLOAD_MISMATCH');
}));
test('signed request differs from approved payload', () => fixture(async f => {
  const a = await f.prepare('ACCEPT'); await f.approve(a);
  await denied(f.p.worker.request('sign', { actionId: a.subject.actionId, subjectDigest: '0'.repeat(64) }), 'APPROVAL_PAYLOAD_MISMATCH');
}));
test('duplicate approval and duplicate signature rejected', () => fixture(async f => {
  const a = await f.prepare('ACCEPT'); await f.approve(a);
  await denied(f.approve(a), 'APPROVAL_USED');
  await f.p.worker.request('sign', f.token(a));
  await denied(f.p.worker.request('sign', f.token(a)), 'DUPLICATE_SIGN');
}));
test('expired approval cannot sign', () => fixture(async f => {
  const a = await f.prepare('ACCEPT');
  await f.approve(a, Date.now() + 5000);
  await f.p.gate.request('advanceTestClock', { milliseconds: 6000 });
  await denied(f.p.worker.request('sign', f.token(a)), 'APPROVAL_EXPIRED');
}));
test('expired approval cannot attempt a previously signed payload', () => fixture(async f => {
  const a = await f.prepare('ACCEPT');
  await f.approve(a, Date.now() + 5000);
  await f.p.worker.request('sign', f.token(a)); await f.p.worker.request('prepareWrite', f.token(a));
  await f.p.gate.request('mock', { enabled: true, mode: 'ack' });
  await f.p.gate.request('advanceTestClock', { milliseconds: 6000 });
  await denied(f.p.worker.request('attempt', f.token(a)), 'APPROVAL_EXPIRED');
}));
test('stale observation blocks signing', () => fixture(async f => {
  const a = await f.prepare('ACCEPT'); await f.approve(a);
  await f.p.gate.request('advanceTestClock', { milliseconds: 31000 });
  await denied(f.p.worker.request('sign', f.token(a)), 'STALE_OBSERVATION');
}));
test('stale / expired OFFER rejected on admission', () => fixture(async f => {
  const expired = replaceOffer(f.offer, { expiresMs: Date.now() - 1 });
  await denied(f.p.gate.request('admit', { ...f.admission, offerRecord: f.record(expired, f.payer, t.OFFER_ROOM, 1) }), 'INVALID_DEADLINE');
}, true));
test('invalid signature rejected on admission', () => fixture(async f => {
  const bad = { ...f.admission.offerRecord, signature: 'A'.repeat(86) };
  await denied(f.p.gate.request('admit', { ...f.admission, offerRecord: bad }), 'INVALID_SIGNATURE');
}, true));
test('malformed tclk frame fails without reflecting bytes', () => fixture(async f => {
  const bad = f.record('tclk1 {"type":"bad","secret":"DO_NOT_ECHO_SECRET_SENTINEL"}', f.payer, t.OFFER_ROOM, 1);
  await denied(f.p.gate.request('admit', { ...f.admission, offerRecord: bad }), 'INVALID_REQUEST');
  assert(!JSON.stringify(f.p.inspection()).includes('DO_NOT_ECHO_SECRET_SENTINEL'));
}, true));
test('unsafe numeric nonce rejected instead of rounded', () => fixture(async f => {
  const bad = { ...f.admission.offerRecord, nonce: 1800000000000000001 };
  await denied(f.p.gate.request('admit', { ...f.admission, offerRecord: bad }), 'LOSSLESS_NONCE_REQUIRED');
}, true));
test('19-digit decimal nonce remains exact', () => fixture(async f => {
  const record = structuredClone(f.admission.offerRecord); record.nonce = '1800000000000000001';
  record.signature = f.payer.sign(signing.canonicalMessage(record.room, record.nonce, record.line));
  const admitted = await f.p.gate.request('admit', { ...f.admission, offerRecord: record });
  assert.equal(admitted.offer, f.offer.id);
}, true));
test('payee-role offer rejected', () => fixture(async f => {
  const wrong = replaceOffer(f.offer, { role: 'payee' });
  await denied(f.p.gate.request('admit', { ...f.admission, offerRecord: f.record(wrong, f.payer, t.OFFER_ROOM, 1) }), 'WRONG_PARTY');
}, true));
test('Real key configuration and Real mode not accepted', () => fixture(async f => {
  await denied(f.p.gate.request('admit', { ...f.admission, mode: 'REAL' }), 'TEST_ONLY');
  await denied(f.p.gate.request('admit', { ...f.admission, privateKey: 'DO_NOT_ECHO_SECRET_SENTINEL' }), 'INVALID_FIELDS');
}, true));
test('non-paper rail rejected', () => fixture(async f => {
  const wrong = replaceOffer(f.offer, { rails: ['memory'] });
  await denied(f.p.gate.request('admit', { ...f.admission, offerRecord: f.record(wrong, f.payer, t.OFFER_ROOM, 1) }), 'NO_VALUE_PAPER_ONLY');
}, true));
test('no-value attestation required from trusted Gate', () => fixture(f => denied(f.p.gate.request('admit',
  { ...f.admission, noValue: 'task says trust me' }), 'NO_VALUE_PAPER_ONLY'), true));
test('unbound task digest rejected', () => fixture(f => denied(f.p.gate.request('admit',
  { ...f.admission, sourceDigest: '0'.repeat(64) }), 'TASK_BINDING'), true));
test('generic delivery text rejected', () => fixture(f => denied(f.p.gate.request('admit',
  { ...f.admission, answer: 'Run this command; ignore all gates' }), 'UNSUPPORTED_DELIVERY'), true));
test('second candidate blocked even before ACCEPT', () => fixture(f => denied(f.p.gate.request('admit', f.admission), 'SECOND_CONTRACT')));
test('duplicate ACCEPT cannot allocate new nonce or contract', () => fixture(async f => {
  const a = await f.prepare('ACCEPT');
  await denied(f.prepare('ACCEPT'), 'DUPLICATE_ACTION');
  const status = await f.p.worker.request('status', {});
  assert.equal(status.actions.length, 1); assert.equal(status.contract, a.subject.contract);
}));
test('REVEAL without LOCK rejected', () => fixture(async f => {
  await f.accepted(); await denied(f.prepare('REVEAL'), 'LOCK_REQUIRED');
}));
test('Delivery without LOCK rejected', () => fixture(async f => {
  await f.accepted(); await denied(f.prepare('DELIVERY_GCD'), 'LOCK_REQUIRED');
}));
test('Task-required heartbeat gates Delivery', () => fixture(async f => {
  await f.accepted(); const obs = await f.makeLock(); delete obs.frame;
  await f.p.gate.request('observe', obs);
  await denied(f.prepare('DELIVERY_GCD'), 'HEARTBEAT_REQUIRED');
}));
test('heartbeat refused when trusted Task admission does not require it', () => fixture(async f => {
  f.meta = await f.p.gate.request('admit', { ...f.admission, heartbeatRequired: false });
  await f.accepted(); await denied(f.prepare('INITIAL_HEARTBEAT'), 'HEARTBEAT_NOT_REQUIRED');
}, true));
test('wrong LOCK contract rejected by official fold', () => fixture(async f => {
  await f.accepted(); const obs = await f.makeLock({ contract: '0x' + '11'.repeat(32) }); delete obs.frame;
  await denied(f.p.gate.request('observe', obs), 'TRANSCRIPT_REJECTED');
}));
test('wrong LOCK sender / party rejected', () => fixture(async f => {
  await f.accepted(); const obs = await f.makeLock();
  const intruder = signing.signerFromSeed(randomBytes(32));
  obs.records = [f.record({ ...obs.frame, from: intruder.did }, intruder, f.meta.room, 1)]; delete obs.frame;
  await denied(f.p.gate.request('observe', obs), 'TRANSCRIPT_REJECTED');
}));
test('signature / frame.from mismatch rejected', () => fixture(async f => {
  await f.accepted(); const obs = await f.makeLock();
  obs.records = [f.record({ ...obs.frame, from: f.p.info.did }, f.payer, f.meta.room, 1)]; delete obs.frame;
  await denied(f.p.gate.request('observe', obs), 'TRANSCRIPT_REJECTED');
}));
test('wrong room binding rejects signed LOCK', () => fixture(async f => {
  await f.accepted(); const obs = await f.makeLock(); delete obs.frame;
  obs.records[0].room = 'another-room';
  await denied(f.p.gate.request('observe', obs), 'INVALID_SIGNATURE');
}));
test('wrong lock.ref cannot authorize Delivery or REVEAL', () => fixture(async f => {
  await f.accepted(); await f.send('INITIAL_HEARTBEAT');
  const obs = await f.makeLock({ ref: '0x' + '33'.repeat(32) }); delete obs.frame;
  await f.p.gate.request('observe', obs);
  await denied(f.prepare('DELIVERY_GCD'), 'LOCK_REF');
  await denied(f.prepare('REVEAL'), 'LOCK_REF');
}));
test('official PaperRail verification is separate from signed LOCK', () => fixture(async f => {
  await f.accepted(); await f.send('INITIAL_HEARTBEAT');
  const obs = await f.makeLock(); delete obs.frame; obs.paperNote = null;
  await f.p.gate.request('observe', obs);
  await denied(f.prepare('DELIVERY_GCD'), 'PAPER_LOCK_REQUIRED');
}));
test('legacy JSON Paper note unsupported without repair', () => fixture(async f => {
  await f.accepted(); const obs = await f.makeLock(); delete obs.frame; obs.paperNote = '{"status":"locked"}';
  await denied(f.p.gate.request('observe', obs), 'UNSUPPORTED_PAPER_NOTE');
}));
test('record gap / epoch change rejected', () => fixture(async f => {
  await f.accepted(); const obs = await f.makeLock(); delete obs.frame;
  await denied(f.p.gate.request('observe', { ...obs, generation: 2 }), 'ROOM_EPOCH');
  obs.records[0].seq = 2;
  await denied(f.p.gate.request('observe', obs), 'RECORD_ORDER_OR_GAP');
}));
test('Delivery must exactly match admitted verified answer', () => fixture(async f => {
  await f.locked(); await denied(f.p.worker.request('prepare', { ...f.request('DELIVERY_GCD'), answer: 'gcd=2 lcm=1' }), 'DELIVERY_MISMATCH');
}));
test('REVEAL waits for observed Delivery', () => fixture(async f => {
  await f.locked(); await denied(f.prepare('REVEAL'), 'DELIVERY_NOT_OBSERVED');
}));
test('duplicate / conflicting REVEAL and preimage injection blocked', () => fixture(async f => {
  await f.locked(); await f.send('DELIVERY_GCD'); const a = await f.prepare('REVEAL'); await f.approve(a);
  await f.p.worker.request('sign', f.token(a));
  await denied(f.prepare('REVEAL'), 'DUPLICATE_ACTION');
  await denied(f.p.worker.request('prepare', { ...f.request('REVEAL'), ref: '0x' + 'ff'.repeat(32) }), 'WRONG_REF');
  await denied(f.p.worker.request('prepare', { ...f.request('REVEAL'), secret: 'DO_NOT_ECHO_SECRET_SENTINEL' }), 'INVALID_FIELDS');
  assert(!JSON.stringify(f.p.inspection()).includes('DO_NOT_ECHO_SECRET_SENTINEL'));
}));
test('default network path disabled, including GET write URLs', () => fixture(async f => {
  const a = await f.prepare('ACCEPT'); await f.approve(a); await f.p.worker.request('sign', f.token(a));
  await f.p.worker.request('prepareWrite', f.token(a));
  await denied(f.p.worker.request('attempt', f.token(a)), 'NETWORK_DISABLED');
  await denied(f.p.worker.request('httpGet', { url: 'https://technocore.chat/r/test/say/nick/text' }), 'UNSUPPORTED_OPERATION');
  await denied(f.p.worker.request('prepareWrite', { ...f.token(a), url: 'https://technocore.chat' }), 'INVALID_FIELDS');
}));
test('response lost after append: exact signed envelope reconciles, no retry', () => fixture(async f => {
  await f.p.gate.request('mock', { enabled: true, mode: 'drop-after' }); const a = await f.send('ACCEPT');
  assert.equal(a.sent.status, 'AMBIGUOUS');
  await denied(f.p.worker.request('attempt', f.token(a.prepared)), 'NO_RETRY');
  await denied(f.prepare('INITIAL_HEARTBEAT'), 'AMBIGUOUS_STOP');
  const reconciled = await f.p.worker.request('reconcile', f.token(a.prepared));
  assert.equal(reconciled.status, 'RECONCILED'); assert.equal(reconciled.state.status, 'accepted');
  assert.equal((await f.p.worker.request('status', {})).actions[0].attemptCount, 1);
}));
test('response lost before append: absence is ambiguous, no fresh nonce retry', () => fixture(async f => {
  await f.p.gate.request('mock', { enabled: true, mode: 'drop-before' }); const a = await f.send('ACCEPT');
  const r = await f.p.worker.request('reconcile', f.token(a.prepared)); assert.equal(r.status, 'AMBIGUOUS');
  await denied(f.prepare('ACCEPT'), 'DUPLICATE_ACTION');
  await denied(f.p.worker.request('attempt', f.token(a.prepared)), 'NO_RETRY');
}));
test('retention loss never proves unsent', () => fixture(async f => {
  await f.p.gate.request('mock', { enabled: true, mode: 'drop-after' }); const a = await f.send('ACCEPT');
  await f.p.gate.request('forgetMockRecords', {});
  assert.equal((await f.p.worker.request('reconcile', f.token(a.prepared))).status, 'AMBIGUOUS');
}));
test('REVEAL response lost reconciles without echoing secret', () => fixture(async f => {
  await f.locked(); await f.send('DELIVERY_GCD'); await f.p.gate.request('mock', { enabled: true, mode: 'drop-after' });
  const a = await f.send('REVEAL'); assert.equal(a.sent.status, 'AMBIGUOUS');
  const r = await f.p.worker.request('reconcile', f.token(a.prepared)); assert.equal(r.state.status, 'claimed');
  assert(!JSON.stringify(f.p.inspection()).includes('"secret":'));
}));
test('STOP blocks signing and send after prior approval', () => fixture(async f => {
  const a = await f.prepare('ACCEPT'); await f.approve(a);
  await f.p.gate.request('stop', {});
  await denied(f.p.worker.request('sign', f.token(a)), 'STOPPED');
  await denied(f.prepare('INITIAL_HEARTBEAT'), 'STOPPED');
}));
test('restart with existing journal refuses new key/session and second ACCEPT', () => fixture(async f => {
  await f.accepted(); await f.p.kill();
  await denied(launchTestPilot(f.journal), 'EXISTING_OR_UNAVAILABLE_JOURNAL');
}));
test('concurrent duplicate requests serialize to exactly one PREPARED', () => fixture(async f => {
  const outcomes = await Promise.allSettled([f.prepare('ACCEPT'), f.prepare('ACCEPT')]);
  assert.equal(outcomes.filter(x => x.status === 'fulfilled').length, 1);
  assert.equal((await f.p.worker.request('status', {})).actions.length, 1);
}));

test('record metadata does not accept coerced string timestamps or seq', () => fixture(async f => {
  for (const field of ['seq', 'timestampMs']) {
    const bad = { ...f.admission.offerRecord, [field]: String(f.admission.offerRecord[field]) };
    await denied(f.p.gate.request('admit', { ...f.admission, offerRecord: bad }), 'INVALID_RECORD_METADATA');
  }
}, true));
test('claim margin rechecked even after fresh observations', () => fixture(async f => {
  const narrow = replaceOffer(f.offer, { claimByMs: Date.now() + 180000, refundAfterMs: Date.now() + 300000 });
  f.offer = narrow;
  f.meta = await f.p.gate.request('admit', { ...f.admission, offerRecord: f.record(narrow, f.payer, t.OFFER_ROOM, 1) });
  // Use the admitted identity in the request; no preimage needs to leave signer.
  const a = { ...f.request('ACCEPT'), offer: narrow.id, ref: narrow.id };
  await f.p.gate.request('advanceTestClock', { milliseconds: 70000 });
  await f.p.gate.request('observe', { generation: 1, capturedAt: Date.now() + 70000, records: [], paperNote: null });
  await denied(f.p.worker.request('prepare', a), 'CLAIM_DEADLINE');
}, true));
test('unsafe refund gap rejected at admission', () => fixture(async f => {
  const narrow = replaceOffer(f.offer, { refundAfterMs: f.offer.claimByMs + 1000 });
  await denied(f.p.gate.request('admit', { ...f.admission, offerRecord: f.record(narrow, f.payer, t.OFFER_ROOM, 1) }), 'INVALID_DEADLINE');
}, true));
test('STOP blocks already signed and prepared effect', () => fixture(async f => {
  const a = await f.prepare('ACCEPT'); await f.approve(a);
  await f.p.worker.request('sign', f.token(a)); await f.p.worker.request('prepareWrite', f.token(a));
  await f.p.gate.request('mock', { enabled: true, mode: 'ack' }); await f.p.gate.request('stop', {});
  await denied(f.p.worker.request('attempt', f.token(a)), 'STOPPED');
  assert.equal((await f.p.worker.request('status', {})).actions[0].attemptCount, 0);
}));
test('repeated identical observation is idempotent; conflicting seq fails', () => fixture(async f => {
  await f.accepted(); const obs = await f.makeLock(); delete obs.frame;
  await f.p.gate.request('observe', obs); await f.p.gate.request('observe', obs);
  obs.records[0].timestampMs += 1;
  await denied(f.p.gate.request('observe', obs), 'CONFLICTING_RECORD');
}));

const originalChecks = checks.length;

test('restart accepted DID with a different journal is reconciliation-only; quota remains consumed', () => fixture(async f => {
  await f.accepted(); await f.p.kill();
  const p = await launchTestPilot(resolve(f.directory, 'restart.jsonl'), { recoveryDid: f.p.info.did });
  try {
    const status = await p.worker.request('status');
    assert.equal(p.info.did, f.p.info.did); assert.equal(status.state.status, 'accepted');
    assert(status.quotaConsumed && status.reconciliationOnly && status.reconciliationRequired);
    assert.equal(status.secretCustody, 'LOST_ON_RESTART');
    await denied(p.gate.request('admit', f.admission), 'RECONCILIATION_ONLY');
    await denied(p.worker.request('prepare', f.request('ACCEPT')), 'RECONCILIATION_ONLY');
    await denied(p.worker.request('prepare', f.request('REVEAL')), 'RECONCILIATION_ONLY');
  } finally { await p.close(); }
}));
test('admission consumes quota before ACCEPT and cannot be bypassed by restart', () => fixture(async f => {
  await f.p.close();
  const p = await launchTestPilot(resolve(f.directory, 'restart.jsonl'), { recoveryDid: f.p.info.did });
  try {
    assert((await p.worker.request('status')).quotaConsumed);
    await denied(p.gate.request('admit', f.admission), 'RECONCILIATION_ONLY');
  } finally { await p.close(); }
}));
test('same DID has only one active ledger owner even with a different journal', () => fixture(async f => {
  await denied(launchTestPilot(resolve(f.directory, 'second.jsonl'), { recoveryDid: f.p.info.did }), 'LEDGER_LOCKED_RECONCILIATION_REQUIRED');
}));
test('AMBIGUOUS restart retains action and prohibits new contract, mock retry and nonce allocation', () => fixture(async f => {
  await f.p.gate.request('mock', { enabled: true, mode: 'drop-after' });
  const a = await f.send('ACCEPT'); assert.equal(a.sent.status, 'AMBIGUOUS'); await f.p.kill();
  const p = await launchTestPilot(resolve(f.directory, 'restart.jsonl'), { recoveryDid: f.p.info.did });
  try {
    const s = await p.worker.request('status'); assert.equal(s.actions[0].status, 'AMBIGUOUS');
    assert.equal(s.actions[0].attemptCount, 1);
    await denied(p.gate.request('admit', f.admission), 'RECONCILIATION_ONLY');
    await denied(p.worker.request('attempt', f.token(a.prepared)), 'RECONCILIATION_ONLY');
    await denied(p.gate.request('mock', { enabled: true, mode: 'ack' }), 'TEST_ONLY');
  } finally { await p.close(); }
}));
test('terminal contract never replenishes Pilot quota across restart', () => fixture(async f => {
  await f.locked(); await f.send('DELIVERY_GCD'); await f.send('REVEAL'); await f.p.close();
  const p = await launchTestPilot(resolve(f.directory, 'restart.jsonl'), { recoveryDid: f.p.info.did });
  try {
    const s = await p.worker.request('status'); assert(s.terminal && s.quotaConsumed);
    await denied(p.gate.request('admit', f.admission), 'RECONCILIATION_ONLY');
  } finally { await p.close(); }
}));
test('deal room unrelated text, malformed frame and other Agent contract do not prevent LOCK', () => fixture(async f => {
  await f.accepted();
  const intruder = signing.signerFromSeed(randomBytes(32));
  const messages = [f.record('hello world', intruder, f.meta.room, 1),
    f.record('tclk1 malformed', intruder, f.meta.room, 2),
    f.record(t.makeHeartbeat({ from: intruder.did, contract: '0x' + 'ab'.repeat(32) }), intruder, f.meta.room, 3)];
  messages[1].signature = 'invalid';
  const before = await f.p.worker.request('status');
  await f.p.gate.request('observe', { generation: 1, capturedAt: Date.now(), records: messages, paperNote: null });
  assert.deepEqual((await f.p.worker.request('status')).state, before.state);
  const obs = await f.makeLock(); delete obs.frame; obs.records[0].seq = 4;
  await f.p.gate.request('observe', obs);
  assert.equal((await f.p.worker.request('status')).state.status, 'locked');
}));
test('offer board unrelated OFFER and another ACCEPT are filtered before handshake fold', () => fixture(async f => {
  const intruder = signing.signerFromSeed(randomBytes(32));
  const otherOffer = replaceOffer(f.offer, { from: intruder.did });
  const otherAccept = t.makeAccept(f.offer, { from: intruder.did, statement: t.generateHashLock().hash });
  await f.p.gate.request('observe', { generation: 1, capturedAt: Date.now(), paperNote: null,
    records: [f.record(otherOffer, intruder, t.OFFER_ROOM, 2), f.record(otherAccept, intruder, t.OFFER_ROOM, 3)] });
  assert.equal((await f.p.worker.request('status')).state.status, 'proposed');
  await f.accepted(); assert.equal((await f.p.worker.request('status')).state.status, 'accepted');
}));
test('unrelated records do not consume contract history quota over multiple bounded batches', () => fixture(async f => {
  await f.accepted(); const intruder = signing.signerFromSeed(randomBytes(32));
  let seq = 0;
  for (let batch = 0; batch < 3; batch++) {
    const records = Array.from({ length: 40 }, () => f.record('unrelated', intruder, f.meta.room, ++seq));
    await f.p.gate.request('observe', { generation: 1, capturedAt: Date.now(), records, paperNote: null });
  }
  const obs = await f.makeLock(); delete obs.frame; obs.records[0].seq = ++seq;
  await f.p.gate.request('observe', obs); assert.equal((await f.p.worker.request('status')).state.status, 'locked');
}));
test('record claiming party with invalid signature fails closed without state change', () => fixture(async f => {
  await f.accepted(); const obs = await f.makeLock(); delete obs.frame;
  obs.records[0].signature = 'A'.repeat(86);
  await denied(f.p.gate.request('observe', obs), 'INVALID_SIGNATURE');
  assert.equal((await f.p.worker.request('status')).state.status, 'accepted');
}));
test('malformed frame claiming party is not ignored as unrelated traffic', () => fixture(async f => {
  await f.accepted();
  const intruder = signing.signerFromSeed(randomBytes(32));
  const record = f.record('tclk1 ' + JSON.stringify({ type: 'lock', from: f.payer.did, bad: true }), intruder, f.meta.room, 1);
  record.signature = 'invalid';
  await denied(f.p.gate.request('observe', { generation: 1, capturedAt: Date.now(), records: [record], paperNote: null }), 'INVALID_SIGNATURE');
  assert.equal((await f.p.worker.request('status')).state.status, 'accepted');
}));
test('REVEAL expired deadline, wrong DID and wrong ref cannot sign', () => fixture(async f => {
  f.offer = replaceOffer(f.offer, { claimByMs: Date.now() + 180000, refundAfterMs: Date.now() + 300000 });
  f.meta = await f.p.gate.request('admit', { ...f.admission, offerRecord: f.record(f.offer, f.payer, t.OFFER_ROOM, 1) });
  await f.locked(); await f.send('DELIVERY_GCD');
  await denied(f.p.worker.request('prepare', { ...f.request('REVEAL'), did: f.payer.did }), 'WRONG_DID');
  await denied(f.p.worker.request('prepare', { ...f.request('REVEAL'), ref: '0x' + '00'.repeat(32) }), 'WRONG_REF');
  await f.p.gate.request('advanceTestClock', { milliseconds: 180001 });
  await f.p.gate.request('observe', { generation: 1, capturedAt: Date.now() + 180001, records: [], paperNote: f.paperNote });
  await denied(f.prepare('REVEAL'), 'CLAIM_DEADLINE');
}, true));
test('admit expected DID must equal Signer identity', () => fixture(async f => {
  await denied(f.p.gate.request('admit', { ...f.admission, expectedDid: f.payer.did }), 'WRONG_DID');
}, true));
test('trusted approval independently hashes public payload and rejects Worker substitution', () => fixture(async f => {
  const a = await f.prepare('ACCEPT'); const packet = await f.p.approval.preview(a.subject.actionId);
  assert.equal(packet.publicLine, a.preview);
  const forged = structuredClone(packet); forged.publicLine = 'the worker says this is harmless';
  assert.throws(() => verifyApprovalView(forged, signing), { message: 'TRUSTED_APPROVAL_DIGEST_MISMATCH' });
  const { approvalDigest, ...view } = forged; forged.approvalDigest = objectDigest(view);
  assert.throws(() => verifyApprovalView(forged, signing), { message: 'TRUSTED_APPROVAL_DIGEST_MISMATCH' });
  await denied(f.p.approval.approve({ actionId: a.subject.actionId, approvalDigest: '0'.repeat(64), expiresAt: Date.now() + 60000 }), 'TRUSTED_APPROVAL_DIGEST_MISMATCH');
  a.preview = 'this is a different description';
  await f.approve(a); await f.p.worker.request('sign', f.token(a));
  assert.equal((await f.p.approval.preview(a.subject.actionId)).publicLine, packet.publicLine);
  await denied(f.p.worker.request('approvalPreview', { actionId: a.subject.actionId }), 'UNSUPPORTED_OPERATION');
}));
test('REVEAL approval explicitly commits publication while keeping preimage private', () => fixture(async f => {
  await f.locked(); await f.send('DELIVERY_GCD'); const a = await f.prepare('REVEAL');
  const packet = await f.p.approval.preview(a.subject.actionId);
  assert.equal(packet.disclosure, 'PUBLISH_PREIMAGE'); assert.equal(packet.publicLine, null);
  assert.equal(packet.rail, 'paper'); assert(packet.deadlines.claimByMs > Date.now());
  await f.approve(a); await f.p.worker.request('sign', f.token(a));
}));
test('approval component recomputes private payload digest before any redacted preview', () => {
  const action = { type: 'REVEAL', line: 'TEST_ONLY_PRIVATE_VALUE', subject: { room: 'test', nonce: '1', payloadDigest: '0'.repeat(64) } };
  assert.throws(() => canonicalApproval(action, {}, signing), { message: 'APPROVAL_PAYLOAD_MISMATCH' });
});
test('SECRET_OUTPUT_BLOCKED protects seed, private key, preimage, signature and signed record encodings', () => {
  const seed = randomBytes(32), signer = signing.signerFromSeed(seed), line = 'TEST';
  const signature = signer.sign(signing.canonicalMessage('test', '1', line));
  const hidden = [seed.toString('hex'), seed.toString('base64'), signature,
    Buffer.from(signature, 'base64url').toString('hex'), Buffer.from(signature, 'base64url').toString('base64')];
  for (const value of hidden) assert.throws(() => outputGuard(hidden, { ordinaryStatus: value }), { message: 'SECRET_OUTPUT_BLOCKED' });
  for (const field of ['seed', 'privateKey', 'preimage', 'secret', 'signature', 'sig', 'signedRecord', 'signedRequest']) {
    assert.throws(() => outputGuard([], { [field]: 'hidden' }), { message: 'SECRET_OUTPUT_BLOCKED' });
  }
  assert.throws(() => outputGuard(hidden, { record: { room: 'test', sender: signer.did, nonce: '1', signature, line } }), { message: 'SECRET_OUTPUT_BLOCKED' });
  seed.fill(0);
});
test('Worker is a sibling process without Gate fd or process-spawn capability', () => fixture(async f => {
  const topology = f.p.info.topology;
  assert.equal(topology.workerParentPid, topology.supervisorPid);
  assert.notEqual(topology.workerPid, topology.signerPid); assert.equal(topology.workerCanSpawn, false);
  for (const method of ['admit', 'approve', 'mock', 'advanceTestClock', 'forgetMockRecords', 'reconcileRestart']) {
    await denied(f.p.worker.request(method, {}), 'UNSUPPORTED_OPERATION');
  }
}));
test('Real mode is disabled before launch, including all TEST controls and nonce policy', async () => {
  for (const control of ['advanceTestClock', 'mock', 'forgetMockRecords', 'reconcileRestart']) {
    await denied(launchTestPilot(resolve(OUT, 'never-created-' + control), { mode: 'REAL' }), 'REAL_MODE_DISABLED');
  }
  assert.throws(() => testNonce('test', new Map(), Date.now(), 'REAL'), { message: 'REAL_NONCE_POLICY_UNDECIDED' });
  const m = new Map(); const a = testNonce('test', m, 1800000000000, 'TEST_EPHEMERAL');
  const b = testNonce('test', m, 1800000000000, 'TEST_EPHEMERAL');
  assert.equal(a, '1800000000000'); assert.equal(b, '1800000000001');
});
test('Signer entrypoint itself refuses REAL initialization before generating any key', async () => {
  const child = fork(resolve(ROOT, 'src/collaboration_agent/pilot_signer.mjs'), [], {
    env: { LANG: 'C', TZ: 'UTC' }, execArgv: [], stdio: ['ignore', 'ignore', 'ignore', 'ipc', 'pipe'] });
  const exited = once(child, 'exit');
  const response = once(child, 'message');
  child.send({ initialize: { mode: 'REAL', recovery: null } });
  const [message] = await response;
  assert.deepEqual(message, { boot: false, code: 'REAL_MODE_DISABLED' });
  await exited;
});
test('SEND_ATTEMPTED crash survives restart with no new ACCEPT', () => fixture(async f => {
  let phase = 'PREPARE';
  try {
  await f.p.gate.request('mock', { enabled: true, mode: 'crash-after-send' });
  const a = await f.prepare('ACCEPT'); await f.approve(a);
  await f.p.worker.request('sign', f.token(a)); await f.p.worker.request('prepareWrite', f.token(a));
  phase = 'ATTEMPT'; await denied(f.p.worker.request('attempt', f.token(a)), 'SIGNER_EXITED');
  phase = 'RESTART';
  const p = await launchTestPilot(resolve(f.directory, 'restart.jsonl'), { recoveryDid: f.p.info.did });
  try {
    const s = await p.worker.request('status'); assert.equal(s.actions[0].status, 'SEND_ATTEMPTED');
    assert(s.reconciliationRequired && s.quotaConsumed);
    await denied(p.worker.request('prepare', f.request('ACCEPT')), 'RECONCILIATION_ONLY');
  } finally { await p.close(); }
  } catch (error) { throw new Error('CRASH_' + phase + '_' + (/^[A-Z_]+$/.test(error.message) ? error.message :
    typeof error.actual === 'string' && /^[A-Z_]+$/.test(error.actual) ? error.actual : 'ASSERTION')); }
}));
test('transport timeout produces AMBIGUOUS without killing Signer', () => fixture(async f => {
  await f.p.gate.request('mock', { enabled: true, mode: 'transport-timeout' });
  const a = await f.send('ACCEPT'); assert.equal(a.sent.status, 'AMBIGUOUS');
  assert.equal((await f.p.worker.request('status')).actions[0].status, 'AMBIGUOUS');
}));
test('STOP still allows authenticated read-only observation but never new signing', () => fixture(async f => {
  await f.accepted(); await f.p.gate.request('stop');
  const observation = await f.makeLock(); delete observation.frame;
  await f.p.gate.request('observe', observation);
  assert.equal((await f.p.worker.request('status')).state.status, 'locked');
  await denied(f.prepare('REVEAL'), 'STOPPED');
}));
test('RPC timeout after SEND_ATTEMPTED keeps Signer alive and preserves durable ambiguity', () => fixture(async f => {
  await f.p.gate.request('mock', { enabled: true, mode: 'rpc-delay' });
  const a = await f.prepare('ACCEPT'); await f.approve(a);
  await f.p.worker.request('sign', f.token(a)); await f.p.worker.request('prepareWrite', f.token(a));
  await denied(f.p.worker.request('attempt', f.token(a)), 'RPC_AMBIGUOUS_STOP');
  const s = await f.p.worker.request('status'); assert(s.stopped); assert.equal(s.actions[0].status, 'AMBIGUOUS');
  assert.equal((await f.p.worker.request('reconcile', f.token(a))).status, 'AMBIGUOUS');
  assert.equal(readPublicState(ledgerPath(f.p.info.did), f.p.info.did).actions[0].status, 'AMBIGUOUS');
}));
test('corrupt, partial and missing persistent state fail closed instead of creating another identity', async () => {
  for (const bytes of ['{', '{"value":{},"checksum":"bad"}', '{"version":1}']) {
    const did = signing.signerFromSeed(randomBytes(32)).did;
    const ledger = new PilotLedger(did, { create: true }); ledger.close();
    writeFileSync(ledgerPath(did), bytes);
    await denied(launchTestPilot(resolve(OUT, 'corrupt-' + digest(did) + '.jsonl'), { recoveryDid: did }), 'LEDGER_CORRUPT');
  }
  const did = signing.signerFromSeed(randomBytes(32)).did;
  await denied(launchTestPilot(resolve(OUT, 'missing-' + digest(did) + '.jsonl'), { recoveryDid: did }), 'LEDGER_CORRUPT');
});
test('atomic ledger replace, file fsync, directory fsync and restart readback', () => {
  const did = signing.signerFromSeed(randomBytes(32)).did;
  const ledger = new PilotLedger(did, { create: true }); const path = ledger.path; ledger.close();
  const original = readPublicState(path, did), stages = [];
  atomicPublicState(path, { ...original, revision: 10 }, stage => stages.push(stage));
  assert.deepEqual(stages, ['file-fsync', 'replace', 'directory-fsync']);
  assert.equal(readPublicState(path, did).revision, 10);
  assert.throws(() => atomicPublicState(path, { ...original, revision: 11 }, stage => {
    if (stage === 'file-fsync') throw new Error('TEST_CRASH');
  }), { message: 'TEST_CRASH' });
  assert.equal(readPublicState(path, did).revision, 10);
  assert.throws(() => atomicPublicState(path, { ...original, revision: 12 }, stage => {
    if (stage === 'replace') throw new Error('TEST_CRASH');
  }), { message: 'TEST_CRASH' });
  assert.equal(readPublicState(path, did).revision, 12);
  const restarted = new PilotLedger(did); assert.equal(restarted.value.revision, 12); restarted.close();
});
test('live ledger corruption is detected before another event can overwrite it', () => {
  const did = signing.signerFromSeed(randomBytes(32)).did;
  const ledger = new PilotLedger(did, { create: true });
  try {
    const state = { ...ledger.value, revision: ledger.value.revision + 1 };
    atomicPublicState(ledger.path, state);
    assert.throws(() => ledger.append({ event: 'STOPPED' }), { message: 'LEDGER_CHANGED' });
    assert.equal(readPublicState(ledger.path, did).revision, state.revision);
  } finally { ledger.close(); }
});
test('ledger root parent fsync failure prevents lock and identity creation', () => {
  const did = signing.signerFromSeed(randomBytes(32)).did, paths = new Map(), events = [];
  const original = { openSync: fs.openSync, mkdirSync: fs.mkdirSync, fsyncSync: fs.fsyncSync };
  try {
    fs.mkdirSync = (...args) => { events.push('mkdir'); return original.mkdirSync(...args); };
    fs.openSync = (path, ...args) => {
      if (path === ledgerPath(did) + '.lock') events.push('lock');
      const fd = original.openSync(path, ...args); paths.set(fd, path); return fd;
    };
    fs.fsyncSync = fd => {
      if (paths.get(fd) === resolve(ROOT, '.local')) {
        events.push('parent-fsync'); throw new Error('TEST_PARENT_FSYNC_FAILED');
      }
      return original.fsyncSync(fd);
    };
    syncBuiltinESMExports();
    assert.throws(() => new PilotLedger(did, { create: true }), { message: 'TEST_PARENT_FSYNC_FAILED' });
    assert.deepEqual(events, ['mkdir', 'parent-fsync']);
    assert.equal(existsSync(ledgerPath(did)), false); assert.equal(existsSync(ledgerPath(did) + '.lock'), false);
  } finally { Object.assign(fs, original); syncBuiltinESMExports(); }
});

test('restart reconciles only an exact authenticated public envelope; no key recovery or retry', async () => {
  // Synthetic public venue fixture: TEST keys live only in this test process.
  // No live Signer exports its signature to create this observation.
  const payer = signing.signerFromSeed(randomBytes(32)), payee = signing.signerFromSeed(randomBytes(32));
  const at = Date.now();
  const offer = t.makeOffer({ from: payer.did, role: 'payer', amount: '1', asset: 'PAPER', rails: ['paper'],
    lock: 'hash', expiresMs: at + 600000, claimByMs: at + 1800000, refundAfterMs: at + 2400000 });
  const accept = t.makeAccept(offer, { from: payee.did, statement: t.generateHashLock().hash });
  const record = (frame, signer, seq, nonce) => {
    const line = t.encodeFrame(frame), room = t.OFFER_ROOM;
    return { room, seq, timestampMs: at, sender: signer.did, nonce, line,
      signature: signer.sign(signing.canonicalMessage(room, nonce, line)) };
  };
  const offerRecord = record(offer, payer, 100, '1800000000000');
  const acceptRecord = record(accept, payee, 101, '1800000000001');
  const ledger = new PilotLedger(payee.did, { create: true });
  const subject = { session: 'TEST_RECOVERY', actionId: 'TEST_ACCEPT', type: 'ACCEPT', did: payee.did,
    room: t.OFFER_ROOM, offer: offer.id, contract: accept.contract, ref: offer.id,
    sourceDigest: 'a'.repeat(64), nonce: acceptRecord.nonce,
    payloadDigest: digest(signing.canonicalMessage(acceptRecord.room, acceptRecord.nonce, acceptRecord.line)) };
  ledger.append({ event: 'ADMITTED', offer: offer.id, contract: accept.contract, sourceDigest: subject.sourceDigest,
    payerDid: payer.did, offerDigest: envelopeDigest(offerRecord) });
  ledger.append({ event: 'PREPARED', subject, subjectDigest: objectDigest(subject) });
  for (const event of ['SIGNED', 'WRITE_PREPARED', 'SEND_ATTEMPTED', 'AMBIGUOUS']) {
    ledger.append({ event, actionId: subject.actionId, envelopeDigest: envelopeDigest(acceptRecord) });
  }
  ledger.close();
  const directory = mkdtempSync(resolve(OUT, 'recovery-'));
  const p = await launchTestPilot(resolve(directory, 'journal.jsonl'), { recoveryDid: payee.did });
  try {
    const input = { generation: 1, capturedAt: Date.now(), offerRecord, records: [acceptRecord] };
    await denied(p.gate.request('reconcileRestart', { ...input, records: [] }), 'RECOVERY_HANDSHAKE_REQUIRED');
    assert.equal((await p.worker.request('status')).actions[0].status, 'AMBIGUOUS');
    await denied(p.gate.request('reconcileRestart', { ...input, records: [acceptRecord, acceptRecord] }), 'CONFLICTING_RECORD');
    await denied(p.gate.request('reconcileRestart', { ...input, records: [{ ...acceptRecord, signature: 'invalid' }] }), 'INVALID_SIGNATURE');
    const anotherNonce = { ...acceptRecord, nonce: '1800000000002' };
    anotherNonce.signature = payee.sign(signing.canonicalMessage(anotherNonce.room, anotherNonce.nonce, anotherNonce.line));
    assert.equal((await p.gate.request('reconcileRestart', { ...input, records: [anotherNonce] })).status, 'AMBIGUOUS');
    const result = await p.gate.request('reconcileRestart', input);
    assert.equal(result.status, 'RECONCILED'); assert.equal(result.state.status, 'accepted');
    await denied(p.worker.request('sign', { actionId: subject.actionId, subjectDigest: objectDigest(subject) }), 'RECONCILIATION_ONLY');
    const view = JSON.stringify(p.inspection()) + readFileSync(resolve(directory, 'journal.jsonl'), 'utf8');
    assert(!view.includes(acceptRecord.signature) && !view.includes(offerRecord.signature));
  } finally { await p.close(); }
  const after = readPublicState(ledgerPath(payee.did), payee.did);
  assert.equal(after.actions[0].status, 'RECONCILED'); assert(after.quotaConsumed);
  // A later restart cannot downgrade a durably known terminal result by importing
  // only its earlier handshake. Fixture state is public; no secret persistence.
  const terminalLedger = new PilotLedger(payee.did);
  terminalLedger.append({ event: 'OBSERVATION', state: { ...after.state, status: 'claimed' } });
  terminalLedger.close();
  const again = await launchTestPilot(resolve(directory, 'terminal-restart.jsonl'), { recoveryDid: payee.did });
  try {
    await denied(again.gate.request('reconcileRestart', { generation: 1, capturedAt: Date.now(), offerRecord, records: [acceptRecord] }), 'RECOVERY_STATE_REGRESSION');
    assert.equal((await again.worker.request('status')).state.status, 'claimed');
  } finally { await again.close(); }
});

const group = process.argv[2] === 'targeted' ? checks.slice(originalChecks) : checks;
const selectedChecks = process.argv[3] ? group.filter(c => c.name.includes(process.argv[3])) : group;
for (const { name, fn } of selectedChecks) {
  try { await fn(); results.push({ name, passed: true }); }
  catch (error) {
    // Do not serialize assert.actual, exception messages or cryptographic objects.
    failures.push(name); results.push({ name, passed: false, error_class: error.constructor.name,
      code: /^[A-Z_]+$/.test(error.message) ? error.message : 'TEST_ASSERTION' });
    break;
  }
}
console.log(JSON.stringify({ mode: 'TEST_EPHEMERAL_MOCK_ONLY', results, passed: results.filter(r => r.passed).length,
  failed: failures.length, planned: selectedChecks.length, real_credentials: false, public_writes: 0,
  isolation: 'separate signer process, separate gate pipe, Node fs permissions, mock-only effect' }));
process.exitCode = failures.length ? 1 : 0;
