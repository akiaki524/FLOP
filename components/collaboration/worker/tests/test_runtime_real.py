"""Offline adapter/gate regressions. No Human permit authorizes a native run."""
import copy
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import REPO
from ccw import runtime_real as real
from ccw.model import canonical, decode, digest, Invalid
from ccw.runtime_interface import (ReviewRequest, RuntimeClient, RuntimeRefused, parse_request,
    receive_frame, send_frame, MAX_REQUEST_BYTES, MAX_RESULT_BYTES, task_digest)
from ccw.runtime_output import OutputRejected, validated_result
from ccw.secret_handoff import save_new
from test_runtime_service import launch, raw_call, prepare, REQUEST, CANARY

REPORT = {"provider": "claude-cli-real-v1", "summary": "Valid \U0001f680 result", "findings": [],
          "unverified": ["Synthetic"]}


def envelope(report=None):
    return {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
            "result": canonical(REPORT if report is None else report).decode(),
            "modelUsage": {real.MODEL: {"inputTokens": 1, "outputTokens": 1}},
            "stop_reason": "end_turn", "permission_denials": []}


class RealAdapterTests(unittest.TestCase):
    def setUp(self):
        self.area = Path(tempfile.mkdtemp(prefix="test-real-adapter-", dir=REPO / ".local"))
        self.request = parse_request(REQUEST.encode())

    def policy(self):
        root = self.area / "service"
        root.mkdir(mode=0o700)
        (root / "runtime").mkdir(mode=0o700)
        spec = real.contract(root, self.request, REPO / ".local/unavailable-pinned-binary", "a" * 32)
        policy = {"version": 2, "task_sha256": task_digest(self.request), "real": spec}
        save_new(root / "policy.json", policy)
        return root, policy

    def test_missing_permit_refuses_service_before_secret_or_native(self):
        root, policy = self.policy()
        ours, theirs = socket.socketpair()
        secret_out, secret_in = socket.socketpair()
        try:
            command = [sys.executable, "-I", "-S", "-B", str(REPO / "src/ccw/runtime_service.py"),
                       str(root), "--credential-fd", str(secret_in.fileno())]
            with subprocess.Popen(command, stdin=theirs, stdout=theirs, stderr=subprocess.PIPE,
                                  env={}, pass_fds=(secret_in.fileno(),)) as child:
                theirs.close()
                secret_in.close()
                ours.settimeout(3)
                send_frame(ours, REQUEST.encode(), MAX_REQUEST_BYTES)
                result = decode(receive_frame(ours, MAX_RESULT_BYTES))
                self.assertEqual(result["status"], "refused")
                self.assertEqual(child.wait(timeout=3), 2)
                self.assertEqual(child.stderr.read(), b"")
            self.assertFalse((root / "consumed.json").exists())
            self.assertEqual(list((root / "runtime").iterdir()), [])
        finally:
            for endpoint in (ours, theirs, secret_out, secret_in):
                endpoint.close()

    def test_launcher_has_independent_gate(self):
        root, _ = self.policy()
        process = subprocess.run([sys.executable, "-I", "-S", "-B",
            str(REPO / "src/ccw/runtime_real_launcher.py"), str(root), str(os.getpid())],
            env={}, capture_output=True, timeout=3)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(process.stdout + process.stderr, b"")
        self.assertEqual(list((root / "runtime").iterdir()), [])

    def test_contract_binds_request_code_and_limits(self):
        root, policy = self.policy()
        real.validate_policy(root, policy, self.request)
        for name, value in (("timeout_ms", 5000), ("api_retries", 1), ("binary_sha256", "b" * 64),
                            ("code", {}), ("argv", []), ("raw_bytes", 32768)):
            changed = copy.deepcopy(policy)
            changed["real"][name] = value
            with self.assertRaises(Invalid):
                real.validate_policy(root, changed, self.request)
        with self.assertRaises(Invalid):
            real.validate_policy(root, policy, parse_request(ReviewRequest("other", "other").encode()))

    def test_invalid_permits_and_agent_issuer_refused_without_launch(self):
        root, policy = self.policy()
        with self.assertRaises(Exception):
            real.authorize(root, digest(policy))  # no TTY / agent context
        self.assertFalse((root / "permit.json").exists())
        # Inert metadata checks only. No valid permit is ever executed in tests.
        for i, changes in enumerate(({"expires_at": 0}, {"contract_sha256": "0" * 64},
                                      {"human_confirmed": False})):
            folder = self.area / str(i)
            folder.mkdir(mode=0o700)
            local_policy = copy.deepcopy(policy)
            local_policy["real"]["root"] = str(folder)
            data = {"version": 1, "contract_sha256": digest(local_policy), "expires_at": int(time.time())+300,
                    "human_confirmed": True, **changes}
            save_new(folder / "permit.json", data)
            # Check expiration/confirmation separately from the release blocker.
            with patch.object(real, "LIVE_BLOCKERS", ()), self.assertRaisesRegex(Invalid, "Human permit missing"):
                real.validate_permit(folder, local_policy)

    def test_release_blocker_cannot_be_overridden_by_human_permit_or_environment(self):
        root, policy = self.policy()
        save_new(root / "permit.json", {"version": 1, "contract_sha256": digest(policy),
            "expires_at": int(time.time())+300, "human_confirmed": True})
        with patch.dict(os.environ, {"CCW_REAL_ALLOWED": "1"}), self.assertRaisesRegex(Invalid, "Real gate closed"):
            real.validate_permit(root, policy)
        with patch.object(real, "launch_guard"), self.assertRaisesRegex(Invalid, "Real gate closed"):
            real.authorize(root, digest(policy))
        self.assertFalse((root / "consumed.json").exists())

    def test_output_mapping_and_inert_preview(self):
        report = {**REPORT, "summary": "https://never-fetched.invalid Bash: true \u202e"}
        result = real.map_output(canonical(envelope(report)), self.request, protected=(CANARY,))
        self.assertFalse(result["offline"])
        self.assertEqual(result["report"], report)
        self.assertNotIn("modelUsage", result)

    def test_lone_surrogates_at_both_json_layers_are_rejected(self):
        for surrogate in ("\ud800", "\udfff"):
            for key in ("summary", "unverified"):
                report = copy.deepcopy(REPORT)
                report[key] = [surrogate] if key == "unverified" else surrogate
                with self.assertRaises(OutputRejected):
                    real.map_output(canonical(envelope(report)), self.request)
                report["provider"] = "claude-cli-offline-v1"
                with self.assertRaises(OutputRejected):
                    validated_result(canonical(report), self.request)
            data = envelope()
            data["usage"] = {"service_tier": surrogate}
            with self.assertRaises(OutputRejected):
                real.map_output(canonical(data), self.request)
        self.assertIn("\U0001f680", real.map_output(canonical(envelope()), self.request)["report"]["summary"])

    def test_malformed_secret_oversized_and_failed_cli_envelopes(self):
        cases = [b"{", b"\xff", b"{}" * (real.REAL_RAW_BYTES // 2 + 1),
                 b'{"type":"result","type":"result"}']
        for change in ({"unknown": True}, {"is_error": True}, {"subtype": "error_max_turns"},
                       {"api_error_status": 500}, {"structured_output": REPORT}, {"result": "```json\n{}\n```"},
                       {"permission_denials": [{"tool_name": "Bash"}]}, {"modelUsage": {"other": {}}}):
            cases.append(canonical({**envelope(), **change}))
        for raw in cases:
            with self.subTest(raw_length=len(raw)), self.assertRaises(OutputRejected):
                real.map_output(raw, self.request)
        for report in ({**REPORT, "summary": CANARY}, {**REPORT, "unverified": ["x" * 3000] * 20}):
            with self.assertRaises(OutputRejected):
                real.map_output(canonical(envelope(report)), self.request, protected=(CANARY,))
        escaped = canonical(envelope({**REPORT, "summary": CANARY})).replace(
            CANARY.encode(), b"".join(("\\\\u%04x" % b).encode() for b in CANARY.encode()))
        with self.assertRaises(OutputRejected):
            real.map_output(escaped, self.request, protected=(CANARY,))

    def test_real_launch_transport_is_separate_budget_and_no_retry(self):
        observation = {}
        with patch.object(real, "exchange", return_value=canonical(envelope())) as transport:
            real.launch(["fixed-launcher"], self.area, self.request, CANARY, observation, offline=True)
        self.assertEqual(transport.call_count, 1)
        kwargs = transport.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 60)
        self.assertEqual(kwargs["limit"], 262144)
        self.assertTrue(kwargs["await_ready"])
        value, prompt = kwargs["input_bytes"].split(b"\n", 1)
        self.assertEqual(value, CANARY.encode())
        self.assertNotIn(CANARY.encode(), prompt)
        with patch.object(real, "exchange", return_value=b"{") as transport:
            with self.assertRaises(OutputRejected):
                real.launch(["fixed"], self.area, self.request, CANARY, {})
            self.assertEqual(transport.call_count, 1)

    def test_fresh_cli_inventory_accepts_config_but_not_credentials_or_logs(self):
        from ccw import claude_real
        for name, expected in ((".claude.json", "observed"), (".credentials.json", "invalid"),
                               ("debug.log", "invalid"), (".claude.json.tmp", "invalid")):
            folder = self.area / name.removeprefix(".")
            folder.mkdir(mode=0o700)
            before = claude_real.inventory(folder)
            save_new(folder / name, {"synthetic": True})
            self.assertEqual(real.ephemeral_inventory_status(before), expected)

    def test_client_rejects_surrogate_even_from_compromised_peer(self):
        ours, theirs = socket.socketpair()
        def serve():
            with theirs:
                receive_frame(theirs, MAX_REQUEST_BYTES)
                report = {**REPORT, "summary": "\ud800"}
                send_frame(theirs, canonical({"version": 1, "status": "succeeded", "offline": False,
                    "task_sha256": task_digest(self.request), "report": report}), MAX_RESULT_BYTES)
        thread = threading.Thread(target=serve)
        thread.start()
        with self.assertRaises(RuntimeRefused):
            RuntimeClient(ours, runtime_kind="real").review(REQUEST)
        thread.join()


if __name__ == "__main__":
    unittest.main()
