#!/usr/bin/env python3
"""Read-only host preflight for FLOP Project DID Policy Signer.

This command performs no sudo, no writes, no service changes and never reads file
contents from Signer credential/state paths. It reports only host capability and
filesystem/principal metadata needed before a test-key Runtime rehearsal.
"""

from __future__ import annotations

import argparse
import importlib.util
import grp
import json
import os
import platform
import pwd
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

# Do not create bytecode files during read-only preflight or in reviewed source.
sys.dont_write_bytecode = True

# Load only the reviewed sibling, including under Python isolated mode (-I).
_gate_spec = importlib.util.spec_from_file_location(
    "signer_wsl_crash_gate", Path(__file__).resolve().with_name("wsl_crash_gate.py"))
crash_gate = importlib.util.module_from_spec(_gate_spec)
_gate_spec.loader.exec_module(crash_gate)

SCHEMA = 2
RUNTIME_NODE_PATH = "/usr/local/lib/flop-policy-signer/node"

BINARIES = (
    "node",
    "systemctl",
    "systemd-creds",
    "systemd-sysusers",
    "systemd-tmpfiles",
)

UNITS = (
    "flop-policy-signer.service",
    "flop-policy-signer.socket",
    "flop-policy-signer-reader.service",
    "flop-policy-signer-reader.socket",
    "flop-policy-signer-bootstrap.service",
)

PATHS = (
    RUNTIME_NODE_PATH,
    "/etc/flop-policy-signer",
    "/etc/flop-policy-signer/config.json",
    "/var/lib/flop-policy-signer",
    "/var/lib/flop-policy-signer/state.json",
    "/var/lib/flop-policy-signer/audit.jsonl",
    "/var/lib/flop-policy-signer-secret",
    "/var/lib/flop-policy-signer-secret/project-seed.cred",
    "/var/lib/flop-policy-signer-input",
    "/run/flop-policy-signer.sock",
    "/run/flop-policy-signer-reader.sock",
)


def _run(argv: list[str], timeout: int = 5) -> dict[str, Any]:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": stdout[:4096],
            "stderr": stderr[:1024],
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": type(exc).__name__,
        }


def _first_line(value: str) -> str | None:
    if not value:
        return None
    return value.splitlines()[0][:512]


def _binary_info(name: str, runner: Callable[[list[str], int], dict[str, Any]] = _run) -> dict[str, Any]:
    path = shutil.which(name)
    if path is None:
        return {"present": False, "path": None, "version": None}
    result = runner([path, "--version"], 5)
    return {
        "present": True,
        "path": path,
        "version": _first_line(result["stdout"] or result["stderr"]),
    }


def _user(name: str) -> dict[str, Any]:
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return {"exists": False}
    groups: list[str] = []
    for g in grp.getgrall():
        if name in g.gr_mem or g.gr_gid == entry.pw_gid:
            groups.append(g.gr_name)
    return {
        "exists": True,
        "uid": entry.pw_uid,
        "gid": entry.pw_gid,
        "home": entry.pw_dir,
        "shell": entry.pw_shell,
        "groups": sorted(set(groups)),
    }


def _group(name: str) -> dict[str, Any]:
    try:
        entry = grp.getgrnam(name)
    except KeyError:
        return {"exists": False}
    return {
        "exists": True,
        "gid": entry.gr_gid,
        "members": sorted(entry.gr_mem),
    }


def _path_metadata(path: str) -> dict[str, Any]:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"exists": False}
    except PermissionError:
        return {"exists": None, "error": "EACCES"}
    except OSError as exc:
        return {"exists": None, "error": exc.__class__.__name__}

    kind = (
        "symlink" if stat.S_ISLNK(st.st_mode)
        else "directory" if stat.S_ISDIR(st.st_mode)
        else "file" if stat.S_ISREG(st.st_mode)
        else "socket" if stat.S_ISSOCK(st.st_mode)
        else "other"
    )
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return {
        "exists": True,
        "kind": kind,
        "uid": st.st_uid,
        "gid": st.st_gid,
        "owner": owner,
        "group": group,
        "mode": f"{stat.S_IMODE(st.st_mode):04o}",
        "size": st.st_size if kind == "file" else None,
    }


def _unit_status(name: str, runner: Callable[[list[str], int], dict[str, Any]] = _run) -> dict[str, Any]:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return {"available": False}
    result = runner(
        [
            systemctl,
            "show",
            name,
            "--no-pager",
            "--property=LoadState,ActiveState,SubState,FragmentPath",
        ],
        5,
    )
    fields: dict[str, str] = {}
    for line in result["stdout"].splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    return {
        "available": True,
        "loadState": fields.get("LoadState"),
        "activeState": fields.get("ActiveState"),
        "subState": fields.get("SubState"),
        "fragmentPath": fields.get("FragmentPath") or None,
    }


def _repo_head(runner: Callable[[list[str], int], dict[str, Any]] = _run) -> str | None:
    root = Path(__file__).resolve().parents[3]
    git = shutil.which("git")
    if git is None:
        return None
    result = runner([git, "-C", str(root), "rev-parse", "HEAD"], 5)
    head = result["stdout"].strip()
    return head if result["ok"] and len(head) == 40 else None


