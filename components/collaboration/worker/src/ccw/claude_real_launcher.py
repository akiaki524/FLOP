"""B2 Linux native runtime. Separate from the B1 fixture syscall policy.

The executable is copied into a sealed memfd, hashed there and execveat'd from
that very descriptor. B2's command entry point accepts offline diagnostics only.
No network-enabled inference entry point is authorized in this release.
"""
import ctypes
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import resource
import signal
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccw import isolation as iso
from ccw.model import require

PROFILE = "claude-native-b2-v1"
SEALS = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE


def sealed_binary(path, expected=None):
    """No path exec or hash/exec gap; even an in-place source mutation is checked."""
    source = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    target = -1
    try:
        info = os.fstat(source)
        require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= 512_000_000,
                "bounded regular native binary required")
        require(os.read(source, 4) == b"\x7fELF", "native ELF required")
        os.lseek(source, 0, os.SEEK_SET)
        target = os.memfd_create("ccw-claude", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        total = 0
        while chunk := os.read(source, 1024 * 1024):
            total += len(chunk)
            require(total <= 512_000_000, "binary grew beyond bound")
            pending = memoryview(chunk)
            while pending:
                pending = pending[os.write(target, pending):]
        os.fchmod(target, 0o500)
        fcntl.fcntl(target, fcntl.F_ADD_SEALS, SEALS)
        require(fcntl.fcntl(target, fcntl.F_GET_SEALS) == SEALS, "binary sealing failed")
        os.lseek(target, 0, os.SEEK_SET)
        hasher = hashlib.sha256()
        while chunk := os.read(target, 1024 * 1024):
            hasher.update(chunk)
        identity = hasher.hexdigest()
        require(expected is None or identity == expected, "sealed binary digest mismatch")
        os.lseek(target, 0, os.SEEK_SET)
        result, target = target, -1
        return result, identity
    finally:
        os.close(source)
        if target >= 0:
            os.close(target)


# x86-64 calls grouped by purpose. Unknown calls fail EPERM. Special argument
# filters below handle clone, clone3, ioctl, execveat, socket, prlimit and tgkill.
CALLS = {
    "memory": (9, 10, 11, 12, 25, 28),
    "filesystem": (0, 1, 2, 3, 4, 5, 6, 8, 17, 18, 19, 20, 21, 74,
                   75, 76, 77, 78, 79, 82, 83, 84, 86, 87, 89, 217, 257,
                   258, 262, 263, 264, 265, 267, 332),
    "threads": (24, 158, 186, 202, 204, 218, 273, 334),
    "event_loop": (7, 23, 35, 230, 232, 233, 270, 271, 281, 290, 291, 293),
    "signals": (13, 14, 15, 131),
    "identity_time_exit": (39, 60, 63, 96, 97, 98, 99, 102, 104, 107, 108,
                           110, 201, 228, 231, 318),
    # INET streams only; sendto/sendmsg operate on these FDs. No bind/listen/
    # accept or UDP sockets, so no UDP DNS packets can be emitted.
    # DNS-over-TCP remains possible via connect; compatibility is a live Gate.
    "network": (42, 44, 45, 46, 47, 48, 51, 52, 54, 55),
}


def seccomp(*, online=False, executable_fd=3):
    allow, deny = 0x7FFF0000, 0x00050000 | errno.EPERM
    rows = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xC000003E),
            (0x06, 0, 0, 0x80000000)]

    def case(number, body):
        rows.extend([(0x20, 0, 0, 0), (0x15, 0, len(body), number), *body])

    def equals(offset, values):
        # Every failure returns immediately, including nonzero high 32 bits.
        return [(0x20, 0, 0, offset + 4), (0x15, 1, 0, 0), (0x06, 0, 0, deny),
                (0x20, 0, 0, offset),
                *[(0x15, len(values) - i, 0, v) for i, v in enumerate(values)],
                (0x06, 0, 0, deny)]

    case(302, equals(16, [0]) + [(0x06, 0, 0, allow)])
    case(234, equals(16, [os.getpid()]) + [(0x06, 0, 0, allow)])
    case(16, equals(24, [0x5401, 0x5451]) + [(0x06, 0, 0, allow)])
    # Dup/get/set FD flags only; F_SETOWN/F_SETSIG/F_NOTIFY and O_ASYNC can
    # otherwise create an indirect signal channel to another same-UID process.
    case(72, equals(24, [0, 1, 2, 3, 4, 1030]) + [
        (0x20, 0, 0, 24), (0x15, 0, 3, 4), (0x20, 0, 0, 32),
        (0x45, 0, 1, 0x2000), (0x06, 0, 0, deny), (0x06, 0, 0, allow)])
    # ENOSYS, not EPERM, is necessary for glibc's pthread clone fallback.
    case(435, [(0x06, 0, 0, 0x00050000 | errno.ENOSYS)])
    # Only the measured pthread flag set. No vfork/process/namespace clone.
    case(56, equals(16, [0x3D0F00]) + [(0x06, 0, 0, allow)])
    case(322, equals(16, [executable_fd]) + equals(48, [0x1000]) + [(0x06, 0, 0, allow)])
    if online:
        case(41, equals(16, [2, 10]) +
             equals(24, [1, 1 | 0x800, 1 | 0x80000, 1 | 0x80800]) +
             equals(32, [0, 6]) + [(0x06, 0, 0, allow)])
    for group, numbers in CALLS.items():
        if group == "network" and not online:
            continue
        for number in numbers:
            case(number, [(0x06, 0, 0, allow)])
    rows.append((0x06, 0, 0, deny))
    filters = (iso.Filter * len(rows))(*(iso.Filter(*r) for r in rows))
    program = iso.Program(len(rows), filters)
    iso.checked(iso.LIBC.prctl(22, 2, ctypes.byref(program), 0, 0), "Real seccomp")


