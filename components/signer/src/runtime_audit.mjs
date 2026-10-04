import {
  closeSync,
  fsyncSync,
  fstatSync,
  openSync,
  writeSync,
} from 'node:fs';

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}
const MAX_AUDIT_CODE_CHARS = 64;

function safeCode(error) {
  const value = error?.code ?? error?.message;
  return typeof value === 'string' && value.length <= MAX_AUDIT_CODE_CHARS
    && /^[A-Z0-9_]+$/.test(value)
    ? value
    : 'REQUEST_DENIED';
}
const AUDIT_METHODS = new Set([
  'status', 'admit', 'issue', 'reconcile', 'observeCompletion',
  'closeCallOwnerRegister', 'closeCallTakerLong',
]);
const AUDIT_ACTIONS = new Set([
  'ACCEPT', 'INITIAL_HEARTBEAT', 'DELIVERY_GCD', 'REVEAL',
  'CLOSE_CALL_OWNER_REGISTER', 'CLOSE_CALL_TAKER_LONG',
]);
export const MAX_AUDIT_BYTES = 1 << 20;

function auditShape(request) {
  const method = typeof request?.method === 'string' && AUDIT_METHODS.has(request.method)
    ? request.method : 'INVALID';
  const candidate = request?.value?.type;
  let action = (method === 'issue' || method === 'reconcile')
    && typeof candidate === 'string' && AUDIT_ACTIONS.has(candidate)
    ? candidate : null;
  if (method === 'closeCallOwnerRegister') action = 'CLOSE_CALL_OWNER_REGISTER';
  if (method === 'closeCallTakerLong') action = 'CLOSE_CALL_TAKER_LONG';
  return { method, action };
}

export class RuntimeAudit {
  #fd;
  #reservedEndBytes = 0;

  constructor(path) {
    try { this.#fd = openSync(path, 'a', 0o600); }
    catch { fail('AUDIT_UNAVAILABLE'); }
  }

  #bytes(entry) {
    const allowed = ['atMs', 'phase', 'method', 'action', 'ok', 'code', 'revision'];
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)
      || Object.keys(entry).some(key => !allowed.includes(key))) {
      fail('AUDIT_INVALID');
    }
    return Buffer.from(JSON.stringify(entry) + '\n', 'utf8');
  }

  #size() {
    const current = fstatSync(this.#fd).size;
    if (!Number.isSafeInteger(current) || current < 0) fail('AUDIT_UNAVAILABLE');
    return current;
  }

  #write(bytes) {
    try {
      writeSync(this.#fd, bytes);
      fsyncSync(this.#fd);
    } catch {
      fail('AUDIT_UNAVAILABLE');
    }
  }

  record(entry) {
    const bytes = this.#bytes(entry);
    const current = this.#size();
    if (current + this.#reservedEndBytes + bytes.length > MAX_AUDIT_BYTES) {
      fail('AUDIT_FULL');
    }
    this.#write(bytes);
  }

  begin(entry, maxEndEntry) {
    const beginBytes = this.#bytes(entry);
    const endBudget = this.#bytes(maxEndEntry).length;
    const current = this.#size();
    if (current + this.#reservedEndBytes + beginBytes.length + endBudget > MAX_AUDIT_BYTES) {
      fail('AUDIT_FULL');
    }
    this.#write(beginBytes);
    this.#reservedEndBytes += endBudget;
    return Object.freeze({ endBudget });
  }

  end(reservation, entry) {
    if (!reservation || !Number.isSafeInteger(reservation.endBudget)
      || reservation.endBudget <= 0 || reservation.endBudget > this.#reservedEndBytes) {
      fail('AUDIT_RESERVATION_INVALID');
    }
    const bytes = this.#bytes(entry);
    if (bytes.length > reservation.endBudget) fail('AUDIT_RESERVATION_INVALID');
    try {
      this.#write(bytes);
    } finally {
      this.#reservedEndBytes -= reservation.endBudget;
    }
  }

  close() {
    if (this.#fd === undefined) return;
    try { closeSync(this.#fd); } catch {}
    this.#fd = undefined;
    this.#reservedEndBytes = 0;
  }
}

export function auditedHandler({ signer, handle, audit, clock = Date.now }) {
  if (!signer || typeof signer.status !== 'function' || typeof handle !== 'function'
    || !audit || typeof audit.begin !== 'function' || typeof audit.end !== 'function') {
    fail('AUDIT_CONFIG_INVALID');
  }
  return async request => {
    const shape = auditShape(request);
    const begin = { atMs: clock(), phase: 'BEGIN', ...shape, ok: null, code: null,
      revision: signer.status().revision };
    const maxEnd = {
      atMs: Number.MAX_SAFE_INTEGER,
      phase: 'END',
      ...shape,
      ok: false,
      code: 'X'.repeat(MAX_AUDIT_CODE_CHARS),
      revision: Number.MAX_SAFE_INTEGER,
    };

    // Reserve enough durable space for the BEGIN and the largest possible END
    // before any state-dependent operation starts. Concurrent requests cannot
    // consume another in-flight request's END reservation.
    const reservation = audit.begin(begin, maxEnd);

    let result;
    let caught = null;
    try {
      result = await handle(request);
    } catch (error) {
      caught = error;
    }

    const end = {
      atMs: clock(),
      phase: 'END',
      ...shape,
      ok: caught === null,
      code: caught === null ? null : safeCode(caught),
      revision: signer.status().revision,
    };
    audit.end(reservation, end);
    if (caught !== null) throw caught;
    return result;
  };
}
