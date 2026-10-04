"""B4 pinned-binary static evidence; no credentials, login or provider calls."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile

from support import REPO
from b3_probe import BINARY, IDENTITY
from ccw import claude_real as real
from ccw.model import canonical, decode


def static(terms):
    data = BINARY.read_bytes()
    assert hashlib.sha256(data).hexdigest() == IDENTITY
    snippets = []
    for term in terms:
        offset = 188000000
        for _ in range(8):
            offset = data.find(term.encode(), offset)
            if offset < 0:
                break
            snippets.append({"term": term, "offset": offset,
                "text": data[max(0, offset - 180):offset + 1100].decode("utf-8", "replace")})
            offset += len(term)
    area = Path(tempfile.mkdtemp(prefix="b4-static-", dir=REPO / ".local"))
    ranges = {"session_registration": (192082169, 3400), "session_peer_key": (192064428, 1900),
        "empty_tools_permissions": (202518000, 4400), "oauth_listener": (207397050, 5100),
        "subscription_login": (211073600, 3100), "strict_mcp_state": (189305775, 450),
        "cloud_mcp_predicate": (196293244, 600), "strict_mcp_cli": (204646700, 450)}
    reviewed = {name: {"offset": offset, "length": length,
        "text": data[offset:offset + length].decode("utf-8", "strict")}
        for name, (offset, length) in ranges.items()}
    (area / "snippets.json").write_text(json.dumps({"binary_sha256": IDENTITY,
        "static_only": True, "snippets": snippets, "reviewed_ranges": reviewed}, ensure_ascii=True, indent=2))
    print(area)
    print(json.dumps({"matches": [{"term": s["term"], "offset": s["offset"]} for s in snippets],
                      "reviewed_ranges": sorted(reviewed)}))


def diagnostics():
    """Pinned official CLI: help/status only, fresh homes, ALL sockets denied."""
    area = Path(tempfile.mkdtemp(prefix="b4-cli-", dir=REPO / ".local"))
    result = {"binary_sha256": IDENTITY, "network": "all sockets denied",
        "credentials_used": False, "login_executed": False, "inference_executed": False,
        "observations": {}}
    for flag in ("--version", "--review-help", "--login-help", "--empty-auth-status"):
        workspace = area / flag[2:]
        workspace.mkdir(mode=0o700)
        raw = real.probe(BINARY, IDENTITY, workspace, flag)
        if flag == "--empty-auth-status":
            assert decode(raw).get("loggedIn") is False
            observed = {"loggedIn": False}  # No raw auth status retained.
        else:
            (workspace / "help.txt").write_bytes(raw)
            if flag == "--version":
                assert raw.decode().strip() == real.PINNED_VERSION
            elif flag == "--login-help":
                assert b"--claudeai" in raw and b"--console" in raw
                assert b"--no-browser" not in raw
            else:
                assert b'--tools' in raw and b'--strict-mcp-config' in raw
            observed = {"stdout_sha256": hashlib.sha256(raw).hexdigest()}
        snapshot = real.inventory(workspace / "claude-home")
        real.validate_auth_inventory(snapshot)
        result["observations"][flag] = dict(observed, auth_after=snapshot)
    result["status"] = "PASS"
    (area / "summary.json").write_bytes(canonical(result))
    print(area)
    print(json.dumps(result))


if __name__ == "__main__":
    if sys.argv[1:] == ["--diagnostics"]:
        diagnostics()
    else:
        static(sys.argv[1:] or ["function Cue(", "function D5r(", "builtInToolsDisabled===!0",
                               "this.authCodeListener.start()", 'this.localServer.listen(e??0,"127.0.0.1"',
                               'H.command("login")', "fr&&!_r&&i3e()"])
