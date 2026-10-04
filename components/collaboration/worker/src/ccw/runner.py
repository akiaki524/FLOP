"""Fixed synthetic executable for B0. NEVER invokes Codex, shell, or network.

Bootstrap imports trusted code before confinement, then reads the frozen input.
An actual Codex binary needs a new reviewed launcher and dedicated OS UID.
"""

import os
from pathlib import Path
import resource
import signal
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccw import isolation
from ccw.llm import SCHEMA, contract, directory, schema_check
from ccw.model import bundle, canonical, read, require
from ccw.providers import FixtureProvider


def confine(workspace, parent):
    directory(workspace)
    # Parent death cannot leave a live child sending requests after lock release.
    isolation.checked(isolation.LIBC.prctl(1, signal.SIGKILL, 0, 0, 0), "parent-death signal")
    require(os.getppid() == parent, "broker exited before confinement")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    os.chdir(workspace)
    isolation.enter(workspace, readonly=True)
    os.environ.update({"HOME": str(workspace / "home"), "CODEX_HOME": str(workspace / "codex-home")})


def main():
    workspace = Path(sys.argv[1]).absolute()
    confine(workspace, int(sys.argv[2]))
    # No data/config read before enter succeeds; no plugin/config discovery.
    require(set(p.name for p in workspace.iterdir()) ==
            {"input.json", "request.json", "schema.json", "policy.json", "home", "codex-home"},
            "unexpected runner workspace entry")
    for name in ("home", "codex-home"):
        directory(workspace / name)
        require(not list((workspace / name).iterdir()), "runner home must be empty")
    frozen = bundle(read(workspace / "input.json"))
    request = read(workspace / "request.json")
    require(request == contract(frozen, "synthetic", request["model"]), "runner contract changed")
    require(read(workspace / "schema.json") == SCHEMA, "schema changed")
    require(read(workspace / "policy.json") == request["config_overrides"], "policy changed")
    outcome = sys.argv[3]
    if outcome == "failure":
        return 76
    if outcome == "timeout":
        time.sleep(130)
    if outcome == "oversized":
        os.write(1, b"x" * 256001)
        return 0
    report = FixtureProvider().run(frozen)
    report["provider"] = "codex-cli-offline-v1"
    schema_check(report)
    if outcome == "invalid":
        report["findings"] = [{"tool": "shell"}]
    result = {"returncode": 0, "events": '{"type":"turn.completed"}',
              "final": canonical(report).decode("ascii")}
    os.write(1, canonical(result))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Never echo input or environment in errors.
        print("synthetic runner stopped", file=sys.stderr)
        sys.exit(2)
