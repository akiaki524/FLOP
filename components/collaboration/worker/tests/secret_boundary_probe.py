"""Independent offline probe of the handoff process boundary.

This probe process plays the same-UID ancestor (e.g. an Agent that started
the launcher). It tries the reads the independent review reproduced, against
real launcher/consumer processes, plus a dumpable control proving the method
works. Dummy values only; never invokes op or opens a vault.
"""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import REPO
from secret_handoff_probe import assert_absent
from ccw import secret_handoff as handoff

LAUNCHER = """
import sys
sys.path.insert(0, sys.argv[1])
from secret_handoff_probe import FakeSource
from ccw import secret_handoff as handoff
from pathlib import Path
source = FakeSource(Path(sys.argv[2]), "hold")
source.value = bytes.fromhex(sys.stdin.readline().strip())
attempt = Path(sys.argv[3])
public = handoff.plan(source, attempt, kind="OFFLINE_FIXTURE_HANDOFF")
print(handoff.run_offline_fixture(source, attempt, handoff.digest(public))["state"])
"""

HOLDER = "import sys,time; v=sys.stdin.buffer.readline(); time.sleep(3)"


def memory_read(pid, value):
    """'denied', 'found' or 'absent' for a same-UID read of /proc/PID/mem."""
    try:
        maps = (Path("/proc") / str(pid) / "maps").read_text().splitlines()
        with open(f"/proc/{pid}/mem", "rb") as memory:
            for line in maps:
                span, mode = line.split()[:2]
                if "r" not in mode:
                    continue
                start, end = (int(part, 16) for part in span.split("-"))
                try:
                    memory.seek(start)
                    if value in memory.read(end - start):
                        return "found"
                except OSError:
                    continue
    except PermissionError:
        return "denied"
    return "absent"


def fd_open(pid):
    try:
        list((Path("/proc") / str(pid) / "fd").iterdir())
    except PermissionError:
        return "denied"
    return "listable"


def descendants(root):
    found, frontier = [], [root]
    while frontier:
        parent = frontier.pop()
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    if int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1]) == parent:
                        found.append(int(entry.name))
                        frontier.append(int(entry.name))
                except (OSError, IndexError):
                    pass
    return found


def cmdline(pid):
    try:
        return (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return b""


def launcher_case(area):
    value = b"ccw-dummy-" + secrets.token_hex(24).encode()
    home, attempt = area / "launcher-home", area / "launcher-attempt"
    home.mkdir(mode=0o700)
    attempt.mkdir(mode=0o700)
    launcher = subprocess.Popen([sys.executable, "-B", "-c", LAUNCHER, str(REPO / "tests"),
                                 str(home), str(attempt)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO / "src")})
    launcher.stdin.write(value.hex().encode() + b"\n")
    launcher.stdin.close()
    result = {"launcher_memory": None, "launcher_fd": None, "resolver_child_memory": None}
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and result["launcher_memory"] is None:
        resolver = [pid for pid in descendants(launcher.pid) if b"--fake-op" in cmdline(pid)]
        if resolver:
            time.sleep(0.3)  # the fake op has written; the launcher now holds the value
            result["launcher_memory"] = memory_read(launcher.pid, value)
            result["launcher_fd"] = fd_open(launcher.pid)
            # Residual by design: exec'd resolver (real op) is dumpable again.
            result["resolver_child_memory"] = memory_read(resolver[0], value)
        time.sleep(0.02)
    stdout = launcher.stdout.read()
    launcher.stderr.read()
    launcher.wait(timeout=20)
    result["handoff_state"] = stdout.decode().strip()
    result["value_persisted"] = False
    assert_absent(value, area)
    return result


