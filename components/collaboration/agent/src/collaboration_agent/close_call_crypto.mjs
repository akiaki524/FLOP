import crypto from 'node:crypto';

const DID_RE = /^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$/;
const SIG_RE = /^[A-Za-z0-9_-]{86}$/;
const BASE58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';

function base58Decode(text) {
  let value = 0n;
  for (const char of text) {
    const digit = BASE58.indexOf(char);
    if (digit < 0) throw new Error('invalid_base58');
    value = value * 58n + BigInt(digit);
  }
  const bytes = [];
  while (value > 0n) {
    bytes.unshift(Number(value & 0xffn));
    value >>= 8n;
  }
  for (const char of text) {
    if (char !== '1') break;
    bytes.unshift(0);
  }
  return Buffer.from(bytes);
}

function publicKeyFromDid(did) {
  if (!DID_RE.test(did)) throw new Error('invalid_did');
  const raw = base58Decode(did.slice('did:key:z'.length));
  if (raw.length !== 34 || raw[0] !== 0xed || raw[1] !== 0x01) {
    throw new Error('invalid_multicodec');
  }
  return crypto.createPublicKey({
    key: {
      kty: 'OKP',
      crv: 'Ed25519',
      x: raw.subarray(2).toString('base64url'),
    },
    format: 'jwk',
  });
}

function verifyRoom(room, message) {
  if (!message || typeof message !== 'object') return false;
  const did = String(message.from ?? '');
  const nonce = String(message.nonce ?? '');
  const text = String(message.text ?? '');
  const sig = String(message.sig ?? '');
  if (!DID_RE.test(did) || !SIG_RE.test(sig)) return false;
  try {
    return crypto.verify(
      null,
      Buffer.from(`${room}|${nonce}|${text}`, 'utf8'),
      publicKeyFromDid(did),
      Buffer.from(sig, 'base64url'),
    );
  } catch {
    return false;
  }
}

function verifyMakerTerms(maker, canonicalTerms, signature) {
  if (!DID_RE.test(maker) || typeof canonicalTerms !== 'string'
      || canonicalTerms.length > 4096 || !SIG_RE.test(signature)) return false;
  try {
    return crypto.verify(
      null,
      Buffer.from(`close-1|terms|${canonicalTerms}`, 'utf8'),
      publicKeyFromDid(maker),
      Buffer.from(signature, 'base64url'),
    );
  } catch {
    return false;
  }
}

function main(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('invalid_request');
  }
  if (value.op === 'verify-room') {
    return { ok: verifyRoom(String(value.room ?? ''), value.message) };
  }
  if (value.op === 'verify-room-batch') {
    if (!Array.isArray(value.messages) || value.messages.length > 200) {
      throw new Error('invalid_batch');
    }
    return {
      ok: value.messages.map(message =>
        verifyRoom(String(value.room ?? ''), message)),
    };
  }
  if (value.op === 'verify-maker-terms') {
    return {
      ok: verifyMakerTerms(
        String(value.maker ?? ''),
        String(value.canonicalTerms ?? ''),
        String(value.signature ?? ''),
      ),
    };
  }
  throw new Error('unsupported_operation');
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => {
  input += chunk;
  if (Buffer.byteLength(input, 'utf8') > 64 * 1024) process.exit(2);
});
process.stdin.on('end', () => {
  try {
    process.stdout.write(JSON.stringify(main(JSON.parse(input))) + '\n');
  } catch {
    process.stdout.write(JSON.stringify({ ok: false }) + '\n');
    process.exitCode = 2;
  }
});
