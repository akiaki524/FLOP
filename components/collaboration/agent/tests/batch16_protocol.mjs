// Batch 16 research harness. No HTTP client, live example, MCP server, or Real signer.
// Official protocol code is reused unchanged except Node's built-in type erasure.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, readdirSync, mkdirSync, existsSync } from 'node:fs';
import { createHash, randomBytes } from 'node:crypto';
import { stripTypeScriptTypes } from 'node:module';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import net from 'node:net';
import tls from 'node:tls';
import http from 'node:http';
import https from 'node:https';
import dgram from 'node:dgram';

const ROOT = resolve(import.meta.dirname, '..');
const OUT = resolve(ROOT, '.local/batch16');
const SOURCE = resolve(ROOT, '.local/research/tclk');
const RUNTIME = resolve(OUT, 'official-runtime');
const PIN = '5cc4ab93efbc8999a3a7e1471b639deca25998ea';
const resultName = process.argv[3];
if (resultName !== undefined) {
  assert(['dryrun', 'replay'].includes(process.argv[2]));
  assert(/^[a-z0-9][a-z0-9_-]*\.json$/.test(resultName));
}
const json = p => JSON.parse(readFileSync(resolve(OUT, p), 'utf8'));
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const save = (name, value) => writeFileSync(resolve(OUT, resultName ?? name), JSON.stringify(value, null, 2) + '\n', { flag: 'wx' });
const denyNetwork = () => { throw new Error('Batch16 offline transport: network forbidden'); };
globalThis.fetch = denyNetwork;
globalThis.WebSocket = class { constructor() { denyNetwork(); } };
net.Socket.prototype.connect = denyNetwork;
net.connect = net.createConnection = tls.connect = http.request = http.get = https.request = https.get = denyNetwork;
dgram.createSocket = denyNetwork;

function build() {
  const files = readdirSync(resolve(SOURCE, 'src')).filter(x => x.endsWith('.ts')).map(x => 'src/' + x);
  files.push('mcp/src/signing.ts');
  const manifest = { pin: PIN, package: JSON.parse(readFileSync(resolve(SOURCE, 'package.json'))),
    observed_main: json('official-retry/main.json').sha, node: process.version,
    method: 'node:module.stripTypeScriptTypes mode=strip; no semantic source edits, no install scripts', files: [] };
  assert.equal(readFileSync(resolve(SOURCE, 'src/transcript.ts'), 'utf8'), readFileSync(resolve(OUT, 'official-retry/transcript.ts'), 'utf8'));
  assert.equal(readFileSync(resolve(SOURCE, 'SPEC.md'), 'utf8'), readFileSync(resolve(OUT, 'official-retry/SPEC.md'), 'utf8'));
  assert.equal(readFileSync(resolve(SOURCE, 'package.json'), 'utf8'), readFileSync(resolve(OUT, 'official-retry/package.json'), 'utf8'));
  for (const file of files) {
    const input = readFileSync(resolve(SOURCE, file), 'utf8');
    const relative = file === 'mcp/src/signing.ts' ? 'signing.js' : file.replace(/\.ts$/, '.js');
    const target = resolve(RUNTIME, relative);
    mkdirSync(resolve(target, '..'), { recursive: true });
    const output = stripTypeScriptTypes(input, { mode: 'strip' });
    writeFileSync(target, output, { flag: 'wx' });
    manifest.files.push({ source: file, source_sha256: digest(input), built: relative, built_sha256: digest(output) });
  }
  writeFileSync(resolve(RUNTIME, 'package.json'), '{"private":true,"type":"module"}\n', { flag: 'wx' });
  save('runtime-manifest.json', manifest);
  console.log(JSON.stringify({ pin: PIN, observed_main: manifest.observed_main, files: files.length, version: manifest.package.version }));
}

async function api() {
  const manifest = json('runtime-manifest.json');
  assert.equal(manifest.pin, PIN);
  for (const file of manifest.files) {
    assert.equal(digest(readFileSync(resolve(RUNTIME, file.built))), file.built_sha256);
    assert.equal(digest(readFileSync(resolve(SOURCE, file.source))), file.source_sha256);
  }
  return import(pathToFileURL(resolve(RUNTIME, 'src/index.js')));
}

