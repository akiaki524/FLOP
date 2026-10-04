import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { canonicalMessage, rawPublicKeyFromSeed, signerFromSeed, verifyDidSignature } from '../src/technocore_signing.mjs';
import {
  OFFER_ROOM,
  TCLK_COMMIT,
  contractId,
  decodeFrame,
  encodeFrame,
  encodePaperRecord,
  hashLockFromPreimage,
  makeAccept,
  makeOffer,
} from '../src/tclk_v1.mjs';
import { normalizeExportSnapshot } from '../src/technocore_read.mjs';
import {
  CLOSE_CALL_ACTION,
  CLOSE_CALL_CONTEST,
  CLOSE_CALL_LOCK_MS,
  CLOSE_CALL_NONCE_POLICY,
  CLOSE_CALL_PACKAGE_MANIFEST_SHA256,
  CLOSE_CALL_POLICY_PROTOCOL,
  CLOSE_CALL_ROOM,
  CLOSE_CALL_RULES_COMMIT,
  PolicySigner,
  closeCallOwnerText,
  policyDigest,
  validateCloseCallOwnerRequest,
} from '../src/policy_signer.mjs';

const BASE = 1_800_000_000_000;
const CLOSE_CALL_BASE = CLOSE_CALL_LOCK_MS - 2 * 60 * 60 * 1000;
const PROJECT_DID = 'did:key:z6MkjQVeF7TCAC6H6LKQnYFannWhsfWzzHhDDr3UhbZgHSTL';
const SOURCE_BYTES = Buffer.from('gcd_lcm 6 42\n');
const SOURCE = createHash('sha256').update(SOURCE_BYTES).digest('hex');
const ANSWER = 'gcd=6 lcm=42';
const PREFIX = 'TEST GCD bundle sha256=';
const PAYEE_SEED = Buffer.from('11'.repeat(32), 'hex');
const PAYER_SEED = Buffer.from('22'.repeat(32), 'hex');
const OTHER_SEED = Buffer.from('33'.repeat(32), 'hex');
const READS = new WeakMap();
function testSigner(options) {
  const signer = new PolicySigner(options);
  READS.set(signer, options.acquisition);
  return signer;
}

function storedLine(room, seq, tsMs, signer, line, nonce) {
  const signature = signer.sign(canonicalMessage(room, nonce, line));
  const row = { seq, ts: new Date(tsMs).toISOString(), from: signer.did, text: line, nonce: String(nonce), sig: signature };
  return JSON.stringify(row).replace(`"nonce":"${nonce}"`, `"nonce":${nonce}`) + '\n';
}
function raw(...lines) { return Buffer.from(lines.join(''), 'utf8'); }
function snapshot(room, generation, body, capturedAtMs, expectedDid) {
  return { room, generation, body, capturedAtMs };
}
function acquisition(initial) {
  const exports = new Map(initial ? [[initial.room, initial]] : []);
  let note = null;
  return {
    export: room => exports.get(room), paperNote: () => note,
    use: value => exports.set(value.room, value), setNote: value => { note = value; },
  };
}
function admit(signer, snapshot, offerSeq = 1) {
  READS.get(signer).use(snapshot);
  return signer.admit({ offerSeq, sourceBytes: SOURCE_BYTES });
}
function policy(did, overrides = {}) {
  return {
    version: 1,
    protocol: 'tclk/1',
    tclkCommit: TCLK_COMMIT,
    expectedDid: did,
    workFamily: 'math.gcd_lcm',
    jobProto: 'a2a',
    jobContextPrefix: PREFIX,
    issuedAtMs: BASE - 1000,
    expiresAtMs: BASE + 60 * 60 * 1000,
    freshMs: 30_000,
    acceptMarginMs: 60_000,
    minCompletionWindowMs: 60_000,
    claimMarginMs: 120_000,
    refundGapMs: 60_000,
    maxWorkItems: 1,
    maxSignatures: 4,
    requireHeartbeat: true,
    noValueMarker: 'EXPLICIT_PAPER_NO_VALUE',
    ...overrides,
  };
}
function closeCallPolicy(did, overrides = {}) {
  return {
    version: 1,
    protocol: CLOSE_CALL_POLICY_PROTOCOL,
    expectedDid: did,
    contest: CLOSE_CALL_CONTEST,
    rulesCommit: CLOSE_CALL_RULES_COMMIT,
    packageManifestSha256: CLOSE_CALL_PACKAGE_MANIFEST_SHA256,
    room: CLOSE_CALL_ROOM,
    noncePolicy: CLOSE_CALL_NONCE_POLICY,
    issuedAtMs: CLOSE_CALL_BASE - 1000,
    expiresAtMs: CLOSE_CALL_BASE + 60 * 60 * 1000,
    notAfterMs: CLOSE_CALL_LOCK_MS,
    freshMs: 30_000,
    maxSignatures: 1,
    noValueMarker: 'EXPLICIT_PAPER_NO_VALUE',
    ...overrides,
  };
}
function closeCallRequest(did) {
  const text = closeCallOwnerText(did);
  return {
    version: 1,
    kind: 'CLOSE_CALL_TYPED_SIGN_REQUEST',
    action: CLOSE_CALL_ACTION,
    expectedDid: did,
    subject: { season: CLOSE_CALL_CONTEST, did },
    binding: {
      contest: CLOSE_CALL_CONTEST,
      rulesCommit: CLOSE_CALL_RULES_COMMIT,
      packageManifestSha256: CLOSE_CALL_PACKAGE_MANIFEST_SHA256,
      room: CLOSE_CALL_ROOM,
      noncePolicy: CLOSE_CALL_NONCE_POLICY,
    },
    preview: {
      sha256: createHash('sha256').update(text, 'utf8').digest('hex'),
      utf8: text,
    },
  };
}
function fixture(overrides = {}) {
  const payee = signerFromSeed(PAYEE_SEED), payer = signerFromSeed(PAYER_SEED);
  const offer = makeOffer({
    from: payer.did, role: 'payer', amount: '1', asset: 'PAPER', lock: 'hash', rails: ['paper'],
    claimByMs: BASE + 30 * 60 * 1000, refundAfterMs: BASE + 40 * 60 * 1000, expiresMs: BASE + 10 * 60 * 1000,
    job: { proto: 'a2a', id: 'TEST-gcd', context: PREFIX + SOURCE },
  }, '0011223344556677');
  const offerLine = encodeFrame(offer);
  const offerStored = storedLine(OFFER_ROOM, 1, BASE - 500, payer, offerLine, '1799999999999999999');
  const roomSnapshot = snapshot(OFFER_ROOM, 1, raw(offerStored), BASE, payee.did);
  const dir = mkdtempSync(join(tmpdir(), 'policy-signer-'));
  let nowValue = BASE;
  const read = acquisition(roomSnapshot);
  const signer = testSigner({ seed: PAYEE_SEED, policy: policy(payee.did, overrides), statePath: join(dir, 'state.json'), acquisition:read, initialize: true, now: () => nowValue });
  return { payee, payer, offer, offerStored, roomSnapshot, dir, signer, read,
    use: value => read.use(value), setNote: value => read.setNote(value), setNow: v => { nowValue = v; } };
}
function acceptedFixture(overrides = {}) {
  const f = fixture(overrides);
  const admission = admit(f.signer, f.roomSnapshot);
  const accept = f.signer.issue('ACCEPT');
  const stored = storedLine(OFFER_ROOM, 2, BASE + 1000, f.payee, accept.line, accept.nonce);
  f.setNow(BASE + 1000);
  f.use(snapshot(OFFER_ROOM, 1, raw(f.offerStored, stored), BASE + 1000));
  assert.equal(f.signer.reconcile('ACCEPT').status, 'OBSERVED');
  return { ...f, admission, accept, acceptStored: stored };
}
function errorCode(fn, expected) {
  assert.throws(fn, error => error?.code === expected || error?.message === expected);
}

