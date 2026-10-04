"""Root-only, additive dummy boundary setup. No real credentials or network writes.

Run from the reviewed checkout: sudo python3 -I deploy/connection/setup.py
Fresh setup refuses existing resources. --recover-probe migrates only the verified
legacy probe failure, preserving old artifacts; downstream installations are refused.
"""
import errno
import fcntl
import stat
import tempfile
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import uuid
import shutil
import subprocess
import sys
import time

ROLES = ['collab-worker', 'collab-approval', 'collab-signer', 'collab-transport']
ROOT = Path('/var/lib/collab-connection-setup')
UNIT = Path('/etc/systemd/system/collab-boundary-probe.service')

def run(args, **kwargs):
    try:
        return subprocess.run(args, check=kwargs.pop('check', True), capture_output=True, **kwargs)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        raise RuntimeError(operation_code(args)) from None


def operation_code(args):
    if args[0] == 'systemctl':
        action = args[1]
        kind = 'PROBE' if 'collab-boundary-probe.service' in args else (
            'SOCKET' if any(a.endswith('.socket') for a in args[2:]) else 'SERVICE')
        if action == 'daemon-reload':
            return 'DAEMON_RELOAD_FAILED'
        return kind + '_' + {'show': 'STATE_QUERY_FAILED', 'reset-failed': 'RESET_FAILED_REFUSED',
                            'start': 'START_FAILED', 'stop': 'STOP_FAILED',
                            'kill': 'KILL_FAILED', 'is-active': 'ACTIVE_QUERY_FAILED',
                            'is-system-running': 'MANAGER_QUERY_FAILED'}.get(action, 'OPERATION_FAILED')
    return {'systemd-creds': 'DUMMY_CREDENTIAL_OPERATION_FAILED',
            'useradd': 'ACCOUNT_CREATE_FAILED', 'journalctl': 'JOURNAL_QUERY_FAILED',
            'runuser': 'UID_OPERATION_FAILED'}.get(args[0], 'HOST_PROCESS_FAILED')


def reset_failed(names, runner=None):
    runner = runner or run
    for name in names:
        result = runner(['systemctl', 'show', name, '-p', 'LoadState', '-p', 'ActiveState'], check=False)
        props = dict(line.split('=', 1) for line in result.stdout.decode().splitlines() if '=' in line)
        load, active = props.get('LoadState'), props.get('ActiveState')
        if load == 'not-found' and active == 'inactive':
            continue
        if getattr(result, 'returncode', 0) != 0:
            raise RuntimeError(operation_code(['systemctl', 'show', name]))
        if load != 'loaded' or active not in ('failed', 'inactive', 'active'):
            raise RuntimeError('UNIT_STATE_UNSAFE')
        if active == 'failed':
            runner(['systemctl', 'reset-failed', name])

def main():
    if os.geteuid() != 0:
        raise RuntimeError('ROOT_REQUIRED')
    if sys.argv[1:] == ['--recover-probe']:
        recover()
        return
    if sys.argv[1:]:
        raise RuntimeError('ARGUMENTS_REFUSED')
    install = Path('/usr/local/lib/collab-boundary-probe')
    if any(p.exists() for p in [ROOT, UNIT, install, Path('/var/lib/collab-boundary-probe')]):
        raise RuntimeError('EXISTING_SETUP_REFUSED')
    for role in ROLES:
        try:
            grp.getgrnam(role)
        except KeyError:
            pass
        else:
            raise RuntimeError('EXISTING_GROUP_REFUSED')
        try:
            pwd.getpwnam(role)
        except KeyError:
            pass
        else:
            raise RuntimeError('EXISTING_ACCOUNT_REFUSED')
    if run(['systemctl', 'is-system-running', '--wait'], timeout=60).stdout.strip() != b'running':
        raise RuntimeError('SYSTEMD_NOT_RUNNING')
    # Capture pre-change public configuration before the first account change.
    ROOT.mkdir(mode=0o700)
    baseline = {'at': time.time(), 'roles_existed': False, 'unit_existed': False,
                'kernel': os.uname().release, 'created_users': [],
                'host_credential_key_existed': Path('/var/lib/systemd/credential.secret').exists()}
    (ROOT / 'before.json').write_text(json.dumps(baseline, indent=2))
    for role in ROLES:
        run(['useradd', '--system', '--no-create-home', '--user-group', '--shell', '/usr/sbin/nologin', role])
        baseline['created_users'].append(role)
        (ROOT / 'before.json').write_text(json.dumps(baseline, indent=2))
    # Only encrypted bytes reach disk. Plain dummy bytes move through anonymous pipes.
    dummy = os.urandom(32)
    dummy_fingerprint = hashlib.sha256(dummy).hexdigest()
    encrypted = run(['systemd-creds', 'encrypt', '--with-key=host', '--name=dummy', '-', '-'], input=dummy).stdout
    del dummy
    (ROOT / 'dummy.cred').write_bytes(encrypted)
    os.chmod(ROOT / 'dummy.cred', 0o600)
    install.mkdir(mode=0o755)
    shutil.copyfile(Path(__file__).resolve().with_name('probe.py'), install / 'probe.py')
    os.chmod(install / 'probe.py', 0o644)
    UNIT.write_bytes(Path(__file__).resolve().with_name('probe.service').read_bytes())
    os.chmod(UNIT, 0o644)
    measure(dummy_fingerprint)

