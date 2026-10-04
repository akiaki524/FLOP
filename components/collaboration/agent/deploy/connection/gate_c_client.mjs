// Fixed Stage C adapter. No credential input, signer creation or network client.
// Only the Human root coordinator invokes execute after the exact terminal challenge.
import { pathToFileURL } from 'node:url';
import { official, objectDigest, requireThat as need, fields, POLICY, Denied } from '../../src/collaboration_agent/pilot_protocol.mjs';
import { callSocket } from '../../src/collaboration_agent/connection_runtime.mjs';
import { candidates, freeze, validateFirstAccept, revalidateFirstAccept, matchFirstAccept } from '../../src/collaboration_agent/first_accept_packet.mjs';

const request = (channel, method, value = {}) => callSocket('/run/collab-connection/' + channel + '.sock', { method, value });

export async function arm(document) {
  const { t, signing } = await official();
  await validateFirstAccept(document, { t, signing, fresh: false });
  const boundary = await request('gate', 'boundaryStatus');
  need(boundary.firstAcceptDigest === document.digest && boundary.externalWriteEnabled === true
    && boundary.did === document.packet.subject.did && boundary.inetDenied && boundary.childDenied,
    'FIRST_ACCEPT_BINDING_REQUIRED');
  const status = await request('worker', 'status');
  need(!status.stopped && !status.reconciliationOnly && status.contract === null && status.actions.length === 0,
    'FIRST_ACCEPT_ALREADY_STARTED');
  return { armed: true, packetDigest: document.digest };
}

export async function execute(document, confirmation, finalSnapshot) {
  await arm(document);
  need(confirmation === 'SEND ACCEPT ' + document.digest, 'HUMAN_APPROVAL_REJECTED');
  const { t, signing } = await official();
  let validated;
  try {
    validated = await revalidateFirstAccept(document, finalSnapshot, { t, signing });
  } catch { throw new Denied('RETURN_C1'); }
  const s = document.packet.subject;
  try {
    const admitted = await request('gate', 'admit', { mode: 'REAL_ACCEPT_PREPARATION', expectedDid: s.did,
      offerRecord: document.offerRecord, sourceDigest: s.sourceDigest, family: 'paper.accept-only', answer: '',
      noValue: 'EXPLICIT_PAPER_NO_VALUE', heartbeatRequired: false,
      finalSnapshot: validated.admissionSnapshot });
    if (admitted.status === 'RETURN_C1' && admitted.admitted === false)
      return { status: 'RETURN_C1', retry: false };
    need(admitted.contract === s.contract, 'FIRST_ACCEPT_BINDING_REQUIRED');
    const prepared = await request('worker', 'prepare', { type: 'ACCEPT', did: s.did, room: s.room,
      offer: s.offer, contract: s.contract, ref: s.ref, sourceDigest: s.sourceDigest });
    need(objectDigest(prepared.subject) === objectDigest(s), 'FIRST_ACCEPT_BINDING_REQUIRED');
    const packet = await request('gate', 'approvalPreview', { actionId: s.actionId });
    need(objectDigest(packet) === objectDigest(document.packet), 'FIRST_ACCEPT_BINDING_REQUIRED');
    await request('gate', 'approve', { actionId: s.actionId, subjectDigest: packet.subjectDigest,
      approvalDigest: packet.approvalDigest, expiresAt: Math.min(validated.offer.expiresMs - POLICY.acceptMarginMs,
        validated.offer.claimByMs - POLICY.claimMarginMs) });
    const token = { actionId: s.actionId, subjectDigest: packet.subjectDigest };
    await request('worker', 'sign', token);
    await request('worker', 'prepareWrite', token);
    // Exactly one call. A lost response is unresolved, never retried.
    const sent = await request('gate', 'sendAccept', token);
    need(sent.status === 'AMBIGUOUS' && sent.stopped === true && sent.retry === false
      && typeof sent.envelopeDigest === 'string' && /^[0-9a-f]{64}$/.test(sent.envelopeDigest), 'SEND_RESULT_UNCERTAIN');
    return { ...sent, packetDigest: document.digest };
  } catch {
    // Once admission is attempted its durable outcome may be unknown. Never
    // suggest creating another packet or resetting custody, even on expiry.
    throw new Error('FIRST_ACCEPT_DURABLE_STATE_UNCERTAIN');
  }
}

export async function match(document, snapshot, envelopeDigest) {
  const { t, signing } = await official();
  const observed = await matchFirstAccept(document, snapshot, envelopeDigest, { t, signing });
  if (observed.status !== 'RECONCILED_PRESENT') return { status: 'AMBIGUOUS', stopped: true, retry: false };
  const result = await request('gate', 'reconcileFirstAccept', { record: observed.record,
    capturedAt: snapshot.observation.verifiedAtMs, generation: snapshot.observation.source.generation });
  need(result.status === 'RECONCILED_PRESENT' && result.stopped === true && result.retry === false,
    'RECONCILIATION_UNCONFIRMED');
  return { ...result, packetDigest: document.digest };
}

async function main() {
  need(process.argv.length === 3 && ['candidates', 'freeze', 'validate', 'arm', 'revalidate', 'execute', 'match'].includes(process.argv[2]), 'ARGUMENTS_REFUSED');
  need(!process.stdin.isTTY && !process.stdout.isTTY, 'COORDINATOR_PIPE_REQUIRED');
  const chunks = []; let size = 0;
  for await (const chunk of process.stdin) { size += chunk.length; need(size <= 16 * 1024 * 1024, 'INPUT_TOO_LARGE'); chunks.push(chunk); }
  const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const { t, signing } = await official();
  let result;
  switch (process.argv[2]) {
    case 'candidates': result = await candidates(value, { t, signing }); break;
    case 'freeze': fields(value, ['snapshot', 'selectedOfferId']); result = await freeze(value.snapshot, value.selectedOfferId, { t, signing }); break;
    case 'validate': fields(value, ['document']); await validateFirstAccept(value.document, { t, signing, fresh: false }); result = { document: value.document }; break;
    case 'arm': fields(value, ['document']); result = await arm(value.document); break;
    case 'revalidate': fields(value, ['document', 'snapshot']); await revalidateFirstAccept(value.document, value.snapshot, { t, signing }); result = { validated: true }; break;
    case 'execute': fields(value, ['document', 'confirmation', 'finalSnapshot']); result = await execute(value.document, value.confirmation, value.finalSnapshot); break;
    case 'match': fields(value, ['document', 'snapshot', 'envelopeDigest']); result = await match(value.document, value.snapshot, value.envelopeDigest); break;
  }
  process.stdout.write(JSON.stringify(result) + '\n');
  if (result?.status === 'RETURN_C1') process.exitCode = 1;
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(error => {
    const stale = process.argv[2] === 'revalidate' || error?.code === 'RETURN_C1';
    process.stdout.write(JSON.stringify({ status: stale ? 'RETURN_C1' : 'HUMAN_STOP', retry: false }) + '\n');
    process.exitCode = 1;
  });
}