test('PR #30 Close Call typed request validates against the exact Project DID interface without a Secret', () => {
  const request = {
    version: 1,
    kind: 'CLOSE_CALL_TYPED_SIGN_REQUEST',
    action: 'CLOSE_CALL_OWNER_REGISTER',
    expectedDid: PROJECT_DID,
    subject: { season: 'close-1', did: PROJECT_DID },
    binding: {
      contest: 'close-1',
      rulesCommit: '66c1da36538e4b1c685417d2f66922906b13fea0',
      packageManifestSha256: 'bae09812e25eb6f1369c611f24964f7ea0acafddfc45301a16f33f941296dafa',
      room: 'close1',
      noncePolicy: 'SIGNER_ALLOCATES_ROOM_NONCE',
    },
    preview: {
      sha256: '2da5479c2ad7fbd6a72bcad0e7db7740f98ffab6b098912ad6074f68e9c3e2ce',
      utf8: '{"t":"owner","season":"close-1","key":"' + PROJECT_DID + '"}',
    },
  };
  const checked = validateCloseCallOwnerRequest(request, PROJECT_DID);
  assert.deepEqual(checked, {
    room: 'close1',
    line: '{"t":"owner","season":"close-1","key":"' + PROJECT_DID + '"}',
  });

  const tampered = structuredClone(request);
  tampered.binding.rulesCommit = '0'.repeat(40);
  errorCode(() => validateCloseCallOwnerRequest(tampered, PROJECT_DID), 'CLOSE_CALL_REQUEST_BINDING');

  const changedPreview = structuredClone(request);
  changedPreview.preview.utf8 = changedPreview.preview.utf8 + ' ';
  errorCode(() => validateCloseCallOwnerRequest(changedPreview, PROJECT_DID), 'CLOSE_CALL_PREVIEW_MISMATCH');
});

test('Close Call owner registration stops at one bounded synthetic signature with Signer-owned nonce', () => {
  const owner = signerFromSeed(PAYEE_SEED);
  const existing = storedLine(
    CLOSE_CALL_ROOM, 1, CLOSE_CALL_BASE - 100,
    owner, '{"t":"note"}', '9000000000000000000',
  );
  const read = acquisition(snapshot(
    CLOSE_CALL_ROOM, 1, raw(existing), CLOSE_CALL_BASE, owner.did,
  ));
  const dir = mkdtempSync(join(tmpdir(), 'close-call-signer-'));
  let nowValue = CLOSE_CALL_BASE;
  const signer = testSigner({
    seed: PAYEE_SEED,
    policy: closeCallPolicy(owner.did),
    statePath: join(dir, 'state.json'),
    acquisition: read,
    initialize: true,
    now: () => nowValue,
  });
  const request = closeCallRequest(owner.did);
  const record = signer.closeCallOwnerRegister(request);
  assert.equal(record.room, CLOSE_CALL_ROOM);
  assert.equal(record.line, closeCallOwnerText(owner.did));
  assert.equal(record.nonce, '9000000000000000001');
  assert.equal(verifyDidSignature(
    owner.did,
    canonicalMessage(record.room, record.nonce, record.line),
    record.signature,
  ), true);
  assert.equal(signer.status().signaturesUsed, 1);
  assert.equal(signer.status().actions[CLOSE_CALL_ACTION].status, 'ATTEMPTED');
  errorCode(() => signer.closeCallOwnerRegister(request), 'DUPLICATE_ACTION');

  const restarted = testSigner({
    seed: PAYEE_SEED,
    policy: closeCallPolicy(owner.did),
    statePath: join(dir, 'state.json'),
    acquisition: read,
    now: () => nowValue,
  });
  assert.equal(restarted.status().signaturesUsed, 1);
  assert.equal(restarted.status().actions[CLOSE_CALL_ACTION].status, 'ATTEMPTED');
  errorCode(() => restarted.closeCallOwnerRegister(request), 'DUPLICATE_ACTION');
});

