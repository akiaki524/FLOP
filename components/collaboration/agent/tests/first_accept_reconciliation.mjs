// Offline only: dummy signers, real pinned signature verifier, no transport.
import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { freeze, matchFirstAccept } from '../src/collaboration_agent/first_accept_packet.mjs';
import { digest, envelopeDigest, official } from '../src/collaboration_agent/pilot_protocol.mjs';
import { NONCE_PROFILE, NONCE_SOURCE } from '../src/collaboration_agent/real_nonce.mjs';

const { t, signing } = await official();
const payer = signing.signerFromSeed(Buffer.alloc(32, 91));
const payee = signing.signerFromSeed(Buffer.alloc(32, 92));
const other = signing.signerFromSeed(Buffer.alloc(32, 93));
const NOW = 1_800_000_000_000;
const options = { t, signing, expectedDid: payee.did, nowMs: NOW };
let passed = 0;
const test = async fn => { await fn(); passed++; };
function signed(signer, line, seq, nonce = String(NOW), timestampMs = NOW) {
  const room = t.OFFER_ROOM;
  return { room, seq, timestampMs, sender: signer.did, nonce, line,
    signature: signer.sign(signing.canonicalMessage(room, nonce, line)) };
}
function snapshot(records, observedRecord = null) {
  const raw = records.map(r => JSON.stringify({ seq: r.seq,
    ts: new Date(r.timestampMs).toISOString(), from: r.sender, nonce: Number(r.nonce),
    text: r.line, sig: r.signature })).join('\n') + '\n';
  const rawBytes = Buffer.byteLength(raw), tailBytes = Math.min(rawBytes, 1 << 20);
  assert(rawBytes < 10 * 1024 * 1024);
  const tail = Buffer.from(raw).subarray(rawBytes - tailBytes).toString().split('\n');
  if (rawBytes > tailBytes) tail.shift();
  const observation = { version: 2, kind: 'PROJECT_DID_PUBLIC_NONCE_OBSERVATION',
    did: payee.did, room: t.OFFER_ROOM, observedNonce: observedRecord?.nonce ?? null,
    observedNone: !observedRecord, verifiedAtMs: NOW, records: observedRecord ? [observedRecord] : [],
    source: { url: NONCE_SOURCE, method: 'GET', httpStatus: 200, generation: 1,
      rawBytes, rawSha256: digest(raw), capturedAt: NOW },
    coverage: { complete: true, basis: NONCE_PROFILE, tailStart: rawBytes - tailBytes,
      tailBytes, lineCount: tail.filter(Boolean).length } };
  return { version: 1, observation, records };
}
const offer = t.makeOffer({ from: payer.did, role: 'payer', amount: '1', asset: 'PAPER',
  rails: ['paper'], lock: 'hash', expiresMs: NOW + 600000, claimByMs: NOW + 1800000,
  refundAfterMs: NOW + 2400000, job: { proto: 'a2a', id: 'RECONCILIATION-PERF' } });
const offerRecord = signed(payer, t.encodeFrame(offer), 1, String(NOW - 1000), NOW - 1000);
const { document } = await freeze(snapshot([offerRecord]), offer.id, options);
const target = signed(payee, document.packet.publicLine, 20001, document.packet.subject.nonce);
const expected = envelopeDigest(target);
const ambiguous = { status: 'AMBIGUOUS', retry: false };

// Count real verifier calls through matchFirstAccept's injected runtime. The
// nonce-observation validator separately uses official() and is not intercepted.
async function measured(input, expectedDigest = expected, extra = {}) {
  const verified = [];
  const counted = { ...t, verifyTranscriptRecord(record) {
    verified.push(record); return t.verifyTranscriptRecord(record);
  } };
  const start = performance.now();
  const result = await matchFirstAccept(document, input, expectedDigest,
    { ...options, ...extra, t: counted });
  return { result, verified, elapsedMs: performance.now() - start };
}
function counts(value, candidates) {
  assert.equal(value.verified.length, 1 + candidates.length); // frozen OFFER + candidates
  assert.equal(value.verified[0], document.offerRecord);
  assert.deepEqual(value.verified.slice(1), candidates);
}

