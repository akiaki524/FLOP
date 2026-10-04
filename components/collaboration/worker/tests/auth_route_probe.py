"""Pinned auth-route evidence: static excerpts and help ONLY, all sockets denied.

Never invokes login, logout or setup-token without --help. Uses a fresh empty
auth home per command; no existing auth inventory or credential is accessed.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import REPO
from b3_probe import BINARY, IDENTITY
from ccw import claude, claude_real as real
from ccw.claude_real_launcher import confine, execute, sealed_binary
from ccw.llm import capture
from ccw.model import canonical, require


COMMANDS = {
    "version": ["--version"],
    "login-help": ["auth", "login", "--claudeai", "--help"],
    "setup-token-help": ["setup-token", "--help"],
    "logout-help": ["auth", "logout", "--help"],
}
PREFIX = ["--safe-mode", "--setting-sources", "", "--settings",
          '{"disableAllHooks":true,"fastMode":false}']
# Offsets belong ONLY to IDENTITY. These are source observations, not proof of
# issued scopes, provider behavior, successful callback or server revocation.
RANGES = {
    "scope_constants": (189597450, 1350),
    "authorize_scopes": (192247651, 1450),
    "refresh_revoke": (192251801, 650),
    "credential_persistence": (192306750, 1900),
    "environment_token": (192308805, 1500),
    "setup_command": (204888470, 1100),
    "logout_cleanup": (207183885, 4600),
    "callback_listener": (207397050, 5500),
    "setup_flow_and_output": (210807000, 3700),
    "setup_lifetime": (211016080, 2500),
    "subscription_login": (211073600, 3100),
}


def child(workspace, parent, mode):
    require(mode in COMMANDS, "help/version only")
    require(workspace.is_absolute() and workspace.resolve() == workspace,
            "resolved scratch workspace required")
    auth = workspace / "claude-home"
    require(real.inventory(auth)["entries"] == {}, "fresh empty auth required")
    args = ["claude", *PREFIX, *COMMANDS[mode]]
    env = real.environment(workspace, auth)
    fd, _ = sealed_binary(BINARY, IDENTITY)
    if fd != 3:
        os.dup2(fd, 3, inheritable=False)
        os.close(fd)
    confine(workspace, auth, parent, online=False)
    execute(3, args, env)


def main():
    data = BINARY.read_bytes()
    require(hashlib.sha256(data).hexdigest() == IDENTITY, "pinned binary required")
    area = Path(tempfile.mkdtemp(prefix="auth-route-", dir=REPO / ".local"))
    snippets = {name: {"offset": offset, "length": length,
        "text": data[offset:offset + length].decode("utf-8", "strict")}
        for name, (offset, length) in RANGES.items()}
    (area / "static.json").write_text(json.dumps({"binary_sha256": IDENTITY,
        "static_only": True, "snippets": snippets}, ensure_ascii=True, indent=2))
    result = {"binary_sha256": IDENTITY, "network": "all sockets denied",
        "credentials_used": False, "authentication_executed": False,
        "inference_executed": False, "observations": {}}
    for mode, command in COMMANDS.items():
        workspace = area / mode
        workspace.mkdir(mode=0o700)
        claude.prepare(workspace)
        errors = bytearray()
        try:
            raw = capture([sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
                "--child", str(workspace), str(os.getpid()), mode], workspace, 20, 128000,
                input_bytes=b"", diagnostics=errors.extend)
        finally:
            (workspace / "stderr.txt").write_bytes(errors)
        (workspace / "stdout.txt").write_bytes(raw)
        if mode == "version":
            require(raw.decode().strip() == real.PINNED_VERSION, "version changed")
        else:
            require(b"Usage:" in raw and b"--help" in raw, "expected command help")
        if mode == "setup-token-help":
            require(b"--expires-in" not in raw and b"--no-browser" not in raw,
                    "review new setup-token options")
        snapshot = real.validate_auth_inventory(real.inventory(workspace / "claude-home"))
        require(snapshot["entries"] == {}, "help unexpectedly wrote auth files")
        result["observations"][mode] = {"argv": [*PREFIX, *command],
            "stdout_sha256": hashlib.sha256(raw).hexdigest(), "auth_after": snapshot}
    result["status"] = "PASS"
    (area / "summary.json").write_bytes(canonical(result))
    print(area)
    print(json.dumps(result))


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--child":
        child(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
    else:
        require(len(sys.argv) == 1, "no arbitrary commands accepted")
        main()
