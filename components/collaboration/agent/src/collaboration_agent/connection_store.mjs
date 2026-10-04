// Durable, single-contract secret custody and nonce authority for the real-connection boundary.
// This module performs no network access and has no dependency outside Node's standard library.
// The caller supplies the 32-byte custody key in memory; it is never accepted from argv or env.
//
// State and its high-water witness are separate durable files. Rolling back only one is detected.
// An attacker able to roll both files back to the same earlier pair cannot be detected without an
// external monotonic witness (TPM, remote append-only log, etc.); deployment must preserve both.
import {
  existsSync, lstatSync, mkdirSync, readFileSync, renameSync, unlinkSync,
} from 'node:fs';
import { open } from 'node:fs/promises';
import { createCipheriv, createDecipheriv, createHash, randomBytes, randomUUID } from 'node:crypto';
import { dirname, resolve } from 'node:path';

const STATE_FILE = 'connection-state.enc';
const WITNESS_FILE = 'connection-highwater.json';
const POINTER_FILE = 'connection-pointer.json';
const LOCK_FILE = 'connection-store.lock';
const DID = /^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$/;
const CONTRACT = /^0x[0-9a-f]{64}$/;
const ROOM = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const DECIMAL = /^(0|[1-9][0-9]*)$/;
const NONCE = /^(0|[1-9][0-9]{0,18})$/;
const MAX_NONCE = 9_999_999_999_999_999_999n;
const HASH = /^[0-9a-f]{64}$/;

export class ConnectionStoreError extends Error {
  constructor(code) { super(code); this.name = 'ConnectionStoreError'; this.code = code; }
}
const need = (condition, code) => { if (!condition) throw new ConnectionStoreError(code); };
const sha = value => createHash('sha256').update(value).digest('hex');
const json = value => Buffer.from(JSON.stringify(value) + '\n', 'utf8');
const exactKeys = (value, keys) => value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
const isRegular = path => {
  try { const s = lstatSync(path); return s.isFile() && !s.isSymbolicLink(); } catch { return false; }
};

async function fsyncDirectory(path) {
  const handle = await open(path, 'r');
  try { await handle.sync(); } finally { await handle.close(); }
}

async function atomicWrite(path, bytes, fault = undefined, target = 'unknown') {
  const temp = path + '.pending-' + randomUUID();
  let handle;
  try {
    handle = await open(temp, 'wx', 0o600);
    await handle.writeFile(bytes);
    fault?.(target, 'before-file-fsync');
    await handle.sync();
    await handle.close(); handle = undefined;
    if (existsSync(path)) need(isRegular(path), 'STORE_UNSAFE_PATH');
    renameSync(temp, path);
    fault?.(target, 'before-directory-fsync');
    await fsyncDirectory(dirname(path));
    need(readFileSync(path).equals(bytes), 'STORE_READBACK_FAILED');
  } catch (error) {
    if (handle !== undefined) await handle.close();
    try { if (isRegular(temp)) unlinkSync(temp); } catch { /* best-effort cleanup of ciphertext only */ }
    throw error;
  }
}

function binding(did, contract, revision) {
  return Buffer.from(JSON.stringify({ version: 1, did, contract, revision }), 'utf8');
}

function encryptState(value, key) {
  const revision = value.revision;
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, iv);
  cipher.setAAD(binding(value.did, value.contract, revision));
  const plaintext = Buffer.from(JSON.stringify(value), 'utf8');
  try {
    const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
    return {
      version: 1, revision,
      iv: iv.toString('base64'), tag: cipher.getAuthTag().toString('base64'),
      ciphertext: ciphertext.toString('base64'),
    };
  } finally { plaintext.fill(0); }
}