async function analyze() {
  const t = await api();
  const rows = json('board.json');
  const counts = {}, invalid = [], valid = [], normal = [];
  const count = name => counts[name] = (counts[name] ?? 0) + 1;
  for (const row of rows) {
    let record;
    try { record = t.transcriptRecord(t.OFFER_ROOM, row); }
    catch (error) { count('normalization_rejected'); invalid.push({ seq: row.seq, stage: 'normalization', reason: error.message }); continue; }
    normal.push(record);
    const verification = t.verifyTranscriptRecord(record);
    if (!verification.ok) { count('signature_rejected'); invalid.push({ seq: row.seq, stage: 'signature', reason: verification.reason }); continue; }
    count('signature_ok');
    const frame = t.tryDecodeFrame(record.line);
    if (!frame) { count(record.line.startsWith('tclk1 ') ? 'unsupported_frame' : 'non_frame'); continue; }
    if (frame.from !== record.sender) { count('sender_mismatch'); continue; }
    count('authenticated_' + frame.type);
    valid.push({ record, frame });
  }
  const offers = new Map(), groups = new Map(), activity = new Map(), rejected = [];
  for (const { record, frame } of valid) {
    if (frame.type === 'offer') { if (!offers.has(frame.id)) offers.set(frame.id, record); }
    else if (frame.type === 'accept') {
      const offer = offers.get(frame.ref);
      if (!offer) { count('accept_missing_preceding_offer'); continue; }
      const handshake = t.findContractHandshake([offer, record], frame.contract);
      assert(handshake);
      const folded = t.foldTranscript([handshake.offer, handshake.accept]);
      if (folded.state?.status !== 'accepted') {
        count('accept_guard_rejected'); rejected.push({ seq: record.seq, contract: frame.contract, steps: folded.steps }); continue;
      }
      count('valid_accept_records');
      const group = groups.get(frame.ref) ?? { offer: t.decodeFrame(offer.line), offer_seq: offer.seq, offer_ts: offer.timestampMs, accepts: [] };
      if (!group.accepts.some(x => x.contract === frame.contract)) group.accepts.push({ contract: frame.contract, seq: record.seq,
        timestampMs: record.timestampMs, sender: record.sender, room: t.dealRoom(frame.contract), paper_note: t.paperNote(frame.contract) });
      groups.set(frame.ref, group);
    } else if (frame.contract) {
      const list = activity.get(frame.contract) ?? [];
      list.push({ seq: record.seq, type: frame.type, sender: record.sender });
      activity.set(frame.contract, list);
    }
  }
  const multiple = [...groups.values()].filter(g => g.accepts.length > 1);
  for (const g of groups.values()) for (const [index, a] of g.accepts.entries()) {
    a.observed_rank = index + 1;
    a.board_activity = activity.get(a.contract) ?? [];
  }
  multiple.sort((a, b) => b.accepts.filter(x => x.board_activity.length).length - a.accepts.filter(x => x.board_activity.length).length || b.accepts.length - a.accepts.length);
  save('board-analysis.json', { counts, invalid, rejected, groups: [...groups.values()],
    multiple_offer_count: multiple.length, unique_contracts: [...groups.values()].reduce((n, g) => n + g.accepts.length, 0) });
  console.log(JSON.stringify({ counts, multiple_offer_count: multiple.length, top: multiple.slice(0, 5) }, null, 2));
}