test('Close Call registration fails closed on observed duplicate, profile crossing and event lock', () => {
  const owner = signerFromSeed(PAYEE_SEED);
  const ownerLine = closeCallOwnerText(owner.did);
  const already = storedLine(
    CLOSE_CALL_ROOM, 1, CLOSE_CALL_BASE - 100, owner, ownerLine, '123',
  );
  const read = acquisition(snapshot(
    CLOSE_CALL_ROOM, 1, raw(already), CLOSE_CALL_BASE, owner.did,
  ));
  const dir = mkdtempSync(join(tmpdir(), 'close-call-duplicate-'));
  const signer = testSigner({
    seed: PAYEE_SEED,
    policy: closeCallPolicy(owner.did),
    statePath: join(dir, 'state.json'),
    acquisition: read,
    initialize: true,
    now: () => CLOSE_CALL_BASE,
  });
  errorCode(() => signer.closeCallOwnerRegister(closeCallRequest(owner.did)),
    'CLOSE_CALL_REGISTRATION_ALREADY_OBSERVED');
  assert.equal(signer.status().signaturesUsed, 0);
  errorCode(() => signer.issue('ACCEPT'), 'POLICY_CAPABILITY');

  const tclk = fixture();
  errorCode(() => tclk.signer.closeCallOwnerRegister(closeCallRequest(tclk.payee.did)),
    'POLICY_CAPABILITY');

  const lockedDir = mkdtempSync(join(tmpdir(), 'close-call-locked-'));
  const locked = testSigner({
    seed: PAYEE_SEED,
    policy: closeCallPolicy(owner.did, {
      issuedAtMs: CLOSE_CALL_LOCK_MS - 2000,
      expiresAtMs: CLOSE_CALL_LOCK_MS,
    }),
    statePath: join(lockedDir, 'state.json'),
    acquisition: read,
    initialize: true,
    now: () => CLOSE_CALL_LOCK_MS,
  });
  errorCode(() => locked.closeCallOwnerRegister(closeCallRequest(owner.did)), 'POLICY_EXPIRED');
});

test('official Ed25519 fixed seed derives the RFC 8032 public key and verifiable Technocore signature', () => {
  const seed = Buffer.from('9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60', 'hex');
  assert.equal(rawPublicKeyFromSeed(seed).toString('hex'), 'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a');
  const signer = signerFromSeed(seed);
  const canonical = canonicalMessage('lobby', '1730000000000000001', 'tclk1 {}');
  const signature = signer.sign(canonical);
  assert.equal(signature.length, 86);
  assert.equal(verifyDidSignature(signer.did, canonical, signature), true);
});

test('official tclk golden offer / accept vector matches current pinned commit', () => {
  const payerDid = 'did:key:z6Mk' + 'f'.repeat(44);
  const payeeDid = 'did:key:z6Mk' + 'g'.repeat(44);
  const offer = makeOffer({
    from: payerDid, role: 'payer', amount: '1000000', asset: 'FLOP', lock: 'hash', rails: ['flop-htlc','x402'],
    claimByMs: 1756703600000, refundAfterMs: 1756707200000, expiresMs: 1756700600000,
    job: { proto: 'a2a', id: 'task-3f', context: 'ctx-1' },
  }, '9f2c81d04c9e1f7a');
  assert.equal(offer.id, '0xd001fbbf4fa36d9ab8ea88df02a8b3303539e9d59f7ff9d9bfeb679318e9ce75');
  const accept = makeAccept(offer, { from: payeeDid, statement: '0x' + 'ab'.repeat(32), nonce: '0011223344556677' });
  assert.equal(accept.contract, '0x2768bf32b455317879796093ff2e5882371cbec238611ca71f555a7fcbe58e1c');
});

test('read adapter preserves a 19-digit nonce exactly and verifies signatures', () => {
  const signer = signerFromSeed(PAYEE_SEED);
  const line = 'hello';
  const stored = storedLine(OFFER_ROOM, 9, BASE, signer, line, '1800000000000000001');
  const parsed = normalizeExportSnapshot({ ...snapshot(OFFER_ROOM, 1, raw(stored), BASE + 1), expectedDid: signer.did });
  assert.equal(parsed.records[0].nonce, '1800000000000000001');
  assert.equal(parsed.nonceObservation.observedNonce, '1800000000000000001');
});

