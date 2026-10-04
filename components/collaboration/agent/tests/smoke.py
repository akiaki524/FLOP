"""Offline CLI flow; preserves inspectable state, previews and summary in .local/."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="smoke-", dir=local))
    state = output / "state"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(ROOT / "src")}

    def cli(*args):
        proc = subprocess.run([sys.executable, "-B", "-m", "collaboration_agent", "--state", str(state), *args],
                              env=env, cwd=ROOT, text=True, capture_output=True, timeout=15)
        assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
        return json.loads(proc.stdout)

    cli("init")
    previews = []
    statuses = []
    for name in ("exact", "extract", "lines", "math", "unknown"):
        path = ROOT / "examples" / f"{name}.json"
        response = cli("run", str(path))
        expected = "UNKNOWN" if name == "unknown" else "COMPLETED"
        assert response["result"]["outcome"]["status"] == expected
        preview = cli("preview", f"example-{name}")
        assert preview["result_digest"] == response["result_digest"]
        assert preview["human_approval"] == "NOT_GRANTED"
        (output / f"preview-{name}.json").write_text(json.dumps(preview, indent=2, ensure_ascii=True) + "\n")
        previews.append(f"preview-{name}.json")
        statuses.append(expected)
    duplicate = cli("run", str(ROOT / "examples" / "math.json"))
    assert duplicate["duplicate"] is True
    malformed = output / "malformed.json"
    malformed.write_text('{"version":1,"version":2}')
    assert cli("run", str(malformed))["result"]["outcome"]["status"] == "HUMAN_REVIEW"
    dry_path = output / "dry-task.json"
    dry = json.loads((ROOT / "examples" / "math.json").read_text())
    dry["task_id"] = "dry-example"
    dry["evidence"][0]["locator"] = "fixture:dry-run"
    dry_path.write_text(json.dumps(dry))
    assert cli("run", str(dry_path), "--dry-run")["result"]["dry_run"] is True
    cli("family", "math.gcd_lcm", "suspend", "--reason", "smoke suspension")
    held = cli("run", str(dry_path))
    assert held["result"]["outcome"]["reason"] == "family_suspended"
    cli("family", "math.gcd_lcm", "resume", "--reason", "smoke review complete")
    report = cli("audit")
    assert report["runs"] == 5
    summary = {"status": "PASS", "statuses": statuses + ["HUMAN_REVIEW"],
               "duplicate_blocked": True, "dry_run_no_admission": True,
               "suspension_enforced": True, "external_writes": 0,
               "state": "state/agent.sqlite", "previews": previews, "audit": report}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"status": "PASS", "artifacts": str(output), "runs": report["runs"]}))


if __name__ == "__main__":
    main()
