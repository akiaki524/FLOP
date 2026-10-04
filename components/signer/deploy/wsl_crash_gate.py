#!/usr/bin/env python3
"""Explicit root-owned, boot-bound WSL no-core gate. status is read-only.

No secret input, environment path overrides, service mutations or automatic
rollback. A failed/partial transition retains evidence and stops.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import platform
import re
import stat
import subprocess
from pathlib import Path

KERNEL = Path('/proc/sys/kernel')
STATE = Path('/run/flop-policy-signer-crash-gate.json')
TARGET = {'corePattern': '', 'coreUsesPid': '0'}
UNITS = ('flop-policy-signer.socket', 'flop-policy-signer.service',
         'flop-policy-signer-bootstrap.service')


class GateError(RuntimeError):
    pass


def need(condition, code):
    if not condition:
        raise GateError(code)


def proc_value(name):
    raw = (KERNEL / name).read_text(encoding='ascii')
    # Remove only the proc terminator, never whitespace in the actual pattern.
    value = raw.removesuffix('\n')
    need('\n' not in value and '\r' not in value and '\x00' not in value and len(value) <= 4096,
         'CRASH_GATE_KERNEL_VALUE_INVALID')
    return value


def is_wsl():
    if platform.system() != 'Linux':
        return False
    release = proc_value('osrelease')
    need(bool(re.match(r'^\d+\.\d+', release)), 'CRASH_GATE_HOST_UNKNOWN')
    return bool(re.search(r'microsoft|wsl', release, re.I))


def boot_id():
    value = proc_value('random/boot_id')
    need(bool(re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value)),
         'CRASH_GATE_BOOT_INVALID')
    return value


def current():
    return {'corePattern': proc_value('core_pattern'),
            'coreUsesPid': proc_value('core_uses_pid')}


def state_present():
    try:
        STATE.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def status():
    report = {'isWsl': None, 'bootId': None, 'corePatternClass': 'unknown',
              'coreUsesPid': None, 'armed': False, 'safe': False,
              'gateStatePresent': state_present()}
    try:
        report['isWsl'] = is_wsl()
        if platform.system() == 'Linux':
            report['bootId'] = boot_id()
            values = current()
            pattern = values['corePattern']
            report['corePatternClass'] = ('empty' if pattern == '' else
                                          'pipe' if pattern.startswith('|') else 'file')
            report['coreUsesPid'] = values['coreUsesPid'] if values['coreUsesPid'] in ('0', '1') else 'unknown'
            report['armed'] = report['isWsl'] and values == TARGET
        report['safe'] = report['isWsl'] is False or report['armed']
    except (OSError, UnicodeError, GateError):
        report['code'] = 'WSL_CRASH_CAPTURE_UNSAFE'
    return report


def require_safe():
    report = status()
    need(report['safe'], 'WSL_CRASH_CAPTURE_UNSAFE')
    return report


def stopped():
    # The socket must also be closed: otherwise an RPC can activate the signer
    # between the service check and restoration of the crash handler.
    for unit in UNITS:
        result = subprocess.run(
            ['/usr/bin/systemctl', 'show', unit, '--no-pager',
             '--property=LoadState,ActiveState,MainPID,ControlPID,Job'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=10, check=False, env={'PATH': '/usr/bin:/bin'})
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        need(result.returncode == 0
             and values.get('LoadState') in ('loaded', 'not-found')
             and values.get('ActiveState') in ('inactive', 'failed')
             and values.get('MainPID', '0' if values.get('LoadState') == 'not-found' or unit.endswith('.socket') else '') == '0'
             and values.get('ControlPID', '0' if values.get('LoadState') == 'not-found' or unit.endswith('.socket') else '') == '0'
             and values.get('Job') == '', 'CRASH_GATE_SERVICE_NOT_STOPPED')


@contextmanager
def gate_lock():
    # /run itself is stable across unlink of our state; no stale lock file and
    # no competing gate operation can replace the inode being locked.
    fd = os.open('/run', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def load_state(boot):
    try:
        fd = os.open(STATE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'r', encoding='ascii') as stream:
        meta = os.fstat(stream.fileno())
        need(stat.S_ISREG(meta.st_mode) and meta.st_uid == 0 and meta.st_gid == 0
             and stat.S_IMODE(meta.st_mode) == 0o600 and meta.st_nlink == 1
             and meta.st_size <= 16384, 'CRASH_GATE_STATE_UNTRUSTED')
        value = json.load(stream)
    need(isinstance(value, dict) and set(value) == {'schema', 'bootId', 'original', 'target'}
         and value['schema'] == 1 and value['target'] == TARGET,
         'CRASH_GATE_STATE_INVALID')
    need(value['bootId'] == boot, 'CRASH_GATE_BOOT_MISMATCH')
    original = value['original']
    need(isinstance(original, dict) and set(original) == set(TARGET)
         and original['coreUsesPid'] in ('0', '1')
         and isinstance(original['corePattern'], str)
         and len(original['corePattern']) <= 4096
         and all(c not in original['corePattern'] for c in ('\n', '\x00', '\r')),
         'CRASH_GATE_STATE_INVALID')
    return value


def save_new_state(boot, original):
    value = {'schema': 1, 'bootId': boot, 'original': original, 'target': TARGET}
    # Exclusive create + fsync BEFORE any sysctl write. Interrupted creation is
    # untrusted/invalid and refused; never silently overwrite previous evidence.
    fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='ascii') as stream:
        os.fchmod(stream.fileno(), 0o600)
        os.fchown(stream.fileno(), 0, 0)
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    sync_state_directory()
    return value


def sync_state_directory():
    fd = os.open(STATE.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_value(name, value):
    # A newline is necessary: zero-byte write would not update an empty pattern.
    data = (value + '\n').encode('ascii')
    fd = os.open(KERNEL / name, os.O_WRONLY)
    try:
        need(os.write(fd, data) == len(data), 'CRASH_GATE_WRITE_FAILED')
    finally:
        os.close(fd)
    need(proc_value(name) == value, 'CRASH_GATE_READBACK_MISMATCH')


def transition(values, boot, order):
    need(boot_id() == boot, 'CRASH_GATE_BOOT_MISMATCH')
    # No secret-bearing services may run during this two-sysctl transition.
    # Never pass through empty pattern + core_uses_pid=1 (local ".PID" cores):
    # arm clears pid before the pattern; restore resets the pattern first.
    keys = {'core_uses_pid': 'coreUsesPid', 'core_pattern': 'corePattern'}
    for name in order:
        write_value(name, values[keys[name]])
    need(boot_id() == boot and current() == values, 'CRASH_GATE_READBACK_MISMATCH')


def operate(command):
    need(command in ('arm', 'disarm'), 'CRASH_GATE_COMMAND_INVALID')
    need(os.geteuid() == 0, 'ROOT_REQUIRED')
    if not is_wsl():
        return status()
    with gate_lock():
        boot = boot_id()
        stopped()
        saved = load_state(boot)
        effective = current()
        if command == 'arm':
            if saved is not None:
                # Also recovers interruption after the last successful sysctl
                # write. Any other prior state needs Human investigation.
                need(effective == TARGET, 'CRASH_GATE_PRIOR_STATE_MISMATCH')
            else:
                need(effective['coreUsesPid'] in ('0', '1'), 'CRASH_GATE_KERNEL_VALUE_INVALID')
                save_new_state(boot, effective)
                transition(TARGET, boot, ('core_uses_pid', 'core_pattern'))
            need(current() == TARGET and boot_id() == boot, 'CRASH_GATE_READBACK_MISMATCH')
        else:
            need(saved is not None, 'CRASH_GATE_STATE_MISSING')
            need(effective == TARGET, 'CRASH_GATE_CURRENT_STATE_MISMATCH')
            transition(saved['original'], boot, ('core_pattern', 'core_uses_pid'))
            STATE.unlink()
            sync_state_directory()
        return status()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('status', 'arm', 'disarm'))
    args = parser.parse_args(argv)
    try:
        result = status() if args.command == 'status' else operate(args.command)
        print(json.dumps(result, sort_keys=True))
        return 0
    except GateError as error:
        print(json.dumps({'safe': False, 'code': str(error)}))
    except Exception:
        print(json.dumps({'safe': False, 'code': 'CRASH_GATE_OPERATION_FAILED'}))
    return 70


if __name__ == '__main__':
    raise SystemExit(main())
