"""Human-side, one-shot dummy handoff. No LLM, broker, login or vault service.

Only an exact op read is constructed. Public plans never contain environment
values or secret digests. This module is NOT a Worker tool or a real-secret API.
The caller and resolver are trusted; the recipient is a fixed confined process.
run_dummy() accepts only OnePasswordSource; run_offline_fixture() never does.
"""
import ctypes
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass


class HandoffError(RuntimeError):
    pass


PR_GET_DUMPABLE, PR_SET_DUMPABLE = 3, 4
PR_SET_CHILD_SUBREAPER, PR_GET_CHILD_SUBREAPER = 36, 37
CONSUMER_READY = b"R"
TERMINATION_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM)
# Environment prefixes and process names set by known coding agents. This only
# prevents accidental launch from an Agent session; it is not a sandbox.
AGENT_ENV_PREFIXES = ("CLAUDE", "CODEX", "AI_AGENT")
AGENT_PROCESS_NAMES = {"claude", "codex"}


def check(condition):
    if not condition:
        raise HandoffError("dummy handoff refused; no fallback or retry")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def private_directory(path):
    path = Path(path)
    info = path.lstat()
    check(path.is_absolute() and path.resolve() == path and stat.S_ISDIR(info.st_mode)
          and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700)
    return path


def prctl(option, value=0):
    from . import isolation
    return isolation.checked(isolation.LIBC.prctl(option, ctypes.c_ulong(value), 0, 0, 0), "prctl")


def protect_process():
    """Deny same-UID ptrace, /proc/PID/mem and /proc/PID/fd, also to ancestors.

    Irreversible for the handoff's lifetime; exec'd children start dumpable again.
    """
    prctl(PR_SET_DUMPABLE, 0)
    check(prctl(PR_GET_DUMPABLE) == 0)


def children():
    """Direct children of this process, found through /proc (no helper binary)."""
    found = set()
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            except (OSError, IndexError):
                continue
            if int(fields[1]) == os.getpid():
                found.add(int(entry.name))
    return found


def terminate_descendants(before):
    """Kill and reap every descendant created since `before`; return the count.

    This process is a child subreaper while exchange() runs, so orphaned or
    setsid/double-forked descendants reparent here for cleanup. This requires
    the launcher to remain alive; SIGKILL/native crashes cannot run cleanup.
    Only unreaped children are signalled, so no PID can have been reused.
    """
    killed = 0
    for _ in range(64):
        remaining = children() - before
        if not remaining:
            return killed
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid in remaining:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            killed += 1
    raise HandoffError("descendants did not stop; no fallback or retry")


def exchange(argv, cwd, *, env=None, input_bytes=b"", timeout=5, limit=4096,
             retain_stdout=True, await_ready=False):
    """Bound both streams in memory; never expose subprocess diagnostics.

    stdin is a pipe, not a terminal. No shell, parent environment, inherited FD,
    automatic retry, raw error, or subprocess exception escapes this boundary.
    Consumer output is discarded, including encoded/transformed secret echoes.
    With await_ready, input is withheld until the child's first stdout byte is
    CONSUMER_READY (sent after it is confined and non-dumpable).
    The child gets its own session without a controlling terminal, so it cannot
    fall back to an interactive tty prompt. Any descendant left after it exits
    is killed, reaped and turns the exchange into a failure.
    """
    previous, before = None, None
    handlers, stopping = {}, []
    try:
        # Defer signals rather than raising asynchronously inside Popen or
        # cleanup. Repeated termination requests cannot interrupt kill/reap.
        # Like the subreaper scope, this is for a dedicated main-thread launcher.
        for signum in TERMINATION_SIGNALS:
            handlers[signum] = signal.signal(signum, lambda sig, frame: stopping.append(sig))
        subreaper = ctypes.c_int(0)
        prctl(PR_GET_CHILD_SUBREAPER, ctypes.addressof(subreaper))
        previous = subreaper.value
        prctl(PR_SET_CHILD_SUBREAPER, 1)
        before = children()
        with ExitStack() as streams:
            # /proc/PID/fd can reopen an ordinary pipe even from a same-UID
            # sibling. Anonymous sockets cannot be reopened that way (ENXIO).
            # Protect stderr too: a resolver may echo the value there on error.
            readers, writers = [], []
            for _ in range(0 if await_ready else 2):
                reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
                streams.enter_context(reader)
                streams.enter_context(writer)
                reader.shutdown(socket.SHUT_WR)
                writer.shutdown(socket.SHUT_RD)
                readers.append(streams.enter_context(reader.makefile("rb", buffering=0)))
                writers.append(writer)
            if await_ready:
                # The fixed consumer requires pipes and is non-dumpable before
                # readiness; its existing isolation boundary stays unchanged.
                writers = [subprocess.PIPE, subprocess.PIPE]
            check(not stopping)
            with subprocess.Popen(argv, cwd=cwd, env={} if env is None else env,
                                  stdin=subprocess.PIPE, stdout=writers[0],
                                  stderr=writers[1], close_fds=True,
                                  start_new_session=True) as child:
                if readers:
                    child.stdout, child.stderr = readers
                    for writer in writers:
                        writer.close()
                try:
                    output = pump(child, input_bytes, timeout, limit, retain_stdout, await_ready,
                                  stopping=stopping)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
        check(terminate_descendants(before) == 0)
        check(not stopping)
        check(child.returncode == 0)
        return output
    except Exception:
        raise HandoffError("private subprocess failed; no fallback or retry") from None
    finally:
        try:
            if before is not None:
                terminate_descendants(before)
        finally:
            try:
                if previous is not None:
                    prctl(PR_SET_CHILD_SUBREAPER, previous)
            finally:
                for signum, handler in handlers.items():
                    signal.signal(signum, handler)
            # A signal arriving during successful cleanup still cancels this
            # exchange; do not proceed to the consumer after a stop request.
            check(not stopping)


