"""B3 synthetic result coverage: no Real launch or live-gate bypass."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_claude_real as b2
from support import REPO
from ccw import claude, claude_real as real
from ccw.model import Invalid, canonical, decode

MODEL = b2.MODEL


def no_subagents():
    return {"spawned": 0, "requested": {"background": 0, "foreground": 0, "unset": 0},
        "started_in_background": 0, "by_type": {}, "max_depth": 0, "spawned_by_subagents": 0,
        "completed": 0, "failed": 0, "killed": {"parent": 0, "user": 0, "system": 0},
        "refused": {"depth_limit": 0, "concurrency_limit": 0, "budget": 0}}


class B3Tests(unittest.TestCase):
    setUp = b2.RealTests.setUp
    result = b2.RealTests.result
    preview = b2.RealTests.preview

    def parse(self, data):
        return real.parse(canonical(data), self.broker.snapshot(self.task), {"model": MODEL})

    def test_reviewed_optional_fields_and_all_cost_bases(self):
        data = self.result()
        data.update(ttft_ms=10, fast_mode_state="off", fast_mode_disabled_reason="extra_usage_disabled",
                    queued_turn_count=0, result_index=0, subagent_stats=no_subagents(),
                    ttft_stream_ms=11, time_to_request_ms=1, request_sent_wall_ms=1.5,
                    first_content_frame_ms=10, first_stream_post_ms=12, first_stream_post_ack_ms=13,
                    first_stream_post_wall_ms=2.5, time_to_request_from_spawn_ms=2,
                    time_origin_ms=1.1, warm_spare_claimed=False, user_message_uuid="synthetic",
                    user_message_uuids=["synthetic"])
        for basis in ("list", "managed", "unknown"):
            data["modelUsage"][MODEL]["costBasis"] = basis
            _, metrics = self.parse(data)
            self.assertEqual(metrics["cost_basis"][MODEL], basis)
            self.assertIsNone(metrics["subscription_actual_charge"])

    def test_auxiliary_models_and_pricing_canonical_are_not_fallback(self):
        data = self.result()
        data["modelUsage"][MODEL].update(canonicalModel="claude-sonnet-4-6-20260101", provider="firstParty")
        for auxiliary in real.AUXILIARY_MODELS:
            data["modelUsage"][auxiliary] = {"inputTokens": 3, "outputTokens": 1, "costBasis": "list"}
        _, metrics = self.parse(data)
        self.assertEqual(metrics["model_usage"], data["modelUsage"])
        self.assertEqual(metrics["auxiliary_usage_keys"], sorted(real.AUXILIARY_MODELS))
        self.assertFalse(metrics["automatic_model_fallback_authorized"])
        self.assertFalse(metrics["model_roles_verified"])
        self.assertIsNone(metrics["reported_primary_model"])
        del data["modelUsage"][MODEL]
        with self.assertRaisesRegex(Invalid, "model missing/mismatched"):
            self.parse(data)
        data["modelUsage"][MODEL] = {}
        data["modelUsage"]["claude-opus-4-6"] = {}
        with self.assertRaises(Invalid):
            self.parse(data)

    def test_missing_usage_and_model_counters_stay_unknown(self):
        data = self.result()
        data["modelUsage"] = {MODEL: {}}
        for missing in (None, {}):
            data["usage"] = missing
            data.pop("total_cost_usd", None)
            _, metrics = self.parse(data)
            self.assertTrue(all(item["value"] is None for item in metrics["usage"].values()))
            self.assertEqual(metrics["model_usage"], {MODEL: {}})
            self.assertIsNone(metrics["cost_basis"][MODEL])
            self.assertIsNone(metrics["api_equivalent_cost"]["value"])

    def test_structured_output_turns_do_not_imply_external_tools(self):
        data = self.result()
        data.update(num_turns=2, stop_reason="tool_use")
        report, metrics = self.parse(data)
        self.assertEqual(report, data["structured_output"])
        self.assertEqual(metrics["cli_turns"], 2)
        self.assertIsNone(metrics["provider_requests"])
        data.pop("structured_output")
        with self.assertRaises(Invalid):
            self.parse(data)
        args = real.argv(self.spec, MODEL, "high")
        self.assertEqual(args[args.index("--max-turns") + 1], "1")
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("only StructuredOutput", args[args.index("--system-prompt") + 1])
        self.assertNotIn("--fallback-model", args)

    def test_unknown_types_activity_and_errors_fail_closed(self):
        mutations = [dict(ttft_ms=True), dict(ttft_ms=-1), dict(ttft_ms=1.5), dict(ttft_ms=None),
            dict(duration_ms=1.5), dict(duration_api_ms=True),
            dict(fast_mode_state="on"), dict(fast_mode_state="cooldown"),
            dict(fast_mode_disabled_reason=[]), dict(fast_mode_disabled_reason="new"),
            dict(queued_turn_count=True), dict(queued_turn_count=1), dict(result_index=1),
            dict(result_index=-1), dict(api_error_code="rate_limit"), dict(api_error_code=5),
            dict(future_metadata=0), dict(num_turns=0), dict(num_turns=True), dict(subagent_stats={}),
            dict(api_error_status=429), dict(is_error=True), dict(permission_denials=[{"tool_name":"Bash"}])]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(Invalid):
                self.parse(dict(self.result(), **mutation))
        for field in ("spawned", "max_depth"):
            data = self.result()
            data["subagent_stats"] = no_subagents()
            data["subagent_stats"][field] = 1
            with self.assertRaises(Invalid):
                self.parse(data)
        for mutation in ({"costBasis": "free"}, {"costBasis": []}, {"canonicalModel": True},
                         {"unknown": 0}, {"inputTokens": True}, {"webSearchRequests": 1}):
            data = self.result()
            data["modelUsage"][real.AUXILIARY_MODELS[0]] = mutation
            with self.assertRaises(Invalid):
                self.parse(data)
        for subtype in ("error_max_turns", "error_max_budget_usd", "error_during_execution",
                        "error_max_structured_output_retries"):
            with self.assertRaisesRegex(Invalid, subtype):
                self.parse(dict(self.result(), subtype=subtype, is_error=True, errors=["PRIVATE_ERROR"]))

    def test_isolated_parser_returns_safe_rejection_reason(self):
        for index, (mutation, reason) in enumerate(((dict(extra="PRIVATE_RESPONSE"), "unknown Real result field"),
                (dict(subtype="error_max_turns", is_error=True, errors=["PRIVATE_ERROR"]), "error_max_turns"),
                (dict(structured_output={"PRIVATE_KEY": "PRIVATE_VALUE"}), "invalid JSON/schema/quote or parser failure"))):
            workspace = self.base / ("rejection-" + str(index))
            workspace.mkdir(mode=0o700)
            with self.assertRaises(real.ParseRejected) as caught:
                real.parse_isolated(canonical(dict(self.result(), **mutation)),
                                    self.broker.snapshot(self.task), {"model": MODEL}, workspace)
            self.assertEqual(caught.exception.reason, reason)

    def test_shared_ledger_preserves_rejected_raw_and_consumed_permit(self):
        preview = self.broker.preview(self.task, provider="claude-fixture", model=claude.MODEL,
            effort=claude.EFFORT, max_attempts=1, timeout=5, max_request_bytes=256000,
            max_response_bytes=128000, max_total_request_bytes=256000, max_cost_microusd=0, ttl=300)
        self.broker.approve(preview, preview["approval_digest"], 300)
        with patch.object(claude, "parse", side_effect=real.ParseRejected("unknown Real result field")):
            with self.assertRaises(real.ParseRejected):
                self.broker.run_claude(self.task)  # Only the B1 Fake executable.
        row = self.broker.db.execute("SELECT * FROM llm_attempts WHERE task=?", (self.task,)).fetchone()
        self.assertEqual(row["state"], "rejected")
        self.assertTrue(row["response"])
        evidence = decode(row["evidence"])
        self.assertEqual(evidence["parser_rejection"], "unknown Real result field")
        self.assertFalse(evidence["automatic_retry"])
        with self.assertRaises(Invalid):
            self.broker.run_claude(self.task)
        with self.assertRaises(Invalid):
            self.broker.retry(self.task, row["id"], preview["approval_digest"], True)
        self.assertEqual(self.broker.db.execute("SELECT COUNT(*) FROM llm_attempts").fetchone()[0], 1)

    def test_auth_inventory_failure_preserves_safe_evidence(self):
        before = real.inventory(self.auth)
        unknown = self.auth / "new-login-file"
        unknown.write_text("SYNTHETIC_ONLY")
        unknown.chmod(0o600)
        result = real.post_inventory(before)
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["diff"]["added"], ["new-login-file"])
        os.link(unknown, self.base / "synthetic-hardlink")
        result = real.post_inventory(before)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["before"], before)
        self.assertIsNone(result["after"])
        self.assertNotIn("SYNTHETIC_ONLY", canonical(result).decode())

    def test_model_id_syntax_and_both_live_gates_remain_closed(self):
        with patch.object(real, "target", return_value=self.spec), patch.object(real, "launch") as launch:
            for model in ("claude-sonnet-4-6", "claude-opus-4.7", "claude-opus-5", "claude-3-7-sonnet-20250219"):
                self.assertEqual(self.preview(model=model)["request"]["model"], model)
            for model in ("sonnet", "claude-opus-latest", "claude-opus-4-6[1m]", "claude-opus-4-6 --help"):
                with self.assertRaises(Invalid):
                    self.preview(model=model)
            preview = self.preview()
            self.broker.approve(preview, preview["approval_digest"], 300)
            with self.assertRaisesRegex(Invalid, "DISABLED"):
                self.broker.run_claude(self.task)
            self.assertFalse(preview["terms"]["real_authorized"])
            self.assertFalse(preview["request"]["egress"]["human_accepted"])
            self.assertEqual(self.broker.status(self.task)["attempts"], [])
            launch.assert_not_called()
        process = subprocess.run([sys.executable, "-I", "-S", "-B",
            str(REPO / "src/ccw/claude_real_launcher.py"), "online",
            "/nonexistent", "0", "/nonexistent", "synthetic", "/nonexistent", MODEL, "high"],
            env={}, capture_output=True)
        self.assertEqual(process.returncode, 2)
        self.assertIn(b"DISABLED: B3", process.stderr)

    def test_runtime_preparation_rejects_unreviewed_binary_before_diagnostics(self):
        home = self.base / "synthetic-install-home"
        installed = home / ".local/bin"
        versions = home / ".local/share/claude/versions"
        installed.mkdir(parents=True)
        versions.mkdir(parents=True)
        substitute = versions / "unreviewed"
        substitute.write_bytes(Path("/usr/bin/true").read_bytes())
        (installed / "claude").symlink_to(substitute)
        with patch.object(Path, "home", return_value=home), patch.object(real, "probe") as probe:
            with self.assertRaisesRegex(Invalid, "sealed binary digest mismatch"):
                real.prepare_runtime(self.root)
            probe.assert_not_called()

    def test_b4_empty_sessions_only_and_known_artifact_types(self):
        before = real.inventory(self.auth)
        sessions = self.auth / "sessions"
        sessions.mkdir(mode=0o700)
        after = real.validate_auth_inventory(real.inventory(self.auth))
        self.assertEqual(real.post_inventory(before)["status"], "observed")
        with patch.object(real, "target", return_value=self.spec):
            self.assertIn("sessions", self.preview()["request"]["auth"]["inventory_before"]["entries"])
        pid = sessions / "123.json"
        pid.write_text('{"pid":123,"cwd":"SYNTHETIC"}')
        pid.chmod(0o600)
        with self.assertRaisesRegex(Invalid, "unexpected auth/config"):
            real.validate_auth_inventory(real.inventory(self.auth))
        self.assertEqual(real.post_inventory(after)["status"], "review_required")
        # Metadata never trusts a legitimate-looking filename as proof of contents.
        key = sessions / ("123." + "a" * 64 + ".key")
        key.write_text("SYNTHETIC_PEER_KEY")
        key.chmod(0o600)
        self.assertEqual(real.post_inventory(after)["status"], "review_required")
        self.assertNotIn("SYNTHETIC_PEER_KEY", canonical(real.inventory(self.auth)).decode())
        for name, kind, mode in (("sessions", "file", 0o600),
                                  ("sessions", "directory", 0o500),
                                  (".credentials.json", "directory", 0o700),
                                  (".claude.json", "file", 0o700)):
            with self.subTest(name=name, kind=kind, mode=mode), self.assertRaises(Invalid):
                real.validate_auth_inventory({"entries": {name: {"kind": kind, "mode": mode}}})

    def test_b4_inventory_replacement_unknown_changes_and_links(self):
        sessions = self.auth / "sessions"
        sessions.mkdir(mode=0o700)
        before = real.inventory(self.auth)
        sessions.rename(self.auth / "retained-sessions")
        sessions.mkdir(mode=0o700)
        self.assertEqual(real.post_inventory(before)["status"], "review_required")
        # New unknown entries are never accepted even if already present before.
        unknown_before = real.inventory(self.auth)
        self.assertEqual(real.post_inventory(unknown_before)["status"], "review_required")
        (sessions / "link").symlink_to(self.base)
        self.assertEqual(real.post_inventory(before)["status"], "invalid")

    def test_b4_login_plan_has_no_authorization_or_side_effects(self):
        with patch.object(real, "target", return_value=self.spec):
            plan = real.login_plan(self.root)
            self.assertFalse(plan["login_authorized"] or plan["inference_authorized"])
            self.assertEqual(plan["argv"][-3:], ["auth", "login", "--claudeai"])
            self.assertFalse(Path(plan["workspace"]).exists())
            self.assertIn("RES_OPTIONS", plan["environment_policy"]["keys"])
            self.assertFalse(plan["environment_policy"]["values_recorded"])
            self.assertTrue(plan["blockers"])
            from ccw.human import llm_command, parser
            args = parser().parse_args(["--root", str(self.root), "llm-claude-login-plan"])
            self.assertEqual(llm_command(self.broker, args), plan)
            credential = self.auth / ".credentials.json"
            credential.write_text("SYNTHETIC_ONLY")
            credential.chmod(0o600)
            with self.assertRaisesRegex(Invalid, "credential-free"):
                real.login_plan(self.root)
        process = subprocess.run([sys.executable, "-I", "-S", "-B",
            str(REPO / "src/ccw/claude_real_launcher.py"), "login"], env={}, capture_output=True)
        self.assertEqual(process.returncode, 2)
        self.assertIn(b"DISABLED: dedicated login", process.stderr)

    def test_b4_resolver_environment_is_fixed_and_fixture_unchanged(self):
        with patch.dict(os.environ, {"RES_OPTIONS": "SYNTHETIC_OVERRIDE", "LOCALDOMAIN": "invalid",
                                     "HOSTALIASES": "invalid", "BUN_OPTIONS": "invalid"}):
            env = real.environment(self.base, self.auth)
            self.assertEqual(env["RES_OPTIONS"], "use-vc timeout:2 attempts:1")
            self.assertFalse(set(env) & {"LOCALDOMAIN", "HOSTALIASES", "BUN_OPTIONS"})
            self.assertNotIn("RES_OPTIONS", claude.environment(self.base))


if __name__ == "__main__":
    unittest.main()
