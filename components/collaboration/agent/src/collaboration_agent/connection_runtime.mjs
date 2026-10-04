// Root-deployed socket adapter. Credential availability never enables external writes.
import net from 'node:net';
import dgram from 'node:dgram';
import { spawn } from 'node:child_process';
import { readFileSync, existsSync, writeFileSync, renameSync, lstatSync, readdirSync } from 'node:fs';
import { hkdfSync, randomUUID } from 'node:crypto';
import { resolve, basename } from 'node:path';
import { ConnectionStore } from './connection_store.mjs';
import { AsyncPilotLedger, ledgerPath } from './pilot_ledger.mjs';
import { official, requireThat as need, fields, envelopeDigest, digest, objectDigest } from './pilot_protocol.mjs';
import { withRealCredentialFromDirectory } from './real_credential.mjs';
import { validateNoncePacket } from './real_nonce.mjs';
import { PROJECT_DID } from './connection_approval.mjs';
import { validateFirstAccept, revalidateFirstAccept } from './first_accept_packet.mjs';

// Closed vocabulary only: never persist exception messages, paths, stacks or RPC material.
let startupPhase = 'ENTRY';
export function connectionPhase(phase) { startupPhase = phase; }
export function recordConnectionFailure(error) {
  const phases = ['ENTRY', 'CONFIG', 'SOCKET_ACTIVATION', 'CREDENTIAL', 'OFFICIAL_RUNTIME',
    'PROCESS_BOUNDARY', 'IDENTITY', 'LEDGER', 'CUSTODY', 'SIGNER_INIT', 'LISTEN', 'RUNNING'];
  const codes = ['ERR_ACCESS_DENIED', 'EACCES', 'EPERM', 'ENOENT', 'EINVAL', 'EBADF',
    'EAFNOSUPPORT', 'SOCKET_ACTIVATION_REQUIRED', 'DUMMY_CREDENTIAL_MISMATCH',
    'OFFICIAL_RUNTIME_UNAVAILABLE', 'OS_BOUNDARY_REQUIRED', 'REAL_MODE_DISABLED',
    'REAL_CREDENTIAL_DIRECTORY_REQUIRED', 'REAL_CREDENTIAL_UNAVAILABLE',
    'REAL_CREDENTIAL_MALFORMED', 'REAL_CREDENTIAL_DID_MISMATCH',
    'REAL_CREDENTIAL_REJECTED', 'LEDGER_LOCKED_RECONCILIATION_REQUIRED', 'LEDGER_CORRUPT',
    'ORPHAN_CUSTODY_RECONCILIATION_REQUIRED'];
  // Some platform read failures are deliberately collapsed by the credential
  // loader into a fixed Error message rather than a Denied code. Only admit
  // messages already present in this closed vocabulary.
  const candidate = typeof error?.code === 'string' ? error.code : error?.message;
  const value = { schema: 1, phase: phases.includes(startupPhase) ? startupPhase : 'ENTRY',
    code: codes.includes(candidate) ? candidate : 'SIGNER_STARTUP_FAILED',
    invocationId: /^[0-9a-f]{32}$/.test(process.env.INVOCATION_ID ?? '') ? process.env.INVOCATION_ID : null };
  try {
    const temp = '/var/lib/collab-signer/.startup-diagnostic-' + randomUUID() + '.json';
    writeFileSync(temp, JSON.stringify(value) + '\n', { flag: 'wx', mode: 0o600 });
    renameSync(temp, '/var/lib/collab-signer/startup-diagnostic.json');
  } catch {} // stdout/stderr remain silent even if the diagnostic directory is inaccessible.
}