function decryptState(envelope, key, did, contract) {
  need(exactKeys(envelope, ['version', 'revision', 'iv', 'tag', 'ciphertext'])
    && envelope.version === 1 && DECIMAL.test(envelope.revision), 'STORE_CORRUPT');
  let plaintext;
  try {
    const iv = Buffer.from(envelope.iv, 'base64');
    const tag = Buffer.from(envelope.tag, 'base64');
    const ciphertext = Buffer.from(envelope.ciphertext, 'base64');
    need(iv.length === 12 && tag.length === 16 && ciphertext.length > 0, 'STORE_CORRUPT');
    const decipher = createDecipheriv('aes-256-gcm', key, iv);
    decipher.setAAD(binding(did, contract, envelope.revision));
    decipher.setAuthTag(tag);
    plaintext = Buffer.concat([decipher.update(ciphertext), decipher.final()]);
    return JSON.parse(plaintext.toString('utf8'));
  } catch (error) {
    if (error instanceof ConnectionStoreError) throw error;
    throw new ConnectionStoreError('STORE_CORRUPT_OR_WRONG_KEY');
  } finally { plaintext?.fill(0); }
}

function validateState(value, did, contract) {
  need(exactKeys(value, ['version', 'did', 'contract', 'revision', 'recoveryVerified',
    'preimageHex', 'commitment', 'context', 'rooms']), 'STORE_CORRUPT');
  need(value.version === 1 && value.did === did && value.contract === contract
    && DECIMAL.test(value.revision) && typeof value.recoveryVerified === 'boolean'
    && /^0x[0-9a-f]{64}$/.test(value.preimageHex) && /^0x[0-9a-f]{64}$/.test(value.commitment)
    && value.commitment === '0x' + sha(Buffer.from(value.preimageHex.slice(2), 'hex'))
    && validContext(value.context)
    && value.rooms !== null && typeof value.rooms === 'object' && !Array.isArray(value.rooms), 'STORE_CORRUPT');
  for (const [room, state] of Object.entries(value.rooms)) {
    need(ROOM.test(room) && exactKeys(state, ['liveVerified', 'verifiedAtMs', 'observedHighwater',
      'highestReserved', 'reservations']), 'STORE_CORRUPT');
    need(typeof state.liveVerified === 'boolean'
      && (state.verifiedAtMs === null || Number.isSafeInteger(state.verifiedAtMs) && state.verifiedAtMs >= 0)
      && (state.observedHighwater === null || NONCE.test(state.observedHighwater))
      && (state.highestReserved === null || NONCE.test(state.highestReserved))
      && Array.isArray(state.reservations) && state.reservations.length <= 64, 'STORE_CORRUPT');
    let previous = -1n;
    const actions = new Set();
    for (const r of state.reservations) {
      need(exactKeys(r, ['nonce', 'actionDigest', 'status']) && NONCE.test(r.nonce)
        && HASH.test(r.actionDigest) && ['RESERVED', 'SEND_ATTEMPTED', 'OBSERVED', 'AMBIGUOUS',
          'RECONCILED_PRESENT', 'RECONCILED_ABSENT'].includes(r.status)
        && BigInt(r.nonce) > previous && !actions.has(r.actionDigest), 'STORE_CORRUPT');
      previous = BigInt(r.nonce); actions.add(r.actionDigest);
    }
    need((state.highestReserved === null && state.reservations.length === 0)
      || state.highestReserved === state.reservations.at(-1)?.nonce, 'STORE_CORRUPT');
  }
  return value;
}

function validContext(value) {
  try {
    const encoded = JSON.stringify(value);
    if (encoded === undefined || Buffer.byteLength(encoded) > 131072) return false;
    const visit = item => {
      if (item === null || ['string', 'number', 'boolean'].includes(typeof item)) return true;
      if (Array.isArray(item)) return item.every(visit);
      if (typeof item !== 'object' || Object.getPrototypeOf(item) !== Object.prototype) return false;
      return Object.entries(item).every(([key, child]) =>
        !/^(?:preimage|secret|private[_-]?key|seed)$/i.test(key) && visit(child));
    };
    return visit(value);
  } catch { return false; }
}

