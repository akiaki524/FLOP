"""Fixed offline B1 smoke, or isolated installed-binary help/version probe.

No arbitrary tasks/commands/auth directories are accepted. Saves fresh evidence.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from support import REPO, cli
from ccw import claude
from ccw.llm import capture
from ccw.model import Invalid, canonical, sha, write_new


def run():
    output = Path(tempfile.mkdtemp(prefix="b1-smoke-", dir=REPO / ".local"))
    root = output / "run"
    cli("human", root, "init")
    task = json.loads(cli("human", root, "stage", "examples/task.json").stdout)["task_id"]
    cli("human", root, "llm-run-claude-fixture", task, expected=2)
    args = ("--provider", "claude-fixture", "--model", claude.MODEL, "--effort", claude.EFFORT,
            "--max-attempts", "1", "--timeout", "3", "--max-request-bytes", "256000",
            "--max-response-bytes", "128000", "--max-total-request-bytes", "256000",
            "--max-cost-microusd", "0", "--ttl", "300")
    preview = json.loads(cli("human", root, "llm-preview", task, *args).stdout)
    write_new(output / "approval-preview.json", preview)
    # This test harness only authorizes the fixed, public synthetic task above.
    cli("human", root, "llm-approve", task, *args, "--confirm-sha256", preview["approval_digest"])
    attempt = json.loads(cli("human", root, "llm-run-claude-fixture", task).stdout)["attempt_id"]
    cli("human", root, "llm-run-claude-fixture", task, expected=2)
    cli("human", root, "llm-verify", attempt)
    cli("worker", root, "intake", task)
    cli("worker", root, "import-llm", task, attempt)
    cli("worker", root, "export", task)
    final = json.loads(cli("human", root, "preview", task).stdout)
    write_new(output / "human-preview.json", final)
    status = json.loads(cli("human", root, "llm-status", task).stdout)
    write_new(output / "status.json", status)
    assert len(status["attempts"]) == 1 and status["attempts"][0]["state"] == "succeeded"
    assert final["broker_provenance"]["kind"] == "BROKER_CLAUDE_FIXTURE_EVIDENCE"
    write_new(output / "summary.json", {"status": "PASS", "root": str(root), "task": task,
        "attempt": attempt, "real_auth": False, "real_inference": False, "real_send": False,
        "flow": "approval -> durable attempt -> exec launcher -> fake -> validation -> worker -> human preview"})
    print(json.dumps({"status": "PASS", "evidence": str(output)}))


def probe_binary():
    output = Path(tempfile.mkdtemp(prefix="b1-cli-probe-", dir=REPO / ".local"))
    located = shutil.which("claude")
    result = {"real_auth": False, "real_inference": False, "network": "socket syscalls denied",
              "status": "UNVERIFIED", "scope": "version/help only, empty private homes, B1 exec confinement"}
    if located:
        binary = Path(located).resolve()
        content = binary.read_bytes()
        result.update(binary=str(binary), binary_digest=sha(content))
        if content.startswith(b"\x7fELF"):
            result["probes"] = []
            for flag in ("--version", "--help"):
                workspace = output / flag[2:]
                workspace.mkdir()
                claude.prepare(workspace)
                command = [sys.executable, "-I", "-S", "-B", str(REPO / "src/ccw/claude_launcher.py"),
                    str(workspace), str(os.getpid()), str(binary), result["binary_digest"], "", "", flag]
                try:
                    raw = capture(command, workspace, 10, 128000, input_bytes=b"")
                    (output / (flag[2:] + ".txt")).write_bytes(raw)
                    result["probes"].append({"flag": flag, "status": "PASS", "stdout_digest": sha(raw)})
                except (Invalid, OSError):
                    result["probes"].append({"flag": flag, "status": "BLOCKED_WITH_BOUNDARY_INTACT"})
                    break  # Do not weaken the sandbox or retry with normal HOME.
            if len(result["probes"]) == 2 and all(p["status"] == "PASS" for p in result["probes"]):
                result["status"] = "HELP_VERSION_ONLY"
    write_new(output / "summary.json", result)
    print(json.dumps({"evidence": str(output), **result}))


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe-installed-cli"]:
        probe_binary()
    elif not sys.argv[1:]:
        run()
    else:
        raise SystemExit("only --probe-installed-cli or no arguments")
