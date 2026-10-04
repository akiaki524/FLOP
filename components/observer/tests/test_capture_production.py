"""Production candidate checks: synthetic local state, never Technocore HTTP."""

from contextlib import contextmanager, closing
import copy
import errno
import io
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from test_capture_spool import page
from test_capture_runtime import ImmediateBudget, WindowAPI, Clock
from technocore_full_capture import production as p
from technocore_full_capture.capture_first import FastCapture
from technocore_full_capture.pacing import BudgetPolicy, HostBudget, GracefulStop
from technocore_full_capture.scheduling import ReservationBudget
from technocore_full_capture.spool import canonical
from technocore_full_capture.spool_archive import ArchiveWorker, validate_source
from technocore_observer.protocol import ObserverError, Reply


def offline_storage(r, role):
    return p.storage_probe(r, role, enforce_boundary=False)


def stop_child(c, ready, result):
    original = p.RoomBudget.request

    @contextmanager
    def started(self, *args):
        ready.set()
        with original(self, *args):
            yield

    probe = p.storage_probe
    try:
        with patch.object(p, "storage_probe", side_effect=lambda r, role: probe(r, role, enforce_boundary=False)), \
                patch.object(p.RoomBudget, "request", started), \
                patch("technocore_full_capture.deadline.DeadlineClient.poll", side_effect=AssertionError("NETWORK_FORBIDDEN")):
            p.run(c, "test-room", "capture")
    except GracefulStop as exc:
        result.put(str(exc))
    except BaseException as exc:
        result.put(type(exc).__name__ + ":" + str(exc))


def manifest_crash_child(path, ready, release, hot):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA cache_size=5")
    conn.execute("BEGIN IMMEDIATE" if hot else "BEGIN EXCLUSIVE")
    if hot:
        conn.execute("UPDATE state SET archive_id='uncommitted-private-value'")
        conn.execute("CREATE TABLE interrupted(payload BLOB)")
        for _ in range(128):
            conn.execute("INSERT INTO interrupted VALUES(?)", (b"x" * 8192,))
    ready.set()
    if hot:
        # SIGKILL must not leave a multiprocessing Condition locked and make
        # the parent's cleanup deadlock on Event.set().
        time.sleep(10)
    else:
        release.wait(10)
    conn.execute("ROLLBACK")
    conn.close()


