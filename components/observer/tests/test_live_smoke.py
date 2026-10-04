"""Offline tests of the smoke runner's privilege/network gates; never uses Docker."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import live_smoke
import run_container_smoke as runner


class SmokeTests(unittest.TestCase):
    def test_request_plan_and_public_source_archive(self):
        plan = runner.plan()
        self.assertEqual(plan["max_requests"], 6)
        self.assertFalse(plan["image_pull"])
        self.assertFalse(plan["image_build"])
        self.assertEqual({item["path"] for item in plan["requests"]},
                         {"/config", "/r/" + runner.ROOM, "/r/" + runner.EMPTY_ROOM})
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            path = Path(root) / "observer.zip"
            hashes = runner.make_archive(path)
            with zipfile.ZipFile(path) as archive:
                self.assertIn("technocore_observer/observer.py", archive.namelist())
                self.assertIn("live_smoke.py", archive.namelist())
                self.assertNotIn("isolation_canary.txt", archive.namelist())
                self.assertEqual(set(archive.namelist()), set(hashes))
                self.assertTrue(all(name.endswith(".py") for name in archive.namelist()))

    def test_diagnostic_denies_unapproved_paths_and_queries(self):
        budget = live_smoke.Budget()
        client = live_smoke.DiagnosticClient(live_smoke.ROOM, budget)
        client._opener = Mock()
        bad_requests = [
            ("/rooms", {}), ("/r/" + live_smoke.ROOM + "/export", {}),
            ("/config", {"url": "https://evil.invalid"}),
            ("/r/" + live_smoke.ROOM, {"format": "json", "limit": 1, "n": 1, "text": "write"}),
            ("/r/" + live_smoke.ROOM, {"format": "json", "limit": 201, "n": 1}),
            ("/r/" + live_smoke.ROOM, {"format": "json", "limit": 200, "n": 1, "since": 0, "wait": 999}),
        ]
        for path, params in bad_requests:
            with self.subTest(path=path, params=params), self.assertRaises(RuntimeError):
                client._get(path, params)
        client._opener.open.assert_not_called()
        self.assertEqual(budget.requests, [])
        with self.assertRaises(RuntimeError):
            live_smoke.DiagnosticClient("unconfigured", budget)

    def test_request_budget_is_finite(self):
        budget = live_smoke.Budget()
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(6):
                budget.begin("/config", {})
            with self.assertRaisesRegex(RuntimeError, "BUDGET_EXCEEDED"):
                budget.begin("/config", {})
        self.assertEqual(len(budget.requests), 6)

    def test_noncontainer_uid_blocks_everything(self):
        with patch("sys.argv", ["live_smoke", "live"]), patch("live_smoke.os.getuid", return_value=1000), \
             patch("live_smoke.process_checks") as checks, patch("live_smoke.live_check") as network, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(live_smoke.main(), 1)
            checks.assert_not_called()
            network.assert_not_called()

    def test_offline_failure_blocks_live(self):
        with patch("sys.argv", ["run_container_smoke", "--execute"]), \
             patch("run_container_smoke.Runner") as factory, contextlib.redirect_stderr(io.StringIO()):
            factory.return_value.stage.side_effect = RuntimeError("OFFLINE_FAILED")
            self.assertEqual(runner.main(), 1)
            factory.return_value.stage.assert_called_once_with("offline")

    def test_container_command_preserves_isolation(self):
        for mode in ("offline", "live"):
            args = runner.container_command(runner.BASE_IMAGE, "test-container", mode)
            self.assertIn("--pull=never", args)
            self.assertIn("--read-only", args)
            self.assertIn("--user=65532:65532", args)
            self.assertIn("--cap-drop=ALL", args)
            self.assertIn("--security-opt=no-new-privileges:true", args)
            self.assertIn("--network=" + ("none" if mode == "offline" else "bridge"), args)
            self.assertFalse(any(arg.startswith(("--volume", "--mount", "--privileged", "--pid=host")) for arg in args))

    def test_real_observer_offline_transaction(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            result = live_smoke.offline_check(Path(root))
            self.assertEqual(result["observer_transaction_and_gap"], "PASS")
            self.assertEqual(result["heartbeat"]["poll_seq"], 201)
            self.assertEqual(result["heartbeat"]["resolved_seq"], 100)

    def test_artifact_transport_rejects_path_escape(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("report.json", "{}")
            archive.writestr("../escape", "untrusted")
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            destination = Path(root) / "results"
            with self.assertRaisesRegex(RuntimeError, "NAMES_REJECTED"):
                runner.save_artifacts(data.getvalue(), destination)
            self.assertFalse(destination.exists())

    def test_artifact_transport_writes_private_files(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("report.json", "{}")
            archive.writestr("response-1.body", b"raw untrusted content")
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            destination = Path(root) / "results"
            runner.save_artifacts(data.getvalue(), destination)
            self.assertEqual((destination / "response-1.body").read_bytes(), b"raw untrusted content")
            self.assertEqual((destination / "report.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)

    def test_wire_gets_use_exact_origin_and_save_raw_only_to_artifacts(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.code = 200
        response.headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        response.read.return_value = b'{"version":"0.12.1"}'
        client = live_smoke.DiagnosticClient(live_smoke.ROOM, live_smoke.Budget())
        client._opener = Mock()
        client._opener.open.return_value = response
        with tempfile.TemporaryDirectory(dir=ROOT) as root, \
             patch("live_smoke.ARTIFACTS", Path(root)), contextlib.redirect_stdout(io.StringIO()) as output:
            result = client.config()
            request = client._opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://technocore.chat/config")
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual((Path(root) / "response-1.body").read_bytes(), result.body)
            self.assertNotIn("0.12.1", output.getvalue())

    def test_expected_0121_and_prior_version_drift(self):
        from technocore_observer.observer import Observer, BASELINE_VERSION
        from technocore_observer.protocol import Reply
        from technocore_observer.storage import StateLock, Store, initialize
        self.assertEqual(BASELINE_VERSION, "0.12.1")
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            directory = Path(root)
            with StateLock(directory):
                anchor, raw = live_smoke.fake_reply([100])
                initialize(directory, live_smoke.ROOM, anchor, raw)
                with contextlib.closing(Store(directory, live_smoke.ROOM)) as store:
                    for version, expected in (("0.12.1", "VERSION_CONFIRMED"), ("0.12.0", "VERSION_CHANGED")):
                        client = Mock()
                        client.config.return_value = Reply(200, "application/json", json.dumps({"version": version}).encode())
                        self.assertEqual(Observer(store, client).check_config()["event"], expected)


if __name__ == "__main__":
    unittest.main()
