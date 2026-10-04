import assert from 'node:assert/strict';
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { syncBuiltinESMExports } from 'node:module';
import fs from 'node:fs';
import test, { after, before, mock } from 'node:test';
import { Readable } from 'node:stream';

import { initializeRuntimeState } from '../src/bootstrap_state.mjs';
import { runProbe } from '../deploy/runtime_probe.mjs';
import { PolicySigner } from '../src/policy_signer.mjs';
import { assertSocketActivation, loadRuntimeConfig } from '../src/runtime_service.mjs';
import { assertReaderSocketActivation } from '../src/reader_service.mjs';
import { auditedHandler, MAX_AUDIT_BYTES, RuntimeAudit } from '../src/runtime_audit.mjs';
import { handoffCredential } from '../src/credential_handoff.mjs';
import {
  exportSnapshotPath,
  paperNotePath,
  TrustedSnapshotAcquisition,
} from '../src/runtime_acquisition.mjs';
import { SYSTEMD_CREDENTIAL_NAME, withSystemdCredential } from '../src/runtime_credential.mjs';
import { createPolicySignerRpcHandler } from '../src/runtime_rpc.mjs';
import { signerFromSeed } from '../src/technocore_signing.mjs';
import { TCLK_COMMIT } from '../src/tclk_v1.mjs';

const SEED = Buffer.from('55'.repeat(32), 'hex');

// Keep canonical secret-read tests runnable on WSL without changing host proc state.
const originalReadFileSync = fs.readFileSync;
let procReadMock;
before(() => {
  procReadMock = mock.method(fs, 'readFileSync', (path, ...args) => {
    const safeProcValues = {
      '/proc/sys/kernel/osrelease': '6.6.87.2-microsoft-standard-WSL2\n',
      '/proc/sys/kernel/core_pattern': '\n',
      '/proc/sys/kernel/core_uses_pid': '0\n',
    };
    const value = safeProcValues[String(path)];
    return value === undefined
      ? originalReadFileSync.call(fs, path, ...args)
      : Buffer.from(value);
  });
  syncBuiltinESMExports();
});
after(() => {
  procReadMock.mock.restore();
  fs.readFileSync = originalReadFileSync;
  syncBuiltinESMExports();
});

test('systemd socket descriptor names reproduce the observed activation failure', () => {
  const pid = 12345;
  for (const [assertion, oldName, expectedName] of [
    [assertSocketActivation, 'flop-policy-signer.socket', 'signer'],
    [assertReaderSocketActivation, 'flop-policy-signer-reader.socket', 'reader'],
  ]) {
    const env = { LISTEN_PID: String(pid), LISTEN_FDS: '1', LISTEN_FDNAMES: oldName };
    assert.throws(() => assertion(env, pid), error => code(error, 'SOCKET_ACTIVATION_REQUIRED'));
    assert.doesNotThrow(() => assertion({ ...env, LISTEN_FDNAMES: expectedName }, pid));
  }
});

function code(error, expected) {
  return error?.code === expected || error?.message === expected;
}

function runtimePolicy(did) {
  const base = 1_900_000_000_000;
  return {
    version: 1,
    protocol: 'tclk/1',
    tclkCommit: TCLK_COMMIT,
    expectedDid: did,
    workFamily: 'math.gcd_lcm',
    jobProto: 'a2a',
    jobContextPrefix: 'TEST GCD bundle sha256=',
    issuedAtMs: base,
    expiresAtMs: base + 60 * 60 * 1000,
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

test('systemd credential loader accepts only the fixed 32-byte credential and erases its read buffer', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'signer-credential-'));
  writeFileSync(join(dir, SYSTEMD_CREDENTIAL_NAME), SEED, { mode: 0o600 });

  let borrowed;
  const result = await withSystemdCredential(dir, seed => {
    borrowed = seed;
    assert.equal(seed.equals(SEED), true);
    return 'initialized';
  });
  assert.equal(result, 'initialized');
  assert.equal(borrowed.equals(Buffer.alloc(32)), true);

  writeFileSync(join(dir, SYSTEMD_CREDENTIAL_NAME), Buffer.alloc(31), { mode: 0o600 });
  await assert.rejects(() => withSystemdCredential(dir, () => null),
    error => code(error, 'CREDENTIAL_MALFORMED'));
  await assert.rejects(() => withSystemdCredential('relative/path', () => null),
    error => code(error, 'CREDENTIAL_DIRECTORY_REQUIRED'));
});

