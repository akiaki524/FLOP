"""Dummy credential boundary probe; invoked only by the dedicated systemd unit."""
import base64
import errno
import hashlib
import json
import os
import socket
from pathlib import Path

def main(output=Path('/var/lib/collab-boundary-probe/result.json')):
    result = {"uid": os.getuid(), "credential_read": False,
              "invocation_id": os.environ.get("INVOCATION_ID"), "schema": 2}
    secret = (Path(os.environ['CREDENTIALS_DIRECTORY']) / 'dummy').read_bytes()
    result['credential_read'] = len(secret) == 32
    result['dummy_credential_sha256'] = hashlib.sha256(secret).hexdigest()
    forms = [secret, secret.hex().encode(), base64.b64encode(secret),
             base64.urlsafe_b64encode(secret).rstrip(b'=')]
    environ = Path('/proc/self/environ').read_bytes()
    argv = Path('/proc/self/cmdline').read_bytes()
    result['secret_absent_env'] = all(value not in environ for value in forms)
    result['secret_absent_argv'] = all(value not in argv for value in forms)
    try:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()
        result['inet_denied'] = False
    except OSError as exc:
        result['inet_errno'] = exc.errno
        result['inet_denied'] = exc.errno in (errno.EAFNOSUPPORT, errno.EPERM, errno.EACCES)
    try:
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        result['fork_denied'] = False
    except OSError as exc:
        result['fork_errno'] = exc.errno
        result['fork_denied'] = exc.errno == errno.EPERM
    status = Path('/proc/self/status').read_text()
    result['no_new_privileges'] = 'NoNewPrivs:\t1' in status
    result['credential_path'] = os.environ['CREDENTIALS_DIRECTORY']  # path only, never contents
    output.write_text(json.dumps(result) + '\n')
    del secret, forms, environ, argv


if __name__ == '__main__':
    main()
