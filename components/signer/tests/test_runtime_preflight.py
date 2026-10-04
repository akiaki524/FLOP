import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import importlib.util

MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "runtime_preflight.py"
SPEC = importlib.util.spec_from_file_location("signer_runtime_preflight", MODULE_PATH)
preflight = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(preflight)


class RuntimePreflightTests(unittest.TestCase):
    def test_preflight_import_does_not_write_bytecode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("runtime_preflight.py", "wsl_crash_gate.py"):
                (root / name).write_bytes((MODULE_PATH.parent / name).read_bytes())
            result = subprocess.run(
                [sys.executable, "-I", str(root / "runtime_preflight.py"), "--help"],
                capture_output=True, timeout=10, check=True)
            self.assertIn(b"--agent-user", result.stdout)
            self.assertEqual({p.name for p in root.iterdir()},
                             {"runtime_preflight.py", "wsl_crash_gate.py"})

    def test_path_metadata_never_reads_file_contents(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "secret"
            path.write_bytes(b"DO-NOT-READ")
            path.chmod(0o600)
            meta = preflight._path_metadata(str(path))
            self.assertTrue(meta["exists"])
            self.assertEqual(meta["kind"], "file")
            self.assertEqual(meta["mode"], "0600")
            self.assertEqual(meta["size"], len(b"DO-NOT-READ"))
            self.assertNotIn("content", meta)

    def test_symlink_is_reported_as_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "target"
            target.write_text("x")
            link = Path(td) / "link"
            link.symlink_to(target)
            meta = preflight._path_metadata(str(link))
            self.assertEqual(meta["kind"], "symlink")

    def test_node_major_parser(self):
        self.assertEqual(preflight._node_major({"version": "v22.22.0"}), 22)
        self.assertEqual(preflight._node_major({"version": "node v22.1.0"}), None)
        self.assertIsNone(preflight._node_major({"version": None}))

    def test_missing_principal_is_safe_metadata(self):
        name = "definitely-not-a-real-user-6d8df9"
        self.assertEqual(preflight._user(name), {"exists": False})
        self.assertEqual(preflight._group(name), {"exists": False})

    def test_runtime_node_metadata_and_version(self):
        safe = {
            "exists": True,
            "kind": "file",
            "uid": 0,
            "gid": 0,
            "owner": "root",
            "group": "root",
            "mode": "0755",
            "size": 1,
        }

        def runner(argv, timeout):
            self.assertEqual(argv, [preflight.RUNTIME_NODE_PATH, "--version"])
            return {"ok": True, "returncode": 0, "stdout": "v22.22.0", "stderr": ""}

        info = preflight._runtime_node_info(safe, runner=runner)
        self.assertTrue(info["present"])
        self.assertTrue(info["safeMetadata"])
        self.assertEqual(preflight._node_major(info), 22)

        unsafe = dict(safe, mode="0775")
        info = preflight._runtime_node_info(unsafe, runner=lambda *_: self.fail("must not execute unsafe runtime"))
        self.assertFalse(info["safeMetadata"])
        self.assertIsNone(info["version"])

        missing = preflight._runtime_node_info({"exists": False})
        self.assertFalse(missing["present"])
        self.assertEqual(missing["path"], preflight.RUNTIME_NODE_PATH)

    def test_collect_is_read_only_and_blocks_existing_credential(self):
        fake_binary = {"present": True, "path": "/usr/bin/fake", "version": "v22.22.0"}
        def binary(name):
            if name == "node":
                return dict(fake_binary)
            return {"present": True, "path": "/usr/bin/" + name, "version": name + " 1"}

        path_map = {
            path: {"exists": False} for path in preflight.PATHS
        }
        path_map["/var/lib/flop-policy-signer-secret/project-seed.cred"] = {
            "exists": True, "kind": "file", "mode": "0600", "size": 100,
        }
        with mock.patch.object(preflight, "_binary_info", side_effect=binary),              mock.patch.object(preflight, "_run", return_value={
                 "ok": True, "returncode": 0, "stdout": "running", "stderr": ""
             }),              mock.patch.object(preflight, "_path_metadata", side_effect=lambda p: path_map[p]),              mock.patch.object(preflight, "_unit_status", return_value={"available": True}),              mock.patch.object(preflight, "_repo_head", return_value="a" * 40),              mock.patch.object(preflight, "_user", return_value={"exists": False}),              mock.patch.object(preflight, "_group", return_value={"exists": False}):
            report = preflight.collect()
        self.assertEqual(report["writes"], 0)
        self.assertFalse(report["sudoUsed"])
        self.assertFalse(report["secretsRead"])
        self.assertIn("EXISTING_SIGNER_CREDENTIAL", report["blockingObservations"])
        self.assertIn(
            "PROVISION_RUNTIME_NODE_FROM_VERIFIED_NODE22",
            report["preparationActions"],
        )
        self.assertFalse(report["readyForTestKeyRehearsalPreparation"])


class CredentialVisibilityTests(unittest.TestCase):
    def test_unknown_credential_existence_blocks_preparation(self):
        fake_binary = {"present": True, "path": "/usr/bin/fake", "version": "v22.22.0"}

        def binary(name):
            if name == "node":
                return dict(fake_binary)
            return {"present": True, "path": "/usr/bin/" + name, "version": name + " 1"}

        path_map = {path: {"exists": False} for path in preflight.PATHS}
        path_map["/var/lib/flop-policy-signer-secret/project-seed.cred"] = {
            "exists": None,
            "error": "EACCES",
        }
        with mock.patch.object(preflight, "_binary_info", side_effect=binary), \
             mock.patch.object(preflight, "_run", return_value={
                 "ok": True, "returncode": 0, "stdout": "running", "stderr": ""
             }), \
             mock.patch.object(preflight, "_path_metadata", side_effect=lambda p: path_map[p]), \
             mock.patch.object(preflight, "_unit_status", return_value={"available": True}), \
             mock.patch.object(preflight, "_repo_head", return_value="b" * 40), \
             mock.patch.object(preflight, "_user", return_value={"exists": False}), \
             mock.patch.object(preflight, "_group", return_value={"exists": False}):
            report = preflight.collect()

        self.assertIn(
            "SIGNER_CREDENTIAL_EXISTENCE_UNKNOWN",
            report["blockingObservations"],
        )
        self.assertFalse(report["readyForTestKeyRehearsalPreparation"])

    def test_missing_runtime_node_is_preparation_action_not_blocker(self):
        fake_binary = {"present": True, "path": "/home/user/.nvm/node", "version": "v22.22.0"}

        def binary(name):
            if name == "node":
                return dict(fake_binary)
            return {"present": True, "path": "/usr/bin/" + name, "version": name + " 1"}

        path_map = {path: {"exists": False} for path in preflight.PATHS}
        with mock.patch.object(preflight, "_binary_info", side_effect=binary), \
             mock.patch.object(preflight, "_run", return_value={
                 "ok": True, "returncode": 0, "stdout": "running", "stderr": ""
             }), \
             mock.patch.object(preflight, "_path_metadata", side_effect=lambda p: path_map[p]), \
             mock.patch.object(preflight, "_unit_status", return_value={"available": True}), \
             mock.patch.object(preflight, "_repo_head", return_value="c" * 40), \
             mock.patch.object(preflight, "_user", return_value={"exists": False}), \
             mock.patch.object(preflight, "_group", return_value={"exists": False}), \
             mock.patch.object(preflight.crash_gate, "status", return_value={
                 "isWsl": True, "safe": False, "corePatternClass": "pipe",
             }):
            report = preflight.collect()

        self.assertFalse(report["crashCapture"]["safe"])
        self.assertEqual(report["crashCapture"]["corePatternClass"], "pipe")
        self.assertEqual(report["blockingObservations"], [])
        self.assertEqual(
            report["preparationActions"],
            ["PROVISION_RUNTIME_NODE_FROM_VERIFIED_NODE22"],
        )
        self.assertTrue(report["readyForTestKeyRehearsalPreparation"])


if __name__ == "__main__":
    unittest.main()
