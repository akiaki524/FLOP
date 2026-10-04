"""Fixed real-binary OFFLINE probe. Never login, inference or live sockets."""
import ctypes
import errno
import json
import os
from pathlib import Path
import tempfile
import signal
import subprocess
import sys
import time
from unittest.mock import patch

from support import REPO, sample
from ccw import claude, claude_real
from ccw.claude_real import prepare_runtime
from ccw.human import Human, initialize
from ccw.llm import Broker
from ccw.model import Invalid, decode, require, write_new


def traced_probe(binary, identity, workspace, flag):
    """Trace only our own empty-home, socket-denied help/version child.

    Records syscall numbers/results and clone flags, never buffers/credentials.
    The tracee remains under Landlock/seccomp; tracing does not allow a syscall.
    """
    assert flag in ("--version", "--help")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.restype = ctypes.c_long
    class Registers(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "r15 r14 r13 r12 rbp rbx r11 r10 r9 r8 rax rcx rdx rsi rdi orig_rax rip cs eflags rsp ss fs_base gs_base ds es fs gs"
        ).split()]
    def trace(request, pid, data=0):
        value = libc.ptrace(request, pid, 0, data)
        if value < 0:
            raise OSError(ctypes.get_errno(), "own-child ptrace")
        return value
    claude.prepare(workspace)
    command = [sys.executable, "-I", "-S", "-B", str(REPO / "src/ccw/claude_real_launcher.py"),
               str(workspace), str(os.getpid()), str(binary), identity, flag]
    process = subprocess.Popen(command, cwd=workspace, env={}, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
        preexec_fn=lambda: trace(0, 0))
    process.stdin.close()
    for pipe in (process.stdout, process.stderr):
        os.set_blocking(pipe.fileno(), False)
    output, errors = bytearray(), bytearray()
    counts, failures, clones, arguments, paths = {}, {}, [], [], []
    def path_string(pid, address):
        content = bytearray()
        for offset in range(0, 512, 8):
            ctypes.set_errno(0)
            word = libc.ptrace(2, pid, ctypes.c_void_p(address + offset), 0)
            if word == -1 and ctypes.get_errno():
                return "<unreadable-path>"
            content.extend(ctypes.c_ulonglong(word).value.to_bytes(8, "little"))
            if 0 in content:
                break
        return bytes(content).split(b"\0", 1)[0].decode("utf-8", errors="replace")
    entering, native, code = True, False, None
    deadline = time.monotonic() + 20
    def drain():
        for pipe, buffer in ((process.stdout, output), (process.stderr, errors)):
            try:
                buffer.extend(os.read(pipe.fileno(), 128001))
            except BlockingIOError:
                pass
        if len(output) + len(errors) > 128000:
            raise RuntimeError("bounded probe output exceeded")
    try:
        os.waitpid(process.pid, 0)  # initial exec stop, before target confinement
        trace(0x4200, process.pid, 1 | (1 << 20))  # TRACESYSGOOD + EXITKILL
        trace(24, process.pid)
        while time.monotonic() < deadline:
            drain()
            pid, status = os.waitpid(process.pid, os.WNOHANG)
            if not pid:
                time.sleep(0.0001)
                continue
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                code = os.waitstatus_to_exitcode(status)
                process.returncode = code
                break
            sig = os.WSTOPSIG(status)
            if sig == signal.SIGTRAP | 0x80:
                regs = Registers()
                trace(12, pid, ctypes.byref(regs))
                number = int(regs.orig_rax)
                if entering and number == 322:
                    native = True
                if native:
                    if entering:
                        counts[str(number)] = counts.get(str(number), 0) + 1
                        if number == 56:
                            clones.append(hex(regs.rdi))
                        if number in (56, 72, 157, 233, 290, 291, 436) and len(arguments) < 100:
                            arguments.append({"syscall": number, "args": [regs.rdi, regs.rsi, regs.rdx]})
                        if number in (2, 257) and len(paths) < 100:
                            paths.append({"syscall": number, "path": path_string(pid, regs.rdi if number == 2 else regs.rsi)})
                    else:
                        result = ctypes.c_longlong(regs.rax).value
                        if -4095 <= result < 0:
                            key = f"{number}:{-result}"
                            failures[key] = failures.get(key, 0) + 1
                entering = not entering
                sig = 0
            elif sig == signal.SIGTRAP:
                sig = 0
            trace(24, pid, sig)
        drain()
    finally:
        if process.returncode is None:
            process.kill()
            process.wait()
        drain()
        process.stdout.close()
        process.stderr.close()
        (workspace / "probe-stderr.txt").write_bytes(errors)
        write_new(workspace / "syscalls.json", {"scope": "main thread from execveat; child threads not traced",
            "counts": counts, "errno_counts": failures, "clone_flags": clones,
            "selected_scalar_arguments": arguments, "exit": code,
            "opened_paths_empty_auth_diagnostic_only": paths,
            "timeout": code is None, "network": "socket denied by tracee seccomp", "ptrace": "diagnostic parent only"})
    if code != 0:
        raise RuntimeError("traced offline CLI failed; see syscalls.json and probe-stderr.txt")
    return bytes(output)


