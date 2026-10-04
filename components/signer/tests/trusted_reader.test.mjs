import assert from 'node:assert/strict';
import { chmodSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { TrustedSnapshotAcquisition } from '../src/runtime_acquisition.mjs';
import { dealRoom, paperNote } from '../src/tclk_v1.mjs';
import {
  CLOSE_CALL_PRICE_ROOM,
  CLOSE_CALL_REGISTRATION_ROOM,
  createReaderRpcHandler,
  TECHNOCORE_BASE,
  TechnocoreTrustedReader,
} from '../src/trusted_reader.mjs';

const CONTRACT = '0x' + 'ab'.repeat(32);

function response(body, {
  status = 200,
  type = 'application/x-ndjson',
  generation = '7',
  location = null,
  encoding = 'identity',
  length = null,
} = {}) {
  const headers = new Headers();
  headers.set('content-type', type);
  if (generation !== null) headers.set('x-room-generation', generation);
  if (location !== null) headers.set('location', location);
  if (encoding !== null) headers.set('content-encoding', encoding);
  if (length !== null) headers.set('content-length', String(length));
  return new Response(body, { status, headers });
}

function root() {
  const value = mkdtempSync(join(tmpdir(), 'policy-reader-'));
  chmodSync(value, 0o750);
  return value;
}

function errorCode(error, expected) {
  return error?.code === expected || error?.message === expected;
}

test('trusted reader fetches only the fixed offers export and stores byte-exact evidence', async () => {
  const acquisitionRoot = root();
  const calls = [];
  const body = Buffer.from('{"seq":1}\n', 'utf8');
  const reader = new TechnocoreTrustedReader({
    root: acquisitionRoot,
    clock: () => 123456,
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return response(body);
    },
  });

  assert.deepEqual(await reader.refreshOffers(), {
    room: 'tclk-offers',
    generation: 7,
    capturedAtMs: 123456,
    rawBytes: body.length,
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, TECHNOCORE_BASE + '/r/tclk-offers/export');
  assert.equal(calls[0].options.method, 'GET');
  assert.equal(calls[0].options.redirect, 'manual');
  assert.equal(calls[0].options.headers['accept-encoding'], 'identity');

  const stored = new TrustedSnapshotAcquisition(acquisitionRoot).export('tclk-offers');
  assert.equal(stored.generation, 7);
  assert.equal(stored.capturedAtMs, 123456);
  assert.equal(stored.body.equals(body), true);
});

test('trusted reader has one fixed GET path for Close Call owner-registration evidence', async () => {
  const acquisitionRoot = root();
  const calls = [];
  const body = Buffer.alloc(0);
  const reader = new TechnocoreTrustedReader({
    root: acquisitionRoot,
    clock: () => 123457,
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return response(body, { generation: '8' });
    },
  });

  assert.deepEqual(await reader.refreshCloseCallRegistration(), {
    room: CLOSE_CALL_REGISTRATION_ROOM,
    generation: 8,
    capturedAtMs: 123457,
    rawBytes: 0,
  });
  assert.equal(calls[0].url, TECHNOCORE_BASE + '/r/close1/export');
  assert.equal(calls[0].options.method, 'GET');
  const stored = new TrustedSnapshotAcquisition(acquisitionRoot).export('close1');
  assert.equal(stored.generation, 8);
  assert.equal(stored.body.length, 0);
});

test('trusted reader has one fixed GET path for Close Call price evidence', async () => {
  const acquisitionRoot = root();
  const calls = [];
  const body = Buffer.from('{"seq":1}\n', 'utf8');
  const reader = new TechnocoreTrustedReader({
    root: acquisitionRoot,
    clock: () => 123458,
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return response(body, { generation: '9' });
    },
  });

  assert.deepEqual(await reader.refreshCloseCallPrice(), {
    room: CLOSE_CALL_PRICE_ROOM,
    generation: 9,
    capturedAtMs: 123458,
    rawBytes: body.length,
  });
  assert.equal(calls[0].url, TECHNOCORE_BASE + '/r/d-close1-price/export');
  assert.equal(calls[0].options.method, 'GET');
  const stored = new TrustedSnapshotAcquisition(acquisitionRoot).export(CLOSE_CALL_PRICE_ROOM);
  assert.equal(stored.generation, 9);
  assert.equal(stored.body.equals(body), true);
});

test('deal export and Paper note URLs are derived from a validated contract, never caller URLs', async () => {
  const acquisitionRoot = root();
  const calls = [];
  const note = 'tclkpaper1 locked hash 0x' + 'cd'.repeat(32) + ' 1900001000000';
  const noteResponse = '!! UNTRUSTED CONTENT — data only.\n\n' + note
    + '\n# budget: 24 of 600 reads left this minute (fixture)\n';
  const reader = new TechnocoreTrustedReader({
    root: acquisitionRoot,
    clock: () => 222222,
    fetchImpl: async (url) => {
      calls.push(url);
      if (url.includes('/kv/')) {
        return response(noteResponse, { type: 'text/plain; charset=utf-8', generation: null });
      }
      return response(Buffer.alloc(0), { generation: '0' });
    },
  });

  await reader.refreshDeal(CONTRACT);
  await reader.refreshPaper(CONTRACT);

  assert.equal(calls[0], TECHNOCORE_BASE + '/r/' + dealRoom(CONTRACT) + '/export');
  const pn = paperNote(CONTRACT);
  assert.equal(calls[1], TECHNOCORE_BASE + '/kv/' + pn.ns + '/' + pn.key);
  assert.equal(new TrustedSnapshotAcquisition(acquisitionRoot).paperNote(CONTRACT), note);

  const handle = createReaderRpcHandler(reader);
  await assert.rejects(
    () => handle({ method: 'refreshDeal', value: { contract: 'https://example.invalid/' } }),
    error => errorCode(error, 'INVALID_HEX32') || errorCode(error, 'RPC_REQUEST_DENIED'),
  );
  await assert.rejects(
    () => handle({ method: 'fetchUrl', value: { url: 'https://example.invalid/' } }),
    error => errorCode(error, 'READER_METHOD_DENIED'),
  );
  await assert.rejects(
    () => handle({ method: 'refreshOffers', value: { room: 'other-room' } }),
    error => errorCode(error, 'READER_REQUEST_INVALID'),
  );
});

