# Appended to Gate A ACL helper definitions, run only by Human's root coordinator
# inside the live Signer mount namespace. stdout contains allowlisted evidence only.
# Not a standalone credential input tool. Never print raw metadata, journal or errors.
import subprocess
import stat
import sys

try:
    pid, invocation, transport_pid, transport_invocation = sys.argv[1:]
    credential = Path('/run/credentials/collab-signer.service/project-seed')
    info = credential.stat()
    if info.st_size != 32 or not stat.S_ISREG(info.st_mode):
        raise ValueError()
    service_uid = pwd.getpwnam('collab-signer').pw_uid
    seed = credential.read_bytes()
    forms = (seed, seed.hex().encode(), base64.b64encode(seed),
             base64.urlsafe_b64encode(seed).rstrip(b'='))
    meta = {str(p): metadata(p) for p in (credential, credential.parent, credential.parent.parent)}
    proc = Path('/proc') / pid
    status = (proc / 'status').read_text()
    process_data = b''.join((Path('/proc') / p / name).read_bytes()
                            for p in (pid, transport_pid) for name in ('environ', 'cmdline'))
    journal = subprocess.run(['/usr/bin/journalctl', '--no-pager', '--quiet', '--output=json',
                              '_SYSTEMD_INVOCATION_ID=' + invocation, '+',
                              '_SYSTEMD_INVOCATION_ID=' + transport_invocation],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
                             timeout=15).stdout
    # Decode journal JSON so escaped control bytes / binary MESSAGE fields cannot
    # evade the raw-byte comparison. Keep all journal content inside this process.
    messages = []
    for line in journal.splitlines():
        entry = json.loads(line)
        message = entry.get('MESSAGE', '')
        if isinstance(message, str):
            messages.append(message.encode())
        elif isinstance(message, list) and all(type(x) is int and 0 <= x <= 255 for x in message):
            messages.append(bytes(message))
        else:
            raise ValueError()
    journal += b'\n'.join(messages)
    result = {
        'credential_32_bytes': len(seed) == 32 and info.st_size == 32,
        'credential_owner_allowed': info.st_uid in (0, service_uid),
        'credential_isolated': only_owner_and_service(info.st_mode, meta[str(credential)]['acl'], service_uid),
        'no_new_privileges': 'NoNewPrivs:\t1' in status,
        'seed_absent_env_argv': len(seed) == 32 and all(form not in process_data for form in forms),
        'seed_absent_journal': len(seed) == 32 and all(form not in journal for form in forms),
        'journal_scope': 'signer_and_transport_current_invocations',
        'credential_metadata': meta,
    }
    seed = forms = process_data = journal = messages = None
    print(json.dumps(result))
except Exception:
    sys.exit(1)
