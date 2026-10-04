"""No SDK/network required: API contract, strict mapper and rail separation."""
import copy
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import REPO
from ccw import runtime_api as api
from ccw.model import canonical, decode, digest
from ccw.secret_handoff import save_new
from ccw.runtime_interface import (ReviewRequest, RuntimeClient, RuntimeRefused, parse_request,
                                  send_frame, MAX_RESULT_BYTES)
from ccw.runtime_output import OutputRejected, validated_result

REQUEST = parse_request(ReviewRequest("saved:test", "line one\nline two").encode())
KEY = "ccw-dummy-" + "7" * 48


def response():
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": api.MODEL,
        "content": [{"type": "text", "text": canonical({"provider": api.PROVIDER,
            "summary": "Synthetic only", "findings": [], "unverified": ["No inference."]}).decode()}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 20}}


class APIContractTests(unittest.TestCase):
    def test_real_transport_requires_certificate_and_hostname_verification(self):
        spec = api.contract(Path("/tmp/test-api"), REQUEST, 443, "net:[123]", "a" * 32,
                            profile=api.REAL_PROFILE, real=True)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls.check_hostname = False
        with patch.object(api, "require_real_release"), patch.dict(sys.modules,
                {"anthropic": types.SimpleNamespace(), "httpx2": types.SimpleNamespace()}):
            with self.assertRaisesRegex(ValueError, "verified TLS required"):
                api.sdk_request(REQUEST, KEY, spec, tls)
            tls.verify_mode = ssl.CERT_NONE
            with self.assertRaisesRegex(ValueError, "verified TLS required"):
                api.sdk_request(REQUEST, KEY, spec, tls)

    def test_transport_refuses_second_request_and_auxiliary_even_with_misbehaving_sdk(self):
        # An adversarial SDK double exercises the public transport boundary.
        # No network and no optional package import are involved in this test.
        sent, configs = [], {}
        class InnerStream:
            def __iter__(self):
                yield canonical(response())
            def close(self):
                pass
        class HTTPTransport:
            def __init__(self, **kwargs):
                configs["transport"] = kwargs
            def handle_request(self, request):
                sent.append(request.method)
                return types.SimpleNamespace(headers={}, stream=InnerStream())
            def close(self):
                pass
        class HttpClient:
            def __init__(self, **kwargs):
                configs["http"] = kwargs
                self.transport = kwargs["transport"]
        class FakeSDK:
            method, count = "POST", 1
            def __init__(self, **kwargs):
                configs["sdk"] = kwargs
                self.http = kwargs["http_client"]
                self.messages = self
            def create(self, **arguments):
                for _ in range(self.count):
                    outgoing = types.SimpleNamespace(method=self.method,
                        url="http://127.0.0.1:12345/v1/messages", content=canonical(arguments))
                    list(self.http.transport.handle_request(outgoing).stream)
            def close(self):
                self.http.transport.close()
        class SDKError(Exception):
            pass
        sdk = types.SimpleNamespace(Anthropic=FakeSDK, DefaultHttpxClient=HttpClient,
            APITimeoutError=SDKError, APIConnectionError=SDKError, APIStatusError=SDKError)
        http = types.SimpleNamespace(HTTPTransport=HTTPTransport, SyncByteStream=object, BaseTransport=object)
        spec = api.contract(Path("/tmp/test-api"), REQUEST, 12345, "net:[123]", "a" * 32)
        with patch.dict(sys.modules, {"anthropic": sdk, "httpx2": http}):
            result = api.sdk_request(REQUEST, KEY, spec, object())
            self.assertEqual(result["report"]["provider"], api.PROVIDER)
            self.assertEqual(sent, ["POST"])
            self.assertEqual(configs["sdk"]["max_retries"], 0)
            self.assertEqual(configs["transport"]["retries"], 0)
            self.assertFalse(configs["transport"]["trust_env"])
            self.assertFalse(configs["http"]["trust_env"])
            self.assertFalse(configs["http"]["follow_redirects"])
            self.assertFalse(configs["transport"]["http2"])
            sent.clear()
            FakeSDK.count = 2
            with self.assertRaises(OutputRejected) as caught:
                api.sdk_request(REQUEST, KEY, spec, object())
            self.assertEqual(caught.exception.reason, "additional_request_blocked")
            self.assertEqual(sent, ["POST"])
            sent.clear()
            FakeSDK.count, FakeSDK.method = 1, "HEAD"
            with self.assertRaises(OutputRejected):
                api.sdk_request(REQUEST, KEY, spec, object())
            self.assertEqual(sent, [])
            FakeSDK.method = "POST"
            candidate = api.contract(Path("/tmp/test-api"), REQUEST, 12345, "net:[123]", "a" * 32,
                                     profile=api.REAL_PROFILE)
            for expiry in (None, int(time.time()) - 1):
                with self.assertRaises(OutputRejected):
                    api.sdk_request(REQUEST, KEY, candidate, object(), permit_expires_at=expiry)
                self.assertEqual(sent, [])
            api.sdk_request(REQUEST, KEY, candidate, object(), permit_expires_at=int(time.time()) + 300)
            self.assertEqual(sent, ["POST"])
            self.assertEqual(configs["sdk"]["timeout"], 45)

    def test_cost_contract_and_fail_closed_mutations(self):
        with tempfile.TemporaryDirectory(dir=REPO / ".local") as area:
            root = Path(area) / "service"
            policy = api.prepare_offline(root, REQUEST, 12345, "net:[123]", "a" * 32)
            with patch.object(api, "namespace_check"):
                api.validate_policy(root, policy, REQUEST)
                spec = policy["api"]
                self.assertEqual(spec["human_authorized_attempts"], 1)
                self.assertEqual(spec["ccw_invocations_max"], 1)
                self.assertEqual(spec["provider_post_max"], 1)
                self.assertTrue(spec["cost_gate"]["api_billing_separate_from_subscription"])
                self.assertIsNone(spec["cost_gate"]["provider_confirmed_cost"])
                self.assertFalse(spec["cost_gate"]["real_spending_authorized"])
                for field, value in (("follow_redirects", True), ("sdk_max_retries", 1),
                        ("transport_retries", 1), ("trust_env", True), ("fallback", True),
                        ("human_authorized_attempts", 2), ("provider_post_max", True),
                        ("input_bytes_max", 999999), ("output_tokens_max", 999999),
                        ("endpoint", "https://api.anthropic.com"), ("stream", True),
                        ("credential_rail", "subscription"), ("real_enabled", True)):
                    bad = copy.deepcopy(policy)
                    bad["api"][field] = value
                    with self.subTest(field=field), self.assertRaises(ValueError):
                        api.validate_policy(root, bad, REQUEST)
                (root / "STOP").touch()
                with self.assertRaises(ValueError):
                    api.validate_policy(root, policy, REQUEST)
            for key in ("sk-" + "ant-synthetic-not-real", "ccw-dummy-short", "ccw-dummy-" + "g" * 48):
                with self.assertRaises(ValueError):
                    api.dummy_key(key)

    def test_input_limit_covers_prompt_and_frozen_source(self):
        self.assertLess(len(canonical(api.message_arguments(REQUEST))), api.INPUT_BYTES)
        request = parse_request(ReviewRequest("x", "x" * 16000).encode())
        with self.assertRaises(ValueError):
            api.contract(Path("/tmp/example"), request, 12345, "net:[123]", "a" * 32)

    def test_strict_mapping_untrusted_preview_and_cli_separation(self):
        result = api.map_response(canonical(response()), REQUEST, KEY)
        with self.assertRaises(OutputRejected):
            validated_result(canonical(result["report"]), REQUEST)
        for kind, accepted in ((api.KIND, True), ("offline-fake", False), ("offline-native", False), ("real", False)):
            ours, theirs = socket.socketpair()
            try:
                send_frame(theirs, canonical(result), MAX_RESULT_BYTES)
                client = RuntimeClient(ours, runtime_kind=kind)
                request = ReviewRequest("saved:test", "line one\nline two")
                if accepted:
                    preview = decode(client.review(request).preview_text())
                    self.assertEqual(preview["runtime_kind"], api.KIND)
                    self.assertTrue(preview["untrusted_output"] and preview["display_only"])
                    self.assertFalse(preview["external_actions_executed"])
                    with self.assertRaises(RuntimeRefused):
                        client.review(request)
                else:
                    with self.assertRaises(RuntimeRefused):
                        client.review(request)
            finally:
                ours.close()
                theirs.close()

    def test_malformed_incomplete_tools_and_secret_fail_closed(self):
        mutations = [lambda d: d.pop("content"), lambda d: d.update(stop_reason="max_tokens"),
            lambda d: d.update(stop_reason="refusal"), lambda d: d.update(model="other"),
            lambda d: d["usage"].update(output_tokens=True),
            lambda d: d["usage"].update(output_tokens=api.OUTPUT_TOKENS+1),
            lambda d: d.update(content=[{"type": "tool_use", "name": "shell"}]),
            lambda d: d["content"][0].update(text="{}"),
            lambda d: d["content"][0].update(text='{"provider":"claude-cli-offline-v1"}'),
            lambda d: d.update(id="msg_\ud800")]
        for mutate in mutations:
            value = response()
            mutate(value)
            with self.assertRaises(OutputRejected):
                api.map_response(canonical(value), REQUEST, KEY)
        for raw in (b"{", b'{}', b'\xff', canonical(response())[:-1] + b',"type":"message"}'):
            with self.assertRaises(OutputRejected):
                api.map_response(raw, REQUEST, KEY)
        value = response()
        value["id"] = KEY
        for raw in (canonical(value), canonical(value).replace(KEY.encode(),
                b"".join(("\\u%04x" % ord(c)).encode() for c in KEY))):
            with self.assertRaises(OutputRejected) as caught:
                api.map_response(raw, REQUEST, KEY)
            self.assertEqual(caught.exception.reason, "secret_withheld")
        with self.assertRaises(OutputRejected) as caught:
            api.map_response(b"x" * (api.RAW_BYTES + 1), REQUEST, KEY)
        self.assertEqual(caught.exception.reason, "oversized_output")

    def test_real_candidate_normal_metadata_and_resource_budgets(self):
        spec = api.profile_values(api.REAL_PROFILE)
        self.assertEqual((spec["wall_timeout_ms"], spec["http_timeout_ms"], spec["output_tokens_max"],
                          spec["raw_response_bytes_max"], spec["result_bytes_max"]),
                         (60000, 45000, 4096, 262144, 32768))
        self.assertEqual(api.message_arguments(REQUEST, spec)["max_tokens"], 4096)
        value = response()
        value.update(container=None, stop_details=None, future_metadata={"ignored": True})
        value["content"][0]["citations"] = None
        value["usage"].update(cache_creation_input_tokens=0, cache_read_input_tokens=12,
            cache_creation={"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
            service_tier="standard", inference_geo="us", output_tokens=256,
            output_tokens_details={"reasoning_tokens": 0}, server_tool_use={"web_search_requests": 0})
        api.map_response(canonical(value), REQUEST, KEY, spec)
        for mutate in (lambda d: d["usage"].update(server_tool_use={"web_search_requests": 1}),
                       lambda d: d.update(container={"id": "tool"}),
                       lambda d: d["usage"].update(output_tokens=4097),
                       lambda d: d["content"][0].update(text="{}")):
            bad = copy.deepcopy(value)
            mutate(bad)
            with self.assertRaises(OutputRejected):
                api.map_response(canonical(bad), REQUEST, KEY, spec)
        value["future_metadata"] = {"ignored": KEY}
        with self.assertRaises(OutputRejected) as caught:
            api.map_response(canonical(value), REQUEST, KEY, spec)
        self.assertEqual(caught.exception.reason, "secret_withheld")
        with self.assertRaises(OutputRejected):
            api.map_response(b"x" * 262145, REQUEST, KEY, spec)

    def test_permit_binds_every_contract_field_and_expiry(self):
        with tempfile.TemporaryDirectory(dir=REPO / ".local") as area:
            root = Path(area) / "service"
            policy = api.prepare_offline(root, REQUEST, 12345, "net:[123]", "a" * 32, profile=api.REAL_PROFILE)
            with self.assertRaises(FileNotFoundError):
                api.require_real_permit(root, policy)
            permit = {"version": 1, "contract_sha256": digest(policy),
                      "expires_at": int(time.time()) + 300, "human_confirmed": True}
            save_new(root / "permit.json", permit)
            self.assertEqual(api.require_real_permit(root, policy), digest(permit))
            for field in policy["api"]:
                bad = copy.deepcopy(policy)
                bad["api"][field] = {"changed": True}
                with self.subTest(field=field), self.assertRaises(ValueError):
                    api.require_real_permit(root, bad)
            with patch.object(api.time, "time", return_value=permit["expires_at"]):
                with self.assertRaises(ValueError):
                    api.require_real_permit(root, policy)

    def test_real_service_admission_rejects_before_credential_or_runtime(self):
        from ccw import runtime_service as service
        from ccw.runtime_interface import receive_frame
        for fault in ("missing", "mismatch", "expired", "wrong-release-input", "changed-contract"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=REPO / ".local") as area:
                root = Path(area) / "service"
                root.mkdir(mode=0o700)
                (root / "runtime").mkdir(mode=0o700)
                spec = api.contract(root, REQUEST, 443, "net:[123]", "a" * 32, profile=api.REAL_PROFILE, real=True)
                policy = {"version": 3, "task_sha256": spec["task_sha256"], "api": spec}
                permit = {"version": 2, "contract_sha256": digest(policy),
                          "expires_at": int(time.time()) + 300, "human_confirmed": True,
                          "execution_net_namespace": os.readlink("/proc/self/ns/net"),
                          "human_attestation": api.pilot.required_attestation(digest(policy))}
                if fault == "mismatch":
                    permit["contract_sha256"] = "0" * 64
                if fault == "expired":
                    permit["expires_at"] = int(time.time()) - 1
                if fault == "changed-contract":
                    policy["api"]["wall_timeout_ms"] = 5000
                if fault != "missing":
                    save_new(root / "permit.json", permit)
                save_new(root / "policy.json", policy)
                ours, theirs = socket.socketpair()
                credential = unittest.mock.Mock()
                try:
                    send_frame(ours, canonical(REQUEST), 65536)
                    with patch.object(service, "protect_process"), patch.object(service.os.environ, "clear"), \
                         patch.object(service.resource, "setrlimit"), patch.object(service.isolation, "abi"), \
                         patch.object(api, "exchange") as launch, \
                         patch.object(api, "require_real_permit", wraps=api.require_real_permit) as gate:
                        self.assertEqual(service.serve(root, theirs, api=True, api_real=True,
                                                      credential_endpoint=credential), 2)
                        self.assertEqual(gate.call_count, 0 if fault == "changed-contract" else 1)
                        launch.assert_not_called()
                    credential.recv.assert_not_called()
                    self.assertFalse((root / "consumed.json").exists())
                    self.assertEqual(decode(receive_frame(ours, 32768))["status"], "refused")
                finally:
                    ours.close()
                    theirs.close()

    def test_expiry_after_consumption_never_launches_or_refunds(self):
        with tempfile.TemporaryDirectory(dir=REPO / ".local") as area:
            root = Path(area) / "service"
            policy = api.prepare_offline(root, REQUEST, 12345, "net:[123]", "a" * 32, profile=api.REAL_PROFILE)
            permit = {"version": 1, "contract_sha256": digest(policy),
                      "expires_at": int(time.time()) + 300, "human_confirmed": True}
            save_new(root / "permit.json", permit)
            marker = api.consumed_marker(policy, digest(policy), digest(permit), os.getpid())
            save_new(root / "consumed.json", marker)
            with patch.object(api, "namespace_check"), patch.object(api, "exchange") as launch, \
                 patch.object(api.time, "time", return_value=permit["expires_at"]):
                with self.assertRaises(ValueError):
                    api.execute_offline(root, REQUEST, KEY, {})
                launch.assert_not_called()
            self.assertEqual(decode((root / "consumed.json").read_bytes()), marker)

    def test_real_mapper_client_identity_and_issuer_release_gate(self):
        # Synthetic bytes only. This test never calls an SDK or live launcher.
        spec = {**api.profile_values(api.REAL_PROFILE), "real_enabled": True}
        value = response()
        report = decode(value["content"][0]["text"])
        report["provider"] = api.REAL_PROVIDER
        value["content"][0]["text"] = canonical(report).decode()
        result = api.map_response(canonical(value), REQUEST, KEY, spec)
        self.assertFalse(result["offline"])
        for kind in (api.REAL_KIND, api.KIND, "real"):
            ours, theirs = socket.socketpair()
            try:
                send_frame(theirs, canonical(result), MAX_RESULT_BYTES)
                client = RuntimeClient(ours, runtime_kind=kind)
                if kind == api.REAL_KIND:
                    preview = decode(client.review(ReviewRequest("saved:test", "line one\nline two")).preview_text())
                    self.assertEqual(preview["runtime_kind"], api.REAL_KIND)
                    self.assertTrue(preview["display_only"])
                else:
                    with self.assertRaises(RuntimeRefused):
                        client.review(ReviewRequest("saved:test", "line one\nline two"))
            finally:
                ours.close()
                theirs.close()
        with tempfile.TemporaryDirectory(dir=REPO / ".local") as area:
            root = Path(area)
            policy = {"version": 3, "api": api.contract(root, REQUEST, 443, "net:[123]", "a" * 32,
                                                       profile=api.REAL_PROFILE, real=True)}
            save_new(root / "policy.json", policy)
            with patch("ccw.secret_handoff.launch_guard"), self.assertRaises(ValueError):
                api.authorize(root, digest(policy))
            self.assertFalse((root / "permit.json").exists())


if __name__ == "__main__":
    unittest.main()
