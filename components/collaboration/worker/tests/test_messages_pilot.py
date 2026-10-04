"""Offline release-candidate gates for the first Messages API pilot."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import REPO
from runtime_service_smoke import MATERIAL
from ccw import messages_pilot as pilot
from ccw import runtime_api as api
from ccw.model import canonical, decode, digest
from ccw.runtime_interface import parse_request
from ccw.runtime_output import OutputRejected
from ccw.secret_handoff import save_new


REQUEST = parse_request(MATERIAL.encode())


class MessagesPilotTests(unittest.TestCase):
    def real_policy(self, root):
        root.mkdir(mode=0o700)
        (root / "runtime").mkdir(mode=0o700)
        spec = api.contract(root, REQUEST, 443, "net:[123]", "a" * 32,
                            profile=api.REAL_PROFILE, real=True)
        return {"version": 3, "task_sha256": spec["task_sha256"], "api": spec}

    def permit(self, policy):
        policy_sha = digest(policy)
        return {"version": 2, "contract_sha256": policy_sha,
                "expires_at": int(time.time()) + 300, "human_confirmed": True,
                "execution_net_namespace": os.readlink("/proc/self/ns/net"),
                "human_attestation": pilot.required_attestation(policy_sha)}

    def write_policy_and_permit(self, root, policy, permit):
        save_new(root / "policy.json", policy)
        save_new(root / "permit.json", permit)

    def test_v2_permit_binds_exact_policy_and_attestation(self):
        with tempfile.TemporaryDirectory(dir=REPO / ".local") as area:
            root = Path(area) / "service"
            policy = self.real_policy(root)
            permit = self.permit(policy)
            self.write_policy_and_permit(root, policy, permit)

            self.assertEqual(api.require_real_permit(root, policy), digest(permit))
            policy_sha, permit_sha = api.admission(root, policy, REQUEST, real=True)
            self.assertEqual(policy_sha, digest(policy))
            self.assertEqual(permit_sha, digest(permit))
            self.assertFalse((root / "consumed.json").exists())
            changed_policy = copy.deepcopy(policy)
            changed_policy["api"]["provider_post_max"] = 2
            with self.assertRaises(ValueError):
                api.require_real_permit(root, changed_policy)

    def test_attestation_mutations_and_old_permit_fail_closed(self):
        def missing(value):
            value.pop("human_attestation")

        def extra(value):
            value["human_attestation"]["unexpected"] = True

        def console_false(value):
            value["human_attestation"]["console_workspace_scope_expiry_and_usd1_limit_checked"] = False

        def bool_for_int(value):
            value["human_attestation"]["conditions"]["credential_expiry_hours"] = True

        def spend_limit_changed(value):
            value["human_attestation"]["conditions"]["workspace_spend_limit_usd"] = 2

        def workspace_scope_changed(value):
            value["human_attestation"]["conditions"]["credential"] = "Personal-API-key-in-Default-Workspace"

        def expiry_changed(value):
            value["human_attestation"]["conditions"]["credential_expiry_hours"] = 4

        def review_false(value):
            value["human_attestation"]["final_release_diff_independent_review_pass"] = False

        def go_false(value):
            value["human_attestation"]["authorize_this_exact_one_shot_now"] = False

        def old_v1(value):
            value["version"] = 1
            value.pop("human_attestation")

        def wrong_net_namespace(value):
            value["execution_net_namespace"] = "net:[0]"

        mutations = {
            "missing": missing, "extra": extra, "false": console_false,
            "bool-vs-int": bool_for_int, "usd-1-changed": spend_limit_changed,
            "workspace-scope": workspace_scope_changed, "three-hour-expiry": expiry_changed,
            "review": review_false, "go": go_false, "old-v1": old_v1,
            "wrong-net-namespace": wrong_net_namespace,
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=REPO / ".local") as area:
                root = Path(area) / "service"
                policy = self.real_policy(root)
                permit = self.permit(policy)
                mutate(permit)
                self.write_policy_and_permit(root, policy, permit)
                with self.assertRaises((KeyError, ValueError)):
                    api.require_real_permit(root, policy)
                self.assertFalse((root / "consumed.json").exists())

    def test_release_pins_request_payload_and_requires_present_pins(self):
        with tempfile.TemporaryDirectory(dir=REPO / ".local") as area:
            spec = self.real_policy(Path(area) / "service")["api"]
            api.require_real_release(spec)
            for field in ("request_sha256", "payload_sha256", "wire_body_sha256"):
                changed = copy.deepcopy(spec)
                changed[field] = "0" * 64
                with self.subTest(field=field), self.assertRaises(ValueError):
                    api.require_real_release(changed)
            for request_pin, payload_pin in ((None, pilot.PAYLOAD_SHA256),
                                             (pilot.REQUEST_SHA256, None), (None, None)):
                with self.subTest(request_pin=request_pin, payload_pin=payload_pin), \
                     patch.object(pilot, "REQUEST_SHA256", request_pin), \
                     patch.object(pilot, "PAYLOAD_SHA256", payload_pin), \
                     self.assertRaises(ValueError):
                    api.require_real_release(spec)

    def test_validate_policy_rejects_code_budget_and_human_condition_mutations(self):
        with tempfile.TemporaryDirectory(dir=REPO / ".local") as area:
            root = Path(area) / "service"
            policy = self.real_policy(root)
            self.assertEqual(api.validate_policy(root, policy, REQUEST, real=True), digest(policy))
            mutations = {
                "code": lambda value: value["api"]["code"].update({"runtime_api.py": "0" * 64}),
                "output-budget": lambda value: value["api"].update(output_tokens_max=4095),
                "post-budget": lambda value: value["api"].update(provider_post_max=2),
                "human-conditions": lambda value: value["api"]["human_conditions"].update(
                    workspace_spend_limit_usd=2),
            }
            for name, mutate in mutations.items():
                changed = copy.deepcopy(policy)
                mutate(changed)
                with self.subTest(name=name), self.assertRaises(ValueError):
                    api.validate_policy(root, changed, REQUEST, real=True)

    def test_authorize_without_attestation_never_creates_permit(self):
        with tempfile.TemporaryDirectory(dir=REPO / ".local") as area:
            root = Path(area) / "service"
            policy = self.real_policy(root)
            save_new(root / "policy.json", policy)
            save_new(root / "request.json", REQUEST)
            with patch("ccw.secret_handoff.launch_guard"), self.assertRaises(ValueError):
                api.authorize(root, digest(policy))
            self.assertFalse((root / "permit.json").exists())

    def test_semantically_equal_but_different_wire_bytes_never_reach_transport(self):
        import ssl
        network = Mock()
        class SDKError(Exception):
            pass
        class FakeSDK:
            def __init__(self, **kwargs):
                self.transport = kwargs["http_client"].transport
                self.messages = self
            def create(self, **arguments):
                # Same JSON object, but canonical key order differs from pinned SDK bytes.
                self.transport.handle_request(types.SimpleNamespace(method="POST",
                    url="https://api.anthropic.com/v1/messages", content=canonical(arguments)))
            def close(self):
                pass
        sdk = types.SimpleNamespace(Anthropic=FakeSDK,
            DefaultHttpxClient=lambda **kw: types.SimpleNamespace(transport=kw["transport"]),
            APITimeoutError=SDKError, APIConnectionError=SDKError, APIStatusError=SDKError)
        http = types.SimpleNamespace(HTTPTransport=lambda **kw: network,
                                     BaseTransport=object, SyncByteStream=object)
        with tempfile.TemporaryDirectory(dir=REPO / ".local") as area:
            spec = self.real_policy(Path(area) / "service")["api"]
            with patch.dict(sys.modules, {"anthropic": sdk, "httpx2": http}), self.assertRaises(OutputRejected):
                api.sdk_request(REQUEST, "ccw-dummy-" + "0" * 48, spec, ssl.create_default_context(),
                                permit_expires_at=int(time.time()) + 300)
            network.handle_request.assert_not_called()

    def test_service_records_human_authorization_without_provider_accounting(self):
        from ccw import runtime_service as service

        # Synthetic fixture only: no Human issuer, credential handoff or transport.
        for fails in (False, True):
            with self.subTest(runtime_fails=fails), tempfile.TemporaryDirectory(dir=REPO / ".local") as area:
                root = Path(area) / "service"
                policy = self.real_policy(root)
                permit = self.permit(policy)
                self.write_policy_and_permit(root, policy, permit)
                def execute(root, request, credential, observation, *, real):
                    self.assertTrue((root / "consumed.json").exists())
                    self.assertTrue(real)
                    if fails:
                        raise OutputRejected("provider_timeout")
                    return {"status": "succeeded"}
                with patch.object(service, "protect_process"), \
                     patch.object(service.os.environ, "clear"), \
                     patch.object(service.resource, "setrlimit"), \
                     patch.object(service.isolation, "abi"), \
                     patch.object(service, "receive_frame", side_effect=[canonical(REQUEST), b"ccw-dummy"]), \
                     patch.object(service, "send_frame"), \
                     patch.object(api, "execute_offline", side_effect=execute) as runtime:
                    self.assertEqual(service.serve(root, Mock(), api=True, api_real=True,
                                                  credential_endpoint=Mock()), 2 if fails else 0)
                    runtime.assert_called_once()
                evidence = decode((root / "evidence.json").read_bytes())
                self.assertIs(evidence["real_spending_authorized"], True)
                self.assertEqual(evidence["permit_sha256"], digest(permit))
                self.assertEqual(evidence["contract_sha256"], digest(policy))
                self.assertIsNone(evidence["provider_usage"])
                self.assertIsNone(evidence["provider_charge"])
                self.assertIs(evidence["automatic_retry"], False)
                self.assertTrue((root / "consumed.json").exists())

    def test_service_refuses_bad_permit_before_consumption_or_credential(self):
        from ccw import runtime_service as service

        for invalid in ("attestation", "missing", "mismatch", "expired"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory(dir=REPO / ".local") as area:
                root = Path(area) / "service"
                policy = self.real_policy(root)
                permit = self.permit(policy)
                if invalid == "attestation":
                    permit["human_attestation"]["authorize_this_exact_one_shot_now"] = False
                elif invalid == "mismatch":
                    permit["contract_sha256"] = "0" * 64
                elif invalid == "expired":
                    permit["expires_at"] = int(time.time()) - 1
                save_new(root / "policy.json", policy)
                if invalid != "missing":
                    save_new(root / "permit.json", permit)
                endpoint = Mock()
                credential = Mock()
                with patch.object(service, "protect_process"), \
                     patch.object(service.os.environ, "clear"), \
                     patch.object(service.resource, "setrlimit"), \
                     patch.object(service.isolation, "abi"), \
                     patch.object(service, "receive_frame", return_value=canonical(REQUEST)) as receive, \
                     patch.object(service, "send_frame") as response_frame, \
                     patch.object(api, "exchange") as runtime:
                    self.assertEqual(service.serve(root, endpoint, api=True, api_real=True,
                                                  credential_endpoint=credential), 2)
                    runtime.assert_not_called()
                credential.recv.assert_not_called()
                receive.assert_called_once_with(endpoint, service.MAX_REQUEST_BYTES)
                self.assertFalse((root / "consumed.json").exists())
                self.assertFalse((root / "evidence.json").exists())
                response_frame.assert_called_once()
                self.assertEqual(decode(response_frame.call_args.args[1])["status"], "refused")


if __name__ == "__main__":
    unittest.main()
