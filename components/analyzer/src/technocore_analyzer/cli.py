"""Command line. Finite commands only; no daemon, no network, no sending.

Human-owned state (review, report submission events, resolution) is written only by the
``review`` / ``report-event`` / ``resolve`` commands, which require ``--reviewer`` /
``--actor`` and record origin HUMAN_CLI. No LLM path calls them.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import config as config_mod
from . import engine, semantic
from .store import Store
from .util import AnalyzerError, parse_rfc3339_ms


def _print(value):
    # ensure_ascii keeps terminal output free of raw control/escape sequences from Evidence.
    sys.stdout.write(json.dumps(value, ensure_ascii=True, indent=1, sort_keys=True) + "\n")


def _time(value, name):
    if value is None:
        return None
    ms = parse_rfc3339_ms(value)
    if ms is None:
        raise AnalyzerError(f"INVALID_{name.upper()}_RFC3339")
    return ms


HUMAN_IDENTITY_ARGS = {"review": "reviewer", "report-event": "actor", "resolve": "actor"}


def _require_human_identity(args):
    """Audit history needs a real Human identity: reject blank/whitespace-only values before
    anything (config, store) is opened. The value itself is stored unchanged."""
    name = HUMAN_IDENTITY_ARGS.get(args.command)
    if name is not None:
        value = getattr(args, name)
        if not isinstance(value, str) or not value.strip():
            raise AnalyzerError(f"BLANK_HUMAN_{name.upper()}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="technocore-analyzer",
                                     description="Read-only Technocore Evidence Analyzer (no external writes)")
    parser.add_argument("--config", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="one finite ingest+analysis run")
    r.add_argument("--as-of", help="RFC3339 as-of time for historical replay (default: now)")
    r.add_argument("--reference-time", help="RFC3339 data reference time (default: as-of)")
    sub.add_parser("runs", help="list runs")
    f = sub.add_parser("findings", help="list findings (latest revision)")
    f.add_argument("--category")
    f.add_argument("--unreviewed", action="store_true")
    s = sub.add_parser("show", help="one finding with revision / review / report / resolution history")
    s.add_argument("finding_id")
    v = sub.add_parser("review", help="HUMAN ONLY: record a review decision")
    v.add_argument("finding_id")
    v.add_argument("--decision", required=True, choices=["CONFIRMED", "REJECTED", "UNREVIEWED"])
    v.add_argument("--reviewer", required=True)
    v.add_argument("--revision", required=True, type=int, help="the finding revision that was reviewed")
    v.add_argument("--note")
    e = sub.add_parser("report-event", help="HUMAN ONLY: record what happened to an external report")
    e.add_argument("finding_id")
    e.add_argument("--state", required=True, choices=["NOT_SUBMITTED", "SUBMITTED", "DELIVERY_UNKNOWN", "ACKNOWLEDGED"])
    e.add_argument("--actor", required=True)
    e.add_argument("--revision", required=True, type=int, help="the finding revision the candidate was made for")
    e.add_argument("--channel")
    e.add_argument("--candidate-sha256", help="exact candidate digest (required for any state except NOT_SUBMITTED)")
    e.add_argument("--source-ref", help="reference to the official response, when recording one")
    e.add_argument("--detail")
    z = sub.add_parser("resolve", help="HUMAN ONLY: record a resolution with its source")
    z.add_argument("finding_id")
    z.add_argument("--resolution", required=True,
                   choices=["OPEN", "FIXED", "FALSE_POSITIVE", "DUPLICATE", "NOT_A_BUG", "UNKNOWN"])
    z.add_argument("--actor", required=True)
    z.add_argument("--revision", required=True, type=int, help="the finding revision this resolution is about")
    z.add_argument("--source-ref")
    z.add_argument("--note")
    sub.add_parser("llm-preflight", help="show whether the configured LLM runtime may run (never runs it)")
    sub.add_parser("rebuild", help="drop rebuildable caches (preserved history is kept)")
    args = parser.parse_args(argv)
    try:
        _require_human_identity(args)
        cfg = config_mod.load(args.config)
        if args.command == "run":
            as_of = _time(args.as_of, "as_of")
            _print(engine.run(cfg, reference_time_ms=_time(args.reference_time, "reference_time"), as_of_ms=as_of))
            return 0
        if args.command == "llm-preflight":
            _print(semantic.runtime_for(cfg["llm"]).preflight())
            return 0
        store = Store(cfg["state_db"])
        try:
            if args.command == "runs":
                _print([dict(r) for r in store.conn.execute(
                    "SELECT run_id,status,mode,started_at,finished_at,error,summary FROM runs ORDER BY started_at")])
            elif args.command == "findings":
                items = store.findings()
                if args.category:
                    items = [i for i in items if i["category"] == args.category]
                if args.unreviewed:
                    items = [i for i in items if i["management"]["human_review"]["decision"]
                             in ("UNREVIEWED", "STALE_RE_REVIEW_REQUIRED")]
                _print([{k: i[k] for k in ("finding_id", "category", "severity", "claim", "revision",
                                           "last_change", "lifecycle", "management")} for i in items])
            elif args.command == "show":
                match = [i for i in store.findings() if i["finding_id"] == args.finding_id]
                if not match:
                    raise AnalyzerError("UNKNOWN_FINDING")
                _print({"finding": match[0], "history": store.finding_history(args.finding_id)})
            elif args.command == "review":
                with store.transaction():
                    store.add_review(args.finding_id, args.decision, args.note, args.reviewer, args.revision)
                _print({"recorded": True, "origin": "HUMAN_CLI"})
            elif args.command == "report-event":
                with store.transaction():
                    store.add_report_event(args.finding_id, args.state, args.channel, args.detail,
                                           args.source_ref, args.actor, args.candidate_sha256, args.revision)
                _print({"recorded": True, "origin": "HUMAN_CLI",
                        "note": "recording only; Analyzer sent nothing"})
            elif args.command == "resolve":
                with store.transaction():
                    store.add_resolution(args.finding_id, args.resolution, args.source_ref, args.note, args.actor,
                                         args.revision)
                _print({"recorded": True, "origin": "HUMAN_CLI"})
            elif args.command == "rebuild":
                store.acquire_run_lock()  # never drop caches under a live run (refused, not waited on)
                store.rebuild_derived()
                _print({"rebuilt": "derived caches dropped; run again to re-derive"})
        finally:
            store.close()
        return 0
    except (AnalyzerError, OSError, ValueError, KeyError) as exc:
        sys.stderr.write(json.dumps({"error": type(exc).__name__, "code": str(exc)[:200]}) + "\n")
        return 2
