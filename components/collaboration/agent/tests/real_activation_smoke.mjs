// Offline, deterministic fixtures only. Invoked against rootless copied services with AF_INET denied.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { official, objectDigest } from '../src/collaboration_agent/pilot_protocol.mjs';
import { callSocket } from '../src/collaboration_agent/connection_runtime.mjs';
const request = (role, method, value = {}) => callSocket('/run/collab-connection/' + role + '.sock', { method, value });
let passed = 0;
let summary = {};
const check = value => { assert(value); passed++; };
const denied = async promise => { await assert.rejects(promise); passed++; };
try {
  const { t, signing } = await official();
  const boundary = await request('human', 'boundaryStatus');
  check(boundary.mode === 'REAL_ACCEPT_PREPARATION' && boundary.inetDenied && boundary.childDenied);
  if (['recovery', 'recovery-admitted', 'recovery-prepared', 'recovery-send-attempted'].includes(process.argv[2])) {
    const status = await request('worker-control', 'status');
    check(status.reconciliationOnly && status.stopped && status.quotaConsumed);
    if (process.argv[2] === 'recovery')
      check(status.actions.length === 1 && status.actions[0].status === 'AMBIGUOUS');
    else if (process.argv[2] === 'recovery-admitted')
      check(status.actions.length === 0);
    else if (process.argv[2] === 'recovery-prepared')
      check(status.actions.length === 1 && status.actions[0].status === 'PREPARED');
    else if (process.argv[2] === 'recovery-send-attempted')
      check(status.actions.length === 1 && status.actions[0].status === 'SEND_ATTEMPTED'
        && status.actions[0].attemptCount === 1);
    await denied(request('worker-control', 'prepare', { type: 'ACCEPT' }));
    await denied(request('human', 'admit', {}));
    if (process.argv[2] === 'recovery')
      await denied(request('human', 'sendAccept', { actionId: status.actions[0].id, subjectDigest: '0'.repeat(64) }));
  } else if (process.argv[2] === 'recovery-empty') {
    const status = await request('worker-control', 'status');
    check(status.reconciliationOnly && status.stopped && !status.quotaConsumed && status.actions.length === 0);
    await denied(request('worker-control', 'prepare', { type: 'ACCEPT' }));
    await denied(request('human', 'admit', {}));
  } else if (process.argv[2] === 'identity-only') {
    const status = await request('worker-control', 'status');
    check(!status.reconciliationOnly && !status.stopped && status.contract === null && status.actions.length === 0);
    check(boundary.externalWriteEnabled === false);
    summary = { did: boundary.did, stage: 'IDENTITY' };
  } else {
    const candidate = JSON.parse(readFileSync('.local/batch17a/candidate.json', 'utf8'));
    const payee = signing.signerFromSeed(Buffer.alloc(32, 65));
    check(boundary.did === payee.did);
    const payer = signing.signerFromSeed(Buffer.alloc(32, 66)), now = Date.now();
    const offer = t.makeOffer({ from: payer.did, role: 'payer', amount: '1', asset: 'PAPER', rails: ['paper'],
      lock: 'hash', expiresMs: now + 600000, claimByMs: now + 1800000, refundAfterMs: now + 2400000,
      job: { proto: 'a2a', id: 'TEST-first-accept', context: 'TEST GCD bundle sha256=' + candidate.source_digest } });
    const record = (signer, line, nonce, seq) => ({ room: t.OFFER_ROOM, seq, timestampMs: now,
      sender: signer.did, nonce, signature: signer.sign(signing.canonicalMessage(t.OFFER_ROOM, nonce, line)), line });
    const offerRecord = record(payer, t.encodeFrame(offer), String(now), 7000002);
    const meta = await request('human', 'admit', { mode: 'REAL_ACCEPT_PREPARATION', expectedDid: boundary.did,
      offerRecord, sourceDigest: candidate.source_digest, family: 'math.gcd_lcm', answer: candidate.answer,
      noValue: 'EXPLICIT_PAPER_NO_VALUE', heartbeatRequired: false });
    if (['admit-only', 'prepare-only'].includes(process.argv[2])) check(boundary.externalWriteEnabled === false);
    if (process.argv[2] === 'admit-only') {
      const status = await request('worker-control', 'status');
      check(!status.reconciliationOnly && status.contract === meta.contract && status.actions.length === 0);
      summary = { did: boundary.did, contract: meta.contract, stage: 'ADMITTED' };
    } else {
    const value = { type: 'ACCEPT', did: boundary.did, room: t.OFFER_ROOM, offer: offer.id,
      contract: meta.contract, ref: offer.id, sourceDigest: candidate.source_digest };
    await denied(request('worker-control', 'prepare', value));
    await denied(request('human', 'nonceObservation', { room: t.OFFER_ROOM, observedNonce: null,
      observedNone: true, verifiedAtMs: now }));
    const prior = record(payee, 'offline fixture prior public message', '9007199254740993123', 7000001);
    const observation = { version: 2, kind: 'PROJECT_DID_PUBLIC_NONCE_OBSERVATION', did: boundary.did,
      room: t.OFFER_ROOM, observedNonce: prior.nonce, observedNone: false, verifiedAtMs: now,
      records: [prior], source: { url: 'https://technocore.chat/r/tclk-offers/export',
        method: 'GET', httpStatus: 200, generation: 1, rawBytes: 1024, rawSha256: 'a'.repeat(64), capturedAt: now },
      coverage: { complete: true, basis: 'technocore-e4c4f73f3b28612d7161170b11e08e580b02123a',
        tailStart: 0, tailBytes: 1024, lineCount: 2 } };
    await denied(request('human', 'nonceObservation', { ...observation, observedNonce: '0' }));
    await request('human', 'nonceObservation', observation);
    await denied(request('worker-control', 'prepare', { ...value, type: 'REVEAL' }));
    await denied(request('worker-control', 'prepare', { ...value, type: 'DELIVERY_GCD', answer: candidate.answer }));
    const action = await request('worker-control', 'prepare', value);
    check(action.subject.nonce === '9007199254740993124');
    await denied(request('worker-control', 'prepare', value));
    if (process.argv[2] === 'prepare-only') {
      const status = await request('worker-control', 'status');
      check(!status.reconciliationOnly && status.actions.length === 1 && status.actions[0].status === 'PREPARED');
      summary = { did: boundary.did, contract: meta.contract, actionId: action.subject.actionId, stage: 'PREPARED' };
    } else {
    const token = { actionId: action.subject.actionId, subjectDigest: objectDigest(action.subject) };
    await denied(request('worker-control', 'sign', token));
    const packet = await request('human', 'approvalPreview', { actionId: token.actionId });
    const approval = { actionId: token.actionId, approvalDigest: packet.approvalDigest,
      confirmation: 'ACCEPT ' + packet.approvalDigest.slice(0, 16), expiresAt: Date.now() + 60000 };
    await denied(request('human', 'approve', { ...approval, expiresAt: now - 1 }));
    await denied(request('human', 'approve', { ...approval, approvalDigest: '0'.repeat(64) }));
    await request('human', 'approve', approval);
    await denied(request('human', 'approve', approval));
    const signed = await request('worker-control', 'sign', token);
    check(signed.verified && !JSON.stringify(signed).match(/seed|signature|privateKey|preimage/));
    await denied(request('worker-control', 'sign', token));
    check((await request('worker-control', 'prepareWrite', token)).effect === 'REAL_ACCEPT_POLICY_VALIDATION_ONLY');
    await denied(request('worker-control', 'sendAccept', token));
    if (process.argv[2] === 'disabled') {
      await denied(request('human', 'sendAccept', token));
      check((await request('worker-control', 'status')).actions[0].attemptCount === 0);
    } else {
      const result = await request('human', 'sendAccept', token);
      check(result.status === 'AMBIGUOUS' && result.retry === false);
      await denied(request('human', 'sendAccept', token));
      const status = await request('worker-control', 'status');
      check(status.stopped && status.actions[0].attemptCount === 1 && status.actions[0].status === 'AMBIGUOUS');
      if (process.argv[2] === 'reconcile') {
        const published = record(payee, packet.publicLine, action.subject.nonce, 7000003);
        published.timestampMs = Date.now();
        await request('human', 'observe', { generation: 1, capturedAt: Date.now(), records: [published], paperNote: null });
        const reconciled = await request('worker-control', 'status');
        check(reconciled.stopped && reconciled.actions[0].status === 'RECONCILED');
        await denied(request('human', 'sendAccept', token));
      }
    }
    }
    }
  }
  console.log(JSON.stringify({ passed, failed: 0, externalWrites: 0, realSecrets: false, ...summary }));
} catch { console.log(JSON.stringify({ passed, failed: 1, code: 'REAL_ACTIVATION_SMOKE_FAILED', externalWrites: 0 })); process.exitCode = 1; }
