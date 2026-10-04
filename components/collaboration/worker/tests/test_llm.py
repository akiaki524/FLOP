"""B0 offline gates: synthetic child only, no Codex binary or credentials."""

import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from support import REPO, cli, sample
from ccw.human import Human, initialize, locked
from ccw.llm import Broker, capture, schema_check
from ccw.model import Invalid, canonical, digest, handoff, read, write_new
from ccw.providers import FixtureProvider


class LLMTests(unittest.TestCase):
    def setUp(self):
        (REPO / ".local").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="test-b0-", dir=REPO / ".local")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "run"
        initialize(self.root)
        self.human = Human(self.root)
        self.addCleanup(self.human.db.close)
        self.task = self.human.stage(sample())["task_id"]
        self.broker = Broker(self.root)
        self.addCleanup(self.broker.db.close)

    def preview(self, **changes):
        settings = dict(provider="synthetic", model="offline-fixture-v1", max_attempts=2,
                        timeout=3, max_request_bytes=256000, max_response_bytes=128000,
                        max_total_request_bytes=512000, max_cost_microusd=0)
        settings.update(changes)
        return self.broker.preview(self.task, **settings)

    def approve(self, **changes):
        preview = self.preview(**changes)
        self.broker.approve(preview, preview["approval_digest"], 300)
        return preview

    def last(self):
        return self.broker.status(self.task)["attempts"][-1]

    def test_approval_required_before_any_process_or_workspace(self):
        with patch("ccw.llm.capture") as launch, self.assertRaises(Invalid):
            self.broker.run(self.task)
        launch.assert_not_called()
        self.assertFalse((self.root / "runner").exists())
        self.assertEqual(self.broker.db.execute("SELECT COUNT(*) FROM llm_attempts").fetchone()[0], 0)

    def test_all_terms_bound_and_no_implicit_model(self):
        original = self.preview()
        for changes in ({"model": "other-fixed-model"}, {"provider": "openai"}, {"timeout": 4},
                        {"max_attempts": 1}, {"max_request_bytes": 255000},
                        {"max_response_bytes": 127000}, {"max_total_request_bytes": 511000}):
            with self.subTest(changes=changes):
                self.assertNotEqual(original["approval_digest"], self.preview(**changes)["approval_digest"])
        with self.assertRaises(Invalid):
            self.preview(model="")
        with self.assertRaises(Invalid):
            self.broker.approve(original, "0" * 64, 300)
        self.approve()
        with self.assertRaises(Invalid):
            self.approve()

    def test_live_approval_still_cannot_launch(self):
        self.approve(provider="openai", model="human-must-select-model", max_cost_microusd=1000)
        with patch("ccw.llm.capture") as launch, self.assertRaisesRegex(Invalid, "Human live approval"):
            self.broker.run(self.task)
        launch.assert_not_called()
        self.assertEqual(self.broker.status(self.task)["attempts"], [])

    def test_synthetic_flow_into_worker_and_human_preview(self):
        preview = self.approve()
        result = self.broker.run(self.task)
        attempt = result["attempt_id"]
        self.assertTrue(self.broker.verify(attempt)["verified"])
        artifact = self.broker.artifact(attempt)
        self.assertEqual(artifact["evidence"]["request_digest"], preview["terms"]["request_digest"])
        self.assertEqual(artifact["evidence"]["provider"], "synthetic")
        self.assertIsNone(artifact["evidence"]["real_cli_version"])
        cli("worker", self.root, "intake", self.task)
        cli("worker", self.root, "import-llm", self.task, attempt)
        cli("worker", self.root, "export", self.task)
        self.human.preview(self.task)
        with self.assertRaises(Invalid):
            self.broker.run(self.task)
        with self.assertRaises(Invalid):
            self.broker.retry(self.task, attempt, preview["approval_digest"], True)

    def test_failed_attempt_consumed_requires_explicit_retry(self):
        preview = self.approve()
        with self.assertRaises(Invalid):
            self.broker.run(self.task, "failure")
        first = self.last()
        self.assertEqual(first["state"], "failed")
        with self.assertRaises(Invalid):
            self.broker.run(self.task)
        for attempt, confirm, stopped in (("0" * 32, preview["approval_digest"], True),
                                           (first["id"], "0" * 64, True),
                                           (first["id"], preview["approval_digest"], False)):
            with self.assertRaises(Invalid):
                self.broker.retry(self.task, attempt, confirm, stopped)
        self.broker.retry(self.task, first["id"], preview["approval_digest"], True)
        self.broker.run(self.task)
        self.assertEqual(len(self.broker.status(self.task)["attempts"]), 2)
        with self.assertRaises(Invalid):
            self.broker.run(self.task)

    def test_real_process_crash_durable_ledger_and_human_recovery(self):
        preview = self.approve()
        cli("human", self.root, "llm-run-synthetic", self.task, "--outcome", "crash-after-start", expected=75)
        first = self.last()
        self.assertEqual(first["state"], "started")
        self.assertFalse((self.root / "runner").exists())
        cli("human", self.root, "llm-run-synthetic", self.task, expected=2)
        cli("human", self.root, "llm-retry", self.task, "--previous-attempt", first["id"],
            "--confirm-sha256", preview["approval_digest"], "--confirm-stopped")
        self.assertEqual(self.broker.status(self.task)["attempts"][0]["state"], "interrupted")
        cli("human", self.root, "llm-run-synthetic", self.task)

    def test_attempt_committed_before_launcher_called(self):
        self.approve()
        def inspect(*args):
            db = sqlite3.connect(self.root / "human" / "llm.sqlite")
            try:
                self.assertEqual(db.execute("SELECT state FROM llm_attempts").fetchone()[0], "started")
                self.assertEqual(db.execute("SELECT consumed FROM llm_permits").fetchone()[0], 1)
            finally:
                db.close()
            raise OSError("offline startup fault")
        with patch("ccw.llm.capture", side_effect=inspect), self.assertRaises(OSError):
            self.broker.run(self.task)
        self.assertEqual(self.last()["state"], "failed")

    def test_timeout_kills_runner_and_fences_retry(self):
        self.approve(timeout=1)
        with self.assertRaisesRegex(Invalid, "timeout"):
            self.broker.run(self.task, "timeout")
        self.assertEqual(self.last()["state"], "failed")
        with self.assertRaises(Invalid):
            self.broker.run(self.task)

    def test_output_budget_and_schema_fail_closed(self):
        self.approve()
        with self.assertRaises(Invalid):
            self.broker.run(self.task, "oversized")
        first = self.last()["id"]
        approval = self.broker.status(self.task)["approval_digest"]
        self.broker.retry(self.task, first, approval, True)
        with self.assertRaises(Invalid):
            self.broker.run(self.task, "invalid")
        self.assertEqual(self.last()["state"], "failed")

    def test_total_budget_reserved_on_failure_no_refund(self):
        request_size = len(canonical(self.preview()["request"]))
        preview = self.approve(max_total_request_bytes=request_size)
        with self.assertRaises(Invalid):
            self.broker.run(self.task, "failure")
        with self.assertRaisesRegex(Invalid, "budget"):
            self.broker.retry(self.task, self.last()["id"], preview["approval_digest"], True)

    def test_expired_and_stopped_approval(self):
        self.approve()
        self.human.stop(True)
        with self.assertRaises(Invalid):
            self.broker.run(self.task)
        self.human.stop(False)
        with self.broker.db:
            self.broker.db.execute("UPDATE llm_approvals SET expires=0")
        with self.assertRaises(Invalid):
            self.broker.run(self.task)

    def test_provenance_tampering_even_with_recomputed_worker_hash(self):
        self.approve()
        result = self.broker.run(self.task)
        path = Path(result["artifact"])
        original = read(path)
        for field, changed in (("provider", "openai"), ("model", "forged"), ("cli_version", "999"),
                               ("task_digest", "0" * 64), ("request_digest", "1" * 64),
                               ("response_digest", "2" * 64), ("attempt_id", "0" * 32)):
            with self.subTest(field=field):
                value = copy.deepcopy(original)
                value["evidence"][field] = changed
                value["evidence_digest"] = digest(value["evidence"])
                path.write_bytes(canonical(value))
                with self.assertRaises(Invalid):
                    self.broker.verify(result["attempt_id"])
        path.write_bytes(canonical(original))
        self.broker.verify(result["attempt_id"])

    def test_worker_cannot_strip_provenance_or_replace_report(self):
        self.approve()
        attempt = self.broker.run(self.task)["attempt_id"]
        cli("worker", self.root, "intake", self.task)
        cli("worker", self.root, "import-llm", self.task, attempt)
        cli("worker", self.root, "export", self.task)
        path = self.root / "worker" / "outbox" / (self.task + ".json")
        original = read(path)
        stripped = copy.deepcopy(original)
        del stripped["llm"]
        path.write_bytes(canonical(stripped))
        with self.assertRaisesRegex(Invalid, "provenance"):
            self.human.preview(self.task)
        report = copy.deepcopy(original["review"])
        report["summary"] = "Worker forged a different valid report"
        changed = handoff(original["bundle"], report)
        changed["llm"] = copy.deepcopy(original["llm"])
        changed["llm"]["report"] = report
        changed["llm"]["evidence"]["report_digest"] = digest(report)
        changed["llm"]["evidence_digest"] = digest(changed["llm"]["evidence"])
        path.write_bytes(canonical(changed))
        with self.assertRaisesRegex(Invalid, "provenance"):
            self.human.preview(self.task)

    def test_contract_changes_or_frozen_tamper_rejected(self):
        self.approve()
        path = self.root / "human" / "tasks" / (self.task + ".json")
        value = read(path)
        value["request"] += "changed"
        path.write_bytes(canonical(value))
        with self.assertRaises(Invalid):
            self.broker.run(self.task)

    def test_config_schema_and_invocation_are_reviewable(self):
        request = self.preview()["request"]
        argv = request["candidate_argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "offline-fixture-v1")
        self.assertEqual(argv[argv.index("--output-schema") + 1], "schema.json")
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ignore-rules", argv)
        parsed = tomllib.loads("\n".join(k + " = " + v for k, v in request["config_overrides"].items()))
        self.assertEqual(parsed["web_search"], "disabled")
        self.assertFalse(any(parsed["features"].values()))
        self.assertEqual(parsed["mcp_servers"], {})
        self.assertEqual(parsed["hooks"], {})
        self.assertEqual(parsed["plugins"], {})
        self.assertFalse(parsed["permissions"]["b0"]["network"]["enabled"])
        self.assertEqual(parsed["default_permissions"], "b0")
        self.assertEqual(parsed["permissions"]["b0"]["filesystem"][":root"], "deny")
        self.assertNotIn("--sandbox", argv)
        self.assertNotIn("destination", request["prompt"])
        report = FixtureProvider().run(sample())
        report["provider"] = "codex-cli-offline-v1"
        schema_check(report)
        report["shell"] = "forbidden"
        with self.assertRaises(Invalid):
            schema_check(report)

    def test_lock_excludes_duplicate_human_attempt(self):
        self.approve()
        with locked(self.root):
            cli("human", self.root, "llm-run-synthetic", self.task, expected=2)
        self.assertEqual(self.broker.status(self.task)["attempts"], [])

    def test_runner_boundary_attack_probe(self):
        files = ("human/signer.sqlite", "human/llm.sqlite", "other-local/data",
                 "ordinary-home/.codex/config.toml", "ordinary-home/.codex/synthetic-auth.json",
                 "ordinary-home/.ssh/synthetic-key", "unrelated-repo/data",
                 "project/.codex/config.toml", "project/hooks.json", "project/.agents/plugins/plugin.json",
                 "worker/inbox/unapproved.json")
        for name in files:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("SYNTHETIC CANARY ONLY")
        workspace = self.root / "probe-workspace"
        workspace.mkdir()
        (workspace / "home").mkdir()
        (workspace / "codex-home").mkdir()
        write_new(workspace / "input.json", {"synthetic": True})
        (workspace / "escape").symlink_to(self.root / "human" / "dummy-key.json")
        os.link(self.root / "human" / "dummy-key.json", workspace / "hardlinked.json")
        for mode in ("runner", "runner-unavailable"):
            result = subprocess.run([sys.executable, "-I", "-B", "tests/isolation_probe.py", str(self.root), mode],
                                    cwd=REPO, env={"MALICIOUS_CONFIG": "synthetic-only"}, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            checks = json.loads(result.stdout)
            self.assertTrue(all(value is True for value in checks.values()))
            if mode == "runner":
                self.assertEqual(len(checks), 43)

    def test_workspace_and_db_symlinks_rejected(self):
        self.approve()
        (self.root / "runner").symlink_to(self.root / "worker")
        with self.assertRaises(Invalid):
            self.broker.run(self.task)
        self.assertEqual(self.last()["state"], "failed")
        # Existing Human stage directories are also checked before writing.
        (self.root / "worker" / "inbox").rename(self.root / "worker" / "old-inbox")
        (self.root / "worker" / "inbox").symlink_to(self.root / "worker" / "old-inbox")
        with self.assertRaises(Invalid):
            self.human.stage(sample("different"))

    def test_injected_home_config_hooks_and_plugins_stop_runner(self):
        self.approve(max_attempts=3)
        for ordinal, name in enumerate(("codex-home/config.toml", "home/hooks.json", ".agents"), 1):
            def poisoned(argv, workspace, timeout, limit):
                (workspace / name).write_text("SYNTHETIC ATTACK CONFIG")
                return capture(argv, workspace, timeout, limit)
            with patch("ccw.llm.capture", side_effect=poisoned), self.assertRaises(Invalid):
                self.broker.run(self.task)
            self.assertEqual(self.last()["state"], "failed")
            if ordinal < 3:
                self.broker.retry(self.task, self.last()["id"], self.broker.status(self.task)["approval_digest"], True)

    def test_response_ledger_corruption_detected(self):
        self.approve()
        attempt = self.broker.run(self.task)["attempt_id"]
        with self.broker.db:
            self.broker.db.execute("UPDATE llm_attempts SET response=? WHERE id=?", (b"{}", attempt))
        with self.assertRaisesRegex(Invalid, "integrity"):
            self.broker.verify(attempt)

    def test_request_limit_checked_before_approval(self):
        with self.assertRaisesRegex(Invalid, "limit"):
            self.preview(max_request_bytes=1)
        self.assertEqual(self.broker.db.execute("SELECT COUNT(*) FROM llm_approvals").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
