// Pure Stage C packet policy. This module performs no network, secret or durable-state I/O.
import { randomUUID } from 'node:crypto';

import { PROJECT_DID } from './connection_approval.mjs';
import { canonicalApproval, verifyApprovalView } from './pilot_approval.mjs';
import { contractRecords, digest, envelopeDigest as recordDigest, fields, objectDigest, POLICY,
  requireThat as need, verifyRecord } from './pilot_protocol.mjs';
import { validateNoncePacket } from './real_nonce.mjs';

const KIND = 'FIRST_PAPER_ACCEPT';
const ROOM = 'tclk-offers';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const NONCE = /^(0|[1-9][0-9]{0,18})$/;

function runtime(options) {
  need(options?.t && options?.signing, 'OFFICIAL_RUNTIME_REQUIRED');
  need(Number.isSafeInteger(options.nowMs) && options.nowMs >= 0, 'INVALID_TIME');
  return options;
}

async function snapshotObservation(snapshot, options, at = options.nowMs) {
  fields(snapshot, ['version', 'observation', 'records']);
  // Acquisition bounds the complete export by raw bytes, not record count.
  // The selected transcript retains its separate record and IPC byte limits.
  need(snapshot.version === 1 && Array.isArray(snapshot.records), 'INVALID_PUBLIC_SNAPSHOT');
  await validateNoncePacket(snapshot.observation, { expectedDid: options.expectedDid, nowMs: at });
  return snapshot.observation;
}

function paperOffer(t, record, expectedDid, nowMs, observation) {
  verifyRecord(t, record);
  need(record.room === ROOM && record.room === t.OFFER_ROOM
    && record.timestampMs <= observation.verifiedAtMs + 1000, 'INVALID_OFFER');
  const offer = t.decodeFrame(record.line);
  need(offer.type === 'offer' && t.encodeFrame(offer) === record.line
    && record.sender === offer.from && offer.from !== expectedDid && offer.role === 'payer', 'INVALID_OFFER');
  need(offer.asset === 'PAPER' && offer.lock === 'hash'
    && Array.isArray(offer.rails) && offer.rails.length === 1 && offer.rails[0] === 'paper'
    && offer.paymentKey === undefined, 'PAPER_ONLY');
  need(typeof offer.amount === 'string' && /^[1-9][0-9]*$/.test(offer.amount), 'PAPER_ONLY');
  need(nowMs + POLICY.acceptMarginMs < offer.expiresMs, 'ACCEPT_DEADLINE');
  need(nowMs + POLICY.claimMarginMs < offer.claimByMs, 'CLAIM_DEADLINE');
  need(offer.refundAfterMs - offer.claimByMs >= POLICY.refundGapMs, 'REFUND_MARGIN');
  // The official state machine is the final authority for the decoded offer.
  need(t.openContract(offer).status === 'proposed', 'INVALID_OFFER');
  return offer;
}

function nextNonce(observation, createdAt) {
  let value = BigInt(createdAt);
  if (!observation.observedNone) {
    const afterObserved = BigInt(observation.observedNonce) + 1n;
    if (afterObserved > value) value = afterObserved;
  }
  const nonce = value.toString();
  need(NONCE.test(nonce), 'NONCE_EXHAUSTED');
  return nonce;
}

export async function candidates(snapshot, { t, signing, nowMs = Date.now(),
  expectedDid = PROJECT_DID } = {}) {
  const options = runtime({ t, signing, nowMs, expectedDid });
  const observation = await snapshotObservation(snapshot, options);
  const found = [];
  for (const offerRecord of snapshot.records) {
    try {
      const offer = paperOffer(t, offerRecord, expectedDid, nowMs, observation);
      found.push({ offerId: offer.id, from: offer.from, offer: structuredClone(offer),
        offerRecord: structuredClone(offerRecord) });
    } catch { /* hostile, unsupported and expired rows are not candidates */ }
  }
  return found;
}

export async function freeze(snapshot, selectedOfferId, { t, signing, nowMs = Date.now(),
  expectedDid = PROJECT_DID } = {}) {
  const options = runtime({ t, signing, nowMs, expectedDid });
  need(typeof selectedOfferId === 'string' && selectedOfferId.length > 0, 'OFFER_SELECTION_REQUIRED');
  const found = await candidates(snapshot, options);
  const selected = found.filter(value => value.offerId === selectedOfferId);
  need(selected.length === 1, selected.length ? 'AMBIGUOUS_OFFER_SELECTION' : 'OFFER_NOT_CANDIDATE');
  const observation = snapshot.observation, createdAt = nowMs;
  const lock = t.generateHashLock();
  const accept = t.makeAccept(selected[0].offer,
    { from: expectedDid, statement: lock.hash });
  need(t.applyFrame(t.openContract(selected[0].offer), accept, createdAt).ok, 'INVALID_ACCEPT');
  const line = t.encodeFrame(accept), nonce = nextNonce(observation, createdAt);
  const subject = { session: randomUUID(), actionId: randomUUID(), type: 'ACCEPT', did: expectedDid,
    room: ROOM, offer: selected[0].offer.id, contract: accept.contract, ref: selected[0].offer.id,
    sourceDigest: observation.source.rawSha256, nonce,
    payloadDigest: digest(signing.canonicalMessage(ROOM, nonce, line)) };
  const packet = canonicalApproval({ type: 'ACCEPT', subject, line },
    { offer: selected[0].offer, statement: accept.statement }, signing);
  const base = { version: 1, kind: KIND, createdAt, observation: structuredClone(observation),
    offerRecord: structuredClone(selected[0].offerRecord), packet };
  return { document: { ...base, digest: objectDigest(base) }, preimage: lock.preimage };
}

