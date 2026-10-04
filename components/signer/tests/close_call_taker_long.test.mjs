import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import {
  CLOSE_CALL_ACTION,
  CLOSE_CALL_CONTEST,
  CLOSE_CALL_EXPECTED_SWEEP_SECONDS,
  CLOSE_CALL_LOCK_MS,
  CLOSE_CALL_NONCE_POLICY,
  CLOSE_CALL_PACKAGE_MANIFEST_SHA256,
  CLOSE_CALL_POLICY_PROTOCOL,
  CLOSE_CALL_PRICE_ROOM,
  CLOSE_CALL_ROOM,
  CLOSE_CALL_RULES_COMMIT,
  CLOSE_CALL_TAKER_LONG_ACTION,
  CLOSE_CALL_TAKER_LONG_POLICY_PROTOCOL,
  PolicySigner,
  closeCallOwnerText,
} from '../src/policy_signer.mjs';
import {
  canonicalMessage,
  signerFromSeed,
  verifyDidSignature,
} from '../src/technocore_signing.mjs';

const OWNER_SEED = Buffer.from('11'.repeat(32), 'hex');
const MAKER_SEED = Buffer.from('44'.repeat(32), 'hex');
const REFEREE_SEED = Buffer.from('66'.repeat(32), 'hex');
const OTHER_SEED = Buffer.from('77'.repeat(32), 'hex');
const NOW = CLOSE_CALL_LOCK_MS - 2 * 60 * 60 * 1000;

