"""Preserving recovery for pristine and post-smoke DUMMY_OFFLINE deployments.

This updater accepts only the c608daf installation or a forward, interrupted
update to the current reviewed source.  It preserves credentials and signer
evidence, resumes initialized state only through signer validation in reconciliation
mode, and never contacts a network service.
"""
import fcntl
import grp
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

SETUP_SPEC = importlib.util.spec_from_file_location(
    'connection_boundary_setup', Path(__file__).resolve().with_name('setup.py'))
setup = importlib.util.module_from_spec(SETUP_SPEC)
SETUP_SPEC.loader.exec_module(setup)

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / 'src/collaboration_agent'
RUNTIME = REPO / '.local/batch16/official-runtime'
DEST = Path('/usr/local/lib/collab-connection')
CONFIG = Path('/etc/collab-connection/config.json')
UNITS = Path('/etc/systemd/system')
ALT_UNITS = [Path('/run/systemd/system'), Path('/usr/lib/systemd/system')]
STATE = Path('/var/lib/collab-signer')
ROOT = Path('/var/lib/collab-connection-setup')
DEPLOYMENT = ROOT / 'deployment.json'
PROBE_INSTALL = Path('/usr/local/lib/collab-boundary-probe/probe.py')
PROBE_UNIT = Path('/etc/systemd/system/collab-boundary-probe.service')
PROJECT = 'did:key:z6MkjQVeF7TCAC6H6LKQnYFannWhsfWzzHhDDr3UhbZgHSTL'
ROLES = setup.ROLES
NAMES = ['collab-worker.socket', 'collab-gate.socket', 'collab-human.socket',
         'collab-transport.socket', 'collab-worker-control.socket',
         'collab-worker.service', 'collab-signer.service',
         'collab-approval.service', 'collab-transport.service']
SOCKETS = [name for name in NAMES if name.endswith('.socket')]
SERVICES = [name for name in NAMES if name.endswith('.service')]

# Immutable source digests from the installation baseline c608daf.
BASELINE = {
    'connection_approval.mjs': '5d33c08af8bc65a3e4b02b458b2fa2246ec094d5f2ecb30d91d8225d4366eda8',
    'connection_runtime.mjs': 'e57add4f5f619fd9e8dbc86c89f409b5dcf4f6b2fc74219659e7ce0614423af7',
    'connection_services.mjs': 'b7a689e88d52f0dd23eab0315416938f86e2eeb610eaaf8939672e40f41c3c9a',
    'connection_store.mjs': '637e716d83890bb8b5ba1ebeb3c7a14c175c6c6ce74d5f0f3d16972636282843',
    'connection_terminal.mjs': '8745246ca1c328f3bc1be215b95d3f66f2cfd9fe791584c69c52d4f29173d7b9',
    'pilot_approval.mjs': '67ee0ba823e49ae0ec9ede2ba6e79ce523ccea2c0c9b4bab2d10c2e2c074dfa3',
    'pilot_client.mjs': 'a21c4d3d4984d43665de1c6d0ac3918cefebed64a8e184237ff2d8dc82e96eb4',
    'pilot_ledger.mjs': '8d71f03bb5c039d7044bb54b1bacc03994aa5d27edafcff88a5569635ba47c77',
    'pilot_protocol.mjs': '985044e000d79f4fd88c33962e6e5d435a21c90ddcec4e0c947f4b307779d76e',
    'pilot_signer.mjs': '1127d253d4d8ea299ea0ec2885e430c3d11ee8fe145584808ceadf18a6aebbd3',
    'pilot_worker.mjs': 'a4d5d8e319f5cb644212de4d9e3f2313a8c108ba8ffbf440eb45d686d8f3a3df',
    'tclk_pin.json': '66f653878691106650ae5e98638b3ed9fa1546c2fd7efc0665b3a3050c49e6f8',
}
PHASES = {'ENTRY', 'CONFIG', 'SOCKET_ACTIVATION', 'CREDENTIAL', 'OFFICIAL_RUNTIME',
          'PROCESS_BOUNDARY', 'IDENTITY', 'LEDGER', 'CUSTODY', 'SIGNER_INIT',
          'LISTEN', 'RUNNING'}
