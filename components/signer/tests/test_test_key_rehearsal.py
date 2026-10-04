import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "test_key_rehearsal.py"
SPEC = importlib.util.spec_from_file_location("signer_test_key_rehearsal", MODULE_PATH)
rehearsal = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(rehearsal)


class TestKeyRehearsalTests(unittest.TestCase):
    def setUp(self):
        # Never run host mutations in offline tests, including on a WSL runner.
        for name, result in (("operate", {"safe": True}),
                             ("require_safe", {"safe": True}),
                             ("status", {"isWsl": False, "safe": True})):
            patcher = mock.patch.object(rehearsal.crash_gate, name, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_unsafe_gate_blocks_offline_decrypt_before_process_spawn(self):
        rehearsal.crash_gate.require_safe.side_effect = rehearsal.crash_gate.GateError(
            "WSL_CRASH_CAPTURE_UNSAFE")
        with mock.patch.object(rehearsal.subprocess, "Popen") as process:
            for operation in (rehearsal.offline_status, rehearsal.credential_identity):
                with self.assertRaisesRegex(rehearsal.crash_gate.GateError, "WSL_CRASH_CAPTURE_UNSAFE"):
                    operation()
            process.assert_not_called()

    def test_cleanup_disarm_failure_preserves_resumable_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = {name: root / name for name in (
                "PROVENANCE", "CREDENTIAL", "CONFIG", "STATE", "AUDIT", "STATE_DIR", "INPUT_DIR")}
            paths["PROVENANCE"].write_text(json.dumps({
                "schema": 2, "mode": "TEST_KEY_ONLY", "phase": "CLEANUP_ARTIFACTS_REMOVED",
            }))
            rehearsal.crash_gate.status.return_value = {"isWsl": True}
            rehearsal.crash_gate.operate.side_effect = rehearsal.crash_gate.GateError(
                "CRASH_GATE_CURRENT_STATE_MISMATCH")
            def save(value, phase, **updates):
                result = {**value, **updates, "phase": phase}
                paths["PROVENANCE"].write_text(json.dumps(result))
                return result
            with mock.patch.multiple(rehearsal, **paths), \
                 mock.patch.object(rehearsal, "require_root"), \
                 mock.patch.object(rehearsal, "systemctl") as ctl, \
                 mock.patch.object(rehearsal, "write_provenance", side_effect=save), \
                 mock.patch.object(rehearsal, "cleanup_tmpfiles_if_principals_exist"):
                with self.assertRaisesRegex(rehearsal.crash_gate.GateError, "CURRENT_STATE_MISMATCH"):
                    rehearsal.cleanup_test_key()
                self.assertTrue(paths["PROVENANCE"].exists())
                self.assertEqual(ctl.call_args_list[0].args[0], "stop")
                rehearsal.crash_gate.operate.side_effect = None
                self.assertTrue(rehearsal.cleanup_test_key()["passed"])
                self.assertFalse(paths["PROVENANCE"].exists())

    def test_config_is_bounded_paper_no_value_only(self):
        identity = {
            "did": "did:key:z6Mk" + "a" * 44,
            "tclkCommit": "5cc4ab93efbc8999a3a7e1471b639deca25998ea",
        }
        config = rehearsal.build_test_config(identity, 1_900_000_000_000)
        self.assertEqual(set(config), {"version", "policy", "acquisitionRoot"})
        policy = config["policy"]
        self.assertEqual(policy["expectedDid"], identity["did"])
        self.assertEqual(policy["protocol"], "tclk/1")
        self.assertEqual(policy["workFamily"], "math.gcd_lcm")
        self.assertEqual(policy["maxWorkItems"], 1)
        self.assertEqual(policy["maxSignatures"], 4)
        self.assertTrue(policy["requireHeartbeat"])
        self.assertEqual(policy["noValueMarker"], "EXPLICIT_PAPER_NO_VALUE")
        self.assertLessEqual(policy["expiresAtMs"] - policy["issuedAtMs"], 24 * 60 * 60 * 1000)
        self.assertEqual(config["acquisitionRoot"], str(rehearsal.INPUT_DIR))

    def test_invalid_test_identity_is_rejected(self):
        with self.assertRaisesRegex(rehearsal.RehearsalError, "TEST_IDENTITY_INVALID"):
            rehearsal.build_test_config({"did": "not-a-did", "tclkCommit": "x"}, 1)

    def test_atomic_json_contains_only_requested_sanitized_fields(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "evidence.json"
            rehearsal.atomic_json(
                path,
                {
                    "testDid": "did:key:test",
                    "testSeedOnlyByConstruction": True,
                    "externalWritesByConstruction": 0,
                },
                0o600,
                os.getuid(),
                os.getgid(),
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            value = json.loads(path.read_text())
            self.assertEqual(
                value,
                {
                    "externalWritesByConstruction": 0,
                    "testDid": "did:key:test",
                    "testSeedOnlyByConstruction": True,
                },
            )

    def test_node_version_accepts_only_root_equivalent_v22_executable(self):
        with tempfile.TemporaryDirectory() as td:
            node = Path(td) / "node"
            node.write_text("#!/bin/sh\necho v22.22.0\n")
            node.chmod(0o755)
            self.assertEqual(
                rehearsal.node_version(node, os.getuid(), os.getgid()),
                "v22.22.0",
            )

            node.write_text("#!/bin/sh\necho v23.0.0\n")
            node.chmod(0o755)
            with self.assertRaisesRegex(rehearsal.RehearsalError, "NODE22_REQUIRED"):
                rehearsal.node_version(node, os.getuid(), os.getgid())

    def test_prepare_cli_has_no_seed_argument(self):
        source = MODULE_PATH.read_text()
        self.assertNotIn("--seed", source)
        self.assertNotIn("--private-key", source)
        self.assertIn('prepare_parser.add_argument("--node", required=True)', source)

    def test_repo_identity_uses_safe_directory_and_requires_clean_tree(self):
        root = rehearsal.COMPONENT.parents[1]
        good = [
            SimpleNamespace(stdout=("a" * 40 + "\n").encode()),
            SimpleNamespace(stdout=b""),
        ]
        with mock.patch.object(rehearsal, "run", side_effect=good) as runner:
            value = rehearsal.repo_identity()
        self.assertEqual(value["head"], "a" * 40)
        self.assertTrue(value["clean"])
        first = runner.call_args_list[0].args[0]
        second = runner.call_args_list[1].args[0]
        self.assertIn("--no-optional-locks", first)
        self.assertIn(f"safe.directory={root}", first)
        self.assertIn("core.fsmonitor=false", first)
        self.assertIn("core.hooksPath=/dev/null", first)
        self.assertIn(f"safe.directory={root}", second)
        self.assertIn("core.fsmonitor=false", second)
        self.assertIn("core.hooksPath=/dev/null", second)

        dirty = [
            SimpleNamespace(stdout=("b" * 40 + "\n").encode()),
            SimpleNamespace(stdout=b" M components/signer/x\n"),
        ]
        with mock.patch.object(rehearsal, "run", side_effect=dirty):
            with self.assertRaisesRegex(rehearsal.RehearsalError, "DIRTY_REVIEWED_TREE"):
                rehearsal.repo_identity()

    def test_effective_units_refuse_dropins_and_wrong_mdwe_policy(self):
        def expected_properties(unit, names):
            expected = rehearsal.EFFECTIVE_UNIT_EXPECTED[unit]
            return {
                "LimitCORE": "0",
                "LimitCORESoft": "0",
                "FragmentPath": str(rehearsal.UNIT_DIR / unit),
                "DropInPaths": "",
                "User": expected["User"],
                "Group": expected["Group"],
                "PrivateNetwork": expected["PrivateNetwork"],
                "MemoryDenyWriteExecute": expected["MemoryDenyWriteExecute"],
                "ProtectSystem": expected["ProtectSystem"],
                "RestrictAddressFamilies": " ".join(sorted(expected["RestrictAddressFamilies"])),
                "InaccessiblePaths": " ".join(sorted(expected["InaccessiblePaths"])),
            }

        with mock.patch.object(rehearsal, "systemctl_properties", side_effect=expected_properties):
            observed = rehearsal.verify_effective_units()
        self.assertEqual(set(observed), set(rehearsal.EFFECTIVE_UNIT_EXPECTED))

        for field in ("LimitCORE", "LimitCORESoft"):
            def wrong_limit(unit, names):
                return {**expected_properties(unit, names), field: "infinity"}
            with mock.patch.object(rehearsal, "systemctl_properties", side_effect=wrong_limit):
                with self.assertRaisesRegex(rehearsal.RehearsalError, "UNIT_CORE_LIMIT_MISMATCH"):
                    rehearsal.verify_effective_units()

        def bad_properties(unit, names):
            value = expected_properties(unit, names)
            if unit == "flop-policy-signer.service":
                value["DropInPaths"] = "/etc/systemd/system/flop-policy-signer.service.d/override.conf"
            return value

        with mock.patch.object(rehearsal, "systemctl_properties", side_effect=bad_properties):
            with self.assertRaisesRegex(rehearsal.RehearsalError, "UNIT_DROPIN_REFUSED"):
                rehearsal.verify_effective_units()

    def test_prepare_records_identity_and_handoff_phase_before_credential_creation(self):
        events = []

        def provenance(value, phase, **updates):
            events.append("phase:" + phase)
            result = dict(value)
            result.update(updates)
            result["phase"] = phase
            return result

        def repo_identity():
            events.append("repo")
            return {"head": "a" * 40, "clean": True, "root": "/repo"}

        def install_runtime(node, source):
            events.append("install")
            return {
                "nodeSha256": "n" * 64,
                "nodeVersion": "v22.22.0",
                "manifest": {"/installed": "f" * 64},
            }

        def handoff(seed):
            rehearsal.crash_gate.operate.assert_called_once_with("arm")
            rehearsal.crash_gate.require_safe.assert_called_once()
            events.append("handoff")
            return "c" * 64

        def urandom(size):
            rehearsal.crash_gate.operate.assert_called_once_with("arm")
            rehearsal.crash_gate.require_safe.assert_called_once()
            events.append("seed")
            return b"1" * size

        fake_state = SimpleNamespace(exists=lambda: True)
        with mock.patch.object(rehearsal, "require_root"), \
             mock.patch.object(rehearsal, "empty_setup_required"), \
             mock.patch.object(rehearsal, "repo_identity", side_effect=repo_identity), \
             mock.patch.object(
                 rehearsal,
                 "source_node_metadata",
                 return_value={"path": "/node", "sha256": "s" * 64},
             ), \
             mock.patch.object(rehearsal, "write_provenance", side_effect=provenance), \
             mock.patch.object(rehearsal, "install_runtime", side_effect=install_runtime), \
             mock.patch.object(rehearsal, "provision_principals_and_dirs"), \
             mock.patch.object(rehearsal, "systemctl", return_value=SimpleNamespace()), \
             mock.patch.object(rehearsal, "verify_effective_units", return_value={"ok": {}}), \
             mock.patch.object(rehearsal, "sandbox_node_probe", return_value={"ok": True}), \
             mock.patch.object(rehearsal.os, "urandom", side_effect=urandom), \
             mock.patch.object(
                 rehearsal,
                 "test_identity",
                 return_value={"did": "did:key:z6Mk" + "a" * 44, "tclkCommit": "commit"},
             ), \
             mock.patch.object(rehearsal, "write_test_config"), \
             mock.patch.object(rehearsal, "sha256_file", return_value="d" * 64), \
             mock.patch.object(rehearsal, "handoff_test_seed", side_effect=handoff), \
             mock.patch.object(rehearsal, "STATE", fake_state), \
             mock.patch.object(rehearsal, "observe", return_value={"passed": True, "checks": {}}):
            result = rehearsal.prepare(Path("/node"))

        self.assertTrue(result["passed"])
        self.assertLess(events.index("repo"), events.index("phase:STARTING"))
        self.assertLess(events.index("phase:STARTING"), events.index("install"))
        self.assertLess(events.index("phase:SANDBOX_PROBED"), events.index("seed"))
        self.assertLess(events.index("seed"), events.index("phase:IDENTITY_READY"))
        self.assertLess(events.index("phase:IDENTITY_READY"), events.index("phase:CONFIG_WRITTEN"))
        self.assertLess(events.index("phase:CREDENTIAL_HANDOFF_STARTING"), events.index("handoff"))
        self.assertLess(events.index("handoff"), events.index("phase:CREDENTIAL_WRITTEN"))

    def test_cleanup_refuses_credential_provenance_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            provenance = root / "provenance.json"
            credential = root / "project-seed.cred"
            config = root / "config.json"
            state = root / "missing-state.json"
            audit = root / "missing-audit.jsonl"
            state_dir = root / "state-dir"
            input_dir = root / "input"
            provenance.write_text(json.dumps({
                "schema": 2,
                "mode": "TEST_KEY_ONLY",
                "phase": "CREDENTIAL_WRITTEN",
                "testDid": "did:key:z6Mk" + "a" * 44,
                "credentialSha256": "0" * 64,
                "configSha256": None,
            }))
            credential.write_bytes(b"different")
            config.write_text(json.dumps({"policy": {"expectedDid": "did:key:z6Mk" + "a" * 44}}))

            with mock.patch.object(rehearsal, "PROVENANCE", provenance), \
                 mock.patch.object(rehearsal, "CREDENTIAL", credential), \
                 mock.patch.object(rehearsal, "CONFIG", config), \
                 mock.patch.object(rehearsal, "STATE", state), \
                 mock.patch.object(rehearsal, "AUDIT", audit), \
                 mock.patch.object(rehearsal, "STATE_DIR", state_dir), \
                 mock.patch.object(rehearsal, "INPUT_DIR", input_dir), \
                 mock.patch.object(rehearsal, "require_root"), \
                 mock.patch.object(rehearsal, "systemctl", return_value=SimpleNamespace()):
                with self.assertRaisesRegex(
                    rehearsal.RehearsalError,
                    "CREDENTIAL_PROVENANCE_MISMATCH",
                ):
                    rehearsal.cleanup_test_key()


    def test_cleanup_retry_uses_recorded_safe_status_after_credential_and_config_are_gone(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            provenance = root / "provenance.json"
            state_dir = root / "state-dir"
            state_dir.mkdir()
            state = state_dir / "state.json"
            state.write_text("encrypted-state")
            input_dir = root / "input"
            input_dir.mkdir()
            credential = root / "missing-credential"
            config = root / "missing-config"
            audit = state_dir / "missing-audit.jsonl"
            tmpfiles = root / "missing-tmpfiles.conf"
            did = "did:key:z6Mk" + "a" * 44
            provenance.write_text(json.dumps({
                "schema": 2,
                "mode": "TEST_KEY_ONLY",
                "phase": "CLEANUP_STARTED",
                "testDid": did,
                "cleanupAuditEvidence": {"exists": False},
                "cleanupProtectedStatus": {
                    "did": did,
                    "signaturesUsed": 0,
                    "work": None,
                    "actions": {},
                },
            }))

            rehearsal.crash_gate.status.return_value = {"isWsl": True}
            def disarm(command):
                self.assertEqual(command, "disarm")
                self.assertFalse(credential.exists())
                self.assertFalse(config.exists())
                self.assertFalse(state.exists())
                self.assertTrue(provenance.exists())
                self.assertEqual(json.loads(provenance.read_text())["phase"], "CLEANUP_ARTIFACTS_REMOVED")
                return {"armed": False}
            rehearsal.crash_gate.operate.side_effect = disarm

            def local_provenance(value, phase, **updates):
                next_value = dict(value)
                next_value.update(updates)
                next_value["phase"] = phase
                provenance.write_text(json.dumps(next_value))
                return next_value

            with mock.patch.object(rehearsal, "PROVENANCE", provenance), \
                 mock.patch.object(rehearsal, "CREDENTIAL", credential), \
                 mock.patch.object(rehearsal, "CONFIG", config), \
                 mock.patch.object(rehearsal, "STATE", state), \
                 mock.patch.object(rehearsal, "AUDIT", audit), \
                 mock.patch.object(rehearsal, "STATE_DIR", state_dir), \
                 mock.patch.object(rehearsal, "INPUT_DIR", input_dir), \
                 mock.patch.object(rehearsal, "TMPFILES", tmpfiles), \
                 mock.patch.object(rehearsal, "require_root"), \
                 mock.patch.object(rehearsal, "systemctl", return_value=SimpleNamespace()), \
                 mock.patch.object(rehearsal, "write_provenance", side_effect=local_provenance), \
                 mock.patch.object(
                     rehearsal,
                     "offline_status",
                     side_effect=AssertionError("must reuse recorded safe status"),
                 ):
                result = rehearsal.cleanup_test_key()

            self.assertTrue(result["passed"])
            self.assertFalse(state.exists())
            self.assertFalse(provenance.exists())
            rehearsal.crash_gate.operate.assert_called_once_with("disarm")

    def test_handoff_starting_credential_requires_test_did_match_before_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            provenance = root / "provenance.json"
            credential = root / "project-seed.cred"
            config = root / "config.json"
            state = root / "missing-state.json"
            audit = root / "missing-audit.jsonl"
            state_dir = root / "state-dir"
            input_dir = root / "input"
            did = "did:key:z6Mk" + "a" * 44
            provenance.write_text(json.dumps({
                "schema": 2,
                "mode": "TEST_KEY_ONLY",
                "phase": "CREDENTIAL_HANDOFF_STARTING",
                "testDid": did,
                "configSha256": None,
            }))
            credential.write_bytes(b"encrypted")
            config.write_text(json.dumps({"policy": {"expectedDid": did}}))

            with mock.patch.object(rehearsal, "PROVENANCE", provenance), \
                 mock.patch.object(rehearsal, "CREDENTIAL", credential), \
                 mock.patch.object(rehearsal, "CONFIG", config), \
                 mock.patch.object(rehearsal, "STATE", state), \
                 mock.patch.object(rehearsal, "AUDIT", audit), \
                 mock.patch.object(rehearsal, "STATE_DIR", state_dir), \
                 mock.patch.object(rehearsal, "INPUT_DIR", input_dir), \
                 mock.patch.object(rehearsal, "require_root"), \
                 mock.patch.object(rehearsal, "systemctl", return_value=SimpleNamespace()), \
                 mock.patch.object(
                     rehearsal,
                     "credential_identity",
                     return_value={"did": "did:key:z6Mk" + "b" * 44, "tclkCommit": "x"},
                 ):
                with self.assertRaisesRegex(
                    rehearsal.RehearsalError,
                    "CREDENTIAL_PROVENANCE_MISMATCH",
                ):
                    rehearsal.cleanup_test_key()


    def test_resume_reviewed_updates_exact_manifest_then_observes_same_safe_state(self):
        events = []
        old_manifest = {"/installed/old": "a" * 64}
        target_manifest = {"/installed/new": "b" * 64}
        did = "did:key:z6Mk" + "a" * 44
        provenance_value = {
            "schema": 2,
            "mode": "TEST_KEY_ONLY",
            "phase": "SOCKETS_STARTED",
            "repoHead": "1" * 40,
            "installManifest": old_manifest,
            "testDid": did,
        }

        def write_provenance(value, phase, **updates):
            events.append("phase:" + phase)
            out = dict(value)
            out.update(updates)
            out["phase"] = phase
            return out

        def systemctl(*args, **kwargs):
            events.append("systemctl:" + ":".join(args))
            return SimpleNamespace(stdout=b"")

        with tempfile.TemporaryDirectory() as td:
            provenance_path = Path(td) / "provenance.json"
            provenance_path.write_text("{}")
            with mock.patch.object(rehearsal, "PROVENANCE", provenance_path), \
                 mock.patch.object(rehearsal, "require_root"), \
                 mock.patch.object(rehearsal, "load_provenance", return_value=provenance_value), \
                 mock.patch.object(rehearsal, "systemctl", side_effect=systemctl), \
                 mock.patch.object(
                     rehearsal,
                     "repo_identity",
                     return_value={"head": "2" * 40, "clean": True, "root": "/repo"},
                 ), \
                 mock.patch.object(
                     rehearsal,
                     "verify_manifest",
                     side_effect=lambda m: events.append("verify"),
                 ), \
                 mock.patch.object(
                     rehearsal,
                     "offline_status",
                     return_value={"did": did, "revision": 0, "signaturesUsed": 0, "work": None, "actions": {}},
                 ), \
                 mock.patch.object(
                     rehearsal,
                     "reviewed_target_manifest",
                     return_value=target_manifest,
                 ), \
                 mock.patch.object(
                     rehearsal,
                     "write_provenance",
                     side_effect=write_provenance,
                 ), \
                 mock.patch.object(
                     rehearsal,
                     "installed_manifest",
                     return_value=old_manifest,
                 ), \
                 mock.patch.object(
                     rehearsal,
                     "update_reviewed_runtime",
                     side_effect=lambda m: events.append("update") or m,
                 ), \
                 mock.patch.object(
                     rehearsal,
                     "verify_effective_units",
                     return_value={"flop-policy-signer.service": {"ok": True}},
                 ), \
                 mock.patch.object(
                     rehearsal,
                     "observe",
                     return_value={"passed": True, "checks": {"didStable": True}},
                 ):
                result = rehearsal.resume_reviewed()

        self.assertTrue(result["passed"])
        self.assertEqual(result["previousRepoHead"], "1" * 40)
        self.assertEqual(result["repoHead"], "2" * 40)
        self.assertIn("update", events)
        self.assertIn("phase:RUNTIME_UPDATE_STARTING", events)
        self.assertIn("phase:RUNTIME_UPDATED", events)
        self.assertIn("phase:RESUME_SOCKETS_STARTED", events)
        self.assertIn("phase:OBSERVED", events)

    def test_resume_reviewed_refuses_mixed_installed_manifest(self):
        old_manifest = {"/old": "a" * 64}
        target_manifest = {"/new": "b" * 64}
        mixed_manifest = {"/old": "a" * 64, "/new": "x" * 64}
        did = "did:key:z6Mk" + "a" * 44
        provenance_value = {
            "schema": 2,
            "mode": "TEST_KEY_ONLY",
            "phase": "SOCKETS_STARTED",
            "repoHead": "1" * 40,
            "installManifest": old_manifest,
            "testDid": did,
        }

        def write_provenance(value, phase, **updates):
            out = dict(value)
            out.update(updates)
            out["phase"] = phase
            return out

        with tempfile.TemporaryDirectory() as td:
            provenance_path = Path(td) / "provenance.json"
            provenance_path.write_text("{}")
            with mock.patch.object(rehearsal, "PROVENANCE", provenance_path), \
                 mock.patch.object(rehearsal, "require_root"), \
                 mock.patch.object(rehearsal, "load_provenance", return_value=provenance_value), \
                 mock.patch.object(
                     rehearsal,
                     "systemctl",
                     return_value=SimpleNamespace(stdout=b""),
                 ), \
                 mock.patch.object(
                     rehearsal,
                     "repo_identity",
                     return_value={"head": "2" * 40, "clean": True, "root": "/repo"},
                 ), \
                 mock.patch.object(rehearsal, "verify_manifest"), \
                 mock.patch.object(
                     rehearsal,
                     "offline_status",
                     return_value={"did": did, "signaturesUsed": 0, "work": None, "actions": {}},
                 ), \
                 mock.patch.object(
                     rehearsal,
                     "reviewed_target_manifest",
                     return_value=target_manifest,
                 ), \
                 mock.patch.object(
                     rehearsal,
                     "write_provenance",
                     side_effect=write_provenance,
                 ), \
                 mock.patch.object(
                     rehearsal,
                     "installed_manifest",
                     return_value=mixed_manifest,
                 ):
                with self.assertRaisesRegex(
                    rehearsal.RehearsalError,
                    "RUNTIME_UPDATE_PARTIAL_MANUAL_REVIEW",
                ):
                    rehearsal.resume_reviewed()

    def test_resume_sockets_started_restarts_stopped_sockets_with_same_safe_state(self):
        did = "did:key:z6Mk" + "a" * 44
        manifest = {"/installed/target": "b" * 64}
        provenance = {
            "schema": 2, "mode": "TEST_KEY_ONLY", "phase": "RESUME_SOCKETS_STARTED",
            "repoHead": "2" * 40, "targetRepoHead": "2" * 40,
            "installManifest": manifest, "targetInstallManifest": manifest,
            "testDid": did,
        }
        status = {"did": did, "revision": 0, "signaturesUsed": 0, "work": None, "actions": {}}
        events = []

        def systemctl(*args, **kwargs):
            events.append((args[0], args[1:]))
            return SimpleNamespace(stdout=b"")

        def write_provenance(value, phase, **updates):
            return {**value, **updates, "phase": phase}

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provenance.json"
            path.write_text("{}")
            with mock.patch.object(rehearsal, "PROVENANCE", path), \
                 mock.patch.object(rehearsal, "require_root"), \
                 mock.patch.object(rehearsal, "load_provenance", return_value=provenance), \
                 mock.patch.object(rehearsal, "systemctl", side_effect=systemctl), \
                 mock.patch.object(rehearsal, "repo_identity", return_value={"head": "2" * 40, "clean": True}), \
                 mock.patch.object(rehearsal, "verify_manifest") as verify, \
                 mock.patch.object(rehearsal, "offline_status", return_value=status) as offline, \
                 mock.patch.object(rehearsal, "write_provenance", side_effect=write_provenance), \
                 mock.patch.object(rehearsal, "observe", return_value={"passed": True, "checks": {"didStable": True}}) as observe, \
                 mock.patch.object(rehearsal, "reviewed_target_manifest") as target, \
                 mock.patch.object(rehearsal, "update_reviewed_runtime") as update, \
                 mock.patch.object(rehearsal, "test_identity") as identity, \
                 mock.patch.object(rehearsal, "handoff_test_seed") as handoff:
                result = rehearsal.resume_reviewed()

        self.assertTrue(result["passed"])
        self.assertEqual(events[0][0], "stop")
        self.assertEqual(events[1], ("start", rehearsal.SOCKETS))
        verify.assert_called_once_with(manifest)
        offline.assert_called_once_with()
        observe.assert_called_once_with()
        for excluded in (target, update, identity, handoff):
            excluded.assert_not_called()

    def test_resume_sockets_started_refuses_unsafe_state_before_socket_start(self):
        did = "did:key:z6Mk" + "a" * 44
        manifest = {"/installed/target": "b" * 64}
        provenance = {
            "schema": 2, "mode": "TEST_KEY_ONLY", "phase": "RESUME_SOCKETS_STARTED",
            "targetRepoHead": "2" * 40, "installManifest": manifest,
            "targetInstallManifest": manifest, "testDid": did,
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provenance.json"
            path.write_text("{}")
            for status in (
                {"did": "wrong", "revision": 0, "signaturesUsed": 0, "work": None, "actions": {}},
                {"did": did, "revision": 1, "signaturesUsed": 0, "work": None, "actions": {}},
                {"did": did, "revision": 0, "signaturesUsed": 1, "work": None, "actions": {}},
                {"did": did, "revision": 0, "signaturesUsed": 0, "work": {"x": 1}, "actions": {}},
                {"did": did, "revision": 0, "signaturesUsed": 0, "work": None, "actions": {"x": 1}},
            ):
                events = []
                with mock.patch.object(rehearsal, "PROVENANCE", path), \
                     mock.patch.object(rehearsal, "require_root"), \
                     mock.patch.object(rehearsal, "load_provenance", return_value=provenance), \
                     mock.patch.object(rehearsal, "systemctl", side_effect=lambda *a, **kw: events.append(a[0])), \
                     mock.patch.object(rehearsal, "repo_identity", return_value={"head": "2" * 40, "clean": True}), \
                     mock.patch.object(rehearsal, "verify_manifest"), \
                     mock.patch.object(rehearsal, "offline_status", return_value=status), \
                     mock.patch.object(rehearsal, "observe") as observe:
                    with self.assertRaisesRegex(rehearsal.RehearsalError, "RUNTIME_RESUME_STATE_UNSAFE"):
                        rehearsal.resume_reviewed()
                    self.assertEqual(events, ["stop"])
                    observe.assert_not_called()

    def test_resume_sockets_started_refuses_head_or_installed_manifest_mismatch(self):
        manifest = {"/installed/target": "b" * 64}
        provenance = {
            "schema": 2, "mode": "TEST_KEY_ONLY", "phase": "RESUME_SOCKETS_STARTED",
            "targetRepoHead": "2" * 40, "installManifest": manifest,
            "targetInstallManifest": manifest, "testDid": "did:key:z6Mk" + "a" * 44,
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provenance.json"
            path.write_text("{}")
            for head, manifest_failure, expected in (
                ("3" * 40, None, "RUNTIME_RESUME_HEAD_MISMATCH"),
                ("2" * 40, rehearsal.RehearsalError("INSTALL_MANIFEST_MISMATCH"), "INSTALL_MANIFEST_MISMATCH"),
            ):
                events = []
                with mock.patch.object(rehearsal, "PROVENANCE", path), \
                     mock.patch.object(rehearsal, "require_root"), \
                     mock.patch.object(rehearsal, "load_provenance", return_value=provenance), \
                     mock.patch.object(rehearsal, "systemctl", side_effect=lambda *a, **kw: events.append(a[0])), \
                     mock.patch.object(rehearsal, "repo_identity", return_value={"head": head, "clean": True}), \
                     mock.patch.object(rehearsal, "verify_manifest", side_effect=manifest_failure), \
                     mock.patch.object(rehearsal, "observe") as observe:
                    with self.assertRaisesRegex(rehearsal.RehearsalError, expected):
                        rehearsal.resume_reviewed()
                    self.assertEqual(events, ["stop"])
                    observe.assert_not_called()


if __name__ == "__main__":
    unittest.main()


# Reusable retained-infrastructure lifecycle tests (also run by signer-ci.yml).
class RetainedInfrastructureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        paths = {
            "INSTALL": root / "install", "CONFIG_DIR": root / "config",
            "SECRET_DIR": root / "secret", "INPUT_DIR": root / "input",
            "STATE_DIR": root / "state", "PROVENANCE": root / "provenance",
            "TRANSIENT_PROBE_STATE": root / "probe",
            "SYSUSERS": root / "sysusers", "TMPFILES": root / "tmpfiles",
            "UNIT_DIR": root / "units", "SRC": root / "source",
        }
        paths.update({
            "INSTALL_SRC": paths["INSTALL"] / "src",
            "INSTALL_DEPLOY": paths["INSTALL"] / "deploy",
            "RUNTIME_NODE": paths["INSTALL"] / "node",
            "CONFIG": paths["CONFIG_DIR"] / "config.json",
            "CREDENTIAL": paths["SECRET_DIR"] / "project-seed.cred",
            "STATE": paths["STATE_DIR"] / "state.json",
            "AUDIT": paths["STATE_DIR"] / "audit.jsonl",
        })
        self.paths = paths
        for key, value in paths.items():
            patch = mock.patch.object(rehearsal, key, value)
            patch.start()
            self.addCleanup(patch.stop)
        for key in ("INSTALL", "INSTALL_SRC", "INSTALL_DEPLOY", "CONFIG_DIR", "SECRET_DIR", "INPUT_DIR", "UNIT_DIR", "SRC"):
            paths[key].mkdir(mode=0o755)
        (paths["SRC"] / "runtime.mjs").write_text("source")
        (paths["INSTALL_SRC"] / "runtime.mjs").write_text("old")
        for name in ("runtime_probe.mjs", "test_identity.mjs", "offline_state_status.mjs", "durability_probe.mjs"):
            (paths["INSTALL_DEPLOY"] / name).write_text("old")
        for path in (paths["RUNTIME_NODE"], paths["SYSUSERS"], paths["TMPFILES"], *(paths["UNIT_DIR"] / n for n in rehearsal.UNITS)):
            path.write_text("old")
        paths["RUNTIME_NODE"].chmod(0o755)
        principal = SimpleNamespace(pw_uid=1001, pw_gid=1001)
        reader = SimpleNamespace(pw_uid=1002, pw_gid=1002)
        acquisition = SimpleNamespace(gr_gid=1003)
        self.patch("retained_principals", return_value=(principal, reader, acquisition))
        self.patch("node_version", return_value="v22.1.0")
        self.patch("historical_install_head", return_value="a" * 40)
        self.patch("stopped_entry_points")
        self.real_metadata = rehearsal.exact_metadata
        def simulated_metadata(path, kind, uid, gid, mode):
            # The fixture cannot chown; verify the real kind and mode here.
            st = path.lstat()
            if ((st.st_mode & 0o7777) != mode or
                (kind == "dir" and not stat.S_ISDIR(st.st_mode)) or
                (kind == "file" and not stat.S_ISREG(st.st_mode))):
                rehearsal.fail("RETAINED_ARTIFACT_UNSAFE")
        self.patch("exact_metadata", side_effect=simulated_metadata)
        paths["CONFIG_DIR"].chmod(0o750)
        paths["SECRET_DIR"].chmod(0o700)
        paths["INPUT_DIR"].chmod(0o2750)

    def patch(self, name, **kwargs):
        patch = mock.patch.object(rehearsal, name, **kwargs)
        value = patch.start()
        self.addCleanup(patch.stop)
        return value

    def test_exact_retained_tree_accepted_and_manifest_recorded(self):
        result = rehearsal.validate_retained_infrastructure()
        self.assertEqual(result["installedHead"], "a" * 40)
        self.assertIn(str(rehearsal.RUNTIME_NODE), result["manifest"])

    def test_protected_artifacts_refused(self):
        for name in ("CREDENTIAL", "CONFIG", "STATE", "AUDIT", "PROVENANCE"):
            with self.subTest(name=name):
                path = self.paths[name]
                path.parent.mkdir(exist_ok=True)
                path.write_text("do not read")
                with self.assertRaisesRegex(rehearsal.RehearsalError, "RETAINED_PROTECTED_ARTIFACT_PRESENT"):
                    rehearsal.validate_retained_infrastructure()
                path.unlink()

    def test_unexpected_files_and_symlinks_refused(self):
        for key in ("INSTALL", "CONFIG_DIR", "SECRET_DIR", "INPUT_DIR"):
            with self.subTest(key=key):
                path = self.paths[key] / "unexpected"
                path.write_text("x")
                with self.assertRaisesRegex(rehearsal.RehearsalError, "CONTENTS_UNEXPECTED"):
                    rehearsal.validate_retained_infrastructure()
                path.unlink()
        path = self.paths["INSTALL_SRC"] / "runtime.mjs"
        path.unlink()
        path.symlink_to(self.paths["SRC"] / "runtime.mjs")
        with self.assertRaises(rehearsal.RehearsalError):
            rehearsal.validate_retained_infrastructure()

    def test_wrong_metadata_and_unreviewed_manifest_refused(self):
        self.paths["RUNTIME_NODE"].chmod(0o777)
        with self.assertRaisesRegex(rehearsal.RehearsalError, "RETAINED_ARTIFACT_UNSAFE"):
            rehearsal.validate_retained_infrastructure()
        self.paths["RUNTIME_NODE"].chmod(0o755)
        with mock.patch.object(rehearsal, "historical_install_head", side_effect=rehearsal.RehearsalError("RETAINED_MANIFEST_UNREVIEWED")):
            with self.assertRaisesRegex(rehearsal.RehearsalError, "UNREVIEWED"):
                rehearsal.validate_retained_infrastructure()

    def test_unit_dropin_refused(self):
        dropin = self.paths["UNIT_DIR"] / (rehearsal.UNITS[0] + ".d")
        dropin.mkdir()
        with self.assertRaises(rehearsal.RehearsalError):
            rehearsal.validate_retained_infrastructure()

    def test_real_metadata_check_rejects_wrong_owner(self):
        with self.assertRaisesRegex(rehearsal.RehearsalError, "RETAINED_ARTIFACT_UNSAFE"):
            self.real_metadata(self.paths["RUNTIME_NODE"], "file", os.getuid() + 1,
                               os.getgid(), 0o755)


class PrepareExistingTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.patchers = []
        for name, result in (
            ("require_root", None),
            ("validate_retained_infrastructure", {"manifest": {"node": "x"}, "installedHead": "a" * 40, "nodeVersion": "v22.1.0"}),
            ("repo_identity", {"head": "b" * 40, "clean": True, "root": "/repo"}),
            ("reviewed_target_manifest", {"node": "x"}),
            ("update_reviewed_runtime", {"node": "x"}),
            ("verify_manifest", None), ("verify_effective_units", {"unit": {"LimitCORE": "0"}}),
            ("finish_prepare", {"passed": True}),
        ):
            self.patch(name, return_value=result)
        self.patch("systemctl")
        self.patch("write_provenance", side_effect=lambda value, phase, **updates: {**value, **updates, "phase": phase})
        self.patch("RUNTIME_NODE", new="node")

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def patch(self, name, **kwargs):
        patcher = mock.patch.object(rehearsal, name, **kwargs)
        self.patchers.append(patcher)
        return patcher.start()

    def test_update_before_existing_rehearsal_flow(self):
        self.assertTrue(rehearsal.prepare_existing()["passed"])
        rehearsal.update_reviewed_runtime.assert_called_once()
        rehearsal.verify_manifest.assert_called_once()
        rehearsal.systemctl.assert_called_once_with("daemon-reload")
        self.assertEqual(rehearsal.finish_prepare.call_args.args[2], "TEST_KEY_RUNTIME_REHEARSAL_PREPARE_EXISTING")
        self.assertEqual(rehearsal.write_provenance.call_args_list[0].args[1], "RUNTIME_UPDATE_STARTING")

    def test_dirty_checkout_and_target_mismatch_refuse_before_update(self):
        for failed in ("repo_identity", "reviewed_target_manifest"):
            with self.subTest(failed=failed), mock.patch.object(rehearsal, failed, side_effect=rehearsal.RehearsalError("REFUSED")):
                with self.assertRaisesRegex(rehearsal.RehearsalError, "REFUSED"):
                    rehearsal.prepare_existing()
                rehearsal.update_reviewed_runtime.assert_not_called()
        with mock.patch.object(rehearsal, "reviewed_target_manifest", return_value={"node": "different"}):
            with self.assertRaisesRegex(rehearsal.RehearsalError, "NODE_MISMATCH"):
                rehearsal.prepare_existing()

    def test_partial_update_stops_before_seed(self):
        with mock.patch.object(rehearsal, "update_reviewed_runtime", side_effect=rehearsal.RehearsalError("RUNTIME_UPDATE_MANIFEST_MISMATCH")):
            with self.assertRaisesRegex(rehearsal.RehearsalError, "MANIFEST_MISMATCH"):
                rehearsal.prepare_existing()
            rehearsal.finish_prepare.assert_not_called()
            rehearsal.systemctl.assert_not_called()


class EntryPointTests(unittest.TestCase):
    def test_partial_update_provenance_blocks_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provenance.json"
            path.write_text("{}")
            value = {"schema": 2, "mode": "TEST_KEY_ONLY", "entryMode": "PREPARE_EXISTING",
                     "phase": "RUNTIME_UPDATE_STARTING"}
            with mock.patch.object(rehearsal, "PROVENANCE", path), \
                 mock.patch.object(rehearsal, "require_root"), \
                 mock.patch.object(rehearsal, "load_provenance", return_value=value), \
                 mock.patch.object(rehearsal, "systemctl") as ctl:
                with self.assertRaisesRegex(rehearsal.RehearsalError, "PARTIAL_MANUAL_REVIEW"):
                    rehearsal.cleanup_test_key()
                ctl.assert_not_called()
                self.assertTrue(path.exists())

    def test_active_unit_or_pending_job_refused(self):
        def properties(unit, names):
            return {
                "LoadState": "loaded", "ActiveState": "inactive", "Job": "0",
                "MainPID": "0", "ControlPID": "0",
                "UnitFileState": "static" if unit.endswith("bootstrap.service") else "disabled",
                "FragmentPath": str(rehearsal.UNIT_DIR / unit), "DropInPaths": "",
            }
        with mock.patch.object(rehearsal, "systemctl_properties", side_effect=properties):
            rehearsal.stopped_entry_points()
        for changed in ({"ActiveState": "active"}, {"Job": "123"},
                        {"MainPID": "123"}, {"UnitFileState": "enabled"},
                        {"DropInPaths": "/unexpected/override.conf"}):
            with self.subTest(changed=changed):
                def bad(unit, names):
                    value = properties(unit, names)
                    if unit == rehearsal.UNITS[0]:
                        value.update(changed)
                    return value
                with mock.patch.object(rehearsal, "systemctl_properties", side_effect=bad):
                    with self.assertRaisesRegex(rehearsal.RehearsalError, "RETAINED_RUNTIME_NOT_STOPPED"):
                        rehearsal.stopped_entry_points()

    def test_principal_mismatch_refused(self):
        users = {
            "flop-signer": SimpleNamespace(pw_name="flop-signer", pw_uid=1001, pw_gid=1001,
                                            pw_dir=str(rehearsal.STATE_DIR), pw_shell="/usr/sbin/nologin"),
            "flop-signer-reader": SimpleNamespace(pw_name="flop-signer-reader", pw_uid=1002,
                                                   pw_gid=1002, pw_dir=str(rehearsal.INPUT_DIR),
                                                   pw_shell="/usr/sbin/nologin"),
        }
        groups = {
            "flop-signer": SimpleNamespace(gr_name="flop-signer", gr_gid=1001, gr_mem=[]),
            "flop-signer-reader": SimpleNamespace(gr_name="flop-signer-reader", gr_gid=1002, gr_mem=[]),
            "flop-signer-acquisition": SimpleNamespace(gr_name="flop-signer-acquisition", gr_gid=1003,
                                                       gr_mem=["flop-signer", "flop-signer-reader"]),
            "flop-agent": SimpleNamespace(gr_name="flop-agent", gr_gid=1004, gr_mem=[]),
        }
        with mock.patch.object(rehearsal.pwd, "getpwnam", side_effect=users.__getitem__), \
             mock.patch.object(rehearsal.grp, "getgrnam", side_effect=groups.__getitem__), \
             mock.patch.object(rehearsal.grp, "getgrall", side_effect=lambda: list(groups.values())):
            rehearsal.retained_principals()
            groups["flop-signer-acquisition"].gr_mem.remove("flop-signer-reader")
            with self.assertRaisesRegex(rehearsal.RehearsalError, "RETAINED_PRINCIPAL_MISMATCH"):
                rehearsal.retained_principals()

    def test_partial_update_provenance_blocks_resume_before_host_actions(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provenance.json"
            path.write_text("{}")
            value = {"schema": 2, "mode": "TEST_KEY_ONLY", "entryMode": "PREPARE_EXISTING",
                     "phase": "RUNTIME_UPDATE_STARTING"}
            with mock.patch.object(rehearsal, "PROVENANCE", path), \
                 mock.patch.object(rehearsal, "require_root"), \
                 mock.patch.object(rehearsal, "load_provenance", return_value=value), \
                 mock.patch.object(rehearsal.crash_gate, "operate") as operate, \
                 mock.patch.object(rehearsal, "systemctl") as ctl:
                with self.assertRaisesRegex(rehearsal.RehearsalError, "PARTIAL_MANUAL_REVIEW"):
                    rehearsal.resume_reviewed()
                ctl.assert_not_called()
                operate.assert_not_called()
                self.assertTrue(path.exists())


class HistoricalInstallHeadTests(unittest.TestCase):
    """Exercise the real git lookup against a temporary repository."""

    DEPLOY_NAMES = ("runtime_probe.mjs", "test_identity.mjs", "offline_state_status.mjs",
                    "durability_probe.mjs", "flop-policy-signer.sysusers.conf",
                    "flop-policy-signer.tmpfiles.conf", *rehearsal.UNITS)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        self.component = self.repo / "components" / "signer"
        (self.component / "src").mkdir(parents=True)
        (self.component / "deploy").mkdir()
        host = base / "host"
        paths = {
            "COMPONENT": self.component,
            "INSTALL_SRC": host / "install" / "src",
            "INSTALL_DEPLOY": host / "install" / "deploy",
            "RUNTIME_NODE": host / "install" / "node",
            "SYSUSERS": host / "sysusers.conf",
            "TMPFILES": host / "tmpfiles.conf",
            "UNIT_DIR": host / "units",
        }
        for key, value in paths.items():
            patcher = mock.patch.object(rehearsal, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for key in ("INSTALL_SRC", "INSTALL_DEPLOY", "UNIT_DIR"):
            paths[key].mkdir(parents=True)
        paths["RUNTIME_NODE"].write_text("node")
        self.git("init", "-q", "-b", "main")

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
             "-c", "commit.gpgsign=false", "-C", str(self.repo), *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()

    def commit(self, version, extra_src=()):
        for path in (self.component / "src").glob("*.mjs"):
            path.unlink()
        for name in ("runtime.mjs", *extra_src):
            (self.component / "src" / name).write_text(f"{name} {version}")
        for name in self.DEPLOY_NAMES:
            (self.component / "deploy" / name).write_text(f"{name} {version}")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", version)
        return self.git("rev-parse", "HEAD")

    def install(self, version, overrides=None, extra_src=()):
        overrides = overrides or {}
        for path in rehearsal.INSTALL_SRC.glob("*.mjs"):
            path.unlink()
        for name in ("runtime.mjs", *extra_src):
            (rehearsal.INSTALL_SRC / name).write_text(f"{name} {overrides.get(name, version)}")
        targets = {
            **{name: rehearsal.INSTALL_DEPLOY / name for name in self.DEPLOY_NAMES[:4]},
            "flop-policy-signer.sysusers.conf": rehearsal.SYSUSERS,
            "flop-policy-signer.tmpfiles.conf": rehearsal.TMPFILES,
            **{name: rehearsal.UNIT_DIR / name for name in rehearsal.UNITS},
        }
        for name, target in targets.items():
            target.write_text(f"{name} {overrides.get(name, version)}")
        return {str(path): "sha" for path in rehearsal.runtime_artifacts()}

    def history_with_merged_branch(self):
        base = self.commit("base")
        self.git("checkout", "-q", "-b", "feature")
        branch_first = self.commit("branch-first")
        branch_second = self.commit("branch-second")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "merge", "feature")
        return base, branch_first, branch_second

    def test_merged_branch_commit_accepted(self):
        _, branch_first, _ = self.history_with_merged_branch()
        manifest = self.install("branch-first")
        self.assertEqual(rehearsal.historical_install_head(manifest), branch_first)

    def test_mixed_commit_files_refused(self):
        self.history_with_merged_branch()
        manifest = self.install("branch-first", {"runtime_probe.mjs": "base"})
        with self.assertRaisesRegex(rehearsal.RehearsalError, "RETAINED_MANIFEST_UNREVIEWED"):
            rehearsal.historical_install_head(manifest)

    def test_extra_or_missing_source_refused(self):
        self.commit("base")
        self.commit("with-extra", extra_src=("extra.mjs",))
        for version, extra in (("base", ("extra.mjs",)), ("with-extra", ())):
            with self.subTest(version=version):
                manifest = self.install(version, extra_src=extra)
                with self.assertRaisesRegex(rehearsal.RehearsalError, "RETAINED_MANIFEST_UNREVIEWED"):
                    rehearsal.historical_install_head(manifest)

    def test_non_ancestor_commit_refused(self):
        self.commit("base")
        self.git("checkout", "-q", "-b", "unmerged")
        self.commit("unmerged")
        self.git("checkout", "-q", "main")
        manifest = self.install("unmerged")
        with self.assertRaisesRegex(rehearsal.RehearsalError, "RETAINED_MANIFEST_UNREVIEWED"):
            rehearsal.historical_install_head(manifest)

    def test_manifest_shape_mismatch_refused(self):
        self.commit("base")
        manifest = self.install("base")
        manifest.pop(str(rehearsal.RUNTIME_NODE))
        with self.assertRaisesRegex(rehearsal.RehearsalError, "RETAINED_MANIFEST_SHAPE_MISMATCH"):
            rehearsal.historical_install_head(manifest)
