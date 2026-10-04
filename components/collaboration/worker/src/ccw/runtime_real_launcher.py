"""Private ready-before-secret native launcher. Never an Activity capability."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccw import claude_real, runtime_real
from ccw.claude_real_launcher import confine, execute, sealed_binary
from ccw.model import require
from ccw.model import read
from ccw.secret_handoff import protect_process


def native(workspace, binary, parent, *, fake_endpoint=None):
    """Mechanics shared by the separately gated entry and offline test harness."""
    require(workspace.is_absolute() and workspace.resolve() == workspace, "workspace")
    require(set(p.name for p in workspace.iterdir()) == {"home", "tmp", "claude-home"}, "workspace")
    require(all(not list((workspace / name).iterdir()) for name in ("home", "tmp", "claude-home")),
            "empty private homes required")
    env = runtime_real.environment(workspace)
    if fake_endpoint is not None:
        # This is test-harness injection, not a production endpoint option.
        env["ANTHROPIC_BASE_URL"] = fake_endpoint
    args = runtime_real.arguments()
    fd, _ = sealed_binary(binary, claude_real.PINNED_DIGEST)
    if fd != 3:
        os.dup2(fd, 3, inheritable=False)
        os.close(fd)
    protect_process()
    confine(workspace, workspace / "claude-home", parent, online=True, secret_stdio=True)
    os.write(1, b"R")
    value = bytearray()
    while len(value) <= 4096:
        byte = os.read(0, 1)
        require(bool(byte), "secret frame")
        if byte == b"\n":
            break
        value.extend(byte)
    else:
        require(False, "secret budget")
    secret = value.decode("ascii")
    import re
    require(re.fullmatch(r"[A-Za-z0-9_.-]{16,4096}", secret), "secret format")
    if fake_endpoint is not None:
        require(secret.startswith("ccw-dummy-"), "synthetic only")
    env["CLAUDE_CODE_OAUTH_TOKEN"] = secret
    execute(3, args, env)


def main():
    require(len(sys.argv) == 3, "Human permit required")
    root, parent = Path(sys.argv[1]), int(sys.argv[2])
    require(parent == os.getppid(), "parent")
    policy = read(root / "policy.json")
    spec = policy["real"]
    require(spec["code"] == runtime_real.code_manifest(), "code changed")
    require(spec["argv"] == runtime_real.arguments() and spec["binary_sha256"] == claude_real.PINNED_DIGEST,
            "runtime contract changed")
    permit_sha = runtime_real.validate_permit(root, policy)
    require(read(root / "consumed.json") == {"version": 2, "state": "consumed", "offline": False,
            "permit_sha256": permit_sha, "service_pid": parent}, "consumed Human permit required")
    native(root / "runtime", spec["binary"], parent)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        sys.exit(2)
