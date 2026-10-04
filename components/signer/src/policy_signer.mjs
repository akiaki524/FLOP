import {
  createCipheriv,
  createDecipheriv,
  createHash,
  hkdfSync,
  randomBytes,
} from 'node:crypto';
import {
  closeSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from 'node:fs';
import { dirname } from 'node:path';

import {
  canonicalMessage,
  signerFromSeed,
  sweep,
  verifyDidSignature,
} from './technocore_signing.mjs';
import {
  TCLK_COMMIT,
  TCLK_VERSION,
  OFFER_ROOM,
  contractId,
  decodeFrame,
  decodePaperRecord,
  dealRoom,
  encodeFrame,
  hashLockFromPreimage,
  makeAccept,
  makeHeartbeat,
  makeReveal,
  validateOffer,
  verifyHashPreimage,
} from './tclk_v1.mjs';
import { normalizeExportSnapshot, validateSnapshot } from './technocore_read.mjs';

const STATE_VERSION = 1;
const STATE_KIND = 'FLOP_PROJECT_DID_POLICY_SIGNER_STATE_V1';
const MAX_POLICY_MS = 24 * 60 * 60 * 1000;
const SOURCE_RE = /^gcd_lcm ([1-9][0-9]{0,8}) ([1-9][0-9]{0,8})\n$/;
const ACTIONS = new Set(['ACCEPT','INITIAL_HEARTBEAT','DELIVERY_GCD','REVEAL']);
export const CLOSE_CALL_POLICY_PROTOCOL = 'close-call/1';
export const CLOSE_CALL_ACTION = 'CLOSE_CALL_OWNER_REGISTER';
export const CLOSE_CALL_TAKER_LONG_POLICY_PROTOCOL = 'close-call-taker-long/1';
export const CLOSE_CALL_TAKER_LONG_ACTION = 'CLOSE_CALL_TAKER_LONG';
export const CLOSE_CALL_CONTEST = 'close-1';
export const CLOSE_CALL_ROOM = 'close1';
export const CLOSE_CALL_PRICE_ROOM = 'd-close1-price';
export const CLOSE_CALL_RULES_COMMIT = '66c1da36538e4b1c685417d2f66922906b13fea0';
export const CLOSE_CALL_PACKAGE_MANIFEST_SHA256 = 'bae09812e25eb6f1369c611f24964f7ea0acafddfc45301a16f33f941296dafa';
export const CLOSE_CALL_NONCE_POLICY = 'SIGNER_ALLOCATES_ROOM_NONCE';
export const CLOSE_CALL_LOCK_MS = Date.parse('2026-10-04T09:00:00Z');
export const CLOSE_CALL_LOCK_SWEEP = 2556;
export const CLOSE_CALL_EXPECTED_SWEEP_SECONDS = 300;

const DID_RE = /^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$/;
const SIG_RE = /^[A-Za-z0-9_-]{85}[AQgw]$/;
const AMOUNT_RE = /^[0-9]{1,7}(?:\.[0-9]{1,2})?$/;
const TRADE_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
const HEX64_RE = /^[0-9a-f]{64}$/;

export class PolicySignerError extends Error {
  constructor(code) { super(code); this.code = code; }
}
function need(condition, code) { if (!condition) throw new PolicySignerError(code); }
function stable(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${stable(value[k])}`).join(',')}}`;
}
function digest(value) { return createHash('sha256').update(typeof value === 'string' || Buffer.isBuffer(value) ? value : stable(value)).digest('hex'); }
function exactKeys(value, names) {
  need(value && typeof value === 'object' && !Array.isArray(value), 'INVALID_REQUEST');
  need(Object.keys(value).sort().join(',') === [...names].sort().join(','), 'INVALID_FIELDS');
}
function atomicWrite(path, bytes) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const tmp = `${path}.tmp-${process.pid}-${Date.now()}`;
  const fd = openSync(tmp, 'w', 0o600);
  try { writeFileSync(fd, bytes); fsyncSync(fd); } finally { closeSync(fd); }
  renameSync(tmp, path);
  const dfd = openSync(dirname(path), 'r');
  try { fsyncSync(dfd); } finally { closeSync(dfd); }
}
function stateKey(seed) { return Buffer.from(hkdfSync('sha256', Buffer.from(seed), Buffer.alloc(0), Buffer.from(STATE_KIND), 32)); }
function aad(did, policyDigest) { return Buffer.from(`${STATE_KIND}|${did}|${policyDigest}`, 'utf8'); }
function encryptState(key, did, policyDigest, state, rng) {
  const iv = Buffer.from(rng(12)); need(iv.length === 12, 'STATE_RNG');
  const cipher = createCipheriv('aes-256-gcm', key, iv); cipher.setAAD(aad(did, policyDigest));
  const ciphertext = Buffer.concat([cipher.update(Buffer.from(stable(state), 'utf8')), cipher.final()]);
  return {
    version: STATE_VERSION, kind: STATE_KIND, did, policyDigest,
    iv: iv.toString('base64url'), ciphertext: ciphertext.toString('base64url'), tag: cipher.getAuthTag().toString('base64url'),
  };
}
function decryptState(key, did, policyDigest, envelope) {
  try {
    exactKeys(envelope, ['version','kind','did','policyDigest','iv','ciphertext','tag']);
    need(envelope.version === STATE_VERSION && envelope.kind === STATE_KIND && envelope.did === did && envelope.policyDigest === policyDigest, 'STATE_BINDING');
    const decipher = createDecipheriv('aes-256-gcm', key, Buffer.from(envelope.iv, 'base64url'));
    decipher.setAAD(aad(did, policyDigest)); decipher.setAuthTag(Buffer.from(envelope.tag, 'base64url'));
    const raw = Buffer.concat([decipher.update(Buffer.from(envelope.ciphertext, 'base64url')), decipher.final()]);
    return JSON.parse(raw.toString('utf8'));
  } catch (error) {
    if (error instanceof PolicySignerError) throw error;
    throw new PolicySignerError('STATE_CORRUPT');
  }
}

