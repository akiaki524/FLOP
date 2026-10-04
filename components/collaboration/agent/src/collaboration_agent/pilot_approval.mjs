// Approval policy code; never trust a Worker-supplied description or preview.
import { digest, objectDigest, requireThat as need, fields } from './pilot_protocol.mjs';

// Runs INSIDE the Signer's secret boundary. Full REVEAL bytes never leave it.
export function canonicalApproval(action, candidate, signing) {
  need(digest(signing.canonicalMessage(action.subject.room, action.subject.nonce, action.line))
    === action.subject.payloadDigest, 'APPROVAL_PAYLOAD_MISMATCH');
  const view = { subject: structuredClone(action.subject), subjectDigest: objectDigest(action.subject),
    publicLine: action.type === 'REVEAL' ? null : action.line,
    disclosure: action.type === 'REVEAL' ? 'PUBLISH_PREIMAGE' : 'PUBLIC_PAYLOAD',
    rail: 'paper', deadlines: { expiresMs: candidate.offer.expiresMs, claimByMs: candidate.offer.claimByMs,
      refundAfterMs: candidate.offer.refundAfterMs }, statement: candidate.statement };
  return { ...view, approvalDigest: objectDigest(view) };
}
// Runs in the trusted Approval/Supervisor process, over the direct Gate channel.
export function verifyApprovalView(packet, signing) {
  fields(packet, ['subject', 'subjectDigest', 'publicLine', 'disclosure', 'rail', 'deadlines', 'statement', 'approvalDigest']);
  const { approvalDigest, ...view } = packet;
  need(objectDigest(view) === approvalDigest && objectDigest(view.subject) === view.subjectDigest, 'TRUSTED_APPROVAL_DIGEST_MISMATCH');
  if (view.subject.type === 'REVEAL') {
    need(view.publicLine === null && view.disclosure === 'PUBLISH_PREIMAGE', 'TRUSTED_APPROVAL_DIGEST_MISMATCH');
  } else {
    need(typeof view.publicLine === 'string' && view.disclosure === 'PUBLIC_PAYLOAD'
      && digest(signing.canonicalMessage(view.subject.room, view.subject.nonce, view.publicLine)) === view.subject.payloadDigest,
    'TRUSTED_APPROVAL_DIGEST_MISMATCH');
  }
  return structuredClone(packet);
}
