"""Offline regression for the one-command Docker recreation handoff."""

import contextlib
import copy
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

import durability_payload as payload
import run_container_recreation as runner
import run_durability as h
from test_durability import container_config


class RecreationTests(unittest.TestCase):
    def preflight(self, hardening=False):
        return {key: key in payload.CORE_CHECKS or hardening
                for key in payload.CORE_CHECKS | payload.MOUNT_HARDENING}

    def test_local_exception_is_exact_and_strict_default_remains(self):
        checks = self.preflight()
        self.assertFalse(payload.preflight_policy(checks)["accepted"])
        local = payload.preflight_policy(checks, payload.LOCAL_PROFILE)
        self.assertTrue(local["accepted"])
        self.assertEqual(local["isolation_boundary"], "PASS")
        self.assertEqual(set(local["hardening_warnings"]), payload.MOUNT_HARDENING)
        self.assertTrue(all(checks[key] is False for key in payload.MOUNT_HARDENING))
        for key in payload.CORE_CHECKS:
            with self.subTest(key=key):
                result = payload.preflight_policy({**checks, key: False}, payload.LOCAL_PROFILE)
                self.assertFalse(result["accepted"])
                self.assertIn(key, result["failed_checks"])

    def test_missing_unknown_and_non_boolean_probe_checks_fail_closed(self):
        checks = self.preflight()
        variants = [None, {**checks, "unknown": True}]
        for key in checks:
            missing = dict(checks)
            del missing[key]
            variants.append(missing)
            variants.extend({**checks, key: value} for value in (None, 1, "true"))
        for variant in variants:
            self.assertFalse(payload.preflight_policy(variant, payload.LOCAL_PROFILE)["accepted"])
        with self.assertRaisesRegex(RuntimeError, "UNKNOWN_VALIDATION_PROFILE"):
            payload.preflight_policy(checks, "unknown")

    def report_events(self, hardening=False):
        events = []
        for name in ("a", "b"):
            checks = self.preflight(hardening)
            policy = payload.preflight_policy(checks, payload.LOCAL_PROFILE)
            events.extend([{"container": name, "preflight": checks, "preflight_policy": policy,
                            "host_preflight_policy": policy},
                           {"container": name, "event": "container_configuration_checked", "checks": {"fixture": True}},
                           {"container": name, "event": "container_verified"},
                           {"container": name, "event": "process_ready"}])
        return events

    def test_local_report_separates_durability_boundary_and_hardening(self):
        cases = [{"case": "named-volume-recreation", "status": "PASS"}]
        events = self.report_events()
        report = runner.local_results(events, cases)
        self.assertEqual(report["isolation_boundary"], "PASS")
        self.assertEqual(report["persistent_storage_durability"], "PASS")
        self.assertEqual(report["production_hardening_gate"], "PENDING")
        for flag in ("noexec", "nosuid", "nodev"):
            self.assertEqual(report["mount_hardening_" + flag], "WARN")
        self.assertEqual(runner.success_status(report), "LOCAL_DURABILITY_PASS_WITH_HARDENING_WARNINGS")
        self.assertEqual(runner.success_status(runner.local_results(self.report_events(True), cases)), "LOCAL_DURABILITY_PASS")
        self.assertEqual(runner.local_results(events, [])["persistent_storage_durability"], "PENDING")
        self.assertEqual(runner.local_results(events[:4], cases)["isolation_boundary"], "PENDING")
        events.append({"event": "container_configuration_checked", "checks": {"exact_labels": False}, "stage": "cleanup"})
        rejected = runner.local_results(events, cases)
        self.assertEqual(rejected["isolation_boundary"], "FAIL")
        self.assertEqual(rejected["mount_hardening_noexec"], "WARN")
        with self.assertRaisesRegex(RuntimeError, "LOCAL_DURABILITY_EVIDENCE_INCOMPLETE"):
            runner.success_status(rejected)

    def test_host_gate_checks_profile_and_never_skips_preflight(self):
        backend = runner.RecreationBackend(self.directory, "test")
        ready = {"ready": True, "source_hashes": payload.source_hashes()}
        for variant in ("pass", "core_failure", "policy_mismatch", "missing_probe"):
            session = object.__new__(h.Session)
            session.backend, session.name, session.command = backend, "fixture", ["unused"]
            checks = self.preflight()
            if variant == "core_failure":
                checks["only_state_persistent_writable"] = False
            policy = payload.preflight_policy(checks, payload.LOCAL_PROFILE)
            event = {"preflight": checks, "preflight_policy": policy}
            if variant == "policy_mismatch":
                event["preflight_policy"] = payload.preflight_policy(checks)
            session.read = Mock(side_effect=[ready] if variant == "missing_probe" else [event, ready])
            with patch.object(h.subprocess, "Popen"):
                if variant == "pass":
                    session.start()
                else:
                    with self.assertRaises(RuntimeError):
                        session.start()
                    self.assertEqual(session.read.call_count, 1)

    def test_local_entrypoint_preserves_all_configuration_checks(self):
        backend = runner.RecreationBackend(self.directory, "test")
        config = container_config()
        config["Config"]["Entrypoint"] = backend.entrypoint
        checks = h.configuration_checks(config, config["Image"], "test-volume", "test", backend.entrypoint)
        self.assertEqual(len(checks), 21)
        self.assertTrue(all(checks.values()))
        self.assertFalse(h.configuration_checks(config, config["Image"], "test-volume", "test")["expected_entrypoint"])
        for kind in ("unknown_label", "bind", "extra_mount"):
            bad = copy.deepcopy(config)
            if kind == "unknown_label":
                bad["Config"]["Labels"]["unknown"] = "unused"
            elif kind == "bind":
                bad["Mounts"][0]["Type"] = "bind"
            else:
                bad["Mounts"].append({"Type": "volume", "Destination": "/extra", "RW": True})
            self.assertFalse(all(h.configuration_checks(bad, bad["Image"], "test-volume", "test", backend.entrypoint).values()))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".observer-test-recreation-", dir=h.ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_real_process_recreation_scenario(self):
        backend = h.LocalBackend(self.directory)
        self.addCleanup(backend.cleanup, False)
        matrix = h.Matrix(backend)
        runner.recreation(matrix)
        self.assertEqual(matrix.cases, [{"case": "named-volume-recreation", "status": "PASS"}])
        path = self.directory / "named-volume-recreation-backup.sqlite"
        with contextlib.closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("SELECT poll_seq,resolved_seq,status FROM state").fetchone(),
                             (203, 103, "DEGRADED"))
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 7)
            self.assertEqual(conn.execute("SELECT start_seq,end_seq,status FROM gaps").fetchall(), [(104, 199, "OPEN")])
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchall(), [("ok",)])
        self.assertEqual(sum(event.get("operation") == "shutdown" for event in backend.log), 2)

    def test_shutdown_before_any_state_operation(self):
        backend = h.LocalBackend(self.directory)
        self.addCleanup(backend.cleanup, False)
        session = backend.spawn()
        session.finish()
        self.assertEqual(list(backend.state.iterdir()), [])

    def archive(self, name="usr"):
        path = self.directory / "base.tar"
        with tarfile.open(path, "w") as archive:
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        return path

    def test_local_image_archive_contains_only_reviewed_additions(self):
        path = self.archive()
        runner.add_fixture(path)
        with tarfile.open(path) as archive:
            state = archive.getmember("state")
            self.assertEqual((state.uid, state.gid, state.mode), (65532, 65532, 0o700))
            driver = archive.getmember("app/tests/durability_payload.py")
            self.assertEqual(driver.mode, 0o444)
            self.assertEqual(archive.extractfile(driver).read(), h.PAYLOAD.read_bytes())
            self.assertFalse(any(".env" in name or name.startswith(("home/", "root/", "run/"))
                                 for name in archive.getnames()))
            self.assertFalse(any(name.endswith(".sqlite") for name in archive.getnames()))

    def test_archive_collision_is_rejected_without_extraction(self):
        with self.assertRaisesRegex(RuntimeError, "BASE_IMAGE_COLLISION"):
            runner.add_fixture(self.archive("state"))
        self.assertFalse((self.directory / "state").exists())

    def test_persistent_mount_inspection(self):
        mountinfo = ("1 0 0:1 / / ro - overlay overlay ro\n"
                     "2 1 8:1 /vol /state rw,noexec,nosuid,nodev - ext4 /dev/sda rw\n"
                     "3 1 0:2 / /dev rw - tmpfs tmpfs rw\n")
        self.assertTrue(all(payload.persistent_mount_checks(mountinfo).values()))
        self.assertFalse(payload.persistent_mount_checks(mountinfo.replace("/ / ro", "/ / rw"))["root_mount_readonly"])
        extra = "4 1 8:1 /host /extra rw - ext4 /dev/sda rw\n"
        self.assertFalse(payload.persistent_mount_checks(mountinfo + extra)["only_state_persistent_writable"])

    def test_evidence_saved_before_container_removal(self):
        backend = runner.RecreationBackend(self.directory, "test")
        backend.names = ["container-a"]
        backend.all_containers = [{"name": "container-a", "id": "id-a"}]
        backend.log = [{"summary": "saved fixture state"}]
        backend.verify = Mock()
        backend.spawn = Mock(return_value="container-b")

        def docker(args):
            self.assertEqual(args, ["rm", "container-a"])
            saved = json.loads((self.directory / "before-remove-container-a.json").read_text())
            self.assertEqual(saved["containers"][0]["id"], "id-a")
            self.assertEqual(saved["events"], backend.log)

        backend.docker = docker
        session = Mock()
        session.name = "container-a"
        session.process.poll.return_value = 0
        self.assertEqual(backend.recreate(session), "container-b")

    def test_evidence_failure_prevents_removal(self):
        backend = runner.RecreationBackend(self.directory, "test")
        backend.verify = Mock()
        backend.preserve = Mock(side_effect=OSError("fixture disk failure"))
        backend.docker = Mock()
        session = Mock()
        session.name = "container-a"
        session.process.poll.return_value = 0
        with self.assertRaises(OSError):
            backend.recreate(session)
        backend.docker.assert_not_called()

    def test_default_command_has_no_side_effects(self):
        with patch("sys.argv", ["run_container_recreation.py"]), patch.object(runner, "execute") as execute:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(runner.main(), 0)
        execute.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["external_requests"], 0)
        self.assertEqual(plan["network"], "none")

    def test_preparation_uses_only_local_docker_commands(self):
        backend = runner.RecreationBackend(self.directory, "test")
        calls = []
        def docker(args):
            calls.append(args)
            if args[:2] == ["image", "inspect"]:
                return json.dumps([{"Id": h.BASE, "Os": "linux", "Config": {}}]).encode()
            if args[0] == "create":
                self.assertIn("--pull=never", args)
                self.assertIn("--read-only", args)
                self.assertIn("--network=none", args)
                return b"test-id"
            if args[:2] == ["container", "inspect"]:
                return json.dumps([{"Id": "test-id", "Image": h.BASE, "State": {"Status": "created"},
                    "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True},
                    "Config": {"User": "65532:65532"}}]).encode()
            if args[0] == "export":
                path = Path(args[1].split("=", 1)[1])
                with tarfile.open(path, "w") as archive:
                    member = tarfile.TarInfo("usr")
                    member.type = tarfile.DIRTYPE
                    archive.addfile(member)
                return b""
            if args[:2] == ["image", "import"]:
                self.assertIn("--change=ENTRYPOINT " + json.dumps(backend.entrypoint), args)
                self.assertEqual(args[-1], backend.image_tag)
                self.assertTrue(Path(args[-2]).is_file())
                return ("sha256:" + "a" * 64).encode()
            self.fail("unexpected Docker operation")
        backend.docker = docker
        backend.prepare_local_image()
        self.assertFalse(any(args[0] in ("pull", "build", "start", "run", "rm") for args in calls))
        self.assertTrue((self.directory / "image-prepared.json").is_file())


if __name__ == "__main__":
    unittest.main()