export function validatePolicy(policy) {
  need(policy && typeof policy === 'object' && !Array.isArray(policy), 'INVALID_REQUEST');
  if (policy.protocol === TCLK_VERSION) {
    exactKeys(policy, ['version','protocol','tclkCommit','expectedDid','workFamily','jobProto','jobContextPrefix','issuedAtMs','expiresAtMs','freshMs','acceptMarginMs','minCompletionWindowMs','claimMarginMs','refundGapMs','maxWorkItems','maxSignatures','requireHeartbeat','noValueMarker']);
    need(policy.version === 1 && policy.tclkCommit === TCLK_COMMIT, 'POLICY_PROTOCOL');
    need(typeof policy.expectedDid === 'string' && policy.expectedDid.startsWith('did:key:z6Mk'), 'POLICY_DID');
    need(policy.workFamily === 'math.gcd_lcm' && policy.jobProto === 'a2a' && typeof policy.jobContextPrefix === 'string' && policy.jobContextPrefix.length > 0, 'POLICY_WORK_FAMILY');
    need(Number.isSafeInteger(policy.issuedAtMs) && Number.isSafeInteger(policy.expiresAtMs) && policy.expiresAtMs > policy.issuedAtMs && policy.expiresAtMs - policy.issuedAtMs <= MAX_POLICY_MS, 'POLICY_LEASE');
    for (const k of ['freshMs','acceptMarginMs','minCompletionWindowMs','claimMarginMs','refundGapMs']) need(Number.isSafeInteger(policy[k]) && policy[k] > 0, 'POLICY_LIMIT');
    need(policy.maxWorkItems === 1 && typeof policy.requireHeartbeat === 'boolean'
      && policy.maxSignatures === (policy.requireHeartbeat ? 4 : 3), 'POLICY_LIMIT');
    need(policy.noValueMarker === 'EXPLICIT_PAPER_NO_VALUE', 'POLICY_NO_VALUE');
    return structuredClone(policy);
  }
  if (policy.protocol === CLOSE_CALL_POLICY_PROTOCOL) {
    exactKeys(policy, ['version','protocol','expectedDid','contest','rulesCommit','packageManifestSha256','room','noncePolicy','issuedAtMs','expiresAtMs','notAfterMs','freshMs','maxSignatures','noValueMarker']);
    need(policy.version === 1, 'POLICY_PROTOCOL');
    need(typeof policy.expectedDid === 'string' && policy.expectedDid.startsWith('did:key:z6Mk'), 'POLICY_DID');
    need(policy.contest === CLOSE_CALL_CONTEST
      && policy.rulesCommit === CLOSE_CALL_RULES_COMMIT
      && policy.packageManifestSha256 === CLOSE_CALL_PACKAGE_MANIFEST_SHA256
      && policy.room === CLOSE_CALL_ROOM
      && policy.noncePolicy === CLOSE_CALL_NONCE_POLICY, 'POLICY_PROTOCOL');
    need(Number.isSafeInteger(policy.issuedAtMs) && Number.isSafeInteger(policy.expiresAtMs)
      && policy.expiresAtMs > policy.issuedAtMs
      && policy.expiresAtMs - policy.issuedAtMs <= MAX_POLICY_MS
      && policy.notAfterMs === CLOSE_CALL_LOCK_MS
      && policy.expiresAtMs <= policy.notAfterMs, 'POLICY_LEASE');
    need(Number.isSafeInteger(policy.freshMs) && policy.freshMs > 0
      && policy.maxSignatures === 1, 'POLICY_LIMIT');
    need(policy.noValueMarker === 'EXPLICIT_PAPER_NO_VALUE', 'POLICY_NO_VALUE');
    return structuredClone(policy);
  }
  if (policy.protocol === CLOSE_CALL_TAKER_LONG_POLICY_PROTOCOL) {
    exactKeys(policy, ['version','protocol','action','expectedDid','contest','rulesCommit','packageManifestSha256','room','priceRoom','refereeDid','noncePolicy','issuedAtMs','expiresAtMs','notAfterMs','freshMs','maxSignatures','noValueMarker']);
    need(policy.version === 1 && policy.action === CLOSE_CALL_TAKER_LONG_ACTION,
      'POLICY_PROTOCOL');
    need(typeof policy.expectedDid === 'string' && DID_RE.test(policy.expectedDid),
      'POLICY_DID');
    need(policy.contest === CLOSE_CALL_CONTEST
      && policy.rulesCommit === CLOSE_CALL_RULES_COMMIT
      && policy.packageManifestSha256 === CLOSE_CALL_PACKAGE_MANIFEST_SHA256
      && policy.room === CLOSE_CALL_ROOM
      && policy.priceRoom === CLOSE_CALL_PRICE_ROOM
      && typeof policy.refereeDid === 'string' && DID_RE.test(policy.refereeDid)
      && policy.refereeDid !== policy.expectedDid
      && policy.noncePolicy === CLOSE_CALL_NONCE_POLICY, 'POLICY_PROTOCOL');
    need(Number.isSafeInteger(policy.issuedAtMs) && Number.isSafeInteger(policy.expiresAtMs)
      && policy.expiresAtMs > policy.issuedAtMs
      && policy.expiresAtMs - policy.issuedAtMs <= MAX_POLICY_MS
      && policy.notAfterMs === CLOSE_CALL_LOCK_MS
      && policy.expiresAtMs <= policy.notAfterMs, 'POLICY_LEASE');
    need(Number.isSafeInteger(policy.freshMs) && policy.freshMs > 0
      && policy.maxSignatures === 2, 'POLICY_LIMIT');
    need(policy.noValueMarker === 'EXPLICIT_PAPER_NO_VALUE', 'POLICY_NO_VALUE');
    return structuredClone(policy);
  }
  throw new PolicySignerError('POLICY_PROTOCOL');
}
export function policyDigest(policy) { return digest(validatePolicy(policy)); }

export function closeCallOwnerText(did) {
  need(typeof did === 'string' && did.startsWith('did:key:z6Mk'), 'WRONG_DID');
  return JSON.stringify({ t: 'owner', season: CLOSE_CALL_CONTEST, key: did });
}

