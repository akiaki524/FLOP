"""Local reproduction of the restricted socket-activated Signer startup.

The fixture uses only a fixed dummy seed and copies the installed file layout to
a temporary directory.  Captured stderr is classified in memory and is never
printed, because production startup diagnostics must not become a secret-output
channel.
"""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


SECCOMP_WRAPPER = r"""
import ctypes
import errno
import os
import socket
import sys

class ArgCmp(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]

lib = ctypes.CDLL('libseccomp.so.2', use_errno=True)
lib.seccomp_init.argtypes = [ctypes.c_uint32]
lib.seccomp_init.restype = ctypes.c_void_p
lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
lib.seccomp_rule_add.restype = ctypes.c_int
lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
lib.seccomp_load.argtypes = [ctypes.c_void_p]
lib.seccomp_load.restype = ctypes.c_int
lib.seccomp_release.argtypes = [ctypes.c_void_p]

allow = 0x7fff0000
errno_action = 0x00050000 | errno.EAFNOSUPPORT
ctx = lib.seccomp_init(allow)
if not ctx:
    os._exit(121)
try:
    syscall = lib.seccomp_syscall_resolve_name(b'socket')
    if syscall < 0:
        os._exit(122)
    for family in (socket.AF_INET, socket.AF_INET6):
        comparison = ArgCmp(0, 4, family, 0)  # SCMP_CMP_EQ
        if lib.seccomp_rule_add(ctx, errno_action, syscall, 1, comparison) != 0:
            os._exit(123)
    if lib.seccomp_load(ctx) != 0:
        os._exit(124)
finally:
    lib.seccomp_release(ctx)

for source, target in ((int(sys.argv[1]), 3), (int(sys.argv[2]), 4)):
    if source != target:
        os.dup2(source, target, inheritable=True)
    else:
        os.set_inheritable(target, True)
os.environ['LISTEN_PID'] = str(os.getpid())
os.execve(sys.argv[3], sys.argv[3:], os.environ)
"""


def classify_failure(returncode, stderr):
    """Return a fixed diagnostic code without exposing the captured bytes."""
    if returncode in (121, 122, 123, 124):
        return "SECCOMP_SETUP_" + str(returncode)
    if b"EAFNOSUPPORT" in stderr:
        return "INET_PROBE_SYNC_THROW"
    if b"ERR_ACCESS_DENIED" in stderr:
        permission_phases = {
            b"readFileSync": "FS_READ",
            b"lstatSync": "FS_STAT",
            b"writeFileSync": "FS_WRITE",
            b"mkdirSync": "FS_MKDIR",
            b"fsyncSync": "FSYNC_FILE",
            b"openSync": "FS_OPEN",
            b"fsyncDirectory": "FSYNC_DIRECTORY",
            b"renameSync": "FS_RENAME",
            b"atomicPublicState": "ATOMIC_PUBLIC_STATE",
            b"PilotLedger": "PILOT_LEDGER",
            b"spawn": "CHILD_PROBE",
            b"Worker": "WORKER",
        }
        for signature, phase in permission_phases.items():
            if signature in stderr:
                return "NODE_PERMISSION_DENIED_" + phase
        source_phases = {
            b"connection_runtime.mjs:49": "CHILD_PROBE",
            b"connection_runtime.mjs:52": "INET_SOCKET_CREATE",
            b"connection_runtime.mjs:54": "INET_SOCKET_BIND",
            b"connection_runtime.mjs:65": "CONFIG_READ",
            b"connection_runtime.mjs:70": "CREDENTIAL_READ",
            b"connection_runtime.mjs:72": "OFFICIAL_RUNTIME",
            b"connection_runtime.mjs:75": "IDENTITY",
            b"connection_runtime.mjs:81": "LEDGER",
        }
        for signature, phase in source_phases.items():
            if signature in stderr:
                return "NODE_PERMISSION_DENIED_" + phase
        return "NODE_PERMISSION_DENIED"
    if b"SOCKET_ACTIVATION_REQUIRED" in stderr:
        return "SOCKET_ACTIVATION_REJECTED"
    if b"OFFICIAL_RUNTIME_UNAVAILABLE" in stderr:
        return "OFFICIAL_RUNTIME_UNAVAILABLE"
    if b"OS_BOUNDARY_REQUIRED" in stderr:
        return "OS_BOUNDARY_REQUIRED"
    if b"ERR_INVALID_FD_TYPE" in stderr or b"EINVAL" in stderr:
        return "SOCKET_FD_REJECTED"
    if b"EBADF" in stderr:
        return "SOCKET_FD_CLOSED"
    signatures = {
        b"ERR_INVALID_ARG_VALUE": "INVALID_ARGUMENT",
        b"ERR_INVALID_FD_TYPE": "INVALID_FD_TYPE",
        b"ERR_SYSTEM_ERROR": "SYSTEM_ERROR",
        b"ERR_SOCKET_BAD_TYPE": "SOCKET_BAD_TYPE",
        b"ERR_MODULE_NOT_FOUND": "MODULE_NOT_FOUND",
        b"ERR_DLOPEN_FAILED": "DLOPEN_FAILED",
        b"ENOSYS": "SYSCALL_UNAVAILABLE",
        b"ENOTSUP": "OPERATION_UNSUPPORTED",
        b"TypeError": "TYPE_ERROR",
        b"RangeError": "RANGE_ERROR",
        b"SyntaxError": "SYNTAX_ERROR",
        b"FileNotFoundError": "FILE_NOT_FOUND",
        b"PermissionError": "PERMISSION_ERROR",
        b"IndexError": "INDEX_ERROR",
        b"OSError": "OS_ERROR",
    }
    for signature, code in signatures.items():
        if signature in stderr:
            return code
    return "UNCLASSIFIED_RC_" + str(returncode) + "_BYTES_" + str(len(stderr))


