// Signer-only Project DID credential loading. This module has no network capability.
import { readFileSync } from 'node:fs';
import { isAbsolute, resolve } from 'node:path';
import { PROJECT_DID } from './connection_approval.mjs';
import { official, requireThat as need } from './pilot_protocol.mjs';

export const REAL_CREDENTIAL_NAME = 'project-seed';

function seedBytes(value) {
  need((Buffer.isBuffer(value) || value instanceof Uint8Array) && value.byteLength === 32,
    'REAL_CREDENTIAL_MALFORMED');
  return Buffer.from(value);
}

// expectedDid is injectable only for deterministic offline tests. Production callers
// omit it, which pins the credential to the one existing Project DID.
export async function validateRealCredential(seed, expectedDid = PROJECT_DID) {
  need(typeof expectedDid === 'string' && expectedDid.length > 0, 'REAL_CREDENTIAL_DID_MISMATCH');
  const copy = seedBytes(seed);
  try {
    const { signing } = await official();
    const did = signing.signerFromSeed(copy).did;
    need(did === expectedDid, 'REAL_CREDENTIAL_DID_MISMATCH');
    return did;
  } finally {
    copy.fill(0);
  }
}

// Only a systemd-provided credential directory is accepted, never a seed filename.
// The trusted signer initializer receives the validated bytes only for the duration
// of this call; it must retain its own private copy if its signer implementation
// closes over the input seed. The credential read buffer is always erased here.
export async function withRealCredentialFromDirectory(credentialDirectory, initializeSigner,
  expectedDid = PROJECT_DID) {
  need(typeof credentialDirectory === 'string' && isAbsolute(credentialDirectory),
    'REAL_CREDENTIAL_DIRECTORY_REQUIRED');
  need(typeof initializeSigner === 'function', 'REAL_SIGNER_INITIALIZER_REQUIRED');
  let seed;
  try {
    seed = readFileSync(resolve(credentialDirectory, REAL_CREDENTIAL_NAME));
  } catch {
    throw new Error('REAL_CREDENTIAL_UNAVAILABLE');
  }
  try {
    const did = await validateRealCredential(seed, expectedDid);
    return await initializeSigner(seed, did);
  } finally {
    seed.fill(0);
  }
}