export function validateCloseCallOwnerRequest(request, expectedDid) {
  exactKeys(request, ['version','kind','action','expectedDid','subject','binding','preview']);
  need(request.version === 1
    && request.kind === 'CLOSE_CALL_TYPED_SIGN_REQUEST'
    && request.action === CLOSE_CALL_ACTION
    && request.expectedDid === expectedDid, 'CLOSE_CALL_REQUEST_BINDING');
  exactKeys(request.subject, ['season','did']);
  need(request.subject.season === CLOSE_CALL_CONTEST
    && request.subject.did === expectedDid, 'CLOSE_CALL_REQUEST_BINDING');
  exactKeys(request.binding, ['contest','rulesCommit','packageManifestSha256','room','noncePolicy']);
  need(request.binding.contest === CLOSE_CALL_CONTEST
    && request.binding.rulesCommit === CLOSE_CALL_RULES_COMMIT
    && request.binding.packageManifestSha256 === CLOSE_CALL_PACKAGE_MANIFEST_SHA256
    && request.binding.room === CLOSE_CALL_ROOM
    && request.binding.noncePolicy === CLOSE_CALL_NONCE_POLICY, 'CLOSE_CALL_REQUEST_BINDING');
  exactKeys(request.preview, ['sha256','utf8']);
  const line = closeCallOwnerText(expectedDid);
  need(request.preview.utf8 === line && request.preview.sha256 === digest(line),
    'CLOSE_CALL_PREVIEW_MISMATCH');
  return { room: CLOSE_CALL_ROOM, line };
}

function decimalUnits(text, minimum = null) {
  need(typeof text === 'string' && AMOUNT_RE.test(text), 'CLOSE_CALL_TERMS_INVALID');
  const [whole, fraction = ''] = text.split('.');
  const value = BigInt(whole) * 100n + BigInt((fraction + '00').slice(0, 2));
  need(value > 0n && (minimum === null || value >= minimum),
    'CLOSE_CALL_TERMS_INVALID');
  return value;
}

function canonicalTerms(text) {
  need(typeof text === 'string' && text.length > 0 && text.length <= 4096,
    'CLOSE_CALL_TERMS_INVALID');
  let terms;
  try { terms = JSON.parse(text); } catch { throw new PolicySignerError('CLOSE_CALL_TERMS_INVALID'); }
  exactKeys(terms, ['id','maker','px','qty','side','taker','until']);
  need(typeof terms.id === 'string' && TRADE_ID_RE.test(terms.id)
    && typeof terms.maker === 'string' && DID_RE.test(terms.maker)
    && terms.side === 'sell'
    && (terms.taker === 'any' || (typeof terms.taker === 'string' && DID_RE.test(terms.taker)))
    && Number.isSafeInteger(terms.until) && terms.until >= 1
    && terms.until <= CLOSE_CALL_LOCK_SWEEP, 'CLOSE_CALL_TERMS_INVALID');
  decimalUnits(terms.px); decimalUnits(terms.qty, 10n);
  need(stable(terms) === text, 'CLOSE_CALL_TERMS_NOT_CANONICAL');
  return terms;
}

function exactTradeRequestOperation(operation, { expectedDid, terms, canonical, makerSig }) {
  exactKeys(operation, ['atomic','steps','takerPreimage','finalTrade','roomEnvelope']);
  need(operation.atomic === true
    && stable(operation.steps) === stable([
      'TAKER_COUNTERSIGN',
      'CONSTRUCT_FINAL_TRADE_JSON',
      'SIGN_CLOSE1_ROOM_MESSAGE',
    ]), 'CLOSE_CALL_REQUEST_BINDING');
  const accept = `${CLOSE_CALL_CONTEST}|accept|${canonical}|${expectedDid}`;
  exactKeys(operation.takerPreimage, ['sha256','utf8']);
  need(operation.takerPreimage.utf8 === accept
    && operation.takerPreimage.sha256 === digest(accept), 'CLOSE_CALL_REQUEST_BINDING');
  exactKeys(operation.finalTrade, ['t','season','terms','taker','maker_sig','taker_sig']);
  need(operation.finalTrade.t === 'trade'
    && operation.finalTrade.season === CLOSE_CALL_CONTEST
    && stable(operation.finalTrade.terms) === canonical
    && operation.finalTrade.taker === expectedDid
    && operation.finalTrade.maker_sig === makerSig
    && operation.finalTrade.taker_sig === 'SIGNER_GENERATES',
  'CLOSE_CALL_REQUEST_BINDING');
  exactKeys(operation.roomEnvelope, ['room','nonce','text','preimageFormat']);
  need(operation.roomEnvelope.room === CLOSE_CALL_ROOM
    && operation.roomEnvelope.nonce === 'SIGNER_ALLOCATES'
    && operation.roomEnvelope.text === 'SIGNER_CONSTRUCTS_EXACT_FINAL_TRADE_JSON'
    && operation.roomEnvelope.preimageFormat
      === 'close1|<SIGNER_ALLOCATED_NONCE>|<EXACT_FINAL_TRADE_JSON>',
  'CLOSE_CALL_REQUEST_BINDING');
  return accept;
}

