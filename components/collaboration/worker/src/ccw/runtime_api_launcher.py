"""Private API child: permit/release check, confinement, ready, then secret."""
import base64
import csv
import ctypes
import errno
import hashlib
import importlib.metadata
import logging
import os
from pathlib import Path
import resource
import signal
import socket
import ssl
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccw import isolation as iso, runtime_api as api
from ccw.model import canonical, decode, digest, read, require
from ccw.runtime_output import OutputRejected
from ccw.runtime_interface import MAX_REQUEST_BYTES, parse_request
from ccw.secret_handoff import protect_process

VENV = api.VENV
# API profile owns these groups independently of the native CLI launcher.
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
    "network": (42, 44, 45, 48, 51, 52, 54, 55),
}


def verify_dependencies():
    """Frozen installed RECORD identities plus wheel provenance, not auto-update.

    Same-UID supervisor / interpreter / OS are trusted, not sealed code objects.
    Wheels are not downloaded, imported or installed by this function.
    RECORD-unlisted site-packages files are NOT enumerated or hashed. The pip
    distribution is allowed by name but is not included in the hashed closure.
    """
    lock = read(Path(__file__).with_name("messages_sdk.lock.json"))
    require(sys.version.split()[0] == lock["python"] and Path(sys.executable) == VENV / "bin/python", "Python pin")
    require(sys.flags.no_site and sys.flags.isolated and sys.flags.dont_write_bytecode, "isolated no-site bootstrap")
    site = VENV / "lib/python3.12/site-packages"
    require(not list(site.glob("*.pth")), "no startup path hooks")
    expected = {p["name"].replace("-", "_").lower() for p in lock["packages"]} | {"pip"}
    actual = {d.metadata["Name"].replace("-", "_").lower() for d in importlib.metadata.distributions(path=[str(site)])}
    require(actual == expected, "dependency surface")
    for package in lock["packages"]:
        name = package["name"].replace("-", "_")
        metadata = site / (name + "-" + package["version"] + ".dist-info")
        record = metadata / "RECORD"
        require(hashlib.sha256(record.read_bytes()).hexdigest() == package["record_sha256"], "RECORD pin")
        require(importlib.metadata.Distribution.at(metadata).version == package["version"], "dependency version")
        for filename, identity, size in csv.reader(record.read_text().splitlines()):
            if not identity:
                require(filename.endswith("/RECORD") or filename.endswith(".pyc"), "unhashed dependency")
                continue
            algorithm, encoded = identity.split("=", 1)
            path = (site / filename).resolve()
            require(path.is_relative_to(VENV) and algorithm == "sha256", "dependency path")
            data = path.read_bytes()
            require(len(data) == int(size) and base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode() == encoded,
                    "installed dependency changed")


