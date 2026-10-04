import { randomUUID } from 'node:crypto';
import {
  closeSync,
  fsyncSync,
  openSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { dirname } from 'node:path';

import {
  assertSafeAcquisitionRoot,
  exportSnapshotPath,
  MAX_EXPORT_BYTES,
  MAX_NOTE_BYTES,
  paperNotePath,
} from './runtime_acquisition.mjs';
import { dealRoom, OFFER_ROOM, paperNote } from './tclk_v1.mjs';

export const TECHNOCORE_BASE = 'https://technocore.chat';
export const CLOSE_CALL_REGISTRATION_ROOM = 'close1';
export const CLOSE_CALL_PRICE_ROOM = 'd-close1-price';

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}
function need(condition, code) {
  if (!condition) fail(code);
}
function parseLength(response, maxBytes) {
  const raw = response.headers.get('content-length');
  if (raw === null) return;
  need(/^(0|[1-9][0-9]{0,8})$/.test(raw), 'READER_CONTENT_LENGTH');
  need(Number(raw) <= maxBytes, 'READER_BODY_TOO_LARGE');
}
async function boundedBody(response, maxBytes) {
  parseLength(response, maxBytes);
  need(response.body !== null, 'READER_BODY_FAILED');
  const chunks = [];
  let size = 0;
  try {
    for await (const value of response.body) {
      const chunk = Buffer.from(value);
      size += chunk.length;
      need(size <= maxBytes, 'READER_BODY_TOO_LARGE');
      chunks.push(chunk);
    }
    return Buffer.concat(chunks, size);
  } catch (error) {
    if (typeof error?.code === 'string' && error.code.startsWith('READER_')) throw error;
    fail('READER_BODY_FAILED');
  }
}
function atomicWrite(path, bytes) {
  const temp = path + '.tmp-' + process.pid + '-' + randomUUID();
  let fd;
  let dfd;
  let created = false;
  try {
    fd = openSync(temp, 'wx', 0o640);
    created = true;
    writeFileSync(fd, bytes);
    fsyncSync(fd);
    closeSync(fd);
    fd = undefined;
    renameSync(temp, path);
    created = false;
    dfd = openSync(dirname(path), 'r');
    fsyncSync(dfd);
    closeSync(dfd);
    dfd = undefined;
  } catch {
    if (fd !== undefined) try { closeSync(fd); } catch {}
    if (dfd !== undefined) try { closeSync(dfd); } catch {}
    if (created) try { unlinkSync(temp); } catch {}
    fail('READER_STORE_FAILED');
  }
}
function exportGeneration(response) {
  const value = response.headers.get('x-room-generation');
  need(typeof value === 'string' && /^(0|[1-9][0-9]{0,15})$/.test(value),
    'READER_GENERATION_INVALID');
  const generation = Number(value);
  need(Number.isSafeInteger(generation) && generation >= 0, 'READER_GENERATION_INVALID');
  return generation;
}
function contentType(response, expected) {
  const value = response.headers.get('content-type') ?? '';
  need(value.split(';', 1)[0].trim().toLowerCase() === expected, 'READER_CONTENT_TYPE');
}
function contentEncoding(response) {
  const value = (response.headers.get('content-encoding') ?? 'identity').trim().toLowerCase();
  need(value === '' || value === 'identity', 'READER_CONTENT_ENCODING');
}
function fetchOptions() {
  return {
    method: 'GET',
    redirect: 'manual',
    signal: AbortSignal.timeout(4000),
    headers: {
      accept: '*/*',
      'accept-encoding': 'identity',
    },
  };
}
function exactResponse(response) {
  need(response && response.status === 200 && !response.headers.get('location'),
    'READER_HTTP_FAILURE');
  contentEncoding(response);
}

export class TechnocoreTrustedReader {
  #root;
  #fetch;
  #clock;
  #lastCapturedAtMs = -1;
  constructor({ root, fetchImpl = globalThis.fetch, clock = Date.now }) {
    need(typeof fetchImpl === 'function' && typeof clock === 'function', 'READER_CONFIG_INVALID');
    assertSafeAcquisitionRoot(root);
    this.#root = root;
    this.#fetch = fetchImpl;
    this.#clock = clock;
  }

