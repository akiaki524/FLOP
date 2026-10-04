#!/usr/bin/env python3
"""Test-key-only Host rehearsal for the Project DID Policy Signer.

Human Gate:
  sudo python3 -I deploy/test_key_rehearsal.py prepare --node <verified-node22>
  sudo python3 -I deploy/test_key_rehearsal.py prepare-existing
  sudo python3 -I deploy/test_key_rehearsal.py observe
  sudo python3 -I deploy/test_key_rehearsal.py cleanup-test-key

This tool never accepts a Real seed. It generates an ephemeral test seed in
memory, verifies its DID through the installed Signer code, encrypts it through
credential_handoff.mjs, and never writes the plaintext seed to disk.

prepare installs the intended Signer infrastructure but does not enable units.
cleanup-test-key removes only test-key config/state/credential/evidence and
stops the units; the reusable root-owned runtime/users/unit templates remain.
"""

from __future__ import annotations

import argparse
import importlib.util
import grp
import hashlib
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Do not create bytecode files during read-only preflight or in reviewed source.
sys.dont_write_bytecode = True

# Load only the reviewed sibling, including under Python isolated mode (-I).
_gate_spec = importlib.util.spec_from_file_location(
    "signer_wsl_crash_gate", Path(__file__).resolve().with_name("wsl_crash_gate.py"))
crash_gate = importlib.util.module_from_spec(_gate_spec)
_gate_spec.loader.exec_module(crash_gate)

COMPONENT = Path(__file__).resolve().parents[1]
DEPLOY = COMPONENT / "deploy"
SRC = COMPONENT / "src"

INSTALL = Path("/usr/local/lib/flop-policy-signer")
INSTALL_SRC = INSTALL / "src"
INSTALL_DEPLOY = INSTALL / "deploy"
RUNTIME_NODE = INSTALL / "node"
CONFIG_DIR = Path("/etc/flop-policy-signer")
CONFIG = CONFIG_DIR / "config.json"
SECRET_DIR = Path("/var/lib/flop-policy-signer-secret")
CREDENTIAL = SECRET_DIR / "project-seed.cred"
STATE_DIR = Path("/var/lib/flop-policy-signer")
STATE = STATE_DIR / "state.json"
AUDIT = STATE_DIR / "audit.jsonl"
INPUT_DIR = Path("/var/lib/flop-policy-signer-input")
PROVENANCE = Path("/var/lib/flop-policy-signer-rehearsal.json")

SYSUSERS = Path("/etc/sysusers.d/flop-policy-signer.conf")
TMPFILES = Path("/etc/tmpfiles.d/flop-policy-signer.conf")
UNIT_DIR = Path("/etc/systemd/system")
UNITS = (
    "flop-policy-signer.service",
    "flop-policy-signer.socket",
    "flop-policy-signer-reader.service",
    "flop-policy-signer-reader.socket",
    "flop-policy-signer-bootstrap.service",
)
SOCKETS = (
    "flop-policy-signer-reader.socket",
    "flop-policy-signer.socket",
)
SERVICES = (
    "flop-policy-signer.service",
    "flop-policy-signer-reader.service",
)
TRANSIENT_PROBE = "flop-policy-signer-node-probe.service"
TRANSIENT_PROBE_STATE = Path("/var/lib/flop-policy-signer-probe")

PHASE_ORDER = {
    "STARTING": 0,
    "RUNTIME_INSTALLED": 1,
    "PRINCIPALS_READY": 2,
    "UNITS_VERIFIED": 3,
    "SANDBOX_PROBED": 4,
    "IDENTITY_READY": 5,
    "CONFIG_WRITTEN": 6,
    "CREDENTIAL_HANDOFF_STARTING": 7,
    "CREDENTIAL_WRITTEN": 8,
    "BOOTSTRAP_STARTING": 9,
    "BOOTSTRAPPED": 10,
    "SOCKETS_STARTING": 11,
    "SOCKETS_STARTED": 12,
    "RUNTIME_UPDATE_STARTING": 13,
    "RUNTIME_UPDATED": 14,
    "RESUME_SOCKETS_STARTED": 15,
    "OBSERVED": 16,
    "CLEANUP_STARTED": 17,
    "CLEANUP_ARTIFACTS_REMOVED": 18,
}

EFFECTIVE_UNIT_EXPECTED = {
    "flop-policy-signer.service": {
        "User": "flop-signer",
        "Group": "flop-signer",
        "PrivateNetwork": "yes",
        "MemoryDenyWriteExecute": "yes",
        "LimitCORE": "0",
        "LimitCORESoft": "0",
        "ProtectSystem": "strict",
        "RestrictAddressFamilies": {"AF_UNIX"},
        "InaccessiblePaths": {"/var/lib/flop-policy-signer-secret"},
    },
    "flop-policy-signer-reader.service": {
        "User": "flop-signer-reader",
        "Group": "flop-signer-reader",
        "PrivateNetwork": "no",
        "MemoryDenyWriteExecute": "no",
        "ProtectSystem": "strict",
        "RestrictAddressFamilies": {"AF_UNIX", "AF_INET", "AF_INET6"},
        "InaccessiblePaths": {
            "/var/lib/flop-policy-signer-secret",
            "/etc/flop-policy-signer",
            "/var/lib/flop-policy-signer",
        },
    },
    "flop-policy-signer-bootstrap.service": {
        "User": "flop-signer",
        "Group": "flop-signer",
        "PrivateNetwork": "yes",
        "MemoryDenyWriteExecute": "yes",
        "LimitCORE": "0",
        "LimitCORESoft": "0",
        "ProtectSystem": "strict",
        "RestrictAddressFamilies": {"AF_UNIX"},
        "InaccessiblePaths": {"/var/lib/flop-policy-signer-secret"},
    },
}


class RehearsalError(RuntimeError):
    pass


def fail(code: str) -> None:
    raise RehearsalError(code)