CODES = {'ERR_ACCESS_DENIED', 'EACCES', 'EPERM', 'ENOENT', 'EINVAL', 'EBADF',
         'EAFNOSUPPORT', 'SOCKET_ACTIVATION_REQUIRED', 'DUMMY_CREDENTIAL_MISMATCH',
         'OFFICIAL_RUNTIME_UNAVAILABLE', 'OS_BOUNDARY_REQUIRED', 'REAL_MODE_DISABLED',
         'REAL_CREDENTIAL_REJECTED', 'LEDGER_LOCKED_RECONCILIATION_REQUIRED',
         'LEDGER_CORRUPT', 'ORPHAN_CUSTODY_RECONCILIATION_REQUIRED',
         'SIGNER_STARTUP_FAILED'}


def run(args, **kwargs):
    return setup.run(args, **kwargs)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def trusted(path, uid=0, directory=False, mode=None):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
        raise RuntimeError('UNTRUSTED_DEPLOYMENT_PATH')
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise RuntimeError('DEPLOYMENT_MODE_MISMATCH')
    return path


def account_preflight():
    before = json.loads(trusted(ROOT / 'before.json').read_text())
    if before.get('roles_existed') is not False or before.get('unit_existed') is not False \
            or before.get('created_users') != ROLES:
        raise RuntimeError('SETUP_PROVENANCE_REFUSED')
    uids = []
    for role in ROLES:
        account, group = pwd.getpwnam(role), grp.getgrnam(role)
        if account.pw_uid == 0 or account.pw_gid != group.gr_gid \
                or account.pw_shell != '/usr/sbin/nologin' or group.gr_mem:
            raise RuntimeError('ACCOUNT_MISMATCH')
        if any(g.gr_gid != group.gr_gid and role in g.gr_mem for g in grp.getgrall()):
            raise RuntimeError('SUPPLEMENTARY_GROUP_REFUSED')
        if any(p.pw_name != role and (p.pw_gid == group.gr_gid or p.pw_uid == account.pw_uid)
               for p in pwd.getpwall()):
            raise RuntimeError('SHARED_ACCOUNT_REFUSED')
        uids.append(account.pw_uid)
    if len(set(uids)) != len(ROLES):
        raise RuntimeError('SHARED_UID_REFUSED')
    return dict(zip(ROLES, uids))


def render_units(human):
    rendered = {}
    sockets = [('worker', 'signer', 'worker', 'root', 'collab-worker', '0660'),
               ('gate', 'signer', 'gate', 'root', 'collab-approval', '0660'),
               ('human', 'approval', 'human', str(human), 'root', '0600'),
               ('transport', 'transport', 'transport', 'root', 'collab-signer', '0660'),
               ('worker-control', 'worker', 'worker-control', str(human), 'root', '0600')]
    for name, service, fdname, owner, group, mode in sockets:
        rendered['collab-' + name + '.socket'] = f'''[Unit]
Description=Collaboration {name} restricted IPC
[Socket]
ListenStream=/run/collab-connection/{name}.sock
DirectoryMode=0755
SocketUser={owner}
SocketGroup={group}
SocketMode={mode}
FileDescriptorName={fdname}
Service=collab-{service}.service
RemoveOnStop=yes
'''.encode()
    for role in ['worker', 'signer', 'approval', 'transport']:
        fs = f'--allow-fs-read={DEST} --allow-fs-read={CONFIG.parent}'
        extra = ''
        if role == 'signer':
            fs += (' --allow-fs-read=/run/credentials/collab-signer.service'
                   ' --allow-fs-read=/var/lib/collab-signer'
                   ' --allow-fs-write=/var/lib/collab-signer')
            command = 'pilot_signer.mjs --offline-connection'
            extra = '''Sockets=collab-worker.socket collab-gate.socket
LoadCredentialEncrypted=dummy:/var/lib/collab-connection-setup/dummy.cred
StateDirectory=collab-signer
StateDirectoryMode=0700
'''
        else:
            command = 'connection_services.mjs ' + role
        network = 'AF_UNIX AF_INET AF_INET6' if role == 'transport' else 'AF_UNIX'
        private = 'no' if role == 'transport' else 'yes'
        rendered['collab-' + role + '.service'] = f'''[Unit]
Description=Collaboration {role} DUMMY OFFLINE ONLY
Requires=collab-worker.socket collab-gate.socket collab-transport.socket
After=collab-worker.socket collab-gate.socket collab-transport.socket
[Service]
User=collab-{role}
Group=collab-{role}
ExecStart={DEST}/node --permission --disable-sigusr1 --disallow-code-generation-from-strings {fs} {DEST}/src/collaboration_agent/{command}
{extra}WorkingDirectory={DEST}
UMask=0077
NoNewPrivileges=yes
PrivateNetwork={private}
RestrictAddressFamilies={network}
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectProc=invisible
ProcSubset=pid
RestrictSUIDSGID=yes
RestrictNamespaces=yes
LockPersonality=yes
CapabilityBoundingSet=
LimitCORE=0
SystemCallFilter=~@debug @mount @reboot @swap
StandardOutput=null
StandardError=null
Restart=no
'''.encode()
    return rendered


