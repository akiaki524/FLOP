// Trusted supervisor storage. Fixed DID namespace, independent of journal choice.
// Only public commitments enter this ledger; it is not secret custody or a signer.
import { mkdirSync, openSync, closeSync, writeFileSync, fsyncSync, readFileSync,
  renameSync, unlinkSync, lstatSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { randomUUID } from 'node:crypto';
import { open } from 'node:fs/promises';
import { ROOT, digest, objectDigest, requireThat as need, fields, TYPES, outputGuard } from './pilot_protocol.mjs';

export const LEDGER_ROOT = resolve(ROOT, '.local/batch17a1');
const lifecycle = ['PREPARED', 'SIGNED', 'WRITE_PREPARED', 'SEND_ATTEMPTED', 'OBSERVED', 'AMBIGUOUS', 'RECONCILED'];
const hash = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
export function ledgerPath(did, root = LEDGER_ROOT) {
  need(/^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$/.test(did), 'INVALID_LEDGER_DID');
  return resolve(root, 'identity-' + digest(did) + '.json');
}
export function fsyncDirectory(directory) {
  const fd = openSync(directory, 'r');
  try { fsyncSync(fd); } finally { closeSync(fd); }
}
export function atomicPublicState(path, value, fault = () => {}) {
  // TEST fault injection is local storage testing, never an RPC capability.
  const bytes = JSON.stringify({ value, checksum: objectDigest(value) }) + '\n';
  const temp = path + '.pending-' + randomUUID();
  const fd = openSync(temp, 'wx', 0o600);
  try { writeFileSync(fd, bytes); fsyncSync(fd); fault('file-fsync'); }
  finally { closeSync(fd); }
  renameSync(temp, path); fault('replace');
  fsyncDirectory(dirname(path)); fault('directory-fsync');
  need(readFileSync(path, 'utf8') === bytes, 'LEDGER_READBACK');
}
function validatePublicState(raw, did) {
  try {
    outputGuard([], raw);
    fields(raw, ['value', 'checksum']);
    const v = raw.value;
    need(hash(raw.checksum) && raw.checksum === objectDigest(v), 'LEDGER_CORRUPT');
    fields(v, ['version', 'mode', 'did', 'revision', 'quotaConsumed', 'admission', 'state', 'actions']);
    need(v.version === 1 && v.mode === 'TEST_EPHEMERAL' && v.did === did
      && Number.isSafeInteger(v.revision) && v.revision >= 0 && typeof v.quotaConsumed === 'boolean'
      && Array.isArray(v.actions) && v.actions.length <= 4, 'LEDGER_CORRUPT');
    need(v.quotaConsumed === (v.admission !== null), 'LEDGER_CORRUPT');
    if (v.admission) {
      fields(v.admission, ['offer', 'contract', 'sourceDigest', 'payerDid', 'offerDigest']);
      need(/^0x[0-9a-f]{64}$/.test(v.admission.offer) && /^0x[0-9a-f]{64}$/.test(v.admission.contract)
        && hash(v.admission.sourceDigest) && hash(v.admission.offerDigest), 'LEDGER_CORRUPT');
      if (v.state !== null) {
        fields(v.state, ['status', 'contract', 'rail', 'railRef']);
        need(['proposed', 'accepted', 'locked', 'claimed', 'refunded', 'cancelled', 'expired'].includes(v.state.status)
          && (v.state.contract === null || v.state.contract === v.admission.contract), 'LEDGER_CORRUPT');
      }
    } else need(v.actions.length === 0 && v.state === null, 'LEDGER_CORRUPT');
    const seen = new Set();
    for (const a of v.actions) {
      fields(a, ['subject', 'subjectDigest', 'status', 'envelopeDigest', ...(Object.hasOwn(a, 'approval') ? ['approval'] : [])]);
      if (a.approval != null) {
        fields(a.approval, ['actionId', 'subjectDigest', 'approvalDigest', 'expiresAt']);
        need(a.approval.actionId === a.subject.actionId && a.approval.subjectDigest === a.subjectDigest
          && hash(a.approval.approvalDigest) && Number.isSafeInteger(a.approval.expiresAt), 'LEDGER_CORRUPT');
      }
      fields(a.subject, ['session', 'actionId', 'type', 'did', 'room', 'offer', 'contract', 'ref', 'sourceDigest', 'nonce', 'payloadDigest']);
      need(TYPES.includes(a.subject.type) && !seen.has(a.subject.type) && lifecycle.includes(a.status)
        && a.subject.did === did && a.subject.contract === v.admission.contract
        && a.subject.offer === v.admission.offer && a.subject.sourceDigest === v.admission.sourceDigest
        && hash(a.subject.payloadDigest) && a.subjectDigest === objectDigest(a.subject)
        && /^(0|[1-9][0-9]{0,18})$/.test(a.subject.nonce)
        && (a.envelopeDigest === null || hash(a.envelopeDigest)), 'LEDGER_CORRUPT');
      need(a.status === 'PREPARED' || hash(a.envelopeDigest), 'LEDGER_CORRUPT');
      seen.add(a.subject.type);
    }
    return v;
  } catch { throw new Error('LEDGER_CORRUPT'); }
}
export function readPublicState(path, did) {
  try {
    const stat = lstatSync(path);
    need(stat.isFile() && !stat.isSymbolicLink() && stat.size <= 131072, 'LEDGER_CORRUPT');
    return validatePublicState(JSON.parse(readFileSync(path, 'utf8')), did);
  } catch { throw new Error('LEDGER_CORRUPT'); }
}
export class PilotLedger {
  constructor(did, { create = false, root = LEDGER_ROOT } = {}) {
    this.root = resolve(root);
    mkdirSync(this.root, { recursive: true, mode: 0o700 });
    need(!lstatSync(this.root).isSymbolicLink() && !lstatSync(dirname(this.root)).isSymbolicLink(), 'LEDGER_SYMLINK');
    // Persist the root directory entry too, including when the caller created it.
    // No identity/lock or durable ACK may precede this parent-directory barrier.
    fsyncDirectory(dirname(this.root));
    this.path = ledgerPath(did, this.root); this.lock = this.path + '.lock'; this.released = false;
    try { this.fd = openSync(this.lock, 'wx', 0o600); }
    catch { throw new Error('LEDGER_LOCKED_RECONCILIATION_REQUIRED'); }
    try {
      writeFileSync(this.fd, JSON.stringify({ pid: process.pid, did })); fsyncSync(this.fd); fsyncDirectory(this.root);
      if (create) {
        need(!existsSync(this.path), 'IDENTITY_ALREADY_REGISTERED');
        this.value = { version: 1, mode: 'TEST_EPHEMERAL', did, revision: 0,
          quotaConsumed: false, admission: null, state: null, actions: [] };
        atomicPublicState(this.path, this.value);
      } else this.value = readPublicState(this.path, did);
    } catch (error) { this.close(); throw error; }
  }
  append(entry) {
    const v = this.next(entry);
    atomicPublicState(this.path, v);
    this.value = readPublicState(this.path, v.did);
  }
  next(entry) {
    need(!this.released, 'LEDGER_CLOSED');
    need(objectDigest(readPublicState(this.path, this.value.did)) === objectDigest(this.value), 'LEDGER_CHANGED');
    const v = structuredClone(this.value);
    if (entry.event === 'ADMITTED') {
      need(!v.quotaConsumed && v.admission === null, 'PILOT_QUOTA_CONSUMED');
      v.quotaConsumed = true;
      v.admission = { offer: entry.offer, contract: entry.contract, sourceDigest: entry.sourceDigest,
        payerDid: entry.payerDid, offerDigest: entry.offerDigest };
    } else if (entry.event === 'PREPARED') {
      need(v.quotaConsumed && !v.actions.some(a => a.subject.type === entry.subject.type), 'PILOT_QUOTA_CONSUMED');
      v.actions.push({ subject: entry.subject, subjectDigest: entry.subjectDigest, status: 'PREPARED', envelopeDigest: null, approval: null });
    } else if (entry.event === 'APPROVED') {
      const a = v.actions.find(a => a.subject.actionId === entry.actionId);
      need(a && a.status === 'PREPARED' && !a.approval, 'LEDGER_APPROVAL_USED');
      a.approval = Object.fromEntries(['actionId', 'subjectDigest', 'approvalDigest', 'expiresAt'].map(k => [k, entry[k]]));
    } else if (lifecycle.includes(entry.event)) {
      const a = v.actions.find(a => a.subject.actionId === entry.actionId);
      need(a, 'LEDGER_ACTION_MISSING');
      const allowed = { SIGNED: ['PREPARED'], WRITE_PREPARED: ['SIGNED'], SEND_ATTEMPTED: ['WRITE_PREPARED'],
        OBSERVED: ['SEND_ATTEMPTED'], AMBIGUOUS: ['SEND_ATTEMPTED'], RECONCILED: ['SEND_ATTEMPTED', 'AMBIGUOUS'] };
      need(allowed[entry.event]?.includes(a.status), 'LEDGER_TRANSITION');
      a.status = entry.event;
      if (entry.event === 'SIGNED') a.envelopeDigest = entry.envelopeDigest;
    } else if (entry.event === 'OBSERVATION') v.state = entry.state;
    else if (entry.event === 'RECOVERY_RECONCILED') {
      for (const id of entry.actionIds) {
        const a = v.actions.find(a => a.subject.actionId === id);
        need(a && ['AMBIGUOUS', 'SEND_ATTEMPTED'].includes(a.status), 'LEDGER_TRANSITION');
        a.status = 'RECONCILED';
      }
      v.state = entry.state;
    }
    v.revision++;
    // Validate before commit and read back after replace. There is no reset/quota-release API.
    validatePublicState({ value: v, checksum: objectDigest(v) }, v.did);
    return v;
  }
  close() {
    if (this.released) return;
    this.released = true; closeSync(this.fd); unlinkSync(this.lock); fsyncDirectory(this.root);
  }
}

// The service uses owned FileHandles: Node 22 disables the raw-fd fsyncSync API
// with --permission, while open(path) + FileHandle.sync() enforces the path grant.
// The unrestricted legacy TEST supervisor retains its synchronous API above.
async function syncDirectory(directory) {
  const handle = await open(directory, 'r');
  try { await handle.sync(); } finally { await handle.close(); }
}
async function atomicPublicStateAsync(path, value, fault = () => {}) {
  const bytes = JSON.stringify({ value, checksum: objectDigest(value) }) + '\n';
  const temp = path + '.pending-' + randomUUID();
  const handle = await open(temp, 'wx', 0o600);
  try { await handle.writeFile(bytes); await handle.sync(); fault('file-fsync'); }
  finally { await handle.close(); }
  renameSync(temp, path);
  fault('replace');
  await syncDirectory(dirname(path));
  fault('directory-fsync');
  need(readFileSync(path, 'utf8') === bytes, 'LEDGER_READBACK');
}
export class AsyncPilotLedger extends PilotLedger {
  constructor() { throw new Error('USE_ASYNC_LEDGER_OPEN'); }
  static async open(did, { create = false, root = LEDGER_ROOT, testFault = undefined } = {}) {
    need(testFault === undefined || typeof testFault === 'function', 'INVALID_TEST_FAULT');
    const ledger = Object.create(AsyncPilotLedger.prototype);
    ledger.root = resolve(root);
    mkdirSync(ledger.root, { recursive: true, mode: 0o700 });
    need(!lstatSync(ledger.root).isSymbolicLink() && !lstatSync(dirname(ledger.root)).isSymbolicLink(), 'LEDGER_SYMLINK');
    await syncDirectory(dirname(ledger.root));
    ledger.path = ledgerPath(did, ledger.root);
    ledger.lock = ledger.path + '.lock'; ledger.released = false; ledger.busy = false;
    ledger.testFault = testFault;
    try { ledger.handle = await open(ledger.lock, 'wx', 0o600); }
    catch { throw new Error('LEDGER_LOCKED_RECONCILIATION_REQUIRED'); }
    try {
      await ledger.handle.writeFile(JSON.stringify({ pid: process.pid, did }));
      await ledger.handle.sync(); await syncDirectory(ledger.root);
      if (create) {
        need(!existsSync(ledger.path), 'IDENTITY_ALREADY_REGISTERED');
        await atomicPublicStateAsync(ledger.path, { version: 1, mode: 'TEST_EPHEMERAL', did, revision: 0,
          quotaConsumed: false, admission: null, state: null, actions: [] }, testFault);
      }
      ledger.value = readPublicState(ledger.path, did);
      return ledger;
    } catch (error) { await ledger.close(); throw error; }
  }
  async append(entry) {
    need(!this.busy && !this.broken, 'LEDGER_BUSY_OR_BROKEN');
    this.busy = true;
    try {
      const v = this.next(entry);
      await atomicPublicStateAsync(this.path, v, this.testFault);
      this.value = readPublicState(this.path, v.did);
    } catch (error) { this.broken = true; throw error; }
    finally { this.busy = false; }
  }
  async close() {
    need(!this.busy, 'LEDGER_BUSY_OR_BROKEN');
    if (this.released) return;
    this.released = true;
    await this.handle.close(); unlinkSync(this.lock); await syncDirectory(this.root);
  }
}