export async function validateFirstAccept(document, { t, signing, nowMs = Date.now(),
  expectedDid = PROJECT_DID, fresh = true } = {}) {
  const options = runtime({ t, signing, nowMs, expectedDid });
  need(typeof fresh === 'boolean', 'INVALID_FRESHNESS_MODE');
  fields(document, ['version', 'kind', 'createdAt', 'observation', 'offerRecord', 'packet', 'digest']);
  const { digest: documentDigest, ...base } = document;
  need(document.version === 1 && document.kind === KIND
    && Number.isSafeInteger(document.createdAt) && document.createdAt >= 0
    && /^[0-9a-f]{64}$/.test(documentDigest) && objectDigest(base) === documentDigest,
  'FIRST_ACCEPT_PACKET_INVALID');
  // Historical validation checks the immutable C1 evidence at its freeze clock.
  // Execution uses revalidateFirstAccept and its separate final observation.
  const validationAt = fresh ? nowMs : document.createdAt;
  await snapshotObservation({ version: 1, observation: document.observation, records: [] },
    options, validationAt);
  need(document.observation.verifiedAtMs <= document.createdAt
    && document.createdAt - document.observation.verifiedAtMs <= POLICY.freshMs,
  'STALE_OBSERVATION');
  const policyAt = fresh ? nowMs : document.createdAt;
  const offer = paperOffer(t, document.offerRecord, expectedDid, policyAt, document.observation);
  const packet = verifyApprovalView(document.packet, signing), subject = packet.subject;
  fields(subject, ['session', 'actionId', 'type', 'did', 'room', 'offer', 'contract', 'ref',
    'sourceDigest', 'nonce', 'payloadDigest']);
  need(UUID.test(subject.session) && UUID.test(subject.actionId), 'APPROVAL_BINDING_MISSING');
  need(subject.type === 'ACCEPT' && subject.did === expectedDid && subject.room === ROOM
    && subject.room === t.OFFER_ROOM && subject.offer === offer.id && subject.ref === offer.id
    && subject.sourceDigest === document.observation.source.rawSha256
    && subject.nonce === nextNonce(document.observation, document.createdAt),
  'FIRST_ACCEPT_BINDING_MISMATCH');
  need(packet.rail === 'paper' && packet.disclosure === 'PUBLIC_PAYLOAD', 'PAPER_ONLY');
  fields(packet.deadlines, ['expiresMs', 'claimByMs', 'refundAfterMs']);
  need(packet.deadlines.expiresMs === offer.expiresMs
    && packet.deadlines.claimByMs === offer.claimByMs
    && packet.deadlines.refundAfterMs === offer.refundAfterMs,
  'FIRST_ACCEPT_BINDING_MISMATCH');
  const accept = t.decodeFrame(packet.publicLine);
  need(accept.type === 'accept' && t.encodeFrame(accept) === packet.publicLine
    && accept.from === expectedDid && accept.ref === offer.id && accept.contract === subject.contract
    && accept.statement === packet.statement && accept.paymentKey === undefined,
  'FIRST_ACCEPT_BINDING_MISMATCH');
  const rebuilt = t.makeAccept(offer,
    { from: accept.from, statement: accept.statement, nonce: accept.nonce });
  need(t.encodeFrame(rebuilt) === packet.publicLine
    && t.applyFrame(t.openContract(offer), accept, document.createdAt).ok,
  'INVALID_ACCEPT');
  need(digest(signing.canonicalMessage(subject.room, subject.nonce, packet.publicLine))
    === subject.payloadDigest, 'APPROVAL_PAYLOAD_MISMATCH');
  const rebuiltPacket = canonicalApproval({ type: 'ACCEPT', subject, line: packet.publicLine },
    { offer, statement: accept.statement }, signing);
  need(rebuiltPacket.approvalDigest === packet.approvalDigest, 'TRUSTED_APPROVAL_DIGEST_MISMATCH');
  return { offer: structuredClone(offer), accept: structuredClone(accept), packet: structuredClone(packet) };
}

