// Typed signer. The legacy TEST child uses IPC; the connection adapter uses protected
// systemd sockets and credentials. Only its explicit Real mode permits Paper ACCEPT.
// No RPC accepts a seed, credential path, URL, arbitrary text or frame to sign.
import { randomBytes, randomUUID } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { createInterface } from 'node:readline';
import { Denied, requireThat as need, fields, digest, objectDigest, official,
  blockNetwork, verifyRecord, safeState, TYPES, POLICY, outputGuard, envelopeDigest, testNonce,
  contractRecords } from './pilot_protocol.mjs';
import { canonicalApproval } from './pilot_approval.mjs';
import { createConnectionRuntime, connectionPhase, recordConnectionFailure } from './connection_runtime.mjs';

const offlineConnection = process.argv.includes('--offline-connection');
const fatal = error => {
  if (offlineConnection) { recordConnectionFailure(error); process.exit(70); }
  const code = error instanceof Denied ? error.code :
    typeof error?.code === 'string' && /^[A-Z_]+$/.test(error.code) ? error.code : 'SIGNER_INTERNAL_ERROR';
  if (process.connected) process.send({ boot: false, code }, () => process.exit(70));
  else process.exit(70);
};
process.on('uncaughtException', fatal);
process.on('unhandledRejection', fatal);
let connection = null;
try { connection = offlineConnection ? await createConnectionRuntime() : null; }
catch (error) { fatal(error); }
connectionPhase('SIGNER_INIT');
if (!connection) blockNetwork(); // Connection service is AF_UNIX-only at the OS boundary.
let stopped = false;
const confidential = [];
function protectedText(data) {
  return outputGuard(confidential, data);
}
const init = connection?.init ?? await new Promise(resolve => {
  process.once('message', message => resolve(message?.initialize));
});
fields(init, ['mode', 'recovery']); need(init.mode === 'TEST_EPHEMERAL', 'REAL_MODE_DISABLED');
let recovery = init.recovery;
let auditId = 0;
const auditPending = new Map();
process.on('message', message => {
  if (!Number.isSafeInteger(message?.auditAck)) return;
  const pending = auditPending.get(message.auditAck);
  if (!pending) return;
  auditPending.delete(message.auditAck); clearTimeout(pending.timer);
  if (message.ok === true) pending.resolve();
  else { stopped = true; pending.reject(new Denied('JOURNAL_UNAVAILABLE')); }
});
async function emit(entry) {
  protectedText(entry);
  if (connection) { await connection.emit(entry); return; }
  await new Promise((resolve, reject) => {
    const id = ++auditId;
    const timer = setTimeout(() => {
      stopped = true; auditPending.delete(id); reject(new Denied('JOURNAL_UNAVAILABLE'));
    }, 3000);
    auditPending.set(id, { resolve, reject, timer });
    process.send({ auditId: id, entry });
  });
}
const firstAccept = connection?.firstAccept;
const session = firstAccept?.packet.subject.session ?? randomUUID();
await emit({ event: 'START_TEST_EPHEMERAL', session });
const { t, signing } = await official();
// Legacy TEST uses local randomness; the connection adapter reads only systemd's dummy credential.
const seed = recovery ? null : connection ? connection.seed : randomBytes(32);
if (seed) confidential.push(seed.toString('hex'), seed.toString('base64'), seed.toString('base64url'));
let signer = recovery ? { did: recovery.did } : signing.signerFromSeed(seed);
// A reused validated identity already has its DID durably pinned. Preserve the
// existing snapshot/revision without appending a second identity announcement.
if (!recovery && !connection?.reusableIdentity) await emit({ event: 'IDENTITY', did: signer.did });
let preimage = null;
let candidate = null, accept = null, observed = [], paper = null, observedAt = 0;
let offset = 0, lastNow = 0, mockEnabled = false, mockMode = 'ack';
let mockRecords = [], ambiguous = null;
const actions = new Map(), nonceByRoom = new Map();
let cursors = new Map(), seenRecords = new Map();
function now() {
  const current = Date.now() + offset;
  need(current >= lastNow, 'CLOCK_ROLLBACK'); lastNow = current; return current;
}
function live() {
  need(!recovery, 'RECONCILIATION_ONLY');
  need(!stopped, 'STOPPED'); need(candidate !== null, 'NO_CANDIDATE');
  need(firstAccept || now() < candidate.sessionDeadline, 'SESSION_EXPIRED');
}
function state(records = observed) {
  const relevant = contractRecords(t, records, candidate.offer, accept, signer.did);
  const handshake = t.findContractHandshake([candidate.offerRecord, ...relevant], accept.contract);
  if (relevant.some(r => t.tryDecodeFrame(r.line)?.type === 'accept')) need(handshake !== null, 'TRANSCRIPT_REJECTED');
  const folded = t.foldTranscript([candidate.offerRecord, ...relevant]);
  for (let i = 0; i < folded.steps.length; i++) {
    const step = folded.steps[i], record = i ? relevant[i - 1] : candidate.offerRecord;
    if (!step.ok) {
      // Only this task's exact approved GCD delivery is allowed outside the frame grammar.
      need(record.room === t.dealRoom(accept.contract) && record.sender === signer.did
        && record.line === candidate.answer && t.verifyTranscriptRecord(record).ok, 'TRANSCRIPT_REJECTED');
    }
  }
  return folded.state;
}
function meta() {
  return { mode: connection ? connection.mode : 'TEST_EPHEMERAL', did: signer.did, offer: candidate.offer.id,
    contract: accept.contract, room: t.dealRoom(accept.contract), statement: accept.statement,
    sourceDigest: candidate.sourceDigest, heartbeatRequired: candidate.heartbeatRequired };
}
function status() {
  if (recovery) return { stopped: true, reconciliationOnly: true, secretCustody: connection ? 'ENCRYPTED_RECOVERED' : 'LOST_ON_RESTART',
    did: recovery.did, quotaConsumed: recovery.quotaConsumed, contract: recovery.admission?.contract ?? null,
    state: recovery.state, terminal: ['claimed', 'refunded', 'cancelled', 'expired'].includes(recovery.state?.status),
    reconciliationRequired: true, network: 'DISABLED',
    actions: recovery.actions.map(a => ({ id: a.subject.actionId, type: a.subject.type, status: a.status,
      payloadDigest: a.subject.payloadDigest, attemptCount: ['SEND_ATTEMPTED', 'AMBIGUOUS', 'OBSERVED', 'RECONCILED'].includes(a.status) ? 1 : 0 })) };
  return { stopped, ambiguous, mockEnabled, network: 'DISABLED', contract: accept?.contract ?? null,
    state: candidate ? safeState(state()) : null,
    actions: [...actions.values()].map(a => ({ id: a.id, type: a.type, status: a.status,
      payloadDigest: a.subject.payloadDigest, attemptCount: a.attemptCount })) };
}
function freshness() {
  if (!firstAccept) need(now() - observedAt <= POLICY.freshMs && observedAt <= now() + 1000,
    'STALE_OBSERVATION');
}
async function eligible(type) {
  live(); need(!ambiguous, 'AMBIGUOUS_STOP'); freshness();
  const current = now(), s = state();
  need(current + POLICY.claimMarginMs < candidate.offer.claimByMs, 'CLAIM_DEADLINE');
  need(candidate.offer.refundAfterMs - candidate.offer.claimByMs >= POLICY.refundGapMs, 'REFUND_MARGIN');
  if (type === 'ACCEPT') {
    if (connection) await connection.assertRecovery();
    need(s.status === 'proposed', 'ACCEPT_STATE');
    need(current + POLICY.acceptMarginMs < candidate.offer.expiresMs, 'ACCEPT_DEADLINE');
    need(t.applyFrame(s, accept, current).ok, 'ACCEPT_INVALID');
  } else {
    need(s.payeeDid === signer.did && s.contract === accept.contract, 'PARTY_OR_CONTRACT');
    if (type === 'INITIAL_HEARTBEAT') {
      need(candidate.heartbeatRequired, 'HEARTBEAT_NOT_REQUIRED');
      need(s.status === 'accepted', 'HEARTBEAT_STATE');
    } else {
      need(s.status === 'locked', 'LOCK_REQUIRED');
      need(s.rail === 'paper' && s.railRef === accept.contract, 'LOCK_REF');
      if (candidate.heartbeatRequired) need(['OBSERVED', 'RECONCILED'].includes(actions.get('INITIAL_HEARTBEAT')?.status), 'HEARTBEAT_REQUIRED');
      const store = new t.MemoryNoteStore(), note = t.paperNote(accept.contract);
      if (paper !== null) await store.set(note.ns, note.key, paper);
      const rail = new t.PaperRail(store, () => current);
      need(await rail.verifyLock(t.lockTerms(s), s.railRef), 'PAPER_LOCK_REQUIRED');
      if (type === 'REVEAL') need(['OBSERVED', 'RECONCILED'].includes(actions.get('DELIVERY_GCD')?.status), 'DELIVERY_NOT_OBSERVED');
    }
  }
  return s;
}
function frameFor(type, heartbeatNonce) {
  if (type === 'ACCEPT') return accept;
  if (type === 'INITIAL_HEARTBEAT') return t.makeHeartbeat({ from: signer.did, contract: accept.contract, nonce: heartbeatNonce });
  if (type === 'REVEAL') return { type: 'reveal', from: signer.did, contract: accept.contract,
    ref: state().railRef, secret: preimage };
  throw new Denied('UNSUPPORTED_ACTION');
}
function sameEnvelope(a, b) {
  return ['room', 'sender', 'nonce', 'signature', 'line'].every(k => a[k] === b[k]);
}
async function ingest(records, capturedAt, noteValue = paper) {
  need(Array.isArray(records) && records.length <= POLICY.maxRecords, 'RECORD_LIMIT');
  const current = now();
  need(Number.isSafeInteger(capturedAt) && capturedAt >= observedAt && capturedAt <= current + 1000
    && current - capturedAt <= POLICY.freshMs, 'STALE_OBSERVATION');
  const next = [...observed];
  const nextCursors = new Map(cursors), nextSeen = new Map(seenRecords);
  for (const record of records) {
    const relevant = contractRecords(t, [record], candidate.offer, accept, signer.did).length > 0;
    // Unrelated malformed rows are ignored; well-formed metadata still advances
    // the trusted room cursor, even when the row is unsigned or not a tclk frame.
    const expectedRoom = record?.room === t.OFFER_ROOM || record?.room === t.dealRoom(accept.contract);
    if (!expectedRoom || !Number.isSafeInteger(record?.seq) || !Number.isSafeInteger(record?.timestampMs)) {
      need(!relevant, 'INVALID_RECORD_METADATA'); continue;
    }
    const key = record.room + ':' + record.seq;
    const existing = nextSeen.get(key);
    if (existing) { need(existing === objectDigest(record), 'CONFLICTING_RECORD'); continue; }
    const previous = nextCursors.get(record.room) ?? (record.room === t.OFFER_ROOM ? candidate.offerRecord : null);
    need(record.seq === (previous?.seq ?? 0) + 1 && record.timestampMs >= (previous?.timestampMs ?? 0)
      && record.timestampMs <= capturedAt + 1000, 'RECORD_ORDER_OR_GAP');
    nextCursors.set(record.room, { seq: record.seq, timestampMs: record.timestampMs });
    nextSeen.set(key, objectDigest(record));
    if (nextSeen.size > 128) nextSeen.delete(nextSeen.keys().next().value);
    // Keep the approved ordinary delivery for envelope reconciliation only.
    if (relevant || (record.sender === signer.did && record.line === candidate.answer)) {
      need(!next.some(r => sameEnvelope(r, record)), 'DUPLICATE_RECORD'); next.push(structuredClone(record));
    }
  }
  need(next.length <= POLICY.maxRecords, 'RECORD_LIMIT');
  const folded = state(next);
  if (noteValue !== null) need(typeof noteValue === 'string' && noteValue.length <= 1024 && t.decodePaperRecord(noteValue) !== null, 'UNSUPPORTED_PAPER_NOTE');
  observed = next; observedAt = capturedAt; paper = noteValue; cursors = nextCursors; seenRecords = nextSeen;
  await emit({ event: 'OBSERVATION', count: observed.length, at: capturedAt, state: safeState(folded) });
}
function getAction(value) {
  fields(value, ['actionId', 'subjectDigest']);
  const action = [...actions.values()].find(a => a.id === value.actionId);
  need(action && value.subjectDigest === objectDigest(action.subject), 'APPROVAL_PAYLOAD_MISMATCH');
  return action;
}
function approved(action) {
  need(action.approval && action.approval.subjectDigest === objectDigest(action.subject), 'APPROVAL_REQUIRED');
  need(now() < action.approval.expiresAt, 'APPROVAL_EXPIRED');
}
async function worker(method, value) {
  if (method === 'status') { fields(value, []); return status(); }
  need(!recovery, 'RECONCILIATION_ONLY');
  if (method === 'prepare') {
    need(TYPES.includes(value?.type), 'UNSUPPORTED_ACTION');
    fields(value, ['type', 'did', 'room', 'offer', 'contract', 'ref', 'sourceDigest', ...(value.type === 'DELIVERY_GCD' ? ['answer'] : [])]);
    live(); const type = value.type;
    need(!connection?.real || type === 'ACCEPT', 'FIRST_ACCEPT_ONLY');
    need(value.did === signer.did, 'WRONG_DID');
    need(value.offer === candidate.offer.id, 'WRONG_OFFER');
    need(value.contract === accept.contract, 'SECOND_CONTRACT');
    need(value.sourceDigest === candidate.sourceDigest, 'TASK_BINDING');
    const room = type === 'ACCEPT' ? t.OFFER_ROOM : t.dealRoom(accept.contract);
    need(value.room === room, 'WRONG_ROOM');
    const ref = type === 'ACCEPT' ? candidate.offer.id : type === 'REVEAL' ? accept.contract : null;
    need(value.ref === ref, 'WRONG_REF');
    if (type === 'DELIVERY_GCD') need(value.answer === candidate.answer, 'DELIVERY_MISMATCH');
    const prior = actions.get(type);
    need(!prior, prior?.requestDigest === objectDigest(value) ? 'DUPLICATE_ACTION' : 'CONFLICTING_ACTION');
    await eligible(type);
    const heartbeatNonce = type === 'INITIAL_HEARTBEAT' ? randomBytes(8).toString('hex') : null;
    const line = type === 'DELIVERY_GCD' ? candidate.answer : t.encodeFrame(frameFor(type, heartbeatNonce));
    need(signing.sweep(line) === line, 'SWEEP_CHANGED');
    const nonce = connection ? await connection.reserve(room, objectDigest(value), now()) : testNonce(room, nonceByRoom, now(), init.mode);
    const id = firstAccept?.packet.subject.actionId ?? randomUUID();
    const subject = { session, actionId: id, type, did: signer.did, room, offer: candidate.offer.id,
      contract: accept.contract, ref, sourceDigest: candidate.sourceDigest, nonce,
      payloadDigest: digest(signing.canonicalMessage(room, nonce, line)) };
    if (firstAccept) need(objectDigest(subject) === objectDigest(firstAccept.packet.subject)
      && line === firstAccept.packet.publicLine, 'FIRST_ACCEPT_BINDING_REQUIRED');
    const action = { id, type, subject, line, status: 'PREPARED', requestDigest: objectDigest(value), approval: null, attemptCount: 0 };
    await emit({ event: 'PREPARED', subject, subjectDigest: objectDigest(subject) }); actions.set(type, action);
    return { subject, subjectDigest: objectDigest(subject), preview: type === 'REVEAL' ? 'REVEAL: secret retained by signer; statement=' + accept.statement : line };
  }
  if (method === 'sign') {
    const action = getAction(value); need(action.status === 'PREPARED', 'DUPLICATE_SIGN');
    approved(action); await eligible(action.type);
    if (connection?.real) action.status = 'SIGNING'; // consume even if crypto/persistence fails
    const signature = signer.sign(signing.canonicalMessage(action.subject.room, action.subject.nonce, action.line));
    const signatureBytes = Buffer.from(signature, 'base64url');
    confidential.push(signature, signatureBytes.toString('hex'), signatureBytes.toString('base64'));
    const record = { room: action.subject.room, seq: 0, timestampMs: now(), sender: signer.did,
      nonce: action.subject.nonce, signature, line: action.line };
    verifyRecord(t, record);
    await emit({ event: 'SIGNED', actionId: action.id, payloadDigest: action.subject.payloadDigest, envelopeDigest: envelopeDigest(record) });
    action.record = record; action.status = 'SIGNED';
    // Opaque handle only: especially no REVEAL secret in Worker IPC or routine logs.
    return { actionId: action.id, status: action.status, payloadDigest: action.subject.payloadDigest, verified: true };
  }
  if (method === 'prepareWrite') {
    const action = getAction(value); need(action.status === 'SIGNED', 'WRITE_STATE');
    approved(action); await eligible(action.type);
    need(digest(signing.canonicalMessage(action.record.room, action.record.nonce, action.record.line)) === action.subject.payloadDigest, 'PAYLOAD_CHANGED');
    verifyRecord(t, action.record); await emit({ event: 'WRITE_PREPARED', actionId: action.id }); action.status = 'WRITE_PREPARED';
    if (connection) await connection.handoff(action.record, { record: action.record,
      packet: canonicalApproval(action, candidate, signing), approval: action.approval, offerRecord: candidate.offerRecord });
    return { actionId: action.id, effect: connection?.real ? 'REAL_ACCEPT_POLICY_VALIDATION_ONLY' : connection ? 'OFFLINE_TRANSPORT_VALIDATION_ONLY' : 'IN_MEMORY_APPEND_ONLY', publicNetwork: false, payloadDigest: action.subject.payloadDigest };
  }
  if (method === 'attempt') {
    need(!connection, 'EXTERNAL_WRITE_DISABLED');
    const action = getAction(value); need(mockEnabled, 'NETWORK_DISABLED');
    need(action.status === 'WRITE_PREPARED' && action.attemptCount === 0, 'NO_RETRY');
    approved(action); await eligible(action.type);
    await emit({ event: 'SEND_ATTEMPTED', actionId: action.id, payloadDigest: action.subject.payloadDigest });
    action.attemptCount = 1; action.status = 'SEND_ATTEMPTED';
    try {
      if (mockMode === 'crash-after-send') process.kill(process.pid, 'SIGKILL');
      if (mockMode === 'transport-timeout') { await new Promise(resolve => setTimeout(resolve, 250)); throw new Denied('TRANSPORT_TIMEOUT'); }
      if (mockMode === 'rpc-delay') { await new Promise(resolve => setTimeout(resolve, 5500)); throw new Denied('MOCK_RESPONSE_LOST'); }
      if (mockMode === 'drop-before') throw new Denied('MOCK_RESPONSE_LOST');
      const previous = [...(action.record.room === t.OFFER_ROOM ? [candidate.offerRecord] : []),
        ...observed.filter(r => r.room === action.record.room), ...mockRecords.filter(r => r.room === action.record.room),
        ...(cursors.has(action.record.room) ? [cursors.get(action.record.room)] : [])];
      const seq = Math.max(0, ...previous.map(r => r.seq)) + 1;
      const record = { ...action.record, seq, timestampMs: now() }; mockRecords.push(record);
      if (mockMode !== 'ack') throw new Denied('MOCK_RESPONSE_LOST');
      await ingest([record], now()); action.status = 'OBSERVED';
      await emit({ event: 'OBSERVED', actionId: action.id }); return { status: action.status };
    } catch {
      ambiguous = action.id; action.status = 'AMBIGUOUS'; await emit({ event: 'AMBIGUOUS', actionId: action.id });
      return { status: 'AMBIGUOUS', next: 'READ_ONLY_RECONCILIATION', retry: false };
    }
  }
  if (method === 'reconcile') {
    need(!connection?.real, 'GATE_CAPABILITY_REQUIRED');
    const action = getAction(value);
    need(action.status === 'AMBIGUOUS' || action.status === 'SEND_ATTEMPTED', 'RECONCILE_STATE');
    const matches = [...observed, ...mockRecords].filter(r => sameEnvelope(r, action.record));
    const unique = [...new Map(matches.map(r => [r.room + ':' + r.seq, r])).values()];
    if (unique.length !== 1) return { status: 'AMBIGUOUS', next: 'HUMAN_STOP', retry: false };
    verifyRecord(t, unique[0]); await ingest(unique, now());
    action.status = 'RECONCILED'; ambiguous = null; await emit({ event: 'RECONCILED', actionId: action.id });
    return { status: 'RECONCILED', state: safeState(state()), retry: false };
  }
  throw new Denied('UNSUPPORTED_OPERATION');
}
async function gate(method, value) {
  if (connection && ['mock', 'advanceTestClock', 'forgetMockRecords'].includes(method)) throw new Denied('TEST_CONTROLS_DISABLED');
  if (connection && method === 'nonceObservation') return connection.observeNonce(value);
  if (method === 'sendAccept') {
    need(connection?.real && connection.externalWriteEnabled, 'EXTERNAL_WRITE_DISABLED');
    const action = getAction(value);
    need(action.type === 'ACCEPT' && action.status === 'WRITE_PREPARED' && action.attemptCount === 0, 'NO_RETRY');
    approved(action); await eligible('ACCEPT');
    // Burn the in-process capability first. Both durable authorities precede the IPC send.
    action.attemptCount = 1; action.status = 'SEND_ATTEMPTED';
    try {
      await connection.markSendAttempted(action.record);
      await emit({ event: 'SEND_ATTEMPTED', actionId: action.id, payloadDigest: action.subject.payloadDigest });
    } catch { stopped = true; throw new Denied('DURABILITY_STOP'); }
    try {
      await connection.sendAccept({ record: action.record, packet: canonicalApproval(action, candidate, signing),
        approval: action.approval, offerRecord: candidate.offerRecord });
    } catch { /* IPC failure is also ambiguous; never call the transport again. */ }
    stopped = true; ambiguous = action.id; action.status = 'AMBIGUOUS';
    await connection.markAmbiguous(action.record);
    await emit({ event: 'AMBIGUOUS', actionId: action.id });
    return { status: 'AMBIGUOUS', next: 'READ_ONLY_RECONCILIATION', retry: false,
      ...(firstAccept ? { stopped: true, envelopeDigest: envelopeDigest(action.record) } : {}) };
  }
  if (method === 'reconcileFirstAccept') {
    fields(value, ['record', 'capturedAt', 'generation']);
    need(firstAccept && stopped && ambiguous && !recovery, 'RECONCILE_STATE');
    need(value.generation === 1 && Number.isSafeInteger(value.capturedAt)
      && value.capturedAt <= Date.now() && Date.now() - value.capturedAt <= POLICY.freshMs, 'STALE_OBSERVATION');
    const action = [...actions.values()].find(a => a.id === ambiguous);
    verifyRecord(t, value.record);
    need(action?.type === 'ACCEPT' && sameEnvelope(value.record, action.record)
      && value.record.seq > candidate.offerRecord.seq
      && value.record.timestampMs >= candidate.offerRecord.timestampMs
      && value.record.timestampMs <= value.capturedAt + 1000, 'EXACT_RECORD_REQUIRED');
    await connection.reconcileNonce(action.subject);
    await emit({ event: 'RECONCILED', actionId: action.id });
    action.status = 'RECONCILED'; ambiguous = null;
    return { status: 'RECONCILED_PRESENT', stopped: true, retry: false };
  }
  if (method === 'reconcileRestart') {
    fields(value, ['generation', 'capturedAt', 'offerRecord', 'records']);
    need(recovery?.admission, 'RECONCILE_STATE');
    need(value.generation === 1 && Number.isSafeInteger(value.capturedAt)
      && Math.abs(Date.now() - value.capturedAt) <= POLICY.freshMs, 'STALE_OBSERVATION');
    need(Array.isArray(value.records) && value.records.length <= POLICY.maxRecords, 'RECORD_LIMIT');
    verifyRecord(t, value.offerRecord);
    need(envelopeDigest(value.offerRecord) === recovery.admission.offerDigest, 'RECOVERY_OFFER_MISMATCH');
    const offer = t.decodeFrame(value.offerRecord.line), recoveredAccept = { contract: recovery.admission.contract };
    const relevant = contractRecords(t, value.records, offer, recoveredAccept, recovery.did);
    const recoveryCursors = new Map([[t.OFFER_ROOM, value.offerRecord]]), recoverySeen = new Set();
    for (const record of value.records) {
      if (![t.OFFER_ROOM, t.dealRoom(recoveredAccept.contract)].includes(record?.room)) continue;
      need(Number.isSafeInteger(record.seq) && Number.isSafeInteger(record.timestampMs), 'INVALID_RECORD_METADATA');
      const key = record.room + ':' + record.seq;
      need(!recoverySeen.has(key), 'CONFLICTING_RECORD'); recoverySeen.add(key);
      const previous = recoveryCursors.get(record.room);
      need(record.seq === (previous?.seq ?? 0) + 1 && record.timestampMs >= (previous?.timestampMs ?? 0)
        && record.timestampMs <= value.capturedAt + 1000, 'RECORD_ORDER_OR_GAP');
      recoveryCursors.set(record.room, record);
    }
    const handshake = t.findContractHandshake([value.offerRecord, ...relevant], recoveredAccept.contract);
    need(handshake !== null, 'RECOVERY_HANDSHAKE_REQUIRED');
    const folded = t.foldTranscript([value.offerRecord, ...relevant]);
    need(folded.steps.every(step => step.ok) && folded.state.contract === recoveredAccept.contract
      && folded.state.payeeDid === recovery.did, 'TRANSCRIPT_REJECTED');
    const priorState = recovery.state, nextState = safeState(folded.state);
    const terminal = state => ['claimed', 'refunded', 'cancelled', 'expired'].includes(state?.status);
    if (terminal(priorState)) need(objectDigest(priorState) === objectDigest(nextState), 'RECOVERY_STATE_REGRESSION');
    if (priorState?.status === 'locked') need(nextState.status !== 'accepted' && nextState.rail === priorState.rail
      && nextState.railRef === priorState.railRef, 'RECOVERY_STATE_REGRESSION');
    const unresolved = recovery.actions.filter(a => ['SEND_ATTEMPTED', 'AMBIGUOUS'].includes(a.status));
    const actionIds = [];
    for (const action of unresolved) {
      const matches = value.records.filter(record => {
        if (record?.sender !== recovery.did || record?.room !== action.subject.room || record?.nonce !== action.subject.nonce) return false;
        verifyRecord(t, record);
        return envelopeDigest(record) === action.envelopeDigest;
      });
      if (matches.length === 1) actionIds.push(action.subject.actionId);
    }
    // Readback can resolve evidence, never authorize a retry or replenish quota.
    if (connection?.real) for (const action of unresolved)
      if (actionIds.includes(action.subject.actionId)) await connection.reconcileNonce(action.subject);
    await emit({ event: 'RECOVERY_RECONCILED', actionIds, state: safeState(folded.state) });
    for (const action of recovery.actions) if (actionIds.includes(action.subject.actionId)) action.status = 'RECONCILED';
    recovery.state = safeState(folded.state);
    return { status: actionIds.length === unresolved.length ? 'RECONCILED' : 'AMBIGUOUS',
      state: recovery.state, retry: false, reconciliationOnly: true };
  }
  if (method === 'admit') {
    need(!recovery, 'RECONCILIATION_ONLY');
    fields(value, ['mode', 'expectedDid', 'offerRecord', 'sourceDigest', 'family', 'answer', 'noValue', 'heartbeatRequired',
      ...(firstAccept ? ['finalSnapshot'] : [])]);
    need(!stopped && candidate === null, 'SECOND_CONTRACT'); need(value.mode === (connection?.real ? 'REAL_ACCEPT_PREPARATION' : 'TEST_EPHEMERAL'), connection?.real ? 'WRONG_ADMISSION_MODE' : 'TEST_ONLY');
    need(value.expectedDid === signer.did, 'WRONG_DID'); verifyRecord(t, value.offerRecord);
    need(value.offerRecord.room === t.OFFER_ROOM, 'WRONG_ROOM');
    let finalValidation = null;
    if (firstAccept) {
      need(objectDigest(value.offerRecord) === objectDigest(firstAccept.offerRecord)
        && value.sourceDigest === firstAccept.packet.subject.sourceDigest
        && value.family === 'paper.accept-only' && value.answer === '' && value.heartbeatRequired === false,
        'FIRST_ACCEPT_BINDING_REQUIRED');
      try { finalValidation = await connection.finalRevalidateFirstAccept(value.finalSnapshot); }
      catch { return { status: 'RETURN_C1', admitted: false }; }
    }
    const offer = t.decodeFrame(value.offerRecord.line);
    need(offer.type === 'offer' && offer.from === value.offerRecord.sender && offer.role === 'payer' && offer.from !== signer.did, 'WRONG_PARTY');
    need(offer.lock === 'hash' && offer.asset === 'PAPER' && JSON.stringify(offer.rails) === '["paper"]'
      && value.noValue === (connection?.real ? 'EXPLICIT_PAPER_NO_VALUE' : 'EXPLICIT_TEST_NO_VALUE'), 'NO_VALUE_PAPER_ONLY');
    need(firstAccept || value.family === 'math.gcd_lcm' && typeof value.answer === 'string'
      && /^gcd=(0|[1-9][0-9]{0,99}) lcm=(0|[1-9][0-9]{0,199})$/.test(value.answer), 'UNSUPPORTED_DELIVERY');
    need(/^[0-9a-f]{64}$/.test(value.sourceDigest) && (firstAccept
      || offer.job?.context === 'TEST GCD bundle sha256=' + value.sourceDigest), 'TASK_BINDING');
    need(typeof value.heartbeatRequired === 'boolean', 'INVALID_REQUEST');
    need(!connection?.real || value.heartbeatRequired === false && offer.paymentKey === undefined
      && t.encodeFrame(offer) === value.offerRecord.line, 'FIRST_ACCEPT_ONLY');
    const current = now();
    need(value.offerRecord.timestampMs <= current + 1000 && (firstAccept || current - value.offerRecord.timestampMs <= POLICY.freshMs), 'STALE_OBSERVATION');
    need(current + POLICY.acceptMarginMs < offer.expiresMs && current + POLICY.claimMarginMs < offer.claimByMs
      && offer.claimByMs + POLICY.refundGapMs <= offer.refundAfterMs, 'INVALID_DEADLINE');
    const hash = firstAccept ? t.hashLockFromPreimage(connection.firstAcceptPreimage) : t.generateHashLock(); preimage = hash.preimage;
    confidential.push(preimage, preimage.slice(2));
    accept = t.makeAccept(offer, { from: signer.did, statement: hash.hash,
      ...(firstAccept ? { nonce: t.decodeFrame(firstAccept.packet.publicLine).nonce } : {}) });
    if (firstAccept) need(t.encodeFrame(accept) === firstAccept.packet.publicLine, 'FIRST_ACCEPT_BINDING_REQUIRED');
    const { finalSnapshot: _finalSnapshot, ...admission } = value;
    candidate = { ...structuredClone(admission), offer, statement: accept.statement,
      sessionDeadline: current + POLICY.sessionMs };
    if (connection) await connection.persistPreimage({ preimage, contract: accept.contract,
      context: { candidate, accept }, ...(firstAccept ? { observation: finalValidation.observation } : {}) });
    connection?.firstAcceptPreimage?.fill(0);
    observedAt = current; await emit({ event: 'ADMITTED', offer: offer.id, contract: accept.contract, sourceDigest: value.sourceDigest,
      payerDid: offer.from, offerDigest: envelopeDigest(value.offerRecord) });
    return meta();
  }
  if (method === 'approvalPreview') {
    fields(value, ['actionId']); live();
    const action = [...actions.values()].find(a => a.id === value.actionId);
    need(action, 'UNKNOWN_ACTION'); return canonicalApproval(action, candidate, signing);
  }
  if (method === 'approve') {
    fields(value, ['actionId', 'subjectDigest', 'approvalDigest', 'expiresAt']); live(); need(!ambiguous, 'AMBIGUOUS_STOP');
    const action = getAction({ actionId: value.actionId, subjectDigest: value.subjectDigest });
    need(canonicalApproval(action, candidate, signing).approvalDigest === value.approvalDigest, 'TRUSTED_APPROVAL_DIGEST_MISMATCH');
    need(action.status === 'PREPARED' && !action.approval, 'APPROVAL_USED');
    const exactFirstAcceptExpiry = firstAccept
      ? Math.min(candidate.offer.expiresMs - POLICY.acceptMarginMs,
        candidate.offer.claimByMs - POLICY.claimMarginMs)
      : null;
    need(Number.isSafeInteger(value.expiresAt) && value.expiresAt > now()
      && (firstAccept ? value.expiresAt === exactFirstAcceptExpiry : value.expiresAt <= now() + 120000),
    'INVALID_APPROVAL_EXPIRY');
    action.approval = structuredClone(value); await emit({ event: 'APPROVED', ...value }); return { approved: true };
  }
  if (method === 'observe') {
    fields(value, ['generation', 'capturedAt', 'records', 'paperNote']);
    need(!recovery && candidate !== null, 'RECONCILE_STATE'); // read-only observations remain available after STOP
    need(value.generation === 1, 'ROOM_EPOCH'); await ingest(value.records, value.capturedAt, value.paperNote);
    if (connection?.real && ambiguous) {
      const action = [...actions.values()].find(a => a.id === ambiguous);
      const matches = observed.filter(r => sameEnvelope(r, action.record));
      if (matches.length === 1) {
        await connection.reconcileNonce(action.subject);
        await emit({ event: 'RECONCILED', actionId: action.id });
        action.status = 'RECONCILED'; ambiguous = null;
      }
    }
    return { state: safeState(state()) };
  }
  if (method === 'mock') {
    need(init.mode === 'TEST_EPHEMERAL' && !recovery, 'TEST_ONLY');
    fields(value, ['enabled', 'mode']); need(typeof value.enabled === 'boolean', 'INVALID_REQUEST');
    need(['ack', 'drop-before', 'drop-after', 'transport-timeout', 'rpc-delay', 'crash-after-send'].includes(value.mode), 'INVALID_MOCK_MODE');
    mockEnabled = value.enabled; mockMode = value.mode; return { mockEnabled, mode: mockMode };
  }
  if (method === 'advanceTestClock') {
    need(init.mode === 'TEST_EPHEMERAL' && !recovery, 'TEST_ONLY');
    fields(value, ['milliseconds']); need(Number.isSafeInteger(value.milliseconds) && value.milliseconds >= 0 && value.milliseconds <= 3600000, 'INVALID_CLOCK');
    offset += value.milliseconds; return { now: now() };
  }
  if (method === 'forgetMockRecords') { need(init.mode === 'TEST_EPHEMERAL' && !recovery, 'TEST_ONLY'); fields(value, []); mockRecords = []; return { retained: 0 }; }
  if (method === 'stop') { fields(value, []); stopped = true; await emit({ event: 'STOPPED' }); return { stopped }; }
  if (method === 'close') {
    fields(value, []); stopped = true; await emit({ event: 'CLOSED' });
    return { closed: true }; // Supervisor closes control fd only after this ACK.
  }
  throw new Denied('UNSUPPORTED_GATE_OPERATION');
}
let queue = Promise.resolve();
function dispatch(channel, message) {
  queue = queue.then(async () => {
    const id = Number.isSafeInteger(message?.id) ? message.id : 0;
    try {
      fields(message, ['id', 'method', 'value']); need(typeof message.method === 'string', 'INVALID_REQUEST');
      need(JSON.stringify(message).length <= 65536, 'REQUEST_LIMIT');
      const result = await (channel === 'gate' ? gate : worker)(message.method, message.value);
      const response = { channel, id, ok: true, result }; protectedText(response); process.send(response);
    } catch (error) {
      // Never reflect caller input, library messages, frame bytes or a stack trace.
      const code = error instanceof Denied ? error.code : 'INVALID_REQUEST';
      process.send({ channel, id, ok: false, code });
    }
  });
}
if (!connection) process.on('message', message => {
  if (message?.rpcTimeoutStop === true) { stopped = true; return; }
  if (!Object.hasOwn(message ?? {}, 'auditAck')) dispatch('worker', message);
});
// Read an already-open fd, without socket type discovery (which restricted
// environments may deny). Supervisor must close its endpoint to release read().
if (connection) {
  connection.listen(dispatch);
} else {
const control = createInterface({ input: createReadStream(null, { fd: 4, autoClose: true }) });
control.on('line', line => {
  if (line.length > 65536) { stopped = true; return; }
  try { dispatch('gate', JSON.parse(line)); } catch { stopped = true; }
});
control.on('close', () => {
  stopped = true;
  queue.finally(() => { seed?.fill(0); preimage = null; signer = null; process.exit(0); });
});
process.on('disconnect', () => { seed?.fill(0); process.exit(0); });
}
process.send({ boot: true, did: signer.did, mode: 'TEST_EPHEMERAL', session, reconciliationOnly: !!recovery,
  permissions: { fsWrite: process.permission.has('fs.write'), childProcess: process.permission.has('child'),
    workerThreads: process.permission.has('worker'), nativeAddons: process.permission.has('addons') } });
