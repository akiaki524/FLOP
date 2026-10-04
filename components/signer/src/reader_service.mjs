import { pathToFileURL } from 'node:url';

import { createReaderRpcHandler, TechnocoreTrustedReader } from './trusted_reader.mjs';
import { serveSocket } from './runtime_rpc.mjs';

export const ACQUISITION_ROOT = '/var/lib/flop-policy-signer-input';

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}
function need(condition, code) {
  if (!condition) fail(code);
}

export function assertReaderSocketActivation(env = process.env, pid = process.pid) {
  need(env.LISTEN_PID === String(pid) && env.LISTEN_FDS === '1'
    && (env.LISTEN_FDNAMES === undefined || env.LISTEN_FDNAMES === 'reader'),
    'SOCKET_ACTIVATION_REQUIRED');
}

export function startReader({
  root = ACQUISITION_ROOT,
  socketFd = 3,
  fetchImpl = globalThis.fetch,
  clock = Date.now,
  enforceSocketActivation = true,
} = {}) {
  if (enforceSocketActivation) assertReaderSocketActivation();
  const reader = new TechnocoreTrustedReader({ root, fetchImpl, clock });
  return serveSocket(socketFd, createReaderRpcHandler(reader));
}

function main() {
  const server = startReader();
  const close = () => server.close(() => { process.exitCode = 0; });
  process.once('SIGTERM', close);
  process.once('SIGINT', close);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try { main(); } catch { process.exitCode = 70; }
}