function pointerBody(did, contract, commitment) { return { version: 1, did, contract, commitment }; }
function pointerBytes(did, contract, commitment) {
  const body = pointerBody(did, contract, commitment);
  return json({ ...body, checksum: sha(json(body)) });
}
function readPointer(directory) {
  const raw = readJsonFile(resolve(directory, POINTER_FILE), 4096, 'POINTER_MISSING_OR_CORRUPT').parsed;
  need(exactKeys(raw, ['version', 'did', 'contract', 'commitment', 'checksum']), 'POINTER_CORRUPT');
  const body = pointerBody(raw.did, raw.contract, raw.commitment);
  need(raw.version === 1 && DID.test(raw.did) && CONTRACT.test(raw.contract)
    && /^0x[0-9a-f]{64}$/.test(raw.commitment) && raw.checksum === sha(json(body)), 'POINTER_CORRUPT');
  return body;
}

function witnessFor(value, stateBytes) {
  const highwater = Object.fromEntries(Object.entries(value.rooms).map(([room, v]) => [room, v.highestReserved]));
  const body = { version: 1, revision: value.revision, stateDigest: sha(stateBytes), highwater };
  return { ...body, checksum: sha(json(body)) };
}

function validateWitness(raw, value, stateBytes) {
  need(exactKeys(raw, ['version', 'revision', 'stateDigest', 'highwater', 'checksum']), 'WITNESS_CORRUPT');
  const body = { version: raw.version, revision: raw.revision, stateDigest: raw.stateDigest, highwater: raw.highwater };
  need(raw.version === 1 && raw.revision === value.revision && HASH.test(raw.stateDigest)
    && raw.stateDigest === sha(stateBytes) && HASH.test(raw.checksum) && raw.checksum === sha(json(body))
    && JSON.stringify(raw.highwater) === JSON.stringify(
      Object.fromEntries(Object.entries(value.rooms).map(([room, v]) => [room, v.highestReserved]))),
  'STORE_ROLLBACK_OR_CORRUPTION');
}

function readJsonFile(path, maxBytes, code) {
  try {
    const stat = lstatSync(path);
    need(stat.isFile() && !stat.isSymbolicLink() && stat.size > 0 && stat.size <= maxBytes, code);
    const bytes = readFileSync(path);
    return { bytes, parsed: JSON.parse(bytes.toString('utf8')) };
  } catch (error) {
    if (error instanceof ConnectionStoreError) throw error;
    throw new ConnectionStoreError(code);
  }
}

function validateIdentity(did, contract, key) {
  need(DID.test(did), 'INVALID_DID');
  need(CONTRACT.test(contract), 'INVALID_CONTRACT');
  need(Buffer.isBuffer(key) && key.length === 32, 'INVALID_CUSTODY_KEY');
}

export class ConnectionStore {
  static async initialize(directory, { key, did, contract, preimage = undefined, context = null, testFault = undefined }) {
    validateIdentity(did, contract, key);
    need(validContext(context), 'INVALID_CONTEXT');
    need(testFault === undefined || typeof testFault === 'function', 'INVALID_TEST_FAULT');
    const root = resolve(directory);
    need(!existsSync(root), 'STORE_ALREADY_EXISTS');
    mkdirSync(root, { mode: 0o700 });
    need(!lstatSync(root).isSymbolicLink(), 'STORE_UNSAFE_PATH');
    await fsyncDirectory(dirname(root));
    const store = new ConnectionStore(root, key, did, contract, testFault);
    let secret;
    try {
      await store.#createLock();
      need(preimage === undefined || Buffer.isBuffer(preimage), 'INVALID_PREIMAGE');
      secret = preimage === undefined ? randomBytes(32) : Buffer.from(preimage);
      need(secret.length === 32, 'INVALID_PREIMAGE');
      const commitment = '0x' + sha(secret);
      await atomicWrite(resolve(root, POINTER_FILE), pointerBytes(did, contract, commitment), testFault, POINTER_FILE);
      store.value = {
        version: 1, did, contract, revision: '0', recoveryVerified: false,
        preimageHex: '0x' + secret.toString('hex'), commitment,
        context: JSON.parse(JSON.stringify(context)), rooms: {},
      };
      await store.#persist(store.value);
      // ACCEPT remains unavailable until ciphertext has been durably committed, read back,
      // decrypted, and checked against the same contract-bound commitment.
      store.value = store.#readPair();
      store.value.recoveryVerified = true;
      await store.#commit(store.value);
      return store;
    } catch (error) { await store.close(); throw error; }
    finally { secret?.fill(0); }
  }

