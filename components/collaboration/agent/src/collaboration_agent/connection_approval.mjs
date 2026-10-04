// Trusted terminal policy. Caller must supply the protected Approval IPC, never a Worker preview.
import { createInterface } from 'node:readline/promises';
import { verifyApprovalView } from './pilot_approval.mjs';
import { requireThat as need, fields } from './pilot_protocol.mjs';

export const PROJECT_DID = 'did:key:z6MkjQVeF7TCAC6H6LKQnYFannWhsfWzzHhDDr3UhbZgHSTL';
export function terminalPacket(packet, signing, expectedDid = PROJECT_DID) {
  const canonical = verifyApprovalView(packet, signing);
  need(canonical.subject.did === expectedDid, 'WRONG_DID');
  need(['ACCEPT', 'DELIVERY_GCD', 'REVEAL', 'INITIAL_HEARTBEAT'].includes(canonical.subject.type), 'UNSUPPORTED_ACTION');
  need(canonical.rail === 'paper', 'PAPER_ONLY');
  const s = canonical.subject;
  // JSON escaping makes terminal control bytes visible, not executable terminal instructions.
  const display = JSON.stringify({ action: s.type, did: s.did, room: s.room,
    offer: s.offer, contract: s.contract, rail: canonical.rail,
    publicPayload: canonical.publicLine, commitment: canonical.statement, payloadDigest: s.payloadDigest,
    deadlines: canonical.deadlines, disclosure: canonical.disclosure,
    warning: s.type === 'REVEAL' ? 'preimageを外部へ公開するActionです' : '表示したpayloadを外部へ公開するActionです',
    approvalDigest: canonical.approvalDigest }, null, 2);
  return { canonical, display, challenge: s.type + ' ' + canonical.approvalDigest.slice(0, 16) };
}

export function validateConnectionApproval(value, packet, signing) {
  fields(value, ['actionId', 'approvalDigest', 'confirmation', 'expiresAt']);
  const view = terminalPacket(packet, signing, packet.subject.did);
  need(value.actionId === packet.subject.actionId && value.approvalDigest === packet.approvalDigest, 'APPROVAL_PAYLOAD_MISMATCH');
  need(value.confirmation === view.challenge, 'HUMAN_APPROVAL_REJECTED');
  return { actionId: value.actionId, approvalDigest: value.approvalDigest,
    expiresAt: value.expiresAt, subjectDigest: packet.subjectDigest };
}

export async function approveAtTerminal({ approval, actionId, signing,
  expectedDid = PROJECT_DID, input = process.stdin, output = process.stdout }) {
  need(input.isTTY === true && output.isTTY === true, 'HUMAN_TERMINAL_REQUIRED');
  const view = terminalPacket(await approval.preview(actionId), signing, expectedDid);
  output.write(view.display + '\n');
  const terminal = createInterface({ input, output });
  let answer;
  try { answer = await terminal.question('承認する場合は ' + view.challenge + ' と入力: '); }
  finally { terminal.close(); }
  need(answer === view.challenge, 'HUMAN_APPROVAL_REJECTED');
  // Re-fetch in Approval adapter and compare the complete digest before committing approval.
  return approval.approve({ actionId, approvalDigest: view.canonical.approvalDigest,
    confirmation: answer, expiresAt: Date.now() + 60_000 });
}
