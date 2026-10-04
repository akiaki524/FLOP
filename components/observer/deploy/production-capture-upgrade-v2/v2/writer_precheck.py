"""Read-only ExecStartPre for one exact staged container."""
import argparse
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from v2.common import Layout, Runner, V2Error, docker_inspect, read_json, require
    from v2.preflight import old_writer_check, policy_check, require_host, shared_writer_check, v2_mount_check
    from v2.stop import identity
else:
    from .common import Layout, Runner, V2Error, docker_inspect, read_json, require
    from .preflight import old_writer_check, policy_check, require_host, shared_writer_check, v2_mount_check
    from .stop import identity


def check(runner, layout, attempt, role, cid):
    require_host()
    staged = read_json(layout.state(attempt) / "staged.json")
    require(staged["container_ids"][role] == cid, "PRECHECK_BOUND_ID")
    source = layout.release(attempt) / "source"
    require(policy_check(layout.shared_etc / "policy.json", source) ==
            staged["baseline"]["policy_sha256"], "PRECHECK_POLICY_CHANGED")
    old_writer_check(runner)
    shared_writer_check(runner, layout, allowed_ids=tuple(staged["container_ids"].values()))
    v2_mount_check(runner, layout)
    row = docker_inspect(runner, cid)
    require(row is not None and identity(row, cid=cid, image=staged["image_id"],
            attempt=attempt, role=role, release=staged["release_commit"]),
            "PRECHECK_IDENTITY")
    require(row.get("State", {}).get("Running") is False, "PRECHECK_ALREADY_RUNNING")


def main(argv=None):
    parser = argparse.ArgumentParser(description="V2 exact Writer read-only precheck")
    parser.add_argument("attempt")
    parser.add_argument("role", choices=("capture", "archive"))
    parser.add_argument("container_id")
    args = parser.parse_args(argv)
    try:
        check(Runner(), Layout(), args.attempt, args.role, args.container_id)
    except V2Error as exc:
        print(exc.code)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
