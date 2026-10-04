import assert from 'node:assert/strict';
import { closeSync, mkdtempSync, mkdirSync, openSync, readFileSync, rmSync, statSync,
  writeFileSync } from 'node:fs';
import { Readable } from 'node:stream';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { official } from '../src/collaboration_agent/pilot_protocol.mjs';
import { PROJECT_DID } from '../src/collaboration_agent/connection_approval.mjs';
import { REAL_CREDENTIAL_NAME, withRealCredentialFromDirectory,
  validateRealCredential } from '../src/collaboration_agent/real_credential.mjs';
import { assertProductionCredentialDestination, cliInput, handoffRealCredential,
  readRawSeed } from '../deploy/connection/handoff_real.mjs';

if (process.argv[2] === '--anonymous-pipe-probe') {
  try {
    const fd = process.argv[3];
    const input = fd === undefined ? cliInput([]) : cliInput(['--input-fd', fd]);
    const probeSeed = await readRawSeed(input);
    probeSeed.fill(0);
    process.exit(0);
  } catch {
    process.exit(1);
  }
}

const root = mkdtempSync(resolve(tmpdir(), 'collab-real-credential-'));
let passed = 0;
const check = async fn => { await fn(); passed++; };
const denied = async (fn, code) => {
  await assert.rejects(fn, error => error?.message === code && !JSON.stringify(error).includes(TEST_SECRET));
  passed++;
};
const seed = Buffer.from(Array.from({ length: 32 }, (_, i) => i + 1));
const TEST_SECRET = seed.toString('hex');