test('trusted acquisition reads fixed hashed files and rejects writable evidence files', () => {
  const root = mkdtempSync(join(tmpdir(), 'signer-acquisition-'));
  chmodSync(root, 0o750);
  const room = 'tclk-offers';
  const contract = '0x' + 'ab'.repeat(32);
  const body = Buffer.from('{"seq":1}\n', 'utf8');

  writeFileSync(exportSnapshotPath(root, room), JSON.stringify({
    room,
    generation: 7,
    capturedAtMs: 123456,
    bodyBase64url: body.toString('base64url'),
  }), { mode: 0o640 });
  writeFileSync(paperNotePath(root, contract), 'paper-record', { mode: 0o640 });

  const acquisition = new TrustedSnapshotAcquisition(root);
  const exported = acquisition.export(room);
  assert.equal(exported.room, room);
  assert.equal(exported.generation, 7);
  assert.equal(exported.capturedAtMs, 123456);
  assert.equal(exported.body.equals(body), true);
  assert.equal(acquisition.paperNote(contract), 'paper-record');

  chmodSync(exportSnapshotPath(root, room), 0o660);
  assert.throws(() => acquisition.export(room), error => code(error, 'ACQUISITION_FILE_UNSAFE'));
});

test('live test policy refreshes and reads trusted snapshot before SOURCE_INVALID without state mutation', async () => {
  const root = mkdtempSync(join(tmpdir(), 'signer-probe-path-'));
  const acquisitionRoot = join(root, 'acquisition');
  mkdirSync(acquisitionRoot, { mode: 0o750 });
  const statePath = join(root, 'state.json');
  const auditPath = join(root, 'audit.jsonl');
  const did = signerFromSeed(SEED).did;
  const now = 1_900_000_000_100;
  const signer = new PolicySigner({
    seed: SEED, policy: runtimePolicy(did), statePath,
    acquisition: new TrustedSnapshotAcquisition(acquisitionRoot),
    initialize: true, now: () => now,
  });
  const stateBefore = readFileSync(statePath);
  const audit = new RuntimeAudit(auditPath);
  let refreshes = 0;
  const handle = auditedHandler({
    signer,
    handle: createPolicySignerRpcHandler(signer, {
      beforeRequest: async () => {
        refreshes += 1;
        writeFileSync(exportSnapshotPath(acquisitionRoot, 'tclk-offers'), JSON.stringify({
          room: 'tclk-offers', generation: refreshes, capturedAtMs: now,
          bodyBase64url: '',
        }), { mode: 0o600 });
      },
    }),
    audit,
    clock: () => now,
  });
  try {
    assert.deepEqual(await runProbe('signer-reader-refresh', (_, request) => handle(request)),
      { readerRefreshed: true, outcome: 'SOURCE_INVALID' });
  } finally {
    audit.close();
  }
  assert.equal(refreshes, 1);
  assert.deepEqual(readFileSync(statePath), stateBefore);
  assert.equal(signer.status().revision, 0);
  assert.equal(signer.status().signaturesUsed, 0);
  assert.equal(signer.status().work, null);
  assert.deepEqual(signer.status().actions, {});
  const entries = readFileSync(auditPath, 'utf8').trim().split('\n').map(JSON.parse);
  assert.deepEqual(entries.map(({ phase, method, ok, code, revision }) =>
    ({ phase, method, ok, code, revision })), [
    { phase: 'BEGIN', method: 'admit', ok: null, code: null, revision: 0 },
    { phase: 'END', method: 'admit', ok: false, code: 'SOURCE_INVALID', revision: 0 },
  ]);
});

