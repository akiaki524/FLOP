"""Human terminal only: one reviewed Messages API attempt, never a Worker tool.

The key lives briefly in Python process memory. Complete zeroization of Python
objects and copies is not guaranteed. Do not invoke this from an Agent session.
"""
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import termios

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccw import runtime_api
from ccw.model import read, require
from ccw.runtime_interface import ReviewRequest, RuntimeClient, parse_request, send_frame
from ccw.secret_handoff import launch_guard, private_directory, protect_process


MAX_KEY_BYTES = 2048


def _terminal_key():
    """Read one bounded line from the controlling TTY with echo disabled."""
    fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
    key = bytearray()
    old = None
    try:
        require(os.isatty(fd), "Human terminal required")
        old = termios.tcgetattr(fd)
        hidden = old.copy()
        hidden[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(fd, termios.TCSAFLUSH, hidden)
        os.write(fd, b"Messages API key (hidden): ")
        while len(key) <= MAX_KEY_BYTES:
            char = os.read(fd, 1)
            if char in (b"\n", b"\r"):
                break
            require(char, "terminal closed")
            key.extend(char)
        require(16 <= len(key) <= MAX_KEY_BYTES and re.fullmatch(rb"[A-Za-z0-9_.-]+", key),
                "credential format")
        return key
    except BaseException:
        key[:] = b"\0" * len(key)
        raise
    finally:
        if old is not None:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old)
            os.write(fd, b"\n")
        os.close(fd)


def run(root):
    launch_guard()
    protect_process()
    root = private_directory(Path(root).absolute())
    request = parse_request((root / "request.json").read_bytes())
    policy = read(root / "policy.json")
    require(policy["api"]["real_enabled"] is True, "Real API policy required")
    runtime_api.admission(root, policy, request, real=True)
    require(not (root / "consumed.json").exists(), "attempt consumed")
    require(not list((root / "runtime").iterdir()), "runtime workspace must be empty")

    key = _terminal_key()
    endpoints = []
    try:
        activity, service_activity = socket.socketpair()
        endpoints.extend((activity, service_activity))
        credential, service_credential = socket.socketpair()
        endpoints.extend((credential, service_credential))
        command = [str(Path(sys.executable).resolve()), "-I", "-S", "-B",
                   str(Path(__file__).with_name("runtime_service.py")), str(root),
                   "--api-real-credential-fd", str(service_credential.fileno())]
        with subprocess.Popen(command, stdin=service_activity, stdout=service_activity,
                              stderr=subprocess.DEVNULL, pass_fds=(service_credential.fileno(),),
                              env={}, start_new_session=True) as child:
            service_activity.close()
            service_credential.close()
            try:
                credential.settimeout(1)
                send_frame(credential, bytes(key), 4096)
                credential.close()
                key[:] = b"\0" * len(key)
                result = RuntimeClient(activity, runtime_kind="real-messages-api").review(
                    ReviewRequest(request["task"]["locator"], request["task"]["text"],
                                  request["timeout_ms"]))
                require(child.wait(timeout=2) == 0, "service failed")
                print(result.preview_text())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
    finally:
        key[:] = b"\0" * len(key)
        for endpoint in endpoints:
            endpoint.close()


def main():
    # No credential, request override, endpoint or retry option in argv.
    if len(sys.argv) != 2:
        raise ValueError("one private root required")
    run(sys.argv[1])


def entrypoint():
    try:
        main()
    except BaseException:
        # Exception text may contain a key or raw provider response.
        print("Real Messages API attempt refused or failed; status may be unknown. Do not retry.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(entrypoint())
