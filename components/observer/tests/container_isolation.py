"""Offline Docker isolation check, run from the user's working Ubuntu terminal.

No pulls, builds, live requests, secret reads, bind mounts, or automatic removal.
The container uses an existing Python base image; the final Observer image must
be verified again after it is built. Default action only prints the plan.
"""

import argparse
import ast
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
CANARY = ROOT / "tests" / "isolation_canary.txt"
IMAGE = "python:3.12-slim"
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]
CLIENT_DIRECTORY = None
ALLOWED_ENV = {"PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256",
               "HOME", "HOSTNAME", "PYTHONDONTWRITEBYTECODE",
               "OBSERVER_HOST_HOME", "OBSERVER_HOST_CANARY"}

PROBE = r'''
import errno
import json
import os
import socket
import stat

checks = {}
checks["nonroot_uid_gid"] = os.getuid() == 65532 and os.getgid() == 65532
with open("/proc/self/status", encoding="ascii") as stream:
    status = dict(line.rstrip().split(":", 1) for line in stream if ":" in line)
checks["no_capabilities"] = all(int(status[name].strip(), 16) == 0 for name in
    ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"))
checks["no_new_privileges"] = status["NoNewPrivs"].strip() == "1"
checks["seccomp_filter"] = status["Seccomp"].strip() == "2"
checks["loopback_only"] = set(name for _, name in socket.if_nameindex()) <= {"lo"}
# The host passes its real absolute HOME path; a missing or relative value fails
# this check closed rather than passing it unconditionally.
_host_home = os.environ.get("OBSERVER_HOST_HOME", "")
checks["host_home_not_visible"] = (
    _host_home.startswith("/") and _host_home != "/" and not os.path.lexists(_host_home))
checks["windows_mount_not_visible"] = not os.path.lexists("/mnt/c")
checks["docker_socket_absent"] = not any(os.path.lexists(path) for path in
    ("/run/docker.sock", "/var/run/docker.sock", "/run/desktop/docker.sock"))
# Only a newly supplied, public repository canary is tested; never a real key.
try:
    with open(CANARY_PATH, "rb"):
        pass
except OSError as exc:
    checks["public_host_canary_unreadable"] = exc.errno in (errno.ENOENT, errno.EACCES)
else:
    checks["public_host_canary_unreadable"] = False
try:
    fd = os.open("/observer-root-write-probe", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except OSError as exc:
    checks["root_write_denied"] = exc.errno in (errno.EROFS, errno.EACCES)
else:
    os.close(fd)
    checks["root_write_denied"] = False
state = os.stat("/state")
checks["private_state_directory"] = state.st_uid == 65532 and stat.S_IMODE(state.st_mode) == 0o700
fd = os.open("/state/public-probe", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.write(fd, b"public probe; no secrets")
os.close(fd)
checks["state_write_succeeded"] = True
print(json.dumps({"checks": checks, "passed": all(checks.values())}, sort_keys=True))
raise SystemExit(0 if all(checks.values()) else 1)
'''


def probe_source():
    return "CANARY_PATH = " + repr(str(CANARY)) + "\n" + PROBE


def host_home():
    """Resolve account HOME independently of env; inspect directory metadata only."""
    try:
        value = pwd.getpwuid(os.getuid()).pw_dir
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError
        path = Path(value)
        if not path.is_absolute() or path == Path("/") or not path.is_dir():
            raise ValueError
    except (KeyError, OSError, ValueError, TypeError):
        raise RuntimeError("HOST_HOME_UNSAFE") from None
    return str(path)


def create_command(image_id, name):
    return DOCKER + [
        "create", "--name", name, "--pull=never",
        "--network=none", "--user=65532:65532", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
        "--pids-limit=32", "--memory=256m", "--restart=no",
        "--tmpfs=/state:rw,noexec,nosuid,nodev,size=64m,mode=0700,uid=65532,gid=65532",
        "--env=HOME=/nonexistent", "--env=PYTHONDONTWRITEBYTECODE=1",
        # Machine-independent host references for the isolation probe: the real
        # host HOME and the public repository canary, resolved on the host.
        "--env=OBSERVER_HOST_HOME=" + host_home(),
        "--env=OBSERVER_HOST_CANARY=" + str(CANARY),
        "--label=technocore-observer.purpose=offline-isolation-check",
        "--interactive", "--entrypoint=python3", image_id, "-I", "-B", "-",
    ]


def run(command, **kwargs):
    if CLIENT_DIRECTORY is None:
        raise RuntimeError("PRIVATE_CLIENT_CONFIG_REQUIRED")
    # Use no host Docker credentials, contexts, proxy, TLS, or remote-host env.
    command = [command[0], "--config=" + str(CLIENT_DIRECTORY)] + command[1:]
    return subprocess.run(command, capture_output=True, text=True, check=kwargs.pop("check", True),
                          timeout=30, env={"PATH": os.environ.get("PATH", os.defpath),
                                           "HOME": str(CLIENT_DIRECTORY)}, **kwargs)


