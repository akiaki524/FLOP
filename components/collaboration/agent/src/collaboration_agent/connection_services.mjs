// Distinct Approval / Transport service users. No credential access, no generic client.
import { readFileSync, lstatSync, readdirSync } from 'node:fs';
import { serveSocket, callSocket } from './connection_runtime.mjs';
import { official, fields, requireThat as need, envelopeDigest, verifyRecord, objectDigest } from './pilot_protocol.mjs';
import { verifyApprovalView } from './pilot_approval.mjs';
import { PROJECT_DID, validateConnectionApproval } from './connection_approval.mjs';
import { validateFirstAccept } from './first_accept_packet.mjs';
import { validateAcceptHandoff, createAcceptTransport } from './real_transport.mjs';
const role = process.argv[2];
const { t, signing } = await official();
need(process.env.LISTEN_PID === String(process.pid) && process.env.LISTEN_FDS === '1', 'SOCKET_ACTIVATION_REQUIRED');
const config = JSON.parse(readFileSync('/etc/collab-connection/config.json', 'utf8'));
const real = config.mode === 'REAL_ACCEPT_PREPARATION';
need((real || config.mode === 'DUMMY_OFFLINE') && (!real || config.projectDid === PROJECT_DID
  && typeof config.externalWriteEnabled === 'boolean'), 'REAL_MODE_DISABLED');
let firstAccept = null;
if (config.firstAcceptDigest !== undefined) {
  need(real && typeof config.firstAcceptDigest === 'string', 'FIRST_ACCEPT_BINDING_REQUIRED');
  firstAccept = JSON.parse(readFileSync('/etc/collab-connection/first-accept.json', 'utf8'));
  need(firstAccept.digest === config.firstAcceptDigest, 'FIRST_ACCEPT_BINDING_REQUIRED');
  await validateFirstAccept(firstAccept, { t, signing, fresh: false });
}
if (role === 'worker') {
  serveSocket(3, request => {
    fields(request, ['method', 'value']);
    need(['status', 'prepare', 'sign', 'prepareWrite'].includes(request.method), 'WORKER_CAPABILITY_DENIED');
    return callSocket('/run/collab-connection/worker.sock', request);
  });
} else if (role === 'approval') {
  const gate = (method, value) => callSocket('/run/collab-connection/gate.sock', { method, value });
  serveSocket(3, async request => {
    fields(request, ['method', 'value']);
    if (request.method === 'approvalPreview') return verifyApprovalView(await gate(request.method, request.value), signing);
    if (request.method === 'approve') {
      fields(request.value, ['actionId', 'approvalDigest', 'confirmation', 'expiresAt']);
      const packet = verifyApprovalView(await gate('approvalPreview', { actionId: request.value.actionId }), signing);
      return gate('approve', validateConnectionApproval(request.value, packet, signing));
    }
    need(['admit', 'observe', 'nonceObservation', 'stop', 'reconcileRestart', 'boundaryStatus', ...(real ? ['sendAccept'] : [])].includes(request.method), 'UNSUPPORTED_ACTION');
    return gate(request.method, request.value);
  });
} else if (role === 'transport') {
  const transport = real ? createAcceptTransport({ t, signing,
    writeEnabled: config.externalWriteEnabled, stateDirectory: '/var/lib/collab-transport' }) : null;
  serveSocket(3, async request => {
    if (real) {
      fields(request, ['operation', 'value']);
      if (request.operation === 'emptyState') {
        // Read-only proof for reusing an unadmitted Real identity. Never returns
        // receipt contents, accepts a path, or invokes the network adapter.
        fields(request.value, []);
        const directory = '/var/lib/collab-transport';
        const state = lstatSync(directory);
        need(state.isDirectory() && !state.isSymbolicLink() && (state.mode & 0o077) === 0,
          'UNSAFE_STATE_DIR');
        return { empty: readdirSync(directory).length === 0 };
      }
      need(['validateAccept', 'sendAccept'].includes(request.operation), 'FIRST_ACCEPT_ONLY');
      // Refuse the write operation from root-owned policy before parsing an
      // approval packet. This gives a write-off host rehearsal a safe request
      // that cannot reach validation, state mutation, or the network client.
      if (request.operation === 'sendAccept')
        need(config.externalWriteEnabled === true, 'EXTERNAL_WRITE_DISABLED');
      if (firstAccept) {
        await validateFirstAccept(firstAccept, { t, signing, fresh: false });
        need(objectDigest(request.value?.packet) === objectDigest(firstAccept.packet)
          && objectDigest(request.value?.offerRecord) === objectDigest(firstAccept.offerRecord),
          'FIRST_ACCEPT_BINDING_REQUIRED');
      }
      validateAcceptHandoff(request.value, { t, signing });
      if (request.operation === 'validateAccept')
        return { envelopeDigest: envelopeDigest(request.value.record), externalWrites: 0 };
      return transport.send(request.value);
    }
    fields(request, ['operation', 'record']);
    need(request.operation === 'validateOffline', 'EXTERNAL_WRITE_DISABLED');
    verifyRecord(t, request.record);
    need(request.record.room === t.OFFER_ROOM || request.record.room.startsWith('tclk-'), 'WRONG_ROOM');
    // No destination parameter, network client, retry loop, disk write, or logging.
    return { envelopeDigest: envelopeDigest(request.record), externalWrites: 0 };
  });
} else throw new Error('ROLE_DENIED');
