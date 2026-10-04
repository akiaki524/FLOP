import { pathToFileURL } from 'node:url';

import { callSocket } from '../src/runtime_rpc.mjs';

const SIGNER_SOCKET = '/run/flop-policy-signer.sock';
const READER_SOCKET = '/run/flop-policy-signer-reader.sock';

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}

export async function runProbe(command, call = callSocket) {
  if (command === 'signer-status') {
    return call(SIGNER_SOCKET, { method: 'status', value: {} });
  }
  if (command === 'reader-offers') {
    return call(READER_SOCKET, { method: 'refreshOffers', value: {} });
  }
  if (command === 'signer-reader-refresh') {
    // The Signer refreshes offers through its internal Reader before policy
    // rejects this intentionally invalid source. No work or signature is made.
    try {
      await call(SIGNER_SOCKET, {
        method: 'admit',
        value: { offerSeq: Number.MAX_SAFE_INTEGER, sourceBase64url: 'AA' },
      });
    } catch (error) {
      // SOURCE_INVALID follows a live policy check and a fresh acquisition
      // snapshot read. POLICY_EXPIRED stops before that read.
      if (error?.code === 'SOURCE_INVALID') {
        return { readerRefreshed: true, outcome: 'SOURCE_INVALID' };
      }
      throw error;
    }
    fail('UNEXPECTED_ADMISSION');
  }
  fail('PROBE_COMMAND_DENIED');
}

async function main() {
  if (process.argv.length !== 3) fail('ARGUMENTS_DENIED');
  const result = await runProbe(process.argv[2]);
  process.stdout.write(JSON.stringify(result) + '\n');
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(error => {
    const code = typeof error?.code === 'string' ? error.code : 'PROBE_FAILED';
    process.stderr.write(code + '\n');
    process.exitCode = 70;
  });
}
