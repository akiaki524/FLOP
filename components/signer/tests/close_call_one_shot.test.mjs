import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  chmodSync, existsSync, mkdtempSync, readFileSync, readdirSync,
  rmSync, symlinkSync, writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import * as p from '../src/policy_signer.mjs';
import { parseOneShotArgs } from '../src/close_call_one_shot.mjs';
import { canonicalMessage, signerFromSeed, verifyDidSignature } from '../src/technocore_signing.mjs';

const SEED = Buffer.alloc(32, 0x11); // Synthetic only; never stored in a file.
const NOW = p.CLOSE_CALL_LOCK_MS - 7_200_000;
const ENTRY = new URL('../src/close_call_one_shot.mjs', import.meta.url).href;
function stable(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${stable(value[k])}`).join(',')}}`;
}
const sha = value => createHash('sha256').update(value).digest('hex');
function row(room, signer, text, nonce) {
  return JSON.stringify({ seq: 1, ts: new Date(NOW - 10).toISOString(),
    from: signer.did, text, nonce, sig: signer.sign(canonicalMessage(room, nonce, text)),
  }).replace(`"nonce":"${nonce}"`, `"nonce":${nonce}`) + '\n';
}
function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'close-call-one-shot-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const operationDir = mkdtempSync(join(root, 'attempt-'));
  const owner = signerFromSeed(SEED);
  const maker = signerFromSeed(Buffer.alloc(32, 0x44));
  const referee = signerFromSeed(Buffer.alloc(32, 0x66));
  const price = { t: 'price', n: 100,
    ref: { px: '226.14', time: new Date(NOW - 100_000).toISOString(), tid: 812345 },
    limits: ['214.84', '237.44'], global: '226.10', file: 'f'.repeat(64) };
  const terms = { id: 'first1', maker: maker.did, px: '226.14', qty: '2.00',
    side: 'sell', taker: 'any', until: 102 };
  const canonical = stable(terms);
  const makerSig = maker.sign(`${p.CLOSE_CALL_CONTEST}|terms|${canonical}`);
  const accept = `${p.CLOSE_CALL_CONTEST}|accept|${canonical}|${owner.did}`;
  const policy = {
    version: 1, protocol: p.CLOSE_CALL_TAKER_LONG_POLICY_PROTOCOL,
    action: p.CLOSE_CALL_TAKER_LONG_ACTION, expectedDid: owner.did,
    contest: p.CLOSE_CALL_CONTEST, rulesCommit: p.CLOSE_CALL_RULES_COMMIT,
    packageManifestSha256: p.CLOSE_CALL_PACKAGE_MANIFEST_SHA256,
    room: p.CLOSE_CALL_ROOM, priceRoom: p.CLOSE_CALL_PRICE_ROOM,
    refereeDid: referee.did, noncePolicy: p.CLOSE_CALL_NONCE_POLICY,
    issuedAtMs: NOW - 1000, expiresAtMs: NOW + 3_600_000,
    notAfterMs: p.CLOSE_CALL_LOCK_MS, freshMs: 30_000,
    maxSignatures: 2, noValueMarker: 'EXPLICIT_PAPER_NO_VALUE',
  };
  const request = {
    version: 1, kind: 'CLOSE_CALL_TYPED_SIGN_REQUEST', action: policy.action,
    expectedDid: owner.did,
    subject: { season: policy.contest, did: owner.did, direction: 'LONG', makerSide: 'SELL' },
    binding: {
      contest: policy.contest, rulesCommit: policy.rulesCommit,
      packageManifestSha256: policy.packageManifestSha256, room: policy.room,
      noncePolicy: policy.noncePolicy, canonicalTerms: canonical,
      termsSha256: sha(canonical), makerDid: maker.did, makerSig,
      authenticatedPrice: { refereeDid: referee.did, sweep: price.n, ref: price.ref,
        limits: price.limits, referenceAgeSeconds: '100', stale: false,
        recordSha256: sha(stable(price)) },
    },
    operation: {
      atomic: true,
      steps: ['TAKER_COUNTERSIGN', 'CONSTRUCT_FINAL_TRADE_JSON', 'SIGN_CLOSE1_ROOM_MESSAGE'],
      takerPreimage: { sha256: sha(accept), utf8: accept },
      finalTrade: { t: 'trade', season: policy.contest, terms, taker: owner.did,
        maker_sig: makerSig, taker_sig: 'SIGNER_GENERATES' },
      roomEnvelope: { room: policy.room, nonce: 'SIGNER_ALLOCATES',
        text: 'SIGNER_CONSTRUCTS_EXACT_FINAL_TRADE_JSON',
        preimageFormat: 'close1|<SIGNER_ALLOCATED_NONCE>|<EXACT_FINAL_TRADE_JSON>' },
    },
  };
  const f = { root, operationDir, owner, referee, price, policy, request,
    approvalPath: join(root, 'approval.json'), requestPath: join(root, 'request.json'),
    exports: {
      close1: row('close1', owner, '{"t":"note"}', '9000000000000000000'),
      'd-close1-price': row('d-close1-price', referee, JSON.stringify(price), '100'),
    } };
  f.save = () => {
    const bytes = JSON.stringify(request) + '\n';
    writeFileSync(f.requestPath, bytes, { mode: 0o600 });
    writeFileSync(f.approvalPath, JSON.stringify({ version: 1, policy, operationDir,
      requestSha256: sha(bytes) }),
      { mode: 0o600 });
  };
  f.save();
  return f;
}

