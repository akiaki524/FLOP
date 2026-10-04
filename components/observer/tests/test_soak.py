"""Offline regression tests: real SQLite/worker processes, no Docker or sockets."""

import contextlib
import errno
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))
import run_container_soak as soak
import soak_payload as payload
from technocore_observer.protocol import Reply


class Clock:
    def __init__(self): self.value = 0.0
    def __call__(self): return self.value
    def sleep(self, seconds): self.value += seconds


class AdvancingTransport(soak.Transport):
    def __init__(self, directory, clock):
        super().__init__(directory)
        self.clock, self.calls = clock, []

    def call(self, command, timeout=60):
        self.calls.append(command)
        result = super().call(command, timeout)
        if command == "poll": self.clock.sleep(10)
        return result

    def watchdog(self):
        self.calls.append("watchdog")
        return super().watchdog()


class GateTests(unittest.TestCase):
    def test_all_roles_share_exact_374_budget(self):
        clock = Clock()
        gate = soak.Gate(3600, clock)
        for role, count in soak.LIMITS.items():
            for _ in range(count):
                gate.begin(role)
                gate.finish()
                clock.sleep(2)
        self.assertEqual(sum(gate.counts.values()), 374)
        for role in soak.LIMITS:
            with self.assertRaisesRegex(soak.Stop, "REQUEST_LIMIT"):
                gate.begin(role)
        self.assertEqual(sum(gate.counts.values()), 374)

    def test_expired_during_wait_cannot_start_request(self):
        clock = Clock()
        gate = soak.Gate(1, clock)
        gate.begin("init")
        gate.finish(600)
        clock.sleep(600)
        with self.assertRaisesRegex(soak.Stop, "TIME_LIMIT"): gate.begin("config")
        self.assertEqual(sum(gate.counts.values()), 1)

    def test_failed_reservations_delays_concurrency_and_boundary(self):
        clock = Clock()
        gate = soak.Gate(3600, clock)
        with self.assertRaisesRegex(soak.Stop, "BOUNDARY"): gate.begin("diagnostic-url")
        gate.begin("init")
        with self.assertRaisesRegex(soak.Stop, "CONCURRENT"): gate.begin("watchdog")
        gate.finish(600)
        clock.sleep(599)
        with self.assertRaisesRegex(soak.Stop, "EARLY"): gate.begin("watchdog")
        clock.sleep(1)
        gate.begin("watchdog")
        gate.finish()
        self.assertEqual(dict(gate.counts), {"init": 1, "watchdog": 1})
        gate.stopped = True
        with self.assertRaisesRegex(soak.Stop, "ALREADY_STOPPED"): gate.begin("poll")

    def test_invalid_duration_and_delay_fail_closed(self):
        for value in (0, -1, 3601, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(soak.Stop): soak.Gate(value)
        gate = soak.Gate(3600)
        with self.assertRaises(soak.Stop): gate.finish(float("nan"))
        self.assertTrue(gate.stopped)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.client = payload.FixtureClient()
        self.worker = payload.Worker(self.directory / "inbox", self.client, True)
        self.addCleanup(self.worker.close)
        self.worker.handle("init")
        self.worker.handle("config")

    def test_real_wal_backup_contains_committed_records_and_raw_controls(self):
        result = self.worker.handle("poll")
        self.assertEqual(result["heartbeat"]["poll_seq"], 101)
        self.assertEqual(self.worker.store.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        meta = self.worker.handle("snapshot")
        path = self.worker.directory / "snapshot.sqlite"
        with contextlib.closing(sqlite3.connect(path)) as conn:
            raw, text, flags = conn.execute("SELECT raw_record_json,text_value,validation_flags FROM messages").fetchone()
            self.assertEqual(text, "offline\n\u202e\x1b")
            self.assertEqual(json.loads(raw)["text"], text)
            self.assertEqual(json.loads(flags), [])
            self.assertEqual(conn.execute("SELECT poll_seq FROM state").fetchone()[0], 101)
        self.assertEqual(meta["state"]["poll_seq"], 101)
        self.worker.handle("ack_snapshot")
        self.assertFalse(path.exists())

    def test_generation_stops_without_inbox_commit_or_later_get(self):
        self.client.poll = Mock(return_value=payload.FixtureClient.reply([101], generation=3))
        result = self.worker.handle("poll")
        self.assertFalse(result["continuing"])
        self.assertEqual(result["heartbeat"]["status"], "NEEDS_RESYNC")
        self.assertEqual(result["heartbeat"]["poll_seq"], 100)
        with self.assertRaisesRegex(RuntimeError, "TERMINAL_STATE"): self.worker.handle("poll")
        self.client.poll.assert_called_once()
        self.assertEqual(self.worker.store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_protocol_three_times_then_no_more_get(self):
        self.client.poll = Mock(return_value=Reply(200, "application/json", b"{"))
        for attempt in range(3):
            result = self.worker.handle("poll")
            self.assertEqual(result["heartbeat"]["poll_seq"], 100)
            self.assertEqual(result["continuing"], attempt < 2)
        with self.assertRaises(RuntimeError): self.worker.handle("poll")
        self.assertEqual(self.client.poll.call_count, 3)

    def test_503_and_429_keep_cursor_and_provide_delay_then_recover(self):
        self.client.poll = Mock(side_effect=[Reply(503, "application/json", b"{}"),
                                           Reply(429, "application/json", b"{}", retry_after="999999"),
                                           payload.FixtureClient.reply([101])])
        first = self.worker.handle("poll")
        self.assertTrue(first["continuing"])
        self.assertGreaterEqual(first["delay"], 1)
        second = self.worker.handle("poll")
        self.assertEqual(second["delay"], 600)
        self.assertEqual(second["heartbeat"]["poll_seq"], 100)
        self.assertEqual(self.worker.handle("poll")["heartbeat"]["poll_seq"], 101)

    def test_gap_continues_and_wait_held_false_is_measured(self):
        self.client.poll = Mock(return_value=payload.FixtureClient.reply([200, 201], held=False))
        result = self.worker.handle("poll")
        self.assertTrue(result["continuing"])
        self.assertEqual(result["heartbeat"]["resolved_seq"], 100)
        self.assertEqual(result["heartbeat"]["status"], "DEGRADED")
        self.assertIs(result["http"]["wait_held"], False)
        self.assertGreaterEqual(result["delay"], 10)

    def test_disk_failure_does_not_advance_cursor(self):
        with patch.object(self.worker.store, "save", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError): self.worker.handle("poll")
        self.assertEqual(self.worker.store.state()["poll_seq"], 100)

    def test_network_metadata_is_safe_and_reraises_original_exception(self):
        hostile = "do-not-log-this\\n\x1b\u202e https://untrusted.invalid/private"
        cases = (
            (TimeoutError(errno.ETIMEDOUT, hostile), "TimeoutError", errno.ETIMEDOUT, None),
            (ConnectionResetError(errno.ECONNRESET, hostile), "ConnectionResetError", errno.ECONNRESET, None),
            (urllib.error.URLError(TimeoutError(errno.ETIMEDOUT, hostile)), "URLError", errno.ETIMEDOUT, "TimeoutError"),
            (urllib.error.URLError(hostile), "URLError", None, None),
        )
        for error, kind, number, reason in cases:
            with self.subTest(kind=kind, reason=reason):
                client = Mock()
                client.poll.side_effect = error
                measured = payload.MeasuredClient(client)
                with self.assertRaises(type(error)) as caught:
                    measured.poll(100)
                self.assertIs(caught.exception, error)
                self.assertEqual(measured.last["error_class"], kind)
                self.assertEqual(measured.last["errno"], number)
                self.assertEqual(measured.last["reason_class"], reason)
                self.assertIsNone(measured.last["http_status"])
                self.assertNotIn("do-not-log-this", json.dumps(measured.last))

    def test_network_metadata_does_not_change_retry_or_leak_into_next_reply(self):
        self.client.poll = Mock(side_effect=[TimeoutError(errno.ETIMEDOUT, "never log"),
                                           Reply(503, "application/json", b"{}"),
                                           payload.FixtureClient.reply([101])])
        first = self.worker.handle("poll")
        self.assertTrue(first["continuing"])
        self.assertGreaterEqual(first["delay"], 1)
        self.assertEqual(first["heartbeat"]["poll_seq"], 100)
        self.assertEqual(first["http"]["error_class"], "TimeoutError")
        second = self.worker.handle("poll")
        self.assertTrue(second["continuing"])
        self.assertEqual(second["heartbeat"]["consecutive_failures"], 2)
        self.assertNotIn("error_class", second["http"])
        third = self.worker.handle("poll")
        self.assertEqual(third["heartbeat"]["poll_seq"], 101)
        self.assertEqual(third["heartbeat"]["consecutive_failures"], 0)
        self.assertNotIn("error_class", third["http"])

    def test_telemetry_never_formats_exception_or_string_reason(self):
        class NoFormat(TimeoutError):
            def __str__(self): raise AssertionError("exception formatting forbidden")
            def __repr__(self): raise AssertionError("exception formatting forbidden")
        error = urllib.error.URLError(NoFormat(errno.ETIMEDOUT, "hidden"))
        error.errno = True
        client = Mock()
        client.poll.side_effect = error
        measured = payload.MeasuredClient(client)
        with self.assertRaises(urllib.error.URLError): measured.poll(100)
        self.assertEqual(measured.last["errno"], errno.ETIMEDOUT)
        self.assertEqual(measured.last["reason_class"], "NoFormat")

    def test_second_writer_lock_refused(self):
        command = [sys.executable, "-B", str(ROOT / "tests/soak_payload.py"), "--offline", "--directory", str(self.worker.directory)]
        result = subprocess.run(command, input=b"", capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.worker.store.state()["poll_seq"], 100)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_virtual_hour_real_process_watchdog_backup_and_bounded_gets(self):
        clock = Clock()
        transport = AdvancingTransport(self.directory, clock)
        controller = soak.Controller(transport, soak.Artifacts(self.directory), clock=clock, sleep=clock.sleep)
        report = controller.run()
        self.assertEqual(report["status"], "OFFLINE_PASS")
        self.assertTrue(report["final_snapshot_saved"])
        self.assertLessEqual(report["request_count"], 374)
        self.assertGreater(report["role_counts"]["watchdog"], 0)
        self.assertLessEqual(report["role_counts"]["watchdog"], 12)
        self.assertIn("watchdog", transport.calls)
        self.assertGreater(report["snapshots"], 1)
        self.assertLessEqual(report["snapshots"], 13)
        records = [json.loads(line) for line in (self.directory / "metrics.jsonl").read_text().splitlines()]
        reservations = [r for r in records if r["kind"] == "request_reserved"]
        self.assertEqual(len(reservations), report["request_count"])
        self.assertLess(reservations[-1]["elapsed_seconds"], 3600)
        self.assertNotIn("offline\\n", (self.directory / "metrics.jsonl").read_text())
        self.assertEqual(report["wait_held_false_rate"], 0)
        for path in self.directory.glob("snapshot-*.sqlite"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_default_plan_performs_no_docker_or_network_or_writes(self):
        with patch.object(sys, "argv", ["soak"]), patch("builtins.print"), patch.object(soak, "new_directory") as create:
            with patch.object(subprocess, "Popen", side_effect=AssertionError("no process")):
                self.assertEqual(soak.main(), 0)
        create.assert_not_called()
        self.assertNotIn("soak_started", soak.plan())

    def test_execution_plan_has_no_false_runtime_started_flag(self):
        (self.directory / "docs").mkdir()
        with patch.object(soak, "ROOT", self.directory), patch.object(soak, "archive_sources"), patch("builtins.print"):
            result = soak.new_directory("live", 3600)
        plan = json.loads((result / "execution-plan.json").read_text())
        self.assertEqual(plan["mode"], "live")
        self.assertNotIn("soak_started", plan)
        self.assertEqual(plan["max_gets"], 374)

    def test_phase_times_distinguish_backup_cleanup_and_wall_clock_changes(self):
        clock = Clock()
        transport = AdvancingTransport(self.directory, clock)
        controller = soak.Controller(transport, soak.Artifacts(self.directory), duration=6,
                                     clock=clock, sleep=clock.sleep, wall_clock=lambda: 10000 - clock())
        original_backup, original_close = controller.backup, transport.close
        def backup():
            original_backup()
            clock.sleep(2)
        def close(remove=False):
            original_close(remove)
            clock.sleep(3)
        controller.backup, transport.close = backup, close
        report = controller.run()
        self.assertEqual(report["status"], "OFFLINE_PASS")
        phases = report["phase_times"]
        order = ("controller_started", "request_window_started", "last_request_dispatched",
                 "last_request_rpc_finished", "request_dispatch_closed", "final_backup_completed",
                 "cleanup_completed", "cleanup_attempt_finished")
        elapsed = [phases[name]["controller_elapsed_seconds"] for name in order]
        self.assertEqual(elapsed, sorted(elapsed))
        self.assertEqual(elapsed[-2] - elapsed[-3], 3)
        self.assertEqual(report["total_elapsed_seconds"] - report["elapsed_seconds"], 3)
        self.assertLess(phases["cleanup_completed"]["unix_seconds"], phases["controller_started"]["unix_seconds"])
        self.assertEqual(report["resource_sampling"]["target_interval_seconds"], 60)
        self.assertEqual(report["request_count"], 3)

    def test_docker_arguments_preserve_boundary_without_running_docker(self):
        for offline in (True, False):
            with self.subTest(offline=offline), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                transport = soak.DockerTransport(Path(directory), offline)
                transport.run = Mock(side_effect=[soak.smoke.BASE_IMAGE.encode(), soak.Stop("CAPTURE_ONLY")])
                with self.assertRaisesRegex(soak.Stop, "CAPTURE_ONLY"): transport.start()
                args = transport.run.call_args_list[1].args[0]
                for flag in ("--pull=never", "--user=65532:65532", "--read-only", "--cap-drop=ALL",
                             "--pids-limit=32", "--memory=256m", "--restart=no", "--init",
                             "--security-opt=no-new-privileges:true"):
                    self.assertIn(flag, args)
                self.assertIn("--network=" + ("none" if offline else "bridge"), args)
                self.assertFalse(any(arg.startswith(("--mount", "--volume", "--privileged", "--pid=host")) for arg in args))
                self.assertFalse(any("docker.sock" in arg for arg in args))
                self.assertEqual(set(transport.env), {"PATH", "HOME"})
                self.assertEqual(transport.env["HOME"], str(Path(directory) / "docker-client"))

    def test_offline_preflight_failure_prevents_live_transport(self):
        with patch.object(sys, "argv", ["soak", "--execute", "--init-mode", "tail"]), patch("builtins.print"):
            with patch.object(soak, "new_directory", return_value=self.directory), patch.object(soak, "DockerTransport") as transport:
                with patch.object(soak.Controller, "run", return_value={"status": "STOPPED"}):
                    self.assertEqual(soak.main(), 1)
        self.assertEqual(transport.call_count, 1)
        self.assertTrue(transport.call_args.args[1])

    def test_host_low_disk_prevents_worker_start(self):
        transport = Mock(offline=True)
        with patch.object(soak.shutil, "disk_usage", return_value=Mock(free=0)):
            report = soak.Controller(transport, soak.Artifacts(self.directory)).run()
        self.assertEqual(report["status"], "STOPPED")
        self.assertEqual(report["request_count"], 0)
        transport.start.assert_not_called()
        self.assertIsNone(report["phase_times"]["request_window_started"])
        self.assertIsNone(report["phase_times"]["last_request_dispatched"])
        self.assertIsNone(report["phase_times"]["final_backup_completed"])

    def test_watchdog_generation_and_worker_terminal_prevent_next_request(self):
        for role in ("poll", "watchdog"):
            with self.subTest(role=role):
                clock = Clock()
                transport = Mock(offline=True)
                transport.call.return_value = {"continuing": False, "heartbeat": {"status": "NEEDS_RESYNC"}, "http": {"http_status": 200}}
                transport.watchdog.return_value = {"watchdog": {"result": "GENERATION_CHANGE"}, "http": {"http_status": 200}}
                controller = soak.Controller(transport, Mock(), clock=clock, sleep=clock.sleep)
                controller.gate = soak.Gate(3600, clock)
                with self.assertRaises(soak.Stop): controller.request(role)
                self.assertEqual(sum(controller.gate.counts.values()), 1)
                self.assertTrue(controller.gate.stopped)
                with self.assertRaisesRegex(soak.Stop, "ALREADY_STOPPED"):
                    controller.request("poll")

    def test_controller_disk_or_boundary_failure_ends_all_gets(self):
        for error in (sqlite3.OperationalError("disk full"), soak.Stop("BOUNDARY_VIOLATION")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                clock, calls = Clock(), []
                transport = Mock(offline=True, enforce_resources=False)
                transport.start.return_value = {"ready": True, "checks": {"fixture": True}}
                hb = {"status": "RUNNING", "open_gap_count": 0, "consecutive_protocol_anomalies": 0}
                def call(command, timeout=60):
                    calls.append(command)
                    if command == "metrics": return {"rss_bytes": 10, "heartbeat": None}
                    if command == "init": return {"heartbeat": hb, "pragmas": {"journal_mode": "wal", "synchronous": 2, "foreign_keys": 1}}
                    if command == "config": return {"config": {"event": "VERSION_CONFIRMED", "deployment_config": {"validation_flags": []}}}
                    if command == "poll": raise error
                    if command == "snapshot": raise RuntimeError("worker exited")
                    self.fail(command)
                transport.call.side_effect = call
                artifacts = Mock(directory=Path(directory), snapshots=0)
                artifacts.guard.return_value = {}
                controller = soak.Controller(transport, artifacts, clock=clock, sleep=clock.sleep)
                report = controller.run()
                self.assertEqual(report["status"], "STOPPED")
                self.assertEqual(report["request_count"], 3)
                self.assertEqual(calls.count("poll"), 1)
                transport.watchdog.assert_not_called()
                self.assertTrue(controller.gate.stopped)

    def test_resource_stop_thresholds_and_missing_measurement(self):
        metrics = {"rss_bytes": 10, "container_memory_bytes": 10, "oom_count": 0,
                   "disk_free_bytes": 64 * soak.MIB, "heartbeat": None}
        for key, value, code in (("oom_count", 1, "OOM"), ("disk_free_bytes", 0, "DISK_RESERVE"),
                                 ("container_memory_bytes", None, "MEMORY_MEASUREMENT_UNAVAILABLE")):
            with self.subTest(key=key):
                transport = Mock(enforce_resources=True)
                transport.call.return_value = {**metrics, key: value}
                artifacts = Mock()
                artifacts.guard.return_value = {}
                controller = soak.Controller(transport, artifacts)
                controller.gate = soak.Gate(3600)
                with self.assertRaisesRegex(soak.Stop, code): controller.sample()
        transport.call.return_value = {**metrics, "rss_bytes": 192 * soak.MIB}
        controller.sample()
        with self.assertRaisesRegex(soak.Stop, "MEMORY_LIMIT"): controller.sample()

    def test_controller_time_includes_init_and_config(self):
        clock = Clock()
        transport = AdvancingTransport(self.directory, clock)
        report = soak.Controller(transport, soak.Artifacts(self.directory), duration=1,
                                 clock=clock, sleep=clock.sleep).run()
        self.assertEqual(report["request_count"], 1)
        self.assertEqual(report["reason"], "TIME_LIMIT")
        self.assertNotIn("config", transport.calls)
        self.assertTrue(report["final_snapshot_saved"])

    def test_no_implicit_tail_and_invalid_long_duration(self):
        for argv in (["--execute"], ["--offline", "--init-mode", "tail", "--duration-seconds", "3601"]):
            with self.subTest(argv=argv), patch.object(sys, "argv", ["soak"] + argv), patch("builtins.print"):
                with patch.object(soak, "new_directory") as create:
                    self.assertEqual(soak.main(), 1)
                    create.assert_not_called()

    def test_snapshot_rejects_traversal_and_hash_mismatch(self):
        artifacts = soak.Artifacts(self.directory)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("../evil", b"x")
            archive.writestr("snapshot.json", b"{}")
        with self.assertRaisesRegex(soak.Stop, "SNAPSHOT_NAMES"):
            artifacts.save_snapshot(data.getvalue(), {})
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("snapshot.sqlite", b"x")
            archive.writestr("snapshot.json", b'{"sha256":"wrong","bytes":1}')
        with self.assertRaisesRegex(soak.Stop, "SNAPSHOT_HASH"):
            artifacts.save_snapshot(data.getvalue(), {"sha256": "wrong", "bytes": 1})

    def test_rpc_timeout_is_bounded(self):
        command = [sys.executable, "-B", "-c", 'import time; print(\'{"ok":true,"result":{"ready":true}}\',flush=True); time.sleep(10)']
        with contextlib.closing(soak.RPC(command, {"PATH": os.defpath})) as rpc:
            with self.assertRaisesRegex(soak.Stop, "RPC_DEADLINE"): rpc.receive(0.01)

    def test_cleanup_failure_cannot_leave_pass_report(self):
        clock = Clock()
        transport = AdvancingTransport(self.directory, clock)
        original_close = transport.close
        def fail_close(remove=False):
            original_close(remove)
            raise soak.Stop("CLEANUP_FAILURE")
        transport.close = fail_close
        report = soak.Controller(transport, soak.Artifacts(self.directory), duration=6,
                                 clock=clock, sleep=clock.sleep).run()
        self.assertEqual(report["status"], "STOPPED")
        self.assertIn("cleanup_error", report)
        self.assertIsNone(report["phase_times"]["cleanup_completed"])
        self.assertIsNotNone(report["phase_times"]["cleanup_attempt_finished"])
        self.assertEqual(json.loads((self.directory / "report.json").read_text())["status"], "STOPPED")

    def test_live_payload_rejects_normal_host_before_network(self):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "tests/soak_payload.py")], capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["error"], "ISOLATED_CONTAINER_REQUIRED")


if __name__ == "__main__":
    unittest.main()
