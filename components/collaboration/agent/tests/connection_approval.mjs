import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import { terminalPacket, approveAtTerminal, validateConnectionApproval, PROJECT_DID } from '../src/collaboration_agent/connection_approval.mjs';
import { validateDummyCredential } from '../src/collaboration_agent/connection_runtime.mjs';
import { objectDigest, digest } from '../src/collaboration_agent/pilot_protocol.mjs';
const signing = { canonicalMessage: (room, nonce, line) => `${room}|${nonce}|${line}` };
const subject = { type: 'ACCEPT', did: PROJECT_DID, room: 'tclk-offers', nonce: '9007199254740993',
  offer: 'offer', contract: 'contract', payloadDigest: digest('tclk-offers|9007199254740993|public') };
function packet(s = subject, line = 'public') {
  const v = { subject: s, subjectDigest: objectDigest(s), publicLine: line,
    disclosure: s.type === 'REVEAL' ? 'PUBLISH_PREIMAGE' : 'PUBLIC_PAYLOAD', rail: 'paper',
    deadlines: { expiresMs: 1000 }, statement: 'commitment' };
  return { ...v, approvalDigest: objectDigest(v) };
}
let passed = 0;
function check(fn) { fn(); passed++; }
check(() => assert.equal(terminalPacket(packet(), signing).challenge.length, 23));
check(() => assert.throws(() => terminalPacket({ ...packet(), publicLine: 'modified' }, signing)));
check(() => assert.throws(() => terminalPacket(packet({ ...subject, type: 'REVEAL' }), signing)));
check(() => assert.throws(() => terminalPacket(packet({ ...subject, did: 'other' }), signing)));
check(() => assert.throws(() => terminalPacket(packet({ ...subject, payloadDigest: 'wrong' }), signing)));
check(() => assert.match(terminalPacket(packet({ ...subject, type: 'REVEAL' }, null), signing).display, /preimage/));
check(() => {
  const line = '\x1b]52;c;fake\x07';
  const p = packet({ ...subject, payloadDigest: digest('tclk-offers|9007199254740993|' + line) }, line);
  assert.ok(!terminalPacket(p, signing).display.includes('\x1b'));
});
await assert.rejects(approveAtTerminal({ approval: {}, actionId: 'x', signing,
  input: { isTTY: false }, output: { isTTY: false } }), /HUMAN_TERMINAL_REQUIRED/); passed++;

function terminalStreams(answer) {
  const input = new PassThrough();
  input.isTTY = true;
  input.setRawMode = () => {};
  const output = new PassThrough();
  output.isTTY = true;
  const chunks = [];
  output.on('data', chunk => chunks.push(chunk));
  return {
    input,
    output,
    outputText: () => Buffer.concat(chunks).toString(),
    sendAnswer: () => setImmediate(() => input.write(answer + '\n')),
    close: () => { input.destroy(); output.destroy(); },
  };
}

async function approveWithAnswer(approval, answer) {
  const io = terminalStreams(answer);
  const pending = approveAtTerminal({ approval, actionId: 'action-1', signing,
    input: io.input, output: io.output });
  io.sendAnswer();
  try { return { result: await pending, output: io.outputText() }; }
  finally { io.close(); }
}

const currentPacket = packet();
const currentView = terminalPacket(currentPacket, signing);
let approvedRequest;
const matchingApproval = {
  preview: async actionId => { assert.equal(actionId, 'action-1'); return currentPacket; },
  approve: async request => { approvedRequest = request; return { approved: true }; },
};
const success = await approveWithAnswer(matchingApproval, 'ACCEPT ' + currentView.canonical.approvalDigest.slice(0, 16));
assert.deepEqual(success.result, { approved: true });
assert.equal(approvedRequest.actionId, 'action-1');
assert.equal(approvedRequest.approvalDigest, currentView.canonical.approvalDigest);
assert.equal(approvedRequest.confirmation, currentView.challenge);
assert.match(success.output, /"action": "ACCEPT"/);
passed++;

let incorrectApprovalCalls = 0;
await assert.rejects(approveWithAnswer({
  preview: async () => currentPacket,
  approve: async () => { incorrectApprovalCalls++; return { approved: true }; },
}, 'ACCEPT 0000000000000000'), /HUMAN_APPROVAL_REJECTED/);
assert.equal(incorrectApprovalCalls, 0);
passed++;

const changedPacket = packet({ ...subject, offer: 'changed-offer' });
let adapterApproveCalls = 0;
const changedPacketApproval = {
  preview: async () => currentPacket,
  // Model the trusted adapter's required re-fetch and full digest comparison.
  approve: async ({ approvalDigest }) => {
    adapterApproveCalls++;
    const refreshed = terminalPacket(changedPacket, signing);
    assert.notEqual(refreshed.canonical.approvalDigest, approvalDigest);
    throw new Error('APPROVAL_PAYLOAD_MISMATCH');
  },
};
await assert.rejects(approveWithAnswer(changedPacketApproval,
  'ACCEPT ' + currentView.canonical.approvalDigest.slice(0, 16)), /APPROVAL_PAYLOAD_MISMATCH/);
assert.equal(adapterApproveCalls, 1);
passed++;

const escapeLine = '\x1b]52;c;injected\x07';
const escapePacket = packet({ ...subject,
  payloadDigest: digest('tclk-offers|9007199254740993|' + escapeLine) }, escapeLine);
const escapeView = terminalPacket(escapePacket, signing);
const escapeOutput = await approveWithAnswer({
  preview: async () => escapePacket,
  approve: async () => ({ approved: true }),
}, 'ACCEPT ' + escapeView.canonical.approvalDigest.slice(0, 16));
const renderedDisplay = escapeOutput.output.slice(0, escapeView.display.length);
assert.equal(renderedDisplay, escapeView.display);
assert.equal(renderedDisplay.includes('\x1b'), false);
passed++;

const servicePacket = packet({ ...subject, actionId: 'action-1' });
const serviceRequest = { actionId: 'action-1', approvalDigest: servicePacket.approvalDigest,
  confirmation: terminalPacket(servicePacket, signing).challenge, expiresAt: Date.now() + 60000 };
check(() => assert.equal(validateConnectionApproval(serviceRequest, servicePacket, signing).subjectDigest, servicePacket.subjectDigest));
check(() => assert.throws(() => validateConnectionApproval({ ...serviceRequest, confirmation: 'REVEAL ' + servicePacket.approvalDigest.slice(0, 16) }, servicePacket, signing)));
check(() => { const { confirmation, ...missing } = serviceRequest; assert.throws(() => validateConnectionApproval(missing, servicePacket, signing)); });
check(() => assert.throws(() => validateConnectionApproval({ ...serviceRequest, actionId: 'other' }, servicePacket, signing)));
const dummy = Buffer.alloc(32, 0x41);
check(() => assert.doesNotThrow(() => validateDummyCredential(dummy, digest(dummy))));
check(() => assert.throws(() => validateDummyCredential(Buffer.alloc(32, 0x42), digest(dummy))));
check(() => assert.throws(() => validateDummyCredential(dummy, undefined)));
dummy.fill(0);
console.log(JSON.stringify({ passed, failed: 0 }));
