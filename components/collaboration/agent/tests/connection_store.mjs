// Dummy-only storage tests. No real key, preimage, signature, or signed frame is used or printed.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { mkdtempSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { spawn } from 'node:child_process';
import { ConnectionStore } from '../src/collaboration_agent/connection_store.mjs';

const DID = 'did:key:z6MkjQVeF7TCAC6H6LKQnYFannWhsfWzzHhDDr3UhbZgHSTL';
const CONTRACT = '0x' + 'ab'.repeat(32);
const ACTION_A = 'a'.repeat(64), ACTION_B = 'b'.repeat(64);
const roots = new Set();
function parent() { const path = mkdtempSync(resolve(tmpdir(), 'connection-store-test-')); roots.add(path); return path; }
function params(overrides = {}) {
  return { key: randomBytes(32), did: DID, contract: CONTRACT, preimage: Buffer.alloc(32, 0x31),
    context: { sourceDigest: 'c'.repeat(64), answer: 'dummy deterministic answer' }, ...overrides };
}
function code(error) { return error?.code ?? error?.message; }
test.afterEach(async () => { for (const path of roots) await rm(path, { recursive: true, force: true }); roots.clear(); });

test('encrypted preimage is durable, contract-bound, recoverable, and absent from ordinary files', async () => {
  const base = parent(), path = resolve(base, 'store'), p = params();
  const store = await ConnectionStore.initialize(path, p);
  const expected = '0x' + createHash('sha256').update(p.preimage).digest('hex');
  assert.deepEqual(store.assertAcceptReady(), { verified: true, commitment: expected, did: DID, contract: CONTRACT });
  assert.deepEqual(store.context(), p.context);
  const callbackDigest = await store.withPreimage(value => createHash('sha256').update(value).digest('hex'));
  assert.equal('0x' + callbackDigest, expected);
  const rawSecretHex = p.preimage.toString('hex'), rawSecretBase64 = p.preimage.toString('base64');
  for (const name of ['connection-state.enc', 'connection-highwater.json', 'connection-pointer.json']) {
    const bytes = readFileSync(resolve(path, name));
    assert(!bytes.includes(rawSecretHex)); assert(!bytes.includes(rawSecretBase64));
    assert.equal(statSync(resolve(path, name)).mode & 0o077, 0);
  }
  assert.equal(statSync(path).mode & 0o077, 0);
  assert.deepEqual(ConnectionStore.readPointer(path), { version: 1, did: DID, contract: CONTRACT, commitment: expected });
  await store.close();
  const reopened = await ConnectionStore.reopen(path, { key: p.key });
  assert.equal(reopened.preimageCommitment(), expected); assert.deepEqual(reopened.context(), p.context);
  await reopened.close();
});

test('ACCEPT readiness re-reads durable state and blocks post-prepare corruption', async () => {
  const base = parent(), path = resolve(base, 'store'), p = params();
  const store = await ConnectionStore.initialize(path, p);
  assert.equal(store.assertAcceptReady().verified, true);
  writeFileSync(resolve(path, 'connection-highwater.json'), Buffer.from('corrupted'));
  assert.throws(() => store.assertAcceptReady(),
    error => code(error) === 'WITNESS_MISSING_OR_CORRUPT');
  await store.close();
});

test('initialize and reopen are explicit; missing, duplicate, wrong key, and binding mismatch fail closed', async () => {
  const base = parent(), path = resolve(base, 'store'), p = params();
  await assert.rejects(ConnectionStore.reopen(path, { key: p.key }), error => code(error) === 'STORE_MISSING');
  const store = await ConnectionStore.initialize(path, p);
  await assert.rejects(ConnectionStore.initialize(path, p), error => code(error) === 'STORE_ALREADY_EXISTS');
  await assert.rejects(ConnectionStore.reopen(path, { key: p.key }),
    error => code(error) === 'STORE_LOCKED_RECONCILIATION_REQUIRED');
  await store.close();
  await assert.rejects(ConnectionStore.reopen(path, { key: randomBytes(32) }),
    error => code(error) === 'STORE_CORRUPT_OR_WRONG_KEY');
  await assert.rejects(ConnectionStore.reopen(path, { key: p.key, contract: '0x' + 'cd'.repeat(32) }),
    error => code(error) === 'POINTER_BINDING_MISMATCH');
});

