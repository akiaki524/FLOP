// Human-launched, Close Call only. No RPC, credential store, transport or retry.
import { createHash } from 'node:crypto';
import {
  closeSync, fsyncSync, lstatSync, mkdirSync, openSync,
  readFileSync, readdirSync, writeFileSync,
} from 'node:fs';
import { isAbsolute, join } from 'node:path';
import { pathToFileURL } from 'node:url';

import { cliInput } from './credential_handoff.mjs';
import { assertHostCrashSafety } from './host_crash_safety.mjs';
import {
  CLOSE_CALL_TAKER_LONG_POLICY_PROTOCOL, PolicySigner,
  policyDigest, validateCloseCallTakerLongRequest, validatePolicy,
} from './policy_signer.mjs';
import { TrustedSnapshotAcquisition } from './runtime_acquisition.mjs';
import { TechnocoreTrustedReader } from './trusted_reader.mjs';

function need(condition, code) {
  if (!condition) {
    const error = new Error(code);
    error.code = code;
    throw error;
  }
}

// Approval and the attempt directory belong to the supervising Human, never
// to the Agent. Same-UID malicious code / root compromise is outside this gate.
function ownedPath(path, directory = false) {
  need(typeof path === 'string' && isAbsolute(path), 'ONESHOT_PATH_REQUIRED');
  const stat = lstatSync(path);
  need(!stat.isSymbolicLink()
    && (directory ? stat.isDirectory() : stat.isFile())
    && stat.uid === process.geteuid()
    && (stat.mode & (directory ? 0o077 : 0o022)) === 0,
  'ONESHOT_PATH_UNSAFE');
  return stat;
}

function readPublicJson(path) {
  const stat = ownedPath(path);
  need(stat.size <= 64 * 1024, 'ONESHOT_INPUT_TOO_LARGE');
  const bytes = readFileSync(path);
  need(bytes.length === stat.size, 'ONESHOT_INPUT_CHANGED');
  return { bytes, value: JSON.parse(bytes.toString('utf8')) };
}

function claimAttempt(operationDir, approval, policy) {
  ownedPath(operationDir, true);
  // Refuse old state, snapshots, even a partial/crashed attempt. No reset path.
  need(readdirSync(operationDir).length === 0, 'ONESHOT_NO_RETRY');
  let fd;
  try {
    fd = openSync(join(operationDir, 'attempt.json'), 'wx', 0o600);
    writeFileSync(fd, JSON.stringify({
      version: 1, status: 'ATTEMPT_RESERVED',
      requestSha256: approval.requestSha256,
      policyDigest: policyDigest(policy),
    }) + '\n');
    fsyncSync(fd);
  } catch (error) {
    if (error?.code === 'EEXIST') need(false, 'ONESHOT_NO_RETRY');
    throw error;
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
  const directoryFd = openSync(operationDir, 'r');
  try { fsyncSync(directoryFd); } finally { closeSync(directoryFd); }
}

async function readSeed(input, seed, liveBuffers) {
  let size = 0;
  for await (const chunk of input) {
    need(Buffer.isBuffer(chunk) || chunk instanceof Uint8Array, 'ONESHOT_SEED_LENGTH');
    liveBuffers.add(chunk);
    try {
      need(size + chunk.byteLength <= 32, 'ONESHOT_SEED_LENGTH');
      seed.set(chunk, size);
      size += chunk.byteLength;
    } finally {
      chunk.fill(0);
      liveBuffers.delete(chunk);
    }
  }
  need(size === 32, 'ONESHOT_SEED_LENGTH');
}

// Test injection is limited to HTTP and time. Seed ingress stays anonymous FD.
export async function runCloseCallOneShot({
  approvalPath, requestPath, operationDir, inputArgs = [],
  fetchImpl = globalThis.fetch, now = Date.now,
}) {
  assertHostCrashSafety();
  const { value: approval } = readPublicJson(approvalPath);
  need(approval && Object.keys(approval).sort().join(',') === 'operationDir,policy,requestSha256,version'
    && approval.version === 1 && /^[0-9a-f]{64}$/.test(approval.requestSha256),
  'ONESHOT_APPROVAL_INVALID');
  need(approval.operationDir === operationDir, 'ONESHOT_APPROVAL_BINDING');
  const policy = validatePolicy(approval.policy);
  need(policy.protocol === CLOSE_CALL_TAKER_LONG_POLICY_PROTOCOL, 'POLICY_CAPABILITY');
  const { bytes, value: request } = readPublicJson(requestPath);
  need(createHash('sha256').update(bytes).digest('hex') === approval.requestSha256,
    'ONESHOT_APPROVAL_BINDING');
  validateCloseCallTakerLongRequest(request, policy.expectedDid);
  const input = cliInput(inputArgs);

  const liveBuffers = new Set();
  const seed = Buffer.alloc(32);
  liveBuffers.add(seed);
  const wipe = () => { for (const buffer of liveBuffers) buffer.fill(0); };
  process.once('exit', wipe);
  try {
    claimAttempt(operationDir, approval, policy);
    await readSeed(input, seed, liveBuffers);
    const acquisitionRoot = join(operationDir, 'acquisition');
    mkdirSync(acquisitionRoot, { mode: 0o700 });
    const signer = new PolicySigner({
      seed, policy, statePath: join(operationDir, 'state.json'),
      acquisition: new TrustedSnapshotAcquisition(acquisitionRoot),
      initialize: true, now,
    });
    seed.fill(0);
    const reader = new TechnocoreTrustedReader({ root: acquisitionRoot, fetchImpl, clock: now });
    // Fixed GETs only; price is the final network read before synchronous signing.
    await reader.refreshCloseCallRegistration();
    await reader.refreshCloseCallPrice();
    const result = signer.closeCallTakerLong(request);
    // Keep the existing lossless-nonce POST-ready record; no network write API.
    // price carries the existing non-rejecting age / stale disclosure for Human review.
    return {
      takerSignature: result.takerSignature, roomRecord: result.roomRecord, price: result.price,
    };
  } finally {
    wipe();
    process.removeListener('exit', wipe);
    input.destroy();
  }
}

export function parseOneShotArgs(argv) {
  const options = {};
  const names = new Map([
    ['--approval', 'approvalPath'], ['--request', 'requestPath'],
    ['--operation-dir', 'operationDir'], ['--input-fd', 'inputFd'],
  ]);
  need(argv.length === 6 || argv.length === 8, 'ARGUMENTS_DENIED');
  for (let i = 0; i < argv.length; i += 2) {
    const name = names.get(argv[i]);
    need(name && !Object.hasOwn(options, name) && argv[i + 1], 'ARGUMENTS_DENIED');
    options[name] = argv[i + 1];
  }
  need(options.approvalPath && options.requestPath && options.operationDir, 'ARGUMENTS_DENIED');
  const { inputFd, ...paths } = options;
  return { ...paths, inputArgs: inputFd === undefined ? [] : ['--input-fd', inputFd] };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  // Catchable termination runs the exit buffer wipe. No restart/retry handler.
  process.once('SIGINT', () => process.exit(130));
  process.once('SIGTERM', () => process.exit(143));
  Promise.resolve().then(() => runCloseCallOneShot(parseOneShotArgs(process.argv.slice(2))))
    .then(result => { process.stdout.write(JSON.stringify(result) + '\n'); })
    .catch(() => {
      // Never expose request, seed or arbitrary exception details in diagnostics.
      process.stderr.write('CLOSE_CALL_ONESHOT_DENIED_NO_RETRY\n');
      process.exitCode = 70;
    });
}
