"""Small, explicit V2 identities, read-only inspection, and immutable evidence."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from dataclasses import dataclass

CANDIDATE = "0ed90b21656c32d6dc37a97b6eb7600c95f88fb0"
ATTEMPT = re.compile(r"[0-9]{8}-v2-[0-9]{2}\Z")
FULL_ID = re.compile(r"[0-9a-f]{64}\Z")
ROLES = ("capture", "archive")
LABELS = {"attempt": "technocore.capture.attempt", "role": "technocore.capture.role",
          "candidate": "technocore.capture.candidate", "release": "technocore.capture.release"}


class V2Error(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def require(ok, code):
    if not ok:
        raise V2Error(code)


def attempt_id(value):
    require(isinstance(value, str) and ATTEMPT.fullmatch(value) is not None, "ATTEMPT_ID")
    return value


def container_id(value):
    require(isinstance(value, str) and FULL_ID.fullmatch(value) is not None, "FULL_CONTAINER_ID")
    return value


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def unique_json(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "DUPLICATE_JSON_KEY")
        value[key] = item
    return value


def read_json(path, *, max_bytes=256 * 1024):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        row = os.fstat(fd)
        require(stat.S_ISREG(row.st_mode) and row.st_size <= max_bytes, "JSON_FILE_TYPE_OR_SIZE")
        data = os.read(fd, max_bytes + 1)
        require(len(data) <= max_bytes, "JSON_SIZE")
    finally:
        os.close(fd)
    try:
        return json.loads(data, object_pairs_hook=unique_json)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise V2Error("INVALID_JSON") from exc


def read_bytes(path, *, max_bytes=8 * 1024 * 1024):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        row = os.fstat(fd)
        require(stat.S_ISREG(row.st_mode) and row.st_size <= max_bytes, "FILE_TYPE_OR_SIZE")
        data = os.read(fd, max_bytes + 1)
        require(len(data) <= max_bytes, "FILE_SIZE")
        return data
    finally:
        os.close(fd)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_once(path, data, *, mode=0o600):
    path = Path(path)
    if not isinstance(data, bytes):
        data = data.encode()
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        # Preserve partial evidence for Human reconciliation; never unlink/overwrite it.
        raise
    sync_dir(path.parent)


def write_json_once(path, value):
    write_once(path, json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


@dataclass(frozen=True)
class Layout:
    release_base: Path = Path("/opt/technocore-capture/upgrade-v2")
    state_base: Path = Path("/etc/technocore-capture/upgrade-v2")
    observation_base: Path = Path("/srv/technocore-capture/control")
    dropin_base: Path = Path("/etc/systemd/system")
    shared_etc: Path = Path("/etc/technocore-capture")
    shared_root: Path = Path("/srv/technocore-capture")

    def release(self, attempt):
        return self.release_base / attempt_id(attempt)

    def state(self, attempt):
        return self.state_base / attempt_id(attempt)

    def observation(self, attempt):
        return self.observation_base / ("observation-v2-" + attempt_id(attempt))

    def switch(self, role, attempt):
        require(role in (*ROLES, "monitor"), "UNIT_ROLE")
        return (self.dropin_base / ("technocore-capture-" + role + ".service.d") /
                ("99-zz-tc-v2-" + attempt_id(attempt) + ".conf"))


class Runner:
    """Subprocess boundary; tests inject a fake. No shell or inherited credentials."""
    def run(self, argv, *, timeout=60):
        require(isinstance(argv, (list, tuple)) and all(isinstance(x, str) for x in argv),
                "COMMAND_ARGV")
        try:
            return subprocess.run(list(argv), capture_output=True, text=True,
                                  timeout=timeout, check=False,
                                  env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise V2Error("COMMAND_UNAVAILABLE") from exc


def command(runner, argv, code, *, timeout=60):
    result = runner.run(argv, timeout=timeout)
    require(result.returncode == 0, code)
    return result.stdout


def docker_absent(stderr, target):
    """Recognize only Docker's complete missing-target diagnostic."""
    message = (stderr or "").strip().lower()
    return message in {"error: no such object: " + target.lower(),
                       "error: no such container: " + target.lower()}