test('context is immutable to callers and secret-bearing context fields are rejected', async () => {
  const base = parent(), p = params(), path = resolve(base, 'store');
  const store = await ConnectionStore.initialize(path, p);
  const context = store.context(); context.answer = 'changed';
  assert.equal(store.context().answer, p.context.answer); await store.close();
  await assert.rejects(ConnectionStore.initialize(resolve(base, 'bad'), params({ context: { preimage: 'dummy' } })),
    error => code(error) === 'INVALID_CONTEXT');
  await assert.rejects(ConnectionStore.initialize(resolve(base, 'bad2'), params({ context: { private_key: 'dummy' } })),
    error => code(error) === 'INVALID_CONTEXT');
});

test('nonce refuses unverified live state and reserves exact BigInt high-water independently by room', async () => {
  const base = parent(), path = resolve(base, 'store'), p = params();
  const store = await ConnectionStore.initialize(path, p);
  await assert.rejects(store.reserveNonce('tclk-offers', { actionDigest: ACTION_A, nowMs: 1_800_000_000_000 }),
    error => code(error) === 'CURRENT_LIVE_STATE_UNVERIFIED');
  await store.verifyLiveRoom('tclk-offers', { observedNonce: '1800000000000000001', verifiedAtMs: 1_800_000_000_000 });
  assert.equal(await store.reserveNonce('tclk-offers', { actionDigest: ACTION_A, nowMs: 1_800_000_000_000 }),
    '1800000000000000002');
  await store.verifyLiveRoom('deal-room', { observedNone: true, verifiedAtMs: 1_800_000_000_000 });
  assert.equal(await store.reserveNonce('deal-room', { actionDigest: ACTION_B, nowMs: 1_800_000_000_000 }),
    '1800000000000');
  await store.close();
});

test('observed-none verification preserves prior high-water and nonce exhaustion fails closed', async () => {
  const base = parent(), path = resolve(base, 'store'), p = params();
  const store = await ConnectionStore.initialize(path, p);
  await store.verifyLiveRoom('tclk-offers', { observedNonce: '9000000000000000000', verifiedAtMs: 10 });
  await store.verifyLiveRoom('tclk-offers', { observedNone: true, verifiedAtMs: 11 });
  assert.equal(store.roomStatus('tclk-offers').observedHighwater, '9000000000000000000');
  assert.equal(await store.reserveNonce('tclk-offers', { actionDigest: ACTION_A, nowMs: 12 }), '9000000000000000001');
  await store.close();

  const path2 = resolve(base, 'exhausted'), exhausted = await ConnectionStore.initialize(path2, params());
  await exhausted.verifyLiveRoom('tclk-offers', { observedNonce: '9999999999999999999', verifiedAtMs: 10 });
  await assert.rejects(exhausted.reserveNonce('tclk-offers', { actionDigest: ACTION_A, nowMs: 12 }),
    error => code(error) === 'NONCE_EXHAUSTED');
  await assert.rejects(exhausted.verifyLiveRoom('other-room', {
    observedNonce: '10000000000000000000', verifiedAtMs: 10,
  }), error => code(error) === 'LOSSLESS_NONCE_REQUIRED');
  await exhausted.close();
});