test('one bounded Paper GCD work completes ACCEPT -> heartbeat -> lock -> delivery -> reveal', () => {
  const f = fixture();
  const admission = admit(f.signer, f.roomSnapshot);
  assert.equal(admission.did, f.payee.did);

  const acceptRecord = f.signer.issue('ACCEPT');
  assert.equal(decodeFrame(acceptRecord.line).type, 'accept');
  f.setNow(BASE + 1000);
  const acceptStored = storedLine(OFFER_ROOM, 2, BASE + 1000, f.payee, acceptRecord.line, acceptRecord.nonce);
  const acceptedSnapshot = snapshot(OFFER_ROOM, 1, raw(f.offerStored, acceptStored), BASE + 1001, f.payee.did);
  f.use(acceptedSnapshot);
  assert.equal(f.signer.reconcile('ACCEPT').status, 'OBSERVED');

  const emptyDeal = snapshot(admission.dealRoom, 0, Buffer.alloc(0), BASE + 1002, f.payee.did);
  f.use(emptyDeal);
  const hb = f.signer.issue('INITIAL_HEARTBEAT');
  const hbStored = storedLine(admission.dealRoom, 1, BASE + 1100, f.payee, hb.line, hb.nonce);
  f.setNow(BASE + 1100);
  const hbSnapshot = snapshot(admission.dealRoom, 1, raw(hbStored), BASE + 1101, f.payee.did);
  f.use(hbSnapshot);
  assert.equal(f.signer.reconcile('INITIAL_HEARTBEAT').status, 'OBSERVED');

  const lockFrame = { type:'lock', from:f.payer.did, contract:admission.contract, rail:'paper', ref:admission.contract };
  const lockLine = encodeFrame(lockFrame);
  const lockStored = storedLine(admission.dealRoom, 2, BASE + 1200, f.payer, lockLine, '1800000000000000100');
  const paperLocked = encodePaperRecord({ status:'locked', lock:'hash', statement:decodeFrame(acceptRecord.line).statement, refundAfterMs:f.offer.refundAfterMs });
  f.setNow(BASE + 1200);
  const lockedSnapshot = snapshot(admission.dealRoom, 1, raw(hbStored, lockStored), BASE + 1201, f.payee.did);
  f.use(lockedSnapshot); f.setNote(paperLocked);
  const delivery = f.signer.issue('DELIVERY_GCD');
  assert.equal(delivery.line, ANSWER);
  const deliveryStored = storedLine(admission.dealRoom, 3, BASE + 1300, f.payee, delivery.line, delivery.nonce);
  f.setNow(BASE + 1300);
  const deliveredSnapshot = snapshot(admission.dealRoom, 1, raw(hbStored, lockStored, deliveryStored), BASE + 1301, f.payee.did);
  f.use(deliveredSnapshot);
  assert.equal(f.signer.reconcile('DELIVERY_GCD').status, 'OBSERVED');

  const reveal = f.signer.issue('REVEAL');
  const revealFrame = decodeFrame(reveal.line);
  assert.equal(revealFrame.type, 'reveal');
  assert.equal(hashLockFromPreimage(Buffer.from(revealFrame.secret.slice(2), 'hex')).hash, revealFrame.secret ? decodeFrame(acceptRecord.line).statement : null);
  const revealStored = storedLine(admission.dealRoom, 4, BASE + 1400, f.payee, reveal.line, reveal.nonce);
  f.setNow(BASE + 1400);
  const revealedSnapshot = snapshot(admission.dealRoom, 1, raw(hbStored, lockStored, deliveryStored, revealStored), BASE + 1401, f.payee.did);
  f.use(revealedSnapshot);
  assert.equal(f.signer.reconcile('REVEAL').status, 'OBSERVED');
  errorCode(() => f.signer.observeCompletion(), 'COMPLETION_NOT_CONFIRMED');
  const claimed = encodePaperRecord({ status:'claimed', lock:'hash', statement:decodeFrame(acceptRecord.line).statement, refundAfterMs:f.offer.refundAfterMs, secret:revealFrame.secret });
  f.setNote(claimed);
  assert.equal(f.signer.observeCompletion().status, 'COMPLETED');
  assert.equal(f.signer.status().signaturesUsed, 4);
});


test('read adapter accepts an export exactly at the 10 MiB bound and rejects larger input', () => {
  const did=signerFromSeed(PAYEE_SEED).did;
  const exact=Buffer.alloc(10 << 20); exact[exact.length-1]=0x0a;
  errorCode(()=>normalizeExportSnapshot({...snapshot(OFFER_ROOM,1,exact,BASE),expectedDid:did}),'EXPORT_RECORD_INVALID');
  const oversized=Buffer.alloc((10 << 20)+1); oversized[oversized.length-1]=0x0a;
  errorCode(()=>normalizeExportSnapshot({...snapshot(OFFER_ROOM,1,oversized,BASE),expectedDid:did}),'EXPORT_TOO_LARGE');
});

test('tampered export signature and incomplete export fail closed', () => {
  const signer = signerFromSeed(PAYEE_SEED);
  const good = storedLine(OFFER_ROOM, 1, BASE, signer, 'hello', '123');
  const tampered = good.replace('hello', 'hullo');
  errorCode(() => normalizeExportSnapshot({...snapshot(OFFER_ROOM,1,raw(tampered),BASE+1),expectedDid:signer.did}),'EXPORT_SIGNATURE_INVALID');
  errorCode(() => normalizeExportSnapshot({...snapshot(OFFER_ROOM,1,Buffer.from(good.trimEnd()),BASE+1),expectedDid:signer.did}),'EXPORT_INCOMPLETE');
});

test('ambiguous outcome never authorizes blind re-issue', () => {
  const f = fixture();
  admit(f.signer, f.roomSnapshot);
  f.signer.issue('ACCEPT');
  f.setNow(BASE+1000);
  const unchanged = snapshot(OFFER_ROOM,1,raw(f.offerStored),BASE+1000,f.payee.did);
  f.use(unchanged);
  const result = f.signer.reconcile('ACCEPT');
  assert.deepEqual(result,{status:'AMBIGUOUS',retry:false});
  errorCode(()=>f.signer.issue('ACCEPT'),'DUPLICATE_ACTION');
});

