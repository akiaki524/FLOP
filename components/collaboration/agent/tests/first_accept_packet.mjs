import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { candidates, freeze, matchFirstAccept,
  validateFirstAccept, revalidateFirstAccept } from '../src/collaboration_agent/first_accept_packet.mjs';
import { digest, envelopeDigest, objectDigest, official, POLICY } from '../src/collaboration_agent/pilot_protocol.mjs';

const { t, signing } = await official();
const payer = signing.signerFromSeed(Buffer.alloc(32, 81));
const payee = signing.signerFromSeed(Buffer.alloc(32, 82));
const other = signing.signerFromSeed(Buffer.alloc(32, 83));
const NOW = 1_800_000_000_000;
let passed = 0;
const test = async (name, fn) => { await fn(); passed++; };

function record(signer, frame, { seq, timestampMs = NOW - 86_400_000, nonce = String(NOW - 1000),
  room = t.OFFER_ROOM } = {}) {
  const line = typeof frame === 'string' ? frame : t.encodeFrame(frame);
  return { room, seq, timestampMs, sender: signer.did, nonce,
    signature: signer.sign(signing.canonicalMessage(room, nonce, line)), line };
}

function offer(signer = payer, changes = {}) {
  return t.makeOffer({ from: signer.did, role: 'payer', amount: '1', asset: 'PAPER', rails: ['paper'],
    lock: 'hash', expiresMs: NOW + 600_000, claimByMs: NOW + 1_800_000,
    refundAfterMs: NOW + 2_400_000, job: { proto: 'a2a', id: 'FIRST-ACCEPT-TEST' }, ...changes });
}

function observation({ at = NOW, observedRecord = null, rawSha256 = 'a'.repeat(64),
  generation = 1, basis = 'technocore-e4c4f73f3b28612d7161170b11e08e580b02123a' } = {}) {
  return { version: 2, kind: 'PROJECT_DID_PUBLIC_NONCE_OBSERVATION', did: payee.did,
    room: t.OFFER_ROOM, observedNonce: observedRecord?.nonce ?? null, observedNone: !observedRecord,
    verifiedAtMs: at, records: observedRecord ? [observedRecord] : [],
    source: { url: 'https://technocore.chat/r/tclk-offers/export', method: 'GET', httpStatus: 200,
      generation, rawBytes: 4096, rawSha256, capturedAt: at },
    coverage: { complete: true, basis, tailStart: 0, tailBytes: 4096, lineCount: 10 } };
}

function snapshot(records, obs = observation()) { return { version: 1, observation: obs, records }; }
const options = { t, signing, nowMs: NOW, expectedDid: payee.did };
const validOffer = offer(), validRecord = record(payer, validOffer, { seq: 1 });

await test('C1 filters hostile and unsupported records, preserves order and accepts historical offers', async () => {
  const secondOffer = offer(other, { job: { proto: 'a2a', id: 'SECOND' }, nonce: '1122334455667788' });
  const malformed = { ...record(other, secondOffer, { seq: 2 }), signature: 'invalid' };
  const expired = offer(other, { expiresMs: NOW + POLICY.acceptMarginMs });
  const self = offer(payee, { nonce: '2233445566778899' });
  const nonPaper = offer(other, { role: 'payee', nonce: '3344556677889900' });
  const rows = [record(other, secondOffer, { seq: 3 }), malformed,
    record(other, expired, { seq: 4 }), record(payee, self, { seq: 5 }),
    record(other, nonPaper, { seq: 6 }), validRecord];
  const found = await candidates(snapshot(rows), options);
  assert.deepEqual(found.map(x => x.offerId), [secondOffer.id, validOffer.id]);
  assert.equal(found[1].offerRecord.timestampMs, NOW - 86_400_000);
});

