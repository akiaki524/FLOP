"""Human-gated V2 command entrypoint. Importing this module performs no operation."""
import argparse
import json
from pathlib import Path

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from v2.activate import activate, explicit_rollback
    from v2.common import Layout, Runner, V2Error, attempt_id
    from v2.preflight import snapshot
    from v2.reconcile import reconcile
    from v2.stage import stage
else:
    from .activate import activate, explicit_rollback
    from .common import Layout, Runner, V2Error, attempt_id
    from .preflight import snapshot
    from .reconcile import reconcile
    from .stage import stage


def main(argv=None):
    parser = argparse.ArgumentParser(description="V2 Capture upgrade; Production use requires separate Human approval")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("pre", "stage", "activate", "rollback", "reconcile"):
        item = sub.add_parser(name)
        item.add_argument("attempt", help="YYYYMMDD-v2-NN")
        if name in ("pre", "stage"):
            item.add_argument("--source", type=Path, required=True)
        if name == "stage":
            item.add_argument("--release-commit", required=True)
        if name in ("stage", "activate", "rollback"):
            item.add_argument("--human-approved", action="store_true",
                              help="records an already obtained separate Human Gate; not an authorization token")
    args = parser.parse_args(argv)
    try:
        attempt = attempt_id(args.attempt)
        runner, layout = Runner(), Layout()
        if args.command == "pre":
            result = snapshot(runner, layout, args.source, attempt)
        elif args.command == "stage":
            result = stage(runner, layout, source_root=args.source, attempt=attempt,
                           release=args.release_commit, human_approved=args.human_approved)
        elif args.command == "activate":
            result = activate(runner, layout, attempt=attempt,
                              human_approved=args.human_approved)
        elif args.command == "rollback":
            result = explicit_rollback(runner, layout, attempt=attempt,
                                       human_approved=args.human_approved)
        else:
            result = reconcile(runner, layout, attempt)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (V2Error, OSError, KeyError, TypeError, ValueError) as exc:
        # Never echo subprocess output, paths from receipts, or raw config values.
        print(json.dumps({"status": "FAIL", "code": exc.code if isinstance(exc, V2Error)
                          else "V2_UNEXPECTED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
