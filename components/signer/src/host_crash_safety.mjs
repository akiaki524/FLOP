import { readFileSync } from 'node:fs';
import { platform } from 'node:os';

const OS_RELEASE_PATH = '/proc/sys/kernel/osrelease';
const CORE_PATTERN_PATH = '/proc/sys/kernel/core_pattern';
const CORE_USES_PID_PATH = '/proc/sys/kernel/core_uses_pid';

function fail() {
  const error = new Error('WSL_CRASH_CAPTURE_UNSAFE');
  error.code = 'WSL_CRASH_CAPTURE_UNSAFE';
  throw error;
}

function readText(readFile, path) {
  const value = readFile(path);
  if (typeof value === 'string') return value;
  if (Buffer.isBuffer(value) || value instanceof Uint8Array) {
    return Buffer.from(value).toString('utf8');
  }
  fail();
}

function removeOneProcNewline(value) {
  return value.endsWith('\n') ? value.slice(0, -1) : value;
}

export function assertHostCrashSafety({ readFile = readFileSync } = {}) {
  if (platform() !== 'linux') return;
  let osRelease;
  try {
    osRelease = readText(readFile, OS_RELEASE_PATH);
  } catch {
    fail();
  }

  if (!/^\d+\.\d+/.test(osRelease.trim())) fail();
  if (!/(?:microsoft|wsl)/i.test(osRelease)) return;

  let corePattern;
  let coreUsesPid;
  try {
    corePattern = readText(readFile, CORE_PATTERN_PATH);
    coreUsesPid = readText(readFile, CORE_USES_PID_PATH);
  } catch {
    fail();
  }

  if (removeOneProcNewline(corePattern) !== ''
    || removeOneProcNewline(coreUsesPid) !== '0') {
    fail();
  }
}
