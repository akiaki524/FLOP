import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { durabilityProbe } from '../deploy/durability_probe.mjs';
import { identityFromSeed } from '../deploy/test_identity.mjs';
import { offlineStatus } from '../deploy/offline_state_status.mjs';
import { runProbe } from '../deploy/runtime_probe.mjs';
import { PolicySigner } from '../src/policy_signer.mjs';
import { signerFromSeed } from '../src/technocore_signing.mjs';
import { TCLK_COMMIT } from '../src/tclk_v1.mjs';

const TEST_SEED = Buffer.from('55'.repeat(32), 'hex');

function policy(did) {
  const now = 1_900_000_000_000;
  return {
    version: 1,
    protocol: 'tclk/1',
    tclkCommit: TCLK_COMMIT,
    expectedDid: did,
    workFamily: 'math.gcd_lcm',
    jobProto: 'a2a',
    jobContextPrefix: 'TEST GCD bundle sha256=',
    issuedAtMs: now - 60_000,
    expiresAtMs: now + 60 * 60 * 1000,
    freshMs: 30_000,
    acceptMarginMs: 60_000,
    minCompletionWindowMs: 60_000,
    claimMarginMs: 120_000,
    refundGapMs: 60_000,
    maxWorkItems: 1,
    maxSignatures: 4,
    requireHeartbeat: true,
    noValueMarker: 'EXPLICIT_PAPER_NO_VALUE',
  };
}

test('test identity helper derives only DID and pinned tclk commit', () => {
  const value = identityFromSeed(TEST_SEED);
  assert.equal(typeof value.did, 'string');
  assert.equal(value.did.startsWith('did:key:z6Mk'), true);
  assert.deepEqual(Object.keys(value).sort(), ['did', 'tclkCommit']);
  assert.equal(value.tclkCommit, TCLK_COMMIT);
});

test('runtime rehearsal probe denies generic or signing commands', async () => {
  for (const command of ['sign', 'issue', 'accept', 'reader-url', 'anything']) {
    await assert.rejects(
      () => runProbe(command),
      error => error?.code === 'PROBE_COMMAND_DENIED',
    );
  }
});

test('Signer-to-Reader rehearsal request reaches policy denial without admitting work', async () => {
  const calls = [];
  const result = await runProbe('signer-reader-refresh', async (socket, request) => {
    calls.push({ socket, request });
    const error = new Error('SOURCE_INVALID');
    error.code = 'SOURCE_INVALID';
    throw error;
  });
  assert.deepEqual(result, { readerRefreshed: true, outcome: 'SOURCE_INVALID' });
  assert.deepEqual(calls, [{
    socket: '/run/flop-policy-signer.sock',
    request: {
      method: 'admit',
      value: { offerSeq: Number.MAX_SAFE_INTEGER, sourceBase64url: 'AA' },
    },
  }]);
  await assert.rejects(() => runProbe('signer-reader-refresh', async () => {
    const error = new Error('POLICY_EXPIRED');
    error.code = 'POLICY_EXPIRED';
    throw error;
  }), error => error?.code === 'POLICY_EXPIRED');
  await assert.rejects(
    () => runProbe('signer-reader-refresh', async () => {
      const error = new Error('READER_HTTP_FAILURE');
      error.code = 'READER_HTTP_FAILURE';
      throw error;
    }),
    error => error?.code === 'READER_HTTP_FAILURE',
  );
});

test('offline state helper reads protected state without acquisition or live service', () => {
  const root = mkdtempSync(join(tmpdir(), 'signer-offline-status-'));
  const statePath = join(root, 'state.json');
  const configPath = join(root, 'config.json');
  const did = signerFromSeed(TEST_SEED).did;
  const p = policy(did);
  const unavailable = () => { throw new Error('SHOULD_NOT_READ_ACQUISITION'); };

  new PolicySigner({
    seed: TEST_SEED,
    policy: p,
    statePath,
    acquisition: { export: unavailable, paperNote: unavailable },
    initialize: true,
    now: () => 1_900_000_000_000,
  });
  writeFileSync(configPath, JSON.stringify({
    version: 1,
    policy: p,
    acquisitionRoot: join(root, 'unused'),
  }), { mode: 0o600 });

  const status = offlineStatus(TEST_SEED, { configPath, statePath });
  assert.equal(status.did, did);
  assert.equal(status.revision, 0);
  assert.equal(status.signaturesUsed, 0);
  assert.equal(status.work, null);
  assert.deepEqual(status.actions, {});
});

test('offline state helper rejects malformed seed', () => {
  assert.throws(
    () => offlineStatus(Buffer.alloc(31), { configPath: '/x', statePath: '/y' }),
    error => error?.code === 'TEST_SEED_LENGTH',
  );
});


test('durability probe performs file and directory fsync without Node permission model', () => {
  const root = mkdtempSync(join(tmpdir(), 'signer-durability-probe-'));
  const result = durabilityProbe(root);
  assert.deepEqual(result, { ok: true, fileFsync: true, directoryFsync: true });
});
