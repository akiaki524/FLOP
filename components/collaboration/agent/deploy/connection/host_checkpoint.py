"""One bounded root entrypoint for setup, dummy IPC smoke, measurements and crash recovery.

Usage: sudo python3 -I deploy/connection/host_checkpoint.py /absolute/path/to/node22
Recovers verified probe or pristine smoke-stage installations; never resets custody.
"""
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
STATE = Path('/var/lib/collab-connection-setup')
DEPLOYMENT = Path('/usr/local/lib/collab-connection')
RESULT_CHECKS = {
    'setup': dict.fromkeys(['credential_read', 'inet_denied', 'fork_denied',
                           'no_new_privileges', 'secret_absent_env', 'secret_absent_argv'], True)
             | {'schema': 2, 'external_writes': 0,
                'cross_uid': {r: True for r in ['collab-worker', 'collab-approval', 'collab-transport']}},
    'recover_smoke': {'recovered': True, 'boundary_remeasured': True,
                      'mode': 'DUMMY_OFFLINE', 'external_writes': 0},
    'install': {'mode': 'DUMMY_OFFLINE', 'external_writes': 0,
                'installed': ['collab-worker.socket', 'collab-gate.socket', 'collab-human.socket',
                              'collab-transport.socket', 'collab-worker-control.socket',
                              'collab-worker.service', 'collab-signer.service',
                              'collab-approval.service', 'collab-transport.service']},
    'smoke': {'passed': 13, 'failed': 0, 'externalWrites': 0, 'realSecrets': False},
    'recovery_smoke': {'passed': 3, 'failed': 0, 'externalWrites': 0, 'realSecrets': False},
    'permissions': {'all_passed': True, 'external_writes': 0, 'sockets_active': True,
                    'cross_uid_read_denied': dict.fromkeys(['worker', 'approval', 'transport'], True),
                    'ipc_denied': dict.fromkeys(['worker:human', 'worker:gate', 'worker:transport', 'transport:gate'], True),
                    'services': {r: dict.fromkeys(['uid_matches', 'no_new_privileges',
                                                   'secret_absent_env_argv', 'secret_absent_journal'], True)
                                 for r in ['worker', 'approval', 'signer', 'transport']}},
    'crash_recover': {'dummy_crash_restarted': True, 'pointer_unchanged': True,
                      'custody_nonce_unchanged': True, 'socket_reactivated': True,
                      'quota_reset': False, 'state_rollback': False},
}

def valid_result(value, expected):
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        return all(key in value and valid_result(value[key], item) for key, item in expected.items())
    return value == expected

def signer_diagnostic(since_ns):
    # Only the dedicated static diagnostic, never journal/stderr or state material.
    import stat
    path = Path('/var/lib/collab-signer/startup-diagnostic.json')
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != pwd.getpwnam('collab-signer').pw_uid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 2048 or info.st_mtime_ns < since_ns):
            return {'available': False}
        value = json.loads(path.read_text())
        phases = {'ENTRY', 'CONFIG', 'SOCKET_ACTIVATION', 'CREDENTIAL', 'OFFICIAL_RUNTIME',
                  'PROCESS_BOUNDARY', 'IDENTITY', 'LEDGER', 'CUSTODY', 'SIGNER_INIT', 'LISTEN', 'RUNNING'}
        codes = {'ERR_ACCESS_DENIED', 'EACCES', 'EPERM', 'ENOENT', 'EINVAL', 'EBADF', 'EAFNOSUPPORT',
                 'SOCKET_ACTIVATION_REQUIRED', 'DUMMY_CREDENTIAL_MISMATCH', 'OFFICIAL_RUNTIME_UNAVAILABLE',
                 'OS_BOUNDARY_REQUIRED', 'REAL_MODE_DISABLED', 'REAL_CREDENTIAL_REJECTED',
                 'LEDGER_LOCKED_RECONCILIATION_REQUIRED', 'LEDGER_CORRUPT',
                 'ORPHAN_CUSTODY_RECONCILIATION_REQUIRED', 'SIGNER_STARTUP_FAILED'}
        if value.get('schema') != 1 or value.get('phase') not in phases or value.get('code') not in codes:
            return {'available': False}
        return {'available': True, 'phase': value['phase'], 'code': value['code']}
    except Exception:
        return {'available': False}

