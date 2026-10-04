"""Safe configuration evidence and a fixed-target, read-only Docker inspector."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = "20260906-223132-4d8372a3"
TARGET = "observer-durability-" + IDENTITY + "-1"
VOLUME = "observer-durability-" + IDENTITY
IMAGE = "sha256:492379ea714f1ad9d279ee7fc507f09a3b72c9c4f3e8d1f6cc730f03b51bfe07"
PURPOSE = "technocore-observer.durability"
# Explicitly reviewed/authorized by the user; no namespace/prefix wildcard.
OPTIONAL_CONTAINER_LABEL_KEYS = frozenset({"desktop.docker.io/wsl-distro"})
MAX_EXTRA_LABEL_KEYS = 64
MAX_LABEL_KEY_LENGTH = 256

CAPABILITIES = set("ALL CHOWN DAC_OVERRIDE DAC_READ_SEARCH FOWNER FSETID KILL SETGID SETUID SETPCAP LINUX_IMMUTABLE NET_BIND_SERVICE NET_BROADCAST NET_ADMIN NET_RAW IPC_LOCK IPC_OWNER SYS_MODULE SYS_RAWIO SYS_CHROOT SYS_PTRACE SYS_PACCT SYS_ADMIN SYS_BOOT SYS_NICE SYS_RESOURCE SYS_TIME SYS_TTY_CONFIG MKNOD LEASE AUDIT_WRITE AUDIT_CONTROL SETFCAP MAC_OVERRIDE MAC_ADMIN SYSLOG WAKE_ALARM BLOCK_SUSPEND AUDIT_READ PERFMON BPF CHECKPOINT_RESTORE".split())
SAFE_ENUMS = {"", "none", "bridge", "default", "host", "private", "shareable", "local", "volume", "bind", "tmpfs",
              "no", "always", "unless-stopped", "on-failure", "65532:65532", "0", "0:0", "root",
              "no-new-privileges", "no-new-privileges:true", "seccomp=unconfined", "apparmor=unconfined", "docker-default"}
SAFE_ENUMS |= CAPABILITIES | {"CAP_" + name for name in CAPABILITIES}


def safe(value, allowed=()):
    """Only known enum strings and caller-owned expected strings may survive."""
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, str):
        return value if value in SAFE_ENUMS or value in allowed else "<redacted>"
    if isinstance(value, list):
        return [safe(item, allowed) for item in value[:64]]
    return {"type": type(value).__name__, "empty": not bool(value)}


def safe_id(value):
    return value if isinstance(value, str) and re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", value) else "<invalid-id>"


def shape(value):
    return {"present": bool(value), "type": type(value).__name__,
            "count": len(value) if isinstance(value, (dict, list)) else None}


def volume_options(value):
    if not isinstance(value, dict):
        return safe(value)
    # Preserve harmless Docker defaults, but never arbitrary label/driver values.
    result = {key: safe(value[key]) for key in ("NoCopy", "Subpath") if key in value}
    for key in ("Labels", "DriverConfig"):
        if key in value:
            result[key] = shape(value[key])
    result["unknown_field_count"] = len(set(value) - {"NoCopy", "Subpath", "Labels", "DriverConfig"})
    return result


def label_key_details(labels):
    """Record bounded key names only; never read or serialize extra values."""
    flags = set()
    keys = []
    if labels is None:
        labels = {}
    if not isinstance(labels, dict):
        flags.add("INVALID_LABEL_MAP_TYPE")
    else:
        extra = [key for key in labels if key != PURPOSE]
        if len(extra) > MAX_EXTRA_LABEL_KEYS:
            flags.add("EXTRA_LABEL_KEYS_LIMIT")
        for key in extra[:MAX_EXTRA_LABEL_KEYS]:
            if not isinstance(key, str):
                flags.add("NON_STRING_LABEL_KEY")
            elif not 1 <= len(key) <= MAX_LABEL_KEY_LENGTH:
                flags.add("INVALID_LABEL_KEY_LENGTH")
            elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", key):
                # Omit control characters, BiDi and unusual syntax, without
                # printing the rejected key or silently changing its spelling.
                flags.add("UNSAFE_LABEL_KEY_CHARACTERS")
            else:
                keys.append(key)
    return {"extra_label_keys": sorted(keys), "extra_label_keys_complete": not flags,
            "label_key_diagnostic_flags": sorted(flags)}


def diagnostic(config, image, volume, identity, checks, stage, name, entrypoint, allowed_env):
    host, process = config["HostConfig"], config["Config"]
    mounts, declared = config.get("Mounts") or [], host.get("Mounts") or []
    env = process.get("Env") or []
    env_names = {item.split("=", 1)[0] for item in env}
    labels = process.get("Labels") or {}
    observed = {
        "local_image_id": safe_id(config.get("Image")),
        "network_none": safe(host.get("NetworkMode")),
        "nonroot_user": safe(process.get("User")),
        "root_readonly": safe(host.get("ReadonlyRootfs")),
        "not_privileged": safe(host.get("Privileged")),
        "no_host_namespaces": {key: safe(host.get(key)) for key in ("PidMode", "IpcMode", "UTSMode", "UsernsMode")},
        "capabilities_dropped": {key: safe(host.get(key)) for key in ("CapDrop", "CapAdd")},
        "no_new_privileges": safe(host.get("SecurityOpt")),
        "no_devices": {key: shape(host.get(key)) for key in ("Devices", "DeviceRequests", "DeviceCgroupRules")},
        "no_published_ports": {"PortBindings": shape(host.get("PortBindings")), "PublishAllPorts": safe(host.get("PublishAllPorts"))},
        "bounded_resources": {key: safe(host.get(key)) for key in ("PidsLimit", "Memory")},
        "no_restart": safe(host.get("RestartPolicy", {}).get("Name")),
        "environment_names_allowlisted": {"known_names": sorted(env_names & allowed_env), "unknown_name_count": len(env_names - allowed_env)},
        "home_is_nonexistent": {"exact_expected_entry_present": "HOME=/nonexistent" in env},
        "exact_state_volume": [{key: safe(item.get(key), (volume, "/state"))
                                for key in ("Type", "Name", "Destination", "Driver", "RW")} for item in mounts],
        "exact_mount_declaration": [{**{key: safe(item.get(key), (volume, "/state"))
                                       for key in ("Type", "Source", "Target")},
                                      "VolumeOptions": volume_options(item.get("VolumeOptions"))} for item in declared],
        "no_additional_mounts": {key: shape(host.get(key)) for key in ("Binds", "VolumesFrom", "Tmpfs")},
        "expected_entrypoint": {"Entrypoint": safe(process.get("Entrypoint"), entrypoint), "Cmd": safe(process.get("Cmd"), entrypoint)},
        "exact_labels": {"purpose_present": PURPOSE in labels,
                         "purpose_value_matches": labels.get(PURPOSE) == identity,
                         "total_count": len(labels),
                         "allowed_metadata_keys_present": sorted(set(labels) & OPTIONAL_CONTAINER_LABEL_KEYS),
                         "unknown_name_count": len(set(labels) - {PURPOSE} - OPTIONAL_CONTAINER_LABEL_KEYS)},
        "no_container_logs": safe(host.get("LogConfig", {}).get("Type")),
        "no_links": {key: shape(host.get(key)) for key in ("Links", "ExtraHosts")},
    }
    expected = {
        "local_image_id": image, "network_none": "none", "nonroot_user": "65532:65532",
        "root_readonly": True, "not_privileged": False, "no_host_namespaces": "each mode != host",
        "capabilities_dropped": {"CapDrop": ["ALL"], "CapAdd": "absent/empty"},
        "no_new_privileges": "contains no-new-privileges or no-new-privileges:true",
        "no_devices": "all absent/empty", "no_published_ports": "no bindings; PublishAllPorts false/absent",
        "bounded_resources": {"PidsLimit": 32, "Memory": 268435456}, "no_restart": "no",
        "environment_names_allowlisted": sorted(allowed_env), "home_is_nonexistent": "HOME=/nonexistent present",
        "exact_state_volume": [{"Type": "volume", "Name": volume, "Destination": "/state", "Driver": "local", "RW": True}],
        "exact_mount_declaration": "exactly one volume, configured Source, Target=/state, VolumeOptions absent/null/empty",
        "no_additional_mounts": "Binds/VolumesFrom/Tmpfs absent/empty",
        "expected_entrypoint": {"Entrypoint": entrypoint, "Cmd": "absent/empty"},
        "exact_labels": {"required_keys": [PURPOSE], "required_value": "configured identity; equality checked without saving value",
                         "optional_metadata_keys": sorted(OPTIONAL_CONTAINER_LABEL_KEYS),
                         "optional_values_used_for_security": False, "unknown_extra_keys_allowed": False},
        "no_container_logs": "none", "no_links": "Links/ExtraHosts absent/empty",
    }
    if set(observed) != set(checks) or set(expected) != set(checks):
        raise RuntimeError("DIAGNOSTIC_CHECK_COVERAGE_MISMATCH")
    return {"stage": stage, "container": name, "container_id": safe_id(config.get("Id")),
            "checks": checks, "failed_checks": sorted(key for key, passed in checks.items() if not passed),
            "observed": observed, "expected": expected,
            **label_key_details(process.get("Labels")),
            "inspect_values_redacted": True}


def main():
    import run_durability as h
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"target": TARGET, "operation": "container inspect only", "external_requests": 0}))
        return 0
    os.umask(0o077)
    directory = ROOT / "docs" / ("durability-inspect-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    print(json.dumps({"create_path": str(directory)}), flush=True)
    directory.mkdir(mode=0o700)
    client = directory / "docker-client"
    client.mkdir(mode=0o700)
    report = {"container": TARGET, "status": "INCOMPLETE", "operation": "read-only inspect",
              "external_requests": 0, "observer_started": False}
    try:
        # Capture in memory only. Never log/persist raw inspect or subprocess errors.
        result = subprocess.run(["docker", "--config=" + str(client), "--host=unix:///var/run/docker.sock",
                                 "container", "inspect", TARGET], capture_output=True, timeout=30, check=False,
                                env={"PATH": os.environ.get("PATH", os.defpath), "HOME": str(client)})
        if result.returncode:
            report.update({"status": "INSPECT_UNAVAILABLE", "exit_code": result.returncode,
                           "error_code": "DOCKER_PERMISSION_DENIED" if b"permission denied" in result.stderr.lower() else "DOCKER_INSPECT_FAILED"})
        else:
            if len(result.stdout) > 4 * 1024 * 1024:
                raise RuntimeError("INSPECT_SIZE_LIMIT")
            values = json.loads(result.stdout)
            if len(values) != 1:
                raise RuntimeError("INSPECT_TARGET_COUNT")
            config = values[0]
            if config.get("Name") != "/" + TARGET:
                raise RuntimeError("INSPECT_TARGET_MISMATCH")
            checks = h.configuration_checks(config, IMAGE, VOLUME, IDENTITY)
            report.update(diagnostic(config, IMAGE, VOLUME, IDENTITY, checks, "read_only_inspect", TARGET,
                                     h.ENTRYPOINT, h.isolation.ALLOWED_ENV))
            report["status"] = "CONFIGURATION_ACCEPTED" if all(checks.values()) else "CONFIGURATION_REJECTED"
    except Exception as exc:
        report.update({"status": "INSPECT_ERROR", "error_class": type(exc).__name__})
    h.private_write(directory / "report.json", json.dumps(report, ensure_ascii=True, indent=2).encode())
    sources = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in (Path(__file__), ROOT / "tests" / "run_durability.py", ROOT / "tests" / "container_isolation.py")}
    h.private_write(directory / "source-hashes.json", json.dumps(sources, indent=2).encode())
    print(json.dumps({"status": report["status"], "failed_checks": report.get("failed_checks"),
                      "extra_label_keys": report.get("extra_label_keys"),
                      "extra_label_keys_complete": report.get("extra_label_keys_complete"),
                      "error_code": report.get("error_code"), "report": str(directory / "report.json")}), flush=True)
    return 0 if report["status"] in ("CONFIGURATION_ACCEPTED", "CONFIGURATION_REJECTED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
