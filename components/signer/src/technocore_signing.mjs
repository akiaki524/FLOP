import {
  createPrivateKey,
  createPublicKey,
  sign as cryptoSign,
  verify as cryptoVerify,
} from 'node:crypto';

const PKCS8_ED25519_SEED_PREFIX = Buffer.from('302e020100300506032b657004220420', 'hex');
const SPKI_ED25519_PUBLIC_PREFIX = Buffer.from('302a300506032b6570032100', 'hex');
const MULTICODEC_ED25519 = Buffer.from([0xed, 0x01]);
const B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
const DID_RE = /^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$/;
const CANONICAL_SIG_RE = /^[A-Za-z0-9_-]{85}[AQgw]$/;
const INVISIBLE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Zl}\p{Zp}]/gu;

function requireSeed(seed) {
  const value = Buffer.from(seed ?? []);
  if (value.length !== 32) throw new Error('SIGNING_SEED_LENGTH');
  return value;
}

function privateKeyFromSeed(seed) {
  const raw = requireSeed(seed);
  const der = Buffer.concat([PKCS8_ED25519_SEED_PREFIX, raw]);
  try {
    return createPrivateKey({
      key: der,
      format: 'der',
      type: 'pkcs8',
    });
  } finally {
    raw.fill(0);
    der.fill(0);
  }
}

export function rawPublicKeyFromSeed(seed) {
  const der = createPublicKey(privateKeyFromSeed(seed)).export({ format: 'der', type: 'spki' });
  if (!Buffer.from(der).subarray(0, SPKI_ED25519_PUBLIC_PREFIX.length).equals(SPKI_ED25519_PUBLIC_PREFIX)) {
    throw new Error('UNEXPECTED_ED25519_SPKI');
  }
  const raw = Buffer.from(der).subarray(SPKI_ED25519_PUBLIC_PREFIX.length);
  if (raw.length !== 32) throw new Error('UNEXPECTED_ED25519_PUBLIC_KEY');
  return Buffer.from(raw);
}

export function base58btcEncode(bytes) {
  const raw = Buffer.from(bytes);
  let zeros = 0;
  while (zeros < raw.length && raw[zeros] === 0) zeros += 1;
  let n = raw.length ? BigInt(`0x${raw.toString('hex') || '0'}`) : 0n;
  let out = '';
  while (n > 0n) {
    out = B58[Number(n % 58n)] + out;
    n /= 58n;
  }
  return '1'.repeat(zeros) + out;
}

export function base58btcDecode(value) {
  if (typeof value !== 'string' || value.length === 0) throw new Error('INVALID_BASE58BTC');
  let n = 0n;
  for (const ch of value) {
    const index = B58.indexOf(ch);
    if (index < 0) throw new Error('INVALID_BASE58BTC');
    n = n * 58n + BigInt(index);
  }
  let hex = n === 0n ? '' : n.toString(16);
  if (hex.length % 2) hex = `0${hex}`;
  const body = hex ? Buffer.from(hex, 'hex') : Buffer.alloc(0);
  let leading = 0;
  while (leading < value.length && value[leading] === '1') leading += 1;
  return Buffer.concat([Buffer.alloc(leading), body]);
}

export function didFromPublicKey(publicKey) {
  const raw = Buffer.from(publicKey ?? []);
  if (raw.length !== 32) throw new Error('ED25519_PUBLIC_KEY_LENGTH');
  return `did:key:z${base58btcEncode(Buffer.concat([MULTICODEC_ED25519, raw]))}`;
}

export function publicKeyFromDid(did) {
  if (typeof did !== 'string' || !DID_RE.test(did)) throw new Error('INVALID_DID');
  const decoded = base58btcDecode(did.slice('did:key:z'.length));
  if (decoded.length !== 34 || decoded[0] !== 0xed || decoded[1] !== 0x01) throw new Error('INVALID_DID');
  return Buffer.from(decoded.subarray(2));
}

export function sweep(text) {
  if (typeof text !== 'string') throw new Error('TEXT_REQUIRED');
  return text.replace(INVISIBLE, ' ').trim();
}

export function canonicalMessage(room, nonce, sweptText) {
  if (typeof room !== 'string' || room.length === 0 || room.includes('|')) throw new Error('INVALID_ROOM');
  const nonceText = String(nonce);
  if (!/^(0|[1-9][0-9]{0,18})$/.test(nonceText)) throw new Error('LOSSLESS_NONCE_REQUIRED');
  if (typeof sweptText !== 'string' || sweptText.includes('\n') || sweptText.includes('\r')) throw new Error('INVALID_LINE');
  return `${room}|${nonceText}|${sweptText}`;
}

export function signerFromSeed(seed) {
  const raw = requireSeed(seed);
  try {
    const privateKey = privateKeyFromSeed(raw);
    const did = didFromPublicKey(rawPublicKeyFromSeed(raw));
    return Object.freeze({
      did,
      sign(canonical) {
        if (typeof canonical !== 'string') throw new Error('CANONICAL_MESSAGE_REQUIRED');
        const signature = cryptoSign(null, Buffer.from(canonical, 'utf8'), privateKey).toString('base64url');
        if (!CANONICAL_SIG_RE.test(signature)) throw new Error('NON_CANONICAL_SIGNATURE');
        return signature;
      },
    });
  } finally {
    raw.fill(0);
  }
}

export function verifyDidSignature(did, canonical, signature) {
  try {
    if (typeof canonical !== 'string' || typeof signature !== 'string' || !CANONICAL_SIG_RE.test(signature)) return false;
    const publicKey = publicKeyFromDid(did);
    const spki = createPublicKey({
      key: Buffer.concat([SPKI_ED25519_PUBLIC_PREFIX, publicKey]),
      format: 'der',
      type: 'spki',
    });
    return cryptoVerify(null, Buffer.from(canonical, 'utf8'), spki, Buffer.from(signature, 'base64url'));
  } catch {
    return false;
  }
}

export const SIGNING_PATTERNS = Object.freeze({ DID_RE, CANONICAL_SIG_RE });
