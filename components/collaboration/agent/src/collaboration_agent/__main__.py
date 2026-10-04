"""Single-shot local CLI. No transport, signer, subprocess or plugin loading."""

import argparse
import json
import sqlite3

from .engine import Agent
from .model import Invalid, read_input
from .state import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline Collaboration Agent v0.1")
    parser.add_argument("--state", required=True, help="Dedicated local state directory")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--per-hour", type=int, default=60)
    init.add_argument("--per-day", type=int, default=500)
    run = sub.add_parser("run")
    run.add_argument("task")
    run.add_argument("--dry-run", action="store_true")
    preview = sub.add_parser("preview")
    preview.add_argument("task_id")
    family = sub.add_parser("family")
    family.add_argument("name")
    family.add_argument("action", choices=("enable", "disable", "suspend", "resume"))
    family.add_argument("--reason", required=True)
    sub.add_parser("status")
    sub.add_parser("audit")
    args = parser.parse_args(argv)
    try:
        kwargs = {"create": True, "hour": args.per_hour, "day": args.per_day} if args.command == "init" else {}
        with Store(args.state, **kwargs) as store:
            agent = Agent(store)
            agent.recover()
            if args.command == "run":
                value = agent.process(read_input(args.task), dry_run=args.dry_run)
            elif args.command == "preview":
                value = agent.preview(args.task_id)
            elif args.command == "family":
                value = store.change_family(args.name, args.action, args.reason)
            else:
                value = store.verify()
        # ASCII JSON also escapes terminal control, bidi and embedded line characters.
        print(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True))
        return 0
    except Invalid as exc:
        print(json.dumps({"status": "HUMAN_REVIEW", "reason": str(exc),
                          "action": "inspect_local_state_and_input_before_retry"}))
        return 2
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        # State/file failures are operational failures, never a successfully processed task.
        print(json.dumps({"status": "HUMAN_REVIEW", "reason": "state_or_input_unavailable",
                          "action": "inspect_local_state_and_input_before_retry"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