// A real child process + anonymous pipe exercises ingress without real keys,
// systemd, live HTTP or production state. Mock proc facts only in this child.
const HARNESS = `
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
const f = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const originalRead = fs.readFileSync;
fs.readFileSync = (path, ...args) => {
  const proc = {
    '/proc/sys/kernel/osrelease': '6.6.87.2-microsoft-standard-WSL2\\n',
    '/proc/sys/kernel/core_pattern': f.unsafeCrash ? '|unsafe-capture\\n' : '\\n',
    '/proc/sys/kernel/core_uses_pid': '0\\n',
  };
  return Object.hasOwn(proc, path) ? Buffer.from(proc[path]) : originalRead(path, ...args);
};
syncBuiltinESMExports();
const { runCloseCallOneShot, parseOneShotArgs } = await import(${JSON.stringify(ENTRY)});
const originalAlloc = Buffer.alloc;
let seedBuffer;
Buffer.alloc = (size, ...args) => {
  const buffer = originalAlloc(size, ...args);
  if (size === 32 && !seedBuffer) seedBuffer = buffer;
  return buffer;
};
let seedReads = 0;
const chunks = [];
const iterator = process.stdin[Symbol.asyncIterator].bind(process.stdin);
process.stdin[Symbol.asyncIterator] = async function* () {
  for await (const chunk of { [Symbol.asyncIterator]: iterator }) {
    seedReads++; chunks.push(chunk); yield chunk;
  }
};
const calls = [];
try {
  const options = parseOneShotArgs(f.args);
  const result = await runCloseCallOneShot({ ...options,
    now: () => f.now ?? ${NOW},
    fetchImpl: async (url, options) => {
      calls.push({ url, method: options.method, redirect: options.redirect });
      if (f.networkFailure) throw new Error('synthetic network failure');
      const room = url.split('/')[4];
      return new Response(f.exports[room], { status: f.httpStatus ?? 200, headers: {
        'content-type': 'application/x-ndjson', 'x-room-generation': '1',
      } });
    },
  });
  console.log(JSON.stringify({ result, calls, seedReads,
    zeroized: seedBuffer.every(b => b === 0) && chunks.every(c => c.every(b => b === 0)) }));
} catch (error) {
  console.log(JSON.stringify({ error: error.code ?? error.message, calls, seedReads,
    zeroized: (!seedBuffer || seedBuffer.every(b => b === 0)) && chunks.every(c => c.every(b => b === 0)) }));
  process.stdin.destroy();
}
`;

