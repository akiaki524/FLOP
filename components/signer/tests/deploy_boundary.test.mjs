import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

function read(name) {
  return readFileSync(new URL('../deploy/' + name, import.meta.url), 'utf8');
}

test('Signer deployment has credential access but no Internet address families', () => {
  const service = read('flop-policy-signer.service');
  assert.match(service, /^Requires=flop-policy-signer\.socket flop-policy-signer-reader\.socket$/m);
  assert.match(service, /^User=flop-signer$/m);
  assert.match(service, /^SupplementaryGroups=flop-signer-acquisition$/m);
  assert.match(service, /^LoadCredentialEncrypted=project-seed:/m);
  assert.match(service, /^PrivateNetwork=yes$/m);
  assert.match(service, /^RestrictAddressFamilies=AF_UNIX$/m);
  assert.doesNotMatch(service, /AF_INET/);
  assert.doesNotMatch(service, /--permission|--allow-fs-(?:read|write)/);
  assert.match(service, /^InaccessiblePaths=\/var\/lib\/flop-policy-signer-secret$/m);
});

test('Trusted Reader deployment has network and snapshot write only, with no credential access', () => {
  const service = read('flop-policy-signer-reader.service');
  assert.match(service, /^User=flop-signer-reader$/m);
  assert.match(service, /^SupplementaryGroups=flop-signer-acquisition$/m);
  assert.match(service, /^RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6$/m);
  assert.match(service, /^ReadWritePaths=\/var\/lib\/flop-policy-signer-input$/m);
  assert.doesNotMatch(service, /--permission|--allow-fs-(?:read|write)/);
  assert.doesNotMatch(service, /LoadCredential/);
  assert.match(service,
    /^InaccessiblePaths=\/var\/lib\/flop-policy-signer-secret \/etc\/flop-policy-signer \/var\/lib\/flop-policy-signer$/m);
});

test('Agent reaches only the signer socket; trusted reader socket is signer-internal', () => {
  const signerSocket = read('flop-policy-signer.socket');
  assert.match(signerSocket, /^SocketMode=0660$/m);
  assert.match(signerSocket, /^FileDescriptorName=signer$/m);
  assert.match(signerSocket, /^SocketGroup=flop-agent$/m);
  assert.match(signerSocket, /^RemoveOnStop=yes$/m);

  const readerSocket = read('flop-policy-signer-reader.socket');
  assert.match(readerSocket, /^SocketMode=0660$/m);
  assert.match(readerSocket, /^FileDescriptorName=reader$/m);
  assert.match(readerSocket, /^SocketGroup=flop-signer$/m);
  assert.doesNotMatch(readerSocket, /^SocketGroup=flop-agent$/m);
  assert.match(readerSocket, /^RemoveOnStop=yes$/m);

  for (const socket of [signerSocket, readerSocket]) {
    assert.doesNotMatch(socket, /ListenDatagram|ListenFIFO|FreeBind|BindToDevice/);
  }
});

test('Filesystem templates keep encrypted Secret root-only and snapshots reader-owned/read-shared', () => {
  const tmpfiles = read('flop-policy-signer.tmpfiles.conf');
  assert.match(tmpfiles,
    /^d \/var\/lib\/flop-policy-signer-secret 0700 root root -$/m);
  assert.match(tmpfiles,
    /^d \/var\/lib\/flop-policy-signer-input 2750 flop-signer-reader flop-signer-acquisition -$/m);

  const sysusers = read('flop-policy-signer.sysusers.conf');
  assert.match(sysusers, /^g flop-signer-acquisition -$/m);
  assert.match(sysusers, /^u flop-signer /m);
  assert.match(sysusers, /^u flop-signer-reader /m);
  assert.match(sysusers, /^m flop-signer flop-signer-acquisition$/m);
  assert.match(sysusers, /^m flop-signer-reader flop-signer-acquisition$/m);
});

test('State bootstrap is one-shot, credential-backed and network-disabled', () => {
  const service = read('flop-policy-signer-bootstrap.service');
  assert.match(service, /^ConditionPathExists=!\/var\/lib\/flop-policy-signer\/state\.json$/m);
  assert.match(service, /^LoadCredentialEncrypted=project-seed:/m);
  assert.match(service, /^PrivateNetwork=yes$/m);
  assert.match(service, /^RestrictAddressFamilies=AF_UNIX$/m);
  assert.doesNotMatch(service, /--permission|--allow-fs-(?:read|write)/);
  assert.match(service, /^InaccessiblePaths=\/var\/lib\/flop-policy-signer-secret$/m);
});


test('all signer units use the provisioned Node 22 runtime binary', () => {
  for (const name of [
    'flop-policy-signer.service',
    'flop-policy-signer-reader.service',
    'flop-policy-signer-bootstrap.service',
  ]) {
    const unit = read(name);
    assert.match(unit, /^ExecStart=\/usr\/local\/lib\/flop-policy-signer\/node /m);
    assert.doesNotMatch(unit, /\/usr\/bin\/node/);
  }
});


test('Secret-bearing Node units run jitless under MemoryDenyWriteExecute; Reader keeps fetch-compatible JIT policy', () => {
  const signer = read('flop-policy-signer.service');
  const bootstrap = read('flop-policy-signer-bootstrap.service');
  const reader = read('flop-policy-signer-reader.service');

  for (const unit of [signer, bootstrap]) {
    assert.match(unit, /^ExecStart=\/usr\/local\/lib\/flop-policy-signer\/node --jitless --disable-sigusr1 /m);
    assert.doesNotMatch(unit, /--permission|--allow-fs-(?:read|write)/);
    assert.match(unit, /^MemoryDenyWriteExecute=yes$/m);
    assert.match(unit, /^LimitCORE=0$/m);
  }

  assert.match(reader, /^ExecStart=\/usr\/local\/lib\/flop-policy-signer\/node --disable-sigusr1 /m);
  assert.doesNotMatch(reader, /--permission|--allow-fs-(?:read|write)/);
  assert.doesNotMatch(reader, /--jitless/);
  assert.doesNotMatch(reader, /^MemoryDenyWriteExecute=yes$/m);
});