test('admission derives the exact GCD answer from source bytes and signed offer binding', () => {
  const f = fixture();
  errorCode(() => f.signer.admit({offerSeq:1,sourceBytes:Buffer.from('gcd_lcm 6 43\n')}),'TASK_BINDING');
  errorCode(() => f.signer.admit({offerSeq:1,sourceBytes:Buffer.from('gcd_lcm 6 42; rm -rf /\n')}),'SOURCE_INVALID');
  assert.equal(admit(f.signer, f.roomSnapshot).offer, f.offer.id);
});

test('signer rejects normalized or tampered trusted export evidence', () => {
  const f = fixture();
  const normalized = normalizeExportSnapshot({...f.roomSnapshot,expectedDid:f.payee.did});
  f.use(normalized);
  errorCode(() => f.signer.admit({offerSeq:1,sourceBytes:SOURCE_BYTES}),'INVALID_FIELDS');
  const forged = {...f.roomSnapshot,body:Buffer.from(f.offerStored.replace('PAPER','FLOP'))};
  f.use(forged);
  errorCode(() => f.signer.admit({offerSeq:1,sourceBytes:SOURCE_BYTES}),'EXPORT_SIGNATURE_INVALID');
});

test('missing protected state and insufficient completion time fail closed', () => {
  const f = fixture();
  const statePath = join(f.dir,'state.json');
  unlinkSync(statePath);
  errorCode(() => testSigner({seed:PAYEE_SEED,policy:policy(f.payee.did),statePath,acquisition:f.read,now:()=>BASE}),'STATE_MISSING');
  const late = testSigner({seed:PAYEE_SEED,policy:policy(f.payee.did),statePath,acquisition:f.read,initialize:true,now:()=>BASE+29*60*1000});
  const lateSnapshot = {...f.roomSnapshot,capturedAtMs:BASE+29*60*1000};
  errorCode(() => admit(late,lateSnapshot),'DEADLINE_POLICY');
});

test('public signer object exposes no seed, policy, mutable state, read port or generic signer', () => {
  const f=fixture();
  assert.deepEqual(Object.keys(f.signer), []);
  assert.equal(f.signer.seed, undefined);
  assert.equal(f.signer.state, undefined);
  assert.equal(f.signer.acquisition, undefined);
  assert.equal(f.signer.signer, undefined);
  assert.equal(f.signer.sign, undefined);
  errorCode(() => f.signer.admit({offerSeq:1,sourceBytes:SOURCE_BYTES,safe:true}), 'INVALID_FIELDS');
  admit(f.signer,f.roomSnapshot);
  errorCode(() => f.signer.issue('ACCEPT',{snapshot:f.roomSnapshot}), 'INVALID_FIELDS');
  unlinkSync(join(f.dir,'state.json'));
  errorCode(() => f.signer.issue('ACCEPT'), 'STATE_MISSING');
});

test('policy requires enough signature quota for its heartbeat path', () => {
  const did=signerFromSeed(PAYEE_SEED).did;
  errorCode(()=>policyDigest(policy(did,{maxSignatures:3,requireHeartbeat:true})),'POLICY_LIMIT');
});

test('expired policy, wrong protocol and malformed canonical work are denied', () => {
  const did=signerFromSeed(PAYEE_SEED).did;
  errorCode(()=>policyDigest(policy(did,{protocol:'tclk/2'})),'POLICY_PROTOCOL');
  const f=fixture();
  const expired = testSigner({seed:PAYEE_SEED,policy:policy(did),statePath:join(f.dir,'state.json'),acquisition:f.read,now:()=>BASE+3_600_001});
  errorCode(()=>admit(expired,{...f.roomSnapshot,capturedAtMs:BASE+3_600_001}),'POLICY_EXPIRED');
  f.use(f.roomSnapshot);
  errorCode(()=>f.signer.admit({offerSeq:1,sourceBytes:Buffer.from('gcd_lcm 06 42\n')}),'SOURCE_INVALID');
});

test('value-bearing offer is rejected at admission', () => {
  const payee = signerFromSeed(PAYEE_SEED), payer = signerFromSeed(PAYER_SEED);
  const offer = makeOffer({
    from:payer.did, role:'payer', amount:'1', asset:'FLOP', lock:'hash', rails:['flop-htlc'],
    claimByMs:BASE+1_800_000, refundAfterMs:BASE+2_400_000, expiresMs:BASE+600_000,
    job:{proto:'a2a',id:'x',context:PREFIX+SOURCE},
  }, '1111111111111111');
  const stored = storedLine(OFFER_ROOM, 1, BASE, payer, encodeFrame(offer), '100');
  const snap = snapshot(OFFER_ROOM, 1, raw(stored), BASE, payee.did);
  const dir = mkdtempSync(join(tmpdir(),'policy-value-'));
  const s = testSigner({seed:PAYEE_SEED,policy:policy(payee.did),statePath:join(dir,'state.json'),acquisition:acquisition(snap),initialize:true,now:()=>BASE});
  errorCode(() => admit(s, snap),'PAPER_ONLY');
});