def confine(workspace, auth_home, parent, *, online=False, executable_fd=3, secret_stdio=False):
    iso.abi()
    require(os.getppid() == parent, "parent changed")
    iso.checked(iso.LIBC.prctl(1, signal.SIGKILL, 0, 0, 0), "parent death")
    require(os.getppid() == parent, "parent exited")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    # Native event-loop startup duplicates an internal FD into a high range;
    # B1's 128 limit makes fcntl fail EINVAL. Keep a bounded native-only limit.
    resource.setrlimit(resource.RLIMIT_NOFILE, (4096, 4096))
    for fd in (0, 1, 2):
        if secret_stdio:
            require(iso.socketpair_peer(fd) == parent, "private supervisor socket required")
        else:
            require(stat.S_ISFIFO(os.fstat(fd).st_mode), "Real launcher requires pipes")
    require(executable_fd == 3, "fixed executable FD required")
    iso.checked(iso.LIBC.syscall(436, 4, ctypes.c_uint(0xFFFFFFFF), 0), "close FDs")
    os.environ.clear()
    os.umask(0o077)
    os.chdir(workspace)
    iso.checked(iso.LIBC.prctl(38, 1, 0, 0, 0), "no_new_privs")
    handled = ctypes.c_uint64((1 << 15) - 1)
    ruleset = iso.checked(iso.LIBC.syscall(444, ctypes.byref(handled), 8, 0), "Landlock")
    read_dir = (1 << 2) | (1 << 3)
    write_dir = sum(1 << b for b in (1, 2, 3, 4, 5, 7, 8, 13, 14))
    rules = [(workspace, read_dir), (workspace / "home", write_dir),
             (workspace / "tmp", write_dir), (auth_home, write_dir)]
    for path in ("/usr/lib", "/lib", "/lib64", "/usr/local/lib"):
        if Path(path).exists():
            rules.append((Path(path).resolve(), read_dir))
    # Native loader and read-only trust/DNS data, never /etc as a directory.
    for path in ("/lib64/ld-linux-x86-64.so.2", "/dev/null", "/dev/urandom",
                 "/etc/ssl/certs", "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf",
                 "/proc/self/maps"):
        candidate = Path(path)
        if candidate.exists():
            access = read_dir if candidate.is_dir() else 1 << 2
            if path == "/lib64/ld-linux-x86-64.so.2":
                access |= 1  # Kernel loads the ELF interpreter during execveat.
            rules.append((candidate.resolve(), access))
    try:
        for path, access in rules:
            fd = os.open(path, os.O_PATH | os.O_NOFOLLOW)
            try:
                rule = iso.PathRule(access, fd)
                iso.checked(iso.LIBC.syscall(445, ruleset, 1, ctypes.byref(rule), 0), "Landlock rule")
            finally:
                os.close(fd)
        iso.checked(iso.LIBC.syscall(446, ruleset, 0), "Landlock restrict")
    finally:
        os.close(ruleset)
    seccomp(online=online, executable_fd=executable_fd)