// One JSON request per connection, bounded bytes and duration. Socket ACLs are installed by root.
export function serveSocket(fd, handle) {
  const server = net.createServer({ allowHalfOpen: true }, socket => {
    let text = '', done = false;
    socket.setTimeout(5000, () => socket.destroy());
    socket.on('error', () => {});
    socket.on('data', async bytes => {
      if (done) return;
      text += bytes.toString('utf8');
      if (Buffer.byteLength(text) > 65536) { done = true; socket.destroy(); return; }
      if (!text.endsWith('\n')) return;
      done = true;
      try { socket.end(JSON.stringify({ ok: true, result: await handle(JSON.parse(text)) }) + '\n'); }
      catch { socket.end('{"ok":false,"code":"CONNECTION_REQUEST_DENIED"}\n'); }
    });
  });
  server.listen({ fd });
  return server;
}
export function callSocket(path, request) {
  return new Promise((resolveCall, reject) => {
    const socket = net.createConnection(path);
    let text = '';
    const fail = () => { socket.destroy(); reject(new Error('CONNECTION_UNAVAILABLE')); };
    socket.setTimeout(5000, fail); socket.on('error', fail);
    socket.on('connect', () => socket.end(JSON.stringify(request) + '\n'));
    socket.on('data', bytes => { text += bytes.toString('utf8'); if (Buffer.byteLength(text) > 65536) fail(); });
    socket.on('end', () => {
      try { const result = JSON.parse(text); need(result.ok === true, 'CONNECTION_REQUEST_DENIED'); resolveCall(result.result); }
      catch { fail(); }
    });
  });
}

export async function observedProcessBoundary() {
  let childDenied = false;
  try { const child = spawn('/usr/bin/true', [], { stdio: 'ignore' }); child.on('error', () => {}); }
  catch (error) { childDenied = error.code === 'ERR_ACCESS_DENIED'; }
  const inetDenied = await new Promise(resolveProbe => {
    const socket = dgram.createSocket('udp4');
    socket.on('error', error => { try { socket.close(); } catch {} resolveProbe(['EAFNOSUPPORT', 'EPERM', 'EACCES'].includes(error.code)); });
    socket.bind(0, '127.0.0.1', () => { socket.close(); resolveProbe(false); });
  });
  return { uid: process.getuid(), childDenied, inetDenied };
}

export function validateDummyCredential(seed, fingerprint) {
  need(Buffer.isBuffer(seed) && seed.length === 32 && typeof fingerprint === 'string'
    && /^[0-9a-f]{64}$/.test(fingerprint) && digest(seed) === fingerprint, 'DUMMY_CREDENTIAL_MISMATCH');
}

// Unlike existsSync, errors and dangling links cannot establish safe absence.
function entryExists(path) {
  try { lstatSync(path); return true; }
  catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}