  static async reopen(directory, { key, did = undefined, contract = undefined, testFault = undefined }) {
    need(Buffer.isBuffer(key) && key.length === 32, 'INVALID_CUSTODY_KEY');
    need(testFault === undefined || typeof testFault === 'function', 'INVALID_TEST_FAULT');
    const root = resolve(directory);
    need(existsSync(root) && lstatSync(root).isDirectory() && !lstatSync(root).isSymbolicLink(), 'STORE_MISSING');
    const pointer = readPointer(root);
    need((did === undefined || did === pointer.did) && (contract === undefined || contract === pointer.contract),
      'POINTER_BINDING_MISMATCH');
    validateIdentity(pointer.did, pointer.contract, key);
    const store = new ConnectionStore(root, key, pointer.did, pointer.contract, testFault);
    try {
      await store.#createLock();
      store.value = store.#readPair();
      need(store.value.commitment === pointer.commitment, 'POINTER_BINDING_MISMATCH');
      if (!store.value.recoveryVerified) {
        store.value.recoveryVerified = true;
        await store.#commit(store.value);
      }
      return store;
    } catch (error) { await store.close(); throw error; }
  }

  static readPointer(directory) { return Object.freeze(readPointer(resolve(directory))); }

  static async recoverStaleLock(directory, { expectedPid, stateReconciled }) {
    const root = resolve(directory), path = resolve(root, LOCK_FILE);
    need(stateReconciled === true && Number.isSafeInteger(expectedPid) && expectedPid > 0,
      'STALE_LOCK_RECOVERY_NOT_AUTHORIZED');
    const lock = readJsonFile(path, 4096, 'STALE_LOCK_MISSING').parsed;
    need(exactKeys(lock, ['version', 'pid']) && lock.version === 1 && lock.pid === expectedPid,
      'STALE_LOCK_OWNER_MISMATCH');
    try { process.kill(expectedPid, 0); throw new ConnectionStoreError('STALE_LOCK_OWNER_ALIVE'); }
    catch (error) {
      if (error instanceof ConnectionStoreError) throw error;
      need(error?.code === 'ESRCH', 'STALE_LOCK_OWNER_UNVERIFIED');
    }
    unlinkSync(path); await fsyncDirectory(root);
  }

  constructor(root, key, did, contract, testFault = undefined) {
    this.root = root; this.did = did; this.contract = contract; this.key = Buffer.from(key);
    this.statePath = resolve(root, STATE_FILE); this.witnessPath = resolve(root, WITNESS_FILE);
    this.lockPath = resolve(root, LOCK_FILE); this.closed = false; this.broken = false;
    this.closing = false; this.ownsLock = false;
    this.mutationTail = Promise.resolve(); this.closePromise = undefined;
    // Local fault injection exercises durability barriers; production callers omit it.
    this.testFault = testFault;
  }

