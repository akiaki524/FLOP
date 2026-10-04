"""Offline local-only container build/isolation/durability check; opt-in execute.

Never pulls, contacts Production, uses sudo, or removes images/containers.
Precondition: local pinned base, working local Docker, nonroot calling user.
Artifacts and stopped containers are retained for review.
"""

import argparse
import ast
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BASE = "python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
PAYLOAD = r'''
import json, os
from pathlib import Path
from technocore_full_capture.archive import Archive
from technocore_full_capture.pacing import BudgetPolicy, HostBudget
root = Path('/capture')
checks = {'nonroot': os.geteuid() != 0}
status = Path('/proc/self/status').read_text().splitlines()
checks['caps_zero'] = any(x.startswith('CapEff:') and int(x.split()[1], 16) == 0 for x in status)
checks['no_new_privileges'] = 'NoNewPrivs:\t1' in '\n'.join(status)
checks['readonly_rootfs'] = bool(os.statvfs('/').f_flag & os.ST_RDONLY)
room = root / 'room'
budget = root / 'budget'
room.mkdir(exist_ok=True)
budget.mkdir(exist_ok=True)
with HostBudget(budget, BudgetPolicy(1200, 300, 300, 600)).request():
    checks['budget_fsync_rename'] = True
with Archive(room, 'test-room', min_free_bytes=0) as archive:
    if archive.state['cursor'] is None:
        archive.append([b'{"seq":1,"text":"offline container fixture"}\n'], 1)
    checks['archive_fsync_rename_and_restart'] = archive.verify()['cursor'] == 1
print(json.dumps({'passed': all(checks.values()), 'checks': checks}), flush=True)
raise SystemExit(0 if all(checks.values()) else 1)
'''


def build_context():
    """Explicit offline context works with legacy and BuildKit builders alike.

    Never depends on Dockerfile-specific ignore support or sends the repo root.
    """
    sources = ["src/technocore_observer/" + name for name in
               ("__init__.py", "http.py", "protocol.py")]
    sources += ["src/technocore_full_capture/" + name for name in
                ("__init__.py", "__main__.py", "archive.py", "pacing.py", "probe.py",
                 "spool.py", "spool_archive.py", "scheduling.py", "deadline.py", "capture_first.py")]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as context:
        context.add(ROOT / "deploy/Containerfile.full-capture", arcname="Dockerfile", recursive=False)
        for name in sources:
            context.add(ROOT / name, arcname=name, recursive=False)
    return buffer.getvalue()


def configuration_checks(config, mount, uid):
    host, proc = config["HostConfig"], config["Config"]
    mounts = config.get("Mounts", [])
    return {
        "nonroot_uid": proc["User"] == uid and uid.split(":")[0] != "0",
        "readonly_rootfs": host["ReadonlyRootfs"] is True,
        "cap_drop_all": host["CapDrop"] == ["ALL"],
        "no_new_privileges": any(x in ("no-new-privileges", "no-new-privileges:true")
                                 for x in host.get("SecurityOpt", [])),
        "network_none": host["NetworkMode"] == "none",
        "restart_no": host["RestartPolicy"]["Name"] == "no",
        "one_dedicated_writable_bind": len(mounts) == 1 and mounts[0]["Type"] == "bind"
            and mounts[0]["Source"] == str(mount) and mounts[0]["Destination"] == "/capture"
            and mounts[0]["RW"] is True,
        "no_privilege_or_devices": not host["Privileged"] and not host.get("Devices")
            and not host.get("CapAdd") and not host.get("VolumesFrom"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    ast.parse(PAYLOAD)
    if not args.execute:
        print(json.dumps({"mode": "PLAN", "base_must_be_local": BASE,
                          "external_requests": 0, "build": "--pull=false --network=none",
                          "runtime": "nonroot host UID; --read-only --cap-drop=ALL --security-opt=no-new-privileges --network=none --restart=no",
                          "writable_mounts": 1, "runs": 2,
                          "retained": "image, stopped container and dedicated scratch bind"}))
        return 0
    if os.geteuid() == 0:
        print(json.dumps({"status": "STOPPED", "error": "NONROOT_CALLER_REQUIRED"}))
        return 2
    # Isolated Docker config avoids credentials/context selection; explicit local socket.
    directory = Path(tempfile.mkdtemp(prefix=".observer-test-fullcap-container-", dir=ROOT / "tests"))
    print(json.dumps({"create_path": str(directory)}), flush=True)
    client = directory / "docker-client"
    client.mkdir(mode=0o700)
    mount = directory / "capture"
    mount.mkdir(mode=0o700)
    env = {"PATH": os.defpath, "HOME": "/nonexistent", "DOCKER_BUILDKIT": "0"}
    docker = ["docker", "--config", str(client), "--host", "unix:///var/run/docker.sock"]
    def run(arguments, timeout=60):
        return subprocess.run(docker + arguments, env=env, cwd=ROOT, capture_output=True,
                              text=True, check=True, timeout=timeout).stdout
    stage = "local_base_check"
    try:
        run(["image", "inspect", "--format", "{{.Id}}", BASE])
        stage = "build"
        tag = "technocore-full-capture-review:" + directory.name.rsplit("-", 1)[-1]
        subprocess.run(docker + ["build", "--pull=false", "--network=none", "-t", tag, "-"],
                       input=build_context(), env=env, cwd=ROOT, capture_output=True,
                       check=True, timeout=180)
        image = json.loads(run(["image", "inspect", tag]))[0]
        if image["Config"]["User"] != "65532:65532":
            raise ValueError("IMAGE_USER_MISMATCH")
        stage = "create_inspect"
        uid = str(os.geteuid()) + ":" + str(os.getegid())
        name = "fullcap-review-" + directory.name.rsplit("-", 1)[-1]
        run(["create", "--name", name, "--user", uid, "--read-only", "--cap-drop=ALL",
             "--security-opt=no-new-privileges", "--network=none", "--restart=no",
             "--pids-limit=32", "--memory=256m", "--mount", "type=bind,src=" + str(mount) + ",dst=/capture",
             "--entrypoint=python3", image["Id"], "-I", "-B", "-c", PAYLOAD])
        config = json.loads(run(["inspect", name]))[0]
        checks = configuration_checks(config, mount, uid)
        if not all(checks.values()):
            raise ValueError("CONTAINER_CONFIGURATION_REJECTED")
        stage = "run_and_restart"
        runs, process_checks = [], []
        for _ in range(2):
            result = json.loads(run(["start", "--attach", name]))
            state = json.loads(run(["inspect", "--format", "{{json .State}}", name]))
            process_checks.append(result)
            runs.append(state["ExitCode"] == 0 and not state["Running"] and result["passed"] is True)
        report = {"status": "PASS" if all(runs) else "FAIL", "checks": checks,
                  "runs": runs, "process_checks": process_checks, "image": image["Id"], "container": name,
                  "external_requests": 0, "host_uid_override": uid,
                  "default_image_uid": "65532:65532", "retained_directory": str(directory)}
        (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
        return 0 if all(runs) else 1
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "UNVERIFIED", "stage": stage,
                          "error_class": type(exc).__name__, "automatic_retry": False,
                          "retained_directory": str(directory)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