def confine(workspace, parent, *, real=False):
    """API-only profile: no exec/fork or filesystem writes.

    Python sockets require FIONBIO (0x5421), which native CLI policy omits.
    CLI and Activity profiles are unchanged; socket domains remain TCP-only.
    Real DNS uses the system resolver over TCP, never a UDP fallback. This
    does not restrict TCP destinations to the provider or to port 443.
    """
    iso.abi()
    require(os.getppid() == parent, "parent")
    require(all(iso.socketpair_peer(fd) == parent for fd in (0, 1, 2)), "secret sockets")
    protect_process()
    iso.checked(iso.LIBC.prctl(1, signal.SIGKILL, 0, 0, 0), "parent death")
    require(os.getppid() == parent, "parent")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    os.environ.clear()
    if real:
        # glibc getaddrinfo reads this on its first resolver initialization.
        # No inherited resolver/proxy configuration enters the child.
        os.environ["RES_OPTIONS"] = "use-vc timeout:2 attempts:1"
    os.chdir(workspace)
    iso.checked(iso.LIBC.syscall(436, 3, ctypes.c_uint(0xFFFFFFFF), 0), "close FDs")
    iso.checked(iso.LIBC.prctl(38, 1, 0, 0, 0), "no_new_privs")
    handled = ctypes.c_uint64((1 << 15) - 1)
    ruleset = iso.checked(iso.LIBC.syscall(444, ctypes.byref(handled), 8, 0), "Landlock")
    paths = [workspace, Path("/usr/lib"), Path("/usr/local/lib"), Path("/dev/null"),
             Path("/dev/urandom"), Path("/etc/ssl/certs")]
    paths.append(VENV / "lib")
    if real:
        # Exact resolver inputs (including their resolved targets), not /etc.
        # Other NSS backends remain unsupported/fail closed under TCP-only.
        paths.extend(Path(p) for p in ("/etc/resolv.conf", "/etc/nsswitch.conf",
                                      "/etc/hosts", "/etc/host.conf", "/etc/gai.conf"))
    try:
        for path in paths:
            if not path.exists():
                continue
            fd = os.open(path.resolve(), os.O_PATH | os.O_NOFOLLOW)
            try:
                access = (1 << 2) | ((1 << 3) if path.is_dir() else 0)
                rule = iso.PathRule(access, fd)
                iso.checked(iso.LIBC.syscall(445, ruleset, 1, ctypes.byref(rule), 0), "Landlock rule")
            finally:
                os.close(fd)
        iso.checked(iso.LIBC.syscall(446, ruleset, 0), "Landlock restrict")
    finally:
        os.close(ruleset)
    allow, deny = 0x7FFF0000, 0x00050000 | errno.EPERM
    rows = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xC000003E), (0x06, 0, 0, 0x80000000)]
    def case(number, body):
        rows.extend([(0x20, 0, 0, 0), (0x15, 0, len(body), number), *body])
    def equals(offset, values):
        return [(0x20, 0, 0, offset + 4), (0x15, 1, 0, 0), (0x06, 0, 0, deny),
                (0x20, 0, 0, offset),
                *[(0x15, len(values) - i, 0, v) for i, v in enumerate(values)], (0x06, 0, 0, deny)]
    case(302, equals(16, [0]) + [(0x06, 0, 0, allow)])
    case(16, equals(24, [0x5401, 0x5451, 0x5421]) + [(0x06, 0, 0, allow)])
    # Only FD duplication / get flags. No F_SETOWN, notification, or async I/O.
    case(72, equals(24, [0, 1, 2, 3, 1030]) + [(0x06, 0, 0, allow)])
    case(41, equals(16, [2, 10]) + equals(24, [1, 1 | 0x800, 1 | 0x80000, 1 | 0x80800]) +
         equals(32, [0, 6]) + [(0x06, 0, 0, allow)])
    for group, numbers in CALLS.items():
        for number in numbers:
            case(number, [(0x06, 0, 0, allow)])
    rows.append((0x06, 0, 0, deny))
    filters = (iso.Filter * len(rows))(*(iso.Filter(*r) for r in rows))
    program = iso.Program(len(rows), filters)
    iso.checked(iso.LIBC.prctl(22, 2, ctypes.byref(program), 0, 0), "seccomp")


def main():
    require(len(sys.argv) == 3, "offline API supervisor required")
    root, parent = Path(sys.argv[1]), int(sys.argv[2])
    require(root.is_absolute() and root.resolve() == root and parent == os.getppid(), "private root")
    policy = read(root / "policy.json")
    spec = policy["api"]
    require(spec["code"] == api.code_manifest(), "API code pin")
    permit_sha = api.require_real_permit(root, policy) if spec["profile"] == api.REAL_PROFILE else None
    require(not spec["real_enabled"] or permit_sha is not None, "Real permit required")
    api.require_real_release(spec)
    if not spec["real_enabled"]:
        api.namespace_check(spec["host_net"])
    require(not (root / "STOP").exists(), "stopped")
    require(read(root / "consumed.json") == api.consumed_marker(policy, digest(policy), permit_sha, parent), "consumed API contract")
    verify_dependencies()
    workspace = root / "runtime"
    require(not list(workspace.iterdir()), "empty workspace")
    # -B suppresses writes, not reads of installed bytecode. Redirect cache
    # lookup to the empty private workspace before importing verified SDK code.
    sys.pycache_prefix = str(workspace / "unused-bytecode")
    sys.path.insert(0, str(VENV / "lib/python3.12/site-packages"))
    import anthropic
    import httpx2
    require(anthropic.__version__ == api.SDK_VERSION, "SDK version")
    logging.disable(logging.CRITICAL)
    tls = ssl.create_default_context()
    expires_at = None
    if permit_sha is not None:
        require(api.require_real_permit(root, policy) == permit_sha, "permit changed before ready")
        expires_at = read(root / "permit.json")["expires_at"]
    confine(workspace, parent, real=spec["real_enabled"])
    os.write(1, b"R")
    frame = decode(sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 4097))
    require(set(frame) == {"key", "request"}, "secret frame")
    if not spec["real_enabled"]:
        api.dummy_key(frame["key"])
    require(expires_at is None or time.time() < expires_at, "permit expired before POST")
    request = parse_request(canonical(frame["request"]))
    require(spec["request_sha256"] == digest(request), "fixed request")
    # validate_policy needs filesystem/net metadata denied after confinement;
    # the parent checked its full contract before durable consumption.
    result, failure = None, None
    try:
        result = api.sdk_request(request, frame["key"], spec, tls, permit_expires_at=expires_at)
    except OutputRejected as exc:
        failure = exc.reason
    sys.stdout.buffer.write(canonical({"result": result, "failure": failure}))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        # Never expose SDK exception context, response, prompt or secret.
        sys.exit(2)
