"""Fail-closed Linux x86-64 process confinement, no root or namespace changes.

Trusted bootstrap must import its modules before enter(). No input is interpreted
until confinement succeeds. Landlock confines files; seccomp permits only the
small syscall surface needed by Python and SQLite, excluding network and exec.
"""

import ctypes
import errno
import os
import platform
import socket
import stat
import struct


class IsolationError(RuntimeError):
    pass


LIBC = ctypes.CDLL(None, use_errno=True)


def checked(result, operation):
    if result < 0:
        raise IsolationError(f"{operation} unavailable (errno={ctypes.get_errno()}); stopped")
    return result


def abi():
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise IsolationError("requires Linux x86_64; no unsafe fallback")
    version = checked(LIBC.syscall(444, 0, 0, 1), "Landlock")
    if version < 3:
        raise IsolationError("Landlock ABI >= 3 required")
    return version


class PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("access", ctypes.c_uint64), ("parent", ctypes.c_int32)]


class Filter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]


class Program(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]


def socketpair_peer(fd):
    """Peer PID of an unnamed AF_UNIX stream socketpair endpoint, else None."""
    if not stat.S_ISSOCK(os.fstat(fd).st_mode):
        return None
    endpoint = socket.socket(fileno=fd)
    try:
        if (endpoint.family, endpoint.type) != (socket.AF_UNIX, socket.SOCK_STREAM) \
                or endpoint.getsockname() != "" or endpoint.getpeername() != "":
            return None
        pid, uid, _ = struct.unpack("3i", endpoint.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        return pid if uid == os.getuid() else None
    finally:
        endpoint.detach()


def enter(workspace, *, readonly=False, secret_peer=None):
    """Irreversibly restrict this process. Only a dedicated worker may call it."""
    abi()
    if secret_peer is not None:
        # Explicit opt-in for the fixed offline secret consumer only: all stdio
        # must be anonymous socketpairs created by the launcher. Unlike pipes,
        # /proc/PID/fd cannot reopen them before this process is non-dumpable.
        if any(socketpair_peer(fd) != secret_peer for fd in (0, 1, 2)):
            raise IsolationError("secret consumer requires launcher socketpair stdio")
    else:
        # Output channels must not be inherited writable signer files or sockets.
        for fd in (1, 2):
            mode = os.fstat(fd).st_mode
            if not (stat.S_ISFIFO(mode) or os.isatty(fd)):
                raise IsolationError("worker stdout/stderr must be a pipe or terminal")
    os.umask(0o077)
    os.environ.clear()
    # Inherited descriptors must not smuggle signer files or sockets in.
    # Include descriptors above a subsequently lowered RLIMIT_NOFILE.
    checked(LIBC.syscall(436, 3, ctypes.c_uint(0xFFFFFFFF), 0), "close inherited descriptors")
    if secret_peer is None:
        # Worker and existing runners still receive /dev/null for stdin.
        null = os.open("/dev/null", os.O_RDONLY)
        os.dup2(null, 0)
        if null > 2:
            os.close(null)
    checked(LIBC.prctl(38, 1, 0, 0, 0), "no_new_privs")
    handled = ctypes.c_uint64((1 << 15) - 1)
    ruleset = checked(LIBC.syscall(444, ctypes.byref(handled), 8, 0), "ruleset")
    directory = os.open(workspace, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Regular data only; no execute, devices, sockets, FIFO or symlinks.
        allowed = sum(1 << bit for bit in ((2, 3) if readonly else (1, 2, 3, 4, 5, 7, 8, 13, 14)))
        rule = PathRule(allowed, directory)
        checked(LIBC.syscall(445, ruleset, 1, ctypes.byref(rule), 0), "path rule")
        checked(LIBC.syscall(446, ruleset, 0), "restrict self")
    finally:
        os.close(directory)
        os.close(ruleset)
    # x86-64 only. Alternate ABI (including x32 syscall numbers) is denied.
    allowed_calls = (
        0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15,
        17, 18, 19, 20, 21, 24, 25, 28, 35, 39, 60, 63, 72, 73,
        74, 75, 76, 77, 78, 79, 82, 83, 84, 86, 87, 89, 96, 97, 98,
        99, 102, 104, 107, 108, 110, 131, 158, 186, 201, 202, 217,
        218, 228, 230, 231, 257, 258, 262, 263, 264, 265, 267, 273,
        318, 332, 334,
    )
    rows = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xC000003E),
            (0x06, 0, 0, 0x80000000), (0x20, 0, 0, 0)]
    # prlimit64 is needed by Python/SQLite only for this process. Never allow
    # a same-UID runner to change the broker's resource limits.
    rows.extend(((0x15, 0, 4, 302), (0x20, 0, 0, 16),
                 (0x15, 0, 1, 0), (0x06, 0, 0, 0x7FFF0000),
                 (0x06, 0, 0, 0x00050000 | errno.EPERM)))
    for number in allowed_calls:
        rows.extend(((0x15, 0, 1, number), (0x06, 0, 0, 0x7FFF0000)))
    rows.append((0x06, 0, 0, 0x00050000 | errno.EPERM))
    filters = (Filter * len(rows))(*(Filter(*row) for row in rows))
    program = Program(len(rows), filters)
    checked(LIBC.prctl(22, 2, ctypes.byref(program), 0, 0), "seccomp")
    # Limits are installed by the trusted launcher before enter(), where needed.


if __name__ == "__main__":
    print(f"Landlock ABI {abi()}; process confinement available")
