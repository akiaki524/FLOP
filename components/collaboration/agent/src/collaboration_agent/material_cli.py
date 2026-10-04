"""Explicit acquisition command separated from offline frozen-task execution."""

import argparse
import json
from pathlib import Path

from .engine import Agent
from .material_fetch import Fetcher
from .materials import Resolver, freeze, run_frozen
from .model import Invalid, decode, read_input
from .state import Store


def main():
    parser = argparse.ArgumentParser(description="Bounded public material acquisition / offline replay")
    sub = parser.add_subparsers(dest="command", required=True)
    acquire = sub.add_parser("resolve")
    acquire.add_argument("request")
    acquire.add_argument("--output", type=Path, required=True)
    replay = sub.add_parser("run")
    replay.add_argument("bundle", type=Path)
    replay.add_argument("--state", required=True)
    args = parser.parse_args()
    try:
        if args.command == "resolve":
            args.output.mkdir(parents=True, exist_ok=False)
            fetcher = Fetcher(args.output / "acquisition")
            bundle = Resolver(fetcher).resolve(decode(read_input(args.request)))
            identifier = freeze(bundle, fetcher, args.output / "frozen")
            result = {"bundle_sha256": identifier, "completeness": bundle["completeness"], "errors": bundle["errors"]}
        else:
            with Store(args.state) as store:
                result = run_frozen(args.bundle, Agent(store))
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (Invalid, OSError, ValueError) as exc:
        print(json.dumps({"status": "HUMAN_REVIEW", "reason": str(exc) if isinstance(exc, Invalid) else "MATERIAL_OPERATION_FAILED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