def validate_pristine_state(signer_uid):
    # Compatibility wrapper for the pristine update path.
    mode, diagnostic, locks = inspect_state(signer_uid)
    if mode != 'pristine':
        raise RuntimeError('SIGNER_STATE_NOT_PRISTINE')
    return diagnostic


def inspect_state(signer_uid):
    """Bound the filesystem before stopping. The signer validates ledger/custody
    cryptography on restart; host code never grants authority from these bytes.
    Unknown/pending files fail closed and remain untouched.
    """
    if not os.path.lexists(STATE):
        return 'pristine', None, []
    trusted(STATE, uid=signer_uid, directory=True, mode=0o700)
    if {p.name for p in STATE.iterdir()} - {'ledger', 'custody', 'startup-diagnostic.json'}:
        raise RuntimeError('RECOVERY_STATE_UNSAFE')
    locks = []
    ledgers = []
    ledger = STATE / 'ledger'
    if os.path.lexists(ledger):
        trusted(ledger, uid=signer_uid, directory=True, mode=0o700)
        for path in ledger.iterdir():
            trusted(path, uid=signer_uid, mode=0o600)
            if re.fullmatch(r'identity-[0-9a-f]{64}\.json', path.name):
                ledgers.append(path)
            elif re.fullmatch(r'identity-[0-9a-f]{64}\.json\.lock', path.name):
                locks.append(path)
            else:
                raise RuntimeError('RECOVERY_STATE_UNSAFE')
    if len(ledgers) > 1:
        raise RuntimeError('RECOVERY_STATE_UNSAFE')
    custody = STATE / 'custody'
    if os.path.lexists(custody):
        trusted(custody, uid=signer_uid, directory=True, mode=0o700)
        names = {p.name for p in custody.iterdir()}
        required = {'connection-state.enc', 'connection-highwater.json', 'connection-pointer.json'}
        if not ledgers or names - (required | {'connection-store.lock'}) or not required <= names:
            raise RuntimeError('RECOVERY_STATE_UNSAFE')
        for path in custody.iterdir():
            trusted(path, uid=signer_uid, mode=0o600)
            if path.name == 'connection-store.lock':
                locks.append(path)
    for lock in locks:
        if lock.stat().st_size > 1024:
            raise RuntimeError('RECOVERY_STATE_UNSAFE')
        try:
            value = json.loads(lock.read_text())
        except (ValueError, OSError):
            raise RuntimeError('RECOVERY_STATE_UNSAFE') from None
        if not isinstance(value, dict):
            raise RuntimeError('RECOVERY_STATE_UNSAFE')
        if type(value.get('pid')) is not int or value['pid'] <= 1:
            raise RuntimeError('RECOVERY_STATE_UNSAFE')
        if lock.parent == ledger:
            if set(value) != {'pid', 'did'} or not isinstance(value['did'], str) or \
                    not re.fullmatch('did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}', value['did']) or \
                    lock.name != 'identity-' + sha(value['did'].encode()) + '.json.lock':
                raise RuntimeError('RECOVERY_STATE_UNSAFE')
        elif set(value) != {'version', 'pid'} or value['version'] != 1:
            raise RuntimeError('RECOVERY_STATE_UNSAFE')
    return ('reconciliation' if ledgers or locks else 'pristine', diagnostic_path(signer_uid), locks)


