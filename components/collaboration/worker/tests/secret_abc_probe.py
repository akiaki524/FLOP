"""Dummy-only same-path baseline/current Linux probes for findings A and B.

No installed op, credentials, network or host policy changes. Baseline is a pinned fixture copied from the legacy Git object during monorepo migration. The harness reaps baseline/SIGKILL survivors.
Only status, PID, errno and public metadata are persisted, never dummy bytes.
"""
import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time
import types

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import REPO
from ccw import secret_handoff as handoff

BASELINE = "bd7138d77415bd32f045c11e7fb69b851ee6658e"
BASELINE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / f"secret_handoff_{BASELINE}.py"


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("probe synchronization timed out")


def resolver(area, case):
    (area / "resolver.pid").write_text(str(os.getpid()))
    if case == "A":
        value = b"ccw-dummy-" + os.urandom(24).hex().encode()
        os.write(1, value)
        os.write(2, value)
        (area / "ready").touch()
        wait_for(lambda: (area / "release").exists())
        return 0
    if os.fork() == 0:
        os.setsid()
        if os.fork():
            os._exit(0)
        null = os.open("/dev/null", os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.close(null)
        (area / "daemon.pid").write_text(str(os.getpid()))
        time.sleep(30)
        os._exit(0)
    wait_for(lambda: (area / "daemon.pid").exists())
    (area / "ready").touch()
    time.sleep(30)
    return 0


def launcher(area, case, version):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    module = handoff
    if version == "baseline":
        module = types.ModuleType("ccw.abc_baseline")
        module.__file__ = handoff.__file__
        module.__package__ = "ccw"
        sys.modules[module.__name__] = module
        exec(compile((area.parent / "baseline.py").read_bytes(), handoff.__file__, "exec"),
             module.__dict__)
    if case == "A":
        original = module.pump
        def gated_pump(*args, **kwargs):
            # Keep output queued so the attacker and launcher do not race to
            # read the canary. Identical synchronization for both revisions.
            wait_for(lambda: (area / "release").exists())
            return original(*args, **kwargs)
        module.pump = gated_pump

    class Source:
        def public(self):
            return {"provider": "offline-abc-fixture", "case": case}

        def resolve(self):
            return module.exchange([sys.executable, "-B", __file__, "--resolver", str(area), case],
                                   area, timeout=15)

    attempt = area / "attempt"
    attempt.mkdir(mode=0o700)
    source = Source()
    public = module.plan(source, attempt, kind="OFFLINE_FIXTURE_HANDOFF")
    try:
        module.run_offline_fixture(source, attempt, module.digest(public))
    except module.HandoffError:
        return 2
    return 0


def attack(pid):
    result = {}
    for fd in (1, 2):
        opened = None
        try:
            opened = os.open(f"/proc/{pid}/fd/{fd}", os.O_RDONLY | os.O_NONBLOCK)
            data = os.read(opened, 4096)
            result[str(fd)] = {"dummy_obtained": bool(re.fullmatch(
                rb"ccw-dummy-[a-f0-9]{48}", data)), "errno": None}
        except OSError as exc:
            result[str(fd)] = {"dummy_obtained": False, "errno": errno.errorcode[exc.errno]}
        finally:
            if opened is not None:
                os.close(opened)
    print(json.dumps(result))
    return 0


def process_case(root, case, version, signum=None):
    suffix = signum.name if signum else "fd-reopen"
    area = root / f"{case}-{version}-{suffix}"
    area.mkdir(mode=0o700)
    before = handoff.children()
    launcher_process = subprocess.Popen(
        [sys.executable, "-B", __file__, "--launcher", str(area), case, version],
        env={}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(lambda: (area / "ready").exists())
        resolver_pid = int((area / "resolver.pid").read_text())
        if case == "A":
            # This process is a sibling of the launcher, not a resolver ancestor.
            probe = subprocess.run([sys.executable, "-B", __file__, "--attack", str(resolver_pid)],
                                   env={}, capture_output=True, timeout=10, check=True)
            observed = json.loads(probe.stdout)
            (area / "release").touch()
        else:
            os.kill(launcher_process.pid, signum)
            observed = {}
        code = launcher_process.wait(timeout=20)
        pids = [int(path.read_text()) for path in (area / "resolver.pid", area / "daemon.pid")
                if path.exists()]
        observed.update({"launcher_returncode": code,
                         "resolver_exists_after": Path(f"/proc/{resolver_pid}").exists(),
                         "descendants_exist_after": any(Path(f"/proc/{pid}").exists() for pid in pids),
                         "attempt_consumed": (area / "attempt/consumed.json").exists(),
                         "failure_recorded": (area / "attempt/failure.json").exists(),
                         "success_recorded": (area / "attempt/result.json").exists()})
        return observed
    finally:
        if launcher_process.poll() is None:
            launcher_process.kill()
        launcher_process.wait()
        handoff.terminate_descendants(before)
        assert not (handoff.children() - before), "harness left descendants"


def run():
    root = Path(tempfile.mkdtemp(prefix="secret-abc-", dir=REPO / ".local"))
    baseline = BASELINE_FIXTURE.read_bytes()
    (root / "baseline.py").write_bytes(baseline)
    previous = ctypes.c_int()
    handoff.prctl(handoff.PR_GET_CHILD_SUBREAPER, ctypes.addressof(previous))
    handoff.prctl(handoff.PR_SET_CHILD_SUBREAPER, 1)
    try:
        a = {version: process_case(root, "A", version) for version in ("baseline", "current")}
        b = {version: {sig.name: process_case(root, "B", version, sig)
                       for sig in (*handoff.TERMINATION_SIGNALS, signal.SIGKILL)}
             for version in ("baseline", "current")}
    finally:
        handoff.prctl(handoff.PR_SET_CHILD_SUBREAPER, previous.value)
    checks = [all(a["baseline"][str(fd)]["dummy_obtained"] for fd in (1, 2)),
              all(a["current"][str(fd)] == {"dummy_obtained": False, "errno": "ENXIO"}
                  for fd in (1, 2)),
              a["current"]["success_recorded"], a["current"]["launcher_returncode"] == 0]
    for version in b:
        for name, result in b[version].items():
            # Old SIGINT already unwinds via KeyboardInterrupt; other defaults
            # terminate without cleanup. SIGKILL remains an explicit residual.
            cleaned = name == "SIGINT" or (version == "current" and name != "SIGKILL")
            checks += [result["attempt_consumed"], not result["success_recorded"],
                       result["descendants_exist_after"] == (not cleaned),
                       result["failure_recorded"] == cleaned,
                       result["launcher_returncode"] == (2 if cleaned else -getattr(signal, name))]
    summary = {"status": "PASS" if all(checks) else "FAIL", "baseline": BASELINE,
               "A": a, "B": b, "harness_cleaned_all_survivors": True,
               "kernel": platform.release(), "same_uid": os.getuid(),
               "ptrace_scope": Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip(),
               "real_op_read": False, "real_secret": False, "real_inference": False}
    handoff.save_new(root / "summary.json", summary)
    print(json.dumps({"status": summary["status"], "evidence": str(root)}))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--resolver"]:
        raise SystemExit(resolver(Path(sys.argv[2]), sys.argv[3]))
    if sys.argv[1:2] == ["--launcher"]:
        raise SystemExit(launcher(Path(sys.argv[2]), sys.argv[3], sys.argv[4]))
    if sys.argv[1:2] == ["--attack"]:
        raise SystemExit(attack(int(sys.argv[2])))
    if sys.argv[1:]:
        raise SystemExit("offline probe accepts no external inputs")
    raise SystemExit(run())