def _node_major(info: dict[str, Any]) -> int | None:
    version = info.get("version")
    if not isinstance(version, str):
        return None
    token = version.strip().split()[0].lstrip("v")
    try:
        return int(token.split(".", 1)[0])
    except (ValueError, IndexError):
        return None


def _runtime_node_info(
    metadata: dict[str, Any],
    runner: Callable[[list[str], int], dict[str, Any]] = _run,
) -> dict[str, Any]:
    if metadata.get("exists") is not True:
        return {
            "present": metadata.get("exists") is True,
            "safeMetadata": False,
            "path": RUNTIME_NODE_PATH,
            "version": None,
        }
    safe = (
        metadata.get("kind") == "file"
        and metadata.get("uid") == 0
        and metadata.get("mode") == "0755"
    )
    version = None
    if safe:
        result = runner([RUNTIME_NODE_PATH, "--version"], 5)
        version = _first_line(result["stdout"] or result["stderr"])
    return {
        "present": True,
        "safeMetadata": safe,
        "path": RUNTIME_NODE_PATH,
        "version": version,
    }


def collect(agent_user: str | None = None) -> dict[str, Any]:
    binaries = {name: _binary_info(name) for name in BINARIES}
    node_major = _node_major(binaries["node"])

    system_state = _run([shutil.which("systemctl") or "systemctl", "is-system-running"], 5)
    systemd_state = _first_line(system_state["stdout"] or system_state["stderr"])

    principals = {
        "flop-signer": _user("flop-signer"),
        "flop-signer-reader": _user("flop-signer-reader"),
        "flop-agent": _group("flop-agent"),
        "flop-signer-acquisition": _group("flop-signer-acquisition"),
    }
    if agent_user is not None:
        principals["requestedAgentUser"] = _user(agent_user)

    paths = {path: _path_metadata(path) for path in PATHS}
    units = {unit: _unit_status(unit) for unit in UNITS}
    runtime_node = _runtime_node_info(paths[RUNTIME_NODE_PATH])

    blocking: list[str] = []
    preparation: list[str] = []
    if node_major != 22:
        blocking.append("NODE_MAJOR_NOT_22")
    if runtime_node["present"]:
        if not runtime_node["safeMetadata"]:
            blocking.append("RUNTIME_NODE_METADATA_UNSAFE")
        elif _node_major(runtime_node) != 22:
            blocking.append("RUNTIME_NODE_MAJOR_NOT_22")
    else:
        preparation.append("PROVISION_RUNTIME_NODE_FROM_VERIFIED_NODE22")
    for name in ("systemctl", "systemd-creds", "systemd-sysusers", "systemd-tmpfiles"):
        if not binaries[name]["present"]:
            blocking.append("MISSING_" + name.upper().replace("-", "_"))
    if systemd_state not in {"running", "degraded"}:
        blocking.append("SYSTEMD_NOT_RUNNING")
    credential = paths["/var/lib/flop-policy-signer-secret/project-seed.cred"]
    if credential.get("exists") is True:
        blocking.append("EXISTING_SIGNER_CREDENTIAL")
    elif credential.get("exists") is None:
        blocking.append("SIGNER_CREDENTIAL_EXISTENCE_UNKNOWN")
    if agent_user is not None:
        user = principals["requestedAgentUser"]
        if user.get("exists") is not True:
            blocking.append("AGENT_USER_MISSING")
        else:
            groups = set(user.get("groups", []))
            if "flop-signer-acquisition" in groups:
                blocking.append("AGENT_IN_SIGNER_ACQUISITION_GROUP")
            if user.get("uid") == 0:
                blocking.append("AGENT_IS_ROOT")

    return {
        "schema": SCHEMA,
        "mode": "READ_ONLY_TEST_KEY_PREFLIGHT",
        "writes": 0,
        "sudoUsed": False,
        "secretsRead": False,
        "repoHead": _repo_head(),
        "host": {
            "platform": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "pid1": _first_line(_run(["ps", "-p", "1", "-o", "comm="], 5)["stdout"]),
            "systemdState": systemd_state,
        },
        "currentUser": {
            "uid": os.getuid(),
            "gid": os.getgid(),
            "name": pwd.getpwuid(os.getuid()).pw_name,
        },
        "binaries": binaries,
        "runtimeNode": runtime_node,
        "crashCapture": crash_gate.status(),
        "preparationActions": preparation,
        "principals": principals,
        "paths": paths,
        "units": units,
        "blockingObservations": sorted(set(blocking)),
        "readyForTestKeyRehearsalPreparation": len(blocking) == 0,
        "humanChecksStillRequired": [
            "sudo -l -U <agent-user>",
            "polkit/systemd mutation authority for <agent-user>",
            "actual Agent socket access and Reader/Secret denial after deployment",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-user", default=None)
    args = parser.parse_args(argv)
    report = collect(args.agent_user)
    print(json.dumps(report, sort_keys=True, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