def diagnostic_path(signer_uid):
    path = STATE / 'startup-diagnostic.json'
    if not os.path.lexists(path):
        return None
    path = trusted(path, uid=signer_uid, mode=0o600)
    data = path.read_bytes()
    if len(data) > 256:
        raise RuntimeError('INVALID_STARTUP_DIAGNOSTIC')
    try:
        value = json.loads(data)
    except Exception:
        raise RuntimeError('INVALID_STARTUP_DIAGNOSTIC')
    if set(value) != {'schema', 'phase', 'code', 'invocationId'} \
            or value.get('schema') != 1 or value.get('phase') not in PHASES \
            or value.get('code') not in CODES \
            or not (value.get('invocationId') is None or
                    re.fullmatch('[0-9a-f]{32}', value['invocationId'])):
        raise RuntimeError('INVALID_STARTUP_DIAGNOSTIC')
    return path


def state_hashes():
    return {str(p.relative_to(STATE)): sha(p.read_bytes()) for p in STATE.rglob('*') if p.is_file()}


def resume_status():
    import socket
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
            if len(data) > 65536:
                raise RuntimeError('RECOVERY_STATUS_INVALID')
    value = json.loads(data)
    status = value.get('result', {})
    if value.get('ok') is not True or status.get('reconciliationOnly') is not True or \
            type(status.get('quotaConsumed')) is not bool or status.get('secretCustody') != 'ENCRYPTED_RECOVERED':
        raise RuntimeError('RECOVERY_STATUS_INVALID')
    return status['quotaConsumed']


def current_sources():
    paths = {p.name: p for p in SOURCE.iterdir()
             if p.is_file() and (p.suffix == '.mjs' or p.name == 'tclk_pin.json')}
    if set(paths) != set(BASELINE) or any(p.is_symlink() for p in paths.values()):
        raise RuntimeError('REPOSITORY_SOURCE_SET_REFUSED')
    # Freeze reviewed input bytes before any root-owned destination is changed.
    data = {name: path.read_bytes() for name, path in paths.items()}
    return data, {name: sha(value) for name, value in data.items()}


def validate_tree(expected_files):
    actual_files, actual_dirs = set(), {Path('.')}
    for path in DEST.rglob('*'):
        relative = path.relative_to(DEST)
        if path.is_symlink():
            raise RuntimeError('DEPLOYMENT_SYMLINK_REFUSED')
        if path.is_dir():
            trusted(path, directory=True)
            actual_dirs.add(relative)
        elif path.is_file():
            trusted(path)
            actual_files.add(relative)
        else:
            raise RuntimeError('UNKNOWN_DEPLOYMENT_ENTRY')
    expected_dirs = {Path('.')}
    for relative in expected_files:
        expected_dirs.update(relative.parents)
    if actual_files != expected_files or actual_dirs != expected_dirs:
        raise RuntimeError('UNKNOWN_DEPLOYMENT_TREE')


