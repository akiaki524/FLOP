import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { official } from '../src/collaboration_agent/pilot_protocol.mjs';
import { validateNoncePacket } from '../src/collaboration_agent/real_nonce.mjs';
const { signing } = await official();
const signer = signing.signerFromSeed(Buffer.alloc(32, 7)), now = Date.now();
const line = 'offline public nonce fixture 🧪';
function fromExport(nonce = '9007199254740993123', filler = false) {
  const sig = signer.sign(signing.canonicalMessage('tclk-offers', nonce, line));
  // Nonce is an unquoted JSON integer; never route it through JS Number.
  const raw = (filler ? ('{"seq":7000000,"text":"filler"}\n').repeat(40000) : '')
    + JSON.stringify({ seq: 7000001, ts: new Date(now).toISOString(), from: signer.did,
      sig, text: line }).slice(0, -1) + ',"nonce":' + nonce + '}\n';
  const result = spawnSync('python3', ['-B', '-c',
    'import json,sys; sys.path.insert(0,"src"); from collaboration_agent.real_nonce import parse_observation; print(json.dumps(parse_observation(sys.stdin.buffer.read(),sys.argv[1],captured_at_ms=int(sys.argv[2]),generation=1)))',
    signer.did, String(now)], { input: raw, encoding: 'utf8', timeout: 10000 });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}
const packet = fromExport(), options = { expectedDid: signer.did, nowMs: now };
assert.equal((await validateNoncePacket(packet, options)).observedNonce, packet.observedNonce);
let passed = 1;
for (const mutate of [
  p => { p.records[0].nonce = 9007199254740993123; },
  p => { p.records[0].nonce = '10000000000000000000'; },
  p => { p.records[0].nonce = '01'; },
  p => { p.records[0].signature = null; },
  p => { p.records[0].signature = 'a'.repeat(86); },
  p => { p.records[0].seq = 0; },
  p => { p.records[0].sender = 'other'; },
  p => { p.records.push(p.records[0]); },
  p => { p.source.generation = 2; },
  p => { p.source.url += '?since=1'; },
  p => { p.source.rawBytes = 10 << 20; },
  p => { p.coverage.complete = false; },
  p => { p.coverage.basis = 'unknown'; },
  p => { p.coverage.tailStart = 1; },
  p => { p.coverage.tailBytes--; },
  p => { p.coverage.lineCount = 0; },
  p => { p.observedNonce = '0'; },
  p => { p.observedNonce = null; p.observedNone = true; },
  p => { p.verifiedAtMs -= 30001; p.source.capturedAt = p.verifiedAtMs; },
  p => { p.verifiedAtMs++; p.source.capturedAt = p.verifiedAtMs; },
  p => { p.did = 'other'; },
  p => { p.version = 1; },
]) {
  const changed = structuredClone(packet); mutate(changed);
  await assert.rejects(validateNoncePacket(changed, options)); passed++;
}
for (const nonce of ['9007199254740993', '9999999999999999999']) {
  const p = fromExport(nonce, true);
  assert.equal((await validateNoncePacket(p, options)).observedNonce, nonce);
  assert(p.coverage.tailStart > 0);
  assert(Buffer.byteLength(JSON.stringify(p)) < 65536); passed++;
}
const empty = { ...packet, observedNonce: null, observedNone: true, records: [],
  source: { ...packet.source, rawBytes: 0 },
  coverage: { ...packet.coverage, tailStart: 0, tailBytes: 0, lineCount: 0 } };
assert.equal((await validateNoncePacket(empty, options)).observedNone, true); passed++;
console.log(JSON.stringify({ passed, failed: 0, externalWrites: 0 }));
