// Stage 1 Real transport: one Human-approved Paper ACCEPT to the fixed Technocore origin.
// This module never signs, reserves a nonce, retries, follows redirects, or interprets a
// response as acknowledgement. Once dispatch starts, read-only reconciliation is required.
import { lstat, open, readFile } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';
import https from 'node:https';

import { PROJECT_DID } from './connection_approval.mjs';
import { Denied, digest, envelopeDigest, fields, objectDigest,
  POLICY, requireThat as need, verifyRecord } from './pilot_protocol.mjs';
import { verifyApprovalView } from './pilot_approval.mjs';

const STAGE1_ROOM = 'tclk-offers';
const DEFAULT_TIMEOUT_MS = 3_000;
const DEFAULT_RESPONSE_BYTES = 16_384;
// These are the canonical path spellings accepted by the official signed lane.
// verifyRecord performs the cryptographic checks; these guards additionally make
// the raw path segments unambiguous and prevent delimiter injection.
const CANONICAL_DID = /^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$/;
const CANONICAL_SIGNATURE = /^[A-Za-z0-9_-]{85}[AQgw]$/;
const CANONICAL_NONCE = /^(0|[1-9][0-9]{0,18})$/;

function stage1Offer(t, offerRecord) {
  verifyRecord(t, offerRecord);
  need(offerRecord.room === STAGE1_ROOM && offerRecord.room === t.OFFER_ROOM, 'WRONG_ROOM');
  const offer = t.decodeFrame(offerRecord.line);
  need(offer.type === 'offer' && t.encodeFrame(offer) === offerRecord.line, 'INVALID_OFFER');
  need(offerRecord.sender === offer.from, 'OFFER_SENDER_MISMATCH');
  need(offer.role === 'payer', 'PAPER_ONLY');
  need(offer.asset === 'PAPER' && offer.lock === 'hash', 'PAPER_ONLY');
  need(Array.isArray(offer.rails) && offer.rails.length === 1 && offer.rails[0] === 'paper', 'PAPER_ONLY');
  need(typeof offer.amount === 'string' && /^[1-9][0-9]*$/.test(offer.amount), 'VALUE_BEARING_DISABLED');
  need(offer.paymentKey === undefined, 'VALUE_BEARING_DISABLED');
  return offer;
}

/** Validate the exact approved/signed handoff without mutating durable state. */
export function validateAcceptHandoff(value, { t, signing, expectedDid = PROJECT_DID,
  nowMs = Date.now() }) {
  fields(value, ['record', 'packet', 'approval', 'offerRecord']);
  need(t && signing, 'OFFICIAL_RUNTIME_REQUIRED');
  need(Number.isSafeInteger(nowMs) && nowMs >= 0, 'INVALID_TIME');
  const { record, packet, approval, offerRecord } = value;
  fields(approval, ['actionId', 'subjectDigest', 'approvalDigest', 'expiresAt']);
  const canonical = verifyApprovalView(packet, signing);
  const subject = canonical.subject;
  need(subject && typeof subject === 'object', 'APPROVAL_REQUIRED');
  fields(subject, ['session', 'actionId', 'type', 'did', 'room', 'offer', 'contract', 'ref',
    'sourceDigest', 'nonce', 'payloadDigest']);
  need(typeof subject.session === 'string' && subject.session.length > 0, 'APPROVAL_BINDING_MISSING');
  need(typeof subject.actionId === 'string' && subject.actionId.length > 0, 'APPROVAL_BINDING_MISSING');
  need(subject.type === 'ACCEPT', 'ACCEPT_ONLY');
  need(subject.did === expectedDid, 'WRONG_DID');
  need(subject.room === STAGE1_ROOM && subject.room === t.OFFER_ROOM, 'WRONG_ROOM');
  need(canonical.rail === 'paper' && canonical.disclosure === 'PUBLIC_PAYLOAD', 'PAPER_ONLY');
  need(approval.actionId === subject.actionId && approval.subjectDigest === packet.subjectDigest
    && approval.approvalDigest === packet.approvalDigest, 'APPROVAL_PAYLOAD_MISMATCH');
  need(Number.isSafeInteger(approval.expiresAt) && approval.expiresAt > nowMs, 'APPROVAL_EXPIRED');
  need(objectDigest(subject) === packet.subjectDigest, 'APPROVAL_PAYLOAD_MISMATCH');

  verifyRecord(t, record);
  need(record.room === subject.room && record.sender === subject.did && record.nonce === subject.nonce,
    'SIGNED_RECORD_BINDING_MISMATCH');
  need(record.line === canonical.publicLine, 'SIGNED_RECORD_BINDING_MISMATCH');
  need(digest(signing.canonicalMessage(record.room, record.nonce, record.line)) === subject.payloadDigest,
    'APPROVAL_PAYLOAD_MISMATCH');

  const offer = stage1Offer(t, offerRecord);
  fields(canonical.deadlines, ['expiresMs', 'claimByMs', 'refundAfterMs']);
  need(canonical.deadlines.expiresMs === offer.expiresMs
    && canonical.deadlines.claimByMs === offer.claimByMs
    && canonical.deadlines.refundAfterMs === offer.refundAfterMs, 'APPROVAL_PAYLOAD_MISMATCH');
  need(nowMs + POLICY.acceptMarginMs < offer.expiresMs, 'ACCEPT_DEADLINE');
  need(nowMs + POLICY.claimMarginMs < offer.claimByMs, 'CLAIM_DEADLINE');
  need(offer.refundAfterMs - offer.claimByMs >= POLICY.refundGapMs, 'REFUND_MARGIN');
  need(subject.offer === offer.id && subject.ref === offer.id, 'WRONG_OFFER');
  const accept = t.decodeFrame(record.line);
  need(accept.type === 'accept', 'ACCEPT_ONLY');
  need(canonical.statement === accept.statement, 'APPROVAL_PAYLOAD_MISMATCH');
  need(t.encodeFrame(accept) === record.line, 'NON_CANONICAL_ACCEPT');
  need(accept.from === subject.did && accept.ref === offer.id && accept.contract === subject.contract,
    'ACCEPT_BINDING_MISMATCH');
  need(accept.paymentKey === undefined, 'VALUE_BEARING_DISABLED');
  const rebuilt = t.makeAccept(offer, { from: accept.from, statement: accept.statement, nonce: accept.nonce });
  need(t.encodeFrame(rebuilt) === record.line, 'INVALID_ACCEPT');

  return Object.freeze({ record: structuredClone(record), offerRecord: structuredClone(offerRecord),
    subject: structuredClone(subject), approval: structuredClone(approval),
    envelopeDigest: envelopeDigest(record) });
}

