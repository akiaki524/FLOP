"""A standalone fake CLI, executed via exec under the actual B1 launcher.

No ccw imports, auth files, sockets used for communication, or real CLI calls.
Fault names are fixed synthetic task markers, included in the approved prompt.
"""
import ctypes
import errno
import json
import os
from pathlib import Path
import socket
import sys
import time


def main():
    if sys.argv[1:] == ["--version"]:
        print("fake-claude-b1-1")
        return
    if sys.argv[1:] == ["auth", "status"]:
        print(json.dumps({"fixture": True, "logged_in": True, "provider": "anthropic",
            "method": "subscription-oauth", "billing": "subscription", "extra_usage": "disabled"}))
        return
    data = json.loads(sys.stdin.read())
    if "probe" in data:
        results = {}
        def denied(name, operation):
            try:
                operation()
            except OSError as exc:
                results[name] = exc.errno in (errno.EPERM, errno.EACCES)
            else:
                results[name] = False
        for n, path in enumerate(data["probe"]):
            denied("read-" + str(n), lambda p=path: Path(p).read_bytes())
            denied("write-" + str(n), lambda p=path: Path(p).write_text("damaged"))
        for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
            denied("socket-" + str(family), lambda f=family: socket.socket(f))
        denied("socketpair", socket.socketpair)
        denied("fork", os.fork)
        denied("signal-parent", lambda: os.kill(os.getppid(), 0))
        libc = ctypes.CDLL(None, use_errno=True)
        for name, call in (("ptrace", 101), ("process_vm", 310), ("pidfd", 434), ("io_uring", 425)):
            result = libc.syscall(call, os.getppid(), 0, 0, 0, 0, 0)
            results[name] = result == -1 and ctypes.get_errno() == errno.EPERM
        results["minimal-env"] = set(os.environ) <= {"HOME", "CLAUDE_CONFIG_DIR", "TMPDIR", "LANG",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_AUTOUPDATER", "LC_CTYPE"}
        Path(os.environ["TMPDIR"], "positive-control").write_text("ok")
        results["workspace-write"] = Path(os.environ["TMPDIR"], "positive-control").read_text() == "ok"
        print(json.dumps(results))
        return
    case = data["request"].split("B1_FIXTURE:")[-1] if "B1_FIXTURE:" in data["request"] else "success"
    if case == "timeout":
        time.sleep(130)
    if case == "closed-output":
        os.close(1)
        os.close(2)
        time.sleep(130)
    if case == "crash":
        os._exit(76)
    if case == "oversized":
        os.write(1, b"x" * 256001)
        return
    model = sys.argv[sys.argv.index("--model") + 1]
    report = {"provider": "claude-cli-offline-v1", "summary": "Offline fake CLI review; no semantic assurance.",
              "findings": [], "unverified": ["Synthetic fixture, no real authentication or inference."]}
    for source in data["sources"]:
        for line, text in enumerate(source["text"].splitlines(), 1):
            if "TODO" in text:
                report["findings"].append({"severity": "info", "observation": "TODO remains.",
                    "suggestion": "Human should verify completion criteria.", "evidence": {
                        "source_id": source["id"], "sha256": source["sha256"],
                        "start_line": line, "end_line": line, "quote": text}})
    result = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
              "session_id": "fixture-session-not-provider-request", "structured_output": report,
              "modelUsage": {model: {}}, "usage": {"input_tokens": 100, "output_tokens": 20,
                  "cache_read_input_tokens": 40, "cache_creation_input_tokens": 10}, "total_cost_usd": 0.001}
    if case == "quota":
        result.update(subtype="error_during_execution", is_error=True)
    if case == "bad-quote":
        report["findings"][0]["evidence"]["quote"] = "forged quote"
    if case == "invalid":
        report["tool"] = "forbidden"
    if case == "model-mismatch":
        result["modelUsage"] = {"unapproved-model": {}}
    if case == "model-missing":
        del result["modelUsage"]
    if case == "usage-missing":
        del result["usage"]
    print(json.dumps(result))


if __name__ == "__main__":
    main()
