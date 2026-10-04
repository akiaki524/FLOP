// Offline preparation for a future Human-authorized credential handoff.
// No network access, service mutation, activation, or write enablement occurs here.
import { createReadStream } from 'node:fs';
import { closeSync, existsSync, fstatSync, fsyncSync, lstatSync, openSync, readFileSync,
  readlinkSync, unlinkSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { timingSafeEqual } from 'node:crypto';
import { dirname } from 'node:path';
import { pathToFileURL } from 'node:url';
import { validateRealCredential, REAL_CREDENTIAL_NAME } from '../../src/collaboration_agent/real_credential.mjs';

export const REAL_ENCRYPTED_CREDENTIAL = '/var/lib/collab-connection-setup/project-seed.cred';
const MAX_ENCRYPTED_BYTES = 1024 * 1024;

function fail(code) { throw new Error(code); }

export async function readRawSeed(input) {
  const chunks = [];
  let size = 0;
  try {
    for await (const value of input) {
      // Stream chunks are owned by the stream implementation. Copy only bounded
      // bytes, then erase our copies; callers must not assume we can erase the
      // stream's internal buffers.
      if (!(Buffer.isBuffer(value) || value instanceof Uint8Array)) {
        fail('REAL_CREDENTIAL_MALFORMED');
      }
      if (size + value.byteLength > 32) fail('REAL_CREDENTIAL_MALFORMED');
      const chunk = Buffer.from(value);
      chunks.push(chunk);
      size += chunk.length;
    }
    if (size !== 32) fail('REAL_CREDENTIAL_MALFORMED');
    return Buffer.concat(chunks, 32);
  } catch (error) {
    if (error?.message === 'REAL_CREDENTIAL_MALFORMED') throw error;
    fail('REAL_CREDENTIAL_READ_FAILED');
  } finally {
    for (const chunk of chunks) chunk.fill(0);
  }
}

export function systemdEncrypt(seed) {
  const result = spawnSync('/usr/bin/systemd-creds', [
    'encrypt', '--with-key=host', '--name=' + REAL_CREDENTIAL_NAME, '-', '-'
  ], {
    input: seed,
    encoding: null,
    env: { PATH: '/usr/bin:/bin' },
    stdio: ['pipe', 'pipe', 'ignore'],
    timeout: 30_000,
    maxBuffer: MAX_ENCRYPTED_BYTES
  });
  if (result.error || result.status !== 0 || !Buffer.isBuffer(result.stdout)
      || result.stdout.length === 0 || result.stdout.length > MAX_ENCRYPTED_BYTES) {
    fail('REAL_CREDENTIAL_ENCRYPT_FAILED');
  }
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
    if (readback.length !== encrypted.length || !timingSafeEqual(readback, encrypted)) {
      fail('REAL_CREDENTIAL_STORE_FAILED');
    }
    directoryFd = openSync(dirname(path), 'r');
    fsyncSync(directoryFd);
    closeSync(directoryFd);
    directoryFd = undefined;
  } catch {
    if (fd !== undefined) try { closeSync(fd); } catch {}
    if (directoryFd !== undefined) try { closeSync(directoryFd); } catch {}
    if (created) try { unlinkSync(path); } catch {}
    fail('REAL_CREDENTIAL_STORE_FAILED');
  } finally {
    if (Buffer.isBuffer(readback)) readback.fill(0);
  }
}

export function assertProductionCredentialDestination(
  outputPath = REAL_ENCRYPTED_CREDENTIAL,
  { geteuid = process.geteuid, lstat = lstatSync } = {}) {
  if (outputPath !== REAL_ENCRYPTED_CREDENTIAL || typeof geteuid !== 'function' || geteuid() !== 0) {
    fail('ROOT_REQUIRED');
  }
  let parent;
  try { parent = lstat(dirname(outputPath)); }
  catch { fail('REAL_CREDENTIAL_DIRECTORY_UNSAFE'); }
  if (!parent.isDirectory() || parent.isSymbolicLink() || parent.uid !== 0
      || (parent.mode & 0o777) !== 0o700) {
    fail('REAL_CREDENTIAL_DIRECTORY_UNSAFE');
  }
}

// outputPath and encrypt are injection points for offline tests. The executable
// entrypoint below supplies neither, so its destination and encryption command
// cannot be changed through argv or environment variables.
export async function handoffRealCredential({ input, outputPath = REAL_ENCRYPTED_CREDENTIAL,
  encrypt = systemdEncrypt, expectedDid } = {}) {
  if (!input || existsSync(outputPath)) fail('REAL_CREDENTIAL_ALREADY_EXISTS');
  const seed = await readRawSeed(input);
  let encrypted;
  try {
    // expectedDid exists for deterministic offline tests only. The production
    // entrypoint never accepts or forwards an identity override.
    const did = expectedDid === undefined
      ? await validateRealCredential(seed)
      : await validateRealCredential(seed, expectedDid);
    try { encrypted = await encrypt(seed); }
    catch { fail('REAL_CREDENTIAL_ENCRYPT_FAILED'); }
    if (!Buffer.isBuffer(encrypted) || encrypted.length === 0
        || encrypted.length > MAX_ENCRYPTED_BYTES) fail('REAL_CREDENTIAL_ENCRYPT_FAILED');
    writeNewEncrypted(outputPath, encrypted);
    return did;
  } finally {
    seed.fill(0);
    if (Buffer.isBuffer(encrypted)) encrypted.fill(0);
  }
}

export function cliInput(argv, { stdin = process.stdin, fstat = fstatSync,
  readlink = readlinkSync,
  streamFromFd = fd => createReadStream(null, { fd, autoClose: false }) } = {}) {
  let fd;
  if (argv.length === 0) {
    fd = stdin.fd;
  } else {
    if (argv.length !== 2 || argv[0] !== '--input-fd' || !/^[0-9]+$/.test(argv[1])) {
      fail('ARGUMENTS_REFUSED');
    }
    fd = Number(argv[1]);
    if (!Number.isSafeInteger(fd) || fd < 3 || fd > 1024) fail('ARGUMENTS_REFUSED');
  }
  let status;
  try { status = fstat(fd); }
  catch { fail('ANONYMOUS_INPUT_REQUIRED'); }
  // Linux commonly reports nlink=1 even for anonymous pipes. /proc identifies
  // those descriptors as pipe:[inode], while a named FIFO resolves to its path.
  // Keep nlink=0 support for kernels that expose unlink state directly.
  let anonymous = status.isFIFO() && status.nlink === 0;
  if (status.isFIFO() && status.nlink === 1) {
    try { anonymous = /^pipe:\[[0-9]+\]$/.test(readlink('/proc/self/fd/' + fd)); }
    catch { anonymous = false; }
  }
  if (!anonymous) fail('ANONYMOUS_INPUT_REQUIRED');
  return argv.length === 0 ? stdin : streamFromFd(fd);
}

async function main() {
  assertProductionCredentialDestination();
  await handoffRealCredential({ input: cliInput(process.argv.slice(2)) });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => { process.exitCode = 1; });
}