export function validateCloseCallTakerLongRequest(request, expectedDid) {
  exactKeys(request, ['version','kind','action','expectedDid','subject','binding','operation']);
  need(request.version === 1
    && request.kind === 'CLOSE_CALL_TYPED_SIGN_REQUEST'
    && request.action === CLOSE_CALL_TAKER_LONG_ACTION
    && request.expectedDid === expectedDid, 'CLOSE_CALL_REQUEST_BINDING');
  exactKeys(request.subject, ['season','did','direction','makerSide']);
  need(request.subject.season === CLOSE_CALL_CONTEST
    && request.subject.did === expectedDid
    && request.subject.direction === 'LONG'
    && request.subject.makerSide === 'SELL', 'CLOSE_CALL_REQUEST_BINDING');
  exactKeys(request.binding, ['contest','rulesCommit','packageManifestSha256','room','noncePolicy','canonicalTerms','termsSha256','makerDid','makerSig','authenticatedPrice']);
  need(request.binding.contest === CLOSE_CALL_CONTEST
    && request.binding.rulesCommit === CLOSE_CALL_RULES_COMMIT
    && request.binding.packageManifestSha256 === CLOSE_CALL_PACKAGE_MANIFEST_SHA256
    && request.binding.room === CLOSE_CALL_ROOM
    && request.binding.noncePolicy === CLOSE_CALL_NONCE_POLICY,
  'CLOSE_CALL_REQUEST_BINDING');
  const terms = canonicalTerms(request.binding.canonicalTerms);
  need(request.binding.termsSha256 === digest(request.binding.canonicalTerms)
    && typeof request.binding.makerDid === 'string'
    && request.binding.makerDid === terms.maker
    && terms.maker !== expectedDid,
  'CLOSE_CALL_REQUEST_BINDING');
  need(terms.taker === 'any' || terms.taker === expectedDid, 'CLOSE_CALL_REQUEST_BINDING');
  need(typeof request.binding.makerSig === 'string' && SIG_RE.test(request.binding.makerSig)
    && verifyDidSignature(
      terms.maker,
      `${CLOSE_CALL_CONTEST}|terms|${request.binding.canonicalTerms}`,
      request.binding.makerSig,
    ), 'CLOSE_CALL_MAKER_SIGNATURE_INVALID');
  const price = request.binding.authenticatedPrice;
  exactKeys(price, ['refereeDid','sweep','ref','limits','referenceAgeSeconds','stale','recordSha256']);
  exactKeys(price.ref, ['px','time','tid']);
  need(typeof price.refereeDid === 'string' && DID_RE.test(price.refereeDid)
    && Number.isSafeInteger(price.sweep) && price.sweep >= 1
    && price.sweep <= CLOSE_CALL_LOCK_SWEEP
    && typeof price.ref.time === 'string' && Number.isSafeInteger(Date.parse(price.ref.time))
    && (typeof price.ref.tid === 'string' || Number.isSafeInteger(price.ref.tid))
    && Array.isArray(price.limits) && price.limits.length === 2
    && typeof price.referenceAgeSeconds === 'string'
    && /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(price.referenceAgeSeconds)
    && typeof price.stale === 'boolean'
    && typeof price.recordSha256 === 'string' && HEX64_RE.test(price.recordSha256),
  'CLOSE_CALL_PRICE_BINDING');
  decimalUnits(price.ref.px); decimalUnits(price.limits[0]); decimalUnits(price.limits[1]);
  const accept = exactTradeRequestOperation(request.operation, {
    expectedDid, terms, canonical: request.binding.canonicalTerms,
    makerSig: request.binding.makerSig,
  });
  return {
    terms,
    canonicalTerms: request.binding.canonicalTerms,
    makerSig: request.binding.makerSig,
    accept,
    boundPrice: structuredClone(price),
  };
}

function closeCallPriceRecord(record) {
  need(record && typeof record === 'object' && !Array.isArray(record)
    && record.t === 'price'
    && Number.isSafeInteger(record.n) && record.n >= 1
    && record.n <= CLOSE_CALL_LOCK_SWEEP, 'CLOSE_CALL_PRICE_INVALID');
  exactKeys(record.ref, ['px','time','tid']);
  need(typeof record.ref.time === 'string'
    && Number.isSafeInteger(Date.parse(record.ref.time))
    && ((typeof record.ref.tid === 'string' && record.ref.tid.length > 0)
      || Number.isSafeInteger(record.ref.tid))
    && Array.isArray(record.limits) && record.limits.length === 2,
  'CLOSE_CALL_PRICE_INVALID');
  const ref = decimalUnits(record.ref.px);
  const low = decimalUnits(record.limits[0]);
  const high = decimalUnits(record.limits[1]);
  need(low <= ref && ref <= high, 'CLOSE_CALL_PRICE_INVALID');
  return { record, sweep: record.n, ref, low, high, recordSha256: digest(record) };
}

function currentCloseCallPrice(snapshot, refereeDid) {
  need(snapshot.generation > 0, 'GENERATION_MISMATCH');
  const posts = [];
  for (const signed of snapshot.records) {
    let record;
    try { record = JSON.parse(signed.line); } catch { continue; }
    if (!record || typeof record !== 'object' || Array.isArray(record)
      || record.t !== 'price') continue;
    if (signed.sender !== refereeDid) continue;
    posts.push(closeCallPriceRecord(record));
  }
  need(posts.length > 0, 'CLOSE_CALL_PRICE_UNAVAILABLE');
  const latestSweep = Math.max(...posts.map(post => post.sweep));
  const latest = posts.filter(post => post.sweep === latestSweep);
  need(new Set(latest.map(post => post.recordSha256)).size === 1,
    'CLOSE_CALL_PRICE_AMBIGUOUS');
  return latest.at(-1);
}

function solveSource(sourceBytes) {
  need(Buffer.isBuffer(sourceBytes) && sourceBytes.length <= 32, 'SOURCE_REQUIRED');
  const source = sourceBytes.toString('utf8');
  need(Buffer.from(source, 'utf8').equals(sourceBytes), 'SOURCE_INVALID');
  const match = SOURCE_RE.exec(source);
  need(match, 'SOURCE_INVALID');
  const a = BigInt(match[1]), b = BigInt(match[2]);
  let x = a, y = b;
  while (y) [x, y] = [y, x % y];
  return { sourceDigest: digest(sourceBytes), answer: `gcd=${x} lcm=${a / x * b}` };
}

function emptyState(did, pDigest) {
  return { version: 1, did, policyDigest: pDigest, revision: 0, signaturesUsed: 0, lastNonceByRoom: {}, work: null, actions: {} };
}