// A fresh execution observation is separate evidence; never edit the C1 intent.
// The coordinator observes the complete fixed export. Only relevant records are
// carried over the existing bounded IPC after this full-snapshot validation.
export async function revalidateFirstAccept(document, snapshot,
  { t, signing, nowMs = Date.now(), expectedDid = PROJECT_DID } = {}) {
  const options = runtime({ t, signing, nowMs, expectedDid });
  const validated = await validateFirstAccept(document, { ...options, fresh: false });
  const observation = await snapshotObservation(snapshot, options);
  need(observation.verifiedAtMs >= document.createdAt, 'FINAL_OBSERVATION_ROLLBACK');
  need(observation.source.generation === document.observation.source.generation
    && observation.coverage.basis === document.observation.coverage.basis,
    'PUBLIC_SNAPSHOT_FRAMING_MISMATCH');
  // Preserve the existing approved venue nonce. A changed nonce condition
  // returns to C1; this is deliberately not transport-nonce late binding.
  need(observation.observedNonce === document.observation.observedNonce
    && observation.observedNone === document.observation.observedNone
    && nextNonce(observation, document.createdAt) === validated.packet.subject.nonce,
    'FROZEN_NONCE_UNAVAILABLE');
  const selected = snapshot.records.filter(record =>
    objectDigest(record) === objectDigest(document.offerRecord));
  need(selected.length === 1, 'FROZEN_OFFER_UNAVAILABLE');
  const offer = paperOffer(t, selected[0], expectedDid, nowMs, observation);
  const related = contractRecords(t, snapshot.records.filter(record => record !== selected[0]),
    offer, validated.accept, expectedDid);
  const records = [selected[0], ...related];
  need(records.length <= POLICY.maxRecords, 'FINAL_TRANSCRIPT_TOO_LARGE');
  const seen = new Set();
  for (const record of records) {
    need(!seen.has(record.seq) && record.timestampMs <= observation.verifiedAtMs + 1000,
      'FINAL_TRANSCRIPT_INVALID');
    seen.add(record.seq);
  }
  records.sort((a, b) => a.seq - b.seq);
  const folded = t.foldTranscript(records);
  need(folded.steps.every(step => step.ok) && folded.state?.status === 'proposed'
    && t.applyFrame(folded.state, validated.accept, nowMs).ok, 'FROZEN_ACCEPT_UNAVAILABLE');
  const admissionSnapshot = { version: 1, observation: structuredClone(observation),
    records: structuredClone(records) };
  // Leave space for the unchanged admission fields under the existing 64KiB RPC cap.
  need(Buffer.byteLength(JSON.stringify(admissionSnapshot))
    + Buffer.byteLength(JSON.stringify(document.offerRecord)) < 60000, 'FINAL_TRANSCRIPT_TOO_LARGE');
  return { ...validated, observation: structuredClone(observation), admissionSnapshot };
}

export async function matchFirstAccept(document, snapshot, expectedEnvelopeDigest,
  { t, signing, nowMs = Date.now(), expectedDid = PROJECT_DID } = {}) {
  const options = runtime({ t, signing, nowMs, expectedDid });
  const validated = await validateFirstAccept(document, { ...options, fresh: false });
  const observation = await snapshotObservation(snapshot, options);
  need(observation.source.generation === document.observation.source.generation
    && observation.coverage.basis === document.observation.coverage.basis,
  'PUBLIC_SNAPSHOT_FRAMING_MISMATCH');
  need(typeof expectedEnvelopeDigest === 'string' && /^[0-9a-f]{64}$/.test(expectedEnvelopeDigest),
    'ENVELOPE_DIGEST_REQUIRED');
  const seqs = new Set();
  for (const record of snapshot.records) {
    // Public-reader sequence metadata is not authentication. Reject duplicate
    // valid sequences without spending crypto on unrelated public traffic.
    if (!Number.isSafeInteger(record?.seq) || record.seq < 0) continue;
    need(!seqs.has(record.seq), 'DUPLICATE_PUBLIC_SEQUENCE');
    seqs.add(record.seq);
  }
  const subject = validated.packet.subject;
  const matches = snapshot.records.filter(record => {
    // This only narrows candidates; every possible match still needs the full
    // verifier and the unchanged timestamp / exact-envelope checks below.
    if (record?.room !== subject.room || record.sender !== subject.did
        || record.nonce !== subject.nonce || record.line !== validated.packet.publicLine
        || !(record.seq > document.offerRecord.seq)) return false;
    try {
      verifyRecord(t, record);
      return record.timestampMs >= document.offerRecord.timestampMs
        && record.timestampMs <= observation.verifiedAtMs + 1000
        && recordDigest(record) === expectedEnvelopeDigest;
    } catch { return false; }
  });
  if (matches.length !== 1) return { status: 'AMBIGUOUS', retry: false };
  return { status: 'RECONCILED_PRESENT', record: structuredClone(matches[0]) };
}
