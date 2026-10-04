import { existsSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

import { PolicySigner } from './policy_signer.mjs';
import { withSystemdCredential } from './runtime_credential.mjs';
import { loadRuntimeConfig, RUNTIME_CONFIG_PATH, RUNTIME_STATE_PATH } from './runtime_service.mjs';

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}

export async function initializeRuntimeState({
  configPath = RUNTIME_CONFIG_PATH,
  statePath = RUNTIME_STATE_PATH,
  credentialDirectory = process.env.CREDENTIALS_DIRECTORY,
} = {}) {
  if (existsSync(statePath)) fail('STATE_ALREADY_EXISTS');
  const config = loadRuntimeConfig(configPath);

  return withSystemdCredential(credentialDirectory, seed => {
    const unavailable = () => { fail('BOOTSTRAP_ACQUISITION_DISABLED'); };
    const signer = new PolicySigner({
      seed,
      policy: config.policy,
      statePath,
      acquisition: { export: unavailable, paperNote: unavailable },
      initialize: true,
    });
    return signer.status();
  });
}

async function main() {
  if (process.argv.length !== 2) fail('ARGUMENTS_DENIED');
  await initializeRuntimeState();
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(error => {
    if (error?.code === 'WSL_CRASH_CAPTURE_UNSAFE') {
      process.stderr.write('WSL_CRASH_CAPTURE_UNSAFE\n');
    }
    process.exitCode = 70;
  });
}