  async #fetchExport(room) {
    assertSafeAcquisitionRoot(this.#root);
    const capturedAtMs = this.#clock();
    need(Number.isSafeInteger(capturedAtMs) && capturedAtMs >= 0, 'READER_CLOCK_INVALID');
    need(capturedAtMs >= this.#lastCapturedAtMs, 'READER_CLOCK_ROLLBACK');
    this.#lastCapturedAtMs = capturedAtMs;
    let response;
    try {
      response = await this.#fetch(
        TECHNOCORE_BASE + '/r/' + encodeURIComponent(room) + '/export',
        fetchOptions(),
      );
    } catch {
      fail('READER_HTTP_FAILURE');
    }
    exactResponse(response);
    contentType(response, 'application/x-ndjson');
    const generation = exportGeneration(response);
    const body = await boundedBody(response, MAX_EXPORT_BYTES);
    const payload = Buffer.from(JSON.stringify({
      room,
      generation,
      capturedAtMs,
      bodyBase64url: body.toString('base64url'),
    }) + '\n', 'utf8');
    atomicWrite(exportSnapshotPath(this.#root, room), payload);
    return { room, generation, capturedAtMs, rawBytes: body.length };
  }

  refreshOffers() {
    return this.#fetchExport(OFFER_ROOM);
  }

  refreshCloseCallRegistration() {
    return this.#fetchExport(CLOSE_CALL_REGISTRATION_ROOM);
  }

  refreshCloseCallPrice() {
    return this.#fetchExport(CLOSE_CALL_PRICE_ROOM);
  }

  refreshDeal(contract) {
    return this.#fetchExport(dealRoom(contract));
  }

  async refreshPaper(contract) {
    assertSafeAcquisitionRoot(this.#root);
    const capturedAtMs = this.#clock();
    need(Number.isSafeInteger(capturedAtMs) && capturedAtMs >= 0, 'READER_CLOCK_INVALID');
    need(capturedAtMs >= this.#lastCapturedAtMs, 'READER_CLOCK_ROLLBACK');
    this.#lastCapturedAtMs = capturedAtMs;
    const { ns, key } = paperNote(contract);
    let response;
    try {
      response = await this.#fetch(
        TECHNOCORE_BASE + '/kv/' + encodeURIComponent(ns) + '/' + encodeURIComponent(key),
        fetchOptions(),
      );
    } catch {
      fail('READER_HTTP_FAILURE');
    }
    exactResponse(response);
    contentType(response, 'text/plain');
    const body = await boundedBody(response, MAX_NOTE_BYTES);
    let text;
    try { text = new TextDecoder('utf-8', { fatal: true }).decode(body); }
    catch { fail('READER_NOTE_INVALID'); }
    need(Buffer.from(text, 'utf8').equals(body), 'READER_NOTE_INVALID');
    // Technocore note reads intentionally prepend an untrusted-content banner.
    // PaperRail values are single-line after the venue sweep, so exactly one
    // non-banner, non-blank line must remain.
    const lines = text.split('\n').filter(line => line.trim() !== '');
    const values = lines.filter(line => !line.startsWith('!!') && !line.startsWith('# budget:'));
    need(values.length === 1
      && lines.every(line => line === values[0] || line.startsWith('!!') || line.startsWith('# budget:')),
      'READER_NOTE_INVALID');
    const value = values[0].trimEnd();
    need(value.startsWith('tclkpaper1 '), 'READER_NOTE_INVALID');
    need(value.length > 0, 'READER_NOTE_INVALID');
    atomicWrite(paperNotePath(this.#root, contract), Buffer.from(value, 'utf8'));
    return { contract, bytes: Buffer.byteLength(value, 'utf8') };
  }
}

export function createReaderRpcHandler(reader) {
  need(reader && typeof reader.refreshOffers === 'function'
    && typeof reader.refreshCloseCallRegistration === 'function'
    && typeof reader.refreshCloseCallPrice === 'function'
    && typeof reader.refreshDeal === 'function'
    && typeof reader.refreshPaper === 'function', 'READER_REQUIRED');

  let tail = Promise.resolve();
  const dispatch = async request => {
    need(request && typeof request === 'object' && !Array.isArray(request)
      && Object.keys(request).sort().join(',') === 'method,value', 'READER_REQUEST_INVALID');
    need(request.value && typeof request.value === 'object' && !Array.isArray(request.value),
      'READER_REQUEST_INVALID');
    if (request.method === 'refreshOffers') {
      need(Object.keys(request.value).length === 0, 'READER_REQUEST_INVALID');
      return reader.refreshOffers();
    }
    if (request.method === 'refreshCloseCallRegistration') {
      need(Object.keys(request.value).length === 0, 'READER_REQUEST_INVALID');
      return reader.refreshCloseCallRegistration();
    }
    if (request.method === 'refreshCloseCallPrice') {
      need(Object.keys(request.value).length === 0, 'READER_REQUEST_INVALID');
      return reader.refreshCloseCallPrice();
    }
    if (request.method === 'refreshDeal') {
      need(Object.keys(request.value).sort().join(',') === 'contract'
        && typeof request.value.contract === 'string', 'READER_REQUEST_INVALID');
      return reader.refreshDeal(request.value.contract);
    }
    if (request.method === 'refreshPaper') {
      need(Object.keys(request.value).sort().join(',') === 'contract'
        && typeof request.value.contract === 'string', 'READER_REQUEST_INVALID');
      return reader.refreshPaper(request.value.contract);
    }
    fail('READER_METHOD_DENIED');
  };

  return request => {
    const current = tail.then(() => dispatch(request));
    // Keep the queue usable after a denied request while preserving the current
    // caller's rejection. This also prevents older network reads from finishing
    // after newer reads and rolling a snapshot backward.
    tail = current.catch(() => {});
    return current;
  };
}
