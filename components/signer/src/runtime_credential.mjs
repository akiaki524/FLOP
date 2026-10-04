import { readFileSync } from 'node:fs';
import { isAbsolute, resolve } from 'node:path';
import { assertHostCrashSafety } from './host_crash_safety.mjs';

export const SYSTEMD_CREDENTIAL_NAME = 'project-seed';

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}

export async function withSystemdCredential(credentialDirectory, initialize) {
  if (typeof credentialDirectory !== 'string' || !isAbsolute(credentialDirectory)) {
    fail('CREDENTIAL_DIRECTORY_REQUIRED');
  }
  if (typeof initialize !== 'function') fail('CREDENTIAL_INITIALIZER_REQUIRED');

  assertHostCrashSafety();
  let seed;
  try {
    seed = readFileSync(resolve(credentialDirectory, SYSTEMD_CREDENTIAL_NAME));
  } catch {
    fail('CREDENTIAL_UNAVAILABLE');
  }
  if (!Buffer.isBuffer(seed) || seed.length !== 32) {
    if (Buffer.isBuffer(seed)) seed.fill(0);
    fail('CREDENTIAL_MALFORMED');
  }

  try {
    return await initialize(seed);
  } finally {
    seed.fill(0);
  }
}