export async function createConnectionRuntime() {
  connectionPhase('CONFIG');
  const config = JSON.parse(readFileSync('/etc/collab-connection/config.json', 'utf8'));
  const real = config.mode === 'REAL_ACCEPT_PREPARATION';
  need((real || config.mode === 'DUMMY_OFFLINE') && config.projectDid === PROJECT_DID, 'REAL_MODE_DISABLED');
  // A separate root-owned config field is required; never inferred from credential presence.
  need(!real || typeof config.externalWriteEnabled === 'boolean', 'WRITE_POLICY_REQUIRED');
  connectionPhase('SOCKET_ACTIVATION');
  need(process.env.LISTEN_PID === String(process.pid) && process.env.LISTEN_FDS === '2', 'SOCKET_ACTIVATION_REQUIRED');
  const names = process.env.LISTEN_FDNAMES?.split(':');
  need(names?.length === 2 && names.includes('worker') && names.includes('gate'), 'SOCKET_ACTIVATION_REQUIRED');
  connectionPhase('CREDENTIAL');
  const seed = real
    ? await withRealCredentialFromDirectory(process.env.CREDENTIALS_DIRECTORY, bytes => Buffer.from(bytes))
    : readFileSync(resolve(process.env.CREDENTIALS_DIRECTORY, 'dummy'));
  if (!real) validateDummyCredential(seed, config.dummyCredentialSha256);
  connectionPhase('OFFICIAL_RUNTIME');
  const { t, signing } = await official();
  let firstAccept = null, firstAcceptPreimage = null;
  if (config.firstAcceptDigest !== undefined) {
    need(real && typeof config.firstAcceptDigest === 'string', 'FIRST_ACCEPT_BINDING_REQUIRED');
    firstAccept = JSON.parse(readFileSync('/etc/collab-connection/first-accept.json', 'utf8'));
    need(firstAccept.digest === config.firstAcceptDigest, 'FIRST_ACCEPT_BINDING_REQUIRED');
    await validateFirstAccept(firstAccept, { t, signing, fresh: false });
    firstAcceptPreimage = readFileSync(resolve(process.env.CREDENTIALS_DIRECTORY, 'accept-preimage'));
    need(firstAcceptPreimage.length === 32
      && t.hashLockFromPreimage(firstAcceptPreimage).hash === firstAccept.packet.statement,
      'FIRST_ACCEPT_PREIMAGE_MISMATCH');
  }
  connectionPhase('PROCESS_BOUNDARY');
  const boundary = await observedProcessBoundary();
  need(boundary.childDenied && boundary.inetDenied, 'OS_BOUNDARY_REQUIRED');
  connectionPhase('IDENTITY');
  const did = signing.signerFromSeed(seed).did;
  need(real ? did === PROJECT_DID : did !== PROJECT_DID, 'REAL_CREDENTIAL_REJECTED');
  const key = Buffer.from(hkdfSync('sha256', seed, Buffer.alloc(0), real ? 'collab-real-accept-custody-v1' : 'collab-offline-custody-v1', 32));
  const root = '/var/lib/collab-signer';
  const ledgerRoot = resolve(root, 'ledger'), custodyRoot = resolve(root, real ? 'real-accept-custody' : 'custody');
  connectionPhase('LEDGER');
  const identityPath = ledgerPath(did, ledgerRoot);
  if (real && entryExists(ledgerRoot)) {
    need(!readdirSync(ledgerRoot).some(name => name.startsWith(basename(identityPath) + '.')
      && name !== basename(identityPath) + '.lock'), 'LEDGER_CORRUPT');
  }
  const prior = real ? entryExists(identityPath) : existsSync(identityPath);
  const ledger = await AsyncPilotLedger.open(did, { root: ledgerRoot, create: !prior });
  connectionPhase('CUSTODY');
  let custody = null;
  // A dangling link or an unreadable path is not proof of absent Real custody.
  // This root contains the encrypted state, nonce reservations and witnesses,
  // including the crash window before ADMITTED reaches the public ledger.
  const custodyExists = real ? entryExists(custodyRoot) : existsSync(custodyRoot);
  if (prior && ledger.value.admission) {
    custody = await ConnectionStore.reopen(custodyRoot, { key, did, contract: ledger.value.admission.contract });
    await custody.assertAcceptReady();
  } else need(!custodyExists, 'ORPHAN_CUSTODY_RECONCILIATION_REQUIRED');
  // open() has already locked and fully validated this exact identity's schema,
  // checksum and DID. Reuse its durable snapshot; never create/reset/delete it.
  const emptyIdentity = real && ledger.value.did === PROJECT_DID
    && ledger.value.quotaConsumed === false && ledger.value.admission === null
    && ledger.value.state === null && ledger.value.actions.length === 0 && !custodyExists
    && !readdirSync(ledgerRoot).some(name => name.startsWith(basename(ledger.path) + '.')
      && name !== basename(ledger.lock)); // Uncommitted/unknown identity siblings are uncertain.
  need(!real || prior || emptyIdentity, 'LEDGER_CORRUPT');
  let reusableIdentity = false;
  if (emptyIdentity) {
    // Transport alone can inspect its receipt directory; no new signer fs grant.
    const state = await callSocket('/run/collab-connection/transport.sock', { operation: 'emptyState', value: {} });
    fields(state, ['empty']); need(typeof state.empty === 'boolean', 'TRANSPORT_STATE_UNVERIFIED');
    need(prior || state.empty === true, 'TRANSPORT_STATE_UNVERIFIED');
    reusableIdentity = prior && state.empty === true;
  }
  const recoveryRequired = prior && !reusableIdentity;
  need(!firstAccept || !recoveryRequired, 'RECONCILIATION_ONLY');
  let armedFirstAcceptObservation = null, armedFirstAcceptNonce = null;
  const pending = new Map();
  const inFlight = new Set(), servers = [];
  let closing = false;
  let next = 0;
  // Replace only the process IPC output in this protected entrypoint. No raw diagnostic output.
  process.send = message => {
    if (Object.hasOwn(message, 'boot')) return;
    const p = pending.get(message.channel + ':' + message.id);
    if (!p) return;
    pending.delete(message.channel + ':' + message.id);
    if (message.ok) p.resolve(message.result); else p.reject(new Error('SIGNER_REQUEST_DENIED'));
  };
  const shutdown = async () => {
    if (closing) return;
    closing = true;
    for (const server of servers) server.close();
    await Promise.allSettled([...inFlight]);
    await custody?.close(); await ledger.close(); key.fill(0); seed.fill(0); firstAcceptPreimage?.fill(0); process.exit(0);
  };
  process.on('SIGTERM', shutdown);
  return {
    seed, real, reusableIdentity, firstAccept, firstAcceptPreimage, mode: config.mode, externalWriteEnabled: real && config.externalWriteEnabled === true, init: { mode: 'TEST_EPHEMERAL', recovery: recoveryRequired ? ledger.value : null },
    emit: entry => ledger.append(entry),
    async finalRevalidateFirstAccept(snapshot) {
      need(firstAccept !== null && custody === null && !recoveryRequired,
        'FIRST_ACCEPT_BINDING_REQUIRED');
      const validated = await revalidateFirstAccept(firstAccept, snapshot, { t, signing });
      const normalized = await validateNoncePacket(validated.observation);
      armedFirstAcceptObservation = structuredClone(validated.observation);
      armedFirstAcceptNonce = structuredClone(normalized);
      return validated;
    },
    async persistPreimage({ preimage, contract, context, observation = undefined }) {
      need(custody === null && !recoveryRequired, 'PILOT_QUOTA_CONSUMED');
      if (firstAccept) need(armedFirstAcceptObservation !== null && armedFirstAcceptNonce !== null
        && objectDigest(observation) === objectDigest(armedFirstAcceptObservation),
      'FIRST_ACCEPT_BINDING_REQUIRED');
      // Persist the official protocol objects' JSON representation. Optional
      // undefined fields are absent on disk; the store still validates the full
      // snapshot (including forbidden secret field names) before any write.
      const snapshot = JSON.parse(JSON.stringify(context));
      const bytes = Buffer.from(preimage.slice(2), 'hex');
      try { custody = await ConnectionStore.initialize(custodyRoot, { key, did, contract, preimage: bytes, context: snapshot }); }
      finally { bytes.fill(0); }
      await custody.assertAcceptReady();
      if (firstAccept) {
        await custody.verifyLiveRoom(armedFirstAcceptNonce.room, {
          ...armedFirstAcceptNonce,
          observedNonce: armedFirstAcceptNonce.observedNonce === null
            ? undefined : armedFirstAcceptNonce.observedNonce,
        });
        armedFirstAcceptObservation = null; armedFirstAcceptNonce = null;
      }
    },
    async assertRecovery() { need(custody !== null, 'PREIMAGE_RECOVERY_UNVERIFIED'); await custody.assertAcceptReady(); },
    async observeNonce(value) {
      need(custody !== null && !recoveryRequired, 'RECONCILIATION_ONLY');
      need(!firstAccept, 'FIRST_ACCEPT_BINDING_REQUIRED');
      if (real) value = await validateNoncePacket(value);
      else fields(value, ['room', 'observedNonce', 'observedNone', 'verifiedAtMs']);
      need(Number.isSafeInteger(value.verifiedAtMs) && Math.abs(Date.now() - value.verifiedAtMs) <= 30000, 'STALE_OBSERVATION');
      await custody.verifyLiveRoom(value.room, { ...value, observedNonce: value.observedNonce === null ? undefined : value.observedNonce });
      return { verified: true, mode: config.mode };
    },
    reserve(room, actionDigest, nowMs) {
      const observation = custody.roomStatus(room);
      need(!real || firstAccept || observation.verifiedAtMs <= nowMs && nowMs - observation.verifiedAtMs <= 30000,
        'STALE_OBSERVATION');
      return custody.reserveNonce(room, { actionDigest, nowMs: firstAccept?.createdAt ?? nowMs });
    },
    async markSendAttempted(record) {
      need(real && config.externalWriteEnabled === true && !recoveryRequired, 'EXTERNAL_WRITE_DISABLED');
      await custody.markSendAttempted(record.room, record.nonce);
    },
    async markAmbiguous(record) { await custody.markAmbiguous(record.room, record.nonce); },
    async reconcileNonce(subject) {
      const reservation = custody.roomStatus(subject.room).reservations.find(r => r.nonce === subject.nonce);
      if (reservation?.status === 'RECONCILED_PRESENT') return;
      await custody.reconcile(subject.room, subject.nonce, { observed: true });
    },
    async sendAccept(value) {
      need(real && config.externalWriteEnabled === true && !recoveryRequired, 'EXTERNAL_WRITE_DISABLED');
      return callSocket('/run/collab-connection/transport.sock', { operation: 'sendAccept', value });
    },
    async handoff(record, value) {
      if (real) {
        const result = await callSocket('/run/collab-connection/transport.sock', { operation: 'validateAccept', value });
        need(result.envelopeDigest === envelopeDigest(record) && result.externalWrites === 0, 'TRANSPORT_REJECTED');
        return;
      }
      const result = await callSocket('/run/collab-connection/transport.sock', { operation: 'validateOffline', record });
      need(result.envelopeDigest === envelopeDigest(record) && result.externalWrites === 0, 'TRANSPORT_REJECTED');
    },
    listen(dispatch) {
      connectionPhase('LISTEN');
      let listening = 0;
      for (const channel of ['worker', 'gate']) servers.push(serveSocket(3 + names.indexOf(channel), request => {
        need(!closing, 'SIGNER_STOPPING');
        fields(request, ['method', 'value']);
        need(channel !== 'worker' || ['status', 'prepare', 'sign', 'prepareWrite'].includes(request.method), 'WORKER_CAPABILITY_DENIED');
        need(channel !== 'gate' || ['admit', 'observe', 'approvalPreview', 'approve', 'nonceObservation', 'stop', 'reconcileRestart', 'boundaryStatus', ...(firstAccept ? ['reconcileFirstAccept'] : []), ...(real ? ['sendAccept'] : [])].includes(request.method), 'GATE_CAPABILITY_DENIED');
        if (channel === 'gate' && request.method === 'boundaryStatus') { fields(request.value, []); return { ...boundary, did, mode: config.mode, externalWriteEnabled: real && config.externalWriteEnabled === true,
          ...(firstAccept ? { firstAcceptDigest: firstAccept.digest } : {}) }; }
        const id = ++next;
        const result = new Promise((resolveRpc, reject) => {
          pending.set(channel + ':' + id, { resolve: resolveRpc, reject });
          dispatch(channel, { id, ...request });
        });
        inFlight.add(result);
        result.then(() => inFlight.delete(result), () => inFlight.delete(result));
        return result;
      }).once('listening', () => { if (++listening === 2) connectionPhase('RUNNING'); }));
    },
  };
}
