import { assertHostCrashSafety } from '../src/host_crash_safety.mjs';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

import { signerFromSeed } from '../src/technocore_signing.mjs';
import { TCLK_COMMIT } from '../src/tclk_v1.mjs';

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}

export function identityFromSeed(seed) {
  if (!Buffer.isBuffer(seed) || seed.length !== 32) fail('TEST_SEED_LENGTH');
  return { did: signerFromSeed(seed).did, tclkCommit: TCLK_COMMIT };
}

function main() {
  assertHostCrashSafety();
  const seed = readFileSync(0);
  try {
    const result = identityFromSeed(seed);
    process.stdout.write(JSON.stringify(result) + '\n');
  } finally {
    seed.fill(0);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try { main(); }
  catch (error) {
    process.stderr.write((error?.code ?? 'TEST_IDENTITY_FAILED') + '\n');
    process.exitCode = 70;
  }
}
