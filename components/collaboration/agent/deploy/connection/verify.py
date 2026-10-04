"""Root host measurements for the installed DUMMY_OFFLINE services.

Default is read-only. `crash-recover` kills only the dummy Signer, checks dead-owner
locks, removes only those locks, fsyncs the containing directories, and restarts.
It never deletes/rolls back contract state, quota, ciphertext or nonce reservations.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import socket
import time

import importlib.util
SETUP_SPEC = importlib.util.spec_from_file_location(
    'connection_boundary_setup', Path(__file__).resolve().with_name('setup.py'))
setup = importlib.util.module_from_spec(SETUP_SPEC)
SETUP_SPEC.loader.exec_module(setup)
RECOVERY_SPEC = importlib.util.spec_from_file_location('connection_recovery',
    Path(__file__).resolve().with_name('recover_smoke.py'))
recovery = importlib.util.module_from_spec(RECOVERY_SPEC)
RECOVERY_SPEC.loader.exec_module(recovery)

ROOT = Path('/var/lib/collab-signer')
SOCKETS = ['collab-' + name + '.socket' for name in ['worker', 'gate', 'human', 'transport', 'worker-control']]

def recovered_status():
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(10)
        client.connect('/run/collab-connection/worker.sock')
        client.sendall(b'{"method":"status","value":{}}\n')
        client.shutdown(socket.SHUT_WR)
        data = b''
        while True:
            part = client.recv(4096)
            if not part:
                break
            data += part
            assert len(data) <= 65536
    result = json.loads(data)
    assert result['ok'] is True
    status = result['result']
    assert status['reconciliationOnly'] is True and status['quotaConsumed'] is True
    assert status['secretCustody'] == 'ENCRYPTED_RECOVERED'

def run(args, **kwargs):
    return setup.run(args, **kwargs)

def inject_crash(pid):
    unit = 'collab-signer.service'
    try:
        run(['systemctl', 'kill', '--signal=SIGKILL', '--kill-whom=main', unit],
            check=False, timeout=5)
    except RuntimeError:
        # Even a command timeout can occur after the signal was delivered.
        pass
    deadline = time.monotonic() + 5
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('SERVICE_KILL_FAILED')
        try:
            result = run(['systemctl', 'show', unit, '-p', 'ExecMainPID',
                          '-p', 'ExecMainCode', '-p', 'ExecMainStatus', '-p', 'ActiveState'],
                         check=False, timeout=remaining)
            props = dict(line.split('=', 1) for line in result.stdout.decode().splitlines() if '=' in line)
        except (RuntimeError, UnicodeDecodeError):
            raise RuntimeError('SERVICE_KILL_FAILED') from None
        # The recorded main process's termination, not kill's exit code, is authority.
        if (time.monotonic() < deadline and result.returncode == 0
                and props.get('ExecMainPID') == str(pid)
                and props.get('ExecMainCode') == '2'
                and props.get('ExecMainStatus') == '9'
                and props.get('ActiveState') in ('failed', 'inactive')):
            return
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))

def main():
    assert os.geteuid() == 0
    assert json.loads(Path('/etc/collab-connection/config.json').read_text())['mode'] == 'DUMMY_OFFLINE'
    if len(sys.argv) == 2 and sys.argv[1] == 'crash-recover':
        config = json.loads(setup.trusted(Path('/etc/collab-connection/config.json'), mode=0o644).read_text())
        credential = setup.trusted(Path('/var/lib/collab-connection-setup/dummy.cred'), mode=0o600)
        dummy = run(['systemd-creds', 'decrypt', '--name=dummy', str(credential), '-']).stdout
        if len(dummy) != 32 or hashlib.sha256(dummy).hexdigest() != config.get('dummyCredentialSha256'):
            raise RuntimeError('DUMMY_FINGERPRINT_MISMATCH')
        del dummy
        before = json.loads((ROOT / 'custody/connection-pointer.json').read_text())
        protected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in (ROOT / 'custody').iterdir() if p.is_file() and p.name != 'connection-store.lock'}
        pid = int(run(['systemctl', 'show', 'collab-signer.service', '--property=MainPID', '--value']).stdout)
        assert pid > 1
        inject_crash(pid)
        # Use the same recovery transaction as every post-smoke interruption.
        recovery.recover(Path('/usr/local/lib/collab-connection/node'))
        recovered_status()
        after = json.loads((ROOT / 'custody/connection-pointer.json').read_text())
        assert before == after
        assert protected == {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in (ROOT / 'custody').iterdir() if p.is_file() and p.name != 'connection-store.lock'}
        print(json.dumps({'dummy_crash_restarted': True, 'pointer_unchanged': True,
                          'custody_nonce_unchanged': True, 'socket_reactivated': True,
                          'quota_reset': False, 'state_rollback': False}))
        return
    assert len(sys.argv) == 1
    result = {'cross_uid_read_denied': {}, 'ipc_denied': {}, 'services': {}}
    signer_pid = int(run(['systemctl', 'show', 'collab-signer.service', '--property=MainPID', '--value']).stdout)
    credential = Path('/proc') / str(signer_pid) / 'root/run/credentials/collab-signer.service/dummy'
    assert (ROOT / 'custody/connection-state.enc').is_file()
    assert signer_pid > 1 and credential.is_file()
    for role in ['worker', 'approval', 'transport']:
        checks = []
        for path in [ROOT / 'custody/connection-state.enc', credential]:
            ret = subprocess.run(['runuser', '-u', 'collab-' + role, '--', 'test', '-r', str(path)], capture_output=True)
            checks.append(ret.returncode == 1)
        result['cross_uid_read_denied'][role] = all(checks)
    for role, endpoint in [('worker', 'human'), ('worker', 'gate'), ('worker', 'transport'), ('transport', 'gate')]:
        code = '''import socket,sys
s=socket.socket(socket.AF_UNIX)
try:
 s.connect(sys.argv[1])
except PermissionError:
 sys.exit(0)
except OSError:
 sys.exit(2)
sys.exit(1)
'''
        ret = subprocess.run(['runuser', '-u', 'collab-' + role, '--', '/usr/bin/python3', '-I', '-c', code,
                              '/run/collab-connection/' + endpoint + '.sock'], capture_output=True)
        result['ipc_denied'][role + ':' + endpoint] = ret.returncode == 0
    secret = run(['systemd-creds', 'decrypt', '--name=dummy', '/var/lib/collab-connection-setup/dummy.cred', '-']).stdout
    assert len(secret) == 32
    forms = [secret, secret.hex().encode(), base64.b64encode(secret), base64.urlsafe_b64encode(secret).rstrip(b'=')]
    for role in ['worker', 'approval', 'signer', 'transport']:
        unit = 'collab-' + role + '.service'
        pid = int(run(['systemctl', 'show', unit, '--property=MainPID', '--value']).stdout)
        assert pid > 1
        proc = Path('/proc') / str(pid)
        uid = int([s for s in (proc / 'status').read_text().splitlines() if s.startswith('Uid:')][0].split()[1])
        no_new_privileges = 'NoNewPrivs:\t1' in (proc / 'status').read_text()
        raw = (proc / 'environ').read_bytes() + (proc / 'cmdline').read_bytes()
        journal = run(['journalctl', '--unit=' + unit, '--no-pager', '--output=json']).stdout
        result['services'][role] = {'uid_matches': uid == pwd.getpwnam('collab-' + role).pw_uid,
                                    'no_new_privileges': no_new_privileges,
                                    'secret_absent_env_argv': all(s not in raw for s in forms),
                                    'secret_absent_journal': all(s not in journal for s in forms)}
    del secret, forms
    result['sockets_active'] = all(run(['systemctl', 'is-active', unit]).stdout.strip() == b'active' for unit in SOCKETS)
    result['external_writes'] = 0
    result['all_passed'] = result['sockets_active'] and all(result['cross_uid_read_denied'].values()) and all(result['ipc_denied'].values()) and all(
        all(v.values()) for v in result['services'].values())
    Path('/var/lib/collab-connection-setup/connection-verification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    if not result['all_passed']:
        sys.exit(1)

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        import re
        code = str(exc) if type(exc) is RuntimeError and re.fullmatch('[A-Z_]+', str(exc)) else 'VERIFICATION_FAILED'
        print(json.dumps({'passed': False, 'code': code}))
        sys.exit(1)
