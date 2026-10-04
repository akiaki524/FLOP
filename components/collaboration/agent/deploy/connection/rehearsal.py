"""Destructive tests ONLY in a newly created collab-rehearsal WSL distro.

Run with root and NODE22 path. Never invoke on the Human checkpoint host.
The disposable distro is the isolation/cleanup boundary; no state reset API exists.
"""
import importlib.util
import json
import os
import pwd
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

HERE = Path(__file__).resolve().parent

def load(name):
    spec = importlib.util.spec_from_file_location('rehearsal_' + name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def exercise_systemd(setup, recovery, save):
    # A separate synthetic unit forces real failed/start-limit/reset semantics.
    unit = Path('/etc/systemd/system/collab-rehearsal-failure.service')
    unit.write_text('[Unit]\nStartLimitIntervalSec=60\nStartLimitBurst=2\n[Service]\nType=exec\nExecStart=/usr/bin/false\nRestart=on-failure\nRestartSec=100ms\n')
    setup.run(['systemctl', 'daemon-reload'])
    since = '@' + str(int(time.time()))
    subprocess.run(['systemctl', 'start', unit.name], capture_output=True)
    deadline = time.monotonic() + 15
    while True:
        # systemd255 can retain Result=exit-code even after start limiting.
        # Inspect only this synthetic credential-free unit's bounded journal;
        # persist a boolean/step, never raw messages or service stderr.
        raw = setup.run(['journalctl', '-u', unit.name, '--since=' + since, '-n', '100', '--no-pager', '--output=json']).stdout
        messages = [json.loads(line).get('MESSAGE', '') for line in raw.splitlines()]
        if any(message == unit.name + ': Start request repeated too quickly.' for message in messages):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('START_LIMIT_NOT_OBSERVED')
        time.sleep(0.1)
    setup.reset_failed([unit.name])
    save('real_failed_and_start_limit_reset')
    setup.run(['systemctl', 'stop'] + recovery.SOCKETS)
    setup.run(['systemctl', 'daemon-reload'])
    setup.reset_failed(recovery.NAMES + ['collab-boundary-probe.service'])
    absent = subprocess.run(['systemctl', 'reset-failed', 'collab-rehearsal-absent.service'], capture_output=True)
    if absent.returncode != 1:
        raise RuntimeError('UNLOADED_RESET_FAILURE_NOT_OBSERVED')
    setup.reset_failed(['collab-rehearsal-absent.service'])
    save('stopped_socket_gc_unloaded_reset_and_reload')


def main():
    if (os.geteuid() != 0 or os.environ.get('WSL_DISTRO_NAME') != 'collab-rehearsal'
            or Path('/proc/1/comm').read_text().strip() != 'systemd'):
        raise RuntimeError('DISPOSABLE_SYSTEMD_REQUIRED')
    if any(Path(p).exists() for p in ['/var/lib/collab-connection-setup', '/usr/local/lib/collab-connection']):
        raise RuntimeError('FRESH_DISPOSABLE_REQUIRED')
    node = str(Path(sys.argv[1]).resolve(strict=True))
    try:
        human = pwd.getpwnam('rehearsal-human')
    except KeyError:
        subprocess.run(['useradd', '--uid', '19000', '--create-home', 'rehearsal-human'], check=True, capture_output=True)
    else:
        if human.pw_uid != 19000 or human.pw_dir != '/home/rehearsal-human':
            raise RuntimeError('REHEARSAL_HUMAN_MISMATCH')
    os.environ['SUDO_UID'] = '19000'
    setup, install, recovery, checkpoint = [load(n) for n in ('setup', 'install', 'recover_smoke', 'host_checkpoint')]
    report = {'real_secret_access': 0, 'external_writes': 0, 'steps': [],
              'kernel': os.uname().release, 'systemd': setup.run(['systemctl', '--version']).stdout.decode().splitlines()[0],
              'source': json.loads((HERE.parents[1] / 'rehearsal-source.json').read_text())}
    evidence = HERE.parents[1] / '.local/connection/rehearsal.json'
    evidence.parent.mkdir(parents=True, exist_ok=True)
    def save(step):
        report['steps'].append(step)
        evidence.write_text(json.dumps(report, indent=2) + '\n')
    with patch.object(sys, 'argv', ['setup']):
        setup.main()
    save('fresh_setup')
    # Interrupt after all installed files/units are written but before reload.
    real_run = install.run
    def interrupted(args):
        if args == ['systemctl', 'daemon-reload']:
            raise RuntimeError('REHEARSAL_PARTIAL_INSTALL')
        return real_run(args)
    with patch.object(install, 'run', side_effect=interrupted), patch.object(sys, 'argv', ['install', node]):
        try:
            install.main()
        except RuntimeError as exc:
            if str(exc) != 'REHEARSAL_PARTIAL_INSTALL':
                raise
        else:
            raise RuntimeError('PARTIAL_INJECTION_MISSING')
    save('partial_install_before_reload')
    recovery.recover(Path(node))
    save('partial_install_recovered_and_update')
    exercise_systemd(setup, recovery, save)
    for name in ['smoke_crash_restart_recovery', 'second_run_recovery']:
        with patch.object(sys, 'argv', ['checkpoint', node]):
            if checkpoint.main() != 0:
                raise RuntimeError('REHEARSAL_CHECKPOINT_FAILED')
        save(name)
    report['passed'] = True
    report['clean_retry'] = 'RECREATE_DISTRO_AND_REPEAT; NEVER_RESET_SAME_IDENTITY'
    evidence.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        import re
        code = str(exc) if type(exc) is RuntimeError and re.fullmatch('[A-Z_]+', str(exc)) else 'REHEARSAL_FAILED'
        print(json.dumps({'passed': False, 'code': code}))
        sys.exit(1)