def configuration_checks(config, image_id):
    host = config["HostConfig"]
    process = config["Config"]
    env = process.get("Env") or []
    env_names = {item.split("=", 1)[0] for item in env}
    return {
        "local_image_id": config["Image"] == image_id,
        "network_none": host["NetworkMode"] == "none",
        "nonroot_user": process["User"] == "65532:65532",
        "root_readonly": host["ReadonlyRootfs"] is True,
        "not_privileged": host["Privileged"] is False,
        "no_host_namespaces": all(host.get(key) != "host" for key in ("PidMode", "IpcMode", "UTSMode", "UsernsMode")),
        "capabilities_dropped": host["CapDrop"] == ["ALL"] and not host.get("CapAdd"),
        "no_new_privileges": any(value in ("no-new-privileges", "no-new-privileges:true")
                                 for value in host.get("SecurityOpt") or []),
        "no_bind_mounts": not host.get("Binds") and not host.get("Mounts")
                          and not host.get("VolumesFrom"),
        "only_state_tmpfs": host.get("Tmpfs") == {
            "/state": "rw,noexec,nosuid,nodev,size=64m,mode=0700,uid=65532,gid=65532"},
        "no_other_mounts": all(mount.get("Type") == "tmpfs" and mount.get("Destination") == "/state"
                               for mount in config.get("Mounts") or []),
        "no_devices": not host.get("Devices") and not host.get("DeviceRequests")
                      and not host.get("DeviceCgroupRules"),
        "no_published_ports": not host.get("PortBindings") and not host.get("PublishAllPorts"),
        "bounded_resources": host.get("PidsLimit") == 32 and host.get("Memory") == 256 * 1024 * 1024,
        "no_restart": host.get("RestartPolicy", {}).get("Name") == "no",
        "expected_entrypoint": process.get("Entrypoint") == ["python3"] and process.get("Cmd") == ["-I", "-B", "-"],
        "environment_names_allowlisted": env_names <= ALLOWED_ENV,
        "home_is_nonexistent": "HOME=/nonexistent" in env,
    }


def execute():
    global CLIENT_DIRECTORY
    # Confirm public canary exists locally without reading any user secrets.
    if not CANARY.is_file() or CANARY.is_symlink():
        raise RuntimeError("PUBLIC_CANARY_MISSING")
    # Fresh, private directory is kept for review; never use ~/.docker/config.json.
    CLIENT_DIRECTORY = ROOT / "tests" / (".isolation-client-" + uuid.uuid4().hex[:12])
    print(json.dumps({"stage": "client_config", "create_path": str(CLIENT_DIRECTORY)}), flush=True)
    CLIENT_DIRECTORY.mkdir(mode=0o700)
    # This lists only the local image store; it cannot pull a missing image.
    ids = run(DOCKER + ["image", "ls", "--quiet", "--no-trunc", IMAGE]).stdout.split()
    if not ids:
        print(json.dumps({"status": "LOCAL_IMAGE_MISSING", "image": IMAGE,
                          "external_requests": 0, "container_created": False}))
        return 2
    image_id = ids[0]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise RuntimeError("INVALID_IMAGE_ID")
    name = "observer-isolation-" + uuid.uuid4().hex[:12]
    print(json.dumps({"stage": "create", "container": name, "image_id": image_id}), flush=True)
    run(create_command(image_id, name))
    config = json.loads(run(DOCKER + ["container", "inspect", name]).stdout)[0]
    checks = configuration_checks(config, image_id)
    if not all(checks.values()):
        print(json.dumps({"status": "CONFIG_REJECTED", "container": name, "checks": checks}))
        return 1  # Container is never started if inspection fails.
    result = run(DOCKER + ["start", "--attach", "--interactive", name], input=probe_source(), check=False)
    probe = json.loads(result.stdout)
    state = json.loads(run(DOCKER + ["container", "inspect", "--format", "{{json .State}}", name]).stdout)
    passed = (result.returncode == 0 and probe.get("passed") is True
              and state["ExitCode"] == 0 and not state["Running"])
    print(json.dumps({"status": "PASS" if passed else "FAIL", "container": name,
                      "image_id": image_id, "configuration_checks": checks,
                      "process_checks": probe["checks"], "external_requests": 0,
                      "scope": "base-image isolation; final Observer image not verified",
                      "retained": "stopped container retained; no automatic removal"}, sort_keys=True))
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Create and run one offline container from a local image")
    args = parser.parse_args()
    ast.parse(probe_source())
    if not args.execute:
        print(json.dumps({"mode": "PLAN_ONLY", "local_image": IMAGE,
                          "create_command": create_command("<local-sha256-image-id>", "<unique-container-name>"),
                          "canary_path": str(CANARY), "external_requests": 0}, indent=2))
        return 0
    try:
        return execute()
    except subprocess.CalledProcessError as exc:
        # Docker errors can include host details. Do not echo raw stderr/inspect.
        print(json.dumps({"status": "DOCKER_COMMAND_FAILED", "operation": exc.cmd[3],
                          "returncode": exc.returncode, "retry": False}), file=sys.stderr)
        return 1
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, RuntimeError) as exc:
        print(json.dumps({"status": "CHECK_FAILED", "error_class": type(exc).__name__,
                          "retry": False}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