def run(
    argv: list[str],
    *,
    input_bytes: bytes | bytearray | None = None,
    timeout: int = 30,
    ok: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    try:
        kwargs: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "timeout": timeout,
            "check": False,
            "env": {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        }
        if input_bytes is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = input_bytes
        result = subprocess.run(
            argv,
            **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired):
        fail("HOST_PROCESS_FAILED")
    if result.returncode not in ok:
        fail("HOST_PROCESS_FAILED")
    return result


def require_root() -> None:
    if os.geteuid() != 0:
        fail("ROOT_REQUIRED")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path: Path, value: dict[str, Any], mode: int, uid: int = 0, gid: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, gid)
            stream.write((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_provenance(value: dict[str, Any], phase: str, **updates: Any) -> dict[str, Any]:
    next_value = dict(value)
    next_value.update(updates)
    next_value["phase"] = phase
    next_value["updatedAtMs"] = int(time.time() * 1000)
    atomic_json(PROVENANCE, next_value, 0o600)
    return next_value


def load_provenance() -> dict[str, Any]:
    try:
        value = json.loads(PROVENANCE.read_text())
    except (OSError, json.JSONDecodeError):
        fail("REHEARSAL_PROVENANCE_INVALID")
    if value.get("schema") != 2 or value.get("mode") != "TEST_KEY_ONLY":
        fail("REHEARSAL_PROVENANCE_INVALID")
    return value


def phase_at_least(provenance: dict[str, Any], phase: str) -> bool:
    current = provenance.get("phase")
    if current not in PHASE_ORDER or phase not in PHASE_ORDER:
        fail("REHEARSAL_PHASE_INVALID")
    return PHASE_ORDER[current] >= PHASE_ORDER[phase]


def copy_root_file(source: Path, target: Path, mode: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="." + target.name + "-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
            os.fchmod(out.fileno(), mode)
            os.fchown(out.fileno(), 0, 0)
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def repo_identity() -> dict[str, Any]:
    root = COMPONENT.parents[1]
    safe = f"safe.directory={root}"
    git_base = [
        "git",
        "--no-optional-locks",
        "-c", safe,
        "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=/dev/null",
        "-C", str(root),
    ]
    head = run(
        [*git_base, "rev-parse", "HEAD"],
        timeout=5,
    ).stdout.decode().strip()
    if len(head) != 40:
        fail("REPO_HEAD_INVALID")
    status = run(
        [*git_base, "status", "--porcelain", "--untracked-files=normal"],
        timeout=5,
    ).stdout.decode()
    if status != "":
        fail("DIRTY_REVIEWED_TREE")
    return {"head": head, "clean": True, "root": str(root)}


def source_node_metadata(node: Path) -> dict[str, str]:
    resolved = node.resolve(strict=True)
    st = resolved.stat()
    if not stat.S_ISREG(st.st_mode) or not os.access(resolved, os.X_OK):
        fail("NODE_SOURCE_UNSAFE")
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
    }


def node_version(node: Path, expected_uid: int = 0, expected_gid: int = 0) -> str:
    st = node.lstat()
    if (
        not stat.S_ISREG(st.st_mode)
        or stat.S_ISLNK(st.st_mode)
        or st.st_uid != expected_uid
        or st.st_gid != expected_gid
        or stat.S_IMODE(st.st_mode) != 0o755
    ):
        fail("RUNTIME_NODE_UNSAFE")
    result = run([str(node), "--version"], timeout=5)
    version = result.stdout.decode().strip()
    if not version.startswith("v22."):
        fail("NODE22_REQUIRED")
    return version


def empty_setup_required() -> None:
    paths = [
        INSTALL,
        CONFIG_DIR,
        SECRET_DIR,
        INPUT_DIR,
        STATE_DIR,
        CONFIG,
        CREDENTIAL,
        STATE,
        PROVENANCE,
        SYSUSERS,
        TMPFILES,
    ]
    for name in UNITS:
        paths.extend([
            Path("/etc/systemd/system") / name,
            Path("/etc/systemd/system") / (name + ".d"),
            Path("/run/systemd/system") / name,
            Path("/run/systemd/system") / (name + ".d"),
            Path("/usr/local/lib/systemd/system") / name,
            Path("/usr/local/lib/systemd/system") / (name + ".d"),
            Path("/usr/lib/systemd/system") / name,
            Path("/usr/lib/systemd/system") / (name + ".d"),
        ])
    if any(os.path.lexists(path) for path in paths):
        fail("EXISTING_SETUP_REFUSED")
    for name in ("flop-signer", "flop-signer-reader"):
        try:
            pwd.getpwnam(name)
        except KeyError:
            pass
        else:
            fail("EXISTING_ACCOUNT_REFUSED")
    for name in ("flop-agent", "flop-signer-acquisition"):
        try:
            grp.getgrnam(name)
        except KeyError:
            pass
        else:
            fail("EXISTING_GROUP_REFUSED")


def runtime_artifacts() -> list[Path]:
    return [
        RUNTIME_NODE,
        *sorted(INSTALL_SRC.glob("*.mjs")),
        *(INSTALL_DEPLOY / name for name in (
            "runtime_probe.mjs",
            "test_identity.mjs",
            "offline_state_status.mjs",
            "durability_probe.mjs",
        )),
        SYSUSERS,
        TMPFILES,
        *(UNIT_DIR / name for name in UNITS),
    ]


def reviewed_target_manifest() -> dict[str, str]:
    if not RUNTIME_NODE.is_file() or RUNTIME_NODE.is_symlink():
        fail("RUNTIME_NODE_UNAVAILABLE")
    manifest: dict[str, str] = {
        str(RUNTIME_NODE): sha256_file(RUNTIME_NODE),
    }
    for source in sorted(SRC.glob("*.mjs")):
        manifest[str(INSTALL_SRC / source.name)] = sha256_file(source)
    for name in (
        "runtime_probe.mjs",
        "test_identity.mjs",
        "offline_state_status.mjs",
        "durability_probe.mjs",
    ):
        manifest[str(INSTALL_DEPLOY / name)] = sha256_file(DEPLOY / name)
    manifest[str(SYSUSERS)] = sha256_file(DEPLOY / "flop-policy-signer.sysusers.conf")
    manifest[str(TMPFILES)] = sha256_file(DEPLOY / "flop-policy-signer.tmpfiles.conf")
    for name in UNITS:
        manifest[str(UNIT_DIR / name)] = sha256_file(DEPLOY / name)
    return manifest


def update_reviewed_runtime(target_manifest: dict[str, str]) -> dict[str, str]:
    for source in sorted(SRC.glob("*.mjs")):
        copy_root_file(source, INSTALL_SRC / source.name, 0o644)
    for name in (
        "runtime_probe.mjs",
        "test_identity.mjs",
        "offline_state_status.mjs",
        "durability_probe.mjs",
    ):
        copy_root_file(DEPLOY / name, INSTALL_DEPLOY / name, 0o644)
    copy_root_file(DEPLOY / "flop-policy-signer.sysusers.conf", SYSUSERS, 0o644)
    copy_root_file(DEPLOY / "flop-policy-signer.tmpfiles.conf", TMPFILES, 0o644)
    for name in UNITS:
        copy_root_file(DEPLOY / name, UNIT_DIR / name, 0o644)

    actual = installed_manifest()
    if actual != target_manifest:
        fail("RUNTIME_UPDATE_MANIFEST_MISMATCH")
    return actual


def installed_manifest() -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in runtime_artifacts():
        if not path.is_file() or path.is_symlink():
            fail("INSTALLED_ARTIFACT_UNSAFE")
        manifest[str(path)] = sha256_file(path)
    return manifest


def exact_metadata(path: Path, kind: str, uid: int, gid: int, mode: int) -> None:
    try:
        st = path.lstat()
    except OSError:
        fail("RETAINED_ARTIFACT_MISSING")
    if (kind == "dir" and not stat.S_ISDIR(st.st_mode)) or (
        kind == "file" and not stat.S_ISREG(st.st_mode)
    ) or (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) != (uid, gid, mode):
        fail("RETAINED_ARTIFACT_UNSAFE")


def exact_children(directory: Path, names: set[str]) -> None:
    try:
        actual = {entry.name for entry in directory.iterdir()}
    except OSError:
        fail("RETAINED_DIRECTORY_UNREADABLE")
    if actual != names:
        fail("RETAINED_DIRECTORY_CONTENTS_UNEXPECTED")


def absent(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        fail("RETAINED_PATH_UNREADABLE")
    fail("RETAINED_PROTECTED_ARTIFACT_PRESENT")


def retained_principals() -> tuple[Any, Any, Any]:
    try:
        signer = pwd.getpwnam("flop-signer")
        reader = pwd.getpwnam("flop-signer-reader")
        signer_group = grp.getgrnam("flop-signer")
        reader_group = grp.getgrnam("flop-signer-reader")
        acquisition = grp.getgrnam("flop-signer-acquisition")
        agent = grp.getgrnam("flop-agent")
    except KeyError:
        fail("RETAINED_PRINCIPAL_MISSING")
    if (signer.pw_uid == 0 or reader.pw_uid == 0 or signer.pw_uid == reader.pw_uid
        or len({signer_group.gr_gid, reader_group.gr_gid, acquisition.gr_gid, agent.gr_gid}) != 4
        or signer.pw_gid != signer_group.gr_gid or reader.pw_gid != reader_group.gr_gid
        or signer.pw_dir != str(STATE_DIR) or reader.pw_dir != str(INPUT_DIR)
        or signer.pw_shell != "/usr/sbin/nologin" or reader.pw_shell != "/usr/sbin/nologin"
        or set(acquisition.gr_mem) != {signer.pw_name, reader.pw_name}
        or signer_group.gr_mem or reader_group.gr_mem
        or any(name in group.gr_mem for group in grp.getgrall()
               for name in (signer.pw_name, reader.pw_name)
               if group.gr_name != "flop-signer-acquisition")):
        fail("RETAINED_PRINCIPAL_MISMATCH")
    return signer, reader, acquisition


def stopped_entry_points() -> None:
    for unit in (*SERVICES, *SOCKETS, "flop-policy-signer-bootstrap.service"):
        values = systemctl_properties(unit, [
            "LoadState", "ActiveState", "Job", "MainPID", "ControlPID",
            "UnitFileState", "FragmentPath", "DropInPaths"])
        expected_state = "static" if unit.endswith("bootstrap.service") else "disabled"
        if (values.get("LoadState") != "loaded" or values.get("ActiveState") != "inactive"
            or values.get("Job") not in {"", "0"}
            or (unit.endswith(".service") and
                (values.get("MainPID") != "0" or values.get("ControlPID") != "0"))
            or values.get("UnitFileState") != expected_state
            or values.get("FragmentPath") != str(UNIT_DIR / unit)
            or values.get("DropInPaths") != ""):
            fail("RETAINED_RUNTIME_NOT_STOPPED")


def historical_install_head(manifest: dict[str, str]) -> str:
    """Require all installed files to equal one checkout that is an ancestor of HEAD.

    Merged PR branches are included: earlier Host installs may come from a
    non-first-parent commit. Old files are only compared, then replaced.
    """
    root = COMPONENT.parents[1]
    git = ["git", "--no-optional-locks", "-c", f"safe.directory={root}",
           "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(root)]
    relative = {str(path.relative_to(INSTALL_SRC)): f"components/signer/src/{path.name}"
                for path in INSTALL_SRC.glob("*.mjs")}
    paths = {
        **{str(INSTALL_SRC / name): source for name, source in relative.items()},
        **{str(INSTALL_DEPLOY / name): f"components/signer/deploy/{name}"
           for name in ("runtime_probe.mjs", "test_identity.mjs", "offline_state_status.mjs", "durability_probe.mjs")},
        str(SYSUSERS): "components/signer/deploy/flop-policy-signer.sysusers.conf",
        str(TMPFILES): "components/signer/deploy/flop-policy-signer.tmpfiles.conf",
        **{str(UNIT_DIR / name): f"components/signer/deploy/{name}" for name in UNITS},
    }
    expected_paths = set(paths.values())
    if set(manifest) != set(paths) | {str(RUNTIME_NODE)}:
        fail("RETAINED_MANIFEST_SHAPE_MISMATCH")
    blob_ids = {}
    for installed, source in paths.items():
        blob_ids[source] = run([*git, "hash-object", "--no-filters", installed], timeout=5).stdout.decode().strip()
    commits = run([*git, "rev-list", "--full-history", "HEAD", "--", "components/signer/src", "components/signer/deploy"], timeout=30).stdout.decode().splitlines()
    for commit in commits:
        tree = run([*git, "ls-tree", "-r", commit, "--", "components/signer/src", "components/signer/deploy"], timeout=10).stdout.decode()
        found = {}
        for line in tree.splitlines():
            try:
                meta, name = line.split("\t", 1)
                if name in expected_paths or (name.startswith("components/signer/src/") and name.endswith(".mjs")):
                    mode, kind, blob = meta.split()
                    found[name] = blob if mode == "100644" and kind == "blob" else None
            except ValueError:
                fail("RETAINED_HISTORY_INVALID")
        if found == blob_ids:
            return commit
    fail("RETAINED_MANIFEST_UNREVIEWED")


def validate_retained_infrastructure() -> dict[str, Any]:
    for path in (CREDENTIAL, CONFIG, STATE, AUDIT, PROVENANCE, TRANSIENT_PROBE_STATE):
        absent(path)
    signer, reader, acquisition = retained_principals()
    exact_metadata(INSTALL, "dir", 0, 0, 0o755)
    exact_metadata(INSTALL_SRC, "dir", 0, 0, 0o755)
    exact_metadata(INSTALL_DEPLOY, "dir", 0, 0, 0o755)
    exact_children(INSTALL, {"node", "src", "deploy"})
    source_names = {p.name for p in INSTALL_SRC.iterdir()}
    if not source_names or any(not name.endswith(".mjs") for name in source_names):
        fail("RETAINED_DIRECTORY_CONTENTS_UNEXPECTED")
    exact_children(INSTALL_DEPLOY, {"runtime_probe.mjs", "test_identity.mjs", "offline_state_status.mjs", "durability_probe.mjs"})
    for path in runtime_artifacts():
        exact_metadata(path, "file", 0, 0, 0o755 if path == RUNTIME_NODE else 0o644)
    exact_metadata(CONFIG_DIR, "dir", 0, signer.pw_gid, 0o750)
    exact_metadata(SECRET_DIR, "dir", 0, 0, 0o700)
    exact_metadata(INPUT_DIR, "dir", reader.pw_uid, acquisition.gr_gid, 0o2750)
    for directory in (CONFIG_DIR, SECRET_DIR, INPUT_DIR):
        exact_children(directory, set())
    if os.path.lexists(STATE_DIR):
        exact_metadata(STATE_DIR, "dir", signer.pw_uid, signer.pw_gid, 0o700)
        exact_children(STATE_DIR, set())
    for name in UNITS:
        for base in (UNIT_DIR, Path("/run/systemd/system"), Path("/usr/local/lib/systemd/system"), Path("/usr/lib/systemd/system")):
            absent(base / (name + ".d"))
            if base != UNIT_DIR:
                absent(base / name)
    stopped_entry_points()
    version = node_version(RUNTIME_NODE)
    manifest = installed_manifest()
    return {"manifest": manifest, "installedHead": historical_install_head(manifest), "nodeVersion": version}


def verify_manifest(expected: dict[str, str]) -> None:
    if not isinstance(expected, dict) or not expected:
        fail("INSTALL_MANIFEST_MISSING")
    actual = installed_manifest()
    if actual != expected:
        fail("INSTALL_MANIFEST_MISMATCH")


def install_runtime(node: Path, source_node: dict[str, str]) -> dict[str, Any]:
    INSTALL.mkdir(parents=True, mode=0o755)
    os.chown(INSTALL, 0, 0)
    os.chmod(INSTALL, 0o755)
    INSTALL_SRC.mkdir(mode=0o755)
    INSTALL_DEPLOY.mkdir(mode=0o755)

    copy_root_file(node.resolve(strict=True), RUNTIME_NODE, 0o755)
    runtime_sha = sha256_file(RUNTIME_NODE)
    if runtime_sha != source_node["sha256"]:
        fail("NODE_COPY_MISMATCH")
    version = node_version(RUNTIME_NODE)

    for source in sorted(SRC.glob("*.mjs")):
        copy_root_file(source, INSTALL_SRC / source.name, 0o644)
    for name in ("runtime_probe.mjs", "test_identity.mjs", "offline_state_status.mjs", "durability_probe.mjs"):
        copy_root_file(DEPLOY / name, INSTALL_DEPLOY / name, 0o644)

    copy_root_file(DEPLOY / "flop-policy-signer.sysusers.conf", SYSUSERS, 0o644)
    copy_root_file(DEPLOY / "flop-policy-signer.tmpfiles.conf", TMPFILES, 0o644)
    for name in UNITS:
        copy_root_file(DEPLOY / name, UNIT_DIR / name, 0o644)

    manifest = installed_manifest()
    return {
        "nodeSha256": runtime_sha,
        "nodeVersion": version,
        "manifest": manifest,
    }


def provision_principals_and_dirs() -> None:
    run(["systemd-sysusers", str(SYSUSERS)])
    run(["systemd-tmpfiles", "--create", str(TMPFILES)])

    signer = pwd.getpwnam("flop-signer")
    reader = pwd.getpwnam("flop-signer-reader")
    acquisition = grp.getgrnam("flop-signer-acquisition")
    agent = grp.getgrnam("flop-agent")
    if signer.pw_uid == 0 or reader.pw_uid == 0 or signer.pw_uid == reader.pw_uid:
        fail("DEDICATED_UID_FAILED")
    if signer.pw_gid == acquisition.gr_gid or reader.pw_gid == acquisition.gr_gid:
        fail("PRIMARY_GROUP_UNEXPECTED")
    if signer.pw_name not in acquisition.gr_mem or reader.pw_name not in acquisition.gr_mem:
        fail("ACQUISITION_MEMBERSHIP_MISSING")
    if agent.gr_gid in (signer.pw_gid, reader.pw_gid, acquisition.gr_gid):
        fail("GROUP_COLLISION")

    CONFIG_DIR.mkdir(mode=0o750)
    os.chown(CONFIG_DIR, 0, signer.pw_gid)
    os.chmod(CONFIG_DIR, 0o750)

    secret = SECRET_DIR.lstat()
    if (
        not stat.S_ISDIR(secret.st_mode)
        or secret.st_uid != 0
        or secret.st_gid != 0
        or stat.S_IMODE(secret.st_mode) != 0o700
    ):
        fail("SECRET_DIRECTORY_UNSAFE")
    inp = INPUT_DIR.lstat()
    if (
        not stat.S_ISDIR(inp.st_mode)
        or inp.st_uid != reader.pw_uid
        or inp.st_gid != acquisition.gr_gid
        or stat.S_IMODE(inp.st_mode) != 0o2750
    ):
        fail("INPUT_DIRECTORY_UNSAFE")


def systemctl_properties(unit: str, names: list[str]) -> dict[str, str]:
    result = systemctl(
        "show",
        unit,
        "--no-pager",
        *("--property=" + name for name in names),
        timeout=10,
    )
    values: dict[str, str] = {}
    for line in result.stdout.decode().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def verify_effective_units() -> dict[str, dict[str, Any]]:
    observed: dict[str, dict[str, Any]] = {}
    names = [
        "LimitCORE",
        "LimitCORESoft",
        "FragmentPath",
        "DropInPaths",
        "User",
        "Group",
        "PrivateNetwork",
        "MemoryDenyWriteExecute",
        "ProtectSystem",
        "RestrictAddressFamilies",
        "InaccessiblePaths",
    ]
    for unit, expected in EFFECTIVE_UNIT_EXPECTED.items():
        values = systemctl_properties(unit, names)
        if values.get("FragmentPath") != str(UNIT_DIR / unit):
            fail("UNIT_FRAGMENT_MISMATCH")
        if values.get("DropInPaths", "") != "":
            fail("UNIT_DROPIN_REFUSED")
        for key in ("User", "Group", "PrivateNetwork", "MemoryDenyWriteExecute", "ProtectSystem"):
            if values.get(key) != expected[key]:
                fail("UNIT_EFFECTIVE_PROPERTY_MISMATCH")
        for key in ("LimitCORE", "LimitCORESoft"):
            if key in expected and values.get(key) != expected[key]:
                fail("UNIT_CORE_LIMIT_MISMATCH")
        families = set(values.get("RestrictAddressFamilies", "").split())
        if families != expected["RestrictAddressFamilies"]:
            fail("UNIT_ADDRESS_FAMILY_MISMATCH")
        inaccessible = set(values.get("InaccessiblePaths", "").split())
        if inaccessible != expected["InaccessiblePaths"]:
            fail("UNIT_INACCESSIBLE_PATH_MISMATCH")
        observed[unit] = {
            **{key: values[key] for key in ("LimitCORE", "LimitCORESoft") if key in expected},
            "FragmentPath": values["FragmentPath"],
            "DropInPaths": values.get("DropInPaths", ""),
            "User": values["User"],
            "Group": values["Group"],
            "PrivateNetwork": values["PrivateNetwork"],
            "MemoryDenyWriteExecute": values["MemoryDenyWriteExecute"],
            "ProtectSystem": values["ProtectSystem"],
            "RestrictAddressFamilies": sorted(families),
            "InaccessiblePaths": sorted(inaccessible),
        }
    return observed


def sandbox_node_probe() -> dict[str, Any]:
    run(["systemctl", "daemon-reload"])
    if TRANSIENT_PROBE_STATE.exists():
        fail("NODE_SANDBOX_PROBE_STATE_EXISTS")
    try:
        result = run(
            [
                "systemd-run",
                "--quiet",
                "--pipe",
                "--wait",
                "--collect",
                "--unit=" + TRANSIENT_PROBE,
                "--uid=flop-signer",
                "--gid=flop-signer",
                "--property=StateDirectory=flop-policy-signer-probe",
                "--property=StateDirectoryMode=0700",
                "--property=PrivateNetwork=yes",
                "--property=RestrictAddressFamilies=AF_UNIX",
                "--property=NoNewPrivileges=yes",
                "--property=ProtectHome=yes",
                "--property=ProtectSystem=strict",
                "--property=InaccessiblePaths=/var/lib/flop-policy-signer-secret",
                "--property=MemoryDenyWriteExecute=yes",
                str(RUNTIME_NODE),
                "--jitless",
                "--disable-sigusr1",
                "--disallow-code-generation-from-strings",
                str(INSTALL_DEPLOY / "durability_probe.mjs"),
                str(TRANSIENT_PROBE_STATE),
            ],
            timeout=30,
        )
        try:
            value = json.loads(result.stdout.decode().strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            fail("NODE_SANDBOX_PROBE_INVALID")
        if (
            value.get("ok") is not True
            or value.get("fileFsync") is not True
            or value.get("directoryFsync") is not True
        ):
            fail("NODE_SANDBOX_PROBE_FAILED")
        return value
    finally:
        if TRANSIENT_PROBE_STATE.exists():
            shutil.rmtree(TRANSIENT_PROBE_STATE)


def test_identity(seed: bytearray) -> dict[str, str]:
    result = run(
        [str(RUNTIME_NODE), "--jitless", str(INSTALL_DEPLOY / "test_identity.mjs")],
        input_bytes=seed,
        timeout=10,
    )
    try:
        value = json.loads(result.stdout.decode())
    except json.JSONDecodeError:
        fail("TEST_IDENTITY_INVALID")
    did = value.get("did")
    commit = value.get("tclkCommit")
    if not isinstance(did, str) or not did.startswith("did:key:z6Mk") or not isinstance(commit, str):
        fail("TEST_IDENTITY_INVALID")
    return {"did": did, "tclkCommit": commit}


def build_test_config(identity: dict[str, str], now_ms: int) -> dict[str, Any]:
    if not isinstance(identity.get("did"), str) or not identity["did"].startswith("did:key:z6Mk"):
        fail("TEST_IDENTITY_INVALID")
    if not isinstance(identity.get("tclkCommit"), str) or not identity["tclkCommit"]:
        fail("TEST_IDENTITY_INVALID")
    return {
        "version": 1,
        "policy": {
            "version": 1,
            "protocol": "tclk/1",
            "tclkCommit": identity["tclkCommit"],
            "expectedDid": identity["did"],
            "workFamily": "math.gcd_lcm",
            "jobProto": "a2a",
            "jobContextPrefix": "TEST GCD bundle sha256=",
            "issuedAtMs": now_ms - 60_000,
            "expiresAtMs": now_ms + 2 * 60 * 60 * 1000,
            "freshMs": 30_000,
            "acceptMarginMs": 60_000,
            "minCompletionWindowMs": 60_000,
            "claimMarginMs": 120_000,
            "refundGapMs": 60_000,
            "maxWorkItems": 1,
            "maxSignatures": 4,
            "requireHeartbeat": True,
            "noValueMarker": "EXPLICIT_PAPER_NO_VALUE",
        },
        "acquisitionRoot": str(INPUT_DIR),
    }


def write_test_config(identity: dict[str, str]) -> None:
    signer_gid = pwd.getpwnam("flop-signer").pw_gid
    config = build_test_config(identity, int(time.time() * 1000))
    atomic_json(CONFIG, config, 0o640, 0, signer_gid)


def handoff_test_seed(seed: bytearray) -> str:
    result = run(
        [str(RUNTIME_NODE), "--jitless", str(INSTALL_SRC / "credential_handoff.mjs")],
        input_bytes=seed,
        timeout=30,
    )
    del result
    if not CREDENTIAL.exists():
        fail("TEST_CREDENTIAL_MISSING")
    return sha256_file(CREDENTIAL)


def systemctl(*args: str, timeout: int = 30, ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[bytes]:
    return run(["systemctl", *args], timeout=timeout, ok=ok)


def run_as(user: str, command: list[str], *, ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[bytes]:
    return run(["runuser", "-u", user, "--", *command], timeout=30, ok=ok)


def probe(user: str, command: str) -> dict[str, Any]:
    result = run_as(
        user,
        [str(RUNTIME_NODE), str(INSTALL_DEPLOY / "runtime_probe.mjs"), command],
    )
    try:
        return json.loads(result.stdout.decode())
    except json.JSONDecodeError:
        fail("RUNTIME_PROBE_INVALID")


def probe_denied(user: str, command: str) -> bool:
    result = run_as(
        user,
        [str(RUNTIME_NODE), str(INSTALL_DEPLOY / "runtime_probe.mjs"), command],
        ok=(0, 70),
    )
    return result.returncode != 0


def path_metadata(path: Path) -> dict[str, Any]:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    kind = (
        "socket" if stat.S_ISSOCK(st.st_mode)
        else "file" if stat.S_ISREG(st.st_mode)
        else "directory" if stat.S_ISDIR(st.st_mode)
        else "symlink" if stat.S_ISLNK(st.st_mode)
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
        "owner": owner,
        "group": group,
        "uid": st.st_uid,
        "gid": st.st_gid,
        "mode": f"{stat.S_IMODE(st.st_mode):04o}",
        "size": st.st_size if stat.S_ISREG(st.st_mode) else None,
    }


def audit_evidence() -> dict[str, Any]:
    if not AUDIT.exists():
        return {"exists": False}
    lines = 0
    with AUDIT.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            lines += chunk.count(b"\n")
    return {
        "exists": True,
        "sha256": sha256_file(AUDIT),
        "bytes": AUDIT.stat().st_size,
        "lines": lines,
    }


def offline_status() -> dict[str, Any]:
    crash_gate.require_safe()
    if not CREDENTIAL.exists() or not CONFIG.exists() or not STATE.exists():
        fail("OFFLINE_STATUS_INPUT_MISSING")
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
    decrypt = None
    inspect = None
    try:
        decrypt = subprocess.Popen(
            [
                "systemd-creds",
                "decrypt",
                "--name=project-seed",
                str(CREDENTIAL),
                "-",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        assert decrypt.stdout is not None
        inspect = subprocess.Popen(
            [
                str(RUNTIME_NODE),
                "--jitless",
                str(INSTALL_DEPLOY / "offline_state_status.mjs"),
            ],
            stdin=decrypt.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        decrypt.stdout.close()
        stdout, _ = inspect.communicate(timeout=15)
        decrypt_code = decrypt.wait(timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        if inspect is not None and inspect.poll() is None:
            inspect.kill()
        if decrypt is not None and decrypt.poll() is None:
            decrypt.kill()
        if inspect is not None:
            try:
                inspect.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if decrypt is not None:
            try:
                decrypt.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        fail("OFFLINE_STATUS_FAILED")
    if decrypt_code != 0 or inspect.returncode != 0:
        fail("OFFLINE_STATUS_FAILED")
    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError:
        fail("OFFLINE_STATUS_INVALID")


def credential_identity() -> dict[str, str]:
    crash_gate.require_safe()
    if not CREDENTIAL.exists():
        fail("TEST_CREDENTIAL_MISSING")
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
    decrypt = None
    inspect = None
    try:
        decrypt = subprocess.Popen(
            [
                "systemd-creds",
                "decrypt",
                "--name=project-seed",
                str(CREDENTIAL),
                "-",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        assert decrypt.stdout is not None
        inspect = subprocess.Popen(
            [
                str(RUNTIME_NODE),
                "--jitless",
                str(INSTALL_DEPLOY / "test_identity.mjs"),
            ],
            stdin=decrypt.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        decrypt.stdout.close()
        stdout, _ = inspect.communicate(timeout=15)
        decrypt_code = decrypt.wait(timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        if inspect is not None and inspect.poll() is None:
            inspect.kill()
        if decrypt is not None and decrypt.poll() is None:
            decrypt.kill()
        fail("TEST_CREDENTIAL_IDENTITY_FAILED")
    if decrypt_code != 0 or inspect.returncode != 0:
        fail("TEST_CREDENTIAL_IDENTITY_FAILED")
    try:
        value = json.loads(stdout.decode())
    except json.JSONDecodeError:
        fail("TEST_CREDENTIAL_IDENTITY_FAILED")
    if not isinstance(value.get("did"), str):
        fail("TEST_CREDENTIAL_IDENTITY_FAILED")
    return {"did": value["did"], "tclkCommit": value.get("tclkCommit")}


def cleanup_tmpfiles_if_principals_exist() -> None:
    if not TMPFILES.exists():
        return
    try:
        pwd.getpwnam("flop-signer-reader")
        grp.getgrnam("flop-signer-acquisition")
    except KeyError:
        return
    run(["systemd-tmpfiles", "--create", str(TMPFILES)])


def access(user: str, mode: str, path: Path) -> bool:
    result = run_as(user, ["test", mode, str(path)], ok=(0, 1))
    return result.returncode == 0


def net_ns(pid: int) -> str:
    if pid <= 0:
        fail("SERVICE_PID_INVALID")
    try:
        return os.readlink(f"/proc/{pid}/ns/net")
    except OSError:
        fail("SERVICE_NETNS_UNAVAILABLE")


def active_pid(unit: str) -> int:
    result = systemctl("show", unit, "--property=MainPID", "--value")
    try:
        pid = int(result.stdout.decode().strip())
    except ValueError:
        fail("SERVICE_PID_INVALID")
    if pid <= 0:
        fail("SERVICE_PID_INVALID")
    return pid


def observe() -> dict[str, Any]:
    require_root()
    crash_gate.require_safe()
    if not PROVENANCE.exists():
        fail("REHEARSAL_PROVENANCE_MISSING")
    provenance = load_provenance()
    verify_manifest(provenance.get("installManifest", {}))
    effective_units = verify_effective_units()

    before = probe("flop-signer", "signer-status")
    reader_before = probe("flop-signer", "reader-offers")
    signer_reader = probe("flop-signer", "signer-reader-refresh")

    before_pid = active_pid("flop-policy-signer.service")
    host_net = os.readlink("/proc/1/ns/net")
    before_net = net_ns(before_pid)

    reader_denied_secret = not access("flop-signer-reader", "-r", CREDENTIAL)
    signer_denied_secret_store = not access("flop-signer", "-r", CREDENTIAL)
    signer_reads_config = access("flop-signer", "-r", CONFIG)
    reader_denied_config = not access("flop-signer-reader", "-r", CONFIG)
    reader_denied_state = not access("flop-signer-reader", "-r", STATE)
    reader_denied_signer_socket_write = not access(
        "flop-signer-reader",
        "-w",
        Path("/run/flop-policy-signer.sock"),
    )
    reader_denied_signer_rpc = probe_denied("flop-signer-reader", "signer-status")

    metadata_before = {
        "runtimeNode": path_metadata(RUNTIME_NODE),
        "config": path_metadata(CONFIG),
        "credential": path_metadata(CREDENTIAL),
        "state": path_metadata(STATE),
        "signerSocket": path_metadata(Path("/run/flop-policy-signer.sock")),
        "readerSocket": path_metadata(Path("/run/flop-policy-signer-reader.sock")),
    }

    systemctl("stop", "flop-policy-signer.service")
    after = probe("flop-signer", "signer-status")
    after_pid = active_pid("flop-policy-signer.service")
    after_net = net_ns(after_pid)

    systemctl("stop", "flop-policy-signer-reader.service")
    reader_after = probe("flop-signer", "reader-offers")
    signer_reader_after = probe("flop-signer", "signer-reader-refresh")

    checks = {
        "didStable": before.get("did") == after.get("did") == provenance.get("testDid"),
        "policyDigestStable": (
            isinstance(before.get("policyDigest"), str)
            and before.get("policyDigest") == after.get("policyDigest")
        ),
        "revisionStable": before.get("revision") == after.get("revision") == 0,
        "noSignatures": before.get("signaturesUsed") == after.get("signaturesUsed") == 0,
        "noWork": before.get("work") is None and after.get("work") is None,
        "actionsEmpty": before.get("actions") == after.get("actions") == {},
        "signerRestarted": before_pid != after_pid,
        "signerPrivateNetNamespaceBefore": before_net != host_net,
        "signerPrivateNetNamespaceAfter": after_net != host_net,
        "readerDeniedSecret": reader_denied_secret,
        "signerDeniedSecretStore": signer_denied_secret_store,
        "signerReadsConfig": signer_reads_config,
        "readerDeniedConfig": reader_denied_config,
        "readerDeniedState": reader_denied_state,
        "readerDeniedSignerSocketWrite": reader_denied_signer_socket_write,
        "readerDeniedSignerRpc": reader_denied_signer_rpc,
        "readerOffersLive": (
            reader_before.get("room") == "tclk-offers"
            and reader_after.get("room") == "tclk-offers"
            and isinstance(reader_before.get("generation"), int)
            and isinstance(reader_after.get("generation"), int)
        ),
        "signerReaderRefreshBefore": signer_reader == {"readerRefreshed": True, "outcome": "SOURCE_INVALID"},
        "signerReaderRefreshAfter": signer_reader_after == {"readerRefreshed": True, "outcome": "SOURCE_INVALID"},
        "runtimeNodeMetadata": (
            metadata_before["runtimeNode"].get("owner") == "root"
            and metadata_before["runtimeNode"].get("group") == "root"
            and metadata_before["runtimeNode"].get("mode") == "0755"
        ),
        "configMetadata": (
            metadata_before["config"].get("owner") == "root"
            and metadata_before["config"].get("group") == "flop-signer"
            and metadata_before["config"].get("mode") == "0640"
        ),
        "credentialMetadata": (
            metadata_before["credential"].get("owner") == "root"
            and metadata_before["credential"].get("group") == "root"
            and metadata_before["credential"].get("mode") == "0600"
        ),
        "stateMetadata": (
            metadata_before["state"].get("owner") == "flop-signer"
            and metadata_before["state"].get("mode") == "0600"
        ),
        "signerSocketMetadata": (
            metadata_before["signerSocket"].get("kind") == "socket"
            and metadata_before["signerSocket"].get("owner") == "flop-signer"
            and metadata_before["signerSocket"].get("group") == "flop-agent"
            and metadata_before["signerSocket"].get("mode") == "0660"
        ),
        "readerSocketMetadata": (
            metadata_before["readerSocket"].get("kind") == "socket"
            and metadata_before["readerSocket"].get("owner") == "flop-signer-reader"
            and metadata_before["readerSocket"].get("group") == "flop-signer"
            and metadata_before["readerSocket"].get("mode") == "0660"
        ),
    }

    result = {
        "schema": 2,
        "mode": "TEST_KEY_RUNTIME_REHEARSAL_OBSERVE",
        "repoHead": provenance.get("repoHead"),
        "testDid": provenance.get("testDid"),
        "effectiveUnits": effective_units,
        "signerBefore": {
            "pid": before_pid,
            "netNamespace": before_net,
            "did": before.get("did"),
            "policyDigest": before.get("policyDigest"),
            "revision": before.get("revision"),
            "signaturesUsed": before.get("signaturesUsed"),
            "work": before.get("work"),
            "actions": before.get("actions"),
        },
        "signerAfterRestart": {
            "pid": after_pid,
            "netNamespace": after_net,
            "did": after.get("did"),
            "policyDigest": after.get("policyDigest"),
            "revision": after.get("revision"),
            "signaturesUsed": after.get("signaturesUsed"),
            "work": after.get("work"),
            "actions": after.get("actions"),
        },
        "readerBefore": reader_before,
        "readerAfterRestart": reader_after,
        "signerReaderRefresh": {"before": signer_reader, "afterRestart": signer_reader_after},
        "metadata": metadata_before,
        "checks": checks,
        "externalWritesByConstruction": 0,
        "testSeedOnlyByConstruction": True,
        "passed": all(checks.values()),
    }
    if not result["passed"]:
        fail("RUNTIME_REHEARSAL_FAILED")
    return result


def prepare(node: Path) -> dict[str, Any]:
    require_root()
    empty_setup_required()

    repo = repo_identity()
    source_node = source_node_metadata(node)
    provenance = {
        "schema": 2,
        "mode": "TEST_KEY_ONLY",
        "repoHead": repo["head"],
        "repoClean": repo["clean"],
        "repoRoot": repo["root"],
        "sourceNode": source_node["path"],
        "sourceNodeSha256": source_node["sha256"],
        "createdAtMs": int(time.time() * 1000),
        "testSeedOnlyByConstruction": True,
        "externalWritesByConstruction": 0,
    }
    provenance = write_provenance(provenance, "STARTING")

    runtime = install_runtime(node, source_node)
    provenance = write_provenance(
        provenance,
        "RUNTIME_INSTALLED",
        nodeSha256=runtime["nodeSha256"],
        nodeVersion=runtime["nodeVersion"],
        installManifest=runtime["manifest"],
    )

    provision_principals_and_dirs()
    provenance = write_provenance(provenance, "PRINCIPALS_READY")

    systemctl("daemon-reload")
    effective_units = verify_effective_units()
    provenance = write_provenance(
        provenance,
        "UNITS_VERIFIED",
        effectiveUnits=effective_units,
    )

    return finish_prepare(provenance, runtime, "TEST_KEY_RUNTIME_REHEARSAL_PREPARE")


def finish_prepare(provenance: dict[str, Any], runtime: dict[str, Any], mode: str) -> dict[str, Any]:

    sandbox = sandbox_node_probe()
    provenance = write_provenance(provenance, "SANDBOX_PROBED", sandboxNode=sandbox)

    crash_gate.operate("arm")
    crash_gate.require_safe()
    seed = bytearray(os.urandom(32))
    try:
        identity = test_identity(seed)
        provenance = write_provenance(
            provenance,
            "IDENTITY_READY",
            testDid=identity["did"],
            tclkCommit=identity["tclkCommit"],
        )

        write_test_config(identity)
        provenance = write_provenance(
            provenance,
            "CONFIG_WRITTEN",
            configSha256=sha256_file(CONFIG),
        )

        provenance = write_provenance(provenance, "CREDENTIAL_HANDOFF_STARTING")
        credential_sha = handoff_test_seed(seed)
        provenance = write_provenance(
            provenance,
            "CREDENTIAL_WRITTEN",
            credentialSha256=credential_sha,
        )
    finally:
        for i in range(len(seed)):
            seed[i] = 0

    provenance = write_provenance(provenance, "BOOTSTRAP_STARTING")
    systemctl("start", "flop-policy-signer-bootstrap.service")
    if not STATE.exists():
        fail("BOOTSTRAP_STATE_MISSING")
    provenance = write_provenance(
        provenance,
        "BOOTSTRAPPED",
        stateSha256=sha256_file(STATE),
    )

    provenance = write_provenance(provenance, "SOCKETS_STARTING")
    systemctl("start", *SOCKETS)
    provenance = write_provenance(provenance, "SOCKETS_STARTED")

    observed = observe()
    provenance = write_provenance(provenance, "OBSERVED", lastObserved=observed["checks"])
    return {
        "schema": 2,
        "mode": mode,
        "repoHead": provenance["repoHead"],
        "testDid": identity["did"],
        "runtimeNode": {
            "nodeSha256": runtime["nodeSha256"],
            "nodeVersion": runtime["nodeVersion"],
        },
        "sandboxNode": sandbox,
        "observed": observed,
        "testSeedOnlyByConstruction": True,
        "externalWritesByConstruction": 0,
        "passed": observed["passed"],
    }


def prepare_existing() -> dict[str, Any]:
    require_root()
    # No mutation, including provenance, until all retained infrastructure and
    # the reviewed source checkout have passed their independent checks.
    retained = validate_retained_infrastructure()
    repo = repo_identity()
    target = reviewed_target_manifest()
    if not set(retained["manifest"]).issubset(set(target)):
        fail("RUNTIME_UPDATE_TARGET_SHAPE_MISMATCH")
    if target[str(RUNTIME_NODE)] != retained["manifest"][str(RUNTIME_NODE)]:
        fail("RUNTIME_UPDATE_TARGET_NODE_MISMATCH")
    provenance = {
        "schema": 2, "mode": "TEST_KEY_ONLY", "entryMode": "PREPARE_EXISTING",
        "reusedInfrastructure": True, "repoHead": repo["head"],
        "repoClean": repo["clean"], "repoRoot": repo["root"],
        "previousInstalledHead": retained["installedHead"],
        "previousInstallManifest": retained["manifest"],
        "targetInstallManifest": target,
        "nodeSha256": target[str(RUNTIME_NODE)],
        "nodeVersion": retained["nodeVersion"],
        "createdAtMs": int(time.time() * 1000),
        "testSeedOnlyByConstruction": True, "externalWritesByConstruction": 0,
    }
    provenance = write_provenance(provenance, "RUNTIME_UPDATE_STARTING")
    # A stopped service remains stopped while files are replaced. If this
    # operation fails, provenance remains and prepare-existing refuses retry.
    manifest = update_reviewed_runtime(target)
    verify_manifest(target)
    systemctl("daemon-reload")
    effective = verify_effective_units()
    provenance = write_provenance(
        provenance, "UNITS_VERIFIED", installManifest=manifest, effectiveUnits=effective)
    runtime = {"nodeSha256": target[str(RUNTIME_NODE)], "nodeVersion": retained["nodeVersion"]}
    return finish_prepare(provenance, runtime, "TEST_KEY_RUNTIME_REHEARSAL_PREPARE_EXISTING")


def resume_reviewed() -> dict[str, Any]:
    require_root()
    if not PROVENANCE.exists():
        fail("REHEARSAL_PROVENANCE_MISSING")
    provenance = load_provenance()
    if (provenance.get("entryMode") == "PREPARE_EXISTING"
        and provenance.get("phase") == "RUNTIME_UPDATE_STARTING"):
        fail("RUNTIME_UPDATE_PARTIAL_MANUAL_REVIEW")

    phase = provenance.get("phase")
    if phase not in {
        "SOCKETS_STARTED",
        "RUNTIME_UPDATE_STARTING",
        "RUNTIME_UPDATED",
        "RESUME_SOCKETS_STARTED",
    }:
        fail("RUNTIME_RESUME_PHASE_REFUSED")

    systemctl(
        "stop",
        "flop-policy-signer.service",
        "flop-policy-signer-reader.service",
        *SOCKETS,
        "flop-policy-signer-bootstrap.service",
        ok=(0, 5),
    )

    crash_gate.operate("arm")
    crash_gate.require_safe()

    repo = repo_identity()
    old_manifest = provenance.get("installManifest")
    if not isinstance(old_manifest, dict) or not old_manifest:
        fail("INSTALL_MANIFEST_MISSING")

    if phase == "SOCKETS_STARTED":
        verify_manifest(old_manifest)
        protected = offline_status()
        if (
            protected.get("did") != provenance.get("testDid")
            or protected.get("signaturesUsed") != 0
            or protected.get("work") is not None
            or protected.get("actions") != {}
        ):
            fail("RUNTIME_RESUME_STATE_UNSAFE")

        target_manifest = reviewed_target_manifest()
        provenance = write_provenance(
            provenance,
            "RUNTIME_UPDATE_STARTING",
            previousRepoHead=provenance.get("repoHead"),
            targetRepoHead=repo["head"],
            targetInstallManifest=target_manifest,
            resumeProtectedStatus=protected,
        )
        phase = "RUNTIME_UPDATE_STARTING"
    else:
        if provenance.get("targetRepoHead") != repo["head"]:
            fail("RUNTIME_RESUME_HEAD_MISMATCH")
        target_manifest = provenance.get("targetInstallManifest")
        if not isinstance(target_manifest, dict) or not target_manifest:
            fail("RUNTIME_UPDATE_TARGET_MISSING")

    if phase == "RUNTIME_UPDATE_STARTING":
        current_manifest = installed_manifest()
        if current_manifest == old_manifest:
            update_reviewed_runtime(target_manifest)
        elif current_manifest == target_manifest:
            pass
        else:
            fail("RUNTIME_UPDATE_PARTIAL_MANUAL_REVIEW")

        systemctl("daemon-reload")
        effective_units = verify_effective_units()
        provenance = write_provenance(
            provenance,
            "RUNTIME_UPDATED",
            repoHead=repo["head"],
            repoClean=repo["clean"],
            installManifest=target_manifest,
            effectiveUnits=effective_units,
        )
        phase = "RUNTIME_UPDATED"

    if phase == "RUNTIME_UPDATED":
        verify_manifest(provenance["installManifest"])
        systemctl("daemon-reload")
        effective_units = verify_effective_units()
        provenance = write_provenance(
            provenance,
            "RUNTIME_UPDATED",
            effectiveUnits=effective_units,
        )

        systemctl("start", *SOCKETS)
        provenance = write_provenance(provenance, "RESUME_SOCKETS_STARTED")
        phase = "RESUME_SOCKETS_STARTED"

    if phase == "RESUME_SOCKETS_STARTED":
        # The entry stop also stops sockets. Recheck the installed target and
        # protected test state before restoring the same socket entry points.
        if provenance.get("installManifest") != target_manifest:
            fail("RUNTIME_UPDATE_TARGET_MISMATCH")
        verify_manifest(provenance["installManifest"])
        protected = offline_status()
        if (
            protected.get("did") != provenance.get("testDid")
            or protected.get("revision") != 0
            or protected.get("signaturesUsed") != 0
            or protected.get("work") is not None
            or protected.get("actions") != {}
        ):
            fail("RUNTIME_RESUME_STATE_UNSAFE")
        systemctl("start", *SOCKETS)
        observed = observe()
        provenance = write_provenance(
            provenance,
            "OBSERVED",
            lastObserved=observed["checks"],
        )
        return {
            "schema": 2,
            "mode": "TEST_KEY_RUNTIME_REHEARSAL_RESUME",
            "previousRepoHead": provenance.get("previousRepoHead"),
            "repoHead": provenance.get("repoHead"),
            "testDid": provenance.get("testDid"),
            "observed": observed,
            "testSeedOnlyByConstruction": True,
            "externalWritesByConstruction": 0,
            "passed": observed["passed"],
        }

    fail("RUNTIME_RESUME_STATE_INVALID")


def cleanup_test_key() -> dict[str, Any]:
    require_root()
    if not PROVENANCE.exists():
        fail("REHEARSAL_PROVENANCE_MISSING")
    provenance = load_provenance()
    if (provenance.get("entryMode") == "PREPARE_EXISTING"
        and provenance.get("phase") == "RUNTIME_UPDATE_STARTING"):
        fail("RUNTIME_UPDATE_PARTIAL_MANUAL_REVIEW")

    if provenance.get("installManifest"):
        verify_manifest(provenance["installManifest"])

    # Close every runtime entry point before checking protected state. Cleanup
    # must not depend on a healthy live Signer service.
    systemctl(
        "stop",
        "flop-policy-signer.service",
        "flop-policy-signer-reader.service",
        *SOCKETS,
        "flop-policy-signer-bootstrap.service",
        ok=(0, 5),
    )

    cleanup_retry = phase_at_least(provenance, "CLEANUP_STARTED")
    audit = provenance.get("cleanupAuditEvidence") or audit_evidence()

    config_exists = CONFIG.exists()
    credential_exists = CREDENTIAL.exists()
    state_exists = STATE.exists()

    if config_exists:
        try:
            config = json.loads(CONFIG.read_text())
        except (OSError, json.JSONDecodeError):
            fail("CONFIG_PROVENANCE_MISMATCH")
        if not isinstance(provenance.get("testDid"), str):
            fail("CONFIG_PROVENANCE_MISMATCH")
        if config.get("policy", {}).get("expectedDid") != provenance["testDid"]:
            fail("CONFIG_PROVENANCE_MISMATCH")
        expected_config_sha = provenance.get("configSha256")
        if expected_config_sha is not None and sha256_file(CONFIG) != expected_config_sha:
            fail("CONFIG_PROVENANCE_MISMATCH")
    elif phase_at_least(provenance, "CONFIG_WRITTEN") and not cleanup_retry:
        fail("CONFIG_PROVENANCE_MISMATCH")

    if credential_exists:
        expected_credential_sha = provenance.get("credentialSha256")
        if expected_credential_sha is not None:
            if sha256_file(CREDENTIAL) != expected_credential_sha:
                fail("CREDENTIAL_PROVENANCE_MISMATCH")
        elif provenance.get("phase") == "CREDENTIAL_HANDOFF_STARTING":
            identity = credential_identity()
            if identity.get("did") != provenance.get("testDid"):
                fail("CREDENTIAL_PROVENANCE_MISMATCH")
        elif not cleanup_retry:
            fail("CREDENTIAL_PROVENANCE_MISMATCH")
    elif phase_at_least(provenance, "CREDENTIAL_WRITTEN") and not cleanup_retry:
        fail("CREDENTIAL_PROVENANCE_MISMATCH")

    protected_status = provenance.get("cleanupProtectedStatus")
    if state_exists:
        if cleanup_retry and isinstance(protected_status, dict):
            pass
        else:
            if not config_exists or not credential_exists:
                fail("TEST_STATE_NOT_INSPECTABLE")
            protected_status = offline_status()
        if (
            not isinstance(protected_status, dict)
            or protected_status.get("did") != provenance.get("testDid")
            or protected_status.get("signaturesUsed") != 0
            or protected_status.get("work") is not None
            or protected_status.get("actions") != {}
        ):
            fail("TEST_STATE_NOT_SAFE_TO_REMOVE")
    elif phase_at_least(provenance, "BOOTSTRAPPED") and not cleanup_retry:
        fail("TEST_STATE_PROVENANCE_MISMATCH")

    provenance = write_provenance(
        provenance,
        "CLEANUP_STARTED",
        cleanupAuditEvidence=audit,
        cleanupProtectedStatus=protected_status,
    )

    if CREDENTIAL.exists():
        CREDENTIAL.unlink()
    if CONFIG.exists():
        CONFIG.unlink()
    if STATE_DIR.exists():
        shutil.rmtree(STATE_DIR)
    if INPUT_DIR.exists():
        shutil.rmtree(INPUT_DIR)
    cleanup_tmpfiles_if_principals_exist()

    provenance = write_provenance(provenance, "CLEANUP_ARTIFACTS_REMOVED")

    for unit in (*SERVICES, *SOCKETS, "flop-policy-signer-bootstrap.service"):
        systemctl("reset-failed", unit, ok=(0, 1, 5))

    result = {
        "schema": 2,
        "mode": "TEST_KEY_RUNTIME_REHEARSAL_CLEANUP",
        "credentialRemoved": not CREDENTIAL.exists(),
        "configRemoved": not CONFIG.exists(),
        "stateRemoved": not STATE.exists(),
        "auditEvidence": audit,
        "protectedStatus": protected_status,
        "infrastructureRetained": True,
        "testSeedOnlyByConstruction": True,
        "externalWritesByConstruction": 0,
        "passed": not CREDENTIAL.exists() and not CONFIG.exists() and not STATE.exists(),
    }
    if not result["passed"]:
        fail("TEST_CLEANUP_INCOMPLETE")

    # Last secret consumer has exited and credential/state are removed. Keep
    # provenance if disarm fails; retry either resumes safely or refuses.
    gate_status = crash_gate.status()
    if gate_status["isWsl"] is not False:
        result["crashCaptureAfterCleanup"] = crash_gate.operate("disarm")
    PROVENANCE.unlink()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--node", required=True)
    sub.add_parser("prepare-existing")
    sub.add_parser("observe")
    sub.add_parser("resume-reviewed")
    sub.add_parser("cleanup-test-key")
    args = parser.parse_args(argv)

    try:
        if args.command == "prepare":
            result = prepare(Path(args.node))
        elif args.command == "prepare-existing":
            result = prepare_existing()
        elif args.command == "observe":
            result = observe()
        elif args.command == "resume-reviewed":
            result = resume_reviewed()
        else:
            result = cleanup_test_key()
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (RehearsalError, crash_gate.GateError) as error:
        print(json.dumps({
            "schema": 1,
            "passed": False,
            "code": str(error),
            "testSeedOnlyByConstruction": True,
        }, sort_keys=True, separators=(",", ":")))
        return 70
    except Exception:
        print(json.dumps({
            "schema": 1,
            "passed": False,
            "code": "UNEXPECTED_REHEARSAL_FAILURE",
            "testSeedOnlyByConstruction": True,
        }, sort_keys=True, separators=(",", ":")))
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
