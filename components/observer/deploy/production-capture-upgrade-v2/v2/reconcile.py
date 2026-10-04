"""Read-only reconciliation of one Attempt. No resume, repair, or cleanup."""
from .common import ROLES, V2Error, docker_inspect, read_bytes, read_json, sha256, timer_state, unit_state
from .preflight import _named_container
from .stop import identity


def _evidence(path):
    if not path.exists():
        return {"status": "ABSENT"}
    try:
        return {"status": "OBSERVED", "value": read_json(path)}
    except (OSError, V2Error):
        return {"status": "UNKNOWN"}


def reconcile(runner, layout, attempt):
    """Inspect exact IDs and owned switch bytes; never issue a mutation command."""
    state = layout.state(attempt)
    report = {"attempt": attempt,
              "evidence": {name: _evidence(state / name) for name in (
                  "attempt.json", "staged.json", "activation-begin.json",
                  "activation-started.json", "accepted.json", "terminal.json", "rollback.json")},
              "containers": {}, "switches": {}, "units": {}, "timer": "UNKNOWN",
              "old_containers": "UNKNOWN", "observation": "ABSENT"}
    staged = report["evidence"]["staged.json"]
    attempt_record = report["evidence"]["attempt.json"]
    terminal = report["evidence"]["terminal.json"]
    if staged["status"] == "OBSERVED":
        item = staged["value"]
    elif terminal["status"] == "OBSERVED" and attempt_record["status"] == "OBSERVED":
        progress = terminal["value"].get("progress", {})
        item = {"container_ids": progress.get("container_ids", {}),
                "image_id": progress.get("image_id"),
                "release_commit": attempt_record["value"].get("release_commit")}
    else:
        item = None
    if item is not None:
        for role in ROLES:
            cid = item.get("container_ids", {}).get(role)
            if cid is None:
                report["containers"][role] = "ABSENT"
                continue
            try:
                row = docker_inspect(runner, cid)
                if row is None:
                    status = "ABSENT"
                elif identity(row, cid=cid, image=item["image_id"], attempt=attempt,
                              role=role, release=item["release_commit"]):
                    status = "RUNNING" if row.get("State", {}).get("Running") else "STOPPED"
                else:
                    status = "MISMATCH"
            except (V2Error, KeyError):
                status = "UNKNOWN"
            report["containers"][role] = status
    begin = report["evidence"]["activation-begin.json"]
    for role in (*ROLES, "monitor"):
        path = layout.switch(role, attempt)
        if path.exists() or path.is_symlink():
            try:
                digest = sha256(read_bytes(path))
                expected = (begin["value"].get("switch_sha256", {}).get(role)
                            if begin["status"] == "OBSERVED" else None)
                report["switches"][role] = ("OBSERVED" if digest == expected else "MISMATCH")
            except (OSError, V2Error):
                report["switches"][role] = "UNKNOWN"
        else:
            report["switches"][role] = "ABSENT"
        try:
            report["units"][role] = unit_state(runner, role)
        except V2Error:
            report["units"][role] = "UNKNOWN"
    try:
        report["timer"] = timer_state(runner)
    except V2Error:
        pass
    try:
        old = {}
        for role in ROLES:
            row = _named_container(runner, "tc-cap-loop-01-lobby-" + role)
            old[role] = "ABSENT" if row is None else (
                "RUNNING" if row.get("State", {}).get("Running") else "STOPPED")
        report["old_containers"] = old
    except V2Error:
        pass
    report["observation"] = "OBSERVED" if layout.observation(attempt).is_dir() else "ABSENT"
    return report