async function replay() {
  const t = await api();
  const board = json('board.json').map(row => t.transcriptRecord(t.OFFER_ROOM, row));
  const selected = json('selected.json');
  const results = [];
  for (const item of selected) {
    const handshake = t.findContractHandshake(board, item.contract);
    assert(handshake);
    const directory = existsSync(resolve(OUT, 'public', item.room + '.jsonl')) ? 'public' : 'public-remaining';
    const path = resolve(OUT, directory, item.room + '.jsonl');
    if (!existsSync(path)) { results.push({ ...item, missing: 'public read unavailable' }); continue; }
    const raw = readFileSync(path, 'utf8');
    let direct = { ok: true };
    try { t.parseTranscriptExport(item.room, raw); } catch (error) { direct = { ok: false, reason: error.message }; }
    // Exact integer nonce conversion already performed by Python, retaining raw bytes separately.
    const rows = json('public-lossless.json')[item.room];
    const records = rows.map(row => t.transcriptRecord(item.room, row));
    const folded = t.foldTranscript([handshake.offer, handshake.accept, ...records]);
    const signature = records.map(record => ({ seq: record.seq, ...t.verifyTranscriptRecord(record) }));
    const deliveries = records.filter(r => !r.line.startsWith('tclk1 ')).map(r => ({ ...r, verification: t.verifyTranscriptRecord(r) }));
    const notePath = resolve(OUT, directory, item.room + '.note');
    const noteRaw = existsSync(notePath) ? readFileSync(notePath, 'utf8') : '';
    const noteLine = noteRaw.split('\n').find(line => line.startsWith('tclkpaper1 '));
    const paper = noteLine ? t.decodePaperRecord(noteLine) : null;
    let paperConsistent = false;
    if (paper && folded.state?.statement) {
      paperConsistent = paper.status === folded.state.status && paper.statement === folded.state.statement
        && paper.refundAfterMs === folded.state.offer.refundAfterMs && paper.lock === folded.state.offer.lock
        && (paper.status !== 'claimed' || t.verifySecret(paper.lock, paper.statement, paper.secret));
    }
    results.push({ ...item, raw_sha256: digest(raw), direct_export_api: direct, records: records.length,
      handshake, steps: folded.steps, signature, deliveries, state: folded.state, paper, paperConsistent,
      counterparty_evaluation: 'inspect signed free-text independently; terminal receipt alone is not grading' });
  }
  save('real-replay.json', results);
  console.log(JSON.stringify(results.map(r => ({ contract: r.contract, rank: r.observed_rank, records: r.records,
    state: r.state?.status, accepted_steps: r.steps?.filter(s => s.ok), rejected_steps: r.steps?.filter(s => !s.ok),
    paper: r.paper, paperConsistent: r.paperConsistent, deliveries: r.deliveries })), null, 2));
}

