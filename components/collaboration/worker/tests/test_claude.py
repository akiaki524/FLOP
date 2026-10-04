"""B1 behavioral tests, synthetic approval/auth/material only."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from support import REPO, cli, sample
from ccw import claude
from ccw.human import Human, initialize, locked
from ccw.llm import Broker, capture
from ccw.model import Invalid, canonical, digest, read


def limits(**changes):
    result = dict(provider="claude-fixture", model=claude.MODEL, effort=claude.EFFORT,
        max_attempts=1, timeout=3, max_request_bytes=256000, max_response_bytes=128000,
        max_total_request_bytes=256000, max_cost_microusd=0, ttl=300)
    result.update(changes)
    return result


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="test-b1-", dir=REPO / ".local"))
        # Retain B1 evidence; never remove prior .local data.
        self.root = self.base / "run"
        initialize(self.root)
        self.human = Human(self.root)
        self.broker = Broker(self.root)
        self.addCleanup(self.human.db.close)
        self.addCleanup(self.broker.db.close)
        self.task = self.stage()

    def stage(self, case="success"):
        value = sample()
        value["request"] += " B1_FIXTURE:" + case
        return self.human.stage(value)["task_id"]

    def approve(self, task=None, **changes):
        task = task or self.task
        preview = self.broker.preview(task, **limits(**changes))
        self.broker.approve(preview, preview["approval_digest"], preview["terms"]["ttl"])
        return preview

    def test_full_launcher_broker_worker_preview_and_tampering(self):
        preview = self.approve()
        self.assertNotIn("destination", preview["request"]["prompt"])
        result = self.broker.run_claude(self.task)
        attempt = result["attempt_id"]
        artifact = self.broker.artifact(attempt)
        self.assertEqual(artifact["evidence"]["launcher_starts"], 1)
        cli("worker", self.root, "intake", self.task)
        cli("worker", self.root, "import-llm", self.task, attempt)
        cli("worker", self.root, "export", self.task)
        self.assertEqual(self.human.preview(self.task)["broker_provenance"], artifact["evidence"])
        path = Path(result["artifact"])
        modified = copy.deepcopy(artifact)
        modified["evidence"]["measurements"]["usage"]["input_tokens"]["value"] = 999
        modified["evidence_digest"] = digest(modified["evidence"])
        path.write_bytes(canonical(modified))
        with self.assertRaises(Invalid):
            self.broker.verify(attempt)
        out = self.root / "worker" / "outbox" / (self.task + ".json")
        value = read(out)
        del value["llm"]
        out.write_bytes(canonical(value))
        with self.assertRaises(Invalid):
            self.human.preview(self.task)

    def test_approval_required_expiry_contract_ttl_and_single_launch(self):
        with patch("ccw.claude.launch") as launch:
            with self.assertRaises(Invalid):
                self.broker.run_claude(self.task)
            launch.assert_not_called()
        preview = self.broker.preview(self.task, **limits())
        self.assertNotEqual(preview["approval_digest"], self.broker.preview(self.task, **limits(ttl=301))["approval_digest"])
        with self.assertRaises(Invalid):
            self.broker.approve(preview, preview["approval_digest"], 301)
        self.broker.approve(preview, preview["approval_digest"], 300)
        with patch("ccw.claude.code_manifest", return_value={"changed": "1"}), self.assertRaises(Invalid):
            self.broker.run_claude(self.task)
        with patch("ccw.llm.time.time", return_value=preview["terms"]["expires"]), self.assertRaises(Invalid):
            self.broker.run_claude(self.task)
        self.assertFalse((self.root / "runner").exists())
        self.broker.run_claude(self.task)
        with self.assertRaises(Invalid):
            self.broker.run_claude(self.task)
        with self.assertRaises(Invalid):
            self.broker.retry(self.task, self.broker.status(self.task)["attempts"][0]["id"], preview["approval_digest"], True)

    def test_ledger_recreation_and_root_reuse_reject_old_approval(self):
        preview = self.approve()
        self.broker.db.close()
        # Preserve old DB; simulate loss by renaming, no deletion/reset.
        (self.root / "human" / "llm.sqlite").rename(self.root / "human" / "llm.saved.sqlite")
        fresh = Broker(self.root)
        self.addCleanup(fresh.db.close)
        with self.assertRaises(Invalid):
            fresh.approve(preview, preview["approval_digest"], 300)
        other = self.base / "other"
        initialize(other)
        broker = Broker(other)
        self.addCleanup(broker.db.close)
        with self.assertRaises(Invalid):
            broker.approve(preview, preview["approval_digest"], 300)

    def test_prelaunch_failure_and_durable_attempt(self):
        self.approve()
        def fail(*args):
            with sqlite3.connect(self.root / "human" / "llm.sqlite") as db:
                self.assertEqual(db.execute("SELECT state FROM llm_attempts").fetchone()[0], "started")
                self.assertEqual(db.execute("SELECT consumed FROM llm_permits").fetchone()[0], 1)
            raise OSError("fixed prelaunch fault")
        with patch("ccw.claude.launch", side_effect=fail), self.assertRaises(OSError):
            self.broker.run_claude(self.task)
        self.assertEqual(self.broker.status(self.task)["attempts"][0]["state"], "not_started")
        with self.assertRaises(Invalid):
            self.broker.run_claude(self.task)

    def test_fault_results_no_fallback_no_implicit_retry(self):
        for case in ("quota", "bad-quote", "invalid", "model-mismatch", "model-missing", "timeout", "crash", "oversized"):
            with self.subTest(case=case):
                task = self.stage(case)
                self.approve(task, timeout=1)
                with self.assertRaises(Invalid):
                    self.broker.run_claude(task)
                attempt = self.broker.status(task)["attempts"][0]
                self.assertEqual(attempt["state"], "unknown" if case in ("timeout", "crash", "oversized") else "rejected")
                self.assertIsNone(attempt["observations"]["usage"])
                with self.assertRaises(Invalid):
                    self.broker.run_claude(task)

    def test_usage_missing_is_null_session_is_not_request_id(self):
        task = self.stage("usage-missing")
        self.approve(task)
        attempt = self.broker.run_claude(task)["attempt_id"]
        measurements = self.broker.artifact(attempt)["evidence"]["measurements"]
        self.assertIsNone(measurements["provider_request_ids"])
        self.assertIsNone(measurements["usage"]["input_tokens"]["value"])
        self.assertIsNone(measurements["subscription_actual_charge"])
        self.assertIsNone(measurements["internal_retries"])

    def test_auth_fixture_rejects_non_subscription_extra_unknown_and_secrets(self):
        good = claude.fixture_auth()
        for key, value in (("method", "api-key"), ("provider", "gateway"), ("billing", None),
                           ("extra_usage", "enabled"), ("logged_in", False), ("api_key", "SYNTHETIC_CANARY")):
            modified = dict(good, **{key: value})
            with self.assertRaises(Invalid):
                claude.auth_status(lambda: canonical(modified))
        with self.assertRaises(Invalid):
            self.broker.preview(self.task, **limits(model="fable"))
        with self.assertRaises(Invalid):
            self.broker.preview(self.task, **limits(effort="real-high"))

    def test_exec_boundary_and_auth_acquisition_with_fake_executable(self):
        workspace = self.base / "probe"
        workspace.mkdir()
        claude.prepare(workspace)
        canaries = []
        for name in ("human", "signer", "llm-db", "ordinary-home", "other-repository", "managed-settings"):
            path = self.base / name
            path.write_text("public synthetic canary")
            canaries.append(str(path))
        spec = claude.target()
        command = [sys.executable, "-I", "-S", "-B", str(REPO / "src/ccw/claude_launcher.py"),
                   str(workspace), str(os.getpid()), spec["binary"], spec["binary_digest"],
                   spec["entrypoint"], spec["entrypoint_digest"], "-I", "-S", "-B", spec["entrypoint"]]
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "SYNTHETIC_CANARY", "HTTPS_PROXY": "not-a-real-proxy",
                                     "WSL_INTEROP": "synthetic", "CLAUDE_CODE_USE_BEDROCK": "1"}):
            raw = capture(command, workspace, 3, 128000, input_bytes=canonical({"probe": canaries}))
        results = json.loads(raw)
        (self.base / "probe-results.json").write_bytes(canonical(results))
        self.assertTrue(all(results.values()), results)
        raw = capture(command + ["auth", "status"], workspace, 3, 128000, input_bytes=b"")
        self.assertEqual(claude.auth_status(lambda: raw), claude.fixture_auth())

    def test_home_contamination_and_unavailable_isolation_fail_closed(self):
        preview = self.approve()
        workspace = self.base / "contamination"
        workspace.mkdir()
        claude.prepare(workspace)
        (workspace / "claude-home" / "settings.json").write_text('{"apiKeyHelper":"forbidden"}')
        with patch("ccw.llm.capture") as capture_call, self.assertRaises(Invalid):
            claude.launch(preview["request"], workspace, 1, 10000, lambda: False, lambda _: None)
        capture_call.assert_not_called()
        from ccw.claude_launcher import confine
        from ccw.isolation import IsolationError
        with patch("ccw.claude_launcher.iso.abi", side_effect=IsolationError("not available")):
            with self.assertRaises(IsolationError):
                confine(workspace, Path(sys.executable), None, os.getppid())

    def test_stop_and_status_during_locked_run(self):
        task = self.stage("closed-output")
        self.approve(task, timeout=5)
        process = subprocess.Popen([sys.executable, "-B", "-m", "ccw.human", "--root", str(self.root),
            "llm-run-claude-fixture", task], env={"PYTHONPATH": str(REPO / "src")},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if self.broker.db.execute("SELECT 1 FROM llm_events WHERE event='launcher-process-started'").fetchone():
                    break
                time.sleep(0.02)
            else:
                self.fail("launcher did not start")
            cli("human", self.root, "llm-status", task)
            cli("human", self.root, "llm-stop")
            process.communicate(timeout=3)
            self.assertEqual(process.returncode, 2)
            self.assertEqual(self.broker.status(task)["attempts"][0]["state"], "unknown")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_broker_kill_preserves_started_and_denies_new_permit(self):
        task = self.stage("timeout")
        preview = self.approve(task, timeout=5)
        process = subprocess.Popen([sys.executable, "-B", "-m", "ccw.human", "--root", str(self.root),
            "llm-run-claude-fixture", task], env={"PYTHONPATH": str(REPO / "src")},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if self.broker.db.execute("SELECT 1 FROM llm_events WHERE event='launcher-process-started'").fetchone():
                    break
                time.sleep(0.02)
            else:
                self.fail("launcher did not start")
            process.kill()
            # Parent-death signal (or pre-exec parent check) closes child pipes.
            process.communicate(timeout=3)
            self.assertEqual(self.broker.status(task)["attempts"][0]["state"], "started")
            with self.assertRaises(Invalid):
                self.broker.run_claude(task)
            with self.assertRaises(Invalid):
                self.broker.retry(task, self.broker.status(task)["attempts"][0]["id"], preview["approval_digest"], True)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
