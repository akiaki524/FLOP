"""Dummy-only same-path probe for P1-A': consumer stdio before non-dumpable.

A same-UID sibling of the launcher (not an ancestor) watches for the consumer
and, while it is still dumpable after exec, reopens /proc/PID/fd/0,1,2:
  forge: write the ready byte into stdout, then read the value from stdin;
  race:  only read stdin (and stdout/stderr echoes) competing with the consumer.
Baseline launcher/consumer/isolation come from pinned fixtures copied from the legacy Git object during monorepo migration and run from `.local/` only. No installed op, credentials, network or host policy
changes. Only booleans, errno and public metadata are saved, never dummy bytes.
"""
import errno
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import time

BASELINE = "85d8ce4227d65ae92338d91eb52ae13243dc2127"
BASELINE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / f"secret_consumer_window_{BASELINE}"
MODULES = ("__init__.py", "isolation.py", "secret_consumer.py", "secret_handoff.py")
VALUE = rb"ccw-dummy-[a-f0-9]{48}"
TRIALS = {"forge": 5, "race": 10}


def launcher(package_root, area):
    sys.path.insert(0, str(package_root))
    from ccw import secret_handoff as handoff

    class Source:
        def public(self):
            return {"provider": "offline-consumer-window-fixture"}

        def resolve(self):
            # Generated in memory; never written to argv, env, files or output.
            return b"ccw-dummy-" + os.urandom(24).hex().encode()

    attempt = area / "attempt"
    attempt.mkdir(mode=0o700)
    source = Source()
    public = handoff.plan(source, attempt, kind="OFFLINE_FIXTURE_HANDOFF")
    try:
        handoff.run_offline_fixture(source, attempt, handoff.digest(public))
    except handoff.HandoffError:
        return 2
    return 0


def reopen(pid, fd, flags):
    try:
        return os.open(f"/proc/{pid}/fd/{fd}", flags | os.O_NONBLOCK), None
    except OSError as exc:
        return None, errno.errorcode[exc.errno]


def attack(marker, mode):
    """Reopen the target consumer's stdio as soon as it appears."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                if marker not in Path(f"/proc/{entry.name}/cmdline").read_bytes():
                    continue
            except OSError:
                continue
            return exploit(int(entry.name), mode)
        time.sleep(0.0005)
    print(json.dumps({"consumer_seen": False}))
    return 0


def exploit(pid, mode):
    handles, result = {}, {"consumer_seen": True, "open_errno": {}, "ready_forged": False}
    for fd in (0, 1, 2):
        handles[fd], result["open_errno"][str(fd)] = reopen(pid, fd, os.O_RDONLY)
    if mode == "forge":
        writer, result["forge_open_errno"] = reopen(pid, 1, os.O_WRONLY)
        if writer is not None:
            try:
                result["ready_forged"] = os.write(writer, b"R") == 1
            finally:
                os.close(writer)
    obtained = {str(fd): False for fd in handles}
    start = time.monotonic()
    while time.monotonic() < start + 5 and any(handles.values()) and not obtained["0"]:
        for fd, handle in handles.items():
            # Hold the stdout reopen but drain it (for later echoes) only after
            # the ready exchange, so the probe never steals the ready byte.
            if handle is None or (fd == 1 and time.monotonic() < start + 1):
                continue
            try:
                data = os.read(handle, 4096)
            except BlockingIOError:
                continue
            if re.search(VALUE, data):
                obtained[str(fd)] = True
    for handle in handles.values():
        if handle is not None:
            os.close(handle)
    result["dummy_obtained"] = obtained
    print(json.dumps(result))
    return 0


def trial(root, version, package_root, mode, number):
    area = root / f"{version}-{mode}-{number}"
    area.mkdir(mode=0o700)
    # Unique per trial; the consumer's workspace path is on its command line.
    marker = str(area / "attempt" / "consumer").encode()
    attacker = subprocess.Popen([sys.executable, "-B", __file__, "--attack", marker.hex(), mode],
                                env={}, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.2)
        launched = subprocess.run([sys.executable, "-B", __file__, "--launcher",
                                   str(package_root), str(area)], env={}, timeout=60,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        observed = json.loads(attacker.communicate(timeout=30)[0])
    finally:
        if attacker.poll() is None:
            attacker.kill()
        attacker.wait()
    observed.update({"launcher_returncode": launched.returncode,
                     "attempt_consumed": (area / "attempt/consumed.json").exists(),
                     "success_recorded": (area / "attempt/result.json").exists(),
                     "failure_recorded": (area / "attempt/failure.json").exists()})
    return observed


def obtained(result):
    return any(result.get("dummy_obtained", {}).values())


def run():
    repo = Path(__file__).resolve().parents[1]
    root = Path(tempfile.mkdtemp(prefix="secret-consumer-window-", dir=repo / ".local"))
    baseline = root / "baseline-src"
    (baseline / "ccw").mkdir(parents=True)
    for name in MODULES:
        (baseline / "ccw" / name).write_bytes(
            (BASELINE_FIXTURE / "ccw" / name).read_bytes())
    versions = {"baseline": baseline, "current": repo / "src"}
    results = {version: {mode: [trial(root, version, source, mode, number)
                                for number in range(count)]
                         for mode, count in TRIALS.items()}
               for version, source in versions.items()}
    base, current = results["baseline"], results["current"]
    checks = [
        # Control: the attacker really reached the dumpable window and the
        # baseline pipe reopen yields the value (always with a forged ready).
        all(r["consumer_seen"] and r["open_errno"]["0"] is None for rs in base.values() for r in rs),
        all(r["ready_forged"] and r["dummy_obtained"]["0"] and not r["success_recorded"]
            for r in base["forge"]),
        # Current: ENXIO (not EACCES) proves the attempt happened while the
        # consumer was still dumpable and that the object cannot be reopened.
        all(r["consumer_seen"] and set(r["open_errno"].values()) == {"ENXIO"}
            for rs in current.values() for r in rs),
        all(r.get("forge_open_errno") == "ENXIO" and not r["ready_forged"] for r in current["forge"]),
        not any(obtained(r) for rs in current.values() for r in rs),
        all(r["launcher_returncode"] == 0 and r["success_recorded"] and not r["failure_recorded"]
            and r["attempt_consumed"] for rs in current.values() for r in rs),
    ]
    summary = {"status": "PASS" if all(checks) else "FAIL", "baseline": BASELINE,
               "results": results,
               "baseline_race_dummy_obtained": sum(obtained(r) for r in base["race"]),
               "kernel": platform.release(), "same_uid": os.getuid(),
               "ptrace_scope": Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip(),
               "attacker": "same-uid-launcher-sibling", "real_op_read": False,
               "real_secret": False, "real_inference": False}
    path = root / "summary.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(summary, stream, sort_keys=True, indent=1)
    print(json.dumps({"status": summary["status"], "evidence": str(root)}))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--launcher"]:
        raise SystemExit(launcher(Path(sys.argv[2]), Path(sys.argv[3])))
    if sys.argv[1:2] == ["--attack"]:
        raise SystemExit(attack(bytes.fromhex(sys.argv[2]), sys.argv[3]))
    if sys.argv[1:]:
        raise SystemExit("offline probe accepts no external inputs")
    raise SystemExit(run())