function stable(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  return `{${Object.keys(value).sort()
    .map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`;
}
function sha(value) {
  return createHash('sha256').update(
    typeof value === 'string' ? value : stable(value),
  ).digest('hex');
}
function storedLine(room, seq, tsMs, signer, line, nonce) {
  const signature = signer.sign(canonicalMessage(room, nonce, line));
  const row = {
    seq, ts: new Date(tsMs).toISOString(), from: signer.did,
    text: line, nonce: String(nonce), sig: signature,
  };
  return JSON.stringify(row).replace(`"nonce":"${nonce}"`, `"nonce":${nonce}`) + '\n';
}
function snapshot(room, body, expectedDid, generation = 1) {
  return { room, generation, body: Buffer.from(body, 'utf8'), capturedAtMs: NOW };
}
function acquisition(...values) {
  const exports = new Map(values.map(value => [value.room, value]));
  return { export: room => exports.get(room), paperNote: () => null };
}
function tradePolicy(ownerDid, refereeDid, overrides = {}) {
  return {
    version: 1,
    protocol: CLOSE_CALL_TAKER_LONG_POLICY_PROTOCOL,
    action: CLOSE_CALL_TAKER_LONG_ACTION,
    expectedDid: ownerDid,
    contest: CLOSE_CALL_CONTEST,
    rulesCommit: CLOSE_CALL_RULES_COMMIT,
    packageManifestSha256: CLOSE_CALL_PACKAGE_MANIFEST_SHA256,
    room: CLOSE_CALL_ROOM,
    priceRoom: CLOSE_CALL_PRICE_ROOM,
    refereeDid,
    noncePolicy: CLOSE_CALL_NONCE_POLICY,
    issuedAtMs: NOW - 1000,
    expiresAtMs: NOW + 60 * 60 * 1000,
    notAfterMs: CLOSE_CALL_LOCK_MS,
    freshMs: 30_000,
    maxSignatures: 2,
    noValueMarker: 'EXPLICIT_PAPER_NO_VALUE',
    ...overrides,
  };
}
function ownerPolicy(ownerDid) {
  return {
    version: 1,
    protocol: CLOSE_CALL_POLICY_PROTOCOL,
    expectedDid: ownerDid,
    contest: CLOSE_CALL_CONTEST,
    rulesCommit: CLOSE_CALL_RULES_COMMIT,
    packageManifestSha256: CLOSE_CALL_PACKAGE_MANIFEST_SHA256,
    room: CLOSE_CALL_ROOM,
    noncePolicy: CLOSE_CALL_NONCE_POLICY,
    issuedAtMs: NOW - 1000,
    expiresAtMs: NOW + 60 * 60 * 1000,
    notAfterMs: CLOSE_CALL_LOCK_MS,
    freshMs: 30_000,
    maxSignatures: 1,
    noValueMarker: 'EXPLICIT_PAPER_NO_VALUE',
  };
}
function priceRecord(overrides = {}) {
  return {
    t: 'price',
    n: 100,
    ref: { px: '226.14', time: new Date(NOW - 100_000).toISOString(), tid: 812345 },
    limits: ['214.84', '237.44'],
    global: '226.10',
    file: 'f'.repeat(64),
    ...overrides,
  };
}
function terms(makerDid, overrides = {}) {
  return {
    id: 'first1', maker: makerDid, px: '226.14', qty: '2.00',
    side: 'sell', taker: 'any', until: 102, ...overrides,
  };
}
function request(ownerDid, maker, price, overrides = {}) {
  const canonical = stable(terms(maker.did, overrides.terms));
  const makerSig = overrides.makerSig
    ?? maker.sign(`${CLOSE_CALL_CONTEST}|terms|${canonical}`);
  const accept = `${CLOSE_CALL_CONTEST}|accept|${canonical}|${ownerDid}`;
  const value = {
    version: 1,
    kind: 'CLOSE_CALL_TYPED_SIGN_REQUEST',
    action: CLOSE_CALL_TAKER_LONG_ACTION,
    expectedDid: ownerDid,
    subject: {
      season: CLOSE_CALL_CONTEST, did: ownerDid,
      direction: 'LONG', makerSide: 'SELL',
    },
    binding: {
      contest: CLOSE_CALL_CONTEST,
      rulesCommit: CLOSE_CALL_RULES_COMMIT,
      packageManifestSha256: CLOSE_CALL_PACKAGE_MANIFEST_SHA256,
      room: CLOSE_CALL_ROOM,
      noncePolicy: CLOSE_CALL_NONCE_POLICY,
      canonicalTerms: canonical,
      termsSha256: sha(canonical),
      makerDid: maker.did,
      makerSig,
      authenticatedPrice: {
        refereeDid: signerFromSeed(REFEREE_SEED).did,
        sweep: price.n,
        ref: structuredClone(price.ref),
        limits: structuredClone(price.limits),
        referenceAgeSeconds: '100',
        stale: false,
        recordSha256: sha(price),
      },
    },
    operation: {
      atomic: true,
      steps: [
        'TAKER_COUNTERSIGN',
        'CONSTRUCT_FINAL_TRADE_JSON',
        'SIGN_CLOSE1_ROOM_MESSAGE',
      ],
      takerPreimage: { sha256: sha(accept), utf8: accept },
      finalTrade: {
        t: 'trade', season: CLOSE_CALL_CONTEST,
        terms: JSON.parse(canonical), taker: ownerDid,
        maker_sig: makerSig, taker_sig: 'SIGNER_GENERATES',
      },
      roomEnvelope: {
        room: CLOSE_CALL_ROOM,
        nonce: 'SIGNER_ALLOCATES',
        text: 'SIGNER_CONSTRUCTS_EXACT_FINAL_TRADE_JSON',
        preimageFormat: 'close1|<SIGNER_ALLOCATED_NONCE>|<EXACT_FINAL_TRADE_JSON>',
      },
    },
  };
  return value;
}
function replaceTerms(value, nextTerms, signingMaker = null) {
  const canonical = stable(nextTerms);
  const makerSig = signingMaker
    ? signingMaker.sign(`${CLOSE_CALL_CONTEST}|terms|${canonical}`)
    : value.binding.makerSig;
  const accept = `${CLOSE_CALL_CONTEST}|accept|${canonical}|${value.expectedDid}`;
  value.binding.canonicalTerms = canonical;
  value.binding.termsSha256 = sha(canonical);
  value.binding.makerDid = nextTerms.maker;
  value.binding.makerSig = makerSig;
  value.operation.takerPreimage = { sha256: sha(accept), utf8: accept };
  value.operation.finalTrade.terms = structuredClone(nextTerms);
  value.operation.finalTrade.maker_sig = makerSig;
}
function fixture({ currentPrice = priceRecord(), priceSigner = null, policy = null } = {}) {
  const owner = signerFromSeed(OWNER_SEED);
  const maker = signerFromSeed(MAKER_SEED);
  const referee = signerFromSeed(REFEREE_SEED);
  const actualPriceSigner = priceSigner ?? referee;
  const priceLine = storedLine(
    CLOSE_CALL_PRICE_ROOM, 5, NOW - 10, actualPriceSigner,
    JSON.stringify(currentPrice), '8999999999999999999',
  );
  const prior = storedLine(
    CLOSE_CALL_ROOM, 8, NOW - 20, owner, '{"t":"note"}',
    '9000000000000000000',
  );
  const reads = acquisition(
    snapshot(CLOSE_CALL_PRICE_ROOM, priceLine, owner.did),
    snapshot(CLOSE_CALL_ROOM, prior, owner.did),
  );
  const dir = mkdtempSync(join(tmpdir(), 'close-call-taker-long-'));
  const statePath = join(dir, 'state.json');
  const signer = new PolicySigner({
    seed: OWNER_SEED,
    policy: policy ?? tradePolicy(owner.did, referee.did),
    statePath,
    acquisition: reads,
    initialize: true,
    now: () => NOW,
  });
  return {
    owner, maker, referee, currentPrice, reads, dir, statePath, signer,
    request: request(owner.did, maker, currentPrice),
  };
}
function rejects(fn, code) {
  assert.throws(fn, error => error?.code === code || error?.message === code);
}