def deployment_preflight(node):
    trusted(ROOT, directory=True, mode=0o700)
    trusted(DEST, directory=True, mode=0o755)
    trusted(CONFIG.parent, directory=True, mode=0o755)
    if os.path.lexists(STATE):
        trusted(STATE, uid=pwd.getpwnam('collab-signer').pw_uid, directory=True, mode=0o700)
    trusted(UNITS, directory=True)
    source_data, current = current_sources()
    pin = json.loads(source_data['tclk_pin.json'])
    if set(pin) != {'repository', 'commit', 'version', 'dependencies', 'files'} \
            or not isinstance(pin['files'], dict) or not pin['files']:
        raise RuntimeError('RUNTIME_PIN_REFUSED')
    runtime_files = {}
    for relative, expected in pin['files'].items():
        if not re.fullmatch('[0-9a-f]{64}', expected) or Path(relative).is_absolute() \
                or '..' in Path(relative).parts:
            raise RuntimeError('RUNTIME_PIN_REFUSED')
        repo_path = RUNTIME / relative
        installed = DEST / '.local/batch16/official-runtime' / relative
        if repo_path.is_symlink() or sha(repo_path.read_bytes()) != expected \
                or sha(trusted(installed).read_bytes()) != expected:
            raise RuntimeError('RUNTIME_PIN_MISMATCH')
        runtime_files[installed.relative_to(DEST)] = expected
    installed_node = trusted(DEST / 'node', mode=0o755)
    if sha(installed_node.read_bytes()) != sha(node.read_bytes()) \
            or not run([str(installed_node), '--version']).stdout.startswith(b'v22.'):
        raise RuntimeError('NODE_RUNTIME_MISMATCH')
    expected_files = {Path('node'), *runtime_files,
                      *(Path('src/collaboration_agent') / name for name in BASELINE)}
    validate_tree(expected_files)
    # Stage outside the strict deployment tree so SIGKILL cannot leave an unknown
    # entry there. Atomic rename requires the pre-existing root setup area and all
    # changed destinations to be on the same filesystem; refuse before stopping.
    if any((DEST / 'src/collaboration_agent' / name).parent.stat().st_dev != ROOT.stat().st_dev
           for name in BASELINE):
        raise RuntimeError('UPDATE_FILESYSTEM_MISMATCH')
    manifest = json.loads(trusted(DEPLOYMENT).read_text())
    expected_keys = {'src/collaboration_agent/' + name for name in BASELINE}
    if set(manifest) != expected_keys:
        raise RuntimeError('DEPLOYMENT_MANIFEST_SET_MISMATCH')
    installed_sources = {}
    for name in BASELINE:
        path = trusted(DEST / 'src/collaboration_agent' / name)
        actual = sha(path.read_bytes())
        recorded = manifest['src/collaboration_agent/' + name]
        if actual not in {BASELINE[name], current[name]} or recorded not in {BASELINE[name], current[name]}:
            raise RuntimeError('UNKNOWN_INSTALLED_SOURCE')
        # A crash may leave a current file with the still-baseline manifest.  The
        # inverse ordering cannot be produced by this updater and is refused.
        if actual != recorded and not (actual == current[name] and recorded == BASELINE[name]):
            raise RuntimeError('DEPLOYMENT_MANIFEST_MISMATCH')
        installed_sources[name] = actual
    return source_data, current, installed_sources


def unit_preflight(human):
    expected = render_units(human)
    for name in NAMES:
        for directory in ALT_UNITS:
            if os.path.lexists(directory / name):
                raise RuntimeError('ALTERNATE_UNIT_REFUSED')
        path = trusted(UNITS / name, mode=0o644)
        if path.read_bytes() != expected[name]:
            raise RuntimeError('UNIT_VERSION_REFUSED')
        output = run(['systemctl', 'show', name, '-p', 'FragmentPath', '-p', 'DropInPaths']).stdout.decode()
        props = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        if props.get('FragmentPath') != str(path) or props.get('DropInPaths') != '':
            raise RuntimeError('UNIT_OVERRIDE_REFUSED')
    return expected


def config_preflight(human, fingerprint):
    config = json.loads(trusted(CONFIG, mode=0o644).read_text())
    if config != {'mode': 'DUMMY_OFFLINE', 'projectDid': PROJECT, 'humanUid': human,
                  'dummyCredentialSha256': fingerprint}:
        raise RuntimeError('CONFIG_MISMATCH')


def probe_preflight():
    expected_probe = Path(__file__).resolve().with_name('probe.py').read_bytes()
    expected_unit = Path(__file__).resolve().with_name('probe.service').read_bytes()
    if trusted(PROBE_INSTALL, mode=0o644).read_bytes() != expected_probe \
            or trusted(PROBE_UNIT, mode=0o644).read_bytes() != expected_unit:
        raise RuntimeError('BOUNDARY_PROBE_VERSION_REFUSED')
    output = run(['systemctl', 'show', 'collab-boundary-probe.service',
                  '-p', 'FragmentPath', '-p', 'DropInPaths']).stdout.decode()
    props = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    if props.get('FragmentPath') != str(PROBE_UNIT) or props.get('DropInPaths') != '':
        raise RuntimeError('BOUNDARY_PROBE_OVERRIDE_REFUSED')


