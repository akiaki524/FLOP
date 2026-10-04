import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import { syncBuiltinESMExports } from 'node:module';
import test, { mock } from 'node:test';

import { cliInput, handoffCredential, readRawSeed } from '../src/credential_handoff.mjs';
import { assertHostCrashSafety } from '../src/host_crash_safety.mjs';
import { withSystemdCredential } from '../src/runtime_credential.mjs';

const WSL_RELEASE = '6.6.87.2-microsoft-standard-WSL2\n';
const PROC = {
  '/proc/sys/kernel/osrelease': WSL_RELEASE,
  '/proc/sys/kernel/core_pattern': '\n',
  '/proc/sys/kernel/core_uses_pid': '0\n',
};
const UNSAFE_PROC = {
  ...PROC,
  '/proc/sys/kernel/core_pattern': '|/wsl-capture-crash %t %E %p %s\n',
};

function mockedReader(values, { failures = [] } = {}) {
  const calls = [];
  return {
    calls,
    readFile(path) {
      calls.push(path);
      if (failures.includes(path)) throw new Error('synthetic read failure');
      if (!(path in values)) throw new Error('unexpected path');
      return Buffer.from(values[path]);
    },
  };
}

function expectUnsafe(readFile) {
  assert.throws(() => assertHostCrashSafety({ readFile }), error =>
    error?.code === 'WSL_CRASH_CAPTURE_UNSAFE'
      && error.message === 'WSL_CRASH_CAPTURE_UNSAFE');
}

test('WSL crash capture gate rejects default and alternate pipe handlers', () => {
  const defaultPipe = mockedReader({
    ...PROC,
    '/proc/sys/kernel/core_pattern': '|/wsl-capture-crash %t %E %p %s\n',
  });
  expectUnsafe(defaultPipe.readFile);

  const otherPipe = mockedReader({
    ...PROC,
    '/proc/sys/kernel/core_pattern': '|/usr/local/bin/crash-handler %p\n',
  });
  expectUnsafe(otherPipe.readFile);
});

test('WSL crash capture gate requires core capture disabled and accepts safe proc values', () => {
  const noCore = mockedReader({
    ...PROC,
    '/proc/sys/kernel/core_pattern': 'core.%p\n',
  });
  expectUnsafe(noCore.readFile);

  const nonzeroPid = mockedReader({
    ...PROC,
    '/proc/sys/kernel/core_uses_pid': '1\n',
  });
  expectUnsafe(nonzeroPid.readFile);

  const extraNewline = mockedReader({
    ...PROC,
    '/proc/sys/kernel/core_pattern': '\n\n',
  });
  expectUnsafe(extraNewline.readFile);

  const extraPidNewline = mockedReader({
    ...PROC,
    '/proc/sys/kernel/core_uses_pid': '0\n\n',
  });
  expectUnsafe(extraPidNewline.readFile);

  const safe = mockedReader(PROC);
  assert.doesNotThrow(() => assertHostCrashSafety({ readFile: safe.readFile }));
  assert.deepEqual(safe.calls, Object.keys(PROC));
});

test('non-WSL hosts do not read WSL proc settings', () => {
  const reader = mockedReader({ '/proc/sys/kernel/osrelease': '6.10.1-generic\n' });
  assert.doesNotThrow(() => assertHostCrashSafety({ readFile: reader.readFile }));
  assert.deepEqual(reader.calls, ['/proc/sys/kernel/osrelease']);
});

test('unknown host and proc read failures fail closed with one public error', () => {
  expectUnsafe(() => { throw new Error('unknown host'); });
  expectUnsafe(mockedReader({
    '/proc/sys/kernel/osrelease': 'unknown\n',
  }).readFile);
  expectUnsafe(mockedReader(PROC, {
    failures: ['/proc/sys/kernel/core_pattern'],
  }).readFile);
  expectUnsafe(mockedReader(PROC, {
    failures: ['/proc/sys/kernel/core_uses_pid'],
  }).readFile);
});

test('host settings are reread for every safety check', () => {
  let current = { ...PROC };
  const calls = [];
  const readFile = path => {
    calls.push(path);
    return current[path];
  };

  current['/proc/sys/kernel/core_pattern'] = '|/wsl-capture-crash\n';
  expectUnsafe(readFile);
  current = { ...PROC };
  assert.doesNotThrow(() => assertHostCrashSafety({ readFile }));
  assert.equal(calls.filter(path => path === '/proc/sys/kernel/osrelease').length, 2);
});

async function withProcMock(callback) {
  const originalRead = fs.readFileSync;
  const readMock = mock.method(fs, 'readFileSync', path => {
    const value = UNSAFE_PROC[String(path)];
    if (value === undefined) throw new Error('unexpected file read');
    return Buffer.from(value);
  });
  syncBuiltinESMExports();
  try {
    return await callback();
  } finally {
    readMock.mock.restore();
    fs.readFileSync = originalRead;
    syncBuiltinESMExports();
  }
}

test('handoff and raw seed reader reject before consuming the supplied iterator', async () => {
  await withProcMock(async () => {
    let consumed = false;
    const input = {
      async *[Symbol.asyncIterator]() {
        consumed = true;
        yield Buffer.alloc(32, 1);
      },
    };

    await assert.rejects(() => readRawSeed(input), error =>
      error?.code === 'WSL_CRASH_CAPTURE_UNSAFE');
    assert.equal(consumed, false);

    await assert.rejects(() => handoffCredential({ input }), error =>
      error?.code === 'WSL_CRASH_CAPTURE_UNSAFE');
    assert.equal(consumed, false);
  });
});

test('CLI rejects before starting an FD stream', async () => {
  await withProcMock(() => {
    let streamStarted = false;
    assert.throws(() => cliInput(['--input-fd', '3'], {
      streamFromFd() {
        streamStarted = true;
        return {};
      },
    }), error => error?.code === 'WSL_CRASH_CAPTURE_UNSAFE');
    assert.equal(streamStarted, false);
  });
});

test('systemd credential loader rejects before reading the credential file', async () => {
  await withProcMock(async () => {
    await assert.rejects(() => withSystemdCredential('/tmp/synthetic-credentials', () => null),
      error => error?.code === 'WSL_CRASH_CAPTURE_UNSAFE');
  });
});

test('non-Linux behavior is unchanged without proc access', () => {
  const platformMock = mock.method(os, 'platform', () => 'darwin');
  syncBuiltinESMExports();
  try {
    assert.doesNotThrow(() => assertHostCrashSafety({
      readFile: () => { throw new Error('must not read proc'); },
    }));
  } finally {
    platformMock.mock.restore();
    syncBuiltinESMExports();
  }
});
