// Human session executable. The root-owned socket ACL identifies the approved OS user.
import { createInterface } from 'node:readline/promises';
import { readFileSync } from 'node:fs';
import { callSocket } from './connection_runtime.mjs';
import { PROJECT_DID, terminalPacket, approveAtTerminal } from './connection_approval.mjs';
import { official, requireThat as need } from './pilot_protocol.mjs';
try {
  const config = JSON.parse(readFileSync('/etc/collab-connection/config.json', 'utf8'));
  const real = config.mode === 'REAL_ACCEPT_PREPARATION';
  need(process.getuid() === config.humanUid && (real || config.mode === 'DUMMY_OFFLINE'), 'HUMAN_SESSION_REQUIRED');
  const send = process.argv[2] === '--send-accept';
  need(send ? real && process.argv.length === 4 : process.argv.length === 3, 'ACTION_ID_REQUIRED');
  const actionId = process.argv[send ? 3 : 2];
  const { signing } = await official();
  const request = (method, value) => callSocket('/run/collab-connection/human.sock', { method, value });
  const approval = { preview: actionId => request('approvalPreview', { actionId }),
    approve: value => request('approve', value) };
  const packet = await approval.preview(actionId);
  // TEST identity comes from protected Signer -> Approval IPC; never from a Worker argument.
  const expectedDid = real ? PROJECT_DID : packet.subject.did;
  need(!real || packet.subject.type === 'ACCEPT', 'FIRST_ACCEPT_ONLY');
  if (send) {
    need(process.stdin.isTTY && process.stdout.isTTY, 'HUMAN_TERMINAL_REQUIRED');
    const view = terminalPacket(packet, signing, expectedDid);
    process.stdout.write(view.display + '\n');
    const terminal = createInterface({ input: process.stdin, output: process.stdout });
    const phrase = 'SEND ACCEPT ' + packet.approvalDigest.slice(0, 16);
    let answer;
    try { answer = await terminal.question('1回だけ送信を試みる場合は ' + phrase + ' と入力: '); }
    finally { terminal.close(); }
    need(answer === phrase, 'HUMAN_APPROVAL_REJECTED');
    await request('sendAccept', { actionId, subjectDigest: packet.subjectDigest });
    process.stdout.write('STOP: 公開記録との照合が必要です。再送しないでください。\n');
  } else {
    await approveAtTerminal({ approval, actionId, signing, expectedDid });
    process.stdout.write('承認を記録しました。この承認は表示したActionだけに有効です。\n');
  }
} catch {
  process.stderr.write('APPROVAL_DENIED\n'); process.exitCode = 1;
}