test('typed RPC exposes bounded signer operations and no arbitrary signing method', async () => {
  const calls = [];
  const signer = {
    status: () => ({ did: 'did:key:test' }),
    admit: value => { calls.push(['admit', value]); return { admitted: true }; },
    issue: type => { calls.push(['issue', type]); return { type }; },
    reconcile: type => { calls.push(['reconcile', type]); return { status: 'OBSERVED' }; },
    observeCompletion: () => { calls.push(['completion']); return { status: 'COMPLETED' }; },
    closeCallOwnerRegister: value => {
      calls.push(['close-call-owner-register', value]);
      return { signed: true };
    },
    closeCallTakerLong: value => {
      calls.push(['close-call-taker-long', value]);
      return { action: 'CLOSE_CALL_TAKER_LONG', request: value };
    },
  };
  const handle = createPolicySignerRpcHandler(signer);

  assert.deepEqual(await handle({ method: 'status', value: {} }), { did: 'did:key:test' });
  const source = Buffer.from('gcd_lcm 6 42\n');
  assert.deepEqual(await handle({
    method: 'admit',
    value: { offerSeq: 9, sourceBase64url: source.toString('base64url') },
  }), { admitted: true });
  assert.equal(calls[0][1].sourceBytes.equals(source), true);

  const closeCallRequest = {
    version: 1,
    kind: 'CLOSE_CALL_TYPED_SIGN_REQUEST',
    action: 'CLOSE_CALL_OWNER_REGISTER',
    expectedDid: 'did:key:test',
    subject: { season: 'close-1', did: 'did:key:test' },
    binding: {
      contest: 'close-1',
      rulesCommit: 'r',
      packageManifestSha256: 'p',
      room: 'close1',
      noncePolicy: 'SIGNER_ALLOCATES_ROOM_NONCE',
    },
    preview: { sha256: 'h', utf8: 'owner' },
  };
  assert.deepEqual(await handle({
    method: 'closeCallOwnerRegister',
    value: closeCallRequest,
  }), { signed: true });
  assert.equal(calls.at(-1)[0], 'close-call-owner-register');

  const takerLongRequest = {
    version: 1,
    kind: 'CLOSE_CALL_TYPED_SIGN_REQUEST',
    action: 'CLOSE_CALL_TAKER_LONG',
    expectedDid: 'did:key:test',
    subject: {}, binding: {}, operation: {},
  };
  assert.equal((await handle({
    method: 'closeCallTakerLong', value: takerLongRequest,
  })).action, 'CLOSE_CALL_TAKER_LONG');
  assert.equal(calls.at(-1)[0], 'close-call-taker-long');

  assert.deepEqual(await handle({ method: 'issue', value: { type: 'ACCEPT' } }), { type: 'ACCEPT' });
  await assert.rejects(() => handle({ method: 'issue', value: { type: 'SIGN_TEXT' } }),
    error => code(error, 'RPC_ACTION_DENIED'));
  await assert.rejects(() => handle({ method: 'sign', value: { text: 'anything' } }),
    error => code(error, 'RPC_METHOD_DENIED'));
  await assert.rejects(() => handle({ method: 'status', value: { extra: true } }),
    error => code(error, 'RPC_INVALID'));
});

test('runtime state bootstrap is explicit, one-shot and separate from the runtime service', async () => {
  const root = mkdtempSync(join(tmpdir(), 'signer-bootstrap-'));
  const credentialDirectory = join(root, 'credentials');
  const acquisitionRoot = join(root, 'acquisition');
  const statePath = join(root, 'state', 'state.json');
  const configPath = join(root, 'config.json');
  mkdirSync(credentialDirectory, { mode: 0o700 });
  mkdirSync(acquisitionRoot, { mode: 0o750 });
  writeFileSync(join(credentialDirectory, SYSTEMD_CREDENTIAL_NAME), SEED, { mode: 0o600 });

  const did = signerFromSeed(SEED).did;
  writeFileSync(configPath, JSON.stringify({
    version: 1,
    policy: runtimePolicy(did),
    acquisitionRoot,
  }), { mode: 0o600 });

  const status = await initializeRuntimeState({ configPath, statePath, credentialDirectory });
  assert.equal(status.did, did);
  assert.equal(existsSync(statePath), true);

  await assert.rejects(
    () => initializeRuntimeState({ configPath, statePath, credentialDirectory }),
    error => code(error, 'STATE_ALREADY_EXISTS'),
  );
});


test('one-time handoff validates the DID, stores only encrypted bytes and erases the borrowed seed', async () => {
  const root = mkdtempSync(join(tmpdir(), 'signer-handoff-'));
  const acquisitionRoot = join(root, 'acquisition');
  const configPath = join(root, 'config.json');
  const outputPath = join(root, 'project-seed.cred');
  mkdirSync(acquisitionRoot, { mode: 0o750 });
  const did = signerFromSeed(SEED).did;
  writeFileSync(configPath, JSON.stringify({
    version: 1,
    policy: runtimePolicy(did),
    acquisitionRoot,
  }), { mode: 0o600 });

  let borrowed;
  const result = await handoffCredential({
    input: Readable.from([SEED]),
    outputPath,
    configPath,
    encrypt: seed => {
      borrowed = seed;
      assert.equal(seed.equals(SEED), true);
      return Buffer.from('encrypted-fixture');
    },
  });
  assert.equal(result.did, did);
  assert.equal(readFileSync(outputPath, 'utf8'), 'encrypted-fixture');
  assert.equal(borrowed.equals(Buffer.alloc(32)), true);

  await assert.rejects(() => handoffCredential({
    input: Readable.from([SEED]),
    outputPath,
    configPath,
    encrypt: () => Buffer.from('second'),
  }), error => code(error, 'CREDENTIAL_ALREADY_EXISTS'));
});


