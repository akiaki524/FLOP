"""Read-only PRE and fresh pre-activation checks; no repair or live execution here."""
import json
import os
from pathlib import Path
import socket
import stat
import sys
import time

from .common import (CANDIDATE, ROLES, V2Error, command, docker_absent, read_bytes, read_json,
                     require, sha256, timer_state, unit_state, unit_fingerprint, unique_json)

VOLUMES = {"spool": (40, 10 * 1024**3), "archive": (41, 8 * 1024**3),
           "control": (42, 256 * 1024**2)}
IDENTITY_PARTS = ("spool/data", "archive/data", "control/registry", "control/budget")


def require_host():
    require(os.geteuid() == 0 and socket.gethostname() == "node-01", "V2_ROOT_NODE01_REQUIRED")


def _json_command(runner, argv, code):
    try:
        return json.loads(command(runner, argv, code))
    except (ValueError, TypeError) as exc:
        raise V2Error(code + "_JSON") from exc


def v2_mount_check(runner, layout):
    """Candidate mount identity checks, but reads old receipt as data without bundle authority."""
    files = _json_command(runner, ["findmnt", "--json", "--mountpoint", "/srv/technocore-data",
                                   "--output", "SOURCE,FSTYPE,TARGET"], "BACKING_MOUNT")
    rows = files.get("filesystems", [])
    require(len(rows) == 1 and rows[0].get("source") == "/dev/sdb" and
            rows[0].get("fstype") == "ext4", "BACKING_MOUNT_CHANGED")
    actual = {}
    for name, (number, size) in VOLUMES.items():
        p = layout.shared_root / name
        require(p.resolve() == p and os.path.ismount(p), "V2_MOUNT_MISSING")
        require(p.stat().st_dev == os.stat("/dev/loop" + str(number)).st_rdev,
                "V2_MOUNT_DEVICE")
        backing = Path(f"/sys/block/loop{number}/loop/backing_file").read_text().strip()
        require(backing == f"/srv/technocore-data/production-capture-volumes/{name}.img",
                "V2_LOOP_BACKING")
        row = Path(backing).stat()
        require(row.st_size == size and row.st_blocks * 512 >= size, "V2_ALLOCATION")
    for part in IDENTITY_PARTS:
        path = layout.shared_root / part
        row = path.stat()
        actual[part] = {"device": row.st_dev, "inode": row.st_ino,
                        "uid": row.st_uid, "mode": stat.S_IMODE(row.st_mode)}
    receipt = read_json(layout.shared_etc / "30-mount-identity.json")
    paths = receipt.get("paths")
    require(isinstance(paths, dict) and set(paths) == set(IDENTITY_PARTS),
            "V2_MOUNT_RECEIPT_SHAPE")
    for part in IDENTITY_PARTS:
        require(all(paths[part].get(key) == value for key, value in actual[part].items()),
                "V2_MOUNT_IDENTITY_CHANGED")
    return actual


def policy_check(path, source_root, *, now=None):
    """D4: host-side, pure Candidate validator; no image start or network GET."""
    path = Path(path)
    data = read_bytes(path, max_bytes=1024 * 1024)
    try:
        config = json.loads(data, object_pairs_hook=unique_json)
    except (ValueError, TypeError, V2Error) as exc:
        raise V2Error("POLICY_JSON") from exc
    source_path = str(Path(source_root) / "src")
    if source_path not in sys.path:
        sys.path.insert(0, source_path)
    from technocore_full_capture.production import validate_config
    try:
        validate_config(config)
    except (ValueError, TypeError, KeyError) as exc:
        raise V2Error("POLICY_CANDIDATE_INVALID") from exc
    now = time.time() if now is None else now
    require(config["start_at"] <= now and
            (config["end_at"] is None or now < config["end_at"]), "POLICY_WINDOW_INACTIVE")
    return sha256(data)


def network_check(runner, layout):
    egress = read_json(layout.shared_etc / "egress.json")
    name, expected_id = egress.get("network"), egress.get("network_id")
    require(isinstance(name, str) and name and isinstance(expected_id, str) and expected_id,
            "EGRESS_IDENTITY")
    rows = _json_command(runner, ["docker", "network", "inspect", name], "NETWORK_INSPECT")
    require(isinstance(rows, list) and len(rows) == 1 and rows[0].get("Id") == expected_id
            and rows[0].get("Name") == name, "NETWORK_CHANGED")
    return {"network": name, "network_id": expected_id}


def _named_container(runner, name):
    result = runner.run(["docker", "inspect", "--type=container", name], timeout=30)
    if result.returncode:
        if docker_absent(result.stderr, name):
            return None
        raise V2Error("OLD_CONTAINER_INSPECT_UNKNOWN")
    try:
        rows = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise V2Error("OLD_CONTAINER_INSPECT_JSON") from exc
    require(isinstance(rows, list) and len(rows) == 1, "OLD_CONTAINER_INSPECT_SHAPE")
    return rows[0]


def old_writer_check(runner):
    observed = {}
    for role in ROLES:
        name = "tc-cap-loop-01-lobby-" + role
        row = _named_container(runner, name)
        require(row is None or row.get("State", {}).get("Running") is False,
                "OLD_WRITER_RUNNING")
        observed[role] = "ABSENT" if row is None else "STOPPED"
    return observed


