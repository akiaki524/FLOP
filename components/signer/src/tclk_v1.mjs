import { createHash, ECDH, randomBytes } from 'node:crypto';

export const TCLK_COMMIT = '5cc4ab93efbc8999a3a7e1471b639deca25998ea';
export const TCLK_VERSION = 'tclk/1';
export const TCLK_PREFIX = 'tclk1 ';
export const TCLK_DOMAIN = 'FLOP::tclk::v1';
export const OFFER_ROOM = 'tclk-offers';
export const PAPER_RECORD_PREFIX = 'tclkpaper1';
export const MAX_FRAME_CHARS = 4096;

const DID_RE = /^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$/;
const HEX32_RE = /^0x[0-9a-f]{64}$/;
const HEX33_RE = /^0x[0-9a-f]{66}$/;
const TCLK_NONCE_RE = /^[0-9a-f]{8,64}$/;
const ASSET_RE = /^[A-Za-z0-9_-]{1,32}$/;
const JOB_PROTO_RE = /^[a-z0-9][a-z0-9._-]{0,31}$/;

function fail(code) { throw new Error(code); }
function sha256Hex(value) { return createHash('sha256').update(value).digest('hex'); }
function exactKeys(value, allowed, required = allowed) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('FRAME_OBJECT_REQUIRED');
  const keys = Object.keys(value);
  if (keys.some(key => !allowed.includes(key))) fail('UNKNOWN_FRAME_FIELD');
  if (required.some(key => value[key] === undefined)) fail('MISSING_FRAME_FIELD');
}
function requireDid(value) { if (typeof value !== 'string' || !DID_RE.test(value)) fail('INVALID_DID'); return value; }
function requireHex32(value) { if (typeof value !== 'string' || !HEX32_RE.test(value)) fail('INVALID_HEX32'); return value; }
function requirePaymentKey(value) {
  if (typeof value !== 'string' || !HEX33_RE.test(value)) fail('INVALID_PAYMENT_KEY');
  try { ECDH.convertKey(value.slice(2), 'secp256k1', 'hex', 'hex', 'compressed'); }
  catch { fail('INVALID_PAYMENT_KEY'); }
  return value;
}
function requireTclkNonce(value) { if (typeof value !== 'string' || !TCLK_NONCE_RE.test(value)) fail('INVALID_TCLK_NONCE'); return value; }
function requireMs(value) { if (!Number.isSafeInteger(value) || value <= 0) fail('INVALID_TIME'); return value; }