def call(args, cwd=None, kind=None):
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True)
    except OSError:
        raise RuntimeError('STEP_LAUNCH_FAILED') from None
    try:
        summary = json.loads(result.stdout)
    except Exception:
        raise RuntimeError('NO_JSON_RESULT') from None
    if not isinstance(summary, dict):
        raise RuntimeError('INVALID_STEP_RESULT')
    if result.returncode:
        code = summary.get('code', 'STEP_FAILED') if isinstance(summary, dict) else 'STEP_FAILED'
        raise RuntimeError(code if isinstance(code, str) and re.fullmatch('[A-Z_]+', code) else 'STEP_FAILED')
    if (kind not in RESULT_CHECKS or not valid_result(summary, RESULT_CHECKS[kind])
            or summary.get('passed') is False):
        raise RuntimeError('STEP_RESULT_INVALID')
    return summary

def main():
    assert os.geteuid() == 0 and len(sys.argv) == 2
    node = str(Path(sys.argv[1]).resolve(strict=True))
    human = pwd.getpwuid(int(os.environ['SUDO_UID'])).pw_name
    assert human != 'root'
    report = {'mode': 'DUMMY_OFFLINE', 'real_secret_access': False, 'external_writes': 0}
    evidence_name = 'host-checkpoint-' + uuid.uuid4().hex + '.json'
    report['evidence_file'] = '.local/connection/' + evidence_name
    step = 'setup'
    started_ns = time.time_ns()
    try:
        if DEPLOYMENT.exists():
            step = 'recover_smoke'
            report['setup'] = call(['/usr/bin/python3', '-I', str(HERE / 'recover_smoke.py'), node], kind='recover_smoke')
        else:
            if STATE.exists():
                report['setup'] = call(['/usr/bin/python3', '-I', str(HERE / 'setup.py'), '--recover-probe'], kind='setup')
            else:
                report['setup'] = call(['/usr/bin/python3', '-I', str(HERE / 'setup.py')], kind='setup')
            step = 'install'
            report['install'] = call(['/usr/bin/python3', '-I', str(HERE / 'install.py'), node], kind='install')
        resumed = report['setup'].get('state_mode') == 'reconciliation'
        if resumed:
            step = 'recovery_smoke'
            if report['setup'].get('quota_consumed') is not True:
                raise RuntimeError('RECOVERY_UNADMITTED_PRESERVED')
            report['recovery'] = call(['runuser', '-u', human, '--', node,
                                     str(REPO / 'tests/connection_smoke.mjs'), 'recovery'],
                                     REPO, kind='recovery_smoke')
        step = 'smoke'
        if not resumed:
            report['smoke'] = call(['runuser', '-u', human, '--', node, str(REPO / 'tests/connection_smoke.mjs')], REPO, kind='smoke')
        step = 'permissions'
        report['permissions'] = call(['/usr/bin/python3', '-I', str(HERE / 'verify.py')], kind='permissions')
        step = 'crash_recover'
        report['crash'] = call(['/usr/bin/python3', '-I', str(HERE / 'verify.py'), 'crash-recover'], kind='crash_recover')
        step = 'recovery_smoke'
        report['recovery'] = call(['runuser', '-u', human, '--', node, str(REPO / 'tests/connection_smoke.mjs'), 'recovery'], REPO, kind='recovery_smoke')
        report['passed'] = True
    except Exception as exc:
        if type(exc) is RuntimeError and re.fullmatch('[A-Z_]+', str(exc)):
            report['failure_code'] = str(exc)
        report.setdefault('failure_code', 'CHECKPOINT_STEP_FAILED')
        report['passed'] = False
        report['failed_step'] = step
        if step in ('smoke', 'recovery_smoke', 'crash_recover'):
            report['signer_diagnostic'] = signer_diagnostic(started_ns)
    if 'setup' in report:
        with (STATE / evidence_name).open('x') as stream:
            stream.write(json.dumps(report, indent=2) + '\n')
    # Only sanitized JSON from the dedicated harnesses; raw stderr is never printed or saved.
    data = json.dumps(report, indent=2) + '\n'
    save = '''import pathlib,sys
p=pathlib.Path('.local/connection') / sys.argv[1]
p.parent.mkdir(parents=True, exist_ok=True)
with p.open('x') as f: f.write(sys.stdin.read())
'''
    subprocess.run(['runuser', '-u', human, '--', '/usr/bin/python3', '-I', '-c', save, evidence_name],
                   cwd=REPO, input=data.encode(), capture_output=True, check=False)
    print(data, end='')
    return 0 if report['passed'] else 1

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print('{"passed":false,"code":"HOST_CHECKPOINT_FAILED"}')
        sys.exit(1)
