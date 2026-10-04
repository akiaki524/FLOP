// Narrow Offline Pilot support. Protocol bytes, IDs, verification and fold stay official.
import { createHash } from 'node:crypto';
import { readFileSync, lstatSync } from 'node:fs';
import { resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import net from 'node:net';
import tls from 'node:tls';
import http from 'node:http';
import https from 'node:https';
import dgram from 'node:dgram';

export const ROOT = resolve(import.meta.dirname, '../..');
export const RUNTIME = resolve(ROOT, '.local/batch16/official-runtime');
export const TYPES = Object.freeze(['ACCEPT', 'INITIAL_HEARTBEAT', 'DELIVERY_GCD', 'REVEAL']);
export const POLICY = Object.freeze({ freshMs: 30_000, acceptMarginMs: 60_000,
  claimMarginMs: 120_000, refundGapMs: 60_000, sessionMs: 900_000, maxRecords: 64 });
export class Denied extends Error {
  constructor(code) { super(code); this.code = code; }
}
export const requireThat = (condition, code) => { if (!condition) throw new Denied(code); };
export const digest = value => createHash('sha256').update(value).digest('hex');
export const objectDigest = value => digest(JSON.stringify(value)); // local approval schema, NOT tclk encoding
export const envelopeDigest = record => objectDigest(Object.fromEntries(
  ['room', 'sender', 'nonce', 'signature', 'line'].map(key => [key, record[key]])));
export function outputGuard(confidential, data) {
  const text = JSON.stringify(data);
  const forbidden = /"(?:seed|privateKey|preimage|secret|signature|sig|signedRecord|signedRequest)"\s*:/;
  requireThat(!forbidden.test(text) && !confidential.some(value => text.includes(value)), 'SECRET_OUTPUT_BLOCKED');
  return text;
}
export function testNonce(room, nonces, at, mode) {
  requireThat(mode === 'TEST_EPHEMERAL', 'REAL_NONCE_POLICY_UNDECIDED');
  const next = BigInt(at) > (nonces.get(room) ?? 0n) ? BigInt(at) : nonces.get(room) + 1n;
  requireThat(next <= BigInt(Number.MAX_SAFE_INTEGER), 'TEST_NONCE_EXHAUSTED');
  nonces.set(room, next); return String(next);
}
// Relevance is a filter, not authentication. Relevant/party records still pass
// the official verifier and fold; unrelated room traffic never opens a contract.
export function contractRecords(t, records, offer, accept, did) {
  return records.filter(record => {
    let frame = null;
    try { frame = t.tryDecodeFrame(record?.line ?? ''); } catch { /* classify below */ }
    let claims = frame;
    if (!claims && typeof record?.line === 'string' && record.line.startsWith('tclk1 ')) {
      try { claims = JSON.parse(record.line.slice(6)); } catch { /* not a trusted frame */ }
    }
    const party = [offer.from, did].includes(record?.sender) || [offer.from, did].includes(claims?.from);
    const related = claims?.contract === accept.contract || claims?.id === offer.id
      || (!frame && typeof record?.line === 'string' && record.line.includes(accept.contract));
    if (!party && !related) return false;
    verifyRecord(t, record);
    if (claims?.from) requireThat(claims.from === record.sender, 'TRANSCRIPT_REJECTED');
    requireThat(record.room === t.OFFER_ROOM || record.room === t.dealRoom(accept.contract), 'WRONG_ROOM');
    if (record.room === t.OFFER_ROOM) {
      if (frame?.type === 'offer' && frame.id !== offer.id) return false;
      if (frame?.type === 'accept' && frame.contract !== accept.contract) {
        requireThat(frame.from !== did, 'TRANSCRIPT_REJECTED'); return false;
      }
      if (!related && !frame) return false;
    }
    // Valid ordinary messages do not transition protocol state.
    if (!frame && !record.line.startsWith('tclk1')) return false;
    return true;
  });
}
export function fields(value, names) {
  requireThat(value !== null && typeof value === 'object' && !Array.isArray(value), 'INVALID_REQUEST');
  requireThat(Object.keys(value).sort().join(',') === [...names].sort().join(','), 'INVALID_FIELDS');
}
export function blockNetwork() {
  const deny = () => { throw new Denied('NETWORK_DISABLED'); };
  globalThis.fetch = deny;
  globalThis.WebSocket = class { constructor() { deny(); } };
  net.Socket.prototype.connect = deny;
  net.connect = net.createConnection = tls.connect = http.get = http.request = https.get = https.request = deny;
  dgram.createSocket = deny;
}
export async function official() {
  try {
    const pin = JSON.parse(readFileSync(resolve(import.meta.dirname, 'tclk_pin.json'), 'utf8'));
    requireThat(pin.commit === '5cc4ab93efbc8999a3a7e1471b639deca25998ea', 'PIN_MISMATCH');
    for (const [relative, expected] of Object.entries(pin.files)) {
      const path = resolve(RUNTIME, relative);
      requireThat(path.startsWith(RUNTIME + sep), 'PIN_PATH');
      let parent = path;
      while (parent.startsWith(RUNTIME)) {
        requireThat(!lstatSync(parent).isSymbolicLink(), 'PIN_SYMLINK');
        if (parent === RUNTIME) break;
        parent = resolve(parent, '..');
      }
      requireThat(lstatSync(path).isFile() && digest(readFileSync(path)) === expected, 'PIN_MISMATCH');
    }
    return { t: await import(pathToFileURL(resolve(RUNTIME, 'src/index.js'))),
      signing: await import(pathToFileURL(resolve(RUNTIME, 'signing.js'))) };
  } catch { throw new Denied('OFFICIAL_RUNTIME_UNAVAILABLE'); }
}
export function verifyRecord(t, record) {
  fields(record, ['room', 'seq', 'timestampMs', 'sender', 'nonce', 'signature', 'line']);
  requireThat(Number.isSafeInteger(record.seq) && record.seq >= 0 && Number.isSafeInteger(record.timestampMs)
    && record.timestampMs >= 0, 'INVALID_RECORD_METADATA');
  requireThat(typeof record.line === 'string' && record.line.length <= 4096, 'INVALID_RECORD');
  requireThat(typeof record.nonce === 'string' && /^(0|[1-9][0-9]{0,18})$/.test(record.nonce), 'LOSSLESS_NONCE_REQUIRED');
  requireThat(t.verifyTranscriptRecord(record).ok, 'INVALID_SIGNATURE');
}
export function safeState(state) {
  return state ? { status: state.status, contract: state.contract ?? null, rail: state.rail ?? null,
    railRef: state.railRef ?? null } : null;
}