class StopAfterReply:
    def __init__(self, room, deadline, stop):
        self.stop = stop

    def poll(self, since):
        self.stop.set()
        return Reply(200, "application/json", json.dumps(page(1, 3)).encode())


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("control", "budget", "spool", "archive", "backup"):
            (self.root / name).mkdir(mode=0o700)
        self.c = {"version": 1, "production_id": "offline-observation", "control_dir": str(self.root / "control"),
                  "budget_dir": str(self.root / "budget"), "start_at": time.time() - 5, "end_at": time.time() + 3600,
                  "deadline": 30, "deletion_enabled": False,
                  "budget": {"read_limit": 600, "observer_reserve": 360, "headroom": 120,
                             "capture_rpm": 120, "max_waiters": 4},
                  "clients": [{"name": "existing-observer", "rpm": 360, "waiters": 2}],
                  "rooms": [{"room": "test-room", "class": "high", "interval": 1, "max_rpm": 108,
                             "spool_dir": str(self.root / "spool"), "archive_dir": str(self.root / "archive"),
                             "planned_messages_sec": 0.1,
                             "storage": {"spool_volume_bytes": 1024 * p.MIB, "archive_volume_bytes": 1024 * p.MIB,
                                         "db_bytes": 128 * p.MIB, "wal_bytes": 8 * p.MIB,
                                         "reserve_bytes": 512 * p.MIB, "min_inodes": 4096,
                                         "spool_bytes_message": 2048, "archive_bytes_message": 1536}}]}
        self.r = self.c["rooms"][0]
        self.probe = p.storage_probe
        self.control_probe = p.control_capacity
        # This suite cannot provision/mount filesystems. Kernel boundary checks
        # are tested as rejection below; lifecycle fixtures mock only that seam.
        self.enterContext(patch.object(p, "control_capacity"))
        with patch.object(p, "storage_probe", side_effect=lambda r, role: self.probe(r, role, enforce_boundary=False)):
            p.initialize(self.c)
        self.registry = p.Registry(self.c)
        self.policy = BudgetPolicy(600, 360, 120, 120)

    def worker(self, spool):
        return ArchiveWorker(spool, self.r["archive_dir"], min_free_bytes=0)

    def synthetic_run(self, role="capture"):
        with patch.object(p, "storage_probe", side_effect=lambda r, role: self.probe(r, role, enforce_boundary=False)), \
                patch.object(p, "DeadlineClient", StopAfterReply), \
                patch.object(p, "RoomBudget", return_value=ImmediateBudget()), \
                patch("sys.stdout", new_callable=io.StringIO):
            p.run(self.c, "test-room", role)

    def history_records(self):
        path = self.registry.path / "test-room.capture.metrics.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_history_multiple_samples_throttle_and_histogram_all_requests(self):
        with p.spool_open(self.r, producer=True) as spool:
            history = p.MetricsHistory(self.registry, self.r, "capture")
            client = p.TimedClient(StopAfterReply("test-room", 30, p.SignalStop()))
            with patch.object(p.time, "monotonic", side_effect=[0, 0.05, 1, 3, 4, 35]):
                for _ in range(3):
                    client.poll(0)
            counts = client.histogram()["counts"]
            self.assertEqual(sum(counts), 3)
            self.assertEqual((counts[0], counts[4], counts[-1]), (1, 1, 1))
            metrics = p.snapshot(self.registry, self.r, "capture", spool, {"start_count": 1},
                                 extra={"service_status": "RUNNING", "get_latency_histogram_process": client.histogram(),
                                        "recovery_export_limit_bytes": 32768,
                                        "recovery_export_content_length_bytes": None,
                                        "recovery_export_received_bytes": 32769,
                                        "step": {"body": "PRIVATE-CREDENTIAL"}})
            with patch.object(p.time, "monotonic", side_effect=[0, 29, 30, 60]):
                for _ in range(4):
                    history.append(metrics)
            rows = self.history_records()
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[-1]["get_latency_histogram_process"]["counts"], counts)
            self.assertEqual(rows[-1]["recovery_export_limit_bytes"], 32768)
            self.assertIsNone(rows[-1]["recovery_export_content_length_bytes"])
            self.assertEqual(rows[-1]["recovery_export_received_bytes"], 32769)
            self.assertNotIn("PRIVATE-CREDENTIAL", history.path.read_text())

    def test_history_restart_retains_previous_process_and_latest(self):
        self.synthetic_run()
        path = self.registry.path / "test-room.capture.metrics.jsonl"
        retained = path.read_bytes()
        # Time seam bypasses only the real 30-second backoff, not persisted state.
        future = time.time() + 31
        with patch.object(p.time, "time", return_value=future):
            self.synthetic_run()
        self.assertTrue(path.read_bytes().startswith(retained))
        rows = self.history_records()
        self.assertEqual({row["process_start_count"] for row in rows}, {1, 2})
        self.assertEqual(rows[-1]["service_status"], "STOPPED")
        self.assertLessEqual(rows[-1]["sampled_at"], rows[-1]["observed_at"])
        self.assertEqual(sum(rows[-1]["get_latency_histogram_process"]["counts"]), 1)

    def test_control_capacity_snapshot_latest_and_rotated_history_both_roles(self):
        expected = {"control_volume_bytes": 256 * p.MIB,
                    "control_free_bytes": 24 * p.MIB, "control_free_inodes": 2048}
        values = list(os.statvfs(self.root))
        values[2] = expected["control_volume_bytes"] // values[1]
        values[4] = expected["control_free_bytes"] // values[1]
        values[7] = expected["control_free_inodes"]
        statvfs = p.os.statvfs

        def probe(path):
            return os.statvfs_result(values) if Path(path) == self.registry.path else statvfs(path)

        with p.spool_open(self.r) as spool, patch.object(p.os, "statvfs", side_effect=probe):
            for role in ("capture", "archive"):
                with self.subTest(role=role):
                    metrics = p.snapshot(self.registry, self.r, role, spool, {"start_count": 1},
                                         extra={"service_status": "RUNNING"})
                    latest = p.load(self.registry.path / f"test-room.{role}.metrics.json")
                    for key, value in expected.items():
                        self.assertEqual(metrics[key], value)
                        self.assertEqual(latest[key], value)
                    history = p.MetricsHistory(self.registry, self.r, role)
                    metrics.update(control_dir="/private/path", secret="PRIVATE-CREDENTIAL",
                                   step={"body": "PRIVATE-CREDENTIAL"})
                    with patch.object(p, "HISTORY_SEGMENT_BYTES", 1):
                        history.append(metrics, force=True)
                        original = history.path
                        history.append(metrics, force=True)
                    self.assertNotEqual(history.path, original)
                    for path in (original, history.path):
                        raw = path.read_text()
                        row = json.loads(raw)
                        for key, value in expected.items():
                            self.assertEqual(row[key], value)
                        self.assertNotIn("PRIVATE-CREDENTIAL", raw)
                        self.assertNotIn("/private/path", raw)
                        self.assertNotIn("control_dir", row)
                    # New fields retain the numeric-only history contract.
                    metrics.update({key: "PRIVATE-CREDENTIAL" for key in expected})
                    history.append(metrics, force=True)
                    row = json.loads(history.path.read_text().splitlines()[-1])
                    for key in expected:
                        self.assertNotIn(key, row)

    def test_control_statvfs_failure_stops_both_writers(self):
        statvfs = p.os.statvfs
        original_load = p.load

        def fail_control(path):
            if Path(path) == self.registry.path:
                raise OSError(errno.EIO, "private failure", "/private/path")
            return statvfs(path)

        for role in ("capture", "archive"):
            with self.subTest(role=role), patch.object(p.os, "statvfs", side_effect=fail_control), \
                    patch.object(p, "load", side_effect=lambda path: self.c if path == "fixture-config" else original_load(path)), \
                    patch.object(p, "storage_probe", side_effect=lambda r, role: self.probe(r, role, enforce_boundary=False)), \
                    patch.object(p, "DeadlineClient") as client, patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(p.main(["run", "--config", "fixture-config", "--room", "test-room", "--role", role]), 2)
            client.return_value.poll.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["error_code"], "PRODUCTION_FILESYSTEM_FAILURE")
            self.assertNotIn("private failure", output.getvalue())
            self.assertNotIn("/private/path", output.getvalue())

    def test_control_protective_stop_threshold_unchanged(self):
        values = list(os.statvfs(self.root))
        values[2] = 256 * p.MIB // values[1]
        with patch.object(p.Path, "stat", autospec=True,
                          side_effect=lambda path: Mock(st_dev=1 if path == Path("/") else 2)):
            for free, inodes, stopped in ((16 * p.MIB, 1024, False),
                                          (16 * p.MIB - values[1], 1024, True),
                                          (16 * p.MIB, 1023, True)):
                values[4], values[7] = free // values[1], inodes
                with self.subTest(free=free, inodes=inodes), \
                        patch.object(p.os, "statvfs", return_value=os.statvfs_result(values)):
                    if stopped:
                        with self.assertRaisesRegex(ObserverError, "PRODUCTION_CONTROL_STORAGE_STOP"):
                            self.control_probe(self.c)
                    else:
                        self.control_probe(self.c)

    def test_history_partial_corrupt_tail_and_rotation_never_overwrite(self):
        self.synthetic_run()
        history = p.MetricsHistory(self.registry, self.r, "capture")
        with history.path.open("ab") as file:
            file.write(b'not-json\n{"torn":')
        before = history.path.read_bytes()
        metrics = p.load(self.registry.path / "test-room.capture.metrics.json")
        history.append(metrics)
        after = history.path.read_bytes()
        self.assertTrue(after.startswith(before + b"\n"))
        self.assertEqual(json.loads(after.splitlines()[-1])["producer"]["messages"], 3)
        original = history.path
        with patch.object(p, "HISTORY_SEGMENT_BYTES", len(after)):
            history.append(metrics, force=True)
        self.assertEqual(original.read_bytes(), after)
        self.assertEqual(json.loads(history.path.read_bytes())["producer"]["messages"], 3)

    def test_history_rotations_restart_and_empty_segment_preserve_bytes(self):
        self.synthetic_run()
        metrics = p.load(self.registry.path / "test-room.capture.metrics.json")
        for role in ("capture", "archive"):
            with self.subTest(role=role), patch.object(p, "HISTORY_SEGMENT_BYTES", 4096):
                metrics["role"] = role
                history = p.MetricsHistory(self.registry, self.r, role)
                sealed = {}
                for _ in range(20):
                    previous = history.path
                    raw = previous.read_bytes() if previous.exists() else b""
                    history.append(metrics, force=True)
                    if history.path != previous:
                        sealed[previous] = raw
                    for path, raw in sealed.items():
                        self.assertEqual(path.read_bytes(), raw)
                self.assertGreaterEqual(history.segment, 2)
                retained = history.path.read_bytes()
                restarted = p.MetricsHistory(self.registry, self.r, role)
                self.assertEqual(restarted.path, history.path)
                restarted.append(metrics, force=True)
                self.assertTrue(history.path.read_bytes().startswith(retained))
                for path, raw in sealed.items():
                    self.assertEqual(path.read_bytes(), raw)
                # Crash between exclusive creation and first write.
                empty = restarted.segment_path(restarted.segment + 1)
                empty.touch(mode=0o600)
                restarted = p.MetricsHistory(self.registry, self.r, role)
                restarted.append(metrics, force=True)
                self.assertEqual(restarted.path, empty)
                self.assertEqual(json.loads(empty.read_bytes())["role"], role)

    def test_history_rotation_preserves_torn_tail_unchanged(self):
        self.synthetic_run()
        history = p.MetricsHistory(self.registry, self.r, "capture")
        original = history.path
        with original.open("ab") as file:
            file.write(b'{"torn":')
        retained = original.read_bytes()
        metrics = p.load(self.registry.path / "test-room.capture.metrics.json")
        with patch.object(p, "HISTORY_SEGMENT_BYTES", len(retained)):
            history.append(metrics, force=True)
        self.assertEqual(original.read_bytes(), retained)
        self.assertEqual(json.loads(history.path.read_bytes())["service_status"], "STOPPED")

    def test_writer_runs_and_restarts_across_segment_boundaries(self):
        with patch.object(p, "HISTORY_SEGMENT_BYTES", 1):
            self.synthetic_run()
            history = p.MetricsHistory(self.registry, self.r, "capture")
            retained = {path: path.read_bytes() for path in self.registry.path.glob(history.base.name + "*")}
            self.assertGreaterEqual(history.segment, 1)
            with patch.object(p.time, "time", return_value=time.time() + 31):
                self.synthetic_run()
            for path, raw in retained.items():
                self.assertEqual(path.read_bytes(), raw)
            latest = p.load(self.registry.path / "test-room.capture.metrics.json")
            self.assertEqual(latest["service_status"], "STOPPED")
            self.assertEqual(latest["process_start_count"], 2)
            self.assertEqual(latest["producer"]["messages"], 3)

    def test_history_rotation_creation_failure_remains_fatal(self):
        self.synthetic_run()
        metrics = p.load(self.registry.path / "test-room.capture.metrics.json")
        original_open = p.regular_open
        for failure in (errno.ENOSPC, errno.EIO, errno.EROFS):
            history = p.MetricsHistory(self.registry, self.r, "capture")
            retained = history.path.read_bytes()

            def fail_create(path, flags):
                if flags & os.O_EXCL:
                    raise OSError(failure, "private failure")
                return original_open(path, flags)

            with self.subTest(failure=failure), patch.object(p, "HISTORY_SEGMENT_BYTES", 1), patch.object(p, "regular_open", side_effect=fail_create):
                with self.assertRaises(OSError) as caught:
                    history.append(metrics, force=True)
            self.assertEqual(p.diagnostic(caught.exception, "run")["error_code"], "PRODUCTION_FILESYSTEM_FAILURE")
            self.assertEqual(caught.exception.production_stage, "metrics_history")
            self.assertEqual(history.path.read_bytes(), retained)
            self.assertEqual(history.segment, 0)
            self.assertIsNone(history.last)
        with patch.object(p.os, "fsync", side_effect=OSError(errno.EIO, "private failure")):
            with self.assertRaises(OSError):
                history.append(metrics, force=True)
        self.assertIsNone(history.last)

    def test_history_write_error_stops_writer_with_fatal_exit(self):
        original_open = p.regular_open
        original_load = p.load

        @contextmanager
        def fail_write(path, flags):
            with original_open(path, flags) as file:
                if ".metrics.jsonl" in Path(path).name:
                    proxy = Mock(wraps=file)
                    proxy.write.side_effect = OSError(errno.ENOSPC, "private failure")
                    yield proxy
                else:
                    yield file

        with patch.object(p, "regular_open", side_effect=fail_write), \
                patch.object(p, "load", side_effect=lambda path: self.c if path == "fixture-config" else original_load(path)), \
                patch.object(p, "storage_probe", side_effect=lambda r, role: self.probe(r, role, enforce_boundary=False)), \
                patch.object(p, "DeadlineClient") as client, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(p.main(["run", "--config", "fixture-config", "--room", "test-room", "--role", "capture"]), 2)
        client.return_value.poll.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["error_code"], "PRODUCTION_FILESYSTEM_FAILURE")
        self.assertEqual(result["stage"], "metrics_history")
        self.assertNotIn("private failure", output.getvalue())

    def test_initialize_does_not_reserve_fixed_history_capacity(self):
        c = copy.deepcopy(self.c)
        fresh = self.root / "fresh"
        fresh.mkdir(mode=0o700)
        for name in ("control", "budget", "spool", "archive"):
            (fresh / name).mkdir(mode=0o700)
        c.update(control_dir=str(fresh / "control"), budget_dir=str(fresh / "budget"))
        c["rooms"][0].update(spool_dir=str(fresh / "spool"), archive_dir=str(fresh / "archive"))
        values = list(os.statvfs(self.root))
        values[4] = 16 * p.MIB // values[1]
        statvfs = p.os.statvfs

        def small_control(path):
            return os.statvfs_result(values) if Path(path) == fresh / "control" else statvfs(path)

        # No whole segment fits, but the existing control free-space floor does.
        with patch.object(p.os, "statvfs", side_effect=small_control), \
                patch.object(p, "storage_probe", side_effect=lambda r, role: self.probe(r, role, enforce_boundary=False)):
            self.assertTrue(p.initialize(c)["initialized"])

    def manifest_child(self, hot):
        ctx = multiprocessing.get_context("fork")
        ready, release = ctx.Event(), ctx.Event()
        child = ctx.Process(target=manifest_crash_child,
                            args=(str(self.root / "archive" / "manifest.sqlite"), ready, release, hot))
        child.start()

        def cleanup():
            if child.is_alive():
                release.set()
            child.join(3)
            if child.is_alive():
                child.kill()
                child.join()
        self.addCleanup(cleanup)
        self.assertTrue(ready.wait(5))
        return child, release

    def rejected_capture(self, expected_name, expected_code):
        with patch.object(p, "load", wraps=p.load) as load, \
                patch.object(p, "storage_probe", side_effect=lambda r, role: self.probe(r, role, enforce_boundary=False)), \
                patch.object(p, "DeadlineClient") as client, patch.object(p, "recover_manifest") as recovery, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            # Supply an offline config without adding a fixture file.
            load.side_effect = lambda path: self.c if path == "fixture-config" else load._mock_wraps(path)
            self.assertEqual(p.main(["run", "--config", "fixture-config", "--room", "test-room", "--role", "capture"]), 2)
            client.assert_not_called()
            recovery.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["error_code"], expected_code)
        self.assertEqual(result["sqlite_errorname"], expected_name)
        self.assertEqual(result["exception_class"], "OperationalError")
        self.assertEqual(result["stage"], "manifest_read")
        self.assertFalse((self.registry.path / "test-room.capture.lifecycle.json").exists())

    def test_hot_journal_capture_readonly_then_archive_recovery_and_start(self):
        child, _ = self.manifest_child(True)
        child.kill()
        child.join(5)
        self.assertEqual(child.exitcode, -signal.SIGKILL)
        archive = self.root / "archive"
        journal = archive / "manifest.sqlite-journal"
        self.assertTrue(journal.exists())
        before = {x.name: x.read_bytes() for x in archive.iterdir() if x.is_file()}
        self.rejected_capture("SQLITE_READONLY_ROLLBACK", "PRODUCTION_MANIFEST_RECOVERY_REQUIRED")
        self.assertEqual({x.name: x.read_bytes() for x in archive.iterdir() if x.is_file()}, before)
        with patch.object(p, "storage_probe", side_effect=lambda r, role: self.probe(r, role, enforce_boundary=False)):
            self.assertEqual(p.recover_archive(self.c, "test-room")["manifest_health"], "READY")
        self.assertFalse(journal.exists())
        self.synthetic_run()
        with p.spool_open(self.r) as spool:
            self.assertEqual(self.registry.source(self.r, spool)["archive_id"],
                             self.registry.check()["rooms"]["test-room"]["archive_id"])

    def test_manifest_busy_repeated_preflight_does_not_exhaust_crash_loop(self):
        child, release = self.manifest_child(False)
        for _ in range(4):
            self.rejected_capture("SQLITE_BUSY", "PRODUCTION_MANIFEST_BUSY")
        release.set()
        child.join(5)
        self.assertEqual(child.exitcode, 0)
        self.synthetic_run()
        self.assertEqual(p.load(self.registry.path / "test-room.capture.lifecycle.json")["start_count"], 1)

    def test_archive_start_recovers_before_readonly_source_check(self):
        child, _ = self.manifest_child(True)
        child.kill()
        child.join(5)
        with patch.object(p.SignalStop, "is_set", return_value=True):
            self.synthetic_run("archive")
        with p.spool_open(self.r) as spool:
            self.registry.source(self.r, spool)

    def test_diagnostics_redact_exception_messages_and_preserve_stage(self):
        private = "PRIVATE_TOKEN /private/absolute/path message body"
        cases = [(ValueError(private), "config_validate", "PRODUCTION_CONFIG_FAILURE"),
                 (KeyError(private), "status", "PRODUCTION_STATE_FAILURE"),
                 (OSError(28, private, "/private/absolute/path"), "snapshot", "PRODUCTION_FILESYSTEM_FAILURE"),
                 (ObserverError("PRIVATE_UPPERCASE_CREDENTIAL"), "run", "PRODUCTION_STATE_FAILURE"),
                 (ObserverError("PRODUCTION_IDENTITY_FENCE"), "startup_preflight", "PRODUCTION_IDENTITY_FENCE")]
        for exc, stage, code in cases:
            with self.subTest(stage=stage):
                try:
                    with p.operation(stage):
                        raise exc
                except type(exc) as caught:
                    result = p.diagnostic(caught, "outer")
                self.assertEqual(result["error_code"], code)
                self.assertEqual(result["stage"], stage)
                self.assertEqual(result["exception_class"], type(exc).__name__)
                self.assertIsNone(result["sqlite_errorname"])
                self.assertNotIn("PRIVATE", json.dumps(result))
                self.assertNotIn("/private", json.dumps(result))

    def test_main_config_exception_diagnostic_and_other_sqlite_error(self):
        with patch.object(p, "load", side_effect=ValueError("SECRET /private/path")), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(p.main(["plan", "--config", "fixture"]), 2)
        result = json.loads(output.getvalue())
        self.assertEqual((result["exception_class"], result["stage"]), ("ValueError", "config_load"))
        self.assertNotIn("SECRET", output.getvalue())
        with p.manifest_open(self.r) as conn:
            try:
                conn.execute("SELECT private_column FROM private_table")
            except sqlite3.Error as exc:
                result = p.diagnostic(exc, "manifest_read")
        self.assertEqual(result["sqlite_errorname"], "SQLITE_ERROR")
        self.assertEqual(result["error_code"], "PRODUCTION_MANIFEST_SQLITE")
        self.assertNotIn("private", json.dumps(result))

    def test_config_rejects_missing_target_and_unbound_window(self):
        for change in ({"rooms": []}, {"start_at": None}, {"deadline": 15}, {"deletion_enabled": True},
                       {"end_at": self.c["start_at"] + 86401}, {"extra": 1}):
            with self.subTest(change=change), self.assertRaises(ObserverError):
                p.validate_config({**self.c, **change})

    def test_config_rejects_reserve_waiters_paths_and_capacity(self):
        configs = []
        c = copy.deepcopy(self.c); c["clients"][0]["rpm"] = 361; configs.append(c)
        c = copy.deepcopy(self.c); c["clients"][0]["waiters"] = 4; configs.append(c)
        c = copy.deepcopy(self.c); c["budget"]["headroom"] = 121; configs.append(c)
        c = copy.deepcopy(self.c); c["rooms"][0]["archive_dir"] = c["rooms"][0]["spool_dir"] + "/nested"; configs.append(c)
        c = copy.deepcopy(self.c); c["rooms"][0]["storage"]["db_bytes"] = p.MIB; configs.append(c)
        c = copy.deepcopy(self.c); c["rooms"][0]["max_rpm"] = float("nan"); configs.append(c)
        for c in configs:
            with self.subTest(config=c), self.assertRaises(ObserverError):
                p.validate_config(c)

    def test_plan_service_boundaries_no_executor(self):
        with patch("subprocess.Popen", side_effect=AssertionError("NO_EXECUTOR")):
            self.assertFalse(p.plan(self.c)["deployment_approved"])
            capture = p.container_spec(self.c, self.r, "capture")
            archive = p.container_spec(self.c, self.r, "archive")
        self.assertEqual(archive[archive.index("--network") + 1], "none")
        self.assertIn("--pids-limit", capture)
        self.assertIn("--device-write-iops", archive)
        self.assertIn("type=bind,src=" + self.r["archive_dir"] + ",dst=" + self.r["archive_dir"] + ",readonly", capture)
        self.assertFalse(any("docker.sock" in x or "/home/" in x for x in capture))

    def test_single_identity_different_budget_and_config_rejected(self):
        (self.root / "another-budget").mkdir(mode=0o700)
        for c in ({**self.c, "budget_dir": str(self.root / "another-budget")},
                  {**self.c, "production_id": "different-production"}):
            with self.assertRaisesRegex(ObserverError, "IDENTITY_FENCE"):
                p.Registry(c).check()

    def test_bound_budget_refuses_unbound_and_legacy_clients(self):
        for factory in (lambda: ReservationBudget(self.c["budget_dir"], self.policy),
                        lambda: HostBudget(self.c["budget_dir"], self.policy),
                        lambda: ReservationBudget(self.c["budget_dir"], self.policy, production_binding={"wrong": True})):
            with self.assertRaisesRegex(ObserverError, "PRODUCTION_BUDGET_FENCE"):
                factory()
        with ReservationBudget(self.c["budget_dir"], self.policy, production_binding=self.registry.bound) as budget:
            self.assertEqual(budget.requests, 0)

    def test_duplicate_role_and_independent_archive_lock(self):
        with self.registry.service(self.r, "capture", now=1000):
            with self.assertRaisesRegex(ObserverError, "ALREADY_RUNNING"):
                with self.registry.service(self.r, "capture", now=1000):
                    pass
            with self.registry.service(self.r, "archive", now=1000):
                pass

    def test_restart_history_and_crash_loop_survive_registry_reopen(self):
        for index in range(3):
            with p.Registry(self.c).service(self.r, "capture", now=1000 + index * 31) as (state, _):
                self.assertEqual(state["start_count"], index + 1)
        with self.assertRaisesRegex(ObserverError, "CRASH_LOOP"):
            with self.registry.service(self.r, "capture", now=1093):
                pass
        with self.registry.service(self.r, "capture", now=1603) as (state, _):
            self.assertEqual(state["start_count"], 4)

    def test_room_spacing_persisted_before_request_and_restart(self):
        clock = Clock()
        budget = ImmediateBudget()
        budget.boot, budget.wait_seconds = "fixture-boot", 0
        state = {"boot": budget.boot, "next_read": 1000}
        path = self.root / "control" / "fixture-lifecycle.json"
        with patch.object(p.time, "monotonic", clock):
            first = p.RoomBudget(budget, state, path, self.r, clock)
            with first.request("test-room"):
                expected = 1000 + 60 / 108
                self.assertEqual(p.load(path)["next_read"], expected)
            restarted = p.RoomBudget(budget, p.load(path), path, self.r, clock)
            with restarted.request("test-room"):
                self.assertAlmostEqual(clock.now, expected)
        self.assertEqual(budget.requests, 2)
        self.assertGreater(budget.wait_seconds, 0)

    def test_archive_outage_does_not_stop_capture_and_local_catchup(self):
        with p.spool_open(self.r, producer=True) as spool:
            api = WindowAPI(2)
            capture = FastCapture(spool, api, ImmediateBudget())
            capture.step()
            with self.worker(spool) as worker:
                with patch.object(worker, "_publish", side_effect=OSError("archive fault")):
                    with self.assertRaises(OSError):
                        worker.step()
            api.grow(3)
            capture.step()
            self.assertEqual(spool.state()["cursor"], 5)
            with self.worker(spool) as worker:
                worker.step()
                self.assertEqual(worker.verify()["messages"], 5)

    def test_capture_failure_does_not_change_archive_and_can_drain(self):
        with p.spool_open(self.r, producer=True) as spool:
            spool.ingest(page(1, 3))
            with self.worker(spool) as worker:
                before = worker.state()
                api = WindowAPI()
                api.failure = Reply(503, "application/json", b"{}")
                capture = FastCapture(spool, api, ImmediateBudget())
                self.assertGreater(capture.step(), 0)
                self.assertEqual(worker.state(), before)
                worker.step()
                self.assertEqual(worker.verify()["messages"], 3)

    def test_same_uuid_stale_spool_manifest_ahead_rejected_before_ack(self):
        stale = self.root / "stale.sqlite"
        with p.spool_open(self.r, producer=True) as spool:
            spool.ingest(page(1, 1))
            with sqlite3.connect(stale) as conn:
                spool.conn.backup(conn)
            spool.ingest(page(2, 3))
            with self.worker(spool) as worker:
                worker.step()
        # Simulates a stopped operator restoring an older SQLite snapshot.
        (self.root / "spool" / "spool.sqlite").write_bytes(stale.read_bytes())
        with p.spool_open(self.r, producer=True) as spool:
            with self.assertRaisesRegex(ObserverError, "MANIFEST_AHEAD_OF_SOURCE"):
                self.worker(spool)
            self.assertEqual(spool.consumer("archive")["ack_entry"], 0)

    def test_same_tip_divergence_and_pending_restore_are_rejected(self):
        with p.spool_open(self.r, producer=True) as spool:
            spool.ingest(page(1, 2))
            with self.worker(spool) as worker:
                worker.step()
                spool.conn.execute("UPDATE entries SET payload=? WHERE seq=2 AND kind='MESSAGE'",
                                   (canonical({"seq": 2, "text": "changed"}),))
                with self.assertRaisesRegex(ObserverError, "SOURCE_DIVERGED"):
                    validate_source(spool, worker.conn)

    def test_pending_publication_cannot_replay_against_divergent_spool(self):
        with p.spool_open(self.r, producer=True) as spool:
            spool.ingest(page(1, 2))
            with self.worker(spool) as worker:
                with patch.object(worker, "_publish", side_effect=OSError("interrupted")):
                    with self.assertRaises(OSError):
                        worker.step()
                spool.conn.execute("UPDATE entries SET payload=? WHERE seq=2 AND kind='MESSAGE'",
                                   (canonical({"seq": 2, "text": "changed"}),))
                with self.assertRaisesRegex(ObserverError, "PENDING_SOURCE"):
                    worker.step()

    def test_control_floor_rejects_stale_pair_and_identity_substitution(self):
        with p.spool_open(self.r, producer=True) as spool:
            spool.ingest(page(1, 2))
            self.registry.floor(self.r, spool)
            floor = p.load(self.registry.path / "test-room.floor.json")
            p.atomic(self.registry.path / "test-room.floor.json", {**floor, "high_entry": floor["high_entry"] + 1})
            with self.assertRaisesRegex(ObserverError, "STALE_SPOOL"):
                self.registry.source(self.r, spool)
            p.atomic(self.registry.path / "test-room.floor.json", floor)
            spool.conn.execute("UPDATE state SET spool_id='different'")
            with self.assertRaisesRegex(ObserverError, "SPOOL_IDENTITY"):
                self.registry.source(self.r, spool)

    def test_archive_identity_and_ack_receipt_substitution_rejected(self):
        with p.spool_open(self.r, producer=True) as spool:
            spool.ingest(page(1, 2))
            with self.worker(spool) as worker:
                worker.step()
                worker.conn.execute("UPDATE state SET archive_id='substituted'")
            with self.assertRaisesRegex(ObserverError, "ARCHIVE_IDENTITY"):
                self.registry.source(self.r, spool)
            saved = self.registry.check()["rooms"]["test-room"]["archive_id"]
            with sqlite3.connect(self.root / "archive" / "manifest.sqlite") as conn:
                conn.execute("UPDATE state SET archive_id=?", (saved,))
            spool.conn.execute("UPDATE consumers SET receipt=?", ("0" * 64,))
            with self.assertRaisesRegex(ObserverError, "ACK_RECEIPT_MISMATCH"):
                self.worker(spool)

    def test_resource_stop_precedes_transport_creation(self):
        with patch.object(p, "storage_probe", side_effect=ObserverError("PRODUCTION_STORAGE_STOP")), \
                patch.object(p, "DeadlineClient") as client:
            with self.assertRaisesRegex(ObserverError, "STORAGE_STOP"):
                p.run(self.c, "test-room", "capture")
            client.assert_not_called()
        with p.spool_open(self.r) as spool:
            self.assertEqual(spool.state()["cursor"], 0)

    def test_restart_backoff_and_duplicate_json_fail_closed(self):
        with self.registry.service(self.r, "capture", now=1000):
            pass
        with self.assertRaisesRegex(ObserverError, "RESTART_BACKOFF"):
            with self.registry.service(self.r, "capture", now=1001):
                pass
        path = self.root / "invalid.json"
        path.write_text('{"deletion_enabled": false, "deletion_enabled": true}')
        with self.assertRaisesRegex(ObserverError, "DUPLICATE_JSON_KEY"):
            p.load(path)

    def test_unbounded_storage_and_low_free_inodes_stop(self):
        with self.assertRaisesRegex(ObserverError, "BOUNDED_CONTROL"):
            self.control_probe(self.c)
        with self.assertRaisesRegex(ObserverError, "DEDICATED_FILESYSTEM"):
            p.storage_probe(self.r, "capture")
        actual = os.statvfs(self.root)
        for free, inodes in ((1, 10000), (1024 ** 3, 0)):
            values = list(actual)
            values[4], values[7] = free // actual.f_frsize, inodes
            with patch.object(p.os, "statvfs", return_value=os.statvfs_result(values)):
                with self.assertRaisesRegex(ObserverError, "STORAGE_STOP"):
                    p.storage_probe(self.r, "capture", enforce_boundary=False)

    def test_monitoring_consistent_with_durable_state_and_ack(self):
        with p.spool_open(self.r, producer=True) as spool:
            spool.ingest(page(301, 303))
            first = p.snapshot(self.registry, self.r, "capture", spool, {"start_count": 2})
            self.assertEqual(first["archive_lag_entries"], spool.state()["high_entry"])
            self.assertEqual(first["producer"]["gaps"], 1)
            self.assertEqual(first["process_restart_count"], 1)
            with self.worker(spool) as worker:
                worker.step()
            after = p.snapshot(self.registry, self.r, "capture", spool, {"start_count": 2})
            self.assertEqual(after["archive_lag_entries"], 0)
            self.assertEqual(after["processed_through"], spool.state()["high_entry"])
            self.assertGreater(after["logical_db_bytes"], 0)
            self.assertEqual(p.load(self.registry.path / "test-room.capture.metrics.json"), after)

    def test_stopped_backup_hashes_and_restore_full_validation(self):
        with p.spool_open(self.r, producer=True) as spool:
            spool.ingest(page(1, 5))
            self.registry.floor(self.r, spool)
            with self.worker(spool) as worker:
                worker.step()
        with patch.object(p, "backup_capacity"):
            result = p.backup(self.c, self.root / "backup")
        self.assertTrue(p.verify_backup(self.root / "backup", result["inventory_sha256"])["backup_verified"])
        with p.spool_open(self.r, producer=True) as spool:
            spool.ingest(page(6, 7))
        # Restore at the same logical path, with retained independent floor=5.
        # No production files or non-fixture paths are touched.
        (self.root / "spool" / "spool.sqlite").write_bytes(
            (self.root / "backup" / "test-room" / "spool.sqlite").read_bytes())
        self.assertTrue(p.verify_restore(self.c)["restore_verified"])
        with p.spool_open(self.r, producer=True) as spool:
            self.assertEqual(spool.state()["cursor"], 5)
            spool.ingest(page(6, 7))
            with self.worker(spool) as worker:
                worker.step()
                self.assertEqual(worker.verify()["messages"], 7)
        inventory = p.load(self.root / "backup" / "inventory.json")
        self.assertEqual(inventory["rooms"]["test-room"]["spool"]["cursor"], 5)
        uri = (self.root / "backup" / "test-room" / "spool.sqlite").as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
        with (self.root / "backup" / "test-room" / "spool.sqlite").open("ab") as file:
            file.write(b"corrupted")
        with self.assertRaisesRegex(ObserverError, "BACKUP_HASH"):
            p.verify_backup(self.root / "backup", result["inventory_sha256"])

    def test_backup_refuses_running_service_and_nonempty_destination(self):
        with self.registry.service(self.r, "capture"), patch.object(p, "backup_capacity"):
            with self.assertRaisesRegex(ObserverError, "ALREADY_RUNNING"):
                p.backup(self.c, self.root / "backup")
        (self.root / "backup" / "retained-evidence").write_text("do not replace")
        with self.assertRaisesRegex(ObserverError, "EMPTY_BACKUP"):
            p.backup(self.c, self.root / "backup")

    def test_restore_rejects_old_corruption_even_when_tip_matches(self):
        with p.spool_open(self.r, producer=True) as spool:
            spool.ingest(page(1, 3))
            with self.worker(spool) as worker:
                worker.step()
                before = worker.state()
            spool.ingest(page(4, 4))
            spool.conn.execute("UPDATE entries SET payload=? WHERE seq=1 AND kind='MESSAGE'",
                               (canonical({"seq": 1, "text": "tampered old record"}),))
        with self.assertRaisesRegex(ObserverError, "SOURCE_DIVERGED"):
            p.verify_restore(self.c)
        with p.manifest_open(self.r) as conn:
            self.assertEqual(dict(conn.execute("SELECT * FROM state WHERE singleton=1").fetchone()), before)

    def test_signal_stop_does_not_reenter_condition_lock(self):
        stop = p.SignalStop()
        previous = signal.signal(signal.SIGUSR1, lambda *_: stop.set())
        try:
            with patch.object(threading.Event, "set", side_effect=AssertionError("SIGNAL_LOCK_REENTRY")):
                signal.raise_signal(signal.SIGUSR1)
                self.assertTrue(stop.wait(1))
                self.assertTrue(stop.is_set())
        finally:
            signal.signal(signal.SIGUSR1, previous)

    def test_graceful_sigterm_interrupts_budget_wait_without_get(self):
        ctx = multiprocessing.get_context("fork")
        ready, result = ctx.Event(), ctx.Queue()
        child = ctx.Process(target=stop_child, args=(self.c, ready, result))
        child.start()
        try:
            self.assertTrue(ready.wait(5))
            os.kill(child.pid, signal.SIGTERM)
            child.join(5)
            self.assertFalse(child.is_alive())
            self.assertEqual(result.get(timeout=1), "CAPTURE_STOP_REQUESTED")
            self.assertEqual(child.exitcode, 0)
        finally:
            if child.is_alive():
                child.kill()
                child.join()
            result.close()
        with p.spool_open(self.r) as spool:
            self.assertEqual(spool.state()["cursor"], 0)

    def test_window_cannot_be_reset_by_restart(self):
        expired = {**self.c, "start_at": time.time() - 1000, "end_at": time.time() - 1}
        with self.assertRaisesRegex(ObserverError, "OUTSIDE_WINDOW"):
            p.run(expired, "test-room", "capture")

    def test_standing_config_requires_explicit_version_and_checkpoint(self):
        c = {**self.c, "version": 2, "end_at": None, "observation_checkpoint_seconds": 86400}
        p.validate_config(c)
        for change in ({"version": 1}, {"observation_checkpoint_seconds": 3600},
                       {"end_at": c["start_at"] + 86400}, {"start_at": None}):
            with self.subTest(change=change), self.assertRaises(ObserverError):
                p.validate_config({**c, **change})

    def test_standing_run_after_checkpoint_has_no_expiry_timer(self):
        # Exercise the actual lifecycle with immutable continuous config and a
        # synthetic reply, 25 hours after start; no network and no real wait.
        self.c.update(version=2, end_at=None, observation_checkpoint_seconds=86400)
        self.c["start_at"] = time.time() - 90000
        # Preserve source identity, but use a separate empty control registry to
        # initialize this independently authorized config (not a live migration).
        control, budget, spool, archive = [self.root / ("continuous-" + name)
                                            for name in ("control", "budget", "spool", "archive")]
        for path in (control, budget, spool, archive):
            path.mkdir(mode=0o700)
        self.c.update(control_dir=str(control), budget_dir=str(budget))
        self.r.update(spool_dir=str(spool), archive_dir=str(archive))
        with patch.object(p, "storage_probe", side_effect=lambda r, role: self.probe(r, role, enforce_boundary=False)):
            p.initialize(self.c)
        with patch.object(p.threading, "Timer", side_effect=AssertionError("no expiry timer")):
            self.synthetic_run()
        metrics = p.load(control / "test-room.capture.metrics.json")
        self.assertEqual(metrics["producer"]["cursor"], 3)
        self.assertEqual(metrics["service_status"], "STOPPED")

    def test_production_capture_metrics_with_fake_transport_only(self):
        class Client:
            def __init__(self, room, deadline, stop):
                self.stop = stop

            def poll(self, since):
                self.stop.set()
                return Reply(200, "application/json", json.dumps(page(1, 3)).encode())

        with patch.object(p, "storage_probe", side_effect=lambda r, role: self.probe(r, role, enforce_boundary=False)), \
                patch.object(p, "DeadlineClient", Client), patch.object(p, "RoomBudget", return_value=ImmediateBudget()), \
                patch("sys.stdout", new_callable=io.StringIO):
            p.run(self.c, "test-room", "capture")
        result = p.load(self.registry.path / "test-room.capture.metrics.json")
        self.assertEqual(result["producer"]["cursor"], 3)
        self.assertGreater(result["messages_sec"], 0)
        self.assertIsNotNone(result["get_latency_seconds"])
        self.assertEqual(result["deadline_failures_process"], 0)
        self.assertEqual(result["service_status"], "STOPPED")
        with p.spool_open(self.r) as spool:
            self.registry.source(self.r, spool)


if __name__ == "__main__":
    unittest.main()