class ConnectionStartupTest(unittest.TestCase):
    maxDiff = None

    def test_scoped_file_handle_sync_supports_file_and_directory_barriers(self):
        self.assertIsNotNone(NODE, "NODE_V22_REQUIRED")
        with tempfile.TemporaryDirectory(prefix="collab-fsync-") as raw:
            root = Path(raw)
            path = root / "state.json"
            path.write_text("fixture")
            script = r"""
import {open} from 'node:fs/promises';
const file = await open(process.argv[1], 'r+');
try { await file.sync(); } finally { await file.close(); }
const directory = await open(process.argv[2], 'r');
try { await directory.sync(); } finally { await directory.close(); }
"""
            result = subprocess.run([
                NODE, "--permission", "--disable-sigusr1",
                "--disallow-code-generation-from-strings",
                "--allow-fs-read=" + str(root),
                "--allow-fs-write=" + str(root),
                "--input-type=module", "-e", script, str(path), str(root),
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(result.stdout, b"", "FILE_HANDLE_SYNC_STDOUT")
            self.assertEqual(result.returncode, 0, "FILE_HANDLE_SYNC_UNAVAILABLE")
            self.assertEqual(result.stderr, b"", "FILE_HANDLE_SYNC_STDERR")

    def test_restricted_socket_activated_signer_stays_running(self):
        self.assertIsNotNone(NODE, "NODE_V22_REQUIRED")
        with tempfile.TemporaryDirectory(prefix="collab-startup-") as raw:
            fixture = Path(raw)
            deployment = fixture / "deployment"
            source = deployment / "src/collaboration_agent"
            config = fixture / "config"
            credentials = fixture / "credentials"
            state = fixture / "state"
            for path in (source, config, credentials, state):
                path.mkdir(parents=True)

            source_ref = os.environ.get("COLLAB_STARTUP_SOURCE_REF")
            for path in (ROOT / "src/collaboration_agent").glob("*.mjs"):
                target = source / path.name
                if source_ref:
                    target.write_bytes(subprocess.check_output(
                        ["git", "show", source_ref + ":src/collaboration_agent/" + path.name], cwd=ROOT))
                else:
                    shutil.copyfile(path, target)
            pin_source = ROOT / "src/collaboration_agent/tclk_pin.json"
            if source_ref:
                (source / "tclk_pin.json").write_bytes(subprocess.check_output(
                    ["git", "show", source_ref + ":src/collaboration_agent/tclk_pin.json"], cwd=ROOT))
            else:
                shutil.copyfile(pin_source, source / "tclk_pin.json")

            pin = json.loads((source / "tclk_pin.json").read_text())
            runtime_source = ROOT / ".local/batch16/official-runtime"
            runtime_target = deployment / ".local/batch16/official-runtime"
            for relative in pin["files"]:
                target = runtime_target / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(runtime_source / relative, target)

            runtime_path = source / "connection_runtime.mjs"
            text = runtime_path.read_text()
            text = text.replace("'/etc/collab-connection/config.json'", repr(str(config / "config.json")))
            text = text.replace("/var/lib/collab-signer", str(state))
            runtime_path.write_text(text)

            seed = b"A" * 32
            (credentials / "dummy").write_bytes(seed)
            (config / "config.json").write_text(json.dumps({
                "mode": "DUMMY_OFFLINE",
                "projectDid": "did:key:z6MkjQVeF7TCAC6H6LKQnYFannWhsfWzzHhDDr3UhbZgHSTL",
                "dummyCredentialSha256": hashlib.sha256(seed).hexdigest(),
            }))

            listeners = []
            passed_fds = []
            try:
                for name in ("worker", "gate"):
                    listener = socket.socket(socket.AF_UNIX)
                    listener.bind(str(fixture / (name + ".sock")))
                    listener.listen(4)
                    listeners.append(listener)
                    passed_fds.append(os.dup(listener.fileno()))

                env = os.environ.copy()
                env.update({
                    "LISTEN_FDS": "2",
                    "LISTEN_FDNAMES": "worker:gate",
                    "CREDENTIALS_DIRECTORY": str(credentials),
                })
                command = [
                    sys.executable, "-I", "-c", SECCOMP_WRAPPER,
                    str(passed_fds[0]), str(passed_fds[1]), NODE,
                    "--permission", "--disable-sigusr1",
                    "--disallow-code-generation-from-strings",
                    "--allow-fs-read=" + str(deployment),
                    "--allow-fs-read=" + str(config),
                    "--allow-fs-read=" + str(credentials),
                    "--allow-fs-read=" + str(state),
                    "--allow-fs-write=" + str(state),
                    str(source / "pilot_signer.mjs"), "--offline-connection",
                ]
                child = subprocess.Popen(
                    command, cwd=deployment, env=env, pass_fds=tuple(passed_fds),
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )
                deadline = time.monotonic() + 2
                while child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                if child.poll() is None:
                    child.terminate()
                    _, stderr = child.communicate(timeout=5)
                    self.assertEqual(stderr, b"", "SIGNER_EMITTED_STARTUP_DIAGNOSTIC")
                    return
                _, stderr = child.communicate(timeout=5)
                diagnostic = state / "startup-diagnostic.json"
                if diagnostic.is_file():
                    value = json.loads(diagnostic.read_text())
                    self.fail("SIGNER_STARTUP_FAILED:" + value.get("phase", "UNKNOWN")
                              + ":" + value.get("code", "UNKNOWN"))
                self.fail("SIGNER_STARTUP_FAILED:" + classify_failure(child.returncode, stderr))
            finally:
                for fd in passed_fds:
                    os.close(fd)
                for listener in listeners:
                    listener.close()


if __name__ == "__main__":
    unittest.main()