test('reserved and ambiguous nonce block concurrency and all retry until explicit reconciliation', async () => {
  const base = parent(), path = resolve(base, 'store'), p = params();
  const store = await ConnectionStore.initialize(path, p);
  await store.verifyLiveRoom('tclk-offers', { observedNone: true, verifiedAtMs: 10 });
  const concurrent = await Promise.allSettled([
    store.reserveNonce('tclk-offers', { actionDigest: ACTION_A, nowMs: 20 }),
    store.reserveNonce('tclk-offers', { actionDigest: ACTION_B, nowMs: 21 }),
  ]);
  assert.deepEqual(concurrent.map(result => result.status), ['fulfilled', 'rejected']);
  assert.equal(code(concurrent[1].reason), 'NONCE_RECONCILIATION_REQUIRED');
  const nonce = concurrent[0].value;
  await assert.rejects(store.reserveNonce('tclk-offers', { actionDigest: ACTION_B, nowMs: 21 }),
    error => code(error) === 'NONCE_RECONCILIATION_REQUIRED');
  await store.markSendAttempted('tclk-offers', nonce); await store.markAmbiguous('tclk-offers', nonce);
  await assert.rejects(store.reserveNonce('tclk-offers', { actionDigest: ACTION_B, nowMs: 22 }),
    error => code(error) === 'NONCE_RECONCILIATION_REQUIRED');
  await store.close();
  const reopened = await ConnectionStore.reopen(path, { key: p.key });
  assert.equal(reopened.roomStatus('tclk-offers').reservations[0].status, 'AMBIGUOUS');
  await assert.rejects(reopened.reserveNonce('tclk-offers', { actionDigest: ACTION_B, nowMs: 23 }),
    error => code(error) === 'NONCE_RECONCILIATION_REQUIRED');
  await reopened.reconcile('tclk-offers', nonce, { observed: false });
  await assert.rejects(reopened.reserveNonce('tclk-offers', { actionDigest: ACTION_A, nowMs: 24 }),
    error => code(error) === 'ACTION_NONCE_ALREADY_USED');
  assert.equal(await reopened.reserveNonce('tclk-offers', { actionDigest: ACTION_B, nowMs: 24 }), '24');
  await reopened.close();
});

test('nonce reservation survives restart and never rolls back below durable high-water', async () => {
  const base = parent(), path = resolve(base, 'store'), p = params();
  let store = await ConnectionStore.initialize(path, p);
  await store.verifyLiveRoom('tclk-offers', { observedNone: true, verifiedAtMs: 10 });
  const first = await store.reserveNonce('tclk-offers', { actionDigest: ACTION_A, nowMs: 100 });
  await store.markSendAttempted('tclk-offers', first); await store.markObserved('tclk-offers', first);
  await store.close();
  store = await ConnectionStore.reopen(path, { key: p.key });
  assert.equal(await store.reserveNonce('tclk-offers', { actionDigest: ACTION_B, nowMs: 50 }), '101');
  await store.close();
});

test('state-only and witness-only rollback/corruption are detected', async () => {
  for (const rollback of ['state', 'witness']) {
    const base = parent(), path = resolve(base, 'store'), p = params();
    const store = await ConnectionStore.initialize(path, p);
    const oldState = readFileSync(resolve(path, 'connection-state.enc'));
    const oldWitness = readFileSync(resolve(path, 'connection-highwater.json'));
    await store.verifyLiveRoom('tclk-offers', { observedNone: true, verifiedAtMs: 10 }); await store.close();
    writeFileSync(resolve(path, rollback === 'state' ? 'connection-state.enc' : 'connection-highwater.json'),
      rollback === 'state' ? oldState : oldWitness);
    await assert.rejects(ConnectionStore.reopen(path, { key: p.key }), error =>
      ['STORE_ROLLBACK_OR_CORRUPTION', 'STORE_CORRUPT_OR_WRONG_KEY'].includes(code(error)));
  }
});

test('missing or corrupted durable components fail closed', async () => {
  const base = parent(), path = resolve(base, 'store'), p = params();
  const store = await ConnectionStore.initialize(path, p); await store.close();
  writeFileSync(resolve(path, 'connection-state.enc'), Buffer.from('not-json'));
  await assert.rejects(ConnectionStore.reopen(path, { key: p.key }),
    error => code(error) === 'STORE_MISSING_OR_CORRUPT');
});