async function dryrun() {
  const t = await api();
  const { signerFromSeed, canonicalMessage, sweep } = await import(pathToFileURL(resolve(RUNTIME, 'signing.js')));
  // Ephemeral TEST seeds only. Never serialize keys or read environment credentials.
  const payer = signerFromSeed(randomBytes(32)), payee = signerFromSeed(randomBytes(32)), other = signerFromSeed(randomBytes(32));
  const now = 1800000000000;
  const solver = json('solver.json');
  assert.equal(solver.proof.valid, true);
  // Wire amount must be positive even though PaperRail moves no value.
  const offer = t.makeOffer({ from: payer.did, role: 'payer', lock: 'hash', amount: '1', asset: 'PAPER', rails: ['paper'],
    claimByMs: now + 600000, refundAfterMs: now + 1200000, expiresMs: now + 300000,
    job: { proto: 'a2a', id: 'batch16-TEST-gcd', context: 'TEST ONLY; frozen bundle sha256=' +
      solver.bundle_sha256 + '; signed gcd=<g> lcm=<l>, then reveal.' } });
  const hash = t.generateHashLock();
  const accept = t.makeAccept(offer, { from: payee.did, statement: hash.hash });
  const alternate = t.makeAccept(offer, { from: other.did, statement: t.generateHashLock().hash });
  const room = t.dealRoom(accept.contract);
  let seq = 0;
  function signed(frameOrText, signer, target, timestampMs = now + 1000) {
    const line = typeof frameOrText === 'string' ? sweep(frameOrText) : t.encodeFrame(frameOrText);
    const nonce = String(1800000000000000000n + BigInt(++seq));
    return { room: target, seq, timestampMs, sender: signer.did, nonce,
      signature: signer.sign(canonicalMessage(target, nonce, line)), line };
  }
  const o = signed(offer, payer, t.OFFER_ROOM, now), a = signed(accept, payee, t.OFFER_ROOM),
    alt = signed(alternate, other, t.OFFER_ROOM, now + 500);
  const checks = [];
  function check(name, fn) { fn(); checks.push({ name, passed: true }); }
  function rejected(name, records, reason) {
    check(name, () => { const f = t.foldTranscript(records); assert.equal(f.steps.at(-1).ok, false);
      if (reason) assert.match(f.steps.at(-1).reason, reason); });
  }
  const accepted = t.foldTranscript([o, a]);
  assert.equal(accepted.state.status, 'accepted');
  const notes = new t.MemoryNoteStore();
  let clock = now + 2000;
  const rail = new t.PaperRail(notes, () => clock);
  const terms = t.lockTerms(accepted.state), ref = await rail.lock(terms);
  assert(await rail.verifyLock(terms, ref));
  const lock = { type: 'lock', from: payer.did, contract: accept.contract, rail: 'paper', ref };
  const l = signed(lock, payer, room, clock);
  const heartbeat = signed(t.makeHeartbeat({ from: payee.did, contract: accept.contract, note: 'TEST only' }), payee, room);
  const delivery = signed(solver.delivery_candidate, payee, room, now + 3000);
  const reveal = { type: 'reveal', from: payee.did, contract: accept.contract, ref, secret: hash.preimage };
  const r = signed(reveal, payee, room, now + 4000);
  await rail.claim(ref, hash.preimage);
  const receipt = { type: 'receipt', from: payer.did, contract: accept.contract, rail: 'paper', ref, outcome: 'claimed' };
  const receiptRecord = signed(receipt, payer, room, now + 5000);
  const transcript = [o, a, heartbeat, l, delivery, r, receiptRecord];
  const folded = t.foldTranscript(transcript);
  check('mock full lifecycle: signed delivery is outside protocol frame grammar', () => {
    assert.equal(folded.state.status, 'claimed'); assert.equal(folded.steps.filter(s => !s.ok).length, 1);
    assert(t.verifyTranscriptRecord(delivery).ok); assert(folded.steps.at(-1).ok);
  });
  check('PaperRail claimed record consistent', () => {
    const note = t.paperNote(accept.contract), rec = t.decodePaperRecord(notes.raw(note.ns, note.key));
    assert.equal(rec.status, 'claimed'); assert(t.verifySecret(rec.lock, rec.statement, rec.secret));
  });
  rejected('tampered signature', [o, { ...a, signature: 'A'.repeat(86) }], /signature/);
  rejected('wrong signed sender', [o, { ...a, sender: other.did }], /signature/);
  rejected('valid signature but frame.from mismatch', [o, signed(accept, other, t.OFFER_ROOM)], /sender/);
  rejected('signature bound to different room', [o, { ...a, room }], /signature/);
  rejected('valid signature in wrong protocol room', [o, a, signed(lock, payer, t.OFFER_ROOM)], /deal room/);
  rejected('accept exactly at expiry', [o, { ...a, timestampMs: offer.expiresMs }], /expired/);
  rejected('wrong contract LOCK', [o, a, signed({ ...lock, contract: alternate.contract }, payer, room)], /different contract/);
  rejected('wrong LOCK payer', [o, a, signed({ ...lock, from: payee.did }, payee, room)], /payer/);
  rejected('wrong offered rail', [o, a, signed({ ...lock, rail: 'memory' }, payer, room)], /not offered/);
  rejected('LOCK at refund boundary', [o, a, { ...l, timestampMs: offer.refundAfterMs }], /refund/);
  rejected('REVEAL before LOCK', [o, a, r], /status accepted/);
  rejected('wrong REVEAL preimage', [o, a, l, signed({ ...reveal, secret: '0x' + '00'.repeat(32) }, payee, room)], /secret/);
  rejected('wrong REVEAL rail ref', [o, a, l, signed({ ...reveal, ref: alternate.contract }, payee, room)], /rail ref/);
  rejected('REVEAL at refund boundary', [o, a, l, { ...r, timestampMs: offer.refundAfterMs }], /refund/);
  rejected('receipt before terminal', [o, a, l, receiptRecord], /terminal/);
  rejected('receipt wrong outcome', [o, a, l, r, signed({ ...receipt, outcome: 'refunded' }, payer, room)], /does not match/);
  rejected('duplicate ACCEPT', [o, a, a], /status accepted/);
  rejected('duplicate LOCK', [o, a, l, l], /status locked/);
  rejected('duplicate REVEAL', [o, a, l, r, r], /status claimed/);
  rejected('mix multiple ACCEPTs into one fold', [o, alt, a], /status accepted/);
  check('later ACCEPT individually valid through official handshake API', () => {
    const h = t.findContractHandshake([o, alt, a], accept.contract);
    assert.equal(t.foldTranscript([h.offer, h.accept, l]).state.status, 'locked');
  });
  check('one offer permits multiple locked contracts in separate folds', () => {
    const otherLock = signed({ ...lock, contract: alternate.contract, ref: alternate.contract }, payer, t.dealRoom(alternate.contract));
    assert.equal(t.foldTranscript([o, alt, otherLock]).state.status, 'locked');
    assert.equal(t.foldTranscript([o, a, l]).state.status, 'locked');
  });
  check('accepted has no automatic expiry transition', () => {
    const hb = { ...heartbeat, timestampMs: offer.refundAfterMs + 1 };
    assert.equal(t.foldTranscript([o, a, hb]).state.status, 'accepted');
    assert(t.foldTranscript([o, a, hb]).steps.at(-1).ok);
  });
  const cancel = signed({ type: 'cancel', from: payee.did, contract: accept.contract }, payee, room);
  check('cancel before lock', () => assert.equal(t.foldTranscript([o, a, cancel]).state.status, 'cancelled'));
  rejected('cancel after lock', [o, a, l, cancel], /status locked/);
  const refund = signed({ type: 'refund', from: payer.did, contract: accept.contract, ref }, payer, room, offer.refundAfterMs);
  check('refund at deadline', () => assert.equal(t.foldTranscript([o, a, l, refund]).state.status, 'refunded'));
  rejected('refund before deadline', [o, a, l, { ...refund, timestampMs: offer.refundAfterMs - 1 }], /not open/);
  check('claimBy is advisory: late reveal before refund accepted by protocol', () => {
    assert(t.foldTranscript([o, a, l, { ...r, timestampMs: offer.claimByMs + 1 }]).steps.at(-1).ok);
  });
  check('timestamp/seq outside signature and fold trusts supplied ordering', () => {
    assert(t.verifyTranscriptRecord({ ...a, seq: 999, timestampMs: offer.expiresMs }).ok);
  });
  check('direct official export parser rejects unsafe numeric nonce', () => {
    const raw = JSON.stringify({ seq: 1, ts: new Date(now).toISOString(), from: a.sender,
      text: a.line, sig: a.signature, nonce: a.nonce }).replace('"' + a.nonce + '"', a.nonce);
    assert.throws(() => t.parseTranscriptExport(t.OFFER_ROOM, raw), /nonce/);
  });
  check('missing signature rejected', () => assert.equal(t.verifyTranscriptRecord({ ...a, signature: null }).ok, false));
  check('HTTP and socket transports disabled', () => { assert.throws(() => globalThis.fetch('https://invalid.test'), /forbidden/);
    assert.throws(() => net.connect(443, 'invalid.test'), /forbidden/); });
  // Ambiguous response is simulated entirely in memory, never by posting a real frame.
  const sent = [];
  const mockSend = record => { sent.push(record); throw new Error('TEST response lost after append'); };
  assert.throws(() => mockSend(a), /response lost/);
  const sameEnvelope = (x, y) => ['room', 'sender', 'nonce', 'signature', 'line'].every(k => x[k] === y[k]);
  check('lost response found by exact authenticated envelope', () => assert(sent.some(x => sameEnvelope(x, a) && t.verifyTranscriptRecord(x).ok)));
  check('not observed remains ambiguous: zero automatic retry', () => {
    const retained = [];
    assert(!retained.some(x => sameEnvelope(x, a))); assert.equal(sent.length, 1);
  });
  check('new frame nonce creates a different contract', () => assert.notEqual(t.makeAccept(offer,
    { from: payee.did, statement: hash.hash, nonce: '0000000000000001' }).contract, accept.contract));
  check('same receipt can repeat: acknowledgment has no transition', () => {
    assert(t.foldTranscript([o, a, l, r, receiptRecord, receiptRecord]).steps.at(-1).ok);
  });
  assert.equal(await rail.verifyLock(terms, alternate.contract), false);
  checks.push({ name: 'PaperRail wrong ref rejected', passed: true });
  const noRail = new t.PaperRail(new t.MemoryNoteStore(), () => now);
  assert.equal(await noRail.verifyLock(terms, ref), false);
  checks.push({ name: 'signed LOCK alone does not establish PaperRail lock', passed: true });
  const secondStore = new t.MemoryNoteStore();
  let secondClock = now;
  const secondRail = new t.PaperRail(secondStore, () => secondClock);
  const secondRef = await secondRail.lock(terms);
  await assert.rejects(secondRail.claim(secondRef, '0x' + '00'.repeat(32)), /secret/);
  checks.push({ name: 'PaperRail wrong preimage rejected', passed: true });
  await assert.rejects(secondRail.refund(secondRef), /before/);
  checks.push({ name: 'PaperRail early refund rejected', passed: true });
  secondClock = offer.refundAfterMs;
  await assert.rejects(secondRail.claim(secondRef, hash.preimage), /after/);
  checks.push({ name: 'PaperRail expired claim rejected', passed: true });
  await secondRail.refund(secondRef);
  assert.equal((await secondRail.read(secondRef)).status, 'refunded');
  checks.push({ name: 'PaperRail refund reaches terminal', passed: true });
  check('PaperRail legacy JSON record is unsupported, never repaired', () => {
    assert.equal(t.decodePaperRecord(JSON.stringify({ status: 'claimed', secret: hash.preimage })), null);
  });
  check('Paper amount zero is not a valid wire amount', () => {
    assert.throws(() => t.makeOffer({ ...offer, amount: '0' }), /amount/);
  });
  save('dryrun.json', { kind: 'TEST fixture only', solver_source: solver.source, checks,
    transcript, steps: folded.steps, terminal: folded.state.status,
    receipt_verified: folded.steps.at(-1).ok, mock_paper_status: (await rail.read(ref)).status,
    real_counterparty_acceptance: false, real_signer_access: false, external_writes: 0,
    reconciliation: { appended_then_response_lost: 'OBSERVED_IDENTICAL_AUTHENTICATED_ENVELOPE',
      absent_in_retained_window: 'AMBIGUOUS_STOP', automatic_retries: 0 },
    isolation: 'in-memory NoteStore and mockSend; network entry points throw; no live/client/server modules imported' });
  console.log(JSON.stringify({ checks: checks.length, passed: checks.length, terminal: folded.state.status, external_writes: 0 }));
}