test('runtime audit records only sanitized request metadata and revisions', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'signer-audit-'));
  const auditPath = join(dir, 'audit.jsonl');
  const audit = new RuntimeAudit(auditPath);
  let revision = 4;
  const signer = { status: () => ({ revision }) };
  const handle = auditedHandler({
    signer,
    audit,
    clock: () => 123,
    handle: async request => {
      assert.equal(request.value.sourceBase64url, 'c2Vuc2l0aXZl');
      revision = 5;
      return { admitted: true };
    },
  });

  await handle({
    method: 'admit',
    value: { offerSeq: 1, sourceBase64url: 'c2Vuc2l0aXZl' },
  });
  audit.close();

  const raw = readFileSync(auditPath, 'utf8');
  assert.equal(raw.includes('c2Vuc2l0aXZl'), false);
  assert.equal(raw.includes('sourceBase64url'), false);
  const rows = raw.trim().split('\n').map(line => JSON.parse(line));
  assert.deepEqual(rows, [
    { atMs: 123, phase: 'BEGIN', method: 'admit', action: null, ok: null, code: null, revision: 4 },
    { atMs: 123, phase: 'END', method: 'admit', action: null, ok: true, code: null, revision: 5 },
  ]);
});


test('validated signer RPC refresh hook runs before state-dependent issue calls', async () => {
  const events = [];
  const signer = {
    status: () => ({ revision: 1, work: { contract: '0x' + 'ab'.repeat(32) } }),
    admit: () => { events.push('admit'); return {}; },
    issue: type => { events.push('issue:' + type); return {}; },
    reconcile: type => { events.push('reconcile:' + type); return {}; },
    observeCompletion: () => { events.push('completion'); return {}; },
    closeCallOwnerRegister: () => { events.push('close-call-owner-register'); return {}; },
    closeCallTakerLong: () => { events.push('close-call-taker-long'); return {}; },
  };
  const handle = createPolicySignerRpcHandler(signer, {
    beforeRequest: async info => { events.push('refresh:' + info.method + ':' + (info.action ?? '-')); },
  });

  await handle({ method: 'closeCallOwnerRegister', value: {
    version: 1,
    kind: 'CLOSE_CALL_TYPED_SIGN_REQUEST',
    action: 'CLOSE_CALL_OWNER_REGISTER',
    expectedDid: 'did:key:test',
    subject: { season: 'close-1', did: 'did:key:test' },
    binding: {
      contest: 'close-1', rulesCommit: 'r', packageManifestSha256: 'p',
      room: 'close1', noncePolicy: 'SIGNER_ALLOCATES_ROOM_NONCE',
    },
    preview: { sha256: 'h', utf8: 'owner' },
  } });
  assert.deepEqual(events, [
    'refresh:closeCallOwnerRegister:CLOSE_CALL_OWNER_REGISTER',
    'close-call-owner-register',
  ]);

  events.length = 0;
  await handle({ method: 'closeCallTakerLong', value: {
    version: 1,
    kind: 'CLOSE_CALL_TYPED_SIGN_REQUEST',
    action: 'CLOSE_CALL_TAKER_LONG',
    expectedDid: 'did:key:test',
    subject: {}, binding: {}, operation: {},
  } });
  assert.deepEqual(events, [
    'refresh:closeCallTakerLong:CLOSE_CALL_TAKER_LONG',
    'close-call-taker-long',
  ]);

  events.length = 0;
  await handle({ method: 'issue', value: { type: 'DELIVERY_GCD' } });
  assert.deepEqual(events, ['refresh:issue:DELIVERY_GCD', 'issue:DELIVERY_GCD']);

  events.length = 0;
  await assert.rejects(
    () => handle({ method: 'issue', value: { type: 'SIGN_TEXT' } }),
    error => code(error, 'RPC_ACTION_DENIED'),
  );
  assert.deepEqual(events, []);
});

test('Taker-Long refreshes close1 before the final authenticated price read', () => {
  const source = readFileSync(new URL('../src/runtime_service.mjs', import.meta.url), 'utf8');
  const block = source.match(
    /if \(method === 'closeCallTakerLong'\) \{([\s\S]*?)\n      \}/,
  )?.[1];
  assert.equal(typeof block, 'string');
  const close1 = block.indexOf("method: 'refreshCloseCallRegistration'");
  const price = block.indexOf("method: 'refreshCloseCallPrice'");
  assert.ok(close1 >= 0 && price > close1);
});


