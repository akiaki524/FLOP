"""Offline identity and monitor-surface checks for the minimal Capture import."""

import ast
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy/production-capture-human"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HELPER))
import monitor
import packet
from technocore_full_capture import production

PROVENANCE = ROOT / "deploy/capture-candidate-integration.json"
MANIFEST = ROOT / "deploy/production-capture-human/source-manifest.json"
# These adopted candidate files have since evolved, including Writer metrics
# segmentation in production.py and its focused tests. Historical digests stay
# in the immutable provenance/manifest artifacts.
EVOLVED_CURRENT_FILES = {"deploy/production-capture-human/monitor.py",
                         "src/technocore_full_capture/production.py",
                         "tests/test_capture_production.py",
                         "src/technocore_full_capture/capture_first.py",
                         "src/technocore_full_capture/spool.py",
                         "src/technocore_full_capture/deadline.py",
                         "src/technocore_full_capture/recovery.py",
                         "src/technocore_observer/http.py"}
PACKET_ATTRIBUTES = {
    "E", "mounted_check", "read_json", "receipt", "require", "require_host",
    "stop_roles", "sync_dir", "write_new",
}


def packet_attributes(source):
    tree = ast.parse(source)
    return {node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == "packet"}


def reviewed_candidate_bytes(name, current):
    if name == "tests/test_full_capture.py":
        # Reverse only the Observer budget assertions moved to a focused test.
        replacements = (
            (b"from technocore_observer.protocol import ObserverError, POLL_LIMIT, Reply, json_dump\n\nROOT =",
             b"from technocore_observer.protocol import ObserverError, POLL_LIMIT, Reply, json_dump\n"
             b"from technocore_observer.storage import MAX_DB_BYTES, MAX_WAL_BYTES\n\nROOT ="),
            (b"    def test_observer_endpoint_allowlist_unchanged(self):\n"
             b"        with self.assertRaisesRegex(ObserverError, \"ENDPOINT_NOT_ALLOWED\"):",
             b"    def test_observer_budgets_and_allowlist_unchanged(self):\n"
             b"        self.assertEqual(MAX_DB_BYTES, 16 * 1024 * 1024)\n"
             b"        self.assertEqual(MAX_WAL_BYTES, 8 * 1024 * 1024)\n"
             b"        with self.assertRaisesRegex(ObserverError, \"ENDPOINT_NOT_ALLOWED\"):"),
        )
        for updated, reviewed in replacements:
            if current.count(updated) != 1:
                raise AssertionError("unexpected Capture test budget delta")
            current = current.replace(updated, reviewed, 1)
    return current


class CandidateIdentityTests(unittest.TestCase):
    def test_provenance_and_manifest_match_file_bytes(self):
        provenance = json.loads(PROVENANCE.read_bytes())
        self.assertEqual(provenance["version"], 1)
        self.assertEqual(provenance["source_candidate_commit"],
                         "0ed90b21656c32d6dc37a97b6eb7600c95f88fb0")
        adopted = provenance["adopted_source_files"]
        existing = provenance["existing_identical_dependency_files"]
        excluded = provenance["intentionally_excluded_reviewed_files"]
        tests = provenance["candidate_regression_tests"]
        support = provenance["candidate_test_support_files"]
        self.assertEqual((len(adopted), len(existing), len(excluded), len(tests),
                          len(support)), (19, 3, 2, 13, 1))
        self.assertEqual(provenance["source_reviewed_file_count"], 24)
        self.assertEqual(set(provenance["source_reviewed_files"]),
                         set(adopted) | set(existing) | set(excluded))
        self.assertEqual(len(provenance["source_reviewed_files"]), 24)
        self.assertEqual(provenance["integration_added_tests"],
                         ["tests/test_capture_candidate_integration.py"])
        for group in (adopted, existing, tests, support):
            for name, expected in group.items():
                with self.subTest(path=name):
                    path = ROOT / name
                    self.assertTrue(path.is_file() and not path.is_symlink())
                    digest = hashlib.sha256(reviewed_candidate_bytes(name, path.read_bytes())).hexdigest()
                    if name in EVOLVED_CURRENT_FILES:
                        self.assertNotEqual(digest, expected)
                    else:
                        self.assertEqual(digest, expected)
        for name in excluded:
            with self.subTest(excluded=name):
                self.assertFalse((ROOT / name).exists())
        self.assertFalse((ROOT / "deploy/production-capture-upgrade").exists())
        self.assertEqual(hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
                         provenance["source_manifest_sha256"])
        manifest = json.loads(MANIFEST.read_bytes())
        self.assertEqual(len(manifest), provenance["source_manifest_entry_count"])
        self.assertEqual(len(manifest), 17)
        self.assertEqual(set(manifest), (set(adopted) | set(existing))
                         - {"deploy/production-capture-human/source-manifest.json",
                            "deploy/production-capture-human/monitor.py",
                            "deploy/production-capture-human/packet.py",
                            "deploy/production-capture-human/policy.py",
                            "deploy/production-capture-human/readonly.py"})
        for name, expected in manifest.items():
            with self.subTest(manifest=name):
                digest = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                if name in EVOLVED_CURRENT_FILES:
                    self.assertNotEqual(digest, expected)
                else:
                    self.assertEqual(digest, expected)

    def test_packet_surface_is_closed_and_unexpected_access_is_detected(self):
        self.assertTrue(callable(production.validate_config))
        source = (HELPER / "monitor.py").read_text()
        self.assertEqual(packet_attributes(source), PACKET_ATTRIBUTES)
        self.assertNotEqual(packet_attributes(source + "\npacket.unexpected()\n"),
                            PACKET_ATTRIBUTES)

    def test_monitor_exposes_explicit_stop_mount_and_notification_seams(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(monitor, "DIRECTORY", root / "observation"), \
                    patch.object(packet, "require_host"), \
                    patch.object(packet, "mounted_check",
                                 side_effect=lambda: calls.append("mount")), \
                    patch.object(packet, "receipt", return_value={
                        "start_at": 1000, "observation_checkpoint_at": 999999}), \
                    patch.object(monitor, "observers", return_value={}), \
                    patch.object(monitor, "metrics", return_value={"producer": {"gaps": 0}}), \
                    patch.object(monitor, "gap_growth", return_value={}), \
                    patch.object(monitor, "evaluate", return_value=(
                        ["CAPTURE_RESPONSE_STALE"], [])), \
                    patch.object(monitor, "terminal_notification_count", return_value=0), \
                    patch.object(monitor, "save_evaluation",
                                 side_effect=lambda *a: calls.append("evidence")), \
                    patch.object(packet, "stop_roles",
                                 side_effect=lambda *a: calls.append("stop")) as stop, \
                    patch.object(monitor, "replace_json",
                                 side_effect=lambda *a: calls.append("replace")), \
                    patch.object(monitor, "notification_event",
                                 side_effect=lambda *a: calls.append("notification")) as notify, \
                    patch.object(monitor.time, "time", return_value=1001), \
                    contextlib.redirect_stdout(io.StringIO()):
                report = monitor.tick()
        self.assertEqual(calls, ["mount", "evidence", "stop", "replace", "notification"])
        stop.assert_called_once_with(("capture", "archive"))
        notify.assert_called_once()
        self.assertTrue(report["stop_applied"])
        # Candidate tick still invokes the old notification path. V2 must
        # replace this seam without changing the reviewed monitor bytes.