export function canonicalJson(value) {
  if (value === null || typeof value !== 'object') {
    const encoded = JSON.stringify(value);
    if (encoded === undefined) fail('UNSUPPORTED_JSON_VALUE');
    return encoded;
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  return `{${Object.keys(value).sort().filter(key => value[key] !== undefined)
    .map(key => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
}

export function toAscii(json) {
  return json.replace(/[\u0080-\uffff]/g, ch => `\\u${ch.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

function domainHash(tag, payload) {
  return `0x${sha256Hex(`${TCLK_DOMAIN}|${tag}|${toAscii(payload)}`)}`;
}

export function offerId(fields) { return domainHash('offer', canonicalJson(fields)); }
export function contractId(offer, acceptCore) { return domainHash('contract', canonicalJson({ offer, accept: acceptCore })); }

function validateJob(job) {
  exactKeys(job, ['proto', 'id', 'context'], ['proto', 'id']);
  if (typeof job.proto !== 'string' || !JOB_PROTO_RE.test(job.proto)) fail('INVALID_JOB_PROTO');
  if (typeof job.id !== 'string' || job.id.length === 0) fail('INVALID_JOB_ID');
  if (job.context !== undefined && (typeof job.context !== 'string' || job.context.length === 0)) fail('INVALID_JOB_CONTEXT');
}

export function validateOffer(offer) {
  exactKeys(offer,
    ['type','from','role','amount','asset','lock','rails','claimByMs','refundAfterMs','expiresMs','paymentKey','job','nonce','id'],
    ['type','from','role','amount','asset','lock','rails','claimByMs','refundAfterMs','expiresMs','nonce','id']);
  if (offer.type !== 'offer') fail('NOT_OFFER');
  requireDid(offer.from);
  if (!['payer','payee'].includes(offer.role)) fail('INVALID_OFFER_ROLE');
  if (typeof offer.amount !== 'string' || !/^[1-9][0-9]*$/.test(offer.amount)) fail('INVALID_AMOUNT');
  if (typeof offer.asset !== 'string' || !ASSET_RE.test(offer.asset)) fail('INVALID_ASSET');
  if (!['hash','point'].includes(offer.lock)) fail('INVALID_LOCK_KIND');
  if (!Array.isArray(offer.rails) || offer.rails.length === 0 || offer.rails.some(v => typeof v !== 'string' || v.length === 0)) fail('INVALID_RAILS');
  const claim = requireMs(offer.claimByMs), refund = requireMs(offer.refundAfterMs); requireMs(offer.expiresMs);
  if (claim >= refund) fail('INVALID_DEADLINES');
  if (offer.paymentKey !== undefined) fail('PAYMENT_KEY_UNSUPPORTED');
  if (offer.lock === 'point') fail('POINT_LOCK_UNSUPPORTED');
  if (offer.job !== undefined) validateJob(offer.job);
  requireTclkNonce(offer.nonce);
  const { id, ...fields } = offer;
  if (typeof id !== 'string' || id !== offerId(fields)) fail('OFFER_ID_MISMATCH');
  return structuredClone(offer);
}

function validateAccept(frame) {
  exactKeys(frame, ['type','from','ref','statement','contract','paymentKey','nonce'], ['type','from','ref','statement','contract','nonce']);
  if (frame.type !== 'accept') fail('NOT_ACCEPT');
  requireDid(frame.from); requireHex32(frame.ref); requireHex32(frame.statement); requireHex32(frame.contract); requireTclkNonce(frame.nonce);
  if (frame.paymentKey !== undefined) requirePaymentKey(frame.paymentKey);
  return frame;
}
function validateLock(frame) {
  exactKeys(frame, ['type','from','contract','rail','ref','presig'], ['type','from','contract','rail','ref']);
  if (frame.type !== 'lock') fail('NOT_LOCK');
  requireDid(frame.from); requireHex32(frame.contract);
  if (frame.rail !== 'paper' || typeof frame.ref !== 'string' || frame.ref.length === 0 || frame.presig !== undefined) fail('NON_PAPER_LOCK');
  return frame;
}
function validateReveal(frame) {
  exactKeys(frame, ['type','from','contract','ref','secret'], ['type','from','contract','secret']);
  if (frame.type !== 'reveal') fail('NOT_REVEAL');
  requireDid(frame.from); requireHex32(frame.contract); requireHex32(frame.secret);
  if (frame.ref !== undefined && (typeof frame.ref !== 'string' || frame.ref.length === 0)) fail('INVALID_REF');
  return frame;
}
function validateHeartbeat(frame) {
  exactKeys(frame, ['type','from','contract','nonce','note'], ['type','from','contract','nonce']);
  if (frame.type !== 'heartbeat') fail('NOT_HEARTBEAT');
  requireDid(frame.from); requireHex32(frame.contract); requireTclkNonce(frame.nonce);
  if (frame.note !== undefined && (typeof frame.note !== 'string' || frame.note.length === 0)) fail('INVALID_HEARTBEAT_NOTE');
  return frame;
}
function validateCancel(frame) {
  exactKeys(frame, ['type','from','contract','reason'], ['type','from','contract']);
  if (frame.type !== 'cancel') fail('NOT_CANCEL');
  requireDid(frame.from); requireHex32(frame.contract);
  if (frame.reason !== undefined && (typeof frame.reason !== 'string' || frame.reason.length === 0)) fail('INVALID_REASON');
  return frame;
}

export function validateFrame(frame) {
  if (!frame || typeof frame !== 'object' || Array.isArray(frame)) fail('FRAME_OBJECT_REQUIRED');
  switch (frame.type) {
    case 'offer': return validateOffer(frame);
    case 'accept': return validateAccept(frame);
    case 'lock': return validateLock(frame);
    case 'reveal': return validateReveal(frame);
    case 'heartbeat': return validateHeartbeat(frame);
    case 'cancel': return validateCancel(frame);
    default: fail('UNSUPPORTED_FRAME');
  }
}

export function encodeFrame(frame) {
  const validated = validateFrame(frame);
  const line = TCLK_PREFIX + toAscii(canonicalJson(validated));
  if (line.length > MAX_FRAME_CHARS || !/^[\x20-\x7e]*$/.test(line)) fail('FRAME_NOT_EMITTABLE');
  return line;
}

export function decodeFrame(line) {
  if (typeof line !== 'string' || !line.startsWith(TCLK_PREFIX) || line.length > MAX_FRAME_CHARS) fail('INVALID_TCLK_LINE');
  let parsed;
  try { parsed = JSON.parse(line.slice(TCLK_PREFIX.length)); } catch { fail('INVALID_TCLK_JSON'); }
  const validated = validateFrame(parsed);
  return structuredClone(validated);
}

export function makeOffer(fields, nonce = randomBytes(8).toString('hex')) {
  const body = { ...fields, type: 'offer', nonce };
  return validateOffer({ ...body, id: offerId(body) });
}

export function makeAccept(offer, { from, statement, nonce = randomBytes(8).toString('hex') }) {
  const checked = validateOffer(offer); requireDid(from); requireHex32(statement); requireTclkNonce(nonce);
  if (from === checked.from) fail('ACCEPT_SELF');
  const core = { from, ref: checked.id, statement, nonce };
  return validateAccept({ type: 'accept', ...core, contract: contractId(checked, core) });
}

export function makeHeartbeat({ from, contract, nonce = randomBytes(8).toString('hex') }) {
  return validateHeartbeat({ type: 'heartbeat', from, contract, nonce });
}
export function makeReveal({ from, contract, ref, secret }) {
  return validateReveal({ type: 'reveal', from, contract, ...(ref === undefined ? {} : { ref }), secret });
}
export function hashLockFromPreimage(preimage) {
  const raw = Buffer.from(preimage ?? []); if (raw.length !== 32) fail('PREIMAGE_LENGTH');
  return { preimage: `0x${raw.toString('hex')}`, hash: `0x${sha256Hex(raw)}` };
}
export function verifyHashPreimage(hash, preimage) {
  try {
    const raw = typeof preimage === 'string' && /^0x[0-9a-f]{64}$/.test(preimage) ? Buffer.from(preimage.slice(2), 'hex') : Buffer.from(preimage ?? []);
    return hashLockFromPreimage(raw).hash === hash;
  } catch { return false; }
}
export function dealRoom(contract) { requireHex32(contract); return `mb-p-tclk-${contract.slice(2, 18)}`; }
export function paperNote(contract) { requireHex32(contract); return { ns: `tclk-paper-${contract.slice(2, 4)}`, key: contract.slice(4, 18) }; }
export function decodePaperRecord(value) {
  if (typeof value !== 'string') return null;
  const parts = value.split(' '); if (parts.length < 5 || parts.length > 6) return null;
  const [prefix,status,lock,statement,refundAfter,secret] = parts;
  if (prefix !== PAPER_RECORD_PREFIX || !['locked','claimed','refunded'].includes(status) || lock !== 'hash' || !HEX32_RE.test(statement)) return null;
  const refundAfterMs = Number(refundAfter); if (!Number.isSafeInteger(refundAfterMs) || refundAfterMs <= 0) return null;
  if (secret !== undefined && !HEX32_RE.test(secret)) return null;
  if ((status === 'claimed') !== (secret !== undefined)) return null;
  return { status, lock, statement, refundAfterMs, ...(secret === undefined ? {} : { secret }) };
}
export function encodePaperRecord(record) {
  if (!record || !['locked','claimed','refunded'].includes(record.status) || record.lock !== 'hash' || !HEX32_RE.test(record.statement)
      || !Number.isSafeInteger(record.refundAfterMs) || record.refundAfterMs <= 0) fail('INVALID_PAPER_RECORD');
  if ((record.status === 'claimed') !== (record.secret !== undefined)) fail('INVALID_PAPER_RECORD');
  if (record.secret !== undefined) requireHex32(record.secret);
  const head = `${PAPER_RECORD_PREFIX} ${record.status} hash ${record.statement} ${record.refundAfterMs}`;
  return record.secret === undefined ? head : `${head} ${record.secret}`;
}
