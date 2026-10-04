"""Exec-capable, credential-free B1 boundary. No online profile exists.

This bootstrap imports trusted modules before confinement; it never reads stdin.
Landlock persists across exec. Unlike the B0 synthetic runner, exec of the pinned
binary is allowed. All network sockets remain denied for offline verification.
An authenticated runtime needs a separately reviewed egress/credential profile.
"""

import ctypes
import errno
import os
from pathlib import Path
import resource
import signal
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccw import isolation as iso
from ccw.claude import environment
from ccw.model import require, sha


def confine(workspace, binary, entry, parent):
    iso.abi()
    require(os.getppid() == parent, "parent changed")
    iso.checked(iso.LIBC.prctl(1, signal.SIGKILL, 0, 0, 0), "parent-death signal")
    require(os.getppid() == parent, "parent exited")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    # Bun/native CLI reserves a large virtual address space; wall/CPU/output are
    # bounded here. No claim of a resident-memory hard cap for the real binary.
    for fd in (0, 1, 2):
        require(stat.S_ISFIFO(os.fstat(fd).st_mode), "launcher requires stdio pipes")
    iso.checked(iso.LIBC.syscall(436, 3, ctypes.c_uint(0xFFFFFFFF), 0), "close inherited descriptors")
    os.environ.clear()
    os.umask(0o077)
    os.chdir(workspace)
    iso.checked(iso.LIBC.prctl(38, 1, 0, 0, 0), "no_new_privs")
    handled = ctypes.c_uint64((1 << 15) - 1)
    ruleset = iso.checked(iso.LIBC.syscall(444, ctypes.byref(handled), 8, 0), "Landlock ruleset")
    rules = [(workspace, (1 << 2) | (1 << 3)),
             (binary, (1 << 0) | (1 << 2))]
    if entry:
        rules.append((entry, 1 << 2))
    for name in ("home", "claude-home", "tmp"):
        rules.append((workspace / name, sum(1 << b for b in (1, 2, 3, 4, 5, 7, 8, 13, 14))))
    for path in ("/usr/lib", "/lib", "/lib64", "/usr/local/lib"):
        if Path(path).exists():
            rules.append((Path(path).resolve(), (1 << 2) | (1 << 3)))
    for path in ("/lib64/ld-linux-x86-64.so.2", "/dev/null", "/dev/urandom"):
        if Path(path).exists():
            rules.append((Path(path).resolve(), (1 << 2) | (1 if "ld-linux" in path else 0)))
    try:
        for path, access in rules:
            fd = os.open(path, os.O_PATH | os.O_NOFOLLOW)
            try:
                rule = iso.PathRule(access, fd)
                iso.checked(iso.LIBC.syscall(445, ruleset, 1, ctypes.byref(rule), 0), "Landlock path")
            finally:
                os.close(fd)
        iso.checked(iso.LIBC.syscall(446, ruleset, 0), "Landlock restrict")
    finally:
        os.close(ruleset)
    allow = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
             17, 18, 19, 20, 21, 24, 25, 28, 35, 39, 59, 60, 63, 72, 73,
             74, 75, 76, 77, 78, 79, 82, 83, 84, 86, 87, 89, 96, 97, 98,
             99, 102, 104, 107, 108, 110, 131, 158, 186, 201, 202, 204, 217,
             218, 228, 230, 231, 232, 233, 257, 258, 262, 263, 264, 265, 267,
             270, 271, 273, 281, 291, 293, 318, 332, 334)
    # ioctl is narrowed below; no sockets/socketpair, fork/vfork, process_vm,
    # ptrace, pidfd, io_uring, namespace, mount, kill or WSL interop endpoints.
    rows = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xC000003E),
            (0x06, 0, 0, 0x80000000), (0x20, 0, 0, 0)]
    def arg_equal(number, offset, expected):
        rows.extend([(0x15, 0, 6, number), (0x20, 0, 0, offset + 4),
                     (0x15, 0, 3, 0), (0x20, 0, 0, offset),
                     (0x15, 0, 1, expected), (0x06, 0, 0, 0x7FFF0000),
                     (0x06, 0, 0, 0x00050000 | errno.EPERM)])
    arg_equal(302, 16, 0)  # prlimit only self, including high bits
    arg_equal(234, 16, os.getpid())  # tgkill only this thread group
    # TCGETS and FIOCLEX (set close-on-exec) only. CPython uses FIOCLEX when
    # opening its script; denying it makes fopen fail despite allowed file read.
    # No TIOCSTI, FIONCLEX, device or terminal mutation is authorized.
    rows.extend([(0x15, 0, 7, 16), (0x20, 0, 0, 28), (0x15, 0, 4, 0),
                 (0x20, 0, 0, 24), (0x15, 1, 0, 0x5401), (0x15, 0, 1, 0x5451),
                 (0x06, 0, 0, 0x7FFF0000), (0x06, 0, 0, 0x00050000 | errno.EPERM)])
    # CLONE_THREAD only; clone3 returns EPERM, which does NOT promise a libc
    # fallback. B1 does not establish working native pthread creation.
    rows.extend([(0x15, 0, 4, 56), (0x20, 0, 0, 16),
                 (0x45, 0, 1, 0x10000), (0x06, 0, 0, 0x7FFF0000),
                 (0x06, 0, 0, 0x00050000 | errno.EPERM)])
    for number in allow:
        if number != 16:
            rows.extend([(0x15, 0, 1, number), (0x06, 0, 0, 0x7FFF0000)])
    rows.append((0x06, 0, 0, 0x00050000 | errno.EPERM))
    filters = (iso.Filter * len(rows))(*(iso.Filter(*row) for row in rows))
    program = iso.Program(len(rows), filters)
    iso.checked(iso.LIBC.prctl(22, 2, ctypes.byref(program), 0, 0), "seccomp")


def main():
    workspace, parent = Path(sys.argv[1]), int(sys.argv[2])
    binary, binary_hash, entry, entry_hash = sys.argv[3:7]
    binary = Path(binary)
    require(binary.is_absolute() and binary.resolve() == binary, "absolute resolved binary required")
    require(sha(binary.read_bytes()) == binary_hash, "binary digest changed")
    if entry:
        require(sha(Path(entry).read_bytes()) == entry_hash, "entrypoint digest changed")
    require(workspace.resolve() == workspace, "workspace must not be symlink")
    env = environment(workspace)
    confine(workspace, binary, Path(entry) if entry else None, parent)
    os.execve(binary, [str(binary), *sys.argv[7:]], env)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("B1 launcher stopped before exec", file=sys.stderr)
        sys.exit(2)
