import { assertHostCrashSafety } from '../src/host_crash_safety.mjs';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

import { PolicySigner } from '../src/policy_signer.mjs';
import {
  loadRuntimeConfig,
  RUNTIME_CONFIG_PATH,
  RUNTIME_STATE_PATH,
} from '../src/runtime_service.mjs';

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}

export function offlineStatus(seed, {
  configPath = RUNTIME_CONFIG_PATH,
  statePath = RUNTIME_STATE_PATH,
} = {}) {
  if (!Buffer.isBuffer(seed) || seed.length !== 32) fail('TEST_SEED_LENGTH');
  const config = loadRuntimeConfig(configPath);
  const unavailable = () => fail('OFFLINE_ACQUISITION_DISABLED');
  const signer = new PolicySigner({
    seed,
    policy: config.policy,
    statePath,
    acquisition: { export: unavailable, paperNote: unavailable },
    initialize: false,
  });
  return signer.status();
}

function main() {
  assertHostCrashSafety();
  const seed = readFileSync(0);
  try {
    const value = offlineStatus(seed);
    process.stdout.write(JSON.stringify(value) + '\n');
  } finally {
    seed.fill(0);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try { main(); }
  catch (error) {
    process.stderr.write((error?.code ?? 'OFFLINE_STATUS_FAILED') + '\n');
    process.exitCode = 70;
  }
}
