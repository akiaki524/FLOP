"""Emergency authority: exact, attempt-bound Docker IDs only."""
from .common import CANDIDATE, LABELS, ROLES, V2Error, container_id, docker_inspect, require


def identity(row, *, cid, image, attempt, role, release):
    """Corroborate a full ID; names, unit state, and mutable receipts give no authority."""
    require(role in ROLES, "ROLE")
    labels = row.get("Config", {}).get("Labels") or {}
    return (row.get("Id") == container_id(cid) and row.get("Image") == image and
            labels.get(LABELS["attempt"]) == attempt and labels.get(LABELS["role"]) == role and
            labels.get(LABELS["candidate"]) == CANDIDATE and labels.get(LABELS["release"]) == release and
            row.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") == "no")


def exact_stop(runner, *, cid, image, attempt, role, release, timeout=40):
    """No fallback by name. Unknown/mismatch must not mutate anything."""
    try:
        row = docker_inspect(runner, cid)
    except V2Error:
        return "UNKNOWN"
    if row is None:
        return "ABSENT"
    if not identity(row, cid=cid, image=image, attempt=attempt, role=role, release=release):
        return "TARGET_MISMATCH"
    state = row.get("State", {})
    if state.get("Running") is False and state.get("Pid") == 0:
        return "ALREADY_STOPPED"
    if state.get("Running") is not True:
        return "UNKNOWN"
    try:
        result = runner.run(["docker", "stop", "--time", str(timeout), cid], timeout=timeout + 15)
    except V2Error:
        return "STOP_FAILED"
    if result.returncode:
        return "STOP_FAILED"
    try:
        after = docker_inspect(runner, cid)
    except V2Error:
        return "UNKNOWN"
    if after is None or not identity(after, cid=cid, image=image, attempt=attempt, role=role, release=release):
        return "UNKNOWN"
    return "STOPPED" if after.get("State", {}).get("Running") is False and after.get("State", {}).get("Pid") == 0 else "STOP_FAILED"


def stop_both(runner, *, attempt, release, image, capture_id, archive_id):
    # Attempt both, preserving each result. Never search by role/name on a failure.
    return {role: exact_stop(runner, cid=cid, image=image, attempt=attempt,
                             role=role, release=release)
            for role, cid in (("capture", capture_id), ("archive", archive_id))}


def stopped(results):
    return all(value in {"STOPPED", "ALREADY_STOPPED", "ABSENT"} for value in results.values())
