"""Synthetic regression fixtures; no real owner identifiers."""
import ast
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import public_check_config as config
import check_public_hygiene as tree
import check_public_history as history
import check_public_commit_metadata as metadata

SYNTHETIC = {"usernames": ["retired-fixture-user"],
             "emails": ["retired-owner@example.invalid"],
             "owner_names": ["fixture-owner"]}

class PublicChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "private.json"
        self.path.write_text(json.dumps(SYNTHETIC))
    def output(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            result = fn(*args)
        return result, out.getvalue()
    def test_default_is_explicit_generic(self):
        data, text = self.output(config.load_config, None)
        self.assertEqual(data["emails"], [])
        self.assertIn("DISABLED", text)
        with patch.object(tree, "tracked_files", return_value=[]):
            result, text = self.output(tree.main, [])
        self.assertEqual(result, 0)
        self.assertIn("generic checks only", text)
    def test_forced_tracked_private_config_blocked_before_read(self):
        forbidden = Path(".local/public-check-identifiers.json")
        with patch.object(tree, "tracked_files", return_value=[forbidden]), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("must not read")), \
             patch.object(Path, "read_text", side_effect=AssertionError("must not read")):
            for args in [[], ["--private-config", "unused-fixture.json"]]:
                result, text = self.output(tree.main, args)
                self.assertEqual(result, 1)
                self.assertIn("must not be Git-tracked", text)
                self.assertNotIn("unused-fixture.json", text)
            self.assertTrue(tree.check_file(forbidden))
    def test_configured_values_not_logged(self):
        data, text = self.output(config.load_config, self.path)
        self.assertEqual(data["emails"], SYNTHETIC["emails"])
        self.assertIn("ENABLED", text)
        for value in SYNTHETIC["emails"] + SYNTHETIC["usernames"]:
            self.assertNotIn(value, text)
    def test_invalid_config_fails_closed(self):
        cases = ["{", "[]", '{"usernames":[],"emails":[]}',
                 '{"usernames":["x"],"emails":["y"],"extra":1}',
                 '{"usernames":["x"],"emails":[3]}',
                 '{"usernames":[" "],"emails":["y"]}',
                 '{"usernames":["x"],"emails":["y"],"emails":["z"]}']
        for raw in cases:
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                with self.assertRaises(config.ConfigError):
                    config.load_config(self.path)
        self.path.unlink()
        with self.assertRaises(config.ConfigError):
            config.load_config(self.path)
        with patch.object(Path, "read_text", side_effect=PermissionError):
            with self.assertRaises(config.ConfigError):
                config.load_config(self.path)
    def test_requested_missing_config_cli_errors(self):
        for fn, args in [(tree.main, ["--private-config", str(self.path)+"missing"]),
                         (history.main, ["--private-config", str(self.path)+"missing"]),
                         (metadata.main, ["checker", "--private-config", str(self.path)+"missing", "HEAD"])]:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    fn(args)
            self.assertEqual(error.exception.code, 2)
    def test_tree_and_history_personal_and_generic(self):
        target = Path(self.tmp.name)/"fixture.txt"
        target.write_text(SYNTHETIC["emails"][0])
        literals = {**tree.LITERALS, **config.identifier_literals(SYNTHETIC)}
        self.assertEqual(tree.check_file(target), [])
        found = tree.check_file(target, literals)
        self.assertTrue(found)
        self.assertNotIn(SYNTHETIC["emails"][0], str(found))
        binary = {**history.LITERALS, **config.identifier_literals(SYNTHETIC, True)}
        self.assertTrue(history.find_bytes(target.read_bytes(), binary))
        generic = ("ghp_" + "A"*40).encode()
        self.assertTrue(history.find_bytes(generic))
        target.write_bytes(generic)
        self.assertTrue(tree.check_file(target))
    def test_personalized_history_commit_path(self):
        payload = b"hash\0fixture\0" + SYNTHETIC["emails"][0].encode() + b"\0other\0safe@example.invalid\0body\0"
        with patch.object(history, "git", return_value=payload):
            self.assertTrue(history.scan_commits(config.identifier_literals(SYNTHETIC, True)))
            self.assertEqual(history.scan_commits(), [])
    def metadata_result(self, name, email, configured, committer=False, revisions=None):
        fields = ["hash", "contributor", "public@example.invalid", "contributor", "public@example.invalid"]
        fields[3 if committer else 1] = name
        fields[4 if committer else 2] = email
        args = ["checker"] + (["--private-config", str(self.path)] if configured else []) + (revisions or ["HEAD"])
        with patch.object(metadata.subprocess, "check_output", return_value="\0".join(fields).encode()):
            return self.output(metadata.main, args)
    def test_history_default_and_configured_entrypoints(self):
        with patch.object(history, "reachable_objects", return_value={"hash": "fixture"}), \
             patch.object(history, "object_types", return_value={"hash": ("blob", 50)}), \
             patch.object(history, "scan_commits", return_value=[]), \
             patch.object(history, "cat_blob", return_value=SYNTHETIC["emails"][0].encode()):
            result, text = self.output(history.main, [])
            self.assertEqual(result, 0)
            self.assertIn("DISABLED", text)
            result, text = self.output(history.main, ["--private-config", str(self.path)])
            self.assertEqual(result, 1)
            self.assertIn("ENABLED", text)
            self.assertNotIn(SYNTHETIC["emails"][0], text)
    def test_personalized_tree_entrypoint(self):
        target = Path(self.tmp.name)/"fixture.txt"
        target.write_text(SYNTHETIC["usernames"][0])
        with patch.object(tree, "tracked_files", return_value=[target]):
            result, text = self.output(tree.main, ["--private-config", str(self.path)])
            self.assertEqual(result, 1)
            self.assertNotIn(SYNTHETIC["usernames"][0], text)
    def test_metadata_author_and_committer_personalization(self):
        for committer in [False, True]:
            result, text = self.metadata_result("contributor", SYNTHETIC["emails"][0].upper(), True, committer)
            self.assertEqual(result, 1)
            self.assertNotIn(SYNTHETIC["emails"][0], text)
            self.assertEqual(self.metadata_result("contributor", SYNTHETIC["emails"][0], False, committer)[0], 0)
            self.assertEqual(self.metadata_result(SYNTHETIC["usernames"][0].upper(), "noreply@github.com", True, committer)[0], 1)
    def test_owner_and_revision_contracts(self):
        self.assertEqual(self.metadata_result("akiaki524", "public@example.invalid", False)[0], 1)
        self.assertEqual(self.metadata_result("akiaki524", "noreply@github.com", False, revisions=["BASE","HEAD"])[0], 0)
        self.assertEqual(self.metadata_result("fixture-owner", "public@example.invalid", True)[0], 1)
        with contextlib.redirect_stderr(io.StringIO()):
            for args in [["checker"], ["checker","A","B","C"]]:
                with self.assertRaises(SystemExit) as error:
                    metadata.main(args)
                self.assertEqual(error.exception.code, 2)
    def test_absolute_config_survives_cwd_change(self):
        import os
        old = os.getcwd()
        try:
            os.chdir(self.tmp.name)
            data, _ = self.output(config.load_config, self.path.resolve())
            self.assertEqual(data["emails"], SYNTHETIC["emails"])
        finally:
            os.chdir(old)
    def test_no_embedded_former_constants_or_split_fixture(self):
        for module in [tree, history, metadata, config]:
            source = Path(module.__file__).read_text()
            parsed = ast.parse(source)
            self.assertFalse(any(isinstance(n, ast.Name) and n.id.startswith("FORMER_") for n in ast.walk(parsed)))
        # Demonstrate why searching source text alone misses split constants:
        source = 'value = "retired-fixture-" + "user"'
        self.assertNotIn(SYNTHETIC["usernames"][0], source)
        node = ast.parse(source).body[0].value
        reconstructed = node.left.value + node.right.value
        self.assertEqual(reconstructed, SYNTHETIC["usernames"][0])
        # Moving identifiers to data still preserves detection of their runtime value.
        self.assertTrue(history.find_bytes(reconstructed.encode(), config.identifier_literals(SYNTHETIC, True)))

if __name__ == "__main__":
    unittest.main()