let timing;
await test(async () => {
  // Every envelope has a real signature; distinct nonces avoid synthetic copies.
  const rows = Array.from({ length: 19999 }, (_, i) =>
    signed(other, 'ordinary public line', i + 2, String(NOW - 30000 + i)));
  const input = snapshot([...rows, target], target);
  const present = await measured(input);
  assert.equal(present.result.status, 'RECONCILED_PRESENT');
  assert.deepEqual(present.result.record, target);
  counts(present, [target]);
  assert(present.elapsedMs < 30000, `reconciliation took ${present.elapsedMs} ms`);
  const absent = await measured(snapshot([...rows,
    signed(other, 'ordinary public line', target.seq, String(NOW - 1))]));
  assert.deepEqual(absent.result, ambiguous);
  counts(absent, []);
  timing = { records: input.records.length, rawBytes: input.observation.source.rawBytes,
    presentMs: present.elapsedMs, absentMs: absent.elapsedMs,
    candidateVerifyCalls: 1, frozenOfferVerifyCalls: 1, nonceObservationVerifyCalls: 1 };
});

await test(async () => {
  // A signature with valid encoding but over the wrong message must fail crypto.
  const bad = { ...target, signature: signed(payee, 'different signed text', 2).signature };
  const value = await measured(snapshot([bad]), envelopeDigest(bad));
  assert.deepEqual(value.result, ambiguous);
  counts(value, [bad]);
});

await test(async () => {
  const wrongLine = signed(payee, 'different text', 2, target.nonce);
  const wrongSender = signed(other, target.line, 3, target.nonce);
  const wrongNonce = signed(payee, target.line, 4, String(NOW + 1));
  const before = { ...target, seq: 0 }, equal = { ...target, seq: offerRecord.seq };
  const wrongRoom = { ...target, seq: 5, room: 'other-room' };
  for (const r of [wrongLine, wrongSender, wrongNonce, before, equal, wrongRoom]) {
    const value = await measured(snapshot([r]), envelopeDigest(r));
    assert.deepEqual(value.result, ambiguous);
    counts(value, []);
  }
});

await test(async () => {
  for (const signature of [target.signature, 'invalid']) {
    const rows = [target, { ...target, signature }];
    const calls = [];
    await assert.rejects(matchFirstAccept(document, snapshot(rows), expected,
      { ...options, t: { ...t, verifyTranscriptRecord(r) {
        calls.push(r); return t.verifyTranscriptRecord(r);
      } } }), { message: 'DUPLICATE_PUBLIC_SEQUENCE' });
    assert.deepEqual(calls, [document.offerRecord]); // no snapshot crypto
  }
});

await test(async () => {
  const second = { ...target, seq: target.seq + 1 };
  const value = await measured(snapshot([target, second]));
  assert.deepEqual(value.result, ambiguous);
  counts(value, [target, second]); // do not stop at first match
});

await test(async () => {
  const invalid = { ...target, seq: target.seq + 1,
    signature: signed(payee, 'wrong signed text', 2).signature };
  for (const rows of [[invalid, target], [target, invalid]]) {
    const value = await measured(snapshot(rows));
    assert.equal(value.result.status, 'RECONCILED_PRESENT');
    assert.deepEqual(value.result.record, target);
    counts(value, rows); // only authenticated exact matches count, independent of order
  }
});

await test(async () => {
  for (const timestampMs of [offerRecord.timestampMs - 1, NOW + 1001]) {
    const r = { ...target, timestampMs };
    const value = await measured(snapshot([r]));
    assert.deepEqual(value.result, ambiguous);
    counts(value, [r]);
  }
  const wrongDigest = await measured(snapshot([target]), '0'.repeat(64));
  assert.deepEqual(wrongDigest.result, ambiguous);
  counts(wrongDigest, [target]);
  const input = snapshot([target]);
  assert.equal((await measured(input, expected, { nowMs: NOW + 30000 })).result.status,
    'RECONCILED_PRESENT');
  await assert.rejects(measured(input, expected, { nowMs: NOW + 30001 }),
    { message: 'STALE_OBSERVATION' });
});

console.log(JSON.stringify({ passed, failed: 0, timing, realSecrets: false, externalWrites: 0 }));
