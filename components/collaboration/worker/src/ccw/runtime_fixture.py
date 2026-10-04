"""Private fixed dummy Claude runtime; no real credential or execution switch."""
import os
from pathlib import Path
import resource
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccw import isolation
from ccw.model import canonical, decode, keys, require
from ccw.providers import FixtureProvider
from ccw.runtime_interface import MAX_REQUEST_BYTES, frozen_task, parse_request


def fixture_report(request):
    report = FixtureProvider().run(frozen_task(request))
    report["provider"] = "claude-cli-offline-v1"
    return report


def confine(workspace, parent):
    require(os.getppid() == parent, "parent")
    isolation.checked(isolation.LIBC.prctl(1, signal.SIGKILL, 0, 0, 0), "parent death")
    require(os.getppid() == parent, "parent")
    isolation.checked(isolation.LIBC.prctl(4, 0, 0, 0, 0), "not dumpable")
    require(isolation.LIBC.prctl(3, 0, 0, 0, 0) == 0, "dumpable")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    os.chdir(workspace)
    isolation.enter(workspace, readonly=True, secret_peer=parent)
    sys.stdout.buffer.write(b"R")
    sys.stdout.buffer.flush()


def main():
    confine(Path(sys.argv[1]), int(sys.argv[2]))
    # This private envelope is never a requester schema or a persisted artifact.
    envelope = decode(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1_025))
    keys(envelope, "request")
    require(not os.environ, "empty environment required")
    request = parse_request(canonical(envelope["request"]))
    # Model-equivalent process receives no credential, including on stdin.
    sys.stdout.buffer.write(canonical(fixture_report(request)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(2)