test('final revalidation rejects a competing ACCEPT', () => {
  const f = fixture();
  admit(f.signer, f.roomSnapshot);
  const other = signerFromSeed(OTHER_SEED);
  const statement = hashLockFromPreimage(Buffer.from('44'.repeat(32),'hex')).hash;
  const rival = makeAccept(f.offer,{from:other.did,statement,nonce:'abcdef0123456789'});
  const rivalStored = storedLine(OFFER_ROOM,2,BASE+100,other,encodeFrame(rival),'101');
  const conflicted = snapshot(OFFER_ROOM,1,raw(f.offerStored,rivalStored),BASE+101,f.payee.did);
  f.use(conflicted);
  errorCode(()=>f.signer.issue('ACCEPT'),'ACCEPT_CONFLICT');
});

test('restart keeps ATTEMPTED replay protection; reconciliation still works after policy expiry', () => {
  const f = fixture();
  admit(f.signer, f.roomSnapshot);
  const accept = f.signer.issue('ACCEPT');
  const statePath = join(f.dir,'state.json');
  const restarted = testSigner({seed:PAYEE_SEED,policy:policy(f.payee.did),statePath,acquisition:f.read,now:()=>BASE+2000});
  errorCode(()=>restarted.issue('ACCEPT'),'DUPLICATE_ACTION');

  const afterExpiry = BASE + 60 * 60 * 1000 + 1;
  const expired = testSigner({seed:PAYEE_SEED,policy:policy(f.payee.did),statePath,acquisition:f.read,now:()=>afterExpiry});
  const stored = storedLine(OFFER_ROOM,2,BASE+1900,f.payee,accept.line,accept.nonce);
  const fresh = snapshot(OFFER_ROOM,1,raw(f.offerStored,stored),afterExpiry,f.payee.did);
  f.use(fresh);
  assert.equal(expired.reconcile('ACCEPT').status,'OBSERVED');
  assert.equal(expired.status().actions.ACCEPT.status,'OBSERVED');
});

test('policy tamper, state corruption and DID mismatch fail closed', () => {
  const f = fixture();
  admit(f.signer, f.roomSnapshot);
  const statePath=join(f.dir,'state.json');
  errorCode(()=>testSigner({seed:PAYEE_SEED,policy:policy(f.payee.did,{freshMs:31_000}),statePath,acquisition:f.read,now:()=>BASE}),'STATE_BINDING');
  const saved=readFileSync(statePath);
  writeFileSync(statePath,'{');
  errorCode(()=>testSigner({seed:PAYEE_SEED,policy:policy(f.payee.did),statePath,acquisition:f.read,now:()=>BASE}),'STATE_CORRUPT');
  writeFileSync(statePath,saved);
  const wrong=signerFromSeed(OTHER_SEED).did;
  errorCode(()=>testSigner({seed:PAYEE_SEED,policy:policy(wrong),statePath:join(f.dir,'other.json'),acquisition:f.read,now:()=>BASE}),'DID_MISMATCH');
});

test('unsupported generic signing operation is absent', () => {
  const f=fixture();
  admit(f.signer, f.roomSnapshot);
  errorCode(()=>f.signer.issue('SIGN_TEXT',{text:'anything'}),'UNSUPPORTED_ACTION');
});

test('policy digest is deterministic and binds tclk commit', () => {
  const did=signerFromSeed(PAYEE_SEED).did;
  assert.equal(policyDigest(policy(did)), policyDigest(policy(did)));
  errorCode(()=>policyDigest(policy(did,{tclkCommit:'0'.repeat(40)})),'POLICY_PROTOCOL');
});

test('official-valid reordered and paymentKey ACCEPT frames block signing', () => {
  for (const withPaymentKey of [false, true]) {
    const f = fixture(); admit(f.signer, f.roomSnapshot);
    const other = signerFromSeed(OTHER_SEED);
    const statement = '0x' + '44'.repeat(32);
    const core = { from: other.did, statement, nonce: 'abcdef0123456789' };
    if (withPaymentKey) core.paymentKey = '0x0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798';
    const rival = withPaymentKey
      ? { type: 'accept', ...core, ref: f.offer.id,
        contract: contractId(f.offer, { ...core, ref: f.offer.id }) }
      : makeAccept(f.offer, core);
    if (withPaymentKey) assert.equal(decodeFrame(encodeFrame(rival)).paymentKey, core.paymentKey);
    const line = 'tclk1 ' + JSON.stringify({ type: rival.type, from: rival.from, ref: rival.ref,
      statement: rival.statement, contract: rival.contract, ...(rival.paymentKey ? { paymentKey: rival.paymentKey } : {}), nonce: rival.nonce });
    f.use(snapshot(OFFER_ROOM, 1, raw(f.offerStored, storedLine(OFFER_ROOM, 2, BASE + 100, other, line, '101')), BASE + 100));
    errorCode(() => f.signer.issue('ACCEPT'), 'ACCEPT_CONFLICT');
  }
});

test('ACCEPT reconciliation requires the first valid transition and an in-time venue timestamp', () => {
  const f = fixture(); admit(f.signer, f.roomSnapshot);
  const own = f.signer.issue('ACCEPT');
  const other = signerFromSeed(OTHER_SEED);
  const rival = makeAccept(f.offer, { from: other.did, statement: '0x' + '55'.repeat(32), nonce: 'abcdef0123456789' });
  const rivalStored = storedLine(OFFER_ROOM, 2, BASE + 100, other, encodeFrame(rival), '101');
  const ownStored = storedLine(OFFER_ROOM, 3, BASE + 200, f.payee, own.line, own.nonce);
  f.setNow(BASE + 200);
  f.use(snapshot(OFFER_ROOM, 1, raw(f.offerStored, rivalStored, ownStored), BASE + 200));
  assert.equal(f.signer.reconcile('ACCEPT').status, 'AMBIGUOUS');

  const g = fixture(); admit(g.signer, g.roomSnapshot);
  const late = g.signer.issue('ACCEPT');
  g.setNow(BASE + 10 * 60 * 1000 + 1);
  g.use(snapshot(OFFER_ROOM, 1, raw(g.offerStored,
    storedLine(OFFER_ROOM, 2, BASE + 10 * 60 * 1000, g.payee, late.line, late.nonce)), BASE + 10 * 60 * 1000 + 1));
  assert.equal(g.signer.reconcile('ACCEPT').status, 'AMBIGUOUS');
  assert.equal(acceptedFixture().signer.status().actions.ACCEPT.status, 'OBSERVED');
});