async function golden() {
  await api();
  const source = readFileSync(resolve(SOURCE, 'tests/vectors.test.ts'), 'utf8');
  const adapter = `import assert from 'node:assert/strict';
    const describe = (_name, fn) => fn();
    const it = (name, fn) => { fn(); console.log('PASS ' + name); };
    const expect = value => ({toBe: expected => assert.equal(value, expected),
      toMatch: pattern => assert.match(value, pattern),
      toContain: part => assert(value.includes(part))});`;
  const adapted = source.replace('import { describe, it, expect } from "vitest";', adapter);
  assert.notEqual(adapted, source);
  const path = resolve(RUNTIME, 'tests/vectors.test.mjs');
  mkdirSync(resolve(path, '..'), { recursive: true });
  writeFileSync(path, stripTypeScriptTypes(adapted, { mode: 'strip' }), { flag: 'wx' });
  await import(pathToFileURL(path));
  save('golden.json', { source: 'tests/vectors.test.ts', source_sha256: digest(source),
    passed: 3, adaptation: 'vitest describe/it/expect replaced with synchronous node:assert adapter; original vectors and cases unchanged' });
}

async function limitations() {
  const t = await api();
  const rows = json('board.json');
  const reasons = {}, examples = {};
  for (const row of rows) {
    if (!row.text.startsWith('tclk1 ')) continue;
    try { t.decodeFrame(row.text); }
    catch (error) {
      const reason = error.message.split(': ').slice(0, 2).join(': ');
      reasons[reason] = (reasons[reason] ?? 0) + 1;
      examples[reason] ??= { seq: row.seq, full_reason: error.message };
    }
  }
  const rankCoverage = json('selected.json').map(item => {
    const between = rows.filter(row => row.seq >= item.offer_seq && row.seq <= item.seq);
    return { contract: item.contract, first_seq: item.offer_seq, last_seq: item.seq,
      records: between.length, missing_seq: item.seq - item.offer_seq + 1 - between.length,
      offer_to_accept_ms: item.timestampMs - Date.parse(between[0].ts) };
  });
  let direct;
  try { t.parseTranscriptExport(t.OFFER_ROOM, readFileSync(resolve(ROOT, '.local/batch15/session-01/export.bin'), 'utf8')); direct = 'accepted'; }
  catch (error) { direct = error.message; }
  save('limitations.json', { unsupported_reasons: reasons, examples, rankCoverage, batch15_direct_export: direct });
  console.log(JSON.stringify({ unsupported_reasons: reasons, examples, rankCoverage, batch15_direct_export: direct }, null, 2));
}

const modes = { build, analyze, replay, dryrun, golden, limitations };
assert(Object.hasOwn(modes, process.argv[2]), 'mode: build|analyze|replay|dryrun|golden|limitations');
await modes[process.argv[2]]();
