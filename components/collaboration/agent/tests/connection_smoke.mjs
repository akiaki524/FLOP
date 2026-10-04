// Human-UID host smoke of installed DUMMY services. No public network operations.
import { randomBytes } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { official, objectDigest } from '../src/collaboration_agent/pilot_protocol.mjs';
import { callSocket } from '../src/collaboration_agent/connection_runtime.mjs';
import { verifyApprovalView } from '../src/collaboration_agent/pilot_approval.mjs';
let passed = 0;
const check = value => { if (!value) throw new Error('CHECK_FAILED'); passed++; };
const denied = async promise => { try { await promise; } catch { passed++; return; } throw new Error('DENIAL_EXPECTED'); };
const request = (role, method, value = {}) => callSocket('/run/collab-connection/' + role + '.sock', { method, value });
try {
  const { t, signing } = await official();
  const boundary = await request('human', 'boundaryStatus');
  check(boundary.mode === 'DUMMY_OFFLINE' && boundary.childDenied && boundary.inetDenied);
  if (process.argv[2] === 'recovery') {
    const s = await request('worker-control', 'status');
    check(s.reconciliationOnly && s.secretCustody === 'ENCRYPTED_RECOVERED' && s.quotaConsumed);
    await denied(request('worker-control', 'prepare', { type: 'ACCEPT' }));
  } else {
    const candidate = JSON.parse(readFileSync('.local/batch17a/candidate.json', 'utf8'));
    check(candidate.independent_verification.valid === true);
    const payer = signing.signerFromSeed(randomBytes(32)), now = Date.now();
    const offer = t.makeOffer({ from: payer.did, role: 'payer', amount: '1', asset: 'PAPER',
      rails: ['paper'], lock: 'hash', expiresMs: now + 600000, claimByMs: now + 1800000,
      refundAfterMs: now + 2400000,
      job: { proto: 'a2a', id: 'TEST-connection-gcd', context: 'TEST GCD bundle sha256=' + candidate.source_digest } });
    const line = t.encodeFrame(offer), nonce = String(now);
    const offerRecord = { room: t.OFFER_ROOM, seq: 1, timestampMs: now, sender: payer.did, nonce,
      signature: payer.sign(signing.canonicalMessage(t.OFFER_ROOM, nonce, line)), line };
    const meta = await request('human', 'admit', { mode: 'TEST_EPHEMERAL', expectedDid: boundary.did,
      offerRecord, sourceDigest: candidate.source_digest, family: 'math.gcd_lcm', answer: candidate.answer,
      noValue: 'EXPLICIT_TEST_NO_VALUE', heartbeatRequired: false });
    const value = { type: 'ACCEPT', did: boundary.did, room: t.OFFER_ROOM, offer: offer.id,
      contract: meta.contract, ref: offer.id, sourceDigest: candidate.source_digest };
    await denied(request('worker-control', 'prepare', value)); // current nonce state unverified
    await request('human', 'nonceObservation', { room: t.OFFER_ROOM, observedNonce: null,
      observedNone: true, verifiedAtMs: Date.now() }); // TEST venue observation, never Project DID evidence
    const action = await request('worker-control', 'prepare', value);
    const token = { actionId: action.subject.actionId, subjectDigest: objectDigest(action.subject) };
    await denied(request('worker-control', 'sign', token));
    const packet = verifyApprovalView(await request('human', 'approvalPreview', { actionId: token.actionId }), signing);
    const confirmation = 'ACCEPT ' + packet.approvalDigest.slice(0, 16);
    await denied(request('human', 'approve', { actionId: token.actionId, approvalDigest: '0'.repeat(64), confirmation, expiresAt: Date.now() + 60000 }));
    await denied(request('human', 'approve', { actionId: token.actionId, approvalDigest: packet.approvalDigest, confirmation, expiresAt: Date.now() - 1 }));
    await denied(request('human', 'approve', { actionId: token.actionId, approvalDigest: packet.approvalDigest,
      confirmation: 'REVEAL ' + packet.approvalDigest.slice(0, 16), expiresAt: Date.now() + 60000 }));
    await request('human', 'approve', { actionId: token.actionId, approvalDigest: packet.approvalDigest,
      confirmation: 'ACCEPT ' + packet.approvalDigest.slice(0, 16), expiresAt: Date.now() + 60000 });
    await denied(request('human', 'approve', { actionId: token.actionId, approvalDigest: packet.approvalDigest, confirmation, expiresAt: Date.now() + 60000 }));
    check((await request('worker-control', 'sign', token)).verified);
    const prepared = await request('worker-control', 'prepareWrite', token);
    check(prepared.effect === 'OFFLINE_TRANSPORT_VALIDATION_ONLY' && prepared.publicNetwork === false);
    await denied(request('worker-control', 'attempt', token));
    await denied(request('worker-control', 'approve', {}));
    await denied(request('human', 'mock', { enabled: true, mode: 'ack' }));
  }
  console.log(JSON.stringify({ passed, failed: 0, externalWrites: 0, realSecrets: false }));
} catch {
  console.log(JSON.stringify({ passed, failed: 1, code: 'CONNECTION_SMOKE_FAILED', externalWrites: 0 }));
  process.exitCode = 1;
}
