import { lstatSync, readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

import { PolicySigner } from './policy_signer.mjs';
import { auditedHandler, RuntimeAudit } from './runtime_audit.mjs';
import { TrustedSnapshotAcquisition } from './runtime_acquisition.mjs';
import { withSystemdCredential } from './runtime_credential.mjs';
import { callSocket, createPolicySignerRpcHandler, serveSocket } from './runtime_rpc.mjs';

export const RUNTIME_CONFIG_PATH = '/etc/flop-policy-signer/config.json';
export const RUNTIME_STATE_PATH = '/var/lib/flop-policy-signer/state.json';
export const RUNTIME_AUDIT_PATH = '/var/lib/flop-policy-signer/audit.jsonl';
export const READER_SOCKET_PATH = '/run/flop-policy-signer-reader.sock';

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}
function need(condition, code) {
  if (!condition) fail(code);
}
function exactKeys(value, names) {
  need(value && typeof value === 'object' && !Array.isArray(value), 'RUNTIME_CONFIG_INVALID');
  need(Object.keys(value).sort().join(',') === [...names].sort().join(','), 'RUNTIME_CONFIG_INVALID');
}

export function loadRuntimeConfig(path = RUNTIME_CONFIG_PATH, {
  expectedUid = path === RUNTIME_CONFIG_PATH ? 0 : null,
  lstat = lstatSync,
  readFile = readFileSync,
} = {}) {
  let stat;
  try { stat = lstat(path); }
  catch { fail('RUNTIME_CONFIG_UNAVAILABLE'); }
  need(stat.isFile() && !stat.isSymbolicLink()
    && (stat.mode & 0o022) === 0
    && (expectedUid === null || stat.uid === expectedUid),
  'RUNTIME_CONFIG_UNSAFE');

  let value;
  try { value = JSON.parse(readFile(path, 'utf8')); }
  catch { fail('RUNTIME_CONFIG_UNAVAILABLE'); }
  exactKeys(value, ['version', 'policy', 'acquisitionRoot']);
  need(value.version === 1 && value.policy && typeof value.acquisitionRoot === 'string',
    'RUNTIME_CONFIG_INVALID');
  return value;
}

export function assertSocketActivation(env = process.env, pid = process.pid) {
  need(env.LISTEN_PID === String(pid) && env.LISTEN_FDS === '1'
    && (env.LISTEN_FDNAMES === undefined || env.LISTEN_FDNAMES === 'signer'),
    'SOCKET_ACTIVATION_REQUIRED');
}

export async function startRuntime({
  configPath = RUNTIME_CONFIG_PATH,
  statePath = RUNTIME_STATE_PATH,
  credentialDirectory = process.env.CREDENTIALS_DIRECTORY,
  socketFd = 3,
  auditPath = RUNTIME_AUDIT_PATH,
  enforceSocketActivation = true,
} = {}) {
  if (enforceSocketActivation) assertSocketActivation();
  const config = loadRuntimeConfig(configPath);
  const acquisition = new TrustedSnapshotAcquisition(config.acquisitionRoot);
  return withSystemdCredential(credentialDirectory, seed => {
    const signer = new PolicySigner({
      seed,
      policy: config.policy,
      statePath,
      acquisition,
      initialize: false,
    });
    const audit = new RuntimeAudit(auditPath);
    const refresh = async ({ method, action, status }) => {
      const contract = status?.work?.contract ?? null;
      if (method === 'admit' || (action === 'ACCEPT')) {
        await callSocket(READER_SOCKET_PATH, { method: 'refreshOffers', value: {} });
        return;
      }
      if (method === 'closeCallOwnerRegister') {
        await callSocket(READER_SOCKET_PATH, {
          method: 'refreshCloseCallRegistration',
          value: {},
        });
        return;
      }
      if (method === 'closeCallTakerLong') {
        // Observe the Signer-owned nonce first, then make authenticated price
        // the final external read before the bounded operation begins.
        await callSocket(READER_SOCKET_PATH, {
          method: 'refreshCloseCallRegistration',
          value: {},
        });
        await callSocket(READER_SOCKET_PATH, {
          method: 'refreshCloseCallPrice',
          value: {},
        });
        return;
      }
      if (method === 'observeCompletion') {
        need(typeof contract === 'string', 'READER_CONTRACT_REQUIRED');
        await callSocket(READER_SOCKET_PATH, { method: 'refreshPaper', value: { contract } });
        return;
      }
      if (method === 'issue' || method === 'reconcile') {
        need(typeof contract === 'string', 'READER_CONTRACT_REQUIRED');
        await callSocket(READER_SOCKET_PATH, { method: 'refreshDeal', value: { contract } });
        if (method === 'issue' && (action === 'DELIVERY_GCD' || action === 'REVEAL')) {
          await callSocket(READER_SOCKET_PATH, { method: 'refreshPaper', value: { contract } });
        }
      }
    };
    const handle = auditedHandler({
      signer,
      handle: createPolicySignerRpcHandler(signer, { beforeRequest: refresh }),
      audit,
    });
    const server = serveSocket(socketFd, handle);
    server.once('close', () => audit.close());
    return server;
  });
}

async function main() {
  const server = await startRuntime();
  const close = () => server.close(() => { process.exitCode = 0; });
  process.once('SIGTERM', close);
  process.once('SIGINT', close);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(error => {
    if (error?.code === 'WSL_CRASH_CAPTURE_UNSAFE') {
      process.stderr.write('WSL_CRASH_CAPTURE_UNSAFE\n');
    }
    process.exitCode = 70;
  });
}