def shared_writer_check(runner, layout, allowed_ids=()):
    """Block any other running container with writable Capture data binds."""
    output = command(runner, ["docker", "ps", "-q", "--no-trunc"], "RUNNING_CONTAINERS")
    for cid in output.split():
        # Imported here to avoid allowing a name as an emergency stop target.
        from .common import docker_inspect
        row = docker_inspect(runner, cid)
        require(row is not None, "RUNNING_CONTAINER_DISAPPEARED")
        if cid in allowed_ids:
            continue
        for mount in row.get("Mounts", []):
            source = mount.get("Source", "")
            if mount.get("RW") and (source == str(layout.shared_root) or
                    source.startswith(str(layout.shared_root) + "/")):
                raise V2Error("FOREIGN_CAPTURE_WRITER_RUNNING")



def process_writer_check(shared_root, proc_root=Path("/proc")):
    """Inspect writable open Capture data FDs without reading process argv/secrets."""
    prefixes = tuple(str(Path(shared_root) / part) for part in IDENTITY_PARTS)
    for process in proc_root.iterdir():
        if not process.name.isdigit():
            continue
        directory = process / "fd"
        try:
            descriptors = list(directory.iterdir())
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise V2Error("WRITER_FD_INVENTORY_UNKNOWN") from exc
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
                if not any(target == prefix or target.startswith(prefix + "/")
                           for prefix in prefixes):
                    continue
                info = (process / "fdinfo" / descriptor.name).read_text()
                flags = next(int(line.split()[1], 8) for line in info.splitlines()
                             if line.startswith("flags:"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError, StopIteration) as exc:
                raise V2Error("WRITER_FD_INVENTORY_UNKNOWN") from exc
            require(flags & os.O_ACCMODE == os.O_RDONLY, "FOREIGN_CAPTURE_FD_WRITER")


def resources(layout):
    # Before STAGE, the release namespace may not exist. /opt is its fixed
    # filesystem ancestor; do not fall through to an unrelated mount.
    for path in (layout.release_base.parent, layout.release_base.parent.parent):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise V2Error("RESOURCE_RELEASE_PATH") from exc
        require(stat.S_ISDIR(mode), "RESOURCE_RELEASE_PATH")
        try:
            row = os.statvfs(path)
        except OSError as exc:
            raise V2Error("RESOURCE_RELEASE_PATH") from exc
        break
    else:
        raise V2Error("RESOURCE_RELEASE_PATH")
    require(row.f_bavail * row.f_frsize >= 2 * 1024**3 and row.f_favail >= 10000,
            "RESOURCE_DISK_OR_INODE_LOW")
    mem = Path("/proc/meminfo").read_text()
    available = next((int(line.split()[1]) * 1024 for line in mem.splitlines()
                      if line.startswith("MemAvailable:")), 0)
    require(available >= 1024**3, "RESOURCE_MEMORY_LOW")
    return {"disk_available": row.f_bavail * row.f_frsize, "inodes_available": row.f_favail,
            "memory_available": available}


def require_v2_dropin_last(layout, attempt, units):
    """PRE: the reviewed baseline paths must sort before each V2 switch."""
    for role in (*ROLES, "monitor"):
        paths = units[role]["DropInPathsList"]
        require(isinstance(paths, list) and
                all(isinstance(path, str) and Path(path).is_absolute() for path in paths),
                "V2_DROPIN_PRECEDENCE")
        names = [Path(path).name for path in paths]
        switch = layout.switch(role, attempt).name
        require(len(names) == len(set(names)) and names == sorted(names) and
                all(name.endswith(".conf") and name < switch for name in names),
                "V2_DROPIN_PRECEDENCE")


def snapshot(runner, layout, source_root, attempt, *, check_unused=True, check_resources=True):
    require_host()
    if check_unused:
        require(not layout.state(attempt).exists() and not layout.release(attempt).exists() and
                not layout.observation(attempt).exists(), "ATTEMPT_ALREADY_USED")
    require(all(not layout.switch(role, attempt).exists() for role in (*ROLES, "monitor")),
            "V2_SWITCH_ALREADY_PRESENT")
    old = old_writer_check(runner)
    shared_writer_check(runner, layout)
    process_writer_check(layout.shared_root)
    mounts = v2_mount_check(runner, layout)
    network = network_check(runner, layout)
    policy_sha = policy_check(layout.shared_etc / "policy.json", source_root)
    units = {role: unit_fingerprint(runner, role) for role in (*ROLES, "monitor")}
    require_v2_dropin_last(layout, attempt, units)
    require(all(units[role]["Restart"] == "no" and units[role]["ActiveState"] != "active"
                for role in ROLES), "BASELINE_UNITS_NOT_STOPPED")
    return {"old_containers": old, "mounts": mounts, "network": network,
            "policy_sha256": policy_sha, "units": units,
            "timer": timer_state(runner),
            "resources": resources(layout) if check_resources else None}


def fresh_before_activation(runner, layout, source_root, attempt, staged):
    current = snapshot(runner, layout, source_root, attempt,
                       check_unused=False, check_resources=False)
    baseline = staged["baseline"]
    require(current["units"] == baseline["units"] and current["timer"] == baseline["timer"],
            "BASELINE_CHANGED")
    require(current["policy_sha256"] == baseline["policy_sha256"] and
            current["network"] == baseline["network"] and
            current["mounts"] == baseline["mounts"], "SHARED_CONFIG_CHANGED")
    return current