test('CLOSE_CALL_TAKER_LONG creates exactly two synthetic signatures and one POST-ready package', () => {
  const f = fixture();
  const result = f.signer.closeCallTakerLong(f.request);
  assert.equal(result.action, CLOSE_CALL_TAKER_LONG_ACTION);
  assert.equal(result.status, 'ATTEMPTED');
  assert.equal(result.retry, false);
  assert.equal(result.roomRecord.room, CLOSE_CALL_ROOM);
  assert.equal(result.roomRecord.sender, f.owner.did);
  assert.equal(result.roomRecord.nonce, '9000000000000000001');
  assert.equal(result.roomRecord.line, result.trade.text);
  assert.deepEqual(JSON.parse(result.trade.text), result.trade.object);
  assert.equal(result.trade.digest, sha(result.trade.text));
  const canonical = f.request.binding.canonicalTerms;
  assert.equal(verifyDidSignature(
    f.owner.did,
    `${CLOSE_CALL_CONTEST}|accept|${canonical}|${f.owner.did}`,
    result.takerSignature,
  ), true);
  assert.equal(verifyDidSignature(
    f.owner.did,
    canonicalMessage(CLOSE_CALL_ROOM, result.roomRecord.nonce, result.trade.text),
    result.roomRecord.signature,
  ), true);
  assert.equal(f.signer.status().signaturesUsed, 2);
  assert.equal(f.signer.status().actions[CLOSE_CALL_TAKER_LONG_ACTION].status,
    'ATTEMPTED');
});

test('trade and owner-registration policies cannot cross capabilities', () => {
  const f = fixture();
  rejects(() => f.signer.closeCallOwnerRegister({}), 'POLICY_CAPABILITY');
  const dir = mkdtempSync(join(tmpdir(), 'close-call-owner-only-'));
  const ownerOnly = new PolicySigner({
    seed: OWNER_SEED,
    policy: ownerPolicy(f.owner.did),
    statePath: join(dir, 'state.json'),
    acquisition: f.reads,
    initialize: true,
    now: () => NOW,
  });
  rejects(() => ownerOnly.closeCallTakerLong(f.request), 'POLICY_CAPABILITY');
});

test('request identity, direction, parties, maker signature and exact fields are independently checked', () => {
  const cases = [
    [value => { value.expectedDid = signerFromSeed(OTHER_SEED).did; }, 'CLOSE_CALL_REQUEST_BINDING'],
    [value => { value.subject.did = signerFromSeed(OTHER_SEED).did; }, 'CLOSE_CALL_REQUEST_BINDING'],
    [value => { value.binding.canonicalTerms = stable({ ...JSON.parse(value.binding.canonicalTerms), side: 'buy' }); }, 'CLOSE_CALL_TERMS_INVALID'],
    [value => replaceTerms(value, {
      ...JSON.parse(value.binding.canonicalTerms), maker: value.expectedDid,
    }), 'CLOSE_CALL_REQUEST_BINDING'],
    [value => { value.binding.makerSig = 'A'.repeat(85) + 'Q'; value.operation.finalTrade.maker_sig = value.binding.makerSig; }, 'CLOSE_CALL_MAKER_SIGNATURE_INVALID'],
    [value => replaceTerms(value, {
      ...JSON.parse(value.binding.canonicalTerms), qty: '2.01',
    }), 'CLOSE_CALL_MAKER_SIGNATURE_INVALID'],
    [value => replaceTerms(value, {
      ...JSON.parse(value.binding.canonicalTerms), taker: signerFromSeed(OTHER_SEED).did,
    }, signerFromSeed(MAKER_SEED)), 'CLOSE_CALL_REQUEST_BINDING'],
    [value => { value.binding.termsSha256 = '0'.repeat(64); }, 'CLOSE_CALL_REQUEST_BINDING'],
    [value => { value.extra = true; }, 'INVALID_FIELDS'],
  ];
  for (const [mutate, expected] of cases) {
    const f = fixture();
    const changed = structuredClone(f.request);
    mutate(changed);
    rejects(() => f.signer.closeCallTakerLong(changed), expected);
    assert.equal(f.signer.status().signaturesUsed, 0);
  }
});