test('fsync failure cannot enable recovery readiness and later verified reopen recovers', async () => {
  const base = parent(), path = resolve(base, 'store'), p = params();
  let stateWrites = 0;
  const failRecoveryFsync = (target, phase) => {
    if (target === 'connection-state.enc' && phase === 'before-file-fsync' && ++stateWrites === 2)
      throw new Error('TEST_FSYNC_FAILURE');
  };
  await assert.rejects(ConnectionStore.initialize(path, { ...p, testFault: failRecoveryFsync }),
    { message: 'TEST_FSYNC_FAILURE' });
  const alwaysFailStateFsync = (target, phase) => {
    if (target === 'connection-state.enc' && phase === 'before-file-fsync') throw new Error('TEST_FSYNC_FAILURE');
  };
  await assert.rejects(ConnectionStore.reopen(path, { key: p.key, testFault: alwaysFailStateFsync }),
    { message: 'TEST_FSYNC_FAILURE' });
  const recovered = await ConnectionStore.reopen(path, { key: p.key });
  assert.equal(recovered.assertAcceptReady().verified, true); await recovered.close();
});

test('SIGKILL leaves lock; explicit reconciliation reopens same preimage and reservation', async () => {
  const base = parent(), path = resolve(base, 'store'), p = params();
  const script = `process.once('message',async m=>{try{`
    + `const {ConnectionStore}=await import('./src/collaboration_agent/connection_store.mjs');`
    + `const s=await ConnectionStore.initialize(m.path,{key:Buffer.from(m.key,'hex'),did:m.did,contract:m.contract,preimage:Buffer.from(m.preimage,'hex')});`
    + `await s.verifyLiveRoom('tclk-offers',{observedNone:true,verifiedAtMs:10});`
    + `const nonce=await s.reserveNonce('tclk-offers',{actionDigest:'a'.repeat(64),nowMs:20});`
    + `process.send({ok:true,commitment:s.preimageCommitment(),nonce},()=>process.kill(process.pid,'SIGKILL'));`
    + `}catch{process.send({ok:false},()=>process.exit(70));}});`;
  const child = spawn(process.execPath, ['--input-type=module', '-e', script], {
    cwd: resolve(import.meta.dirname, '..'), stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
  });
  let outputBytes = 0;
  child.stdout.on('data', chunk => { outputBytes += chunk.length; });
  child.stderr.on('data', chunk => { outputBytes += chunk.length; });
  const message = new Promise((resolveMessage, reject) => {
    child.once('message', resolveMessage); child.once('error', reject);
  });
  const exited = new Promise((resolveExit, reject) => {
    child.once('exit', (status, signal) => resolveExit({ status, signal })); child.once('error', reject);
  });
  child.send({ path, key: p.key.toString('hex'), preimage: p.preimage.toString('hex'), did: DID, contract: CONTRACT });
  const ready = await message, exit = await exited;
  assert.equal(ready.ok, true); assert.equal(exit.status, null); assert.equal(exit.signal, 'SIGKILL');
  assert.equal(outputBytes, 0);
  const owner = JSON.parse(readFileSync(resolve(path, 'connection-store.lock'), 'utf8')).pid;
  assert.equal(owner, child.pid);
  await assert.rejects(ConnectionStore.reopen(path, { key: p.key }),
    error => code(error) === 'STORE_LOCKED_RECONCILIATION_REQUIRED');
  await assert.rejects(ConnectionStore.recoverStaleLock(path, { expectedPid: owner, stateReconciled: false }),
    error => code(error) === 'STALE_LOCK_RECOVERY_NOT_AUTHORIZED');
  await ConnectionStore.recoverStaleLock(path, { expectedPid: owner, stateReconciled: true });
  const reopened = await ConnectionStore.reopen(path, { key: p.key });
  assert.equal(reopened.preimageCommitment(), ready.commitment);
  assert.equal(reopened.roomStatus('tclk-offers').reservations[0].nonce, ready.nonce);
  assert.equal(reopened.roomStatus('tclk-offers').reservations[0].status, 'RESERVED');
  await reopened.close();
});