test('deal generation starts at zero, pins on creation and survives restart', () => {
  const f = acceptedFixture();
  f.use(snapshot(f.admission.dealRoom, 0, Buffer.alloc(0), BASE + 1000));
  const hb = f.signer.issue('INITIAL_HEARTBEAT');
  const hbStored = storedLine(f.admission.dealRoom, 1, BASE + 1100, f.payee, hb.line, hb.nonce);
  f.setNow(BASE + 1100);
  f.use(snapshot(f.admission.dealRoom, 1, raw(hbStored), BASE + 1100));
  assert.equal(f.signer.reconcile('INITIAL_HEARTBEAT').status, 'OBSERVED');
  const restarted = testSigner({ seed: PAYEE_SEED, policy: policy(f.payee.did), statePath: join(f.dir, 'state.json'), acquisition: f.read, now: () => BASE + 1200 });
  f.use(snapshot(f.admission.dealRoom, 2, raw(hbStored), BASE + 1200));
  errorCode(() => restarted.issue('DELIVERY_GCD'), 'GENERATION_MISMATCH');
});

test('offers generation change rejects signing and reconciliation', () => {
  const f = fixture(); admit(f.signer, f.roomSnapshot);
  f.use(snapshot(OFFER_ROOM, 2, raw(f.offerStored), BASE));
  errorCode(() => f.signer.issue('ACCEPT'), 'GENERATION_MISMATCH');
});

test('LOCK requires authenticated payer and follows the observed ACCEPT', () => {
  const f = acceptedFixture({ requireHeartbeat: false, maxSignatures: 3 });
  const lock = { type: 'lock', from: f.payer.did, contract: f.admission.contract, rail: 'paper', ref: f.admission.contract };
  const line = encodeFrame(lock);
  const mallory = signerFromSeed(OTHER_SEED);
  const forged = storedLine(f.admission.dealRoom, 1, BASE + 1200, mallory, line, '111');
  const paper = encodePaperRecord({ status: 'locked', lock: 'hash', statement: decodeFrame(f.accept.line).statement, refundAfterMs: f.offer.refundAfterMs });
  f.setNote(paper); f.setNow(BASE + 1200);
  f.use(snapshot(f.admission.dealRoom, 1, raw(forged), BASE + 1200));
  errorCode(() => f.signer.issue('DELIVERY_GCD'), 'PAPER_LOCK_REQUIRED');
  const early = storedLine(f.admission.dealRoom, 1, BASE + 900, f.payer, line, '111');
  f.use(snapshot(f.admission.dealRoom, 1, raw(early), BASE + 1200));
  errorCode(() => f.signer.issue('DELIVERY_GCD'), 'PAPER_LOCK_REQUIRED');
  const genuine = storedLine(f.admission.dealRoom, 2, BASE + 1200, f.payer, line, '112');
  f.use(snapshot(f.admission.dealRoom, 1, raw(forged, genuine), BASE + 1200));
  assert.equal(f.signer.issue('DELIVERY_GCD').line, ANSWER);
});

test('deadline reserves accept, completion and claim margins at admission and final revalidation', () => {
  function nearDeadline(claimOffsetMs) {
    const f = fixture();
    const offer = makeOffer({ from: f.payer.did, role: 'payer', amount: '1', asset: 'PAPER', lock: 'hash', rails: ['paper'],
      claimByMs: BASE + claimOffsetMs, refundAfterMs: BASE + claimOffsetMs + 60_000,
      expiresMs: BASE + 4 * 60_000, job: { proto: 'a2a', id: 'TEST-gcd', context: PREFIX + SOURCE } }, '0011223344556677');
    const line = storedLine(OFFER_ROOM, 1, BASE - 500, f.payer, encodeFrame(offer), '1799999999999999999');
    f.use(snapshot(OFFER_ROOM, 1, raw(line), BASE));
    return f;
  }
  const short = nearDeadline(130_000);
  errorCode(() => short.signer.admit({ offerSeq: 1, sourceBytes: SOURCE_BYTES }), 'DEADLINE_POLICY');
  const enough = nearDeadline(300_000);
  enough.signer.admit({ offerSeq: 1, sourceBytes: SOURCE_BYTES });
  enough.setNow(BASE + 90_000);
  const fresh = enough.read.export(OFFER_ROOM);
  enough.use({ ...fresh, capturedAtMs: BASE + 90_000 });
  errorCode(() => enough.signer.issue('ACCEPT'), 'DEADLINE_POLICY');
});

test('AMBIGUOUS ACCEPT becomes OBSERVED only after exact first valid publication', () => {
  const f = fixture(); admit(f.signer, f.roomSnapshot);
  const accept = f.signer.issue('ACCEPT');
  f.setNow(BASE + 1000);
  f.use(snapshot(OFFER_ROOM, 1, raw(f.offerStored), BASE + 1000));
  assert.equal(f.signer.reconcile('ACCEPT').status, 'AMBIGUOUS');
  const published = storedLine(OFFER_ROOM, 2, BASE + 1100, f.payee, accept.line, accept.nonce);
  f.setNow(BASE + 1100);
  f.use(snapshot(OFFER_ROOM, 1, raw(f.offerStored, published), BASE + 1100));
  assert.equal(f.signer.reconcile('ACCEPT').status, 'OBSERVED');
});