test('wrong contest, rules, package and room are rejected', () => {
  for (const [key, value] of [
    ['contest', 'close-2'],
    ['rulesCommit', '0'.repeat(40)],
    ['packageManifestSha256', '0'.repeat(64)],
    ['room', 'other'],
  ]) {
    const f = fixture();
    const changed = structuredClone(f.request);
    changed.binding[key] = value;
    rejects(() => f.signer.closeCallTakerLong(changed),
      'CLOSE_CALL_REQUEST_BINDING');
  }
});

test('current referee price is authoritative and changed evidence requires replan', () => {
  const wrong = signerFromSeed(OTHER_SEED);
  const wrongReferee = fixture({ priceSigner: wrong });
  rejects(() => wrongReferee.signer.closeCallTakerLong(wrongReferee.request),
    'CLOSE_CALL_PRICE_UNAVAILABLE');

  const wrongBoundReferee = fixture();
  wrongBoundReferee.request.binding.authenticatedPrice.refereeDid = wrong.did;
  rejects(() => wrongBoundReferee.signer.closeCallTakerLong(wrongBoundReferee.request),
    'CLOSE_CALL_PRICE_REFEREE');

  const f = fixture();
  const changed = structuredClone(f.request);
  changed.binding.authenticatedPrice.recordSha256 = '0'.repeat(64);
  rejects(() => f.signer.closeCallTakerLong(changed),
    'CLOSE_CALL_REPLAN_REQUIRED');
});

test('missing or malformed current price evidence fails before either signature', () => {
  const missing = fixture();
  missing.reads = acquisition(
    snapshot(CLOSE_CALL_PRICE_ROOM, '', missing.owner.did),
    missing.reads.export(CLOSE_CALL_ROOM),
  );
  const missingSigner = new PolicySigner({
    seed: OWNER_SEED,
    policy: tradePolicy(missing.owner.did, missing.referee.did),
    statePath: join(missing.dir, 'missing-price-state.json'),
    acquisition: missing.reads,
    initialize: true,
    now: () => NOW,
  });
  rejects(() => missingSigner.closeCallTakerLong(missing.request),
    'CLOSE_CALL_PRICE_UNAVAILABLE');
  assert.equal(missingSigner.status().signaturesUsed, 0);

  const invalidRecord = priceRecord({ limits: ['240.00', '250.00'] });
  const invalid = fixture({ currentPrice: invalidRecord });
  rejects(() => invalid.signer.closeCallTakerLong(invalid.request),
    'CLOSE_CALL_PRICE_INVALID');
  assert.equal(invalid.signer.status().signaturesUsed, 0);
});

test('current official stale reference remains usable and is surfaced independently', () => {
  const oldPrice = priceRecord({
    ref: {
      ...priceRecord().ref,
      time: new Date(NOW - (CLOSE_CALL_EXPECTED_SWEEP_SECONDS + 1) * 1000).toISOString(),
    },
  });
  const f = fixture({ currentPrice: oldPrice });
  // This preview field is an Agent claim; the Signer recomputes current age/state.
  f.request.binding.authenticatedPrice.stale = false;
  f.request.binding.authenticatedPrice.referenceAgeSeconds = '0';
  const result = f.signer.closeCallTakerLong(f.request);
  assert.equal(result.price.referenceAgeSeconds,
    String(CLOSE_CALL_EXPECTED_SWEEP_SECONDS + 1));
  assert.equal(result.price.stale, true);
});