def docker_inspect(runner, cid, *, timeout=30):
    """Exact full-ID raw inspect only; never follow names."""
    container_id(cid)
    result = runner.run(["docker", "inspect", "--type=container", cid], timeout=timeout)
    if result.returncode:
        if docker_absent(result.stderr, cid):
            return None
        raise V2Error("DOCKER_INSPECT_UNKNOWN")
    try:
        rows = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise V2Error("DOCKER_INSPECT_JSON") from exc
    require(isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict),
            "DOCKER_INSPECT_SHAPE")
    return rows[0]


def unit_state(runner, role):
    require(role in (*ROLES, "monitor"), "UNIT_ROLE")
    unit = "technocore-capture-" + role + ".service"
    raw = command(runner, ["systemctl", "show", unit, "--property=ExecStartPre,ExecStart,ExecStartPost,ExecStop,ExecStopPost,Restart,DropInPaths,ActiveState,KillMode,TimeoutStartUSec,TimeoutStopUSec"],
                  "SYSTEMD_SHOW")
    values = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    # systemd 255 omits unset Exec properties even when explicitly requested.
    optional_empty = ({"ExecStartPost", "ExecStopPost"} if role in ROLES else
                      {"ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost"})
    required = ("ExecStartPre", "ExecStart", "ExecStartPost", "ExecStop", "ExecStopPost",
                "Restart", "DropInPaths", "ActiveState", "KillMode", "TimeoutStartUSec",
                "TimeoutStopUSec")
    require(all(key in values for key in required if key not in optional_empty),
            "SYSTEMD_SHOW_FIELDS")
    for key in optional_empty:
        values.setdefault(key, "")
    return values


def timer_state(runner):
    raw = command(runner, ["systemctl", "show", "technocore-capture-monitor.timer", "--property=ActiveState"],
                  "TIMER_SHOW")
    return raw.strip().removeprefix("ActiveState=")


def effective_argv(value):
    """Extract every systemd show argv[] command, ignoring runtime fields."""
    commands = [part.strip() for part in re.findall(r"argv\[\]=([^;}]+)", value)]
    require(not value or (commands and value.count("argv[]=") == len(commands)),
            "SYSTEMD_EXEC_ARGV")
    return commands


def exec_fingerprint(value):
    argv = effective_argv(value)
    blocks = re.findall(r"\{([^{}]*)\}", value)
    require(len(blocks) == len(argv), "SYSTEMD_EXEC_ARGV")
    commands = []
    for block, args in zip(blocks, argv):
        path = re.search(r"(?:^|;)\s*path=([^;]+)", block)
        ignore_errors = re.search(r"(?:^|;)\s*ignore_errors=([^;]+)", block)
        require(path is not None, "SYSTEMD_EXEC_ARGV")
        commands.append((path.group(1).strip(), args,
                         ignore_errors.group(1).strip() if ignore_errors else ""))
    return sha256(json.dumps(commands, separators=(",", ":")).encode())


def unit_fingerprint(runner, role):
    """Retain comparable effective commands without copying possible argv secrets into evidence."""
    row = unit_state(runner, role)
    return {key: exec_fingerprint(row[key]) for key in
            ("ExecStartPre", "ExecStart", "ExecStartPost", "ExecStop", "ExecStopPost")} | {
            "DropInPaths": sha256(row["DropInPaths"].encode()),
            "Restart": row["Restart"], "ActiveState": row["ActiveState"],
            "DropInPathsList": row["DropInPaths"].split(),
            "KillMode": row["KillMode"], "TimeoutStartUSec": row["TimeoutStartUSec"],
            "TimeoutStopUSec": row["TimeoutStopUSec"]}