def measure(dummy_fingerprint):
    output = Path('/var/lib/collab-boundary-probe/result.json')
    signer_uid = pwd.getpwnam('collab-signer').pw_uid
    # Preserve any old output before start: a successful oneshot loses its
    # systemd InvocationID on exit, so post-exit show cannot establish freshness.
    if os.path.lexists(output):
        trusted(output, uid=signer_uid)
        output.rename(ROOT / ('probe-result-before-' + uuid.uuid4().hex + '.json'))
        sync_dir(ROOT)
        sync_dir(output.parent)
    run(['systemctl', 'daemon-reload'])
    reset_failed(['collab-boundary-probe.service'])
    run(['systemctl', 'start', 'collab-boundary-probe.service'])
    result = json.loads(trusted(output, uid=signer_uid).read_text())
    result['cross_uid'] = {}
    for role in ['collab-worker', 'collab-approval', 'collab-transport']:
        checks = []
        for path in [ROOT / 'dummy.cred', Path('/var/lib/collab-boundary-probe/result.json')]:
            check = subprocess.run(['runuser', '-u', role, '--', 'test', '-r', str(path)], capture_output=True)
            checks.append(check.returncode == 1)
        result['cross_uid'][role] = all(checks)
    result['roles'] = {r: pwd.getpwnam(r).pw_uid for r in ROLES}
    if result.get('dummy_credential_sha256') != dummy_fingerprint:
        raise RuntimeError('DUMMY_FINGERPRINT_MISMATCH')
    validate_result(result)
    result['external_writes'] = 0
    atomic_write(ROOT / 'result.json', (json.dumps(result, indent=2) + '\n').encode(), 0o600)
    print(json.dumps(result, indent=2))


def atomic_write(path, data, mode):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def trusted(path, uid=0, directory=False, mode=None):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
        raise RuntimeError('UNTRUSTED_SETUP_PATH')
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise RuntimeError('SETUP_MODE_MISMATCH')
    return path


def validate_result(result):
    checks = ['credential_read', 'inet_denied', 'fork_denied', 'no_new_privileges',
              'secret_absent_env', 'secret_absent_argv']
    if not all(result.get(k) is True for k in checks):
        raise RuntimeError('BOUNDARY_MEASUREMENT_FAILED')
    if result.get('schema') != 2 or result.get('fork_errno') != errno.EPERM:
        raise RuntimeError('PROCESS_DENIAL_NOT_EPERM')
    roles = {r: pwd.getpwnam(r).pw_uid for r in ROLES}
    if len(set(roles.values())) != 4 or 0 in roles.values() or result['uid'] != roles['collab-signer']:
        raise RuntimeError('DEDICATED_UID_FAILED')
    if result.get('cross_uid') != {r: True for r in ROLES if r != 'collab-signer'}:
        raise RuntimeError('CROSS_UID_FAILED')
    if not re.fullmatch('[0-9a-f]{32}', result.get('invocation_id') or ''):
        raise RuntimeError('MISSING_PROBE_INVOCATION')