try {
  const { signing } = await official();
  const testDid = signing.signerFromSeed(seed).did;

  await check(async () => assert.equal(await validateRealCredential(seed, testDid), testDid));
  await denied(() => validateRealCredential(seed, PROJECT_DID), 'REAL_CREDENTIAL_DID_MISMATCH');
  await denied(() => validateRealCredential(seed.subarray(0, 31), testDid), 'REAL_CREDENTIAL_MALFORMED');

  const directory = resolve(root, 'credentials');
  mkdirSync(directory);
  writeFileSync(resolve(directory, REAL_CREDENTIAL_NAME), seed, { mode: 0o400 });
  await check(async () => {
    const seen = await withRealCredentialFromDirectory(directory, (input, did) => {
      assert.deepEqual(input, seed);
      assert.equal(did, testDid);
      return did;
    }, testDid);
    assert.equal(seen, testDid);
  });
  await denied(() => withRealCredentialFromDirectory(resolve(root, 'missing'), () => {}, testDid),
    'REAL_CREDENTIAL_UNAVAILABLE');

  await check(async () => {
    const split = [seed.subarray(0, 7), seed.subarray(7, 29), seed.subarray(29)];
    assert.deepEqual(await readRawSeed(Readable.from(split)), seed);
  });
  await denied(() => readRawSeed(Readable.from([seed, Buffer.from([0])])),
    'REAL_CREDENTIAL_MALFORMED');
  await denied(() => readRawSeed(Readable.from(['0'.repeat(32)])),
    'REAL_CREDENTIAL_MALFORMED');
  await denied(() => readRawSeed(Readable.from((async function* () {
    throw new Error(TEST_SECRET);
  })())), 'REAL_CREDENTIAL_READ_FAILED');

  const encryptedPath = resolve(root, 'project-seed.cred');
  let encryptCalls = 0;
  const encrypted = Buffer.from('TEST-ENCRYPTED-CREDENTIAL');
  await check(async () => {
    const did = await handoffRealCredential({
      input: Readable.from([seed]), outputPath: encryptedPath,
      expectedDid: testDid,
      encrypt: input => {
        encryptCalls++;
        assert.deepEqual(input, seed);
        return Buffer.from(encrypted);
      }
    });
    assert.equal(did, testDid);
    assert.deepEqual(readFileSync(encryptedPath), encrypted);
    assert.equal(statSync(encryptedPath).mode & 0o777, 0o600);
  });
  assert.equal(encryptCalls, 1);

  const anonymous = { isFIFO: () => true, nlink: 0 };
  const named = { isFIFO: () => true, nlink: 1 };
  const regular = { isFIFO: () => false, nlink: 1 };
  const fakeStdin = { fd: 0 };
  await check(async () => assert.equal(cliInput([], { stdin: fakeStdin,
    fstat: fd => { assert.equal(fd, 0); return anonymous; } }), fakeStdin));
  await check(async () => {
    const stream = {};
    assert.equal(cliInput(['--input-fd', '7'], { stdin: fakeStdin,
      fstat: fd => { assert.equal(fd, 7); return anonymous; },
      streamFromFd: fd => { assert.equal(fd, 7); return stream; } }), stream);
  });
  await check(async () => assert.equal(cliInput([], { stdin: fakeStdin,
    fstat: () => ({ isFIFO: () => true, nlink: 1 }),
    readlink: path => { assert.equal(path, '/proc/self/fd/0'); return 'pipe:[1234]'; } }), fakeStdin));
  await denied(async () => cliInput([], { stdin: fakeStdin, fstat: () => regular }),
    'ANONYMOUS_INPUT_REQUIRED');
  await denied(async () => cliInput([], { stdin: fakeStdin, fstat: () => named,
    readlink: () => '/tmp/named-fifo' }),
    'ANONYMOUS_INPUT_REQUIRED');

  const probeAnonymousPipe = async fd => {
    const testFile = fileURLToPath(import.meta.url);
    // Node implements extra stdio pipes as socketpairs. For the explicit-FD case,
    // bash duplicates the anonymous stdin pipe onto fd 3 before exec.
    const script = fd === 0
      ? 'cat | "$1" "$2" --anonymous-pipe-probe'
      : 'cat | "$1" "$2" --anonymous-pipe-probe 3 3<&0';
    const child = spawn('/bin/bash', ['-c', script, 'probe', process.execPath, testFile],
      { stdio: ['pipe', 'ignore', 'ignore'], env: {} });
    const pipe = child.stdin;
    pipe.on('error', () => {});
    pipe.end(seed);
    const [code, signal] = await once(child, 'exit');
    assert.equal(signal, null);
    assert.equal(code, 0);
  };
  await check(() => probeAnonymousPipe(0));
  await check(() => probeAnonymousPipe(3));
  await check(async () => {
    const plaintextFd = openSync(resolve(directory, REAL_CREDENTIAL_NAME), 'r');
    const child = spawn(process.execPath,
      [fileURLToPath(import.meta.url), '--anonymous-pipe-probe'],
      { stdio: [plaintextFd, 'ignore', 'ignore'], env: {} });
    closeSync(plaintextFd);
    const [code, signal] = await once(child, 'exit');
    assert.equal(signal, null);
    assert.equal(code, 1);
  });

  await check(async () => assert.doesNotThrow(() => assertProductionCredentialDestination(undefined, {
    geteuid: () => 0,
    lstat: () => ({ isDirectory: () => true, isSymbolicLink: () => false, uid: 0, mode: 0o40700 })
  })));
  await denied(async () => assertProductionCredentialDestination(undefined, {
    geteuid: () => 1000,
    lstat: () => ({ isDirectory: () => true, isSymbolicLink: () => false, uid: 0, mode: 0o40700 })
  }), 'ROOT_REQUIRED');
  await denied(async () => assertProductionCredentialDestination(undefined, {
    geteuid: () => 0,
    lstat: () => ({ isDirectory: () => true, isSymbolicLink: () => true, uid: 0, mode: 0o40700 })
  }), 'REAL_CREDENTIAL_DIRECTORY_UNSAFE');

  const adapterFailurePath = resolve(root, 'adapter-failure.cred');
  await denied(() => handoffRealCredential({ input: Readable.from([seed]),
    outputPath: adapterFailurePath, expectedDid: testDid,
    encrypt: () => { throw new Error(TEST_SECRET); } }), 'REAL_CREDENTIAL_ENCRYPT_FAILED');
  await denied(() => handoffRealCredential({ input: Readable.from([seed]),
    outputPath: encryptedPath, expectedDid: testDid,
    encrypt: () => { encryptCalls++; return Buffer.from(encrypted); } }),
  'REAL_CREDENTIAL_ALREADY_EXISTS');
  assert.equal(encryptCalls, 1);

  const rejectedPath = resolve(root, 'must-not-exist.cred');
  await denied(() => handoffRealCredential({ input: Readable.from([seed]),
    outputPath: rejectedPath,
    encrypt: () => { encryptCalls++; return Buffer.from(encrypted); } }),
  'REAL_CREDENTIAL_DID_MISMATCH');
  assert.equal(encryptCalls, 1);

  assert(!process.argv.some(value => value.includes(TEST_SECRET)));
  assert(!Object.values(process.env).some(value => value?.includes(TEST_SECRET)));
  passed += 2;
  console.log(JSON.stringify({ passed, failed: 0, realSecrets: false, externalWrites: 0 }));
} catch {
  console.log(JSON.stringify({ passed, failed: 1, code: 'REAL_CREDENTIAL_TEST_FAILED',
    realSecrets: false, externalWrites: 0 }));
  process.exitCode = 1;
} finally {
  seed.fill(0);
  rmSync(root, { recursive: true, force: true });
}
