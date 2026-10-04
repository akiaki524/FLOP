"""Offline tests for redaction and evidence-before-rejection ordering."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import durability_diagnostics as d
import run_durability as h
from test_durability import container_config


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".observer-test-diagnostic-", dir=h.ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = container_config()
        self.config["Id"] = "a" * 64

    def evidence(self, config):
        checks = h.configuration_checks(config, self.config["Image"], "test-volume", "test")
        return d.diagnostic(config, self.config["Image"], "test-volume", "test", checks, "configuration", "test-container",
                            h.ENTRYPOINT, h.isolation.ALLOWED_ENV)

    def backend(self, config):
        backend = h.DockerBackend(self.directory, self.config["Image"], "test")
        backend.volume = "test-volume"
        backend.names = ["test-container"]
        backend.verify_volume = Mock()
        backend.docker = Mock(return_value=json.dumps([config]).encode())
        return backend

    def test_all_checks_have_observation_and_expectation(self):
        evidence = self.evidence(self.config)
        self.assertEqual(evidence["failed_checks"], [])
        self.assertEqual(set(evidence["checks"]), set(evidence["expected"]))
        self.assertEqual(set(evidence["checks"]), set(evidence["observed"]))
        self.assertEqual(evidence["container_id"], "a" * 64)

    def test_extra_label_zero(self):
        evidence = self.evidence(self.config)
        self.assertEqual(evidence["extra_label_keys"], [])
        self.assertTrue(evidence["extra_label_keys_complete"])
        self.assertTrue(evidence["observed"]["exact_labels"]["purpose_value_matches"])

    def test_extra_label_one_and_values_not_written_to_artifact(self):
        marker = "FIXTURE_LABEL_VALUE_MUST_NOT_BE_SAVED"
        self.config["Config"]["Labels"]["fixture.example/metadata"] = marker
        backend = self.backend(self.config)
        with self.assertRaisesRegex(RuntimeError, "CONTAINER_CONFIGURATION_REJECTED"):
            backend.verify("test-container")
        serialized = (self.directory / "configuration-0001.json").read_text()
        evidence = json.loads(serialized)
        self.assertEqual(evidence["extra_label_keys"], ["fixture.example/metadata"])
        self.assertEqual(evidence["failed_checks"], ["exact_labels"])
        self.assertNotIn(marker, serialized)

    def test_extra_label_multiple_keys_only(self):
        self.config["Config"]["Labels"].update({"fixture.z": "VALUE_Z", "fixture.a": {"nested": "VALUE_A"}})
        evidence = self.evidence(self.config)
        self.assertEqual(evidence["extra_label_keys"], ["fixture.a", "fixture.z"])
        self.assertTrue(evidence["extra_label_keys_complete"])
        self.assertNotIn("VALUE_", json.dumps(evidence))
        self.assertFalse(evidence["checks"]["exact_labels"])

    def test_expected_label_value_is_compared_but_not_saved(self):
        identity = "EXPECTED_LABEL_VALUE_MUST_NOT_BE_SAVED"
        self.config["Config"]["Labels"] = {h.PURPOSE: identity}
        checks = h.configuration_checks(self.config, self.config["Image"], "test-volume", identity)
        evidence = d.diagnostic(self.config, self.config["Image"], "test-volume", identity, checks,
                                "configuration", "test-container", h.ENTRYPOINT, h.isolation.ALLOWED_ENV)
        self.assertTrue(evidence["observed"]["exact_labels"]["purpose_value_matches"])
        self.assertNotIn(identity, json.dumps(evidence))

    def test_label_key_type_length_and_controls(self):
        invalid = {1: "value", "x" * 257: "value", "bad\nkey": "value", "bad\u202ekey": "value", "": "value"}
        result = d.label_key_details(invalid)
        self.assertEqual(result["extra_label_keys"], [])
        self.assertFalse(result["extra_label_keys_complete"])
        self.assertEqual(set(result["label_key_diagnostic_flags"]),
                         {"NON_STRING_LABEL_KEY", "INVALID_LABEL_KEY_LENGTH", "UNSAFE_LABEL_KEY_CHARACTERS"})
        self.assertEqual(d.label_key_details({"x" * 256: "value"})["extra_label_keys"], ["x" * 256])
        self.assertFalse(d.label_key_details([])["extra_label_keys_complete"])

    def test_label_key_count_is_bounded_and_incomplete_is_explicit(self):
        result = d.label_key_details({"fixture.%03d" % i: "value" for i in range(65)})
        self.assertEqual(len(result["extra_label_keys"]), 64)
        self.assertEqual(result["label_key_diagnostic_flags"], ["EXTRA_LABEL_KEYS_LIMIT"])

    def test_reviewed_desktop_key_is_optional_and_value_is_ignored(self):
        baseline = self.evidence(self.config)
        self.assertEqual(len(baseline["checks"]), 21)
        key = "desktop.docker.io/wsl-distro"
        for value in ("Ubuntu", "ANOTHER_FIXTURE_VALUE", "", None, {"ignored": True}):
            config = copy.deepcopy(self.config)
            config["Config"]["Labels"][key] = value
            evidence = self.evidence(config)
            self.assertTrue(evidence["checks"]["exact_labels"])
            self.assertEqual(evidence["extra_label_keys"], [key])
            self.assertEqual(evidence["observed"]["exact_labels"]["unknown_name_count"], 0)
            self.assertEqual(evidence["observed"]["exact_labels"]["allowed_metadata_keys_present"], [key])
            self.assertEqual({k: v for k, v in evidence["checks"].items() if k != "exact_labels"},
                             {k: v for k, v in baseline["checks"].items() if k != "exact_labels"})

    def test_reviewed_metadata_never_substitutes_for_required_purpose(self):
        for labels in ({}, {"desktop.docker.io/wsl-distro": "Ubuntu"},
                       {h.PURPOSE: "wrong", "desktop.docker.io/wsl-distro": "Ubuntu"}):
            config = copy.deepcopy(self.config)
            config["Config"]["Labels"] = labels
            self.assertFalse(self.evidence(config)["checks"]["exact_labels"])

    def test_unknown_extra_and_similar_namespace_keys_still_rejected(self):
        for key in ("desktop.docker.io/unknown", "desktop.docker.io/wsl-distro.extra", "fixture.unknown"):
            config = copy.deepcopy(self.config)
            config["Config"]["Labels"].update({"desktop.docker.io/wsl-distro": "Ubuntu", key: "ignored"})
            evidence = self.evidence(config)
            self.assertEqual(evidence["failed_checks"], ["exact_labels"])
            self.assertEqual(evidence["observed"]["exact_labels"]["unknown_name_count"], 1)

    def test_optional_value_not_saved_and_volume_policy_unchanged(self):
        marker = "OPTIONAL_LABEL_VALUE_MUST_NOT_BE_SAVED"
        self.config["Config"]["Labels"]["desktop.docker.io/wsl-distro"] = marker
        backend = self.backend(self.config)
        backend.verify("test-container")
        serialized = (self.directory / "configuration-0001.json").read_text()
        self.assertNotIn(marker, serialized)
        self.assertTrue(json.loads(serialized)["checks"]["exact_labels"])
        volume = {"Name": "test-volume", "Driver": "local", "Scope": "local",
                  "Labels": self.config["Config"]["Labels"], "Options": None}
        self.assertFalse(h.volume_checks(volume, "test-volume", "test")["exact_labels"])

    def test_failed_options_values_are_recorded_without_relaxing_check(self):
        self.config["HostConfig"]["Mounts"][0]["VolumeOptions"] = {"NoCopy": False}
        evidence = self.evidence(self.config)
        self.assertEqual(evidence["failed_checks"], ["exact_mount_declaration"])
        self.assertFalse(evidence["observed"]["exact_mount_declaration"][0]["VolumeOptions"]["NoCopy"])

    def test_capability_difference_remains_rejected_and_visible(self):
        self.config["HostConfig"]["CapDrop"] = ["CAP_ALL"]
        evidence = self.evidence(self.config)
        self.assertEqual(evidence["failed_checks"], ["capabilities_dropped"])
        self.assertEqual(evidence["observed"]["capabilities_dropped"]["CapDrop"], ["CAP_ALL"])
        self.assertEqual(evidence["expected"]["capabilities_dropped"]["CapDrop"], ["ALL"])

    def test_secret_and_unneeded_inspect_fields_are_not_saved(self):
        marker = "TEST_SECRET_DO_NOT_STORE\x1b\n"
        config = copy.deepcopy(self.config)
        config["Config"]["Env"] += ["TOKEN=" + marker, "HOME=" + marker]
        config["Config"]["Labels"] = {h.PURPOSE: marker, marker: marker}
        config["Config"]["Entrypoint"] = [marker]
        config["Config"]["Cmd"] = [marker]
        config["HostConfig"]["Binds"] = [marker]
        config["HostConfig"]["LogConfig"]["Config"] = {"secret": marker}
        config["HostConfig"]["SecurityOpt"] += [marker]
        config["Mounts"][0]["Source"] = marker
        config["HostConfig"]["Mounts"][0]["VolumeOptions"] = {"Labels": {"token": marker}, "DriverConfig": {"Options": {"token": marker}}}
        config["GraphDriver"] = {"Data": marker}
        evidence = self.evidence(config)
        serialized = json.dumps(evidence)
        self.assertNotIn("TEST_SECRET_DO_NOT_STORE", serialized)
        self.assertNotIn("TOKEN", serialized)
        self.assertNotIn("GraphDriver", serialized)
        self.assertGreater(len(evidence["failed_checks"]), 0)

    def test_rejection_is_saved_before_exception(self):
        self.config["HostConfig"]["NetworkMode"] = "host"
        backend = self.backend(self.config)
        with self.assertRaisesRegex(RuntimeError, "CONTAINER_CONFIGURATION_REJECTED"):
            backend.verify("test-container")
        report = json.loads((self.directory / "configuration-0001.json").read_text())
        self.assertEqual(report["failed_checks"], ["network_none"])
        self.assertEqual(report["observed"]["network_none"], "host")
        self.assertEqual(report["expected"]["network_none"], "none")
        self.assertEqual(report["container_id"], "a" * 64)
        self.assertEqual((self.directory / "configuration-0001.json").stat().st_mode & 0o777, 0o600)

    def test_cleanup_refusal_is_saved_and_does_not_stop_or_remove(self):
        self.config["HostConfig"]["NetworkMode"] = "host"
        backend = self.backend(self.config)
        with self.assertRaisesRegex(RuntimeError, "CONTAINER_CONFIGURATION_REJECTED"):
            backend.cleanup(False)
        report = json.loads((self.directory / "configuration-0001.json").read_text())
        self.assertEqual(report["stage"], "cleanup")
        self.assertEqual(report["failed_checks"], ["network_none"])
        backend.docker.assert_called_once_with(["container", "inspect", "test-container"])

    def test_evidence_write_failure_prevents_start(self):
        backend = self.backend(self.config)
        with patch.object(h, "private_write", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                backend.verify("test-container")
        self.assertEqual(len(backend.docker.call_args_list), 1)


if __name__ == "__main__":
    unittest.main()
