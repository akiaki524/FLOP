"""Offline-only handoff probe. The fake op reads a random canary from a pipe.

Never invokes installed op, opens a vault, or accepts a real secret/reference.
"""
import base64
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import REPO
from ccw import secret_handoff as handoff

REFERENCE = "op://ccw-dummy-vault/ccw-dummy-item/ccw-dummy-secret"


class FakeSource:
    def __init__(self, home, mode="success"):
        self.home, self.mode = home, mode
        self.value = b"ccw-dummy-" + secrets.token_hex(24).encode()
        self.calls = 0

    def public(self):
        return {"provider": "fake-op-offline", "reference": REFERENCE,
                "mode": self.mode, "fixture_digest": handoff.file_digest(__file__)}

    def resolve(self):
        self.calls += 1
        return handoff.exchange([str(Path(sys.executable).resolve()), "-I", "-S", "-B",
            str(Path(__file__).resolve()), "--fake-op", self.mode,
            "read", REFERENCE, "--account", "dummy", "--no-newline"], self.home,
            input_bytes=self.value, timeout=0.3 if self.mode == "timeout" else 5)


def descendant_alive(home):
    """True if the fake cache daemon (a reaped PID after handoff) still runs."""
    pid = int((Path(home) / "descendant.pid").read_text())
    try:
        state = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0]
        cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False
    return state != "Z" and b"--fake-op" in cmdline


def assert_absent(value, area, extra=()):
    variants = (value, base64.b64encode(value), value.hex().encode())
    for path in Path(area).rglob("*"):
        if path.is_file() and not path.is_symlink():
            content = path.read_bytes()
            if any(item in content for item in variants):
                raise AssertionError("dummy value persisted (details suppressed)")
    for content in extra:
        if any(item in content for item in variants):
            raise AssertionError("dummy value exported (details suppressed)")


def fake_op():
    mode = sys.argv[2]
    if sys.argv[3:] != ["read", REFERENCE, "--account", "dummy", "--no-newline"]:
        return 2
    value = sys.stdin.buffer.read(4097)
    if mode == "descendant":
        # Imitate a cache daemon: setsid + double fork, stdio detached. The
        # launcher must kill it and refuse the otherwise successful read.
        if os.fork() == 0:
            os.setsid()
            if os.fork() == 0:
                null = os.open("/dev/null", os.O_RDWR)
                for fd in (0, 1, 2):
                    os.dup2(null, fd)
                Path("descendant.pid").write_text(str(os.getpid()))
                time.sleep(30)
            os._exit(0)
        while not Path("descendant.pid").exists():
            time.sleep(0.01)
    if mode == "hold":
        # Keep the launcher holding the value while a memory probe runs.
        sys.stdout.buffer.write(value)
        sys.stdout.buffer.flush()
        time.sleep(2)
        return 0
    if mode == "timeout":
        time.sleep(5)
    if mode == "empty":
        return 0
    if mode == "invalid":
        sys.stdout.buffer.write(b"unexpected-format")
        return 0
    if mode == "oversized":
        sys.stdout.buffer.write(value * 200)
        return 0
    sys.stdout.buffer.write(value)
    sys.stderr.buffer.write(value)
    return 2 if mode == "failure" else 0


def run():
    area = Path(tempfile.mkdtemp(prefix="secret-handoff-", dir=REPO / ".local"))
    results, values = {}, []
    for mode in ("success", "failure", "empty", "invalid", "oversized", "timeout", "descendant"):
        attempt = area / mode
        attempt.mkdir(mode=0o700)
        home = area / f"home-{mode}"
        home.mkdir(mode=0o700)
        source = FakeSource(home, mode)
        values.append(source.value)
        public = handoff.plan(source, attempt, kind="OFFLINE_FIXTURE_HANDOFF")
        confirmation = handoff.digest(public)
        try:
            result = handoff.run_offline_fixture(source, attempt, confirmation)
        except handoff.HandoffError:
            if mode == "success":
                raise
            result = {"state": "expected-fail-closed"}
        else:
            if mode != "success":
                raise AssertionError("fault accepted")
        try:
            handoff.run_offline_fixture(source, attempt, confirmation)
        except handoff.HandoffError:
            pass
        else:
            raise AssertionError("attempt reused")
        if source.calls != 1:
            raise AssertionError("unexpected resolver calls")
        if mode == "descendant" and descendant_alive(home):
            raise AssertionError("resolver descendant survived")
        results[mode] = result["state"]
    diff = subprocess.run(["git", "diff", "--no-ext-diff", "HEAD"], cwd=REPO,
                          capture_output=True, check=True).stdout
    for value in values:
        assert_absent(value, area, [diff])
        for tree in ("src", "tests", "docs"):
            assert_absent(value, REPO / tree)
    summary = {"status": "PASS", "cases": results, "canary_absence": True,
               "checked": ["scratch files and evidence", "git diff", "src", "tests", "docs/checkpoints"],
               "kernel": platform.release(), "platform": platform.system(),
               "op_on_path": shutil.which("op"), "op_exe_on_path": shutil.which("op.exe"),
               "real_op_read": False, "real_secret": False, "real_inference": False,
               "windows_bridge": "UNVERIFIED; not enabled"}
    handoff.save_new(area / "summary.json", summary)
    print(json.dumps({"status": "PASS", "evidence": str(area), "real_op_read": False}))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fake-op"]:
        raise SystemExit(fake_op())
    if sys.argv[1:]:
        raise SystemExit("offline probe accepts no arguments")
    run()
