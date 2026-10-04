import { createHash } from 'node:crypto';
import { canonicalMessage, verifyDidSignature } from './technocore_signing.mjs';

export const READ_BUDGET = 1 << 20;
export const MAX_EXPORT = 10 << 20;
const ROOM_RE = /^[a-z0-9][a-z0-9_-]{0,47}$/;
const DID_RE = /^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$/;
const DECIMAL_NONCE_RE = /^(0|[1-9][0-9]{0,18})$/;
const MAX_SAFE = Number.MAX_SAFE_INTEGER;

function fail(code) { throw new Error(code); }
function sha256(value) { return createHash('sha256').update(value).digest('hex'); }
function timestampMs(value) {
  if (typeof value !== 'string' || value.length === 0) fail('EXPORT_TIMESTAMP_INVALID');
  const parsed = Date.parse(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0) fail('EXPORT_TIMESTAMP_INVALID');
  return parsed;
}

function parseLosslessRecord(rawLine) {
  let text;
  try { text = Buffer.from(rawLine).toString('utf8'); } catch { return null; }
  if (!Buffer.from(text, 'utf8').equals(Buffer.from(rawLine))) return null;
  const matches = [...text.matchAll(/"nonce":(-?[0-9]+)/g)];
  if (matches.length > 1) fail('EXPORT_DUPLICATE_NONCE_FIELD');
  let nonceText = null;
  let jsonText = text;
  if (matches.length === 1) {
    nonceText = matches[0][1];
    const start = matches[0].index + '"nonce":'.length;
    jsonText = `${text.slice(0, start)}"${nonceText}"${text.slice(start + nonceText.length)}`;
  }
  let row;
  try { row = JSON.parse(jsonText); } catch { return null; }
  if (!row || typeof row !== 'object' || Array.isArray(row)) return null;
  if (nonceText !== null) row.nonce = nonceText;
  return row;
}

function normalizeSignedRecord(room, row) {
  if (!Number.isSafeInteger(row.seq) || row.seq <= 0 || row.seq > MAX_SAFE) fail('EXPORT_SEQ_INVALID');
  if (typeof row.from !== 'string' || !DID_RE.test(row.from)) fail('EXPORT_DID_INVALID');
  if (typeof row.text !== 'string' || row.text.length > 4096) fail('EXPORT_TEXT_INVALID');
  if (typeof row.nonce !== 'string' || !DECIMAL_NONCE_RE.test(row.nonce)) fail('EXPORT_NONCE_INVALID');
  if (typeof row.sig !== 'string') fail('EXPORT_SIGNATURE_INVALID');
  const record = {
    room,
    seq: row.seq,
    timestampMs: timestampMs(row.ts),
    sender: row.from,
    nonce: row.nonce,
    signature: row.sig,
    line: row.text,
  };
  if (!verifyDidSignature(record.sender, canonicalMessage(room, record.nonce, record.line), record.signature)) fail('EXPORT_SIGNATURE_INVALID');
  return record;
}

function linesFromRaw(raw) {
  if (raw.length === 0) return [];
  if (raw[raw.length - 1] !== 0x0a) fail('EXPORT_INCOMPLETE');
  return raw.subarray(0, raw.length - 1).toString('binary').split('\n').map(line => Buffer.from(line, 'binary'));
}

function reverseTailLines(raw) {
  const start = Math.max(0, raw.length - READ_BUDGET);
  let parts = raw.subarray(start).toString('binary').split('\n');
  if (start) parts = parts.slice(1);
  return parts.filter(Boolean).reverse().map(line => Buffer.from(line, 'binary'));
}

export function normalizeExportSnapshot({ room, generation, body, capturedAtMs, expectedDid }) {
  if (typeof room !== 'string' || !ROOM_RE.test(room)) fail('EXPORT_ROOM_INVALID');
  if (!Number.isSafeInteger(generation) || generation < 0) fail('EXPORT_GENERATION_INVALID');
  if (!Number.isSafeInteger(capturedAtMs) || capturedAtMs < 0) fail('EXPORT_CAPTURE_TIME_INVALID');
  if (typeof expectedDid !== 'string' || !DID_RE.test(expectedDid)) fail('EXPORT_EXPECTED_DID_INVALID');
  const raw = Buffer.isBuffer(body) ? Buffer.from(body) : Buffer.from(body ?? []);
  if (raw.length > MAX_EXPORT) fail('EXPORT_TOO_LARGE');
  const rawLines = linesFromRaw(raw);
  const records = [];
  let lastSeq = 0;
  for (const rawLine of rawLines) {
    const row = parseLosslessRecord(rawLine);
    if (row === null) fail('EXPORT_RECORD_INVALID');
    if (!Number.isSafeInteger(row.seq) || row.seq <= lastSeq) fail('EXPORT_ORDER_INVALID');
    lastSeq = row.seq;
    if (row.sig === undefined) continue;
    records.push(normalizeSignedRecord(room, row));
  }

  let nonceRecord = null;
  for (const rawLine of reverseTailLines(raw)) {
    if (!rawLine.includes(Buffer.from(expectedDid, 'ascii'))) continue;
    const row = parseLosslessRecord(rawLine);
    if (row === null || row.from !== expectedDid || row.nonce === undefined) continue;
    nonceRecord = normalizeSignedRecord(room, row);
    break;
  }

  return {
    version: 1,
    kind: 'TECHNOCORE_EXPORT_SNAPSHOT',
    room,
    generation,
    capturedAtMs,
    rawBytes: raw.length,
    rawSha256: sha256(raw),
    records,
    nonceObservation: {
      did: expectedDid,
      observedNonce: nonceRecord?.nonce ?? null,
      observedNone: nonceRecord === null,
      tailStart: Math.max(0, raw.length - READ_BUDGET),
      tailBytes: Math.min(raw.length, READ_BUDGET),
      lineCount: reverseTailLines(raw).length,
    },
  };
}

export function validateSnapshot(snapshot, { room, expectedDid, maxAgeMs, nowMs }) {
  if (!snapshot || snapshot.version !== 1 || snapshot.kind !== 'TECHNOCORE_EXPORT_SNAPSHOT') fail('SNAPSHOT_INVALID');
  if (snapshot.room !== room || snapshot.nonceObservation?.did !== expectedDid) fail('SNAPSHOT_BINDING');
  if (!Number.isSafeInteger(nowMs) || !Number.isSafeInteger(maxAgeMs) || maxAgeMs <= 0
      || snapshot.capturedAtMs > nowMs + 1000 || nowMs - snapshot.capturedAtMs > maxAgeMs) fail('SNAPSHOT_STALE');
  return snapshot;
}
