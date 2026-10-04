"""Bounded Batch 2 material acquisition and network-free frozen replay.

Native task completion and human-selected literal inspections are separate metrics.
No solver output supplies ground truth; native semantic answers remain unscored.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from collaboration_agent.engine import Agent
from collaboration_agent.material_fetch import Fetcher, MAX_RUN_BYTES, MAX_RUN_FETCHES, allowed_url
from collaboration_agent.materials import Resolver, freeze, load_frozen, request, run_frozen
from collaboration_agent.model import check, digest, encode, sha
from collaboration_agent.state import Store


def save(path, data):
    raw = encode(data)
    with path.open("xb") as f:
        f.write(raw)
    return sha(raw)


def code_manifest():
    return {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in
            sorted((ROOT / "src/collaboration_agent").glob("*.py")) + [Path(__file__).resolve()]}


def seed(fetcher, source):
    """Carry forward acquisition budgets and bytes, never recapture old responses."""
    records = sorted(source.glob("response-*.json"), key=lambda p: int(p.stem.split("-")[1]))
    for i, path in enumerate(records, 1):
        result = json.loads(path.read_bytes())
        check(result["url"] == allowed_url(result["url"])[0], "INVALID_SEED")
        raw = (source / result["blob"]).read_bytes()
        check(sha(raw) == result["raw_sha256"] == result["stored_sha256"], "INVALID_SEED")
        check(len(raw) == result["raw_bytes"], "INVALID_SEED")
        target = fetcher.root / result["blob"]
        if not target.exists():
            target.write_bytes(raw)
        save(fetcher.root / f"response-{i}.json", result)
        save(fetcher.root / f"attempt-{i}.json", {"reused_from": str(path), "method": "GET",
                                                "url": result["url"], "at": result["fetched_at"]})
        fetcher.cache[result["url"]] = result
        fetcher.count += 1
        fetcher.total_bytes += len(raw)
    check(fetcher.count <= MAX_RUN_FETCHES and fetcher.total_bytes <= MAX_RUN_BYTES, "SEED_BUDGET_EXCEEDED")


def aggregate(rows):
    nodes = [n for r in rows for n in r["materials"] if not n["reference"].startswith("inline:")]
    missing = [r for r in rows if r["batch2_reason"] == "missing_job_material"]
    return {"public_tasks_total": len(rows),
            "material_references_detected": len(nodes),
            "successfully_resolved_references": sum(n["status"] == "RESOLVED" for n in nodes),
            "rejected_by_source_policy": sum(n["status"] in {"SOURCE_NOT_ALLOWED", "PRIVATE_SOURCE", "PRIVATE_ADDRESS"} for n in nodes),
            "missing_references": sum(n["status"] == "MISSING" for n in nodes),
            "missing_task_reference": sum("MISSING_TASK_REFERENCE" in r["errors"] for r in rows),
            "truncated_or_incomplete_references": sum(n["truncation"] == "SUSPECTED" for n in nodes),
            "fetch_failure": sum(n["status"] in {"FETCH_FAILURE", "TIMEOUT", "HTTP_FAILURE", "AUTH_OR_ACCESS_REQUIRED"} for n in nodes),
            "incomplete_tasks": sum(r["completeness"] != "COMPLETE_WITHIN_SCOPE" for r in rows),
            "agent_classifier_reached": sum(r["result"].get("agent_reached", False) for r in rows),
            "registered_solver_reached": sum(r["result"].get("solver_reached", False) for r in rows),
            "previously_material_missing": len(missing),
            "previously_missing_now_agent_reached": sum(r["result"].get("agent_reached", False) for r in missing),
            "previously_missing_now_registered_solver_reached": sum(r["result"].get("solver_reached", False) for r in missing),
            **{s: sum(r["result"]["status"] == s for r in rows) for s in ("COMPLETED", "HUMAN_REVIEW", "UNKNOWN")},
            # All 25 original tasks are outside the existing answer contract. Any completion
            # is a capability-boundary failure, never automatically judged semantically correct.
            "false_complete": sum(r["result"]["status"] == "COMPLETED" for r in rows),
            "answer_quality": "UNSCORED / HUMAN_EVAL_REQUIRED",
            "errors": dict(Counter(e for r in rows for e in r["errors"]))}


def acquire(args):
    manifest = json.loads((args.dataset / "manifest.json").read_bytes())
    dataset_raw = (args.dataset / f"{args.split}.json").read_bytes()
    check(sha(dataset_raw) == manifest[f"{args.split}_sha256"], "DATASET_DIGEST_MISMATCH")
    cases = [c for c in json.loads(dataset_raw)["cases"] if c["category"] == "native_task"]
    args.output.mkdir(parents=True, exist_ok=False)
    save(args.output / "implementation-lock.json", code_manifest())
    fetcher = Fetcher(args.output / "acquisition")
    if args.seed:
        seed(fetcher, args.seed)
    before_count, before_bytes = fetcher.count, fetcher.total_bytes
    frozen = []
    for case in cases:
        # stdlib JSON preserves arbitrarily large integer nonce values. Never parse via JS.
        frame = json.loads(case["task"]["evidence"][0]["text"][6:])
        context = frame["job"].get("context", "")
        req = request(case["id"], context, origin={**case["provenance"],
                      "dataset_sha256": sha(dataset_raw), "batch2_case_id": case["id"]})
        bundle = Resolver(fetcher).resolve(req)
        path = args.output / case["id"]
        bundle_hash = freeze(bundle, fetcher, path)
        frozen.append({"id": case["id"], "batch2_reason": case["ground_truth"]["reason"],
                       "bundle_sha256": bundle_hash, "classification":
                       "no_context" if not context else "preview_full_spec" if " | full spec:" in context else "direct_reference"})
    save(args.output / "manifest.json", {"split": args.split, "dataset_sha256": sha(dataset_raw),
         "at": datetime.now(timezone.utc).isoformat(), "cases": frozen,
         "source_status": "SOURCE_UNVERIFIED", "code": code_manifest(),
         "network": {"new_fetches": fetcher.count - before_count,
                     "new_bytes": fetcher.total_bytes - before_bytes, "cumulative_fetches": fetcher.count,
                     "cumulative_bytes": fetcher.total_bytes, "cache_hits": fetcher.cache_hits},
         "starting_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()})
    replay(args.output, args.output / "evaluation")


def replay(frozen, output):
    """No Fetcher constructed; inputs must be already frozen and hash-verified."""
    manifest = json.loads((frozen / "manifest.json").read_bytes())
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    with Store(output / "state", create=True, hour=10000, day=10000) as store:
        agent = Agent(store)
        for case in manifest["cases"]:
            path = frozen / case["id"]
            check(sha((path / "bundle.json").read_bytes()) == case["bundle_sha256"], "FROZEN_DIGEST_MISMATCH")
            bundle = load_frozen(path)
            result = run_frozen(path, agent)
            preview = agent.preview(case["id"]) if result.get("agent_reached") else {
                "human_approval": "NOT_GRANTED", "materials": bundle["materials"], "errors": bundle["errors"]}
            save(output / (case["id"] + "-preview.json"), preview)
            rows.append({**case, "completeness": bundle["completeness"], "errors": bundle["errors"],
                         "family": (bundle["spec"] or {}).get("family"),
                         "materials": [{k: n[k] for k in ("id", "reference", "status", "truncation")} for n in bundle["materials"]],
                         "result": result})
        audit = store.verify()
    summary = {"split": manifest["split"], "metrics": aggregate(rows), "network": manifest["network"],
               "replay_network_requests": 0, "external_writes": 0,
               "classification": dict(Counter(r["classification"] for r in rows)),
               "by_family": {f: aggregate([r for r in rows if (r["family"] or "unresolved") == f])
                             for f in sorted({r["family"] or "unresolved" for r in rows})},
               "results_sha256": save(output / "results.json", rows), "audit": audit}
    save(output / "summary.json", summary)
    print(json.dumps({"output": str(output), **summary["metrics"], "network": summary["network"]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    live = sub.add_parser("acquire", help="explicit bounded public GET; freezes once then evaluates offline")
    live.add_argument("--dataset", type=Path, required=True)
    live.add_argument("--split", choices=("development", "holdout"), required=True)
    live.add_argument("--seed", type=Path)
    live.add_argument("--output", type=Path, required=True)
    offline = sub.add_parser("replay", help="offline only; no network dependencies")
    offline.add_argument("--frozen", type=Path, required=True)
    offline.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "acquire":
        acquire(args)
    else:
        replay(args.frozen, args.output)
