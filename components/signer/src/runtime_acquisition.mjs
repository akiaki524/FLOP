import { createHash } from 'node:crypto';
import { lstatSync, readFileSync } from 'node:fs';
import { isAbsolute, join } from 'node:path';

export const MAX_EXPORT_BYTES = 10 << 20;
export const MAX_EXPORT_FILE_BYTES = 15 << 20;
export const MAX_NOTE_BYTES = 4096;

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}
function need(condition, code) {
  if (!condition) fail(code);
}
function exactKeys(value, names) {
  need(value && typeof value === 'object' && !Array.isArray(value), 'ACQUISITION_INVALID');
  need(Object.keys(value).sort().join(',') === [...names].sort().join(','), 'ACQUISITION_INVALID');
}
function digestName(kind, value) {
  need(typeof value === 'string' && value.length > 0 && value.length <= 512, 'ACQUISITION_KEY_INVALID');
  return kind + '-' + createHash('sha256').update(value, 'utf8').digest('hex');
}
export function assertSafeAcquisitionRoot(path) {
  let stat;
  try { stat = lstatSync(path); } catch { fail('ACQUISITION_ROOT_UNAVAILABLE'); }
  need(stat.isDirectory() && !stat.isSymbolicLink() && (stat.mode & 0o022) === 0,
    'ACQUISITION_ROOT_UNSAFE');
}
function safeRead(path, maxBytes) {
  let stat;
  try { stat = lstatSync(path); } catch { fail('ACQUISITION_UNAVAILABLE'); }
  need(stat.isFile() && !stat.isSymbolicLink() && (stat.mode & 0o022) === 0
    && stat.size >= 0 && stat.size <= maxBytes, 'ACQUISITION_FILE_UNSAFE');
  const bytes = readFileSync(path);
  need(bytes.length === stat.size && bytes.length <= maxBytes, 'ACQUISITION_FILE_UNSAFE');
  return bytes;
}

export function exportSnapshotPath(root, room) {
  return join(root, digestName('export', room) + '.json');
}
export function paperNotePath(root, contract) {
  return join(root, digestName('paper', contract) + '.txt');
}

export class TrustedSnapshotAcquisition {
  #root;
  constructor(root) {
    need(typeof root === 'string' && isAbsolute(root), 'ACQUISITION_ROOT_REQUIRED');
    assertSafeAcquisitionRoot(root);
    this.#root = root;
  }

  export(room) {
    assertSafeAcquisitionRoot(this.#root);
    const bytes = safeRead(exportSnapshotPath(this.#root, room), MAX_EXPORT_FILE_BYTES);
    let parsed;
    try { parsed = JSON.parse(bytes.toString('utf8')); } catch { fail('ACQUISITION_INVALID'); }
    exactKeys(parsed, ['room', 'generation', 'capturedAtMs', 'bodyBase64url']);
    need(parsed.room === room && Number.isSafeInteger(parsed.generation) && parsed.generation >= 0
      && Number.isSafeInteger(parsed.capturedAtMs) && parsed.capturedAtMs >= 0
      && typeof parsed.bodyBase64url === 'string', 'ACQUISITION_INVALID');
    let body;
    try { body = Buffer.from(parsed.bodyBase64url, 'base64url'); } catch { fail('ACQUISITION_INVALID'); }
    need(body.length <= MAX_EXPORT_BYTES && body.toString('base64url') === parsed.bodyBase64url,
      'ACQUISITION_INVALID');
    return { room, generation: parsed.generation, body, capturedAtMs: parsed.capturedAtMs };
  }

  paperNote(contract) {
    assertSafeAcquisitionRoot(this.#root);
    const bytes = safeRead(paperNotePath(this.#root, contract), MAX_NOTE_BYTES);
    let text;
    try { text = new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
    catch { fail('ACQUISITION_INVALID'); }
    need(Buffer.from(text, 'utf8').equals(bytes), 'ACQUISITION_INVALID');
    return text;
  }
}
