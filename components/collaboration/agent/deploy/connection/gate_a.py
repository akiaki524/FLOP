"""Reversible existing-host Gate A; fake credential only, external write OFF.

sudo python3 -I deploy/connection/gate_a.py run
sudo python3 -I deploy/connection/gate_a.py rollback
sudo python3 -I deploy/connection/gate_a.py rerun   # archive a rolled-back attempt, then run

No boot enablement. Final state is original DUMMY files with ALL ingress/services
STOPPED. Root-only durable backups allow rollback after process interruption.
Existing state and evidence are preserved. Never consumes project-seed.cred.
"""
import base64
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('gate_a_recovery', HERE / 'recover_smoke.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
REPO = HERE.parents[1]
TX = r.ROOT / 'gate-a'
FAKE = r.ROOT / 'gate-a-fake.cred'
PROBE = r.DEST / 'gate_a_probe.py'
PROBE_RESULT = r.STATE / 'gate-a-probe.json'
PROBE_TEMP = r.STATE / 'gate-a-probe.pending'
DIAGNOSTIC = r.STATE / 'startup-diagnostic.json'
DROPINS = [r.UNITS / ('collab-' + role + '.service.d') / 'gate-a.conf'
           for role in ('signer', 'transport')]


def need(value, code):
    if not value:
        raise RuntimeError(code)


def run(args, **kwargs):
    kwargs.setdefault('timeout', 30)
    return r.run(args, **kwargs)


def props(unit, *names):
    result = run(['systemctl', 'show', unit, *['--property=' + n for n in names]])
    return dict(line.split('=', 1) for line in result.stdout.decode().splitlines() if '=' in line)


def save(path, value):
    r.atomic_write(path, (json.dumps(value, indent=2) + '\n').encode(), 0o600)


def stop():
    run(['systemctl', 'stop', *r.SOCKETS])
    run(['systemctl', 'stop', *r.SERVICES])
    need(all(props(n, 'ActiveState')['ActiveState'] in ('inactive', 'failed') for n in r.NAMES),
         'UNITS_NOT_STOPPED')


def state_hashes():
    # Contents never leave the process; evidence stores hashes only.
    result = {}
    for path in r.STATE.rglob('*'):
        need(not path.is_symlink(), 'STATE_SYMLINK_REFUSED')
        if path.is_file() and path not in (DIAGNOSTIC, PROBE_RESULT, PROBE_TEMP):
            result[str(path.relative_to(r.STATE))] = r.sha(path.read_bytes())
    return result


def config_bytes(human):
    value = json.loads((HERE / 'real-accept/config.json.example').read_text())
    need(value == {'mode': 'REAL_ACCEPT_PREPARATION', 'projectDid': r.PROJECT,
                   'humanUid': 'REPLACE_WITH_APPROVED_HUMAN_UID', 'externalWriteEnabled': False},
         'REAL_CONFIG_REFUSED')
    value['humanUid'] = human
    return (json.dumps(value) + '\n').encode()


def dropin_bytes():
    signer = (HERE / 'real-accept/signer.conf.example').read_text().replace(
        '/var/lib/collab-connection-setup/project-seed.cred', str(FAKE))
    signer += '\nExecStartPre=/usr/bin/python3 -I ' + str(PROBE) + '\n'
    transport = (HERE / 'real-accept/transport.conf.example').read_bytes()
    return [signer.encode(), transport]


def preflight():
    need(not os.path.lexists(TX), 'TRANSACTION_EXISTS_USE_ROLLBACK')
    need(not os.path.lexists(FAKE) and not os.path.lexists(r.ROOT / 'project-seed.cred'),
         'CREDENTIAL_DESTINATION_EXISTS')
    need(not os.path.lexists(PROBE) and not os.path.lexists(PROBE_RESULT) and not os.path.lexists(PROBE_TEMP), 'PROBE_EXISTS')
    r.trusted(r.DEST, directory=True, mode=0o755)
    r.trusted(r.CONFIG.parent, directory=True, mode=0o755)
    r.trusted(r.UNITS, directory=True)
    roles = r.account_preflight()
    config = json.loads(r.trusted(r.CONFIG, mode=0o644).read_text())
    human = config.get('humanUid')
    need(type(human) is int and human > 0 and os.environ.get('SUDO_UID') == str(human),
         'HUMAN_UID_MISMATCH')
    r.config_preflight(human, config.get('dummyCredentialSha256'))
    r.unit_preflight(human)
    need(all(props(n, 'UnitFileState').get('UnitFileState') in ('static', 'disabled')
             for n in r.NAMES), 'BOOT_ENABLED_UNIT_REFUSED')
    r.inspect_state(roles['collab-signer'])
    for path in DROPINS:
        need(not os.path.lexists(path.parent), 'DROPIN_DIRECTORY_EXISTS')
    # Verify installed root-owned files against their installation manifest and
    # the pinned official runtime. No real credential is decrypted or inspected.
    manifest = json.loads(r.trusted(r.DEPLOYMENT).read_text())
    need(bool(manifest), 'EMPTY_MANIFEST')
    for relative, digest in manifest.items():
        need(re.fullmatch(r'src/collaboration_agent/[a-zA-Z0-9_]+\.(mjs|json)', relative),
             'MANIFEST_PATH_REFUSED')
        need(r.sha(r.trusted(r.DEST / relative, mode=0o644).read_bytes()) == digest, 'MANIFEST_MISMATCH')
    sources = {p.name: p.read_bytes() for p in r.SOURCE.iterdir()
               if p.suffix == '.mjs' or p.name == 'tclk_pin.json'}
    need(not any(p.is_symlink() for p in r.SOURCE.iterdir()), 'SOURCE_SYMLINK_REFUSED')
    pin = json.loads(sources['tclk_pin.json'])
    need(sources['tclk_pin.json'] == r.trusted(r.DEST / 'src/collaboration_agent/tclk_pin.json').read_bytes(),
         'PIN_CHANGED')
    runtime_paths = set()
    for relative, digest in pin['files'].items():
        path = Path('.local/batch16/official-runtime') / relative
        need(not Path(relative).is_absolute() and '..' not in Path(relative).parts, 'PIN_PATH_REFUSED')
        need(r.sha(r.trusted(r.DEST / path).read_bytes()) == digest, 'RUNTIME_PIN_MISMATCH')
        runtime_paths.add(path)
    r.validate_tree({Path('node'), *runtime_paths, *(Path(p) for p in manifest)})
    need(run([str(r.DEST / 'node'), '--version']).stdout.startswith(b'v22.'), 'NODE22_REQUIRED')
    transport_state = Path('/var/lib/collab-transport')
    if os.path.lexists(transport_state):
        r.trusted(transport_state, uid=roles['collab-transport'], directory=True, mode=0o700)
        need(not list(transport_state.iterdir()), 'TRANSPORT_STATE_NOT_EMPTY')
    config_bytes(human)
    need(r.ROOT.stat().st_dev == r.CONFIG.parent.stat().st_dev == r.DEST.stat().st_dev == r.UNITS.stat().st_dev,
         'UPDATE_FILESYSTEM_MISMATCH')
    return human, sources


def prepare(human, sources):
    TX.mkdir(mode=0o700)
    # The complete recovery manifest is durable BEFORE any live-file mutation.
    changes = [(r.DEST / 'src/collaboration_agent' / n, data) for n, data in sources.items()]
    changes += [(r.CONFIG, config_bytes(human)), (PROBE, (HERE / 'gate_a_probe.py').read_bytes())]
    changes += list(zip(DROPINS, dropin_bytes()))
    entries = []
    for index, (path, data) in enumerate(changes):
        previous = path.read_bytes() if path.exists() else None
        backup = str(index) + '.before'
        if previous is not None:
            r.atomic_write(TX / backup, previous, 0o600)
        entries.append({'path': str(path), 'backup': backup if previous is not None else None,
                        'before_sha256': r.sha(previous) if previous is not None else None,
                        'after_sha256': r.sha(data)})
    if DIAGNOSTIC.exists():
        r.atomic_write(TX / 'diagnostic.before', DIAGNOSTIC.read_bytes(), 0o600)
    save(TX / 'transaction.json', {'entries': entries})
    return changes


def rollback():
    r.trusted(TX, directory=True, mode=0o700)
    tx = json.loads(r.trusted(TX / 'transaction.json', mode=0o600).read_text())
    stop()
    # Reject unexpected edits rather than overwrite them. Root-owned transaction
    # paths must still be within the exact known deployment destinations.
    allowed = {str(r.CONFIG), str(PROBE), *(str(p) for p in DROPINS)}
    for entry in tx['entries']:
        path = Path(entry['path'])
        need(str(path) in allowed or (path.parent == r.DEST / 'src/collaboration_agent'
             and re.fullmatch(r'[a-zA-Z0-9_]+\.(mjs|json)', path.name)), 'ROLLBACK_PATH_REFUSED')
        if path.exists():
            actual = r.sha(r.trusted(path).read_bytes())
            need(actual in (entry['before_sha256'], entry['after_sha256']), 'ROLLBACK_FILE_CHANGED')
        if entry['backup'] is not None:
            need(re.fullmatch(r'[0-9]+\.before', entry['backup']), 'BACKUP_PATH_REFUSED')
            data = r.trusted(TX / entry['backup'], mode=0o600).read_bytes()
            need(r.sha(data) == entry['before_sha256'], 'BACKUP_CHANGED')
            r.atomic_write(path, data, 0o644)
        elif path.exists():
            path.unlink()
            r.setup.sync_dir(path.parent)
    for path in DROPINS:
        if path.parent.exists():
            path.parent.rmdir()  # Only our now-empty directory; never recursive.
    if FAKE.exists():
        r.trusted(FAKE, mode=0o600).unlink()
        r.setup.sync_dir(FAKE.parent)
    for path in (PROBE_RESULT, PROBE_TEMP, DIAGNOSTIC):
        if path.exists():
            # Preserve the observed diagnostic, never discard historical evidence.
            target = TX / (path.name + '.after')
            if not target.exists():
                r.atomic_write(target, path.read_bytes(), 0o600)
            path.unlink()
    old = TX / 'diagnostic.before'
    if old.exists():
        r.atomic_write(DIAGNOSTIC, old.read_bytes(), 0o600)
        os.chown(DIAGNOSTIC, pwd.getpwnam('collab-signer').pw_uid, pwd.getpwnam('collab-signer').pw_gid)
    run(['systemctl', 'daemon-reload'])
    r.setup.reset_failed(r.NAMES, run)
    stop()
    need(json.loads(r.CONFIG.read_text())['mode'] == 'DUMMY_OFFLINE', 'RESTORE_CONFIG_FAILED')
    before = TX / 'state-before.json'
    if before.exists():
        need(json.loads(before.read_text()) == state_hashes(), 'STATE_CHANGED')
    need(not FAKE.exists() and not any(p.exists() for p in DROPINS), 'CLEANUP_FAILED')
    save(TX / 'rollback.json', {'passed': True, 'final_state': 'DUMMY_CONFIG_ALL_STOPPED',
                               'state_rollback': False, 'fake_credential_removed': True})


def archive_completed():
    """Keep a finished, rolled-back attempt as evidence; never delete or edit it."""
    r.trusted(TX, directory=True, mode=0o700)
    need((TX / 'rollback.json').is_file(), 'TRANSACTION_NOT_ROLLED_BACK')
    done = json.loads(r.trusted(TX / 'rollback.json', mode=0o600).read_text())
    need(done.get('passed') is True and done.get('final_state') == 'DUMMY_CONFIG_ALL_STOPPED'
         and (TX / 'result.json').is_file(), 'TRANSACTION_NOT_ROLLED_BACK')
    need(not os.path.lexists(FAKE) and not os.path.lexists(PROBE)
         and not any(os.path.lexists(p) for p in DROPINS), 'CLEANUP_INCOMPLETE')
    index = 1
    while os.path.lexists(r.ROOT / ('gate-a-attempt-' + str(index))):
        index += 1
    os.rename(TX, r.ROOT / ('gate-a-attempt-' + str(index)))
    r.setup.sync_dir(r.ROOT)


def signer_attempt(index):
    if PROBE_RESULT.exists():
        r.atomic_write(TX / ('probe-' + str(index - 1) + '.json'), PROBE_RESULT.read_bytes(), 0o600)
        PROBE_RESULT.unlink()
    invocation = props('collab-signer.service', 'InvocationID').get('InvocationID')
    process = subprocess.Popen(['systemctl', 'start', 'collab-signer.service'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while not PROBE_RESULT.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        need(PROBE_RESULT.exists(), 'CREDENTIAL_PROBE_NOT_REACHED')
        probe = json.loads(PROBE_RESULT.read_text())
        required = {'fake_credential_exact', 'credential_32_bytes', 'uid_matches',
                    'credential_owner_allowed', 'credential_other_permissions_absent',
                    'no_new_privileges', 'seed_absent_env_argv'}
        need(set(probe) == required | {'pid', 'invocation_id', 'credential_metadata'}
             and all(probe[k] is True for k in required),
             'CREDENTIAL_PROBE_FAILED')
        need(probe['invocation_id'] != invocation, 'STALE_INVOCATION')
        pid = probe['pid']
        need(props('collab-signer.service', 'ControlPID')['ControlPID'] == str(pid), 'PROBE_PID_MISMATCH')
        denied = {}
        for role in ('worker', 'approval', 'transport'):
            # Enter the credential mount namespace as root, then drop to each UID;
            # thus denial is credential DAC, not merely /proc traversal denial.
            result = run(['nsenter', '--target', str(pid), '--mount', '--', 'runuser', '-u',
                'collab-' + role, '--', '/usr/bin/python3', '-I', '-c',
                "import sys\ntry:\n open('/run/credentials/collab-signer.service/project-seed','rb').read()\nexcept PermissionError:\n sys.exit(0)\nsys.exit(1)"], check=False)
            denied[role] = result.returncode == 0
        need(all(denied.values()), 'CROSS_UID_READ_NOT_DENIED')
        process.wait(timeout=15)
        deadline = time.monotonic() + 10
        while props('collab-signer.service', 'ActiveState')['ActiveState'] not in ('failed', 'inactive') and time.monotonic() < deadline:
            time.sleep(.05)
        observed = props('collab-signer.service', 'InvocationID', 'ActiveState', 'ExecMainStatus')
        diagnostic = json.loads(DIAGNOSTIC.read_text())
        need(observed['ActiveState'] == 'failed' and observed['ExecMainStatus'] == '70', 'SIGNER_NOT_FAILED_CLOSED')
        need(diagnostic.get('phase') == 'CREDENTIAL' and diagnostic.get('code') == 'REAL_CREDENTIAL_DID_MISMATCH'
             and diagnostic.get('invocationId') == probe['invocation_id'] == observed['InvocationID'],
             'EXPECTED_DID_MISMATCH_NOT_OBSERVED')
        result = {'probe': probe, 'cross_uid_read_denied': denied,
                  'diagnostic': diagnostic, 'service': observed}
        save(TX / ('signer-' + str(index) + '.json'), result)
        return result
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def transport_attempt():
    request = '''import json,socket
s=socket.socket(socket.AF_UNIX);s.settimeout(10)
s.connect('/run/collab-connection/transport.sock')
s.sendall(b'{"operation":"sendAccept","value":{}}\\n');s.shutdown(socket.SHUT_WR)
b=b''
while True:
 c=s.recv(4096)
 if not c: break
 b+=c
 if len(b)>65536: raise RuntimeError()
assert json.loads(b)=={'ok':False,'code':'CONNECTION_REQUEST_DENIED'}
'''
    run(['runuser', '-u', 'collab-signer', '--', '/usr/bin/python3', '-I', '-c', request])
    unit = props('collab-transport.service', 'ActiveState', 'MainPID', 'NoNewPrivileges', 'Restart')
    need(unit['ActiveState'] == 'active' and unit['NoNewPrivileges'] == 'yes' and unit['Restart'] == 'no',
         'TRANSPORT_START_FAILED')
    proc = Path('/proc') / unit['MainPID']
    need(int(next(s for s in (proc / 'status').read_text().splitlines() if s.startswith('Uid:')).split()[1])
         == pwd.getpwnam('collab-transport').pw_uid, 'TRANSPORT_UID_MISMATCH')
    state = Path('/var/lib/collab-transport')
    r.trusted(state, uid=pwd.getpwnam('collab-transport').pw_uid, directory=True, mode=0o700)
    need(not list(state.iterdir()), 'TRANSPORT_ATTEMPT_STATE_PRESENT')
    return {'socket_activation': True, 'send_refused': True, 'state_empty': True, 'service': unit}


def execute():
    human, sources = preflight()
    changes = prepare(human, sources)
    report = {'passed': False, 'real_secret_access': 0, 'external_writes': 0,
              'external_write_basis': 'disabled policy before handoff validation; empty transport attempt state',
              'packet_capture': False}
    try:
        stop()
        save(TX / 'state-before.json', state_hashes())
        for path, data in changes:
            if path in DROPINS:
                path.parent.mkdir(mode=0o755)
            r.atomic_write(path, data, 0o644)
        # Fixed public bytes go to systemd-creds via anonymous stdin pipe only.
        encrypted = run(['systemd-creds', 'encrypt', '--with-key=host', '--name=project-seed', '-', '-'],
                        input=bytes(range(32))).stdout
        need(bool(encrypted), 'ENCRYPT_FAILED')
        with FAKE.open('xb') as stream:
            os.chmod(FAKE, 0o600)
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        r.setup.sync_dir(FAKE.parent)
        run(['systemd-analyze', 'verify', *[str(r.UNITS / n) for n in r.NAMES]])
        run(['systemctl', 'daemon-reload'])
        r.setup.reset_failed(r.NAMES, run)
        run(['systemctl', 'start', 'collab-worker.socket', 'collab-gate.socket', 'collab-transport.socket'])
        for name, role in [('worker', 'worker'), ('gate', 'approval'), ('transport', 'signer')]:
            info = (Path('/run/collab-connection') / (name + '.sock')).stat()
            need(stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o660
                 and info.st_uid == 0 and info.st_gid == pwd.getpwnam('collab-' + role).pw_gid,
                 'SOCKET_PERMISSION_MISMATCH')
        report['socket_permissions_verified'] = True
        report['transport'] = transport_attempt()
        report['signer_attempts'] = [signer_attempt(1)]
        need(state_hashes() == json.loads((TX / 'state-before.json').read_text()), 'STATE_CHANGED')
        r.setup.reset_failed(['collab-signer.service'], run)
        report['signer_attempts'].append(signer_attempt(2))  # Reset/restart after real failure.
        run(['systemctl', 'restart', 'collab-transport.service'])
        report['transport_recovery'] = transport_attempt()
        seed = bytes(range(32))
        forms = (seed, seed.hex().encode(), base64.b64encode(seed), base64.urlsafe_b64encode(seed).rstrip(b'='))
        for role in ('signer', 'transport'):
            journal = run(['journalctl', '--unit=collab-' + role + '.service', '--no-pager', '--output=json']).stdout
            need(all(value not in journal for value in forms), 'JOURNAL_EXPOSURE')
        proc = Path('/proc') / props('collab-transport.service', 'MainPID')['MainPID']
        need(all(value not in (proc / 'environ').read_bytes() + (proc / 'cmdline').read_bytes() for value in forms),
             'TRANSPORT_METADATA_EXPOSURE')
        report.update(passed=True, expected_did_mismatch=True, recovery=True, journal_seed_absent=True)
    except Exception as exc:
        report['code'] = str(exc) if type(exc) is RuntimeError and re.fullmatch('[A-Z_]+', str(exc)) else 'GATE_A_FAILED'
    finally:
        try:
            rollback()
            report['rollback'] = 'DUMMY_CONFIG_ALL_STOPPED'
            report['state_unchanged'] = (TX / 'state-before.json').exists()
            report['fake_credential_removed'] = True
        except Exception:
            report.update(passed=False, rollback='FAILED_MANUAL_RECOVERY_REQUIRED')
        save(TX / 'result.json', report)
    return report


def main():
    need(os.geteuid() == 0, 'ROOT_REQUIRED')
    need(len(sys.argv) == 2 and sys.argv[1] in ('run', 'rerun', 'rollback'), 'ARGUMENTS_REFUSED')
    r.trusted(r.ROOT, directory=True, mode=0o700)
    fd = os.open(r.ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if sys.argv[1] == 'rollback':
            rollback()
            result = {'passed': True, 'final_state': 'DUMMY_CONFIG_ALL_STOPPED'}
        else:
            if sys.argv[1] == 'rerun':
                archive_completed()
            result = execute()
        print(json.dumps(result, indent=2))
        return 0 if result['passed'] else 1
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        code = str(exc) if type(exc) is RuntimeError and re.fullmatch('[A-Z_]+', str(exc)) else 'GATE_A_FAILED'
        print(json.dumps({'passed': False, 'code': code}))
        sys.exit(1)
