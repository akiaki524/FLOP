import net from 'node:net';

const ACTIONS = new Set(['ACCEPT', 'INITIAL_HEARTBEAT', 'DELIVERY_GCD', 'REVEAL']);
const METHODS = new Set([
  'status', 'admit', 'issue', 'reconcile', 'observeCompletion',
  'closeCallOwnerRegister', 'closeCallTakerLong',
]);
const MAX_REQUEST_BYTES = 16 << 10;

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}
function need(condition, code) {
  if (!condition) fail(code);
}
function exactKeys(value, names) {
  need(value && typeof value === 'object' && !Array.isArray(value), 'RPC_INVALID');
  need(Object.keys(value).sort().join(',') === [...names].sort().join(','), 'RPC_INVALID');
}
function sourceBytes(value) {
  need(typeof value === 'string' && value.length > 0 && value.length <= 128, 'RPC_INVALID');
  let bytes;
  try { bytes = Buffer.from(value, 'base64url'); } catch { fail('RPC_INVALID'); }
  need(bytes.length > 0 && bytes.length <= 32 && bytes.toString('base64url') === value, 'RPC_INVALID');
  return bytes;
}

export function createPolicySignerRpcHandler(signer, { beforeRequest = async () => {} } = {}) {
  need(signer && typeof signer.status === 'function' && typeof signer.admit === 'function'
    && typeof signer.issue === 'function' && typeof signer.reconcile === 'function'
    && typeof signer.observeCompletion === 'function'
    && typeof signer.closeCallOwnerRegister === 'function'
    && typeof signer.closeCallTakerLong === 'function', 'RPC_SIGNER_REQUIRED');
  need(typeof beforeRequest === 'function', 'RPC_HOOK_REQUIRED');

  return async request => {
    exactKeys(request, ['method', 'value']);
    need(METHODS.has(request.method), 'RPC_METHOD_DENIED');
    if (request.method === 'status') {
      exactKeys(request.value, []);
      return signer.status();
    }
    if (request.method === 'closeCallOwnerRegister') {
      exactKeys(request.value, ['version','kind','action','expectedDid','subject','binding','preview']);
      await beforeRequest({
        method: 'closeCallOwnerRegister',
        action: 'CLOSE_CALL_OWNER_REGISTER',
        status: signer.status(),
      });
      return signer.closeCallOwnerRegister(request.value);
    }
    if (request.method === 'closeCallTakerLong') {
      exactKeys(request.value, [
        'version','kind','action','expectedDid','subject','binding','operation',
      ]);
      await beforeRequest({
        method: 'closeCallTakerLong',
        action: 'CLOSE_CALL_TAKER_LONG',
        status: signer.status(),
      });
      return signer.closeCallTakerLong(request.value);
    }
    if (request.method === 'admit') {
      exactKeys(request.value, ['offerSeq', 'sourceBase64url']);
      need(Number.isSafeInteger(request.value.offerSeq) && request.value.offerSeq > 0, 'RPC_INVALID');
      const source = sourceBytes(request.value.sourceBase64url);
      await beforeRequest({ method: 'admit', action: null, status: signer.status() });
      return signer.admit({
        offerSeq: request.value.offerSeq,
        sourceBytes: source,
      });
    }
    if (request.method === 'issue') {
      exactKeys(request.value, ['type']);
      need(ACTIONS.has(request.value.type), 'RPC_ACTION_DENIED');
      await beforeRequest({ method: 'issue', action: request.value.type, status: signer.status() });
      return signer.issue(request.value.type);
    }
    if (request.method === 'reconcile') {
      exactKeys(request.value, ['type']);
      need(ACTIONS.has(request.value.type), 'RPC_ACTION_DENIED');
      await beforeRequest({ method: 'reconcile', action: request.value.type, status: signer.status() });
      return signer.reconcile(request.value.type);
    }
    exactKeys(request.value, []);
    await beforeRequest({ method: 'observeCompletion', action: null, status: signer.status() });
    return signer.observeCompletion();
  };
}

function publicCode(error) {
  const code = error?.code;
  return typeof code === 'string' && /^[A-Z0-9_]+$/.test(code) ? code : 'RPC_REQUEST_DENIED';
}

export function serveSocket(fd, handle) {
  need(Number.isSafeInteger(fd) && fd >= 0 && typeof handle === 'function', 'RPC_LISTENER_INVALID');
  const server = net.createServer({ allowHalfOpen: true }, socket => {
    let text = '';
    let done = false;
    socket.setTimeout(5000, () => socket.destroy());
    socket.on('error', () => {});
    socket.on('data', async bytes => {
      if (done) return;
      text += bytes.toString('utf8');
      if (Buffer.byteLength(text, 'utf8') > MAX_REQUEST_BYTES) {
        done = true;
        socket.destroy();
        return;
      }
      if (!text.endsWith('\n')) return;
      done = true;
      try {
        const request = JSON.parse(text);
        const result = await handle(request);
        socket.end(JSON.stringify({ ok: true, result }) + '\n');
      } catch (error) {
        socket.end(JSON.stringify({ ok: false, code: publicCode(error) }) + '\n');
      }
    });
  });
  server.listen({ fd });
  return server;
}


export function callSocket(path, request) {
  return new Promise((resolveCall, reject) => {
    const socket = net.createConnection(path);
    let text = '';
    let settled = false;
    const failCall = code => {
      if (settled) return;
      settled = true;
      socket.destroy();
      const error = new Error(code);
      error.code = code;
      reject(error);
    };
    socket.setTimeout(5000, () => failCall('RPC_DEPENDENCY_TIMEOUT'));
    socket.on('error', () => failCall('RPC_DEPENDENCY_UNAVAILABLE'));
    socket.on('connect', () => socket.end(JSON.stringify(request) + '\n'));
    socket.on('data', bytes => {
      if (settled) return;
      text += bytes.toString('utf8');
      if (Buffer.byteLength(text, 'utf8') > MAX_REQUEST_BYTES) {
        failCall('RPC_DEPENDENCY_INVALID');
      }
    });
    socket.on('end', () => {
      if (settled) return;
      try {
        const response = JSON.parse(text);
        if (!response || response.ok !== true) {
          const code = typeof response?.code === 'string' && /^[A-Z0-9_]+$/.test(response.code)
            ? response.code : 'RPC_DEPENDENCY_DENIED';
          failCall(code);
          return;
        }
        settled = true;
        resolveCall(response.result);
      } catch {
        failCall('RPC_DEPENDENCY_INVALID');
      }
    });
  });
}
