"""Human-gated first Real Paper ACCEPT: public C1 and one-shot C2.

This file is safe to test offline.  The production CLI is intentionally root
only; C2 inherits an anonymous stdin pipe directly into the reviewed Gate B
credential handoff and never reads the seed in this coordinator.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import resource
import signal
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / 'src'))
spec = importlib.util.spec_from_file_location('gate_c_gate_b', HERE / 'gate_b.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
g, r = b.g, b.r

ROOT = r.ROOT / 'gate-c'
CREDENTIAL = r.ROOT / 'project-seed.cred'
PUBLIC_PACKET = r.CONFIG.parent / 'first-accept.json'
CLIENT = HERE / 'gate_c_client.mjs'
DROPINS = [r.UNITS / ('collab-' + role + '.service.d') / 'gate-c.conf'
           for role in ('signer', 'transport')]
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}')
HASH = re.compile(r'[0-9a-f]{64}')
PREIMAGE = re.compile(r'0x[0-9a-f]{64}')
MAX_HELPER_OUTPUT = 16 * 1024 * 1024
need, run, props, save = b.need, b.run, b.props, b.save


class ReturnC1(RuntimeError):
    """Final public observation changed before admission; create a new C1 packet."""


def attempt_path(attempt_id):
    need(type(attempt_id) is str and UUID.fullmatch(attempt_id), 'INVALID_GATE_C_ID')
    return ROOT / attempt_id


def stopped():
    return all(props(name, 'ActiveState')['ActiveState'] in ('inactive', 'failed')
               for name in r.NAMES)


def stop():
    errors = []
    for names in (r.SOCKETS, ['collab-transport.service'], ['collab-signer.service'],
                  ['collab-worker.service', 'collab-approval.service']):
        try:
            run(['systemctl', 'stop', *names])
        except Exception:
            errors.append(True)
    need(not errors and stopped(), 'STOP_FAILED')


def acquire_snapshot():
    from collaboration_agent.first_accept_read import acquire_snapshot as acquire
    return acquire()


def helper(mode, value):
    need(mode in ('candidates', 'freeze', 'validate', 'arm', 'revalidate',
                  'execute', 'match'),
         'HELPER_MODE_REFUSED')
    payload = (json.dumps(value, ensure_ascii=True, separators=(',', ':')) + '\n').encode()
    need(len(payload) <= MAX_HELPER_OUTPUT, 'HELPER_INPUT_TOO_LARGE')
    result = subprocess.run([str(r.DEST / 'node'), str(CLIENT), mode], input=payload,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env={'PATH': '/usr/bin:/bin'}, timeout=60)
    need(0 < len(result.stdout) <= MAX_HELPER_OUTPUT, 'GATE_C_HELPER_FAILED')
    try:
        output = json.loads(result.stdout)
    except (UnicodeError, ValueError):
        need(False, 'GATE_C_HELPER_FAILED')
    if result.returncode != 0:
        if (mode in ('revalidate', 'execute') and output == {
                'status': 'RETURN_C1', 'retry': False}):
            raise ReturnC1('RETURN_C1')
        need(False, 'GATE_C_HELPER_FAILED')
    return output


def tty_prompt(display, prompt):
    with open('/dev/tty', 'r+', encoding='utf-8', buffering=1) as terminal:
        terminal.write(display + ('' if display.endswith('\n') else '\n'))
        terminal.write(prompt)
        answer = terminal.readline(1024)
    need(answer.endswith('\n') and len(answer) < 1024, 'HUMAN_INPUT_REFUSED')
    return answer[:-1]


def public_display(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2)


def _json_sha(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def safe_empty_real_state():
    """Gate-C-only scoped state check; existing recovery policy is unchanged."""
    signer = pwd.getpwnam('collab-signer').pw_uid
    r.trusted(r.STATE, uid=signer, directory=True, mode=0o700)
    need({path.name for path in r.STATE.iterdir()} <= {
        'ledger', 'custody', 'startup-diagnostic.json'}, 'REAL_STATE_UNCERTAIN')
    ledger_root = r.STATE / 'ledger'
    r.trusted(ledger_root, uid=signer, directory=True, mode=0o700)
    real = ledger_root / ('identity-' + r.sha(r.PROJECT.encode()) + '.json')
    raw = json.loads(r.trusted(real, uid=signer, mode=0o600).read_text())
    need(type(raw) is dict and list(raw) == ['value', 'checksum']
         and type(raw.get('checksum')) is str and HASH.fullmatch(raw['checksum'])
         and raw['checksum'] == _json_sha(raw.get('value')), 'REAL_LEDGER_INVALID')
    value = raw['value']
    need(type(value) is dict
         and set(value) == {'version', 'mode', 'did', 'revision', 'quotaConsumed',
                            'admission', 'state', 'actions'}
         and type(value['version']) is int and value['version'] == 1
         and value['mode'] == 'TEST_EPHEMERAL' and value['did'] == r.PROJECT
         and type(value['revision']) is int
         and 0 <= value['revision'] <= 9_007_199_254_740_991
         and value['quotaConsumed'] is False
         and value['admission'] is None and value['state'] is None
         and value['actions'] == [], 'REAL_LEDGER_NOT_EMPTY')
    entries = list(ledger_root.iterdir())
    for path in entries:
        r.trusted(path, uid=signer, mode=0o600)
        need(re.fullmatch(r'identity-[0-9a-f]{64}\.json', path.name) is not None,
             'REAL_STATE_UNCERTAIN')
    # Gate B recorded every pre-existing DUMMY file before creating the fixed
    # Real identity.  Accept exactly that unchanged checkpoint plus this ledger.
    before = json.loads(r.trusted(r.ROOT / 'gate-b/state-before.json', mode=0o600).read_text())
    after = g.state_hashes()
    need(type(before) is dict
         and all(after.get(name) == digest for name, digest in before.items()),
         'EXISTING_STATE_CHANGED')
    real_relative = str(real.relative_to(r.STATE))
    need(set(after) - set(before) == {real_relative}, 'REAL_STATE_UNCERTAIN')
    need(not os.path.lexists(r.STATE / 'real-accept-custody'), 'REAL_CUSTODY_EXISTS')
    transport_uid = pwd.getpwnam('collab-transport').pw_uid
    transport = Path('/var/lib/collab-transport')
    if os.path.lexists(transport):
        r.trusted(transport, uid=transport_uid, directory=True, mode=0o700)
        need(not list(transport.iterdir()), 'TRANSPORT_STATE_NOT_EMPTY')
    return value


def deployment_preflight():
    """Gate A trust checks without its one-ledger recovery classifier."""
    r.trusted(r.DEST, directory=True, mode=0o755)
    r.trusted(r.CONFIG.parent, directory=True, mode=0o755)
    r.trusted(r.UNITS, directory=True)
    need(all(props(name, 'UnitFileState').get('UnitFileState') in ('static', 'disabled')
             for name in r.NAMES), 'BOOT_ENABLED_UNIT_REFUSED')
    manifest = json.loads(r.trusted(r.DEPLOYMENT).read_text())
    need(bool(manifest), 'EMPTY_MANIFEST')
    for relative, digest in manifest.items():
        need(re.fullmatch(r'src/collaboration_agent/[a-zA-Z0-9_]+\.(mjs|json)', relative),
             'MANIFEST_PATH_REFUSED')
        need(r.sha(r.trusted(r.DEST / relative, mode=0o644).read_bytes()) == digest,
             'MANIFEST_MISMATCH')
    entries = list(r.SOURCE.iterdir())
    need(not any(path.is_symlink() for path in entries), 'SOURCE_SYMLINK_REFUSED')
    sources = {path.name: path.read_bytes() for path in entries
               if path.suffix == '.mjs' or path.name == 'tclk_pin.json'}
    need('tclk_pin.json' in sources, 'PIN_MISSING')
    installed_pin = r.DEST / 'src/collaboration_agent/tclk_pin.json'
    need(sources['tclk_pin.json'] == r.trusted(installed_pin, mode=0o644).read_bytes(),
         'PIN_CHANGED')
    pin = json.loads(sources['tclk_pin.json'])
    runtime_paths = set()
    for relative, digest in pin['files'].items():
        path = Path('.local/batch16/official-runtime') / relative
        need(not Path(relative).is_absolute() and '..' not in Path(relative).parts,
             'PIN_PATH_REFUSED')
        need(r.sha(r.trusted(r.DEST / path).read_bytes()) == digest,
             'RUNTIME_PIN_MISMATCH')
        runtime_paths.add(path)
    r.validate_tree({Path('node'), *runtime_paths, *(Path(path) for path in manifest)})
    return sources


def no_active_attempt():
    if not os.path.lexists(ROOT):
        return
    r.trusted(ROOT, directory=True, mode=0o700)
    for path in ROOT.iterdir():
        r.trusted(path, directory=True, mode=0o700)
        need(UUID.fullmatch(path.name), 'GATE_C_ARTIFACT_UNSAFE')
        marker = path / 'c2-attempt.json'
        transaction = path / 'transaction.json'
        rollback_record = path / 'rollback.json'
        if os.path.lexists(transaction) or os.path.lexists(marker):
            # The marker is evidence that C2 was entered, not authority to
            # block every future packet. A completed rollback plus the current
            # baseline's independently verified safe-empty state may proceed.
            need(os.path.lexists(rollback_record), 'UNFINISHED_C2_TRANSACTION')
            result = json.loads(r.trusted(rollback_record, mode=0o600).read_text())
            need(result.get('passed') is True
                 and result.get('finalState') == 'DUMMY_CONFIG_ALL_STOPPED',
                 'UNFINISHED_C2_TRANSACTION')
        else:
            need(not os.path.lexists(rollback_record), 'GATE_C_ARTIFACT_UNSAFE')


def baseline_preflight():
    config = json.loads(r.trusted(r.CONFIG, mode=0o644).read_text())
    if config.get('externalWriteEnabled') is True:
        try:
            stop()
        finally:
            remove_seed()
        need(False, 'EXTERNAL_WRITE_ENABLED_STOPPED')
    need(config.get('mode') == 'DUMMY_OFFLINE'
         and 'externalWriteEnabled' not in config, 'DUMMY_WRITE_OFF_REQUIRED')
    need(stopped(), 'INITIAL_ALL_STOPPED_REQUIRED')
    need(not os.path.lexists(CREDENTIAL), 'REAL_CREDENTIAL_EXISTS')
    need(not os.path.lexists(PUBLIC_PACKET), 'PUBLIC_PACKET_EXISTS')
    need(all(not os.path.lexists(path.parent) for path in DROPINS),
         'DROPIN_DIRECTORY_EXISTS')
    no_active_attempt()
    gate_b = json.loads(r.trusted(r.ROOT / 'gate-b/result.json', mode=0o600).read_text())
    need(gate_b.get('passed') is True
         and gate_b.get('rollback') == 'DUMMY_CONFIG_ALL_STOPPED', 'GATE_B_PASS_REQUIRED')
    human = config.get('humanUid')
    need(type(human) is int and human > 0 and os.environ.get('SUDO_UID') == str(human),
         'HUMAN_UID_MISMATCH')
    r.account_preflight()
    r.config_preflight(human, config.get('dummyCredentialSha256'))
    r.unit_preflight(human)
    deployment_preflight()
    safe_empty_real_state()
    need(run([str(r.DEST / 'node'), '--version']).stdout.startswith(b'v22.'),
         'NODE22_REQUIRED')
    return human


def encrypt_preimage(value, path):
    need(type(value) is str and PREIMAGE.fullmatch(value), 'INVALID_PREIMAGE')
    plaintext = bytearray.fromhex(value[2:])
    encrypted = None
    try:
        result = subprocess.run(['/usr/bin/systemd-creds', 'encrypt', '--with-key=host',
                                 '--name=accept-preimage', '-', '-'], input=plaintext,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env={'PATH': '/usr/bin:/bin'}, timeout=30)
        need(result.returncode == 0 and 0 < len(result.stdout) <= 1024 * 1024,
             'PREIMAGE_ENCRYPT_FAILED')
        encrypted = bytearray(result.stdout)
        need(not os.path.lexists(path), 'PREIMAGE_CREDENTIAL_EXISTS')
        r.atomic_write(path, encrypted, 0o600)
    finally:
        plaintext[:] = b'\0' * len(plaintext)
        if encrypted is not None:
            encrypted[:] = b'\0' * len(encrypted)


def c1():
    baseline_preflight()
    candidates = helper('candidates', acquire_snapshot())
    need(type(candidates) is list and candidates, 'NO_FIRST_ACCEPT_CANDIDATES')
    need(all(type(item) is dict and type(item.get('offerId')) is str
             for item in candidates), 'INVALID_CANDIDATE_OUTPUT')
    selected = tty_prompt(public_display({'candidates': candidates}),
                          'Enter exact offerId (no default): ')
    need(any(item['offerId'] == selected for item in candidates), 'OFFER_SELECTION_REFUSED')
    frozen = helper('freeze', {'snapshot': acquire_snapshot(),
                               'selectedOfferId': selected})
    need(type(frozen) is dict and set(frozen) == {'document', 'preimage'}
         and type(frozen['document']) is dict, 'INVALID_FREEZE_OUTPUT')
    validated = helper('validate', {'document': frozen['document']})
    need(type(validated) is dict and validated.get('document') == frozen['document'],
         'PACKET_VALIDATION_FAILED')
    attempt_id = str(uuid.uuid4())
    root_existed = ROOT.exists()
    ROOT.mkdir(mode=0o700, parents=False, exist_ok=True)
    if not root_existed:
        r.setup.sync_dir(r.ROOT)
    r.trusted(ROOT, directory=True, mode=0o700)
    attempt = attempt_path(attempt_id)
    attempt.mkdir(mode=0o700)
    r.setup.sync_dir(ROOT)
    r.atomic_write(attempt / 'packet.json',
                   (json.dumps(frozen['document'], ensure_ascii=True,
                               separators=(',', ':')) + '\n').encode(), 0o600)
    encrypt_preimage(frozen['preimage'], attempt / 'accept-preimage.cred')
    document = frozen['document']
    return {'passed': True, 'stage': 'C1_STOPPED', 'attemptId': attempt_id,
            'externalWrites': 0, 'candidates': candidates, 'document': document,
            'nextConfirmation': 'SEND ACCEPT ' + document['digest']}


def load_artifact(attempt_id):
    attempt = attempt_path(attempt_id)
    r.trusted(attempt, directory=True, mode=0o700)
    allowed = {'packet.json', 'accept-preimage.cred', 'transaction.json',
               'c2-attempt.json', 'result.json', 'rollback.json'}
    allowed.update(str(i) + '.before' for i in range(64))
    need({path.name for path in attempt.iterdir()} <= allowed, 'GATE_C_ARTIFACT_UNSAFE')
    packet = json.loads(r.trusted(attempt / 'packet.json', mode=0o600).read_text())
    r.trusted(attempt / 'accept-preimage.cred', mode=0o600)
    validated = helper('validate', {'document': packet})
    need(type(validated) is dict and validated.get('document') == packet,
         'PACKET_VALIDATION_FAILED')
    return attempt, packet


def signer_dropin(preimage_path):
    return (('[Service]\nLoadCredentialEncrypted=\n'
             'LoadCredentialEncrypted=project-seed:' + str(CREDENTIAL) + '\n'
             'LoadCredentialEncrypted=accept-preimage:' + str(preimage_path) + '\n')).encode()


def config_bytes(human, digest):
    need(type(digest) is str and HASH.fullmatch(digest), 'PACKET_DIGEST_INVALID')
    return (json.dumps({'mode': 'REAL_ACCEPT_PREPARATION', 'projectDid': r.PROJECT,
                        'humanUid': human, 'externalWriteEnabled': True,
                        'firstAcceptDigest': digest}, separators=(',', ':')) + '\n').encode()


def prepare(attempt, human, document):
    need(not os.path.lexists(attempt / 'transaction.json'), 'C2_TRANSACTION_EXISTS')
    # Re-read and pin the complete source snapshot immediately before backup.
    sources = deployment_preflight()
    changes = [(r.DEST / 'src/collaboration_agent' / name, data)
               for name, data in sources.items()]
    changes += [(PUBLIC_PACKET,
                 (json.dumps(document, ensure_ascii=True, separators=(',', ':')) + '\n').encode()),
                (DROPINS[0], signer_dropin(attempt / 'accept-preimage.cred')),
                (DROPINS[1], (HERE / 'real-accept/transport.conf.example').read_bytes()),
                (r.CONFIG, config_bytes(human, document['digest']))]
    entries = []
    for index, (path, data) in enumerate(changes):
        previous = path.read_bytes() if path.exists() else None
        backup = str(index) + '.before'
        if previous is not None:
            r.atomic_write(attempt / backup, previous, 0o600)
        entries.append({'path': str(path), 'backup': backup if previous is not None else None,
                        'before': r.sha(previous) if previous is not None else None,
                        'after': r.sha(data)})
    save(attempt / 'transaction.json', {'entries': entries})
    return changes


def exclusive_marker(attempt, document):
    path = attempt / 'c2-attempt.json'
    data = (json.dumps({'version': 1, 'packetDigest': document['digest'],
                        'retry': False}, separators=(',', ':')) + '\n').encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        need(False, 'C2_ALREADY_ATTEMPTED')
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    r.setup.sync_dir(attempt)


def apply_changes(changes):
    for path, data in changes:
        if path == r.CONFIG:
            continue
        if path in DROPINS:
            path.parent.mkdir(mode=0o755)
        r.atomic_write(path, data, 0o644)


def handoff_seed():
    result = subprocess.run([str(r.DEST / 'node'), str(HERE / 'handoff_real.mjs')],
                            stdin=0, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            env={'PATH': '/usr/bin:/bin'}, timeout=120)
    need(result.returncode == 0, 'REAL_HANDOFF_FAILED')
    r.trusted(CREDENTIAL, mode=0o600)


def remove_seed():
    if os.path.lexists(CREDENTIAL):
        r.trusted(CREDENTIAL, mode=0o600).unlink()
        r.setup.sync_dir(CREDENTIAL.parent)


def rollback(attempt_id):
    attempt = attempt_path(attempt_id)
    stop_error = None
    try:
        stop()
    except Exception as exc:
        stop_error = exc
    remove_seed()
    if stop_error:
        raise stop_error
    r.trusted(attempt, directory=True, mode=0o700)
    tx = json.loads(r.trusted(attempt / 'transaction.json', mode=0o600).read_text())
    entries = tx['entries']
    # Restore write policy first. An unrelated source conflict must not leave a
    # reviewed after-image with externalWriteEnabled=true on disk.
    entries = sorted(entries, key=lambda entry: entry['path'] != str(r.CONFIG))
    for entry in entries:
        path = Path(entry['path'])
        need(path in [r.CONFIG, PUBLIC_PACKET, *DROPINS]
             or (path.parent == r.DEST / 'src/collaboration_agent'
                 and re.fullmatch(r'[a-zA-Z0-9_]+\.(mjs|json)', path.name)),
             'ROLLBACK_PATH_REFUSED')
        if os.path.lexists(path):
            need(r.sha(r.trusted(path).read_bytes()) in (entry['before'], entry['after']),
                 'ROLLBACK_FILE_CHANGED')
        if entry['backup'] is not None:
            need(re.fullmatch(r'[0-9]+\.before', entry['backup']), 'BACKUP_PATH_REFUSED')
            data = r.trusted(attempt / entry['backup'], mode=0o600).read_bytes()
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
    config = json.loads(r.trusted(r.CONFIG, mode=0o644).read_text())
    need(config.get('mode') == 'DUMMY_OFFLINE'
         and 'externalWriteEnabled' not in config, 'RESTORE_WRITE_OFF_FAILED')
    need(not CREDENTIAL.exists() and not PUBLIC_PACKET.exists()
         and not any(path.exists() for path in DROPINS), 'CLEANUP_FAILED')
    save(attempt / 'rollback.json', {
        'passed': True, 'finalState': 'DUMMY_CONFIG_ALL_STOPPED',
        'projectSeedRemoved': True, 'acceptPreimageRetained': True,
        'durableActionStatePreserved': True, 'cryptographicRevocation': False})


def c2(attempt_id):
    human = baseline_preflight()
    attempt, document = load_artifact(attempt_id)
    need(not os.path.lexists(attempt / 'c2-attempt.json'), 'C2_ALREADY_ATTEMPTED')
    changes = prepare(attempt, human, document)
    exclusive_marker(attempt, document)
    report = {'passed': False, 'stage': 'C2_STOPPED', 'externalWrites': 0,
              'retry': False, 'packetDigest': document['digest']}
    try:
        apply_changes(changes)
        handoff_seed()
        r.atomic_write(r.CONFIG, dict(changes)[r.CONFIG], 0o644)
        run(['systemd-analyze', 'verify', *[str(r.UNITS / name) for name in r.NAMES]])
        run(['systemctl', 'daemon-reload'])
        r.setup.reset_failed(r.NAMES, run)
        run(['systemctl', 'start', 'collab-worker.socket', 'collab-gate.socket',
             'collab-transport.socket'])
        run(['systemctl', 'start', 'collab-transport.service', 'collab-signer.service'])
        armed = helper('arm', {'document': document})
        need(armed == {'armed': True, 'packetDigest': document['digest']},
             'FIRST_ACCEPT_ARM_FAILED')
        confirmation = tty_prompt(public_display({'document': document}),
                                  'Type exact SEND ACCEPT <digest>: ')
        need(confirmation == 'SEND ACCEPT ' + document['digest'],
             'EXACT_APPROVAL_REQUIRED')
        # This is the only C2 public read before admission.  It occurs after
        # the final Human approval so a delayed terminal session cannot reuse
        # the earlier C1 observation as freshness authority.
        snapshot = acquire_snapshot()
        try:
            revalidated = helper('revalidate', {
                'document': document, 'snapshot': snapshot})
        except ReturnC1:
            report.update(stage='RETURN_C1', status='RETURN_C1',
                          credentialHandoff=True)
        else:
            need(revalidated == {'validated': True}, 'FINAL_REVALIDATION_FAILED')
            # A failed/lost RPC response after this call cannot prove whether
            # the single external attempt occurred.
            report['externalWrites'] = 'UNKNOWN'
            try:
                executed = helper('execute', {
                    'document': document, 'confirmation': confirmation,
                    'finalSnapshot': snapshot})
            except ReturnC1:
                # The helper returns this only with a proved pre-admission
                # outcome. No custody, admission, nonce or send may exist.
                report.update(stage='RETURN_C1', status='RETURN_C1',
                              externalWrites=0, credentialHandoff=True)
            else:
                need(type(executed) is dict and executed.get('status') == 'AMBIGUOUS'
                     and executed.get('stopped') is True
                     and executed.get('retry') is False
                     and executed.get('packetDigest') == document['digest']
                     and type(executed.get('envelopeDigest')) is str
                     and HASH.fullmatch(executed['envelopeDigest']),
                     'FIRST_ACCEPT_EXECUTION_FAILED')
                report['envelopeDigest'] = executed['envelopeDigest']
                reconciliation = acquire_snapshot()
                matched = helper('match', {'document': document,
                                           'snapshot': reconciliation,
                                           'envelopeDigest': executed['envelopeDigest']})
                need(type(matched) is dict and matched.get('stopped') is True
                     and matched.get('retry') is False, 'RECONCILIATION_FAILED')
                report['status'] = ('RECONCILED_PRESENT'
                                    if matched.get('status') == 'RECONCILED_PRESENT'
                                    else 'AMBIGUOUS')
                report['passed'] = report['status'] == 'RECONCILED_PRESENT'
                if report['passed']:
                    report['externalWrites'] = 1
    except Exception:
        report.setdefault('status', 'AMBIGUOUS' if report['externalWrites'] == 'UNKNOWN'
                          else 'HUMAN_STOP')
        report['code'] = 'GATE_C2_FAILED'
    finally:
        try:
            rollback(attempt_id)
            report['rollback'] = 'DUMMY_CONFIG_ALL_STOPPED'
            report['projectSeedRemoved'] = True
        except Exception:
            report.update(passed=False, rollback='FAILED_MANUAL_RECOVERY_REQUIRED')
        save(attempt / 'result.json', report)
    return report


def interrupted(_signum, _frame):
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    raise RuntimeError('GATE_C_INTERRUPTED')


def main():
    need(os.geteuid() == 0, 'ROOT_REQUIRED')
    need(len(sys.argv) in (2, 3), 'ARGUMENTS_REFUSED')
    command = sys.argv[1]
    need((command == 'c1' and len(sys.argv) == 2)
         or (command in ('c2', 'rollback') and len(sys.argv) == 3
             and UUID.fullmatch(sys.argv[2])), 'ARGUMENTS_REFUSED')
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    r.trusted(r.ROOT, directory=True, mode=0o700)
    fd = os.open(r.ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, interrupted)
        if command == 'c1':
            result = c1()
        elif command == 'c2':
            b.anonymous_stdin()
            result = c2(sys.argv[2])
        else:
            rollback(sys.argv[2])
            result = {'passed': True, 'finalState': 'DUMMY_CONFIG_ALL_STOPPED'}
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0 if result.get('passed') else 2 if result.get('stage') == 'RETURN_C1' else 1
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({'passed': False, 'code': 'GATE_C_FAILED'}))
        raise SystemExit(1)