async function run(f, overrides = {}, seed = SEED) {
  const args = ['--approval', f.approvalPath, '--request', f.requestPath,
    '--operation-dir', f.operationDir];
  const harnessPath = join(f.root, 'harness-input.json');
  writeFileSync(harnessPath, JSON.stringify({ args, exports: f.exports, ...overrides }));
  // Node spawn uses socketpairs for "pipe" stdio on Unix. Use os.pipe so the
  // signer sees an actual anonymous FIFO, exactly as the existing gate requires.
  const launcher = `
import os, subprocess, sys, json
fixture = json.load(open(sys.argv[3]))
seed = sys.stdin.buffer.read()
r, w = os.pipe()
os.write(w, seed)
os.close(w)
if fixture.get('inputKind') == 'file':
    os.close(r)
    r = os.open(fixture['args'][3], os.O_RDONLY)
elif fixture.get('inputKind') == 'fifo':
    os.close(r)
    path = sys.argv[3] + '.fifo'
    os.mkfifo(path, 0o600)
    r = os.open(path, os.O_RDWR | os.O_NONBLOCK)
child = subprocess.Popen([sys.argv[1], '--input-type=module', '-e', sys.argv[2], sys.argv[3]],
    stdin=r, pass_fds=(r,))
os.close(r)
sys.exit(child.wait())
`;
  const child = spawn('python3', ['-c', launcher, process.execPath, HARNESS, harnessPath],
    { stdio: ['pipe', 'pipe', 'pipe'] });
  let stdout = '', stderr = '';
  child.stdout.on('data', chunk => { stdout += chunk; });
  child.stderr.on('data', chunk => { stderr += chunk; });
  child.stdin.on('error', () => {});
  const completed = new Promise((resolve, reject) => {
    child.on('error', reject);
    child.on('close', code => {
      try { assert.equal(code, 0, stderr); resolve(JSON.parse(stdout)); }
      catch (error) { reject(error); }
    });
  });
  child.stdin.end(Buffer.from(seed));
  return completed;
}

test('one process returns only two verified signatures and the POST-ready package', async t => {
  const f = fixture(t);
  const out = await run(f);
  assert.equal(out.error, undefined);
  assert.equal(out.zeroized, true);
  assert.deepEqual(Object.keys(out.result).sort(), ['price', 'roomRecord', 'takerSignature']);
  assert.deepEqual(out.result.price, { sweep: 100, referenceAgeSeconds: '100', stale: false });
  const { roomRecord: r, takerSignature } = out.result;
  assert.equal(r.room, 'close1');
  assert.equal(r.sender, f.owner.did);
  assert.equal(r.nonce, '9000000000000000001');
  assert.equal(verifyDidSignature(r.sender, f.request.operation.takerPreimage.utf8, takerSignature), true);
  assert.equal(verifyDidSignature(r.sender, canonicalMessage(r.room, r.nonce, r.line), r.signature), true);
  assert.equal(JSON.parse(r.line).taker_sig, takerSignature);
  assert.deepEqual(out.calls, ['close1', 'd-close1-price'].map(room => ({
    url: 'https://technocore.chat/r/' + room + '/export', method: 'GET', redirect: 'manual',
  })));
  assert.equal(existsSync(join(f.operationDir, 'attempt.json')), true);
  const state = readFileSync(join(f.operationDir, 'state.json'), 'utf8');
  assert.equal(state.includes(SEED.toString('hex')), false);
  assert.equal(state.includes('taker_sig'), false);
});

test('explicit anonymous FD is accepted; regular file and named FIFO are denied', async t => {
  const f = fixture(t);
  const args = ['--approval', f.approvalPath, '--request', f.requestPath,
    '--operation-dir', f.operationDir, '--input-fd', '3'];
  assert.equal((await run(f, { args })).error, undefined);
  // FD 1 is a pipe, but cannot be an explicit secret input FD.
  const bad = fixture(t);
  const out = await run(bad, { args: args.map(v => v === '3' ? '1' : v) });
  assert.equal(out.error, 'ARGUMENTS_DENIED');
  assert.equal(out.seedReads, 0);
  for (const inputKind of ['file', 'fifo']) {
    const denied = fixture(t);
    const out = await run(denied, { inputKind });
    assert.equal(out.error, 'ANONYMOUS_INPUT_REQUIRED');
    assert.equal(out.seedReads, 0);
  }
});

test('invalid typed request and approval-byte mismatch stop before seed input', async t => {
  for (const mutate of [
    r => { r.action = 'SIGN_TEXT'; },
    r => { r.subject.direction = 'SHORT'; },
    r => { r.approved = true; },
    r => { r.operation.finalTrade.taker_sig = 'caller-selected'; },
  ]) {
    const f = fixture(t); mutate(f.request); f.save();
    const out = await run(f);
    assert.ok(out.error); assert.equal(out.seedReads, 0);
    assert.equal(readdirSync(f.operationDir).length, 0);
  }
  const f = fixture(t);
  writeFileSync(f.requestPath, JSON.stringify(f.request) + '\n\n');
  assert.equal((await run(f)).error, 'ONESHOT_APPROVAL_BINDING');
  const rebound = fixture(t);
  const alternate = mkdtempSync(join(rebound.root, 'alternate-'));
  const out = await run(rebound, { args: ['--approval', rebound.approvalPath,
    '--request', rebound.requestPath, '--operation-dir', alternate] });
  assert.equal(out.error, 'ONESHOT_APPROVAL_BINDING');
  assert.equal(out.seedReads, 0);
});