def run(profile=False, saved_b3=False):
    output = Path(tempfile.mkdtemp(prefix="b2-probe-", dir=REPO / ".local"))
    root = output / "run"
    initialize(root)
    result = {"network": "all socket syscalls denied", "real_auth": False,
              "real_inference": False, "real_send": False, "evidence": str(output)}
    try:
        if profile:
            with patch.object(claude_real, "probe", traced_probe):
                target = prepare_runtime(root)
        elif saved_b3:
            # Explicit historical pinned runtime, binary ONLY. Never import auth.
            previous = REPO / ".local/b2-probe-vg70xwcm/run"
            target = prepare_runtime(root, from_prepared_root=previous)
            result["binary_source"] = "explicit saved B3 target; no installed-version fallback"
            require(claude_real.inventory(Path(target["auth_home"]))["entries"] == {},
                    "fresh auth required after binary-only preparation")
            write_new(output / "login-plan.json", claude_real.login_plan(root))
        else:
            target = prepare_runtime(root)
        result.update(status="HELP_VERSION_ONLY", target=target)
        workspace = root / "human" / "claude-runtime" / "empty-auth"
        workspace.mkdir(mode=0o700)
        raw = claude_real.probe(Path(target["binary"]), target["binary_digest"], workspace, "--empty-auth-status")
        auth = decode(raw)
        require(type(auth) is dict and auth.get("loggedIn") is False, "empty-auth diagnostic unexpected status")
        # Retain no account fields/raw auth JSON, even in this empty fixture.
        result["empty_auth_status"] = {"loggedIn": False, "scope": "fresh empty home; sockets denied"}
        human, broker = Human(root), Broker(root)
        try:
            task = human.stage(sample())["task_id"]
            preview = broker.preview(task, "claude-real", "claude-sonnet-4-6", 1, 10,
                256000, 128000, 256000, 0, 300, "high")
            write_new(output / "unapproved-real-preview.json", preview)
            try:
                broker.run_claude(task)
            except Invalid as exc:
                require(str(exc) == "no Human LLM-use approval", "unexpected preapproval failure")
            else:
                raise AssertionError("unapproved Real run accepted")
            require(broker.db.execute("SELECT COUNT(*) FROM llm_attempts").fetchone()[0] == 0,
                    "unapproved Real created an attempt")
            result["real_contract"] = "previewed with synthetic task/model example; never approved; attempts=0"
        finally:
            human.db.close()
            broker.db.close()
    except Exception as exc:
        result.update(status="BLOCKED", error=str(exc))
        write_new(output / "summary.json", result)
        print(json.dumps(result))
        raise
    write_new(output / "summary.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["--profile"], ["--saved-b3"]):
        raise SystemExit("only --profile, --saved-b3 or no arguments")
    run(profile=sys.argv[1:] == ["--profile"], saved_b3=sys.argv[1:] == ["--saved-b3"])
