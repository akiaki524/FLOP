"""One-shot Human-run Real credential gate, always write OFF and finally STOPPED.

stdin must be a Human-controlled anonymous pipe containing exactly 32 raw bytes.
This coordinator never reads stdin; only the existing handoff entrypoint does.
No Real secret is needed for offline tests. No automatic rerun or state reset.
"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import pwd
import signal
import socket
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('gate_b_gate_a', HERE / 'gate_a.py')
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
r = g.r
TX = r.ROOT / 'gate-b'
CREDENTIAL = r.ROOT / 'project-seed.cred'
DROPINS = [r.UNITS / ('collab-' + role + '.service.d') / 'gate-b.conf'
           for role in ('signer', 'transport')]
need, run, props, save = g.need, g.run, g.props, g.save


def stop():
    # Ingress first; Transport before Signer. Continue remaining stops on errors.
    errors = []
    for names in (r.SOCKETS, ['collab-transport.service'], ['collab-signer.service'],
                  ['collab-worker.service', 'collab-approval.service']):
        try:
            run(['systemctl', 'stop', *names])
        except Exception:
            errors.append(True)
    need(not errors and all(props(n, 'ActiveState')['ActiveState'] in ('inactive', 'failed')
                            for n in r.NAMES), 'STOP_FAILED')


def anonymous_stdin():
    info = os.fstat(0)
    need(stat.S_ISFIFO(info.st_mode) and
         (info.st_nlink == 0 or re.fullmatch(r'pipe:\[[0-9]+\]', os.readlink('/proc/self/fd/0'))),
         'ANONYMOUS_INPUT_REQUIRED')


def write_off():
    config = json.loads(r.trusted(r.CONFIG, mode=0o644).read_text())
    need(config.get('externalWriteEnabled') is False and
         config.get('mode') == 'REAL_ACCEPT_PREPARATION' and
         config.get('projectDid') == r.PROJECT, 'WRITE_OFF_REQUIRED')


def preflight():
    initial = json.loads(r.trusted(r.CONFIG, mode=0o644).read_text())
    if initial.get('externalWriteEnabled') is True:
        try:
            stop()
        finally:
            remove_credential()
        need(False, 'EXTERNAL_WRITE_ENABLED_STOPPED')
    need(not os.path.lexists(TX), 'GATE_B_TRANSACTION_EXISTS')
    # Reuse Gate A's manifest, fixed units, accounts, runtime pin and state checks.
    # Only its transaction destination differs; do not call Gate A execute/rollback.
    g.TX = TX
    human, sources = g.preflight()
    need(all(props(n, 'ActiveState')['ActiveState'] == 'inactive' for n in r.NAMES),
         'INITIAL_ALL_STOPPED_REQUIRED')
    evidence = json.loads(r.trusted(r.ROOT / 'gate-a/result.json', mode=0o600).read_text())
    need(evidence.get('passed') is True and evidence.get('rollback') == 'DUMMY_CONFIG_ALL_STOPPED',
         'GATE_A_PASS_REQUIRED')
    for path in DROPINS:
        need(not os.path.lexists(path.parent), 'DROPIN_DIRECTORY_EXISTS')
    real_ledger = r.STATE / 'ledger' / ('identity-' + r.sha(r.PROJECT.encode()) + '.json')
    need(not os.path.lexists(real_ledger) and not os.path.lexists(Path(str(real_ledger) + '.lock'))
         and not os.path.lexists(r.STATE / 'real-accept-custody'), 'EXISTING_REAL_STATE_REFUSED')
    return human, sources


def prepare(human, sources):
    TX.mkdir(mode=0o700)
    changes = [(r.DEST / 'src/collaboration_agent' / name, data) for name, data in sources.items()]
    changes += [(r.CONFIG, g.config_bytes(human))]
    changes += [(path, (HERE / 'real-accept' / (role + '.conf.example')).read_bytes())
                for path, role in zip(DROPINS, ('signer', 'transport'))]
    entries = []
    for i, (path, data) in enumerate(changes):
        previous = path.read_bytes() if path.exists() else None
        backup = str(i) + '.before'
        if previous is not None:
            r.atomic_write(TX / backup, previous, 0o600)
        entries.append({'path': str(path), 'backup': backup if previous is not None else None,
                        'before': r.sha(previous) if previous is not None else None, 'after': r.sha(data)})
    save(TX / 'state-before.json', g.state_hashes())
    save(TX / 'transaction.json', {'entries': entries})
    return changes


def remove_credential():
    if os.path.lexists(CREDENTIAL):
        r.trusted(CREDENTIAL, mode=0o600).unlink()
        r.setup.sync_dir(CREDENTIAL.parent)


def rollback():
    # Credential removal precedes file restoration, even if restoration conflicts.
    stop_error = None
    try:
        stop()
    except Exception as exc:
        stop_error = exc
    remove_credential()
    if stop_error:
        raise stop_error
    r.trusted(TX, directory=True, mode=0o700)
    tx = json.loads(r.trusted(TX / 'transaction.json', mode=0o600).read_text())
    for entry in tx['entries']:
        path = Path(entry['path'])
        need(path in [r.CONFIG, *DROPINS] or
             (path.parent == r.DEST / 'src/collaboration_agent' and
              re.fullmatch(r'[a-zA-Z0-9_]+\.(mjs|json)', path.name)), 'ROLLBACK_PATH_REFUSED')
        if os.path.lexists(path):
            need(r.sha(r.trusted(path).read_bytes()) in (entry['before'], entry['after']),
                 'ROLLBACK_FILE_CHANGED')
        if entry['backup'] is not None:
            need(re.fullmatch(r'[0-9]+\.before', entry['backup']), 'BACKUP_PATH_REFUSED')
            data = r.trusted(TX / entry['backup'], mode=0o600).read_bytes()
            need(r.sha(data) == entry['before'], 'BACKUP_CHANGED')
            r.atomic_write(path, data, 0o644)
        elif path.exists():
            path.unlink()
            r.setup.sync_dir(path.parent)
    for path in DROPINS:
        if path.parent.exists():
            path.parent.rmdir()
    run(['systemctl', 'daemon-reload'])
    r.setup.reset_failed(r.NAMES, run)
    stop()
    need(json.loads(r.CONFIG.read_text()).get('mode') == 'DUMMY_OFFLINE', 'RESTORE_CONFIG_FAILED')
    # New Real identity ledger is retained; old ledger/nonce/custody must be unchanged.
    before = json.loads((TX / 'state-before.json').read_text())
    after = g.state_hashes()
    need(all(after.get(name) == digest for name, digest in before.items()), 'EXISTING_STATE_CHANGED')
    save(TX / 'rollback.json', {'passed': True, 'final_state': 'DUMMY_CONFIG_ALL_STOPPED',
                               'real_credential_removed': not CREDENTIAL.exists(),
                               'existing_state_unchanged': True, 'new_real_state_preserved': True,
                               'cryptographic_revocation': False})


def ipc(name, method):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(10)
        client.connect('/run/collab-connection/' + name + '.sock')
        client.sendall((json.dumps({'method': method, 'value': {}}) + '\n').encode())
        client.shutdown(socket.SHUT_WR)
        data = b''
        while True:
            part = client.recv(4096)
            if not part:
                break
            data += part
            need(len(data) <= 65536, 'STATUS_TOO_LARGE')
    reply = json.loads(data)
    need(reply.get('ok') is True, 'STATUS_REFUSED')
    return reply['result']


def observe():
    write_off()
    boundary = ipc('gate', 'boundaryStatus')
    need(boundary.get('did') == r.PROJECT and boundary.get('mode') == 'REAL_ACCEPT_PREPARATION'
         and boundary.get('externalWriteEnabled') is False and boundary.get('inetDenied') is True
         and boundary.get('childDenied') is True
         and boundary.get('uid') == pwd.getpwnam('collab-signer').pw_uid, 'REAL_BOUNDARY_FAILED')
    status = ipc('worker', 'status')
    need(status.get('actions') == [] and status.get('contract') is None and status.get('state') is None
         and status.get('stopped') is False, 'SIGNER_NOT_EMPTY')
    service = props('collab-signer.service', 'ActiveState', 'MainPID', 'InvocationID',
                    'NoNewPrivileges', 'PrivateNetwork', 'RestrictAddressFamilies')
    need(service['ActiveState'] == 'active' and service['NoNewPrivileges'] == 'yes'
         and service['PrivateNetwork'] == 'yes' and service['RestrictAddressFamilies'] == 'AF_UNIX',
         'SIGNER_SERVICE_BOUNDARY_FAILED')
    pid = service['MainPID']
    need(pid.isdecimal() and int(pid) > 1, 'SIGNER_PID_INVALID')
    # Human-executed measurement only. Code travels via pipe; secret never returns.
    probe_source = (HERE / 'gate_a_probe.py').read_text().split("if __name__ == '__main__':")[0]
    probe_source += (HERE / 'gate_b_measure.py').read_text()
    measurement = run(['nsenter', '--target', pid, '--mount', '--', '/usr/bin/python3', '-I', '-',
                       pid, service['InvocationID'],
                       props('collab-transport.service', 'MainPID')['MainPID'],
                       props('collab-transport.service', 'InvocationID')['InvocationID']], input=probe_source.encode())
    measured = json.loads(measurement.stdout)
    checks = ('credential_32_bytes', 'credential_owner_allowed', 'credential_isolated',
              'no_new_privileges', 'seed_absent_env_argv', 'seed_absent_journal')
    need(all(measured.get(key) is True for key in checks), 'CREDENTIAL_MEASUREMENT_FAILED')
    denied = {}
    for role in ('worker', 'approval', 'transport'):
        result = run(['nsenter', '--target', pid, '--mount', '--', 'runuser', '-u', 'collab-' + role,
                      '--', '/usr/bin/python3', '-I', '-c',
                      "import os,sys\ntry:\n fd=os.open('/run/credentials/collab-signer.service/project-seed',os.O_RDONLY)\nexcept PermissionError:\n sys.exit(0)\nos.close(fd)\nsys.exit(1)"], check=False)
        denied[role] = result.returncode == 0
    need(all(denied.values()), 'CROSS_UID_READ_NOT_DENIED')
    need(props('collab-signer.service', 'MainPID', 'InvocationID') ==
         {'MainPID': pid, 'InvocationID': service['InvocationID']}, 'SIGNER_CHANGED_DURING_MEASUREMENT')
    write_off()
    return {'derived_did': boundary['did'], 'boundary': boundary, 'empty_actions': True,
            'service': service, 'measurement': measured, 'cross_uid_read_denied': denied}


def empty_real_ledger():
    path = r.STATE / 'ledger' / ('identity-' + r.sha(r.PROJECT.encode()) + '.json')
    value = json.loads(r.trusted(path, uid=pwd.getpwnam('collab-signer').pw_uid, mode=0o600).read_text())['value']
    need(value.get('did') == r.PROJECT and value.get('quotaConsumed') is False
         and value.get('admission') is None and value.get('state') is None
         and value.get('actions') == [] and not (r.STATE / 'real-accept-custody').exists(),
         'REAL_LEDGER_NOT_EMPTY')
    return {'quota_consumed': False, 'admission': None, 'actions': [], 'custody_created': False}


def execute():
    anonymous_stdin()
    human, sources = preflight()
    changes = prepare(human, sources)
    report = {'passed': False, 'external_writes': 0, 'packet_capture': False,
              'external_write_basis': 'write-off policy, refused send, empty attempt state'}
    try:
        for path, data in changes:
            if path in DROPINS:
                path.parent.mkdir(mode=0o755)
            r.atomic_write(path, data, 0o644)
        write_off()
        # Existing CLI enforces anonymous pipe, 32 bytes, fixed DID, exclusive ciphertext.
        # stdout/stderr stay suppressed; no seed reaches coordinator memory or evidence.
        handoff = subprocess.run([str(r.DEST / 'node'), str(HERE / 'handoff_real.mjs')],
                                 stdin=0, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env={'PATH': '/usr/bin:/bin'}, timeout=120)
        need(handoff.returncode == 0, 'REAL_HANDOFF_FAILED')
        r.trusted(CREDENTIAL, mode=0o600)
        write_off()
        run(['systemd-analyze', 'verify', *[str(r.UNITS / n) for n in r.NAMES]])
        run(['systemctl', 'daemon-reload'])
        r.setup.reset_failed(r.NAMES, run)
        run(['systemctl', 'start', 'collab-worker.socket', 'collab-gate.socket', 'collab-transport.socket'])
        write_off()
        report['transport'] = g.transport_attempt()
        run(['systemctl', 'start', 'collab-signer.service'])
        deadline = time.monotonic() + 10
        while props('collab-signer.service', 'ActiveState')['ActiveState'] == 'activating' and time.monotonic() < deadline:
            time.sleep(.05)
        report['signer'] = observe()
        write_off()
        report['transport_after_signer'] = g.transport_attempt()
        report['real_ledger'] = empty_real_ledger()
        report['passed'] = True
    except Exception:
        # Never serialize arbitrary subprocess output, exception text or journal.
        report['code'] = 'GATE_B_FAILED'
    finally:
        try:
            rollback()
            report['rollback'] = 'DUMMY_CONFIG_ALL_STOPPED'
            report['real_credential_removed'] = True
        except Exception:
            report.update(passed=False, rollback='FAILED_MANUAL_RECOVERY_REQUIRED')
        save(TX / 'result.json', report)
    return report


def interrupted(_signum, _frame):
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    raise RuntimeError('GATE_B_INTERRUPTED')


def main():
    need(os.geteuid() == 0, 'ROOT_REQUIRED')
    need(len(sys.argv) == 2 and sys.argv[1] in ('run', 'rollback'), 'ARGUMENTS_REFUSED')
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    r.trusted(r.ROOT, directory=True, mode=0o700)
    fd = os.open(r.ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, interrupted)
        if sys.argv[1] == 'rollback':
            rollback()
            result = {'passed': True, 'final_state': 'DUMMY_CONFIG_ALL_STOPPED'}
        else:
            result = execute()
        print(json.dumps(result, indent=2))
        return 0 if result['passed'] else 1
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({'passed': False, 'code': 'GATE_B_PREFLIGHT_OR_RECOVERY_FAILED'}))
        sys.exit(1)
