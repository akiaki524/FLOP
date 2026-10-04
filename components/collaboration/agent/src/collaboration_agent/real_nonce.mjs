// Trusted Human read-side coverage packet; signature checked inside the offline signer.
import { official, fields, requireThat as need, verifyRecord } from './pilot_protocol.mjs';
import { PROJECT_DID } from './connection_approval.mjs';
export const NONCE_SOURCE = 'https://technocore.chat/r/tclk-offers/export';
export const NONCE_PROFILE = 'technocore-e4c4f73f3b28612d7161170b11e08e580b02123a';
const READ_BUDGET = 1 << 20, MAX_EXPORT = 10 << 20;
export async function validateNoncePacket(packet, { expectedDid = PROJECT_DID, nowMs = Date.now() } = {}) {
  fields(packet, ['version', 'kind', 'did', 'room', 'observedNonce', 'observedNone', 'verifiedAtMs', 'records', 'source', 'coverage']);
  need(packet.version === 2 && packet.kind === 'PROJECT_DID_PUBLIC_NONCE_OBSERVATION'
    && packet.did === expectedDid && packet.room === 'tclk-offers', 'NONCE_IDENTITY_MISMATCH');
  const s = packet.source, c = packet.coverage;
  fields(s, ['url', 'method', 'httpStatus', 'generation', 'rawBytes', 'rawSha256', 'capturedAt']);
  fields(c, ['complete', 'basis', 'tailStart', 'tailBytes', 'lineCount']);
  // Generation 1 is the existing activation/custody contract; do not reset it.
  need(s.url === NONCE_SOURCE && s.method === 'GET' && s.httpStatus === 200 && s.generation === 1
    && Number.isSafeInteger(s.rawBytes) && s.rawBytes >= 0 && s.rawBytes < MAX_EXPORT
    && typeof s.rawSha256 === 'string' && /^[0-9a-f]{64}$/.test(s.rawSha256), 'NONCE_SOURCE_INVALID');
  need(Number.isSafeInteger(packet.verifiedAtMs) && s.capturedAt === packet.verifiedAtMs
    && packet.verifiedAtMs >= 0 && packet.verifiedAtMs <= nowMs
    && nowMs - packet.verifiedAtMs <= 30000, 'STALE_OBSERVATION');
  need(Array.isArray(packet.records) && packet.records.length <= 1
    && c.complete === true && c.basis === NONCE_PROFILE
    && c.tailStart === Math.max(0, s.rawBytes - READ_BUDGET)
    && c.tailBytes === Math.min(s.rawBytes, READ_BUDGET)
    && Number.isSafeInteger(c.lineCount) && c.lineCount >= packet.records.length
    && c.lineCount <= Math.floor(c.tailBytes / 2), 'NONCE_COVERAGE_INSUFFICIENT');
  let observedNonce = null;
  if (packet.records.length) {
    const r = packet.records[0];
    fields(r, ['room', 'seq', 'timestampMs', 'sender', 'nonce', 'signature', 'line']);
    need(r.room === packet.room && Number.isSafeInteger(r.seq) && r.seq > 0
      && Number.isSafeInteger(r.timestampMs) && r.timestampMs >= 0
      && r.timestampMs <= packet.verifiedAtMs + 1000 && r.sender === expectedDid
      && typeof r.line === 'string' && r.line.length <= 4096, 'NONCE_RECORD_INVALID');
    need(typeof r.nonce === 'string' && /^(0|[1-9][0-9]{0,18})$/.test(r.nonce)
      && typeof r.signature === 'string', 'LOSSLESS_NONCE_REQUIRED');
    const { t } = await official();
    verifyRecord(t, r);
    // Python selects the newest integer nonce using the server byte window;
    // the exact decimal remains lossless through this validation and custody.
    observedNonce = BigInt(r.nonce).toString();
  }
  need(packet.observedNonce === observedNonce && packet.observedNone === (observedNonce === null),
    'NONCE_OBSERVATION_MISMATCH');
  return { room: packet.room, observedNonce, observedNone: observedNonce === null, verifiedAtMs: packet.verifiedAtMs };
}