test('current limits, next-sweep until and lock sweep fail closed', () => {
  const outside = fixture();
  const outsideRequest = request(
    outside.owner.did, outside.maker, outside.currentPrice,
    { terms: { px: '240.00' } },
  );
  rejects(() => outside.signer.closeCallTakerLong(outsideRequest),
    'CLOSE_CALL_PRICE_LIMITS');

  const short = fixture();
  const shortRequest = request(
    short.owner.did, short.maker, short.currentPrice,
    { terms: { until: 100 } },
  );
  rejects(() => short.signer.closeCallTakerLong(shortRequest),
    'CLOSE_CALL_UNTIL');

  const lockedPrice = priceRecord({ n: 2556 });
  const locked = fixture({ currentPrice: lockedPrice });
  rejects(() => locked.signer.closeCallTakerLong(locked.request),
    'CLOSE_CALL_LOCKED');
});

test('quota, ATTEMPTED replay and restart denial are durable', () => {
  const f = fixture();
  rejects(() => new PolicySigner({
    seed: OWNER_SEED,
    policy: tradePolicy(f.owner.did, f.referee.did, { maxSignatures: 1 }),
    statePath: join(f.dir, 'bad-quota.json'),
    acquisition: f.reads,
    initialize: true,
    now: () => NOW,
  }), 'POLICY_LIMIT');

  f.signer.closeCallTakerLong(f.request);
  rejects(() => f.signer.closeCallTakerLong(f.request), 'DUPLICATE_ACTION');
  const restarted = new PolicySigner({
    seed: OWNER_SEED,
    policy: tradePolicy(f.owner.did, f.referee.did),
    statePath: f.statePath,
    acquisition: f.reads,
    now: () => NOW,
  });
  assert.equal(restarted.status().signaturesUsed, 2);
  rejects(() => restarted.closeCallTakerLong(f.request), 'DUPLICATE_ACTION');
});

test('failure after durable reservation remains non-retryable across restart', () => {
  const f = fixture();
  const statePath = join(f.dir, 'reserved-state.json');
  let writes = 0;
  const signer = new PolicySigner({
    seed: OWNER_SEED,
    policy: tradePolicy(f.owner.did, f.referee.did),
    statePath,
    acquisition: f.reads,
    initialize: true,
    now: () => NOW,
    rng: size => {
      writes += 1;
      return Buffer.alloc(writes === 3 ? size - 1 : size, writes);
    },
  });
  rejects(() => signer.closeCallTakerLong(f.request), 'STATE_RNG');

  const restarted = new PolicySigner({
    seed: OWNER_SEED,
    policy: tradePolicy(f.owner.did, f.referee.did),
    statePath,
    acquisition: f.reads,
    now: () => NOW,
  });
  assert.equal(restarted.status().signaturesUsed, 2);
  assert.equal(restarted.status().actions[CLOSE_CALL_TAKER_LONG_ACTION].status,
    'RESERVED');
  rejects(() => restarted.closeCallTakerLong(f.request), 'DUPLICATE_ACTION');
});

test('trade policy tamper and protected state corruption fail closed', () => {
  const f = fixture();
  rejects(() => new PolicySigner({
    seed: OWNER_SEED,
    policy: tradePolicy(f.owner.did, signerFromSeed(OTHER_SEED).did),
    statePath: f.statePath,
    acquisition: f.reads,
    now: () => NOW,
  }), 'STATE_BINDING');

  const envelope = JSON.parse(readFileSync(f.statePath, 'utf8'));
  envelope.ciphertext = (envelope.ciphertext[0] === 'A' ? 'B' : 'A')
    + envelope.ciphertext.slice(1);
  writeFileSync(f.statePath, JSON.stringify(envelope));
  rejects(() => new PolicySigner({
    seed: OWNER_SEED,
    policy: tradePolicy(f.owner.did, f.referee.did),
    statePath: f.statePath,
    acquisition: f.reads,
    now: () => NOW,
  }), 'STATE_CORRUPT');
});

test('trade signer exposes no generic signing or POST method', () => {
  const f = fixture();
  assert.equal('sign' in f.signer, false);
  assert.equal('post' in f.signer, false);
  assert.equal('send' in f.signer, false);
  assert.equal('seed' in f.signer, false);
});
