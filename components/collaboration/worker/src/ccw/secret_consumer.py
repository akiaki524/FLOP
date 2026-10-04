"""Fixed non-LLM consumer: confine first, then receive exactly one dummy value.

No task-driven code, auth store, op executable, shell or live-mode switch.
Exit status is the only result used by the trusted launcher. The launcher
writes the value only after the ready byte, i.e. after this process is
non-dumpable (no same-UID/ancestor ptrace, /proc/PID/mem or fd access) and
confined. Before that this process is dumpable, so all stdio must be launcher
socketpairs, which /proc/PID/fd cannot reopen (pipes could be reopened to read
the value or forge the ready byte).
"""
import base64
import errno
import os
from pathlib import Path
import re
import resource
import signal
import socket
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccw import isolation


def main():
    workspace, parent = Path(sys.argv[1]), int(sys.argv[2])
    if os.getppid() != parent:
        return 2
    isolation.checked(isolation.LIBC.prctl(1, signal.SIGKILL, 0, 0, 0), "parent death")
    if os.getppid() != parent:
        return 2
    isolation.checked(isolation.LIBC.prctl(4, 0, 0, 0, 0), "not dumpable")
    if isolation.LIBC.prctl(3, 0, 0, 0, 0) != 0:
        return 2
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    os.chdir(workspace)
    isolation.enter(workspace, readonly=True, secret_peer=parent)
    sys.stdout.buffer.write(b"R")
    sys.stdout.buffer.flush()
    value = sys.stdin.buffer.read(4097)
    if not re.fullmatch(rb"ccw-dummy-[a-zA-Z0-9_-]{16,128}", value) or os.environ:
        return 2
    os.environ["CCW_DUMMY_SECRET"] = value.decode("ascii")
    # Exercise the process-local environment boundary without invoking a CLI.
    if set(os.environ) != {"CCW_DUMMY_SECRET"} or os.environ["CCW_DUMMY_SECRET"].encode() != value:
        return 2
    for action in (lambda: socket.socket(),
                   lambda: subprocess.run(["/usr/bin/true"]),
                   lambda: Path("/etc/passwd").read_bytes(),
                   lambda: (workspace / "forbidden-write").write_bytes(value)):
        try:
            action()
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EACCES):
                return 2
        else:
            return 2
    # Deliberate negative probe: even transformed echoes must never be saved.
    sys.stdout.buffer.write(value + base64.b64encode(value))
    sys.stderr.buffer.write(value)
    os.environ.clear()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(2)