def pump(child, input_bytes, timeout, limit, retain_stdout, await_ready, *, stopping=()):
    with selectors.DefaultSelector() as poll:
        for stream in (child.stdout, child.stderr):
            poll.register(stream, selectors.EVENT_READ)
        os.set_blocking(child.stdin.fileno(), False)
        pending = memoryview(input_bytes)
        output, size, ready = bytearray(), 0, not await_ready
        if ready:
            poll.register(child.stdin, selectors.EVENT_WRITE)
        deadline = time.monotonic() + timeout
        while poll.get_map() or child.poll() is None:
            check(not stopping and time.monotonic() < deadline)
            for key, _ in poll.select(0.02):
                if key.fileobj is child.stdin:
                    if pending:
                        try:
                            pending = pending[os.write(key.fd, pending[:4096]):]
                        except BrokenPipeError:
                            check(False)
                    if not pending:
                        poll.unregister(child.stdin)
                        child.stdin.close()
                    continue
                data = os.read(key.fd, min(4096, limit + 1))
                if not data:
                    poll.unregister(key.fileobj)
                size += len(data)
                check(size <= limit)
                if not ready and data and key.fileobj is child.stdout:
                    check(data[:1] == CONSUMER_READY)
                    ready = True
                    poll.register(child.stdin, selectors.EVENT_WRITE)
                    data = data[1:]
                if retain_stdout and key.fileobj is child.stdout:
                    output.extend(data)
        check(ready and not pending)
        return bytes(output)


@dataclass(frozen=True)
class OnePasswordSource:
    """Nonsecret identity supplied by Human, never inferred from task material.

    Names are deliberately restricted to the dummy namespace. Human must attest
    this is a newly created dummy item before read; syntax cannot prove contents.
    Native Linux op only: Windows interop needs its own reviewed resolver bridge.
    """
    binary: str
    binary_digest: str
    account: str
    reference: str
    resolver_home: str

    def argv(self):
        # --cache=false: no op cache/background daemon holding the value after
        # exit. Taken from the public CLI reference, NOT verified on a local op
        # binary. An op that rejects the flag fails closed; never drop the flag.
        return [self.binary, "read", self.reference, "--account", self.account,
                "--no-newline", "--cache=false"]

    def public(self):
        check(type(self) is OnePasswordSource)
        check(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,127}", self.account) is not None)
        check(re.fullmatch(r"op://ccw-dummy-[a-z0-9-]+/ccw-dummy-[a-z0-9-]+/ccw-dummy-secret",
                           self.reference) is not None)
        check(re.fullmatch(r"[a-f0-9]{64}", self.binary_digest) is not None)
        binary = Path(self.binary)
        check(binary.is_absolute() and binary.resolve() == binary)
        return {"provider": "1password-cli", "operation": "read-one-dummy-field",
                "binary": self.binary, "binary_digest": self.binary_digest,
                "account": self.account, "reference": self.reference,
                "resolver_home": self.resolver_home, "argv": self.argv(),
                "environment_keys": ["HOME", "OP_BIOMETRIC_UNLOCK_ENABLED"],
                "cli_flags_verified_on_local_binary": False, "fallback": False}

    def resolve(self):
        self.public()
        home = private_directory(self.resolver_home)
        # Do not pick up cached manual-login credentials or another account's
        # config. Every Human-approved dummy read starts from a fresh home.
        check(not list(home.iterdir()))
        check(file_digest(self.binary) == self.binary_digest)
        with open(self.binary, "rb") as binary:
            check(binary.read(4) == b"\x7fELF")
        return exchange(self.argv(), home,
                        env={"HOME": str(home), "OP_BIOMETRIC_UNLOCK_ENABLED": "true"},
                        timeout=30, limit=4096)


