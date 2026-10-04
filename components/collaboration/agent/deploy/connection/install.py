"""Install the reviewed DUMMY_OFFLINE socket services after setup.py passed.

No service is enabled at boot. All code is copied into root-owned deployment paths.
No real credential input or outbound write exists in this installation.
"""
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys

import importlib.util
SETUP_SPEC = importlib.util.spec_from_file_location(
    'connection_boundary_setup', Path(__file__).resolve().with_name('setup.py'))
setup = importlib.util.module_from_spec(SETUP_SPEC)
SETUP_SPEC.loader.exec_module(setup)

REPO = Path(__file__).resolve().parents[2]
DEST = Path('/usr/local/lib/collab-connection')
CONFIG = Path('/etc/collab-connection')
UNITS = Path('/etc/systemd/system')
PROJECT = 'did:key:z6MkjQVeF7TCAC6H6LKQnYFannWhsfWzzHhDDr3UhbZgHSTL'
NAMES = ['collab-worker.socket', 'collab-gate.socket', 'collab-human.socket', 'collab-transport.socket',
         'collab-worker-control.socket', 'collab-worker.service',
         'collab-signer.service', 'collab-approval.service', 'collab-transport.service']

def run(args):
    return setup.run(args)

def main():
    assert os.geteuid() == 0
    human = int(os.environ['SUDO_UID'])
    assert human != 0 and human not in [pwd.getpwnam('collab-' + r).pw_uid for r in ['worker', 'approval', 'signer', 'transport']]
    node = Path(sys.argv[1]).resolve(strict=True)
    assert node.is_file() and run([str(node), '--version']).stdout.startswith(b'v22.')
    assert not any(p.exists() for p in [DEST, CONFIG, Path('/var/lib/collab-signer')])
    assert not any((UNITS / name).exists() for name in NAMES)
    probe = json.loads(Path('/var/lib/collab-connection-setup/result.json').read_text())
    assert all(probe[k] for k in ['credential_read', 'inet_denied', 'fork_denied', 'no_new_privileges', 'secret_absent_env', 'secret_absent_argv'])
    assert all(probe['cross_uid'].values())
    import re
    assert re.fullmatch('[0-9a-f]{64}', probe.get('dummy_credential_sha256', ''))
    before = {'units_existed': False, 'deployment_existed': False, 'human_uid': human, 'node_version': '22'}
    Path('/var/lib/collab-connection-setup/connection-before.json').write_text(json.dumps(before, indent=2))
    DEST.mkdir(mode=0o755)
    (DEST / 'src/collaboration_agent').mkdir(parents=True)
    for source in (REPO / 'src/collaboration_agent').glob('*.mjs'):
        shutil.copyfile(source, DEST / 'src/collaboration_agent' / source.name)
    shutil.copyfile(REPO / 'src/collaboration_agent/tclk_pin.json', DEST / 'src/collaboration_agent/tclk_pin.json')
    pin = json.loads((REPO / 'src/collaboration_agent/tclk_pin.json').read_text())
    runtime = Path('.local/batch16/official-runtime')
    import hashlib
    for relative, expected in pin['files'].items():
        source = REPO / runtime / relative
        assert not source.is_symlink() and hashlib.sha256(source.read_bytes()).hexdigest() == expected
        target = DEST / runtime / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copyfile(node, DEST / 'node')
    os.chmod(DEST / 'node', 0o755)
    CONFIG.mkdir(mode=0o755)
    (CONFIG / 'config.json').write_text(json.dumps({'mode': 'DUMMY_OFFLINE', 'projectDid': PROJECT,
        'humanUid': human, 'dummyCredentialSha256': probe['dummy_credential_sha256']}) + '\n')
    manifest = {str(p.relative_to(DEST)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (DEST / 'src/collaboration_agent').iterdir() if p.is_file()}
    Path('/var/lib/collab-connection-setup/deployment.json').write_text(json.dumps(manifest, indent=2) + '\n')
    sockets = [('worker', 'signer', 'worker', 'root', 'collab-worker', '0660'),
               ('gate', 'signer', 'gate', 'root', 'collab-approval', '0660'),
               ('human', 'approval', 'human', str(human), 'root', '0600'),
               ('transport', 'transport', 'transport', 'root', 'collab-signer', '0660'),
               ('worker-control', 'worker', 'worker-control', str(human), 'root', '0600')]
    for name, service, fdname, owner, group, mode in sockets:
        (UNITS / ('collab-' + name + '.socket')).write_text(f'''[Unit]
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
''')
    for role in ['worker', 'signer', 'approval', 'transport']:
        fs = f'--allow-fs-read={DEST} --allow-fs-read={CONFIG}'
        extra = ''
        if role == 'signer':
            fs += ' --allow-fs-read=/run/credentials/collab-signer.service --allow-fs-read=/var/lib/collab-signer --allow-fs-write=/var/lib/collab-signer'
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
        (UNITS / ('collab-' + role + '.service')).write_text(f'''[Unit]
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
''')
    # No automatic service start or automatic replay. Sockets launch a role on an authorized request.
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'start'] + ['collab-' + x + '.socket' for x in ['worker', 'gate', 'human', 'transport', 'worker-control']])
    print(json.dumps({'installed': NAMES, 'mode': 'DUMMY_OFFLINE', 'external_writes': 0}))

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        import re
        code = str(exc) if type(exc) is RuntimeError and re.fullmatch('[A-Z_]+', str(exc)) else 'INSTALL_FAILED'
        print(json.dumps({'passed': False, 'code': code}))
        sys.exit(1)
