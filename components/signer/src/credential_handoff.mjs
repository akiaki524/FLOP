import { timingSafeEqual } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { createReadStream } from 'node:fs';
import {
  closeSync,
  existsSync,
  fstatSync,
  fsyncSync,
  lstatSync,
  openSync,
  readFileSync,
  readlinkSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { dirname } from 'node:path';
import { pathToFileURL } from 'node:url';

import { loadRuntimeConfig, RUNTIME_CONFIG_PATH } from './runtime_service.mjs';
import { assertHostCrashSafety } from './host_crash_safety.mjs';
import { SYSTEMD_CREDENTIAL_NAME } from './runtime_credential.mjs';
import { signerFromSeed } from './technocore_signing.mjs';

export const ENCRYPTED_CREDENTIAL_PATH =
  '/var/lib/flop-policy-signer-secret/project-seed.cred';

const MAX_ENCRYPTED_BYTES = 1 << 20;

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}
function need(condition, code) {
  if (!condition) fail(code);
}

export async function readRawSeed(input) {
  assertHostCrashSafety();
  const chunks = [];
  let size = 0;
  try {
    for await (const value of input) {
      need(Buffer.isBuffer(value) || value instanceof Uint8Array, 'CREDENTIAL_MALFORMED');
      need(size + value.byteLength <= 32, 'CREDENTIAL_MALFORMED');
      const copy = Buffer.from(value);
      chunks.push(copy);
      size += copy.length;
    }
    need(size === 32, 'CREDENTIAL_MALFORMED');
    return Buffer.concat(chunks, 32);
  } finally {
    for (const chunk of chunks) chunk.fill(0);
  }
}

export function systemdEncrypt(seed) {
  const result = spawnSync('/usr/bin/systemd-creds', [
    'encrypt',
    '--with-key=host',
    '--name=' + SYSTEMD_CREDENTIAL_NAME,
    '-',
    '-',
  ], {
    input: seed,
    encoding: null,
    env: { PATH: '/usr/bin:/bin' },
    stdio: ['pipe', 'pipe', 'ignore'],
    timeout: 30_000,
    maxBuffer: MAX_ENCRYPTED_BYTES,
  });
  need(!result.error && result.status === 0 && Buffer.isBuffer(result.stdout)
    && result.stdout.length > 0 && result.stdout.length <= MAX_ENCRYPTED_BYTES,
  'CREDENTIAL_ENCRYPT_FAILED');
  return result.stdout;
}

function writeNewEncrypted(path, encrypted) {
  let fd;
  let directoryFd;
  let readback;
  let created = false;
  try {
    fd = openSync(path, 'wx', 0o600);
    created = true;
    writeFileSync(fd, encrypted);
    fsyncSync(fd);
    closeSync(fd);
    fd = undefined;
    readback = readFileSync(path);
    need(readback.length === encrypted.length && timingSafeEqual(readback, encrypted),
      'CREDENTIAL_STORE_FAILED');
    directoryFd = openSync(dirname(path), 'r');
    fsyncSync(directoryFd);
    closeSync(directoryFd);
    directoryFd = undefined;
  } catch (error) {
    if (fd !== undefined) try { closeSync(fd); } catch {}
    if (directoryFd !== undefined) try { closeSync(directoryFd); } catch {}
    if (created) try { unlinkSync(path); } catch {}
    if (error?.code === 'CREDENTIAL_STORE_FAILED') throw error;
    fail('CREDENTIAL_STORE_FAILED');
  } finally {
    if (Buffer.isBuffer(readback)) readback.fill(0);
  }
}

export function assertCredentialDestination(
  outputPath = ENCRYPTED_CREDENTIAL_PATH,
  { geteuid = process.geteuid, lstat = lstatSync } = {},
) {
  need(outputPath === ENCRYPTED_CREDENTIAL_PATH
    && typeof geteuid === 'function' && geteuid() === 0, 'ROOT_REQUIRED');
  let parent;
  try { parent = lstat(dirname(outputPath)); }
  catch { fail('CREDENTIAL_DIRECTORY_UNSAFE'); }
  need(parent.isDirectory() && !parent.isSymbolicLink() && parent.uid === 0
    && (parent.mode & 0o777) === 0o700, 'CREDENTIAL_DIRECTORY_UNSAFE');
}

export async function handoffCredential({
  input,
  outputPath = ENCRYPTED_CREDENTIAL_PATH,
  encrypt = systemdEncrypt,
  configPath = RUNTIME_CONFIG_PATH,
} = {}) {
  assertHostCrashSafety();
  need(input && !existsSync(outputPath), 'CREDENTIAL_ALREADY_EXISTS');
  const config = loadRuntimeConfig(configPath);
  const seed = await readRawSeed(input);
  let encrypted;
  try {
    need(signerFromSeed(seed).did === config.policy.expectedDid, 'CREDENTIAL_DID_MISMATCH');
    encrypted = await encrypt(seed);
    need(Buffer.isBuffer(encrypted) && encrypted.length > 0
      && encrypted.length <= MAX_ENCRYPTED_BYTES, 'CREDENTIAL_ENCRYPT_FAILED');
    writeNewEncrypted(outputPath, encrypted);
    return { did: config.policy.expectedDid };
  } finally {
    seed.fill(0);
    if (Buffer.isBuffer(encrypted)) encrypted.fill(0);
  }
}

export function cliInput(argv, {
  stdin = process.stdin,
  fstat = fstatSync,
  readlink = readlinkSync,
  streamFromFd = fd => createReadStream(null, { fd, autoClose: false }),
} = {}) {
  assertHostCrashSafety();
  let fd;
  if (argv.length === 0) {
    fd = stdin.fd;
  } else {
    need(argv.length === 2 && argv[0] === '--input-fd' && /^[0-9]+$/.test(argv[1]),
      'ARGUMENTS_DENIED');
    fd = Number(argv[1]);
    need(Number.isSafeInteger(fd) && fd >= 3 && fd <= 1024, 'ARGUMENTS_DENIED');
  }
  let status;
  try { status = fstat(fd); } catch { fail('ANONYMOUS_INPUT_REQUIRED'); }
  let anonymous = status.isFIFO() && status.nlink === 0;
  if (status.isFIFO() && status.nlink === 1) {
    try { anonymous = /^pipe:\[[0-9]+\]$/.test(readlink('/proc/self/fd/' + fd)); }
    catch { anonymous = false; }
  }
  need(anonymous, 'ANONYMOUS_INPUT_REQUIRED');
  return argv.length === 0 ? stdin : streamFromFd(fd);
}

async function main() {
  assertCredentialDestination();
  await handoffCredential({ input: cliInput(process.argv.slice(2)) });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(error => {
    if (error?.code === 'WSL_CRASH_CAPTURE_UNSAFE') {
      process.stderr.write('WSL_CRASH_CAPTURE_UNSAFE\n');
    }
    process.exitCode = 70;
  });
}