/** Construct the only Stage 1 request. No caller-controlled URL/origin is accepted. */
export function buildAcceptRequest(value, options) {
  const validated = validateAcceptHandoff(value, options);
  const r = validated.record;
  need(r.room === STAGE1_ROOM, 'WRONG_ROOM');
  need(CANONICAL_DID.test(r.sender) && !r.sender.includes('%'), 'NON_CANONICAL_DID');
  need(CANONICAL_SIGNATURE.test(r.signature) && !r.signature.includes('%'),
    'NON_CANONICAL_SIGNATURE');
  need(CANONICAL_NONCE.test(r.nonce) && !r.nonce.includes('%'), 'NON_CANONICAL_NONCE');
  // The official surface keeps the fixed room, DID, signature and nonce raw.
  // Only the signed text is encoded for its path segment; signing bytes remain
  // the validated canonical record.line above.
  const components = [r.room, 'say-signed', r.sender, r.signature, r.nonce,
    encodeURIComponent(r.line)];
  return Object.freeze({ protocol: 'https:', hostname: 'technocore.chat', port: 443,
    method: 'GET', path: '/r/' + components.join('/'), agent: false,
    envelopeDigest: validated.envelopeDigest, actionId: validated.subject.actionId });
}

/** Fixed HTTPS adapter. `httpsModule` exists only for offline boundary tests. */
function requestAcceptOnce(request, { timeoutMs = DEFAULT_TIMEOUT_MS,
  maxResponseBytes = DEFAULT_RESPONSE_BYTES, httpsModule = https } = {}) {
  need(request?.protocol === 'https:' && request.hostname === 'technocore.chat'
    && request.port === 443 && request.method === 'GET' && request.agent === false
    && typeof request.path === 'string' && request.path.startsWith('/r/tclk-offers/say-signed/'),
  'TRANSPORT_POLICY_REJECTED');
  need(Number.isSafeInteger(timeoutMs) && timeoutMs > 0 && timeoutMs <= 30_000, 'INVALID_TIMEOUT');
  need(Number.isSafeInteger(maxResponseBytes) && maxResponseBytes > 0 && maxResponseBytes <= 65_536,
    'INVALID_RESPONSE_BOUND');
  return new Promise((resolve, reject) => {
    let settled = false, bytes = 0, timer;
    const finish = (fn, value) => { if (settled) return; settled = true; clearTimeout(timer); fn(value); };
    const req = httpsModule.request({ protocol: 'https:', hostname: 'technocore.chat', port: 443,
      method: 'GET', path: request.path, agent: false, headers: { accept: 'text/plain' } }, response => {
      response.on('data', chunk => {
        bytes += Buffer.byteLength(chunk);
        if (bytes > maxResponseBytes) {
          response.destroy?.(); req.destroy?.();
          finish(reject, new Denied('RESPONSE_TOO_LARGE'));
        }
      });
      response.on('end', () => finish(resolve, { statusCode: response.statusCode, bytes }));
      response.on('error', error => finish(reject, error));
    });
    timer = setTimeout(() => {
      req.destroy?.(); finish(reject, new Denied('TRANSPORT_TIMEOUT'));
    }, timeoutMs);
    req.once('error', error => finish(reject, error));
    req.end();
  });
}