def agent_markers(environ=None, pid=None):
    """Names (never values) of Agent markers in the environment or ancestry."""
    environ = os.environ if environ is None else environ
    found = sorted(key for key in environ if key.startswith(AGENT_ENV_PREFIXES))
    pid = os.getppid() if pid is None else pid
    while pid > 1:
        base = Path("/proc") / str(pid)
        try:
            names = {(base / "comm").read_text().strip()}
            names.update(Path(arg.decode(errors="replace")).name
                         for arg in (base / "cmdline").read_bytes().split(b"\0")[:2] if arg)
            pid = int((base / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            found.append("unreadable-ancestor")
            break
        found.extend(f"ancestor:{name}" for name in sorted(names & AGENT_PROCESS_NAMES))
    return found


def launch_guard():
    """Real reads start only from a Human terminal outside any Agent session.

    Refuses rather than warns. It catches accidental launches (Agent shell,
    `!` prefix, Agent-spawned terminal); it cannot stop a hostile same-UID
    process, which 1Password's own authorization prompt must still gate.
    """
    check(os.isatty(0) and os.isatty(1))
    check(not agent_markers())


def consumer_identity():
    directory = Path(__file__).resolve().parent
    return {"kind": "fixed-offline-dummy-consumer", "binary": str(Path(sys.executable).resolve()),
            "binary_digest": file_digest(Path(sys.executable).resolve()),
            "code": {name: file_digest(directory / name) for name in
                     ("secret_handoff.py", "secret_consumer.py", "isolation.py")},
            "network": False, "exec": False, "filesystem": "empty-readonly",
            "secret_input": "stdin-pipe-to-process-local-environment",
            "environment_key": "CCW_DUMMY_SECRET", "output": "discard-both-streams"}


PROCESS_POLICY = {"launcher_dumpable": False, "consumer_dumpable": False,
                  "secret_after_consumer_ready": True, "child_subreaper": True,
                  "descendants_after_child_exit": "kill-reap-and-fail",
                  "resolver_child_dumpable": "unchanged-by-ccw",
                  "resolver_output_transport": "anonymous-unix-sockets-no-proc-fd-reopen",
                  "termination_cleanup": [sig.name for sig in TERMINATION_SIGNALS],
                  "uncatchable_termination_cleanup": False}


def plan(source, attempt, *, kind="DUMMY_ONLY_HANDOFF"):
    attempt = Path(attempt)
    check(attempt.is_absolute() and attempt.resolve() == attempt)
    return {"version": 2, "kind": kind, "attempt": str(attempt),
            "source": source.public(), "consumer": consumer_identity(),
            "process": PROCESS_POLICY,
            "launch": ("human-tty-no-agent-env-or-ancestor" if kind == "DUMMY_ONLY_HANDOFF"
                       else "offline-fixture-any-caller"),
            "max_starts": 1, "automatic_retry": False, "secret_values_recorded": False,
            "real_secret_authorized": False}


def save_new(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run_dummy(source, attempt, confirmed_plan_digest):
    """The only real-op entry: reviewed OnePasswordSource from a Human terminal.

    No other source type, subclass or injected resolver is accepted, and there
    is no alternative account, login or credential path to fall back to.
    A new directory is not permission to retry an uncertain real task.
    Real credentials are deliberately rejected by the dummy value format.
    """
    check(type(source) is OnePasswordSource)
    public = plan(source, attempt)
    check(digest(public) == confirmed_plan_digest)
    launch_guard()
    return handoff(source, public, real=True)


def run_offline_fixture(source, attempt, confirmed_plan_digest):
    """Test harness entry for a fake resolver; can never run the op adapter."""
    check(not isinstance(source, OnePasswordSource))
    public = plan(source, attempt, kind="OFFLINE_FIXTURE_HANDOFF")
    check(digest(public) == confirmed_plan_digest)
    return handoff(source, public, real=False)


def handoff(source, public, *, real):
    """Consume the Human-confirmed attempt BEFORE resolving or launching."""
    attempt = private_directory(public["attempt"])
    check(not list(attempt.iterdir()))
    save_new(attempt / "consumed.json", {"state": "consumed-before-resolution", "plan": public})
    secret = None
    try:
        # Preflight before the store is touched; child enforces again before input.
        from . import isolation
        isolation.abi()
        protect_process()
        secret = source.resolve()
        check(type(secret) is bytes and re.fullmatch(rb"ccw-dummy-[a-zA-Z0-9_-]{16,128}", secret) is not None)
        workspace = attempt / "consumer"
        workspace.mkdir(mode=0o700)
        current = consumer_identity()
        check(current == public["consumer"])
        command = [current["binary"], "-I", "-S", "-B",
                   str(Path(__file__).with_name("secret_consumer.py")), str(workspace), str(os.getpid())]
        exchange(command, workspace, input_bytes=secret, retain_stdout=False, await_ready=True)
        result = {"state": "dummy-consumer-succeeded", "plan_digest": digest(public),
                  "source": public["source"], "secret_values_recorded": False,
                  "real_op_read": real, "real_inference": False, "automatic_retry": False,
                  "descendants_remaining": 0}
        save_new(attempt / "result.json", result)
        return result
    except BaseException:
        # Includes Ctrl-C during an authorization prompt: stop, never resume.
        save_new(attempt / "failure.json", {"state": "stopped-no-retry", "plan_digest": digest(public)})
        raise HandoffError("dummy handoff failed; attempt consumed; Human review required") from None
    finally:
        # Lifetime reduction, NOT a promise of Python/OS memory zeroization.
        secret = None