def atomic_write(path, data, mode):
    fd, name = tempfile.mkstemp(prefix='.smoke-update-' + path.name + '-', dir=ROOT)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        setup.sync_dir(path.parent)
        if path.parent != ROOT:
            setup.sync_dir(ROOT)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def recover(node):
    trusted(ROOT, directory=True, mode=0o700)
    lock = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
    evidence = None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        roles = account_preflight()
        before = json.loads(trusted(ROOT / 'connection-before.json').read_text())
        human = before.get('human_uid')
        if before != {'units_existed': False, 'deployment_existed': False,
                      'human_uid': human, 'node_version': '22'} \
                or not isinstance(human, int) or human <= 0 \
                or human in roles.values():
            raise RuntimeError('INSTALL_PROVENANCE_REFUSED')
        credential = trusted(ROOT / 'dummy.cred', mode=0o600)
        secret = run(['systemd-creds', 'decrypt', '--name=dummy', str(credential), '-']).stdout
        if len(secret) != 32:
            raise RuntimeError('DUMMY_SIZE_MISMATCH')
        fingerprint = sha(secret)
        del secret
        probe = json.loads(trusted(ROOT / 'result.json').read_text())
        setup.validate_result(probe)
        if probe.get('dummy_credential_sha256') != fingerprint:
            raise RuntimeError('DUMMY_FINGERPRINT_MISMATCH')
        config_preflight(human, fingerprint)
        probe_preflight()
        source_data, current, installed = deployment_preflight(node)
        unit_preflight(human)
        state_mode, _, _ = inspect_state(roles['collab-signer'])

        evidence = Path(tempfile.mkdtemp(prefix='state-recovery-', dir=ROOT))
        atomic_write(evidence / 'before.json', json.dumps({'mode': 'DUMMY_OFFLINE',
                     'state_mode': state_mode, 'files': state_hashes()}).encode(), 0o600)
        # Close ingress first, then stop service processes before the final state check.
        run(['systemctl', 'stop'] + SOCKETS)
        run(['systemctl', 'stop'] + SERVICES)
        state_mode, diagnostic, locks = inspect_state(roles['collab-signer'])
        # Every service must be stopped before any lock can be moved. PID reuse
        # deliberately refuses recovery; never kill a process inferred from a file.
        for name in NAMES:
            props = dict(line.split('=', 1) for line in run(['systemctl', 'show', name,
                         '-p', 'LoadState', '-p', 'ActiveState']).stdout.decode().splitlines() if '=' in line)
            if props.get('LoadState') not in ('loaded', 'not-found') or props.get('ActiveState') not in ('inactive', 'failed'):
                raise RuntimeError('RECOVERY_UNITS_NOT_STOPPED')
        for path in locks:
            try:
                os.kill(json.loads(path.read_text())['pid'], 0)
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError('RECOVERY_OWNER_STILL_ALIVE')
        atomic_write(evidence / 'stopped.json', json.dumps({'files': state_hashes()}).encode(), 0o600)

        changed = [(DEST / 'src/collaboration_agent' / name, source_data[name], 0o644)
                   for name in BASELINE if installed[name] != current[name]]
        new_manifest = {('src/collaboration_agent/' + name): current[name] for name in BASELINE}
        manifest_bytes = (json.dumps(new_manifest, indent=2) + '\n').encode()
        if DEPLOYMENT.read_bytes() != manifest_bytes:
            changed.append((DEPLOYMENT, manifest_bytes, stat.S_IMODE(DEPLOYMENT.stat().st_mode)))
        manifest = {}
        if changed or diagnostic is not None:
            backup = Path(tempfile.mkdtemp(prefix='smoke-recovery-', dir=ROOT))
            for index, (path, _, _) in enumerate(changed):
                data = path.read_bytes()
                name = str(index) + '-' + path.name
                atomic_write(backup / name, data, 0o600)
                manifest[str(path)] = {'backup': name, 'sha256': sha(data)}
            if diagnostic is not None:
                data = diagnostic.read_bytes()
                name = str(len(manifest)) + '-startup-diagnostic.json'
                os.replace(diagnostic, backup / name)
                setup.sync_dir(STATE)
                setup.sync_dir(backup)
                manifest[str(diagnostic)] = {'backup': name, 'sha256': sha(data)}
            atomic_write(backup / 'manifest.json', (json.dumps(manifest, indent=2) + '\n').encode(), 0o600)
            setup.sync_dir(ROOT)
        for path, data, mode in changed:
            atomic_write(path, data, mode)
        run(['systemctl', 'daemon-reload'])
        # Re-run the pinned dummy boundary measurement; it decrypts no other credential.
        measured_output = io.StringIO()
        with redirect_stdout(measured_output):
            setup.measure(fingerprint)
        measured = json.loads(measured_output.getvalue())
        if not isinstance(measured, dict) or measured.get('external_writes') != 0:
            raise RuntimeError('BOUNDARY_REMEASUREMENT_FAILED')
        # Keep ingress closed if the fresh OS boundary measurement fails.
        setup.reset_failed(NAMES, run)
        # Archive dead locks only after reset succeeds. No quota/custody/nonce file
        # is deleted, replaced or rolled back. Retrying after any move is safe.
        for index, path in enumerate(locks):
            os.replace(path, evidence / ('dead-lock-' + str(index) + '.json'))
            setup.sync_dir(path.parent)
            setup.sync_dir(evidence)
        try:
            run(['systemctl', 'start'] + SOCKETS)
            consumed = resume_status() if state_mode == 'reconciliation' else None
            if state_mode == 'reconciliation':
                # Reconciliation smoke does not exercise transport, but the
                # checkpoint measures every role. All are passive IPC listeners.
                run(['systemctl', 'start'] + SERVICES)
        except Exception:
            run(['systemctl', 'stop'] + SOCKETS)
            run(['systemctl', 'stop'] + SERVICES)
            raise
        atomic_write(evidence / 'after.json', json.dumps({'files': state_hashes(),
                     'quota_consumed': consumed, 'state_mode': state_mode,
                     'external_writes': 0}).encode(), 0o600)
        return {'recovered': True, 'mode': 'DUMMY_OFFLINE', 'external_writes': 0,
                'updated_sources': [path.name for path, _, _ in changed if path.parent.name == 'collaboration_agent'],
                'evidence_preserved': diagnostic is not None,
                'boundary_remeasured': True, 'state_mode': state_mode, 'quota_consumed': consumed,
                'recovery_evidence': evidence.name}
    except Exception as exc:
        if evidence is not None:
            code = str(exc) if type(exc) is RuntimeError and re.fullmatch('[A-Z_]+', str(exc)) else 'RECOVERY_OPERATION_FAILED'
            atomic_write(evidence / 'failure.json', json.dumps({'passed': False, 'code': code,
                         'state_preserved': True, 'external_writes': 0}).encode(), 0o600)
        raise
    finally:
        os.close(lock)


def main():
    if os.geteuid() != 0:
        raise RuntimeError('ROOT_REQUIRED')
    if len(sys.argv) != 2:
        raise RuntimeError('ARGUMENTS_REFUSED')
    node = Path(sys.argv[1]).resolve(strict=True)
    info = node.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or not info.st_mode & stat.S_IXUSR:
        raise RuntimeError('NODE_SOURCE_REFUSED')
    before = json.loads(trusted(ROOT / 'connection-before.json').read_text())
    if os.environ.get('SUDO_UID') != str(before.get('human_uid')):
        raise RuntimeError('HUMAN_UID_MISMATCH')
    result = recover(node)
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        code = str(exc) if type(exc) is RuntimeError and re.fullmatch('[A-Z_]+', str(exc)) else 'SMOKE_RECOVERY_FAILED'
        print(json.dumps({'passed': False, 'code': code, 'external_writes': 0}))
        sys.exit(1)
