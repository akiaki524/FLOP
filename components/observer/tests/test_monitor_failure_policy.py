"""Offline history-boundary and V2 terminal failure integration regressions."""
import contextlib
import errno
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from test_capture_v2_monitor_evaluation import monitor, healthy
from test_capture_upgrade_v2 import (monitor_runner, FakeDocker, row, layout, staged,
                                    result, ATTEMPT, CAPTURE, ARCHIVE, IMAGE, RELEASE, FOREIGN)

PACKET = monitor.packet


class SchedulerDocker(FakeDocker):
    def run(self, argv, *, timeout=60):
        if argv[:2] == ["systemctl", "stop"] and getattr(self, "fail_timer_stop", False):
            self.calls.append(list(argv))
            return result(1)
        if argv[:2] == ["systemctl", "show"]:
            self.calls.append(list(argv))
            return result(stdout="ActiveState=inactive\n")
        return super().run(argv, timeout=timeout)


class MonitorFailurePolicy(unittest.TestCase):
    def prepare(self, tmp, *, running=True):
        l = layout(tmp)
        l.state(ATTEMPT).mkdir()
        l.observation(ATTEMPT).mkdir()
        (l.state(ATTEMPT) / "staged.json").write_text(json.dumps(staged()))
        (l.state(ATTEMPT) / "activation-started.json").write_text(json.dumps({
            "start_at": 800, "observation_checkpoint_at": 1000}))
        runner = SchedulerDocker({CAPTURE: row(CAPTURE, "capture", running=running),
                                  ARCHIVE: row(ARCHIVE, "archive", running=running),
                                  FOREIGN: row(FOREIGN, "capture", running=True)})
        return l, runner

    def invoke(self, l, runner, *, history_error=None):
        with contextlib.ExitStack() as stack:
            # Restore Candidate module globals after the V2 adapter binds them.
            for name in ("packet", "DIRECTORY", "GAP_BASELINE", "notification_event"):
                stack.enter_context(patch.object(monitor, name, getattr(monitor, name)))
            stack.enter_context(patch.object(monitor_runner, "load_candidate", return_value=(PACKET, monitor)))
            stack.enter_context(patch.object(monitor_runner, "require_host"))
            stack.enter_context(patch.object(monitor_runner, "v2_mount_check"))
            stack.enter_context(patch.object(monitor, "observers", return_value=healthy()["observers"]))
            stack.enter_context(patch.object(monitor, "metrics", side_effect=lambda role: healthy()["metrics"][role]))
            stack.enter_context(patch.object(monitor, "terminal_notification_count", return_value=0))
            stack.enter_context(patch.object(monitor.time, "time", return_value=1000))
            if history_error:
                stack.enter_context(patch.object(monitor, "append_history", side_effect=history_error))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = monitor_runner.run_bound(attempt=ATTEMPT, capture_id=CAPTURE,
                archive_id=ARCHIVE, image=IMAGE, release=RELEASE, runner=runner, layout=l)
            return code, output.getvalue()

    def test_rotation_continues_without_stops_preserves_legacy_latest_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp)
            directory = l.observation(ATTEMPT)
            legacy = directory / "history.jsonl"
            legacy.write_bytes(b"historical-production-evidence\n" * 1000)
            original = legacy.read_bytes()
            with patch.object(monitor, "HISTORY_LIMIT", 4096):
                for _ in range(12):
                    self.assertEqual(self.invoke(l, runner)[0], 0)
            self.assertEqual(legacy.read_bytes(), original)
            for name in ("history-current.jsonl", "history-previous.jsonl"):
                self.assertLessEqual((directory / name).stat().st_size, 4096)
                for line in (directory / name).read_text().splitlines():
                    self.assertEqual(json.loads(line)["failures"], [])
            self.assertEqual(json.loads((directory / "latest.json").read_text())["samples"], 12)
            self.assertEqual(json.loads((directory / "checkpoint-24h.json").read_text())["samples"], 1)
            self.assertFalse(any(x[:2] == ["docker", "stop"] for x in runner.calls))

    def test_auxiliary_access_failure_is_durable_warning_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp)
            code, output = self.invoke(l, runner, history_error=PermissionError(errno.EACCES, "fixture"))
            self.assertEqual(code, 0)
            self.assertIn("MONITOR_HISTORY_UNAVAILABLE:EACCES", output)
            latest = l.observation(ATTEMPT) / "latest.json"
            report = json.loads(latest.read_text())
            self.assertFalse(report["stop_applied"])
            self.assertEqual(report["verdict"], "PASS WITH FINDINGS")
            self.assertEqual(self.invoke(l, runner)[0], 0)
            self.assertIn("MONITOR_HISTORY_UNAVAILABLE:EACCES", json.loads(latest.read_text())["findings"])
            self.assertTrue((l.observation(ATTEMPT) / "history-current.jsonl").is_file())
            self.assertFalse(any(x[:2] == ["docker", "stop"] for x in runner.calls))

    def test_filesystem_danger_stops_exact_pair_once_and_quiesces_timer(self):
        for number in (errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS):
            with self.subTest(errno=number), tempfile.TemporaryDirectory() as tmp:
                l, runner = self.prepare(tmp)
                self.assertEqual(self.invoke(l, runner, history_error=OSError(number, "fixture"))[0], 2)
                self.assertEqual([x[-1] for x in runner.calls if x[:2] == ["docker", "stop"]], [CAPTURE, ARCHIVE])
                self.assertTrue(runner.containers[FOREIGN]["State"]["Running"])
                terminal = l.state(ATTEMPT) / "monitor-terminal.json"
                original = terminal.read_bytes()
                evidence = json.loads(original)
                self.assertTrue(evidence["stop_confirmed"])
                self.assertFalse(evidence["timer_quiesced"])
                status = json.loads(terminal.with_name("monitor-terminal-latest.json").read_text())
                self.assertTrue(status["timer_quiesced"])
                self.assertIn(["systemctl", "stop", "technocore-capture-monitor.timer"], runner.calls)
                runner.calls.clear()
                code, output = self.invoke(l, runner)
                self.assertEqual(code, 2)
                self.assertIn('"retry_suppressed": true', output)
                self.assertEqual(terminal.read_bytes(), original)
                self.assertFalse(any(x[:2] == ["docker", "stop"] for x in runner.calls))

    def test_corrupt_latest_remains_fatal_and_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp)
            latest = l.observation(ATTEMPT) / "latest.json"
            latest.write_bytes(b"corrupt-state")
            self.assertEqual(self.invoke(l, runner)[0], 2)
            self.assertEqual(latest.read_bytes(), b"corrupt-state")
            self.assertEqual([x[-1] for x in runner.calls if x[:2] == ["docker", "stop"]], [CAPTURE, ARCHIVE])

    def test_capture_fatal_with_auxiliary_warning_preserves_evidence_before_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp)
            with patch.object(monitor, "evaluate", return_value=(["RESOURCE_RUNWAY_LOW:capture"], [])):
                self.assertEqual(self.invoke(l, runner, history_error=PermissionError(errno.EPERM, "fixture"))[0], 2)
            directory = l.observation(ATTEMPT)
            checkpoint = json.loads((directory / "checkpoint-24h.json").read_text())
            self.assertTrue(checkpoint["stop_required"])
            self.assertFalse(checkpoint["stop_applied"])
            self.assertIn("MONITOR_HISTORY_UNAVAILABLE:EPERM", checkpoint["findings"])
            latest = json.loads((directory / "latest.json").read_text())
            self.assertTrue(latest["stop_applied"])
            self.assertEqual(latest["failures"], ["RESOURCE_RUNWAY_LOW:capture"])
            self.assertEqual([x[-1] for x in runner.calls if x[:2] == ["docker", "stop"]], [CAPTURE, ARCHIVE])

    def test_failed_timer_stop_keeps_failure_without_repeating_writer_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp)
            runner.fail_timer_stop = True
            self.assertEqual(self.invoke(l, runner, history_error=OSError(errno.EIO, "fixture"))[0], 2)
            runner.calls.clear()
            self.assertEqual(self.invoke(l, runner)[0], 2)
            evidence = json.loads((l.state(ATTEMPT) / "monitor-terminal-latest.json").read_text())
            self.assertFalse(evidence["timer_quiesced"])
            self.assertTrue(evidence["stop_confirmed"])
            self.assertEqual(evidence["verdict"], "FAIL")
            self.assertFalse(any(x[:2] == ["docker", "stop"] for x in runner.calls))

    def test_unknown_stopped_pair_keeps_read_only_observation_without_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp, running=False)
            with patch.object(monitor, "tick", side_effect=AssertionError("must not retry")):
                for _ in range(2):
                    runner.calls.clear()
                    code, output = self.invoke(l, runner)
                    self.assertEqual(code, 2)
                    evidence = json.loads(output)
                    self.assertEqual(evidence["verdict"], "UNKNOWN")
                    self.assertEqual(evidence["writers_state"], "STOPPED")
                    self.assertEqual(evidence["stop_reason"], "UNKNOWN_NOT_RECOVERY")
                    self.assertFalse(evidence["stop_attempted"])
                    self.assertFalse(evidence["timer_quiesced"])
                    self.assertEqual([x[3] for x in runner.calls], [CAPTURE, ARCHIVE])
                    self.assertTrue(all(x[:3] == ["docker", "inspect", "--type=container"]
                                        for x in runner.calls))
                    for name in ("monitor-terminal.json", "monitor-terminal-latest.json"):
                        self.assertFalse((l.state(ATTEMPT) / name).exists())

    def test_unknown_stopped_pair_can_resume_healthy_monitoring_with_same_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp, running=False)
            self.assertEqual(self.invoke(l, runner)[0], 2)
            for cid in (CAPTURE, ARCHIVE):
                runner.containers[cid]["State"].update(Running=True, Pid=37, Status="running")
            runner.calls.clear()
            for _ in range(2):
                self.assertEqual(self.invoke(l, runner)[0], 0)
            report = json.loads((l.observation(ATTEMPT) / "latest.json").read_text())
            self.assertEqual(report["samples"], 2)
            self.assertEqual(report["failures"], [])
            self.assertFalse(report["stop_applied"])
            self.assertTrue(all(x[:3] == ["docker", "inspect", "--type=container"]
                                for x in runner.calls))
            self.assertTrue(all(runner.containers[cid]["State"]["Running"]
                                for cid in (CAPTURE, ARCHIVE, FOREIGN)))
            self.assertFalse((l.state(ATTEMPT) / "monitor-terminal.json").exists())

    def test_transient_inspect_failure_does_not_stop_healthy_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp)
            run = runner.run
            failed = []

            def flaky(argv, *, timeout=60):
                if argv[:2] == ["docker", "inspect"] and not failed:
                    failed.append(argv)
                    return result(1, stderr="Cannot connect to the Docker daemon")
                return run(argv, timeout=timeout)
            runner.run = flaky
            self.assertEqual(self.invoke(l, runner)[0], 0)
            self.assertTrue(failed)
            self.assertFalse(any(x[:2] == ["docker", "stop"] or x[0] == "systemctl" for x in runner.calls))
            self.assertFalse((l.state(ATTEMPT) / "monitor-terminal.json").exists())

    def test_wrong_identity_never_qualifies_as_manual_stopped_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp, running=False)
            runner.containers[CAPTURE]["Image"] = "sha256:" + "f" * 64
            self.assertEqual(self.invoke(l, runner, history_error=OSError(errno.EIO, "fixture"))[0], 2)
            evidence = json.loads((l.state(ATTEMPT) / "monitor-terminal.json").read_text())
            self.assertEqual(evidence["stop_results"]["capture"], "TARGET_MISMATCH")
            self.assertNotIn("stop_reason", evidence)

    def test_stop_failure_keeps_timer_and_retries_exact_pair_next_invocation(self):
        for failure in ("STOP_FAILED", "UNKNOWN", "TARGET_MISMATCH"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                l, runner = self.prepare(tmp)
                run = runner.run
                def unavailable(argv, *, timeout=60):
                    if argv[:2] == ["docker", "stop"] or (failure == "UNKNOWN" and argv[:2] == ["docker", "inspect"]):
                        runner.calls.append(list(argv))
                        return result(1, stderr="fixture unavailable")
                    return run(argv, timeout=timeout)
                runner.run = unavailable
                if failure == "TARGET_MISMATCH":
                    runner.containers[CAPTURE]["Image"] = "sha256:" + "f" * 64
                self.assertEqual(self.invoke(l, runner, history_error=OSError(errno.EIO, "fixture"))[0], 2)
                terminal = l.state(ATTEMPT) / "monitor-terminal.json"
                original = terminal.read_bytes()
                evidence = json.loads(original)
                self.assertEqual(evidence["stop_results"]["capture"], failure)
                self.assertFalse(evidence["stop_confirmed"])
                self.assertTrue(evidence["retry_required"])
                self.assertFalse(any(x[0] == "systemctl" for x in runner.calls))
                self.assertTrue(runner.containers[CAPTURE]["State"]["Running"])
                # Persistent failure remains retryable without mutating first Evidence.
                runner.calls.clear()
                self.assertEqual(self.invoke(l, runner)[0], 2)
                pending = json.loads(terminal.with_name("monitor-terminal-latest.json").read_text())
                self.assertTrue(pending["retry_required"])
                self.assertFalse(pending["timer_quiesced"])
                self.assertFalse(any(x[0] == "systemctl" for x in runner.calls))
                self.assertEqual(terminal.read_bytes(), original)
                runner.run = run
                runner.containers[CAPTURE]["Image"] = IMAGE
                runner.calls.clear()
                self.assertEqual(self.invoke(l, runner)[0], 2)
                evidence = json.loads(terminal.with_name("monitor-terminal-latest.json").read_text())
                self.assertTrue(evidence["stop_confirmed"])
                self.assertFalse(evidence["retry_required"])
                self.assertTrue(evidence["timer_quiesced"])
                self.assertEqual(evidence["verdict"], "FAIL")
                self.assertEqual(terminal.read_bytes(), original)
                self.assertTrue(runner.containers[FOREIGN]["State"]["Running"])
                self.assertIn(["docker", "stop", "--time", "40", CAPTURE], runner.calls)
                self.assertFalse(any(x[-1] == FOREIGN for x in runner.calls))

    def test_terminal_stop_evidence_is_durable_before_timer_quiesce(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp)
            run = runner.run
            checked = []
            def ordered(argv, *, timeout=60):
                if argv[:2] == ["systemctl", "stop"]:
                    for name in ("monitor-terminal.json", "monitor-terminal-latest.json"):
                        evidence = json.loads((l.state(ATTEMPT) / name).read_text())
                        self.assertTrue(evidence["stop_confirmed"])
                        self.assertFalse(evidence["timer_quiesced"])
                        self.assertEqual(evidence["stop_results"], {"capture": "STOPPED", "archive": "STOPPED"})
                    checked.append(argv)
                return run(argv, timeout=timeout)
            runner.run = ordered
            self.assertEqual(self.invoke(l, runner, history_error=OSError(errno.EIO, "fixture"))[0], 2)
            self.assertEqual(len(checked), 1)

    def test_terminal_save_failure_keeps_scheduler_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp)
            with patch.object(monitor_runner, "save_terminal_status", side_effect=OSError(errno.EIO, "fixture")):
                code, output = self.invoke(l, runner, history_error=OSError(errno.EIO, "fixture"))
            self.assertEqual(code, 2)
            self.assertIn("SAVE_FAILED", output)
            self.assertFalse(any(x[0] == "systemctl" for x in runner.calls))
            self.assertEqual(self.invoke(l, runner)[0], 2)
            self.assertIn(["systemctl", "stop", "technocore-capture-monitor.timer"], runner.calls)

    def test_third_generation_preserves_previous_bytes_and_keeps_sampling(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp)
            directory = l.observation(ATTEMPT)
            previous = directory / "history-previous.jsonl"
            previous.write_bytes(b"historical previous evidence\n")
            original = previous.read_bytes()
            with patch.object(monitor, "HISTORY_LIMIT", 4096):
                for _ in range(12):
                    self.assertEqual(self.invoke(l, runner)[0], 0)
            sealed = list(directory.glob("history-sealed-*.jsonl"))
            self.assertTrue(any(path.read_bytes() == original for path in sealed))
            for path in [directory / "history-current.jsonl", previous, *sealed]:
                self.assertLessEqual(path.stat().st_size, 4096)
            count = sum(len(path.read_text().splitlines()) for path in [directory / "history-current.jsonl", previous, *sealed])
            self.assertEqual(count, 13)  # 12 new samples + original Evidence.
            self.assertEqual(json.loads((directory / "latest.json").read_text())["samples"], 12)
            self.assertFalse(any(x[:2] == ["docker", "stop"] or x[0] == "systemctl" for x in runner.calls))

    def test_seven_day_backup_and_ten_day_retention_pending_never_delete(self):
        for age, finding in ((7 * 86400, "MONITOR_HISTORY_BACKUP_REQUIRED"),
                             (10 * 86400 + 1, "MONITOR_HISTORY_RETENTION_BACKUP_REQUIRED")):
            with self.subTest(age=age), tempfile.TemporaryDirectory() as tmp:
                l, runner = self.prepare(tmp)
                (l.state(ATTEMPT) / "activation-started.json").write_text(json.dumps({
                    "start_at": 1000 - age, "observation_checkpoint_at": 900}))
                directory = l.observation(ATTEMPT)
                legacy = directory / "history.jsonl"
                legacy.write_bytes(b"legacy evidence")
                self.assertEqual(self.invoke(l, runner)[0], 0)
                for name in ("latest.json", "checkpoint-24h.json"):
                    report = json.loads((directory / name).read_text())
                    self.assertIn(finding, report["findings"])
                    self.assertEqual(report["failures"], [])
                policy = json.loads((directory / "latest.json").read_text())["history_retention"]
                self.assertEqual(policy["retention_seconds"], 10 * 86400)
                self.assertEqual(policy["backup_interval_seconds"], 7 * 86400)
                self.assertFalse(policy["verified_backup"])
                self.assertFalse(policy["deletion_enabled"])
                self.assertEqual(legacy.read_bytes(), b"legacy evidence")
                self.assertTrue((directory / "history-current.jsonl").is_file())
                self.assertFalse(any(x[:2] == ["docker", "stop"] or x[0] == "systemctl" for x in runner.calls))

    def test_single_oversized_sample_continues_latest_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp)
            with patch.object(monitor, "HISTORY_LIMIT", 1):
                self.assertEqual(self.invoke(l, runner)[0], 0)
            directory = l.observation(ATTEMPT)
            self.assertFalse((directory / "history-current.jsonl").exists())
            for name in ("latest.json", "checkpoint-24h.json"):
                self.assertIn("MONITOR_HISTORY_UNAVAILABLE:SAMPLE_TOO_LARGE",
                              json.loads((directory / name).read_text())["findings"])
            self.assertFalse(any(x[:2] == ["docker", "stop"] for x in runner.calls))

    def test_history_symlink_and_non_regular_file_remain_fatal(self):
        for name in ("history-current.jsonl", "history-previous.jsonl"):
            for kind in ("symlink", "directory"):
                with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as tmp:
                    l, runner = self.prepare(tmp)
                    target = l.observation(ATTEMPT) / "preserved"
                    target.write_bytes(b"historical evidence")
                    path = l.observation(ATTEMPT) / name
                    if kind == "symlink":
                        path.symlink_to(target)
                    else:
                        path.mkdir()
                    self.assertEqual(self.invoke(l, runner)[0], 2)
                    self.assertEqual(target.read_bytes(), b"historical evidence")
                    self.assertEqual([x[-1] for x in runner.calls if x[:2] == ["docker", "stop"]], [CAPTURE, ARCHIVE])

    def test_actual_append_open_access_errors_are_auxiliary(self):
        for number in (errno.EACCES, errno.EPERM):
            with self.subTest(errno=number), tempfile.TemporaryDirectory() as tmp:
                l, runner = self.prepare(tmp)
                original_open = os.open
                def access_error(path, flags, *args, **kwargs):
                    if str(path).endswith("history-current.jsonl") and flags & os.O_APPEND:
                        raise PermissionError(number, "fixture")
                    return original_open(path, flags, *args, **kwargs)
                with patch.object(monitor.os, "open", side_effect=access_error):
                    self.assertEqual(self.invoke(l, runner)[0], 0)
                report = json.loads((l.observation(ATTEMPT) / "latest.json").read_text())
                self.assertIn("MONITOR_HISTORY_UNAVAILABLE:" + errno.errorcode[number], report["findings"])
                self.assertFalse(any(x[:2] == ["docker", "stop"] for x in runner.calls))

    def test_interrupted_rotation_preserves_sealed_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp)
            directory = l.observation(ATTEMPT)
            current = directory / "history-current.jsonl"
            previous = directory / "history-previous.jsonl"
            current.write_bytes(b"sealed evidence\n")
            os.link(current, previous)
            self.assertEqual(self.invoke(l, runner)[0], 0)
            self.assertNotEqual(current.read_bytes(), b"sealed evidence\n")
            self.assertEqual(previous.read_bytes(), b"sealed evidence\n")

    def test_transient_inspect_failure_cannot_mask_genuine_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp)
            run = runner.run
            failed = []
            def flaky(argv, *, timeout=60):
                if argv[:2] == ["docker", "inspect"] and not failed:
                    failed.append(argv)
                    return result(1, stderr="fixture unavailable")
                return run(argv, timeout=timeout)
            runner.run = flaky
            with patch.object(monitor, "evaluate", return_value=(["RESOURCE_RUNWAY_LOW:capture"], [])):
                self.assertEqual(self.invoke(l, runner)[0], 2)
            self.assertEqual([x[-1] for x in runner.calls if x[:2] == ["docker", "stop"]], [CAPTURE, ARCHIVE])

    def test_saved_confirmation_cannot_suppress_stop_of_running_fatal_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            l, runner = self.prepare(tmp)
            self.assertEqual(self.invoke(l, runner, history_error=OSError(errno.EIO, "fixture"))[0], 2)
            terminal = l.state(ATTEMPT) / "monitor-terminal.json"
            original = terminal.read_bytes()
            for cid in (CAPTURE, ARCHIVE):
                runner.containers[cid]["State"].update(Running=True, Pid=37)
            runner.calls.clear()
            self.assertEqual(self.invoke(l, runner)[0], 2)
            self.assertEqual([x[-1] for x in runner.calls if x[:2] == ["docker", "stop"]], [CAPTURE, ARCHIVE])
            self.assertEqual(terminal.read_bytes(), original)
            self.assertTrue(runner.containers[FOREIGN]["State"]["Running"])