def execute(fd, argv, env):
    args = (ctypes.c_char_p * (len(argv) + 1))(*[v.encode() for v in argv], None)
    values = [f"{k}={v}".encode() for k, v in env.items()]
    environ = (ctypes.c_char_p * (len(values) + 1))(*values, None)
    iso.checked(iso.LIBC.syscall(322, fd, ctypes.c_char_p(b""), args, environ, 0x1000), "execveat")


def online_runtime(workspace, parent, binary, identity, auth_home, model, effort):
    """Fixed online mechanics; B2 main and Broker independently refuse entry."""
    from ccw import claude, claude_real
    from ccw.model import canonical
    require(workspace.resolve() == workspace, "resolved workspace required")
    claude_real.inventory(auth_home)
    args = claude_real.argv({"kind": "REAL_CLAUDE_NATIVE", "binary": "claude"}, model, effort)
    env = claude_real.environment(workspace, auth_home)
    fd, _ = sealed_binary(binary, identity)
    if fd != 3:
        os.dup2(fd, 3, inheritable=False)
        os.close(fd)
    confine(workspace, auth_home, parent, online=True)
    execute(3, args, env)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        from ccw.claude_real import LOGIN_GATE
        require(False, LOGIN_GATE)  # No env/argv/plan digest can authorize login.
    if len(sys.argv) == 9 and sys.argv[1] == "online":
        from ccw.claude_real import LIVE_GATE
        require(False, LIVE_GATE)  # No config/env flag overrides the B2 gate.
        online_runtime(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5],
                       Path(sys.argv[6]), sys.argv[7], sys.argv[8])
        return
    if len(sys.argv) == 3 and sys.argv[1] == "parse":
        from ccw.claude_real import parse, rejection_reason
        from ccw.llm import schema_check  # import trusted dependencies before isolation
        from ccw.model import canonical, read
        workspace = Path(sys.argv[2])
        iso.enter(workspace, readonly=True)
        data = read(workspace / "input.json")
        try:
            report, measurements = parse(data["raw"].encode("utf-8"), data["frozen"], {"model": data["model"]})
            result = {"ok": True, "report": report, "measurements": measurements}
        except Exception as exc:
            result = {"ok": False, "reason": rejection_reason(exc)}
        sys.stdout.buffer.write(canonical(result))
        return
    from ccw.claude_real import environment, inventory
    require(len(sys.argv) == 6 and sys.argv[5] in (
            "--version", "--help", "--empty-auth-status", "--login-help", "--review-help"),
            "B2 entry point allows offline diagnostics only; live gate closed")
    workspace = Path(sys.argv[1])
    require(workspace.resolve() == workspace, "resolved workspace required")
    auth = workspace / "claude-home"
    require(inventory(auth)["entries"] == {}, "offline probe requires empty auth home")
    fd, _ = sealed_binary(sys.argv[3], sys.argv[4])
    if fd != 3:
        os.dup2(fd, 3, inheritable=False)
        os.close(fd)
    env = environment(workspace, auth)
    flag = sys.argv[5]
    if flag == "--empty-auth-status":
        args = ["auth", "status"]
    elif flag == "--login-help":
        args = ["--safe-mode", "--setting-sources", "", "--settings",
                '{"disableAllHooks":true,"fastMode":false}', "auth", "login", "--claudeai", "--help"]
    elif flag == "--review-help":
        from ccw.claude_real import argv
        args = argv({"kind": "REAL_CLAUDE_NATIVE", "binary": "claude"},
                    "claude-sonnet-4-6", "high")[1:] + ["--help"]
    else:
        args = [flag]
    confine(workspace, auth, int(sys.argv[2]))
    execute(3, ["claude", *args], env)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("B2 launcher stopped: " + str(exc), file=sys.stderr)
        sys.exit(2)
