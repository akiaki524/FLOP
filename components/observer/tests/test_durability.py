"""Offline process durability plus negative harness/boundary regression tests."""

import ast
import copy
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import durability_payload as payload
import run_durability as harness
from technocore_observer.protocol import ObserverError
from technocore_observer.storage import APPLICATION_ID, SCHEMA_VERSION, _private


def container_config():
    return {"Image": "sha256:" + "a" * 64, "Id": "fixture-container-id",
            "Config": {"User": "65532:65532", "Env": ["HOME=/nonexistent", "PYTHONDONTWRITEBYTECODE=1"],
                       "Entrypoint": harness.ENTRYPOINT, "Cmd": [],
                       "Labels": {harness.PURPOSE: "test"}},
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
                           "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
                           "PidsLimit": 32, "Memory": 256 * 1024 * 1024,
                           "RestartPolicy": {"Name": "no"}, "LogConfig": {"Type": "none"},
                           "Mounts": [{"Type": "volume", "Source": "test-volume", "Target": "/state"}]},
            "Mounts": [{"Type": "volume", "Name": "test-volume", "Destination": "/state",
                        "Driver": "local", "RW": True}]}


class ProcessDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".observer-test-durability-", dir=harness.ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.backend = harness.LocalBackend(self.directory)
        self.addCleanup(self.backend.cleanup, False)

    def test_real_process_matrix_and_backup(self):
        matrix = harness.Matrix(self.backend)
        cases = matrix.run()
        self.assertEqual(len(cases), 31)
        self.assertTrue(all(case["status"] == "PASS" for case in cases))
        signals = [event for event in self.backend.log if event.get("event") == "signal_exit"]
        self.assertEqual(len(signals), 19)
        self.assertEqual(sum(event["signal"] == "SIGTERM" for event in signals), 2)
        self.assertEqual(sum(event["signal"] == "SIGKILL" for event in signals), 17)
        backups = list(self.directory.glob("*-backup.sqlite"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        with contextlib.closing(sqlite3.connect(backups[0])) as conn:
            self.assertEqual(conn.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchall(), [("ok",)])
            self.assertEqual(conn.execute("SELECT poll_seq,resolved_seq,status FROM state").fetchone(),
                             (301, 103, "DEGRADED"))
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 9)
            self.assertEqual(conn.execute("SELECT count(*) FROM gaps").fetchone()[0], 2)
            row = conn.execute("SELECT seq,text_value,text_sha256 FROM messages ORDER BY seq LIMIT 1").fetchone()
            self.assertEqual(row[1], payload.record(row[0])["text"])
            self.assertEqual(row[2], hashlib.sha256(row[1].encode()).hexdigest())
        safe_output = json.dumps(self.backend.log, ensure_ascii=True)
        for control in ("\x1b", "\x00", "\u202e"):
            self.assertNotIn(control, safe_output)
        self.assertNotIn("fixture-101", safe_output)

    def test_resume_with_missing_db_does_not_init(self):
        path = self.backend.state / "empty-state"
        path.mkdir(mode=0o700)
        session = self.backend.spawn()
        result = session.rpc("empty-state", "open", ok=False)
        self.assertEqual(result["error_code"], "STATE_DB_MISSING")
        self.assertEqual(result["fixture_calls"], 0)
        self.assertFalse((path / "state.sqlite").exists())
        session.finish()

    def test_missing_wal_after_clean_close_is_valid(self):
        session = self.backend.spawn()
        session.rpc("wal-clean", "init")
        session.rpc("wal-clean", "open")
        session.rpc("wal-clean", "poll", fixture="contiguous")
        session.finish()
        self.assertFalse((self.backend.state / "wal-clean" / "state.sqlite-wal").exists())
        restarted = self.backend.spawn()
        self.assertEqual(restarted.rpc("wal-clean", "open")["summary"]["core"]["poll_seq"], 103)
        restarted.finish()

    def test_invalid_owner_rejected_without_chown(self):
        with patch("technocore_observer.storage.os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(ObserverError, "UNSAFE_STATE_PATH"):
                _private(self.backend.state, directory=True)

    def test_arbitrary_case_and_fixture_rejected(self):
        session = self.backend.spawn()
        result = session.rpc("../escape", "init", ok=False)
        self.assertEqual(result["error_code"], "CASE_REJECTED")
        session.rpc("fixed", "init")
        session.rpc("fixed", "open")
        result = session.rpc("fixed", "poll", fixture="unknown", ok=False)
        self.assertEqual(result["fixture_calls"], 0)
        session.finish()


class BoundaryTests(unittest.TestCase):
    def test_mount_flags_fail_closed(self):
        line = "44 31 8:1 /volume /state rw,nosuid,nodev,noexec,relatime - ext4 /dev/sda rw"
        self.assertTrue(all(payload.mount_checks(line).values()))
        ordinary = line.replace("nosuid,nodev,noexec,", "")
        checks = payload.mount_checks(ordinary)
        self.assertFalse(checks["noexec"])
        self.assertFalse(checks["nosuid"])
        self.assertFalse(checks["nodev"])
        for text in ("", line + "\n" + line):
            with self.assertRaisesRegex(RuntimeError, "STATE_MOUNT_COUNT"):
                payload.mount_checks(text)

    def test_volume_identity_driver_and_options(self):
        config = {"Name": "test-volume", "Driver": "local", "Scope": "local",
                  "Options": None, "Labels": {harness.PURPOSE: "test"}}
        self.assertTrue(all(harness.volume_checks(config, "test-volume", "test").values()))
        for key, value in (("Name", "other-volume"), ("Driver", "nfs"), ("Scope", "global"),
                           ("Options", {"device": "/host/path"}), ("Labels", {})):
            modified = {**config, key: value}
            self.assertFalse(all(harness.volume_checks(modified, "test-volume", "test").values()), key)

    def test_container_boundary_inspector(self):
        config = container_config()
        check = lambda item: harness.configuration_checks(item, config["Image"], "test-volume", "test")
        self.assertTrue(all(check(config).values()))
        for field, value in (("NetworkMode", "host"), ("Privileged", True), ("ReadonlyRootfs", False),
                             ("CapAdd", ["SYS_ADMIN"]), ("PidMode", "host"), ("IpcMode", "host"),
                             ("Binds", ["/home:/home"]), ("VolumesFrom", ["another"]),
                             ("Tmpfs", {"/tmp": "rw"}), ("Devices", [{"PathOnHost": "/dev/sda"}]),
                             ("SecurityOpt", ["seccomp=unconfined"]), ("Memory", 0),
                             ("PortBindings", {"80/tcp": []}), ("ExtraHosts", ["example:1.2.3.4"])):
            modified = copy.deepcopy(config)
            modified["HostConfig"][field] = value
            self.assertFalse(all(check(modified).values()), field)
        for field, value in (("User", "0"), ("Env", ["HOME=/root", "SSH_AUTH_SOCK=/socket"]),
                             ("Entrypoint", ["sh"]), ("Labels", {})):
            modified = copy.deepcopy(config)
            modified["Config"][field] = value
            self.assertFalse(all(check(modified).values()), field)
        modified = copy.deepcopy(config)
        modified["Mounts"][0]["Name"] = "wrong-volume"
        self.assertFalse(all(check(modified).values()))
        modified["Mounts"].append({"Type": "bind", "Destination": "/var/run/docker.sock"})
        self.assertFalse(all(check(modified).values()))

    def test_container_create_has_no_pull_or_host_mount(self):
        isolation = harness.isolation
        expected = str(Path(isolation.pwd.getpwuid(os.getuid()).pw_dir))
        self.assertTrue(Path(expected).is_absolute())
        self.assertTrue(Path(expected).is_dir())
        producers = (
            lambda: isolation.create_command("test-image", "test-name"),
            lambda: harness.container_args("test-image", "test-name", "test-volume", "test"),
        )
        self.assertEqual(isolation.host_home(), expected)
        for value in ("/nonexistent", "/var/empty", "/", "relative/path", None):
            with self.subTest(home=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = value
                self.assertEqual(isolation.host_home(), expected)
                for producer in producers:
                    self.assertEqual([arg for arg in producer()
                                      if arg.startswith("--env=OBSERVER_HOST_HOME=")],
                                     ["--env=OBSERVER_HOST_HOME=" + expected])
        for value in ("", "relative/path", "/", None, "\x00"):
            with self.subTest(account_home=value), patch.object(
                    isolation.pwd, "getpwuid", return_value=Mock(pw_dir=value)):
                for producer in producers:
                    with self.assertRaisesRegex(RuntimeError, "HOST_HOME_UNSAFE"):
                        producer()
        for error in (KeyError("missing account"), OSError("account lookup unavailable")):
            with self.subTest(error=type(error).__name__), patch.object(
                    isolation.pwd, "getpwuid", side_effect=error):
                for producer in producers:
                    with self.assertRaisesRegex(RuntimeError, "HOST_HOME_UNSAFE"):
                        producer()
        # Public repository fixtures only; never read or enumerate HOME contents.
        with tempfile.TemporaryDirectory(prefix=".observer-test-home-", dir=harness.ROOT) as root:
            for value in (str(Path(root) / "missing"), __file__):
                with self.subTest(invalid_path=value), patch.object(
                        isolation.pwd, "getpwuid", return_value=Mock(pw_dir=value)):
                    for producer in producers:
                        with self.assertRaisesRegex(RuntimeError, "HOST_HOME_UNSAFE"):
                            producer()
        with patch.object(Path, "is_dir", side_effect=PermissionError):
            for producer in producers:
                with self.assertRaisesRegex(RuntimeError, "HOST_HOME_UNSAFE"):
                    producer()
        args = harness.container_args("sha256:" + "a" * 64, "test-name", "test-volume", "test")
        for required in ("--pull=never", "--network=none", "--user=65532:65532", "--read-only",
                         "--cap-drop=ALL", "--security-opt=no-new-privileges:true"):
            self.assertIn(required, args)
        mounts = [arg for arg in args if arg.startswith("--mount=")]
        self.assertEqual(mounts, ["--mount=type=volume,source=test-volume,target=/state"])
        self.assertFalse(any(arg in ("--privileged", "--network=host", "-v") for arg in args))

    def test_container_recreation_retains_original_volume(self):
        # Orchestration regression, NOT evidence of Docker persistence.
        backend = object.__new__(harness.DockerBackend)
        backend.names = ["container-a"]
        backend.log = []
        backend.volume = "original-volume"
        backend.verify = Mock()
        backend.preserve = Mock()
        backend.docker = Mock()
        backend.spawn = Mock(return_value="container-b")
        session = Mock(name="session")
        session.name = "container-a"
        session.process.poll.return_value = 0
        self.assertEqual(backend.recreate(session), "container-b")
        backend.docker.assert_called_once_with(["rm", "container-a"])
        self.assertEqual(backend.volume, "original-volume")
        backend.verify.assert_called_once_with("container-a")

    def test_backup_uses_sqlite_api_and_no_restore_copy(self):
        tree = ast.parse(harness.PAYLOAD.read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        self.assertTrue(any(isinstance(node.func, ast.Attribute) and node.func.attr == "backup" for node in calls))
        for source in (harness.PAYLOAD, Path(harness.__file__)):
            for node in ast.walk(ast.parse(source.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, ("copy", "copyfile", "copy2", "copytree", "restore"))

    def test_no_network_fallback_or_live_mode(self):
        client = payload.FixtureClient(201, "resume", lambda _: None)
        with self.assertRaisesRegex(RuntimeError, "RESUME_CURSOR_MISMATCH"):
            client.poll(100)
        for name in ("tail", "config"):
            with self.assertRaises(RuntimeError):
                getattr(client, name)()
        client.poll(201)
        with self.assertRaisesRegex(RuntimeError, "FIXTURE_CALL_LIMIT"):
            client.poll(201)
        for event in ("socket.__new__", "socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system"):
            with self.assertRaisesRegex(RuntimeError, "OFFLINE_BOUNDARY_VIOLATION"):
                payload.deny_network(event, ())
        self.assertEqual(harness.plan()["external_requests"], 0)
        self.assertFalse(harness.plan()["live_executed"])

    def test_logical_comparator_rejects_changed_rows_gaps_state(self):
        value = {"state": {"poll_seq": 201}, "messages_sha256": "a", "gaps_sha256": "b", "events": []}
        harness.same(value, copy.deepcopy(value))
        for key in value:
            with self.assertRaises(RuntimeError):
                harness.same(value, {**value, key: "changed"})


if __name__ == "__main__":
    unittest.main()