export class PolicySigner {
  #acquisition; #stateKey; #signer; #policy; #policyDigest; #statePath; #initialize; #now; #rng; #state;
  constructor({ seed, policy, statePath, acquisition, initialize = false, now = Date.now, rng = randomBytes }) {
    const seedCopy = Buffer.from(seed ?? []); need(seedCopy.length === 32, 'SEED_LENGTH');
    try {
      this.#signer = signerFromSeed(seedCopy);
      this.#stateKey = stateKey(seedCopy);
    } finally {
      seedCopy.fill(0);
    }
    this.#policy = validatePolicy(policy);
    need(this.#signer.did === this.#policy.expectedDid, 'DID_MISMATCH');
    this.#policyDigest = policyDigest(this.#policy);
    this.#statePath = statePath;
    need(typeof acquisition?.export === 'function' && typeof acquisition?.paperNote === 'function', 'TRUSTED_ACQUISITION_REQUIRED');
    this.#acquisition = acquisition;
    this.#initialize = initialize;
    this.#now = now;
    this.#rng = rng;
    this.#state = this.#load();
    this.#validateState();
  }

  #load() {
    if (!existsSync(this.#statePath)) {
      need(this.#initialize === true, 'STATE_MISSING');
      const initial = emptyState(this.#signer.did, this.#policyDigest);
      atomicWrite(this.#statePath, Buffer.from(stable(encryptState(this.#stateKey, this.#signer.did, this.#policyDigest, initial, this.#rng)) + '\n'));
      return initial;
    }
    let envelope;
    try { envelope = JSON.parse(readFileSync(this.#statePath, 'utf8')); } catch { throw new PolicySignerError('STATE_CORRUPT'); }
    return decryptState(this.#stateKey, this.#signer.did, this.#policyDigest, envelope);
  }
  #save() {
    need(existsSync(this.#statePath), 'STATE_MISSING');
    this.#state.revision += 1;
    atomicWrite(this.#statePath, Buffer.from(stable(encryptState(this.#stateKey, this.#signer.did, this.#policyDigest, this.#state, this.#rng)) + '\n'));
  }
  #validateState() {
    need(this.#state?.version === 1 && this.#state.did === this.#signer.did && this.#state.policyDigest === this.#policyDigest, 'STATE_BINDING');
    need(Number.isSafeInteger(this.#state.revision) && this.#state.revision >= 0 && Number.isSafeInteger(this.#state.signaturesUsed) && this.#state.signaturesUsed >= 0 && this.#state.signaturesUsed <= this.#policy.maxSignatures, 'STATE_CORRUPT');
    need(this.#state.lastNonceByRoom && typeof this.#state.lastNonceByRoom === 'object' && this.#state.actions && typeof this.#state.actions === 'object', 'STATE_CORRUPT');
    if (this.#state.work) {
      const w = this.#state.work;
      need(Number.isSafeInteger(w.offersGeneration) && w.offersGeneration > 0
        && (w.dealGeneration === null || (Number.isSafeInteger(w.dealGeneration) && w.dealGeneration > 0)), 'STATE_CORRUPT');
    }
    if (this.#policy.protocol === CLOSE_CALL_TAKER_LONG_POLICY_PROTOCOL) {
      need(this.#state.work === null, 'STATE_CORRUPT');
      const keys = Object.keys(this.#state.actions);
      need(keys.length <= 1 && keys.every(key => key === CLOSE_CALL_TAKER_LONG_ACTION),
        'STATE_CORRUPT');
      const action = this.#state.actions[CLOSE_CALL_TAKER_LONG_ACTION];
      need(this.#state.signaturesUsed === (action ? 2 : 0), 'STATE_CORRUPT');
      if (action) {
        exactKeys(action, ['type','status','requestDigest','nonce','takerPayloadDigest','finalTradeDigest','payloadDigest','envelopeDigest']);
        need(action.type === CLOSE_CALL_TAKER_LONG_ACTION
          && ['RESERVED','ATTEMPTED'].includes(action.status)
          && HEX64_RE.test(action.requestDigest)
          && /^(?:0|[1-9][0-9]{0,18})$/.test(action.nonce), 'STATE_CORRUPT');
        const digests = [
          action.takerPayloadDigest, action.finalTradeDigest,
          action.payloadDigest, action.envelopeDigest,
        ];
        need(action.status === 'RESERVED'
          ? digests.every(value => value === null)
          : digests.every(value => typeof value === 'string' && HEX64_RE.test(value)),
        'STATE_CORRUPT');
      }
    }
  }
  #policyLive() {
    const n = this.#now();
    need(n >= this.#policy.issuedAtMs && n < this.#policy.expiresAtMs, 'POLICY_EXPIRED');
    if ([CLOSE_CALL_POLICY_PROTOCOL, CLOSE_CALL_TAKER_LONG_POLICY_PROTOCOL]
      .includes(this.#policy.protocol))
      need(n < this.#policy.notAfterMs, 'CLOSE_CALL_LOCKED');
    return n;
  }
  #preSignRevalidate(type, offer) {
    this.#policyLive();
    if (type === 'ACCEPT') this.#completionWindow(offer);
    else need(this.#now() + this.#policy.claimMarginMs < offer.claimByMs, 'CLAIM_DEADLINE');
  }
  #snapshot(room) {
    const current = this.#now();
    const checked = this.#verifiedExport(room, this.#policy.freshMs, current);
    return this.#pinGeneration(room, checked);
  }
  #pinGeneration(room, checked) {
    const w = this.#state.work;
    if (!w) return checked;
    if (room === OFFER_ROOM) need(checked.generation === w.offersGeneration, 'GENERATION_MISMATCH');
    else if (room === w.room) {
      if (w.dealGeneration === null) {
        if (checked.generation === 0) need(checked.records.length === 0, 'GENERATION_MISMATCH');
        else { w.dealGeneration = checked.generation; this.#save(); }
      } else need(checked.generation === w.dealGeneration, 'GENERATION_MISMATCH');
    }
    return checked;
  }
  #completionWindow(offer) {
    const n = this.#now();
    need(n + this.#policy.acceptMarginMs < offer.expiresMs
      && n + this.#policy.acceptMarginMs + this.#policy.minCompletionWindowMs + this.#policy.claimMarginMs
        < Math.min(offer.claimByMs, this.#policy.expiresAtMs)
      && offer.claimByMs + this.#policy.refundGapMs <= offer.refundAfterMs, 'DEADLINE_POLICY');
  }
  #verifiedExport(room, maxAgeMs, nowMs) {
    const exportInput = this.#acquisition.export(room);
    exactKeys(exportInput, ['room','generation','body','capturedAtMs']);
    need(exportInput.room === room, 'SNAPSHOT_BINDING');
    need(Buffer.isBuffer(exportInput.body), 'RAW_EXPORT_REQUIRED');
    const checked = normalizeExportSnapshot({ ...exportInput, expectedDid: this.#signer.did });
    return validateSnapshot(checked, { room, expectedDid: this.#signer.did, maxAgeMs, nowMs });
  }
  #nextNonce(room, snapshot) {
    const observed = snapshot.nonceObservation.observedNonce;
    let value = BigInt(this.#now());
    const local = this.#state.lastNonceByRoom[room];
    if (local !== undefined && BigInt(local) >= value) value = BigInt(local) + 1n;
    if (observed !== null && BigInt(observed) >= value) value = BigInt(observed) + 1n;
    need(value <= 9_999_999_999_999_999_999n, 'NONCE_EXHAUSTED');
    const result = value.toString(); this.#state.lastNonceByRoom[room] = result; return result;
  }
  #recordDigest(record) { return digest({ room:record.room, sender:record.sender, nonce:record.nonce, signature:record.signature, line:record.line }); }
  #findOffer(snapshot, seq) {
    const records = snapshot.records.filter(r => r.seq === seq);
    need(records.length === 1, 'OFFER_NOT_UNIQUE');
    const record = records[0]; need(record.room === OFFER_ROOM, 'WRONG_ROOM');
    const offer = validateOffer(decodeFrame(record.line));
    need(offer.from === record.sender, 'OFFER_SENDER_MISMATCH');
    need(!this.#earlierOffer(snapshot, seq, offer.id), 'FROZEN_OFFER_UNAVAILABLE');
    return { record, offer };
  }
  #earlierOffer(snapshot, seq, id) {
    return snapshot.records.some(r => {
      if (r.seq >= seq) return false;
      try { const f = decodeFrame(r.line); return f.type === 'offer' && f.id === id && f.from === r.sender; }
      catch { return false; }
    });
  }

  admit(request) {
    need(this.#policy.protocol === TCLK_VERSION, 'POLICY_CAPABILITY');
    exactKeys(request, ['offerSeq','sourceBytes']);
    const { offerSeq, sourceBytes } = request;
    this.#policyLive(); need(this.#state.work === null, 'WORK_ALREADY_ADMITTED');
    const checked = this.#snapshot(OFFER_ROOM);
    need(checked.generation > 0, 'GENERATION_MISMATCH');
    need(Number.isSafeInteger(offerSeq) && offerSeq > 0, 'TASK_BINDING');
    const { sourceDigest, answer } = solveSource(sourceBytes);
    const { record, offer } = this.#findOffer(checked, offerSeq);
    need(offer.role === 'payer' && offer.from !== this.#signer.did && offer.lock === 'hash' && offer.asset === 'PAPER'
      && JSON.stringify(offer.rails) === '["paper"]' && offer.paymentKey === undefined, 'PAPER_ONLY');
    need(offer.job?.proto === this.#policy.jobProto && offer.job.context === this.#policy.jobContextPrefix + sourceDigest, 'TASK_BINDING');
    this.#completionWindow(offer);
    const lock = hashLockFromPreimage(this.#rng(32));
    const accept = makeAccept(offer, { from: this.#signer.did, statement: lock.hash, nonce: this.#rng(8).toString('hex') });
    this.#state.work = {
      sourceDigest, answer, offerRecord: record, offer, offerDigest: this.#recordDigest(record),
      accept, acceptLine: encodeFrame(accept), preimage: lock.preimage, contract: accept.contract,
      room: dealRoom(accept.contract), offersGeneration: checked.generation, dealGeneration: null,
      status: 'ADMITTED', heartbeatRequired: this.#policy.requireHeartbeat,
    };
    this.#save();
    return { did: this.#signer.did, offer: offer.id, contract: accept.contract, dealRoom: this.#state.work.room, heartbeatRequired: this.#policy.requireHeartbeat };
  }

  #finalRevalidateAccept() {
    const checked = this.#snapshot(OFFER_ROOM); const w = this.#state.work; need(w, 'NO_WORK');
    this.#frozenOffer(checked);
    need(this.#firstValidAccept(checked) === null, 'ACCEPT_CONFLICT');
    this.#completionWindow(w.offer);
    return checked;
  }
  #frozenOffer(checked) {
    const w = this.#state.work;
    const exact = checked.records.filter(r => r.seq === w.offerRecord.seq && this.#recordDigest(r) === w.offerDigest);
    need(exact.length === 1, 'FROZEN_OFFER_UNAVAILABLE');
    const frame = validateOffer(decodeFrame(exact[0].line));
    need(exact[0].sender === frame.from && frame.id === w.offer.id, 'FROZEN_OFFER_UNAVAILABLE');
    need(!this.#earlierOffer(checked, exact[0].seq, frame.id), 'FROZEN_OFFER_UNAVAILABLE');
    return exact[0];
  }
  #firstValidAccept(checked) {
    const w = this.#state.work;
    for (const record of checked.records) {
      if (record.seq <= w.offerRecord.seq) continue;
      let frame;
      try { frame = decodeFrame(record.line); } catch { continue; }
      if (frame.type === 'cancel' && frame.from === record.sender && frame.from === w.offer.from)
        return { cancelled: true };
      if (frame.type !== 'accept' || frame.ref !== w.offer.id || frame.from !== record.sender
        || frame.from === w.offer.from || record.timestampMs >= w.offer.expiresMs) continue;
      if (contractId(w.offer, { from: frame.from, ref: frame.ref, statement: frame.statement,
        paymentKey: frame.paymentKey, nonce: frame.nonce }) !== frame.contract) continue;
      return { record, frame };
    }
    return null;
  }

  #lockEvidence() {
    const w = this.#state.work; need(w, 'NO_WORK'); const checked = this.#snapshot(w.room);
    const accepted = this.#state.actions.ACCEPT;
    need(accepted?.status === 'OBSERVED' && Number.isSafeInteger(accepted.observedTimestampMs), 'ACCEPT_NOT_OBSERVED');
    let locked = false;
    for (const r of checked.records) {
      let f;
      try {
        if (!r.line.startsWith('tclk1 ')) continue;
        f = JSON.parse(r.line.slice(6));
      } catch { continue; }
      if (!f || typeof f !== 'object' || Array.isArray(f) || f.from !== r.sender
        || f.contract !== w.contract) continue;
      if (f.type === 'cancel' && !locked && (f.from === w.offer.from || f.from === this.#signer.did))
        need(false, 'PAPER_LOCK_REQUIRED');
      if (f.type !== 'lock' || f.from !== w.offer.from) continue;
      if (locked) continue; // The official machine ignores later LOCKs after the first transition.
      need(r.timestampMs >= accepted.observedTimestampMs && r.timestampMs < w.offer.refundAfterMs
        && Object.keys(f).sort().join(',') === 'contract,from,rail,ref,type'
        && f.rail === 'paper' && f.ref === w.contract, 'PAPER_LOCK_REQUIRED');
      locked = true;
    }
    need(locked, 'PAPER_LOCK_REQUIRED');
    const paper = decodePaperRecord(this.#acquisition.paperNote(w.contract));
    need(paper?.status === 'locked' && paper.statement === w.accept.statement && paper.refundAfterMs === w.offer.refundAfterMs, 'PAPER_LOCK_REQUIRED');
    return checked;
  }

  issue(type, request = {}) {
    need(this.#policy.protocol === TCLK_VERSION, 'POLICY_CAPABILITY');
    need(ACTIONS.has(type), 'UNSUPPORTED_ACTION'); this.#policyLive(); const w = this.#state.work; need(w, 'NO_WORK');
    need(!this.#state.actions[type], 'DUPLICATE_ACTION'); need(this.#state.signaturesUsed < this.#policy.maxSignatures, 'SIGNATURE_QUOTA');
    if (type !== 'ACCEPT') need(this.#now() + this.#policy.claimMarginMs < w.offer.claimByMs, 'CLAIM_DEADLINE');
    let room, line, snapshot;
    if (type === 'ACCEPT') {
      exactKeys(request, []);
      snapshot = this.#finalRevalidateAccept(); room = OFFER_ROOM; line = w.acceptLine;
    } else if (type === 'INITIAL_HEARTBEAT') {
      exactKeys(request, []); need(w.heartbeatRequired, 'HEARTBEAT_NOT_REQUIRED');
      snapshot = this.#snapshot(w.room); need(this.#state.actions.ACCEPT?.status === 'OBSERVED', 'ACCEPT_NOT_OBSERVED');
      room = w.room; line = encodeFrame(makeHeartbeat({ from: this.#signer.did, contract: w.contract, nonce: this.#rng(8).toString('hex') }));
    } else if (type === 'DELIVERY_GCD') {
      exactKeys(request, []);
      need(this.#state.actions.ACCEPT?.status === 'OBSERVED', 'ACCEPT_NOT_OBSERVED');
      if (w.heartbeatRequired) need(this.#state.actions.INITIAL_HEARTBEAT?.status === 'OBSERVED', 'HEARTBEAT_REQUIRED');
      snapshot = this.#lockEvidence(); room = w.room; line = w.answer;
    } else {
      exactKeys(request, []); need(this.#state.actions.DELIVERY_GCD?.status === 'OBSERVED', 'DELIVERY_NOT_OBSERVED');
      snapshot = this.#lockEvidence(); const n = this.#now(); need(n + this.#policy.claimMarginMs < w.offer.claimByMs, 'CLAIM_DEADLINE');
      room = w.room; line = encodeFrame(makeReveal({ from: this.#signer.did, contract: w.contract, ref: w.contract, secret: w.preimage }));
    }
    line = sweep(line); need(line.length > 0 && line.length <= 4096, 'INVALID_LINE');
    // External acquisition may block long enough for the bounded policy lease or
    // claim window to expire. Revalidate immediately before nonce allocation/signing.
    this.#preSignRevalidate(type, w.offer);
    const nonce = this.#nextNonce(room, snapshot); const canonical = canonicalMessage(room, nonce, line); const signature = this.#signer.sign(canonical);
    const record = { room, seq: 0, timestampMs: this.#now(), sender: this.#signer.did, nonce, signature, line };
    const action = { type, status: 'ATTEMPTED', envelopeDigest: this.#recordDigest(record), payloadDigest: digest(canonical), requestDigest: digest(request) };
    this.#state.actions[type] = action; this.#state.signaturesUsed += 1; this.#save();
    return structuredClone(record);
  }

  reconcile(type) {
    need(this.#policy.protocol === TCLK_VERSION, 'POLICY_CAPABILITY');
    need(ACTIONS.has(type), 'UNSUPPORTED_ACTION'); const action = this.#state.actions[type]; need(action, 'UNKNOWN_ACTION');
    need(['ATTEMPTED','AMBIGUOUS'].includes(action.status), 'RECONCILE_STATE');
    const w = this.#state.work; const room = type === 'ACCEPT' ? OFFER_ROOM : w.room;
    const checked = this.#snapshotForReconciliation(room);
    if (type === 'ACCEPT') {
      this.#frozenOffer(checked);
      const first = this.#firstValidAccept(checked);
      if (!first || first.cancelled || this.#recordDigest(first.record) !== action.envelopeDigest
        || first.frame.contract !== w.contract || first.frame.statement !== w.accept.statement
        || first.frame.from !== this.#signer.did) {
        action.status = 'AMBIGUOUS'; this.#save(); return { status: 'AMBIGUOUS', retry: false };
      }
      action.status = 'OBSERVED'; action.observedSeq = first.record.seq;
      action.observedTimestampMs = first.record.timestampMs;
      this.#save(); return { status: 'OBSERVED', retry: false, seq: first.record.seq };
    }
    const predecessor = type === 'ACCEPT' ? w.offerRecord.seq
      : type === 'INITIAL_HEARTBEAT' ? 0
      : type === 'DELIVERY_GCD' ? (w.heartbeatRequired ? this.#state.actions.INITIAL_HEARTBEAT?.observedSeq : 0)
      : this.#state.actions.DELIVERY_GCD?.observedSeq;
    need(Number.isSafeInteger(predecessor), 'RECONCILE_ORDER');
    const matches = checked.records.filter(r => r.seq > predecessor && this.#recordDigest(r) === action.envelopeDigest);
    if (matches.length !== 1) { action.status = 'AMBIGUOUS'; this.#save(); return { status: 'AMBIGUOUS', retry: false }; }
    action.status = 'OBSERVED'; action.observedSeq = matches[0].seq; this.#save(); return { status: 'OBSERVED', retry: false, seq: matches[0].seq };
  }
  #snapshotForReconciliation(room) {
    const now = this.#now();
    const checked = this.#verifiedExport(room, Math.max(this.#policy.freshMs, 5 * 60 * 1000), now);
    return this.#pinGeneration(room, checked);
  }

  closeCallOwnerRegister(request) {
    need(this.#policy.protocol === CLOSE_CALL_POLICY_PROTOCOL, 'POLICY_CAPABILITY');
    this.#policyLive();
    need(this.#state.work === null, 'WORK_ALREADY_ADMITTED');
    need(!this.#state.actions[CLOSE_CALL_ACTION], 'DUPLICATE_ACTION');
    need(this.#state.signaturesUsed < this.#policy.maxSignatures, 'SIGNATURE_QUOTA');
    const { room, line } = validateCloseCallOwnerRequest(request, this.#signer.did);
    const snapshot = this.#snapshot(room);
    need(snapshot.generation > 0, 'GENERATION_MISMATCH');
    need(!snapshot.records.some(record =>
      record.sender === this.#signer.did && record.line === line),
    'CLOSE_CALL_REGISTRATION_ALREADY_OBSERVED');

    // Revalidate after acquisition immediately before nonce allocation/signing.
    this.#policyLive();
    const nonce = this.#nextNonce(room, snapshot);
    const canonical = canonicalMessage(room, nonce, line);
    const signature = this.#signer.sign(canonical);
    const record = {
      room, seq: 0, timestampMs: this.#now(), sender: this.#signer.did,
      nonce, signature, line,
    };
    this.#state.actions[CLOSE_CALL_ACTION] = {
      type: CLOSE_CALL_ACTION,
      status: 'ATTEMPTED',
      envelopeDigest: this.#recordDigest(record),
      payloadDigest: digest(canonical),
      requestDigest: digest(request),
    };
    this.#state.signaturesUsed += 1;
    this.#save();
    return structuredClone(record);
  }

  closeCallTakerLong(request) {
    need(this.#policy.protocol === CLOSE_CALL_TAKER_LONG_POLICY_PROTOCOL,
      'POLICY_CAPABILITY');
    this.#policyLive();
    need(this.#state.work === null, 'WORK_ALREADY_ADMITTED');
    need(!this.#state.actions[CLOSE_CALL_TAKER_LONG_ACTION], 'DUPLICATE_ACTION');
    need(this.#state.signaturesUsed + 2 <= this.#policy.maxSignatures,
      'SIGNATURE_QUOTA');
    const validated = validateCloseCallTakerLongRequest(request, this.#signer.did);
    need(validated.boundPrice.refereeDid === this.#policy.refereeDid,
      'CLOSE_CALL_PRICE_REFEREE');

    const priceSnapshot = this.#snapshot(CLOSE_CALL_PRICE_ROOM);
    const current = currentCloseCallPrice(priceSnapshot, this.#policy.refereeDid);
    const bound = validated.boundPrice;
    need(bound.sweep === current.sweep
      && stable(bound.ref) === stable(current.record.ref)
      && stable(bound.limits) === stable(current.record.limits)
      && bound.recordSha256 === current.recordSha256,
    'CLOSE_CALL_REPLAN_REQUIRED');

    const px = decimalUnits(validated.terms.px);
    need(px >= current.low && px <= current.high, 'CLOSE_CALL_PRICE_LIMITS');
    need(current.sweep < CLOSE_CALL_LOCK_SWEEP, 'CLOSE_CALL_LOCKED');
    need(validated.terms.until >= current.sweep + 1, 'CLOSE_CALL_UNTIL');
    const refTimeMs = Date.parse(current.record.ref.time);
    const referenceAgeMs = this.#now() - refTimeMs;
    need(referenceAgeMs >= 0, 'CLOSE_CALL_PRICE_INVALID');
    const referenceAgeSeconds = String(referenceAgeMs / 1000);
    const stale = referenceAgeMs > CLOSE_CALL_EXPECTED_SWEEP_SECONDS * 1000;

    const roomSnapshot = this.#snapshot(CLOSE_CALL_ROOM);
    need(roomSnapshot.generation > 0, 'GENERATION_MISMATCH');
    // Revalidate after both trusted reads. Reserve nonce, replay marker and both
    // signature units durably before generating either cryptographic signature.
    this.#policyLive();
    const nonce = this.#nextNonce(CLOSE_CALL_ROOM, roomSnapshot);
    const action = {
      type: CLOSE_CALL_TAKER_LONG_ACTION,
      status: 'RESERVED',
      requestDigest: digest(request),
      nonce,
      takerPayloadDigest: null,
      finalTradeDigest: null,
      payloadDigest: null,
      envelopeDigest: null,
    };
    this.#state.actions[CLOSE_CALL_TAKER_LONG_ACTION] = action;
    this.#state.signaturesUsed += 2;
    this.#save();

    const takerSignature = this.#signer.sign(validated.accept);
    const tradeObject = {
      t: 'trade',
      season: CLOSE_CALL_CONTEST,
      terms: validated.terms,
      taker: this.#signer.did,
      maker_sig: validated.makerSig,
      taker_sig: takerSignature,
    };
    const line = JSON.stringify(tradeObject);
    need(line.length <= 4096, 'INVALID_LINE');
    const roomPreimage = canonicalMessage(CLOSE_CALL_ROOM, nonce, line);
    const signature = this.#signer.sign(roomPreimage);
    const record = {
      room: CLOSE_CALL_ROOM,
      nonce,
      sender: this.#signer.did,
      signature,
      line,
    };
    action.status = 'ATTEMPTED';
    action.takerPayloadDigest = digest(validated.accept);
    action.finalTradeDigest = digest(line);
    action.payloadDigest = digest(roomPreimage);
    action.envelopeDigest = this.#recordDigest({
      ...record, seq: 0, timestampMs: this.#now(),
    });
    this.#save();
    return {
      action: CLOSE_CALL_TAKER_LONG_ACTION,
      status: 'ATTEMPTED',
      retry: false,
      takerSignature,
      price: {
        sweep: current.sweep,
        referenceAgeSeconds,
        stale,
      },
      trade: {
        object: structuredClone(tradeObject),
        text: line,
        digest: action.finalTradeDigest,
      },
      roomRecord: structuredClone(record),
    };
  }

  observeCompletion() {
    need(this.#policy.protocol === TCLK_VERSION, 'POLICY_CAPABILITY');
    const w = this.#state.work; need(w, 'NO_WORK'); need(this.#state.actions.REVEAL?.status === 'OBSERVED', 'REVEAL_NOT_OBSERVED');
    const paper = decodePaperRecord(this.#acquisition.paperNote(w.contract));
    need(paper?.status === 'claimed' && paper.statement === w.accept.statement && paper.refundAfterMs === w.offer.refundAfterMs && verifyHashPreimage(w.accept.statement, paper.secret) && paper.secret === w.preimage, 'COMPLETION_NOT_CONFIRMED');
    w.status = 'COMPLETED'; this.#save(); return { status: 'COMPLETED', contract: w.contract };
  }

  status() {
    return {
      did: this.#signer.did,
      policyDigest: this.#policyDigest,
      revision: this.#state.revision,
      signaturesUsed: this.#state.signaturesUsed,
      work: this.#state.work ? { offer: this.#state.work.offer.id, contract: this.#state.work.contract, room: this.#state.work.room, status: this.#state.work.status } : null,
      actions: Object.fromEntries(Object.entries(this.#state.actions).map(([k,v]) => [k,{
        status:v.status,
        payloadDigest:v.payloadDigest,
        envelopeDigest:v.envelopeDigest,
        ...(v.finalTradeDigest ? { finalTradeDigest:v.finalTradeDigest } : {}),
      }])),
    };
  }
}