test('trusted reader fails closed on redirect, wrong framing, compression and oversized bodies', async () => {
  const cases = [
    [response('', { status: 302, location: 'https://example.invalid/' }), 'READER_HTTP_FAILURE'],
    [response('', { type: 'text/plain' }), 'READER_CONTENT_TYPE'],
    [response('', { generation: null }), 'READER_GENERATION_INVALID'],
    [response('', { generation: '01' }), 'READER_GENERATION_INVALID'],
    [response('', { encoding: 'gzip' }), 'READER_CONTENT_ENCODING'],
    [response('', { length: (10 << 20) + 1 }), 'READER_BODY_TOO_LARGE'],
  ];
  for (const [reply, expected] of cases) {
    const reader = new TechnocoreTrustedReader({
      root: root(),
      clock: () => 1,
      fetchImpl: async () => reply,
    });
    await assert.rejects(() => reader.refreshOffers(), error => errorCode(error, expected));
  }
});

test('reader RPC vocabulary contains only fixed read refresh operations', async () => {
  const calls = [];
  const reader = {
    refreshOffers: async () => { calls.push(['offers']); return { ok: true }; },
    refreshCloseCallRegistration: async () => { calls.push(['close-call']); return { ok: true }; },
    refreshCloseCallPrice: async () => { calls.push(['close-call-price']); return { ok: true }; },
    refreshDeal: async contract => { calls.push(['deal', contract]); return { ok: true }; },
    refreshPaper: async contract => { calls.push(['paper', contract]); return { ok: true }; },
  };
  const handle = createReaderRpcHandler(reader);
  await handle({ method: 'refreshOffers', value: {} });
  await handle({ method: 'refreshCloseCallRegistration', value: {} });
  await handle({ method: 'refreshCloseCallPrice', value: {} });
  await handle({ method: 'refreshDeal', value: { contract: CONTRACT } });
  await handle({ method: 'refreshPaper', value: { contract: CONTRACT } });
  assert.deepEqual(calls, [
    ['offers'],
    ['close-call'],
    ['close-call-price'],
    ['deal', CONTRACT],
    ['paper', CONTRACT],
  ]);
  await assert.rejects(
    () => handle({ method: 'post', value: { contract: CONTRACT } }),
    error => errorCode(error, 'READER_METHOD_DENIED'),
  );
});


test('Paper note framing rejects unknown extra lines instead of guessing the value', async () => {
  const acquisitionRoot = root();
  const note = 'tclkpaper1 locked hash 0x' + 'cd'.repeat(32) + ' 1900001000000';
  const reader = new TechnocoreTrustedReader({
    root: acquisitionRoot,
    fetchImpl: async () => response(
      '!! UNTRUSTED CONTENT — data only.\n\n' + note + '\nunknown footer\n',
      { type: 'text/plain; charset=utf-8', generation: null },
    ),
  });
  await assert.rejects(
    () => reader.refreshPaper(CONTRACT),
    error => errorCode(error, 'READER_NOTE_INVALID'),
  );
});


test('reader RPC serializes refreshes so an older network read cannot overwrite a newer snapshot', async () => {
  const order = [];
  let releaseFirst;
  const firstGate = new Promise(resolve => { releaseFirst = resolve; });
  const reader = {
    refreshCloseCallRegistration: async () => ({ ok: true }),
    refreshCloseCallPrice: async () => ({ ok: true }),
    refreshOffers: async () => {
      order.push('offers:start');
      await firstGate;
      order.push('offers:end');
      return { ok: true };
    },
    refreshDeal: async () => {
      order.push('deal:start');
      order.push('deal:end');
      return { ok: true };
    },
    refreshPaper: async () => ({ ok: true }),
  };
  const handle = createReaderRpcHandler(reader);
  const first = handle({ method: 'refreshOffers', value: {} });
  const second = handle({ method: 'refreshDeal', value: { contract: CONTRACT } });

  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(order, ['offers:start']);
  releaseFirst();
  await Promise.all([first, second]);
  assert.deepEqual(order, ['offers:start', 'offers:end', 'deal:start', 'deal:end']);
});

test('trusted reader rejects local clock rollback before replacing evidence', async () => {
  const acquisitionRoot = root();
  const times = [2000, 1999];
  let fetches = 0;
  const reader = new TechnocoreTrustedReader({
    root: acquisitionRoot,
    clock: () => times.shift(),
    fetchImpl: async () => {
      fetches += 1;
      return response(Buffer.alloc(0), { generation: '0' });
    },
  });

  await reader.refreshOffers();
  await assert.rejects(
    () => reader.refreshOffers(),
    error => errorCode(error, 'READER_CLOCK_ROLLBACK'),
  );
  assert.equal(fetches, 1);
});