  async #createLock() {
    let created = false;
    try {
      this.lockHandle = await open(this.lockPath, 'wx', 0o600);
      created = true;
      await this.lockHandle.writeFile(json({ version: 1, pid: process.pid }));
      await this.lockHandle.sync(); await fsyncDirectory(this.root);
      this.ownsLock = true;
    } catch {
      try {
        if (this.lockHandle !== undefined) await this.lockHandle.close();
        this.lockHandle = undefined;
      } catch { /* fail closed below */ }
      try {
        if (created && isRegular(this.lockPath)) { unlinkSync(this.lockPath); await fsyncDirectory(this.root); }
      } catch { /* fail closed */ }
      this.key.fill(0);
      throw new ConnectionStoreError('STORE_LOCKED_RECONCILIATION_REQUIRED');
    }
  }

  #assertUsable() { need(!this.closed && !this.broken, this.closed ? 'STORE_CLOSED' : 'STORE_RECOVERY_REQUIRED'); }

  #readPair() {
    const state = readJsonFile(this.statePath, 1024 * 1024, 'STORE_MISSING_OR_CORRUPT');
    const value = validateState(decryptState(state.parsed, this.key, this.did, this.contract), this.did, this.contract);
    need(state.parsed.revision === value.revision, 'STORE_CORRUPT');
    const witness = readJsonFile(this.witnessPath, 256 * 1024, 'WITNESS_MISSING_OR_CORRUPT');
    validateWitness(witness.parsed, value, state.bytes);
    return value;
  }

  async #persist(value) {
    validateState(value, this.did, this.contract);
    const stateBytes = json(encryptState(value, this.key));
    const witnessBytes = json(witnessFor(value, stateBytes));
    try {
      await atomicWrite(this.statePath, stateBytes, this.testFault, STATE_FILE);
      await atomicWrite(this.witnessPath, witnessBytes, this.testFault, WITNESS_FILE);
      const recovered = this.#readPair();
      need(recovered.revision === value.revision, 'STORE_READBACK_FAILED');
    } catch (error) { this.broken = true; throw error; }
  }

  async #commit(next) {
    this.#assertUsable();
    need(this.#readPair().revision === this.value.revision, 'STORE_CHANGED');
    next.revision = (BigInt(this.value.revision) + 1n).toString();
    await this.#persist(next);
    this.value = this.#readPair();
  }

  #enqueueMutation(operation) {
    need(!this.closed && !this.closing, 'STORE_CLOSED');
    const result = this.mutationTail.then(async () => {
      this.#assertUsable();
      return operation();
    });
    this.mutationTail = result.catch(() => undefined);
    return result;
  }

  recoveryStatus() {
    this.#assertUsable();
    return Object.freeze({ verified: this.value.recoveryVerified, commitment: this.value.commitment,
      did: this.did, contract: this.contract });
  }

  assertAcceptReady() {
    this.#assertUsable();
    const disk = this.#readPair(), pointer = readPointer(this.root);
    need(disk.revision === this.value.revision && disk.commitment === this.value.commitment
      && pointer.did === this.did && pointer.contract === this.contract
      && pointer.commitment === this.value.commitment, 'STORE_CHANGED');
    need(disk.recoveryVerified, 'PREIMAGE_RECOVERY_UNVERIFIED');
    return this.recoveryStatus();
  }

  preimageCommitment() { return this.assertAcceptReady().commitment; }

  context() { this.#assertUsable(); return structuredClone(this.value.context); }

  // The callback is part of the trusted signer boundary. The temporary Buffer is zeroed after
  // callback completion, but JavaScript cannot prevent trusted callback code from copying it.
  async withPreimage(callback) {
    this.assertAcceptReady(); need(typeof callback === 'function', 'INVALID_CALLBACK');
    const secret = Buffer.from(this.value.preimageHex.slice(2), 'hex');
    try { return await callback(secret); } finally { secret.fill(0); }
  }

  async verifyLiveRoom(room, { observedNonce = undefined, observedNone = false, verifiedAtMs }) {
    return this.#enqueueMutation(async () => {
      need(ROOM.test(room), 'INVALID_ROOM');
      need((observedNone === true) !== (observedNonce !== undefined), 'INVALID_LIVE_OBSERVATION');
      need(observedNonce === undefined || typeof observedNonce === 'string' && NONCE.test(observedNonce),
        'LOSSLESS_NONCE_REQUIRED');
      need(Number.isSafeInteger(verifiedAtMs) && verifiedAtMs >= 0, 'INVALID_OBSERVATION_TIME');
      const next = structuredClone(this.value);
      const prior = next.rooms[room];
      if (prior) {
        need(prior.reservations.every(r => !['RESERVED', 'SEND_ATTEMPTED', 'AMBIGUOUS'].includes(r.status)),
          'NONCE_RECONCILIATION_REQUIRED');
        if (prior.observedHighwater !== null && observedNonce !== undefined)
          need(BigInt(observedNonce) >= BigInt(prior.observedHighwater), 'NONCE_OBSERVATION_ROLLBACK');
      }
      next.rooms[room] = {
        liveVerified: true, verifiedAtMs, observedHighwater: observedNonce ?? prior?.observedHighwater ?? null,
        highestReserved: prior?.highestReserved ?? null, reservations: prior?.reservations ?? [],
      };
      await this.#commit(next);
    });
  }

  async reserveNonce(room, { actionDigest, nowMs }) {
    // actionDigest binds the typed pre-nonce request. The later signed subject may include the
    // returned nonce; reusing the same request digest is permanently rejected.
    return this.#enqueueMutation(async () => {
      need(ROOM.test(room), 'INVALID_ROOM'); need(HASH.test(actionDigest), 'INVALID_ACTION_DIGEST');
      need(Number.isSafeInteger(nowMs) && nowMs >= 0, 'INVALID_NONCE_CLOCK');
      const state = this.value.rooms[room];
      need(state?.liveVerified === true, 'CURRENT_LIVE_STATE_UNVERIFIED');
      need(!state.reservations.some(r => r.actionDigest === actionDigest), 'ACTION_NONCE_ALREADY_USED');
      need(state.reservations.every(r => !['RESERVED', 'SEND_ATTEMPTED', 'AMBIGUOUS'].includes(r.status)),
        'NONCE_RECONCILIATION_REQUIRED');
      const candidates = [BigInt(nowMs)];
      if (state.highestReserved !== null) candidates.push(BigInt(state.highestReserved) + 1n);
      if (state.observedHighwater !== null) candidates.push(BigInt(state.observedHighwater) + 1n);
      const nextNonce = candidates.reduce((a, b) => a > b ? a : b);
      need(nextNonce <= MAX_NONCE, 'NONCE_EXHAUSTED');
      const nonce = nextNonce.toString();
      const next = structuredClone(this.value), target = next.rooms[room];
      target.highestReserved = nonce; target.reservations.push({ nonce, actionDigest, status: 'RESERVED' });
      await this.#commit(next); return nonce;
    });
  }

  async #transition(room, nonce, from, to) {
    this.#assertUsable(); need(ROOM.test(room) && typeof nonce === 'string' && NONCE.test(nonce), 'INVALID_NONCE');
    const next = structuredClone(this.value), target = next.rooms[room];
    const reservation = target?.reservations.find(r => r.nonce === nonce);
    need(reservation && from.includes(reservation.status), 'NONCE_TRANSITION_REJECTED');
    reservation.status = to; await this.#commit(next);
  }

  async markSendAttempted(room, nonce) {
    return this.#enqueueMutation(() => this.#transition(room, nonce, ['RESERVED'], 'SEND_ATTEMPTED'));
  }
  async markObserved(room, nonce) {
    return this.#enqueueMutation(() => this.#transition(room, nonce, ['SEND_ATTEMPTED'], 'OBSERVED'));
  }
  async markAmbiguous(room, nonce) {
    return this.#enqueueMutation(() => this.#transition(room, nonce, ['SEND_ATTEMPTED'], 'AMBIGUOUS'));
  }
  async reconcile(room, nonce, { observed }) {
    return this.#enqueueMutation(async () => {
      need(typeof observed === 'boolean', 'INVALID_RECONCILIATION');
      await this.#transition(room, nonce, ['SEND_ATTEMPTED', 'AMBIGUOUS'],
        observed ? 'RECONCILED_PRESENT' : 'RECONCILED_ABSENT');
    });
  }

  roomStatus(room) {
    this.#assertUsable(); need(ROOM.test(room), 'INVALID_ROOM');
    const state = this.value.rooms[room];
    return state ? structuredClone(state) : { liveVerified: false, verifiedAtMs: null,
      observedHighwater: null, highestReserved: null, reservations: [] };
  }

  async close() {
    if (this.closed) return;
    if (this.closePromise !== undefined) return this.closePromise;
    this.closing = true;
    this.closePromise = (async () => {
      await this.mutationTail;
      this.closed = true; this.key.fill(0);
      if (this.lockHandle !== undefined) await this.lockHandle.close();
      // A crashed process leaves this lock in place deliberately. There is no automatic stale-lock
      // deletion; an operator must first establish owner death and reconcile durable state.
      if (this.ownsLock && isRegular(this.lockPath)) {
        unlinkSync(this.lockPath); await fsyncDirectory(this.root); this.ownsLock = false;
      }
    })();
    return this.closePromise;
  }
}
