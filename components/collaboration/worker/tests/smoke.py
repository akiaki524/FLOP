"""Synthetic Human actions in fresh offline roots; NEVER a production runner.

No external inputs, credentials, user tasks, arbitrary commands or live targets.
The receipts and approval records remain in .local/ for inspection.
"""

import json
from pathlib import Path
import sqlite3
import tempfile

from support import REPO, cli
from ccw.model import write_new


def run():
    (REPO / ".local").mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="smoke-", dir=REPO / ".local"))
    summary = {"mode": "OFFLINE_SYNTHETIC_HUMAN_ONLY", "real_llm": False, "real_send": False, "scenarios": []}
    for outcome in ("success", "lost-before", "lost-after", "crash-after"):
        root = output / outcome
        cli("human", root, "init")
        task = json.loads(cli("human", root, "stage", "examples/task.json").stdout)["task_id"]
        cli("worker", root, "intake", task)
        cli("worker", root, "review", task)
        key = json.loads(cli("worker", root, "export", task).stdout)["payload_sha256"]
        preview = json.loads(cli("human", root, "preview", task).stdout)
        assert preview["payload_sha256"] == key
        # Only this fixed test harness simulates a Human issuing the CLI command.
        cli("human", root, "approve", task, "--confirm-sha256", key)
        sent = cli("human", root, "send-mock", key, "--outcome", outcome, expected=75 if outcome == "crash-after" else 0)
        initial = "in_flight" if outcome == "crash-after" else json.loads(sent.stdout)["state"]
        cli("human", root, "send-mock", key, "--outcome", "success", expected=2)
        if outcome != "success":
            final = json.loads(cli("human", root, "reconcile-mock", key).stdout)["state"]
        else:
            final = initial
        assert final == ("unknown" if outcome == "lost-before" else "sent")
        db = sqlite3.connect(root / "human" / "mock-remote.sqlite")
        try:
            count = db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        finally:
            db.close()
        assert count == (0 if outcome == "lost-before" else 1)
        summary["scenarios"].append({"outcome": outcome, "initial": initial, "final": final,
                                     "deliveries": count, "resend_blocked": True,
                                     "task_id": task, "payload_sha256": key, "root": str(root)})
    root = output / "scout"
    cli("human", root, "init")
    task = json.loads(cli("human", root, "stage-scout", "examples/scout-request.json", "examples/scout-evidence.json", "--index", "0").stdout)["task_id"]
    cli("worker", root, "intake", task)
    cli("worker", root, "review", task)
    cli("worker", root, "export", task)
    cli("human", root, "preview", task)
    summary["scout"] = {"synthetic": True, "state": "previewed", "task_id": task, "root": str(root)}
    root = output / "batch-b0"
    cli("human", root, "init")
    task = json.loads(cli("human", root, "stage", "examples/task.json").stdout)["task_id"]
    cli("human", root, "llm-run-synthetic", task, expected=2)
    limits = ("--provider", "synthetic", "--model", "offline-fixture-v1", "--max-attempts", "2",
              "--timeout", "3", "--max-request-bytes", "256000", "--max-response-bytes", "128000",
              "--max-total-request-bytes", "512000", "--max-cost-microusd", "0")
    llm_preview = json.loads(cli("human", root, "llm-preview", task, *limits).stdout)
    # Only fixed synthetic inputs are automatically approved by this test harness.
    cli("human", root, "llm-approve", task, *limits, "--confirm-sha256", llm_preview["approval_digest"])
    cli("human", root, "llm-run-synthetic", task, "--outcome", "crash-after-start", expected=75)
    status = json.loads(cli("human", root, "llm-status", task).stdout)
    previous = status["attempts"][0]["id"]
    assert status["attempts"][0]["state"] == "started"
    cli("human", root, "llm-run-synthetic", task, expected=2)
    cli("human", root, "llm-retry", task, "--previous-attempt", previous,
        "--confirm-sha256", llm_preview["approval_digest"], "--confirm-stopped")
    attempt = json.loads(cli("human", root, "llm-run-synthetic", task).stdout)["attempt_id"]
    cli("human", root, "llm-run-synthetic", task, expected=2)
    cli("human", root, "llm-verify", attempt)
    cli("worker", root, "intake", task)
    cli("worker", root, "import-llm", task, attempt)
    cli("worker", root, "export", task)
    final_preview = json.loads(cli("human", root, "preview", task).stdout)
    assert final_preview["broker_provenance"]["provider"] == "synthetic"
    summary["batch_b0"] = {"root": str(root), "attempt_id": attempt, "interrupted_attempt": previous,
                           "task_id": task, "approval_digest": llm_preview["approval_digest"],
                           "no_approval_blocked": True, "duplicate_blocked": True,
                           "explicit_retry": True, "broker_provenance_verified": True}
    write_new(output / "summary.json", summary)
    print(json.dumps({"status": "PASS", "summary": str(output / "summary.json"),
                      "scenarios": 4, "scout_preview": True, "batch_b0": True, "real_llm": False, "real_send": False}))


if __name__ == "__main__":
    run()
