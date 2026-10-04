// Trusted TEST launcher / Approval controller. Do not execute this in a Worker.
import { fork } from 'node:child_process';
import { lstatSync, openSync, writeSync, fsyncSync, closeSync } from 'node:fs';
import { resolve, sep } from 'node:path';
import { once } from 'node:events';
import { ROOT, RUNTIME, official, requireThat as need } from './pilot_protocol.mjs';
import { PilotLedger, fsyncDirectory } from './pilot_ledger.mjs';
import { verifyApprovalView } from './pilot_approval.mjs';

export async function launchTestPilot(journalPath, options = {}) {
  const { mode = 'TEST_EPHEMERAL', recoveryDid = null, rpcTimeoutMs = 5000 } = options;
  need(Object.keys(options).every(k => ['mode', 'recoveryDid', 'rpcTimeoutMs'].includes(k)), 'INVALID_OPTIONS');
  need(mode === 'TEST_EPHEMERAL', 'REAL_MODE_DISABLED');
  need(Number.isSafeInteger(rpcTimeoutMs) && rpcTimeoutMs >= 50 && rpcTimeoutMs <= 5000, 'INVALID_TIMEOUT');
  const journal = resolve(journalPath);
  need(journal.startsWith(resolve(ROOT, '.local') + sep), 'LOCAL_JOURNAL_ONLY');
  for (const start of [resolve(journal, '..'), RUNTIME]) {
    let parent = start;
    while (parent !== ROOT) {
      need(!lstatSync(parent).isSymbolicLink(), 'LOCAL_SYMLINK'); parent = resolve(parent, '..');
    }
  }
  const { signing } = await official();
  let ledger = recoveryDid ? new PilotLedger(recoveryDid) : null;
  let journalFd;
  try { journalFd = openSync(journal, 'wx', 0o600); fsyncDirectory(resolve(journal, '..')); }
  catch { ledger?.close(); throw new Error('EXISTING_OR_UNAVAILABLE_JOURNAL'); }
  const childOptions = {
    cwd: ROOT, env: { LANG: 'C', TZ: 'UTC' },
    execArgv: ['--permission', '--disable-sigusr1', '--disallow-code-generation-from-strings',
      '--allow-fs-read=' + import.meta.dirname, '--allow-fs-read=' + RUNTIME],
    stdio: ['ignore', 'ignore', 'ignore', 'ipc', 'pipe'],
  };
  const child = fork(resolve(import.meta.dirname, 'pilot_signer.mjs'), [], childOptions);
  let next = 1, closed = false, workerChild = null;
  const trace = [], pending = new Map(), workerPending = new Map();
  const exited = once(child, 'exit');
  const boot = new Promise((resolveBoot, rejectBoot) => {
    const timer = setTimeout(() => { child.kill(); rejectBoot(new Error('BOOT_TIMEOUT')); }, 10000);
    child.on('message', message => {
      if (Number.isSafeInteger(message?.auditId)) {
        try {
          if (message.entry.event === 'IDENTITY') {
            need(ledger === null, 'IDENTITY_ALREADY_REGISTERED');
            ledger = new PilotLedger(message.entry.did, { create: true });
          }
          ledger?.append(message.entry);
          writeSync(journalFd, JSON.stringify(message.entry) + '\n'); fsyncSync(journalFd);
          child.send({ auditAck: message.auditId, ok: true });
        } catch { child.send({ auditAck: message.auditId, ok: false }); }
        return;
      }
      trace.push(message);
      if (Object.hasOwn(message, 'boot')) {
        clearTimeout(timer);
        if (message.boot) resolveBoot(message); else rejectBoot(new Error(message.code));
        return;
      }
      const key = message.channel + ':' + message.id, waiter = pending.get(key);
      if (!waiter) return;
      pending.delete(key); clearTimeout(waiter.timer);
      if (message.ok) waiter.resolve(message.result); else waiter.reject(new Error(message.code));
    });
    child.once('exit', () => { clearTimeout(timer); rejectBoot(new Error('SIGNER_EXITED')); });
  });
  child.on('exit', () => {
    closed = true; closeSync(journalFd); ledger?.close();
    if (workerChild?.connected) workerChild.disconnect();
    for (const waiter of [...pending.values(), ...workerPending.values()]) {
      clearTimeout(waiter.timer); waiter.reject(new Error('SIGNER_EXITED'));
    }
    pending.clear(); workerPending.clear();
  });
  child.send({ initialize: { mode, recovery: ledger?.value ?? null } });
  let info;
  try { info = await boot; } catch (error) { if (!closed) child.kill(); await exited; throw error; }
  const rpc = (channel, method, value = {}) => new Promise((resolveRpc, reject) => {
    if (closed) { reject(new Error('SIGNER_EXITED')); return; }
    const id = next++, message = structuredClone({ id, method, value });
    if (JSON.stringify(message).length > 65536) { reject(new Error('REQUEST_LIMIT')); return; }
    const timer = setTimeout(() => {
      pending.delete(channel + ':' + id);
      // A process timeout is not a transport result: preserve in-memory custody.
      if (child.connected) child.send({ rpcTimeoutStop: true });
      reject(new Error('RPC_AMBIGUOUS_STOP'));
    }, rpcTimeoutMs);
    pending.set(channel + ':' + id, { resolve: resolveRpc, reject, timer });
    if (channel === 'gate') child.stdio[4].write(JSON.stringify(message) + '\n');
    else child.send(message);
  });
  workerChild = fork(resolve(import.meta.dirname, 'pilot_worker.mjs'), [], {
    ...childOptions, execArgv: childOptions.execArgv.slice(0, -1), stdio: ['ignore', 'ignore', 'ignore', 'ipc'],
  });
  const workerExited = once(workerChild, 'exit');
  workerChild.on('exit', () => {
    if (!closed && child.connected) child.send({ rpcTimeoutStop: true });
    for (const waiter of workerPending.values()) waiter.reject(new Error('WORKER_EXITED'));
    workerPending.clear();
  });
  const topology = await new Promise((resolveReady, rejectReady) => {
    const timer = setTimeout(() => rejectReady(new Error('WORKER_BOOT_TIMEOUT')), 10000);
    workerChild.once('exit', () => { clearTimeout(timer); rejectReady(new Error('WORKER_EXITED')); });
    workerChild.on('message', async message => {
      if (message?.kind === 'ready') { clearTimeout(timer); resolveReady(message); return; }
      if (message?.kind === 'workerRequest') {
        let response;
        try { response = { ok: true, result: await rpc('worker', message.method, message.value) }; }
        catch (error) { response = { ok: false, code: /^[A-Z_]+$/.test(error.message) ? error.message : 'WORKER_ERROR' }; }
        if (workerChild.connected) workerChild.send({ kind: 'response', id: message.id, ...response });
      } else if (message?.kind === 'workerResponse') {
        const p = workerPending.get(message.id); if (!p) return;
        workerPending.delete(message.id);
        if (message.ok) p.resolve(message.result); else p.reject(new Error(message.code));
      }
    });
  }).catch(async error => { child.kill('SIGKILL'); await exited; await workerExited; throw error; });
  const approval = Object.freeze({
    preview: async actionId => verifyApprovalView(await rpc('gate', 'approvalPreview', { actionId }), signing),
    approve: async ({ actionId, approvalDigest, expiresAt }) => {
      const packet = await approval.preview(actionId);
      need(packet.approvalDigest === approvalDigest, 'TRUSTED_APPROVAL_DIGEST_MISMATCH');
      return rpc('gate', 'approve', { actionId, subjectDigest: packet.subjectDigest, approvalDigest, expiresAt });
    },
  });
  return Object.freeze({
    info: Object.freeze({ ...info, topology: { supervisorPid: process.pid, signerPid: child.pid,
      workerPid: topology.pid, workerParentPid: topology.parentPid, workerCanSpawn: topology.canSpawn } }),
    worker: Object.freeze({ request: (method, value = {}) => new Promise((resolveWorker, reject) => {
      if (closed || !workerChild.connected) { reject(new Error('SIGNER_EXITED')); return; }
      if (JSON.stringify({ method, value }).length > 65536) { reject(new Error('REQUEST_LIMIT')); return; }
      const id = next++; workerPending.set(id, { resolve: resolveWorker, reject });
      workerChild.send({ kind: 'request', id, method, value });
    }) }),
    gate: Object.freeze({ request: (method, value) => rpc('gate', method, value) }), approval,
    inspection: () => ({ trace: structuredClone(trace), diagnostics: '' }),
    close: async () => {
      let forced = false;
      const timer = setTimeout(() => { if (!closed) { forced = true; child.kill('SIGKILL'); } }, 7000);
      try {
        if (!closed) {
          try { await rpc('gate', 'close'); }
          catch (error) { if (error.message !== 'SIGNER_EXITED') throw error; }
          finally { child.stdio[4].destroy(); }
        }
        await exited; await workerExited;
      } finally { clearTimeout(timer); }
      return { diagnostics: '', forced };
    },
    kill: async () => { if (!closed) child.kill('SIGKILL'); await exited; await workerExited; },
  });
}