def consumer_case(area):
    value = b"ccw-dummy-" + secrets.token_hex(24).encode()
    workspace = area / "consumer"
    workspace.mkdir(mode=0o700)
    # The consumer accepts only launcher socketpair stdio (no reopenable pipes).
    stdin, stdout, stderr = pairs = [socket.socketpair() for _ in range(3)]
    with subprocess.Popen([sys.executable, "-I", "-S", "-B", str(REPO / "src/ccw/secret_consumer.py"),
                           str(workspace), str(os.getpid())], stdin=stdin[1],
                          stdout=stdout[1], stderr=stderr[1], env={}) as consumer:
        for _, theirs in pairs:
            theirs.close()
        ready = stdout[0].recv(1)
        stdin[0].sendall(value)  # stdin stays open: the consumer holds the value, blocked in read
        time.sleep(0.3)
        result = {"ready_byte": ready == handoff.CONSUMER_READY,
                  "consumer_memory": memory_read(consumer.pid, value),
                  "consumer_fd": fd_open(consumer.pid)}
        stdin[0].close()
        while stdout[0].recv(4096):
            pass
        result["consumer_exit"] = consumer.wait(timeout=10)
    for ours, _ in pairs:
        ours.close()
    return result


def control_case():
    """Same method against an ordinary dumpable process: must find the value."""
    value = b"ccw-dummy-" + secrets.token_hex(24).encode()
    with subprocess.Popen([sys.executable, "-c", HOLDER], stdin=subprocess.PIPE, env={}) as holder:
        holder.stdin.write(value + b"\n")
        holder.stdin.flush()
        time.sleep(0.3)
        found = memory_read(holder.pid, value)
        holder.kill()
    return found


def descendant_case(area):
    script = ("import os,sys,time\n"
              "if os.fork()==0:\n"
              " if sys.argv[1]=='daemon':\n"
              "  os.setsid()\n"
              "  if os.fork(): os._exit(0)\n"
              "  n=os.open('/dev/null',os.O_RDWR)\n"
              "  [os.dup2(n,f) for f in (0,1,2)]\n"
              " open(sys.argv[2],'w').write(str(os.getpid())); time.sleep(30); os._exit(0)\n"
              "while not os.path.exists(sys.argv[2]): time.sleep(0.01)\n"
              "sys.stdout.write('ccw-dummy-'+'a'*20)\n")
    result = {}
    for mode in ("inherit", "daemon"):
        marker = area / f"{mode}.pid"
        try:
            handoff.exchange([sys.executable, "-c", script, mode, str(marker)], area, timeout=3)
            outcome = "succeeded"
        except handoff.HandoffError:
            outcome = "failed-closed"
        pid = int(marker.read_text())
        alive = Path(f"/proc/{pid}").exists() and b"-c" in cmdline(pid)
        result[mode] = {"exchange": outcome, "descendant_alive_after": alive}
    return result


def run():
    area = Path(tempfile.mkdtemp(prefix="secret-boundary-", dir=REPO / ".local"))
    summary = {"control_dumpable_process_memory": control_case(),
               "launcher": launcher_case(area), "consumer": consumer_case(area),
               "descendants": descendant_case(area),
               "launch_guard_refuses_this_session": bool(handoff.agent_markers()) or not os.isatty(0),
               "ptrace_scope": Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip(),
               "real_op_read": False, "real_secret": False}
    expected = (summary["control_dumpable_process_memory"] == "found"
                and summary["launcher"]["launcher_memory"] == "denied"
                and summary["launcher"]["launcher_fd"] == "denied"
                and summary["launcher"]["handoff_state"] == "dummy-consumer-succeeded"
                and summary["consumer"] == {"ready_byte": True, "consumer_memory": "denied",
                                            "consumer_fd": "denied", "consumer_exit": 0}
                and all(item == {"exchange": "failed-closed", "descendant_alive_after": False}
                        for item in summary["descendants"].values())
                and summary["launch_guard_refuses_this_session"])
    summary["status"] = "PASS" if expected else "FAIL"
    handoff.save_new(area / "summary.json", summary)
    print(json.dumps({"status": summary["status"], "evidence": str(area)}))
    return 0 if expected else 1


if __name__ == "__main__":
    if sys.argv[1:]:
        raise SystemExit("offline probe accepts no arguments")
    raise SystemExit(run())