test('runtime audit sanitizes invalid vocabulary and stops before unbounded growth', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'signer-audit-bound-'));
  const auditPath = join(dir, 'audit.jsonl');
  const audit = new RuntimeAudit(auditPath);
  const signer = { status: () => ({ revision: 0 }) };
  const handle = auditedHandler({
    signer,
    audit,
    clock: () => 456,
    handle: async () => { throw Object.assign(new Error('RPC_METHOD_DENIED'), { code: 'RPC_METHOD_DENIED' }); },
  });

  await assert.rejects(
    () => handle({ method: 'X'.repeat(8000), value: { type: 'Y'.repeat(4000) } }),
    error => code(error, 'RPC_METHOD_DENIED'),
  );
  audit.close();
  const first = JSON.parse(readFileSync(auditPath, 'utf8').trim().split('\n')[0]);
  assert.equal(first.method, 'INVALID');
  assert.equal(first.action, null);
  assert.equal(readFileSync(auditPath).length < 1024, true);

  writeFileSync(auditPath, Buffer.alloc(MAX_AUDIT_BYTES - 8), { mode: 0o600 });
  const full = new RuntimeAudit(auditPath);
  assert.throws(
    () => full.record({ atMs: 1, phase: 'BEGIN', method: 'issue', action: 'ACCEPT', ok: null, code: null, revision: 0 }),
    error => code(error, 'AUDIT_FULL'),
  );
  full.close();
});


test('runtime config rejects writable files, symlinks and unexpected owners', () => {
  const root = mkdtempSync(join(tmpdir(), 'signer-config-'));
  const acquisitionRoot = join(root, 'acquisition');
  mkdirSync(acquisitionRoot, { mode: 0o750 });
  const did = signerFromSeed(SEED).did;
  const configPath = join(root, 'config.json');
  const body = JSON.stringify({
    version: 1,
    policy: runtimePolicy(did),
    acquisitionRoot,
  });

  writeFileSync(configPath, body, { mode: 0o600 });
  assert.equal(loadRuntimeConfig(configPath).policy.expectedDid, did);

  chmodSync(configPath, 0o620);
  assert.throws(
    () => loadRuntimeConfig(configPath),
    error => code(error, 'RUNTIME_CONFIG_UNSAFE'),
  );

  chmodSync(configPath, 0o600);
  const linkPath = join(root, 'config-link.json');
  symlinkSync(configPath, linkPath);
  assert.throws(
    () => loadRuntimeConfig(linkPath),
    error => code(error, 'RUNTIME_CONFIG_UNSAFE'),
  );

  const statUid = process.getuid();
  assert.throws(
    () => loadRuntimeConfig(configPath, { expectedUid: statUid + 1 }),
    error => code(error, 'RUNTIME_CONFIG_UNSAFE'),
  );
});


test('audit capacity is reserved for END before any state mutation begins', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'signer-audit-reserve-'));
  const auditPath = join(dir, 'audit.jsonl');
  const begin = {
    atMs: 1,
    phase: 'BEGIN',
    method: 'issue',
    action: 'ACCEPT',
    ok: null,
    code: null,
    revision: 0,
  };
  const beginBytes = Buffer.byteLength(JSON.stringify(begin) + '\n', 'utf8');

  // Leave enough room for BEGIN itself, but not for any meaningful END line.
  // The old behavior would write BEGIN, mutate state, then fail on END.
  writeFileSync(auditPath, Buffer.alloc(MAX_AUDIT_BYTES - beginBytes - 1), { mode: 0o600 });

  const audit = new RuntimeAudit(auditPath);
  let revision = 0;
  let mutated = false;
  const signer = { status: () => ({ revision }) };
  const handle = auditedHandler({
    signer,
    audit,
    clock: () => 1,
    handle: async () => {
      mutated = true;
      revision += 1;
      return { signed: true };
    },
  });

  await assert.rejects(
    () => handle({ method: 'issue', value: { type: 'ACCEPT' } }),
    error => code(error, 'AUDIT_FULL'),
  );
  assert.equal(mutated, false);
  assert.equal(revision, 0);
  assert.equal(readFileSync(auditPath).length, MAX_AUDIT_BYTES - beginBytes - 1);
  audit.close();
});