await test('Byte-bounded exports reach C1 candidates beyond 4096 records and keep IPC selection bounded', async () => {
  const ordinary = record(other, 'ordinary public line', { seq: 2 });
  for (const count of [4096, 4097, 10000]) {
    // Exercise the real Python acquisition/normalization and Node candidate path
    // together, using only an injected offline HTTP response and dummy signers.
    const rows = Array.from({ length: count - 1 }, (_, i) => ({ ...ordinary, seq: i + 2 }));
    rows.push({ ...validRecord, seq: count + 1 });
    const raw = rows.map(r => JSON.stringify({ seq: r.seq,
      ts: new Date(r.timestampMs).toISOString(), from: r.sender, nonce: Number(r.nonce),
      sig: r.signature, text: r.line })).join('\n') + '\n';
    assert(Buffer.byteLength(raw) < 10 * 1024 * 1024);
    const directory = mkdtempSync(join(tmpdir(), 'first-accept-export-'));
    let result;
    try {
      const path = join(directory, 'export.ndjson');
      writeFileSync(path, raw);
      result = spawnSync('python3', ['-B', '-c', `
import json, sys
sys.path.insert(0, 'src')
from collaboration_agent.first_accept_read import acquire_snapshot
with open(sys.argv[1], 'rb') as source:
    raw = source.read()
def send(url, cap, *, intake_export):
    return 200, {'content-type': 'application/x-ndjson',
                 'content-length': str(len(raw)), 'x-room-generation': '1'}, raw, None
print(json.dumps(acquire_snapshot(send=send, clock_ms=lambda: ${NOW},
                                 expected_did='${payee.did}')))
`, path], { encoding: 'utf8', maxBuffer: 20 * 1024 * 1024, timeout: 30000 });
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
    assert.equal(result.status, 0, result.error?.message ?? result.stderr);
    const input = JSON.parse(result.stdout);
    assert.equal(input.records.length, count);
    const found = await candidates(input, options);
    assert.deepEqual(found.map(x => x.offerId), [validOffer.id]);
    const { document } = await freeze(input, validOffer.id, options);
    const final = await revalidateFirstAccept(document, input, options);
    assert.deepEqual(final.admissionSnapshot.records, [rows.at(-1)]);
    assert(Buffer.byteLength(JSON.stringify(final.admissionSnapshot)) < 60000);
  }
});

await test('C1 freezes only the selected offer and binds exact source, nonce, contract and approval', async () => {
  const prior = record(payee, 'prior public line', { seq: 90, timestampMs: NOW,
    nonce: String(NOW + 99) });
  const input = snapshot([validRecord], observation({ observedRecord: prior }));
  const { document, preimage } = await freeze(input, validOffer.id, options);
  assert.equal(document.packet.subject.nonce, String(NOW + 100));
  assert.equal(document.packet.subject.sourceDigest, input.observation.source.rawSha256);
  assert.equal(document.packet.subject.contract, t.decodeFrame(document.packet.publicLine).contract);
  assert.equal(document.digest, objectDigest(Object.fromEntries(Object.entries(document).slice(0, -1))));
  assert.match(preimage, /^0x[0-9a-f]{64}$/);
  assert(!JSON.stringify(document).includes(preimage));
  const checked = await validateFirstAccept(document, options);
  assert.equal(checked.offer.id, validOffer.id);
  assert.equal(checked.accept.from, payee.did);
});

await test('C1 never auto-selects and duplicate offer ids are ambiguous', async () => {
  await assert.rejects(freeze(snapshot([validRecord]), '0x' + '00'.repeat(32), options),
    { message: 'OFFER_NOT_CANDIDATE' });
  const duplicate = { ...validRecord, seq: 2 };
  await assert.rejects(freeze(snapshot([validRecord, duplicate]), validOffer.id, options),
    { message: 'AMBIGUOUS_OFFER_SELECTION' });
});

const frozen = (await freeze(snapshot([validRecord]), validOffer.id, options)).document;

function redigest(document) {
  const { digest: ignored, ...base } = document;
  document.digest = objectDigest(base);
  return document;
}

function reapprove(document) {
  const packet = document.packet;
  packet.subjectDigest = objectDigest(packet.subject);
  const { approvalDigest: ignored, ...view } = packet;
  packet.approvalDigest = objectDigest(view);
  return redigest(document);
}

await test('Strict validation rejects tamper, stale observation, wrong nonce and contract', async () => {
  const tampered = structuredClone(frozen); tampered.offerRecord.seq++;
  await assert.rejects(validateFirstAccept(tampered, options), { message: 'FIRST_ACCEPT_PACKET_INVALID' });
  await assert.rejects(validateFirstAccept(frozen, { ...options, nowMs: NOW + POLICY.freshMs + 1 }),
    { message: 'STALE_OBSERVATION' });
  const nonce = structuredClone(frozen); nonce.packet.subject.nonce = String(NOW + 1);
  nonce.packet.subject.payloadDigest = digest(signing.canonicalMessage(nonce.packet.subject.room,
    nonce.packet.subject.nonce, nonce.packet.publicLine));
  await assert.rejects(validateFirstAccept(reapprove(nonce), options),
    { message: 'FIRST_ACCEPT_BINDING_MISMATCH' });
  const contract = structuredClone(frozen); contract.packet.subject.contract = '0x' + '11'.repeat(32);
  await assert.rejects(validateFirstAccept(reapprove(contract), options),
    { message: 'FIRST_ACCEPT_BINDING_MISMATCH' });
});

await test('read-only reconciliation validates at createdAt and finds one exact published envelope', async () => {
  const subject = frozen.packet.subject;
  const published = record(payee, frozen.packet.publicLine,
    { seq: 100, timestampMs: NOW + 1000, nonce: subject.nonce });
  const readAt = NOW + POLICY.freshMs + 5000;
  const publicRead = snapshot([published], observation({ at: readAt, observedRecord: published,
    rawSha256: 'b'.repeat(64) }));
  const result = await matchFirstAccept(frozen, publicRead, envelopeDigest(published),
    { ...options, nowMs: readAt });
  assert.equal(result.status, 'RECONCILED_PRESENT');
  assert.deepEqual(result.record, published);
});

await test('wrong or absent reconciliation record remains ambiguous with retry disabled', async () => {
  const subject = frozen.packet.subject, readAt = NOW + 1000;
  const wrong = record(payee, frozen.packet.publicLine + ' ',
    { seq: 101, timestampMs: readAt, nonce: subject.nonce });
  const publicRead = snapshot([wrong], observation({ at: readAt, observedRecord: wrong }));
  const result = await matchFirstAccept(frozen, publicRead, envelopeDigest(wrong),
    { ...options, nowMs: readAt });
  assert.deepEqual(result, { status: 'AMBIGUOUS', retry: false });
});

await test('duplicate public sequences fail closed and framing metadata must remain pinned', async () => {
  const subject = frozen.packet.subject;
  const published = record(payee, frozen.packet.publicLine,
    { seq: 102, timestampMs: NOW + 1000, nonce: subject.nonce });
  const otherRecord = record(other, 'ordinary line', { seq: 102, timestampMs: NOW + 1000,
    nonce: String(NOW + 500) });
  const publicRead = snapshot([published, otherRecord], observation({ at: NOW + 1000,
    observedRecord: published }));
  await assert.rejects(matchFirstAccept(frozen, publicRead, envelopeDigest(published),
    { ...options, nowMs: NOW + 1000 }), { message: 'DUPLICATE_PUBLIC_SEQUENCE' });
  const wrongProfile = structuredClone(publicRead);
  wrongProfile.observation.coverage.basis = 'wrong';
  await assert.rejects(matchFirstAccept(frozen, wrongProfile, envelopeDigest(published),
    { ...options, nowMs: NOW + 1000 }));
});

await test('Human delay does not change frozen intent or approved venue nonce', async () => {
  const later = NOW + 61_000;
  const before = JSON.stringify(frozen);
  const final = snapshot([validRecord], observation({ at: later, rawSha256: 'c'.repeat(64) }));
  const result = await revalidateFirstAccept(frozen, final, { ...options, nowMs: later });
  assert.equal(JSON.stringify(frozen), before);
  assert.deepEqual(result.packet, frozen.packet);
  assert.equal(result.packet.subject.nonce, String(NOW));
  assert.equal(result.packet.subject.sourceDigest, frozen.observation.source.rawSha256);
  assert.equal(result.observation.verifiedAtMs, later);
  assert.notEqual(result.observation.source.rawSha256, result.packet.subject.sourceDigest);
  assert.deepEqual(result.admissionSnapshot.records, [validRecord]);
});

await test('Selected protocol transcript retains its record-count IPC limit', async () => {
  const records = Array.from({ length: POLICY.maxRecords + 1 }, (_, i) =>
    ({ ...validRecord, seq: i + 1 }));
  await assert.rejects(revalidateFirstAccept(frozen, snapshot(records), options),
    { message: 'FINAL_TRANSCRIPT_TOO_LARGE' });
});

await test('Only the final observation has a 30-second pre-commit freshness bound', async () => {
  const final = snapshot([validRecord], observation({ at: NOW + 61_000 }));
  await assert.rejects(revalidateFirstAccept(frozen, final,
    { ...options, nowMs: NOW + 92_000 }), { message: 'STALE_OBSERVATION' });
  await assert.rejects(revalidateFirstAccept(frozen,
    snapshot([validRecord], observation({ at: NOW - 1 })), options),
    { message: 'FINAL_OBSERVATION_ROLLBACK' });
});

await test('Changed nonce, missing offer, expiry and profile/generation fail final validation', async () => {
  const later = NOW + 61_000;
  const prior = record(payee, 'a new venue record', { seq: 100, timestampMs: later,
    nonce: String(NOW + 1) });
  const cases = [
    snapshot([validRecord, prior], observation({ at: later, observedRecord: prior })),
    snapshot([], observation({ at: later })),
    snapshot([validRecord], observation({ at: later, generation: 2 })),
    snapshot([validRecord], observation({ at: later, basis: 'unknown-profile' })),
  ];
  for (const final of cases)
    await assert.rejects(revalidateFirstAccept(frozen, final, { ...options, nowMs: later }));
  const expiredAt = validOffer.expiresMs;
  await assert.rejects(revalidateFirstAccept(frozen,
    snapshot([validRecord], observation({ at: expiredAt })),
    { ...options, nowMs: expiredAt }), { message: 'ACCEPT_DEADLINE' });
});

await test('Incompatible protocol record and duplicate offer cannot pass final validation', async () => {
  const later = NOW + 61_000;
  const cancel = record(payer, { type: 'cancel', from: payer.did,
    contract: frozen.packet.subject.contract },
    { seq: 2, timestampMs: later });
  await assert.rejects(revalidateFirstAccept(frozen,
    snapshot([validRecord, cancel], observation({ at: later })),
    { ...options, nowMs: later }), { message: 'FROZEN_ACCEPT_UNAVAILABLE' });
  await assert.rejects(revalidateFirstAccept(frozen,
    snapshot([validRecord, validRecord], observation({ at: later })),
    { ...options, nowMs: later }), { message: 'FROZEN_OFFER_UNAVAILABLE' });
});

console.log(JSON.stringify({ passed, failed: 0, externalWrites: 0, realSecrets: false }));