test('DID mismatch and malformed raw seed fail closed and wipe buffers', async t => {
  for (const seed of [Buffer.alloc(32, 0x77), Buffer.alloc(31), Buffer.alloc(33)]) {
    const f = fixture(t);
    const out = await run(f, {}, seed);
    assert.equal(out.error, seed.length === 32 ? 'DID_MISMATCH' : 'ONESHOT_SEED_LENGTH');
    assert.equal(out.zeroized, true); assert.deepEqual(out.calls, []);
    const retry = await run(f);
    assert.equal(retry.error, 'ONESHOT_NO_RETRY'); assert.equal(retry.seedReads, 0);
  }
});

test('changed current price requires replan, with no output or retry', async t => {
  const f = fixture(t);
  f.exports['d-close1-price'] = row('d-close1-price', f.referee,
    JSON.stringify({ ...f.price, n: 101 }), '101');
  const out = await run(f);
  assert.equal(out.error, 'CLOSE_CALL_REPLAN_REQUIRED');
  assert.equal(out.result, undefined); assert.equal(out.zeroized, true);
  assert.equal((await run(f)).error, 'ONESHOT_NO_RETRY');
});

test('duplicate process, partial/crashed attempt and network failure are non-retryable', async t => {
  const f = fixture(t);
  const outcomes = await Promise.all([run(f), run(f)]);
  assert.equal(outcomes.filter(o => o.result).length, 1);
  assert.equal(outcomes.filter(o => o.error === 'ONESHOT_NO_RETRY').length, 1);
  const crashed = fixture(t);
  writeFileSync(join(crashed.operationDir, 'attempt.json'), '');
  assert.equal((await run(crashed)).error, 'ONESHOT_NO_RETRY');
  const failed = fixture(t);
  const out = await run(failed, { networkFailure: true });
  assert.equal(out.error, 'READER_HTTP_FAILURE');
  assert.equal(out.calls.length, 1); assert.equal(out.zeroized, true);
  assert.equal((await run(failed)).error, 'ONESHOT_NO_RETRY');
});

test('WSL crash-safety failure precedes seed read and durable claim', async t => {
  const f = fixture(t);
  const out = await run(f, { unsafeCrash: true });
  assert.equal(out.error, 'WSL_CRASH_CAPTURE_UNSAFE');
  assert.equal(out.seedReads, 0); assert.deepEqual(out.calls, []);
  assert.equal(readdirSync(f.operationDir).length, 0);
});

test('approval/state ownership guards, policy expiry and invalid HTTP fail closed', async t => {
  const unsafe = fixture(t); chmodSync(unsafe.approvalPath, 0o666);
  assert.equal((await run(unsafe)).error, 'ONESHOT_PATH_UNSAFE');
  const linked = fixture(t);
  const link = join(linked.root, 'linked.json'); symlinkSync(linked.approvalPath, link);
  assert.equal((await run(linked, { args: ['--approval', link, '--request', linked.requestPath,
    '--operation-dir', linked.operationDir] })).error, 'ONESHOT_PATH_UNSAFE');
  const expired = fixture(t);
  assert.equal((await run(expired, { now: expired.policy.expiresAtMs })).error, 'POLICY_EXPIRED');
  const redirect = fixture(t);
  assert.equal((await run(redirect, { httpStatus: 302 })).error, 'READER_HTTP_FAILURE');
});

test('CLI rejects secret options, extra arguments and duplicate options', () => {
  for (const args of [[], ['--seed', 'dummy'],
    ['--approval', '/a', '--request', '/r', '--request', '/s'],
    ['--approval', '/a', '--request', '/r', '--operation-dir', '/o', '--secret', 'dummy']]) {
    assert.throws(() => parseOneShotArgs(args), /ARGUMENTS_DENIED/);
  }
});