def recovery_preflight():
    # This is specifically a setup-stage recovery, never a service/state updater.
    for path in [Path('/usr/local/lib/collab-connection'), Path('/etc/collab-connection'),
                 Path('/var/lib/collab-signer'), Path('/run/collab-connection')]:
        if os.path.lexists(path):
            raise RuntimeError('DOWNSTREAM_INSTALLATION_REFUSED')
    names = ['worker', 'signer', 'approval', 'transport', 'gate', 'human', 'worker-control']
    for name in names:
        for suffix in ['service', 'socket']:
            for directory in ['/etc/systemd/system', '/run/systemd/system', '/usr/lib/systemd/system']:
                if os.path.lexists(Path(directory) / ('collab-' + name + '.' + suffix)):
                    raise RuntimeError('DOWNSTREAM_UNIT_REFUSED')
    for marker in ['connection-before.json', 'deployment.json']:
        if os.path.lexists(ROOT / marker):
            raise RuntimeError('DOWNSTREAM_RECORD_REFUSED')
    trusted(ROOT, directory=True, mode=0o700)
    before = json.loads(trusted(ROOT / 'before.json').read_text())
    if before.get('roles_existed') is not False or before.get('unit_existed') is not False or before.get('created_users') != ROLES:
        raise RuntimeError('SETUP_PROVENANCE_REFUSED')
    uids = []
    for role in ROLES:
        account, group = pwd.getpwnam(role), grp.getgrnam(role)
        if account.pw_uid == 0 or account.pw_gid != group.gr_gid or account.pw_shell != '/usr/sbin/nologin' or group.gr_mem:
            raise RuntimeError('ACCOUNT_MISMATCH')
        if any(g.gr_gid != group.gr_gid and role in g.gr_mem for g in grp.getgrall()):
            raise RuntimeError('SUPPLEMENTARY_GROUP_REFUSED')
        if any(p.pw_name != role and (p.pw_gid == group.gr_gid or p.pw_uid == account.pw_uid) for p in pwd.getpwall()):
            raise RuntimeError('SHARED_GROUP_REFUSED')
        uids.append(account.pw_uid)
    if len(set(uids)) != 4:
        raise RuntimeError('SHARED_UID_REFUSED')
    credential = trusted(ROOT / 'dummy.cred', mode=0o600)
    install = trusted(Path('/usr/local/lib/collab-boundary-probe'), directory=True, mode=0o755)
    if {p.name for p in install.iterdir()} != {'probe.py'}:
        raise RuntimeError('UNKNOWN_PROBE_FILES')
    trusted(Path('/var/lib/collab-boundary-probe'), uid=uids[2], directory=True, mode=0o700)
    # Any override could defeat the source-unit comparison. Refuse instead of editing it.
    properties = run(['systemctl', 'show', 'collab-boundary-probe.service',
                      '-p', 'FragmentPath', '-p', 'DropInPaths', '-p', 'ActiveState']).stdout.decode()
    props = dict(line.split('=', 1) for line in properties.splitlines() if '=' in line)
    if props.get('FragmentPath') != str(UNIT) or props.get('DropInPaths') != '' or props.get('ActiveState') not in ('failed', 'inactive'):
        raise RuntimeError('PROBE_UNIT_STATE_REFUSED')
    source = Path(__file__).resolve().parent
    artifacts = [(trusted(UNIT, mode=0o644), source / 'probe.service',
                  'c9a5d7354ce6e1f90a8c1f154651befa4111b1ad7f09c3428ed3c83e6d92d1b2'),
                 (trusted(install / 'probe.py', mode=0o644), source / 'probe.py',
                  '87f6534f324142b30b0c1bb118a929c61e044f678440218517bfee8138fe2bad')]
    for installed, target, legacy in artifacts:
        if hashlib.sha256(installed.read_bytes()).hexdigest() not in (legacy, hashlib.sha256(target.read_bytes()).hexdigest()):
            raise RuntimeError('UNKNOWN_PROBE_VERSION')
    for name in ['result.json', 'host-checkpoint.json']:
        if os.path.lexists(ROOT / name):
            trusted(ROOT / name)
    output = Path('/var/lib/collab-boundary-probe/result.json')
    if os.path.lexists(output):
        trusted(output, uid=uids[2])
    return credential, artifacts


def recover():
    trusted(ROOT, directory=True, mode=0o700)
    # Lock the existing directory: no lock-file replacement and no account recreation.
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        credential, artifacts = recovery_preflight()
        # Preserve the original encrypted dummy; never regenerate identity/custody material.
        dummy = run(['systemd-creds', 'decrypt', '--name=dummy', str(credential), '-']).stdout
        if len(dummy) != 32:
            raise RuntimeError('DUMMY_SIZE_MISMATCH')
        fingerprint = hashlib.sha256(dummy).hexdigest()
        del dummy
        backup = Path(tempfile.mkdtemp(prefix='probe-recovery-', dir=ROOT))
        originals = [p for p, _, _ in artifacts] + [ROOT / 'before.json', credential]
        originals += [p for p in [ROOT / 'result.json', ROOT / 'host-checkpoint.json',
                                  Path('/var/lib/collab-boundary-probe/result.json')] if p.exists()]
        manifest = {}
        for index, original in enumerate(originals):
            data = original.read_bytes()
            name = str(index) + '-' + original.name
            atomic_write(backup / name, data, 0o600)
            manifest[str(original)] = {'backup': name, 'sha256': hashlib.sha256(data).hexdigest()}
        atomic_write(backup / 'manifest.json', json.dumps(manifest, indent=2).encode(), 0o600)
        sync_dir(ROOT)
        for installed, target, _ in artifacts:
            atomic_write(installed, target.read_bytes(), 0o644)
        measure(fingerprint)
    finally:
        os.close(fd)

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Only static codes from this module; never interpolate subprocess output.
        code = str(exc) if type(exc) is RuntimeError and re.fullmatch('[A-Z_]+', str(exc)) else 'SETUP_FAILED'
        print(json.dumps({'passed': False, 'code': code}))
        sys.exit(1)