test('three-signature path without heartbeat completes the same synthetic work', () => {
  const f = acceptedFixture({ requireHeartbeat: false, maxSignatures: 3 });
  const room = f.admission.dealRoom;
  const lock = encodeFrame({ type: 'lock', from: f.payer.did, contract: f.admission.contract, rail: 'paper', ref: f.admission.contract });
  const locked = storedLine(room, 1, BASE + 1100, f.payer, lock, '111');
  f.setNow(BASE + 1100);
  f.use(snapshot(room, 1, raw(locked), BASE + 1100));
  f.setNote(encodePaperRecord({ status: 'locked', lock: 'hash', statement: decodeFrame(f.accept.line).statement, refundAfterMs: f.offer.refundAfterMs }));
  const delivery = f.signer.issue('DELIVERY_GCD');
  const delivered = storedLine(room, 2, BASE + 1200, f.payee, delivery.line, delivery.nonce);
  f.setNow(BASE + 1200);
  f.use(snapshot(room, 1, raw(locked, delivered), BASE + 1200));
  assert.equal(f.signer.reconcile('DELIVERY_GCD').status, 'OBSERVED');
  const reveal = f.signer.issue('REVEAL');
  const revealed = storedLine(room, 3, BASE + 1300, f.payee, reveal.line, reveal.nonce);
  f.setNow(BASE + 1300);
  f.use(snapshot(room, 1, raw(locked, delivered, revealed), BASE + 1300));
  assert.equal(f.signer.reconcile('REVEAL').status, 'OBSERVED');
  f.setNote(encodePaperRecord({ status: 'claimed', lock: 'hash', statement: decodeFrame(f.accept.line).statement,
    refundAfterMs: f.offer.refundAfterMs, secret: decodeFrame(reveal.line).secret }));
  assert.equal(f.signer.observeCompletion().status, 'COMPLETED');
  assert.equal(f.signer.status().signaturesUsed, 3);
});

test('first payer LOCK wins and payer CANCEL before LOCK blocks delivery', () => {
  for (const firstType of ['wrong-ref', 'cancel']) {
    const f = acceptedFixture({ requireHeartbeat: false, maxSignatures: 3 });
    const room = f.admission.dealRoom;
    const first = firstType === 'cancel'
      ? encodeFrame({ type: 'cancel', from: f.payer.did, contract: f.admission.contract })
      : encodeFrame({ type: 'lock', from: f.payer.did, contract: f.admission.contract, rail: 'paper', ref: 'other-ref' });
    const correct = encodeFrame({ type: 'lock', from: f.payer.did, contract: f.admission.contract, rail: 'paper', ref: f.admission.contract });
    const records = raw(storedLine(room, 1, BASE + 1100, f.payer, first, '111'),
      storedLine(room, 2, BASE + 1200, f.payer, correct, '112'));
    f.setNow(BASE + 1200);
    f.use(snapshot(room, 1, records, BASE + 1200));
    f.setNote(encodePaperRecord({ status: 'locked', lock: 'hash', statement: decodeFrame(f.accept.line).statement,
      refundAfterMs: f.offer.refundAfterMs }));
    errorCode(() => f.signer.issue('DELIVERY_GCD'), 'PAPER_LOCK_REQUIRED');
  }
});

test('a reposted OFFER cannot hide an earlier valid ACCEPT', () => {
  const f = fixture();
  const other = signerFromSeed(OTHER_SEED);
  const rival = makeAccept(f.offer, { from: other.did, statement: '0x' + '55'.repeat(32), nonce: 'abcdef0123456789' });
  const earlierAccept = storedLine(OFFER_ROOM, 7, BASE + 100, other, encodeFrame(rival), '101');
  const repost = storedLine(OFFER_ROOM, 10, BASE + 200, f.payer, encodeFrame(f.offer), '102');
  f.setNow(BASE + 200);
  f.use(snapshot(OFFER_ROOM, 1, raw(f.offerStored, earlierAccept, repost), BASE + 200));
  errorCode(() => f.signer.admit({ offerSeq: 10, sourceBytes: SOURCE_BYTES }), 'FROZEN_OFFER_UNAVAILABLE');
});


test('external acquisition cannot carry a request past policy expiry into signing', () => {
  const f = acceptedFixture({
    expiresAtMs: BASE + 10_000,
    acceptMarginMs: 1_000,
    minCompletionWindowMs: 1_000,
    claimMarginMs: 1_000,
  });
  const statePath = join(f.dir, 'state.json');
  let nowValue = BASE + 9_000;
  const delayedRead = {
    export: room => {
      assert.equal(room, f.admission.dealRoom);
      const result = snapshot(room, 0, Buffer.alloc(0), nowValue);
      nowValue = BASE + 10_001;
      return result;
    },
    paperNote: () => null,
  };
  const restarted = testSigner({
    seed: PAYEE_SEED,
    policy: policy(f.payee.did, {
      expiresAtMs: BASE + 10_000,
      acceptMarginMs: 1_000,
      minCompletionWindowMs: 1_000,
      claimMarginMs: 1_000,
    }),
    statePath,
    acquisition: delayedRead,
    now: () => nowValue,
  });
  errorCode(() => restarted.issue('INITIAL_HEARTBEAT'), 'POLICY_EXPIRED');
});