async function durableClaim(stateDir, validated, nowMs) {
  const path = join(stateDir, 'first-real-accept.json');
  const receipt = { version: 1, state: 'SEND_ATTEMPTED', actionId: validated.subject.actionId,
    subjectDigest: objectDigest(validated.subject), envelopeDigest: validated.envelopeDigest, createdAt: nowMs };
  let handle;
  try {
    handle = await open(path, 'wx', 0o600);
    const serialized = JSON.stringify(receipt) + '\n';
    await handle.writeFile(serialized, 'utf8');
    await handle.sync();
    await handle.close(); handle = null;
    const directory = await open(stateDir, 'r');
    try { await directory.sync(); } finally { await directory.close(); }
    need(await readFile(path, 'utf8') === serialized, 'TRANSPORT_RECEIPT_MISMATCH');
  } catch (error) {
    if (handle) await handle.close().catch(() => {});
    if (error?.code === 'EEXIST') throw new Denied('NO_RETRY');
    throw error;
  }
}

/** Trusted service factory. Writes remain disabled unless explicitly enabled by its owner. */
function createRealTransport({ stateDir, writeEnabled = false,
  requestImpl = null, timeoutMs = DEFAULT_TIMEOUT_MS,
  maxResponseBytes = DEFAULT_RESPONSE_BYTES, httpsModule = https, clock = Date.now } = {}) {
  need(typeof stateDir === 'string' && isAbsolute(stateDir), 'INVALID_STATE_DIR');
  need(typeof writeEnabled === 'boolean' && (requestImpl === null || typeof requestImpl === 'function'),
    'INVALID_TRANSPORT_CONFIG');
  return Object.freeze({
    async sendAcceptOnce(value, options) {
      need(writeEnabled, 'EXTERNAL_WRITE_DISABLED');
      const startedAt = clock();
      const nowMs = options?.nowMs ?? Date.now();
      const validated = validateAcceptHandoff(value, { ...options, nowMs });
      const request = buildAcceptRequest(value, { ...options, nowMs });
      const state = await lstat(stateDir);
      need(state.isDirectory() && !state.isSymbolicLink() && (state.mode & 0o077) === 0,
        'UNSAFE_STATE_DIR');
      await durableClaim(stateDir, validated, nowMs);
      const dispatchNow = nowMs + Math.max(0, clock() - startedAt);
      validateAcceptHandoff(value, { ...options, nowMs: dispatchNow });
      try {
        if (requestImpl) await requestImpl(request, { timeoutMs, maxResponseBytes });
        else await requestAcceptOnce(request, { timeoutMs, maxResponseBytes, httpsModule });
      } catch { /* Dispatch started: absence of a classified ACK is ambiguous. */ }
      return Object.freeze({ status: 'AMBIGUOUS', retry: false,
        next: 'READ_ONLY_RECONCILIATION', envelopeDigest: validated.envelopeDigest });
    },
  });
}

/** Integration-facing factory: official runtime dependencies are fixed at construction. */
export function createAcceptTransport({ t, signing, expectedDid = PROJECT_DID,
  stateDirectory, writeEnabled = false, requestImpl = null,
  timeoutMs = DEFAULT_TIMEOUT_MS, maxResponseBytes = DEFAULT_RESPONSE_BYTES,
  httpsModule = https, clock = Date.now } = {}) {
  need(t && signing, 'OFFICIAL_RUNTIME_REQUIRED');
  const service = createRealTransport({ stateDir: stateDirectory, writeEnabled, requestImpl,
    timeoutMs, maxResponseBytes, httpsModule, clock });
  return Object.freeze({ send: (value, { nowMs = Date.now() } = {}) => service.sendAcceptOnce(value,
    { t, signing, expectedDid, nowMs }) });
}
