"""Adversarial operations against ONLY synthetic files, run in a child."""

import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import socket
import resource
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ccw.isolation import enter, LIBC, IsolationError
from ccw.human import Human

root = Path(sys.argv[1]).resolve()
if len(sys.argv) > 2 and sys.argv[2].startswith("runner"):
    from ccw.runner import confine
    from ccw.model import read, Invalid
    workspace = root / "probe-workspace"
    inherited = os.open(root / "human" / "dummy-key.json", os.O_RDONLY)
    high_inherited = fcntl.fcntl(inherited, fcntl.F_DUPFD, 256)
    if sys.argv[2] == "runner-unavailable":
        from ccw import isolation
        def unavailable(*args, **kwargs):
            raise IsolationError("synthetic unavailable")
        isolation.enter = unavailable
        try:
            confine(workspace, os.getppid())
        except IsolationError:
            print(json.dumps({"stopped_before_input": True}))
            sys.exit(0)
        sys.exit(1)
    confine(workspace, os.getppid())
    results = {}

    def blocked(name, operation, errors=(errno.EACCES, errno.EPERM)):
        try:
            operation()
        except OSError as exc:
            results[name] = exc.errno in errors
        else:
            results[name] = False

    # Every path was created by the test; ENOENT is never an isolation success.
    for name in ("human/dummy-key.json", "human/signer.sqlite", "human/llm.sqlite",
                 "other-local/data", "ordinary-home/.codex/config.toml",
                 "ordinary-home/.codex/synthetic-auth.json", "ordinary-home/.ssh/synthetic-key",
                 "unrelated-repo/data", "project/.codex/config.toml", "project/hooks.json",
                 "project/.agents/plugins/plugin.json", "worker/inbox/unapproved.json"):
        path = root / name
        blocked(name + ":read", lambda p=path: p.read_bytes())
        blocked(name + ":write", lambda p=path: p.open("r+b"))
    blocked("workspace_write", lambda: (workspace / "input.json").write_text("forged"))
    blocked("home_config_write", lambda: (workspace / "codex-home" / "config.toml").write_text("forged"))
    blocked("symlink_escape", lambda: (workspace / "escape").read_bytes())
    blocked("hardlink_create", lambda: os.link(root / "human" / "dummy-key.json", workspace / "copied"))
    blocked("inherited_fd", lambda: os.read(inherited, 1), (errno.EBADF,))
    blocked("high_inherited_fd", lambda: os.read(high_inherited, 1), (errno.EBADF,))
    blocked("proc_fd", lambda: Path(f"/proc/{os.getppid()}/fd/0").read_bytes())
    blocked("tcp", lambda: socket.socket(socket.AF_INET))
    blocked("unix_socket", lambda: socket.socket(socket.AF_UNIX))
    blocked("exec", lambda: os.execve("/bin/true", ["true"], {}))
    blocked("fork", os.fork)
    blocked("signal_broker", lambda: os.kill(os.getppid(), 0))
    blocked("prlimit_broker", lambda: resource.prlimit(os.getppid(), resource.RLIMIT_NOFILE))
    results["ptrace"] = LIBC.syscall(101, 0, 0, 0, 0) == -1 and ctypes.get_errno() == errno.EPERM
    results["process_vm"] = LIBC.syscall(310, os.getppid(), 0, 0, 0, 0, 0) == -1 and ctypes.get_errno() == errno.EPERM
    results["cwd"] = Path.cwd() == workspace
    results["environment"] = dict(os.environ) == {"HOME": str(workspace / "home"), "CODEX_HOME": str(workspace / "codex-home")}
    results["frozen_read"] = read(workspace / "input.json") == {"synthetic": True}
    try:
        read(workspace / "hardlinked.json")
    except Invalid:
        results["preexisting_hardlink_rejected"] = True
    else:
        results["preexisting_hardlink_rejected"] = False
    print(json.dumps(results))
    sys.exit(0 if all(results.values()) else 1)

if len(sys.argv) > 2 and sys.argv[2] == "unavailable":
    from ccw import worker

    def unavailable(_):
        raise IsolationError("synthetic unsupported sandbox")

    worker.isolation.enter = unavailable
    sys.exit(worker.main(["--root", str(root), "intake", sys.argv[3]]))

workspace = root / "worker"
key_path = root / "human" / "dummy-key.json"
inherited = os.open(key_path, os.O_RDONLY)
high_inherited = fcntl.fcntl(inherited, fcntl.F_DUPFD, 256)
_, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (128, hard_limit))
os.environ["CCW_SYNTHETIC_SECRET"] = "fixture-only"
enter(workspace)
results = {}


def denied(name, operation):
    try:
        operation()
    except OSError as exc:
        results[name] = exc.errno in (errno.EACCES, errno.EPERM, errno.EBADF, errno.EXDEV)
    else:
        results[name] = False


denied("key_read", lambda: key_path.read_bytes())
denied("approval_read", lambda: (root / "human" / "synthetic-approval.json").read_bytes())
denied("auth_read", lambda: (root / "human" / "synthetic-auth.json").read_bytes())
denied("approval_write", lambda: (root / "human" / "forged.json").write_text("forged"))
denied("inherited_fd", lambda: os.read(inherited, 1))
denied("high_inherited_fd", lambda: os.read(high_inherited, 1))
denied("symlink_escape", lambda: (workspace / "escape").read_bytes())
denied("hardlink_escape", lambda: os.link(key_path, workspace / "key-copy"))
denied("network", lambda: socket.socket())
denied("unix_socket", lambda: socket.socket(socket.AF_UNIX))
denied("exec", lambda: os.execve("/bin/true", ["true"], {}))
denied("fork", os.fork)
results["ptrace"] = LIBC.syscall(101, 0, 0, 0, 0) == -1 and ctypes.get_errno() == errno.EPERM
results["process_vm"] = LIBC.syscall(310, os.getppid(), 0, 0, 0, 0, 0) == -1 and ctypes.get_errno() == errno.EPERM
results["environment"] = not os.environ
try:
    Human(root)
except Exception:
    results["signer_call"] = True
else:
    results["signer_call"] = False
(workspace / "allowed.txt").write_text("local data")
results["workspace_write"] = (workspace / "allowed.txt").read_text() == "local data"
print(json.dumps(results))
sys.exit(0 if all(results.values()) else 1)
