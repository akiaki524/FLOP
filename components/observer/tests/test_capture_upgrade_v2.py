"""Offline adversarial checks for V2 authority and one-shot boundaries."""
import contextlib
import io
import json
import os
import signal
import stat
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/production-capture-upgrade-v2"))
sys.path.insert(0, str(ROOT / "src"))
from v2 import activate, artifact, common, monitor_runner, reconcile, stage, stop, preflight, writer_precheck
from v2.common import (CANDIDATE, LABELS, Layout, V2Error, docker_inspect,
                       exec_fingerprint, read_json, sha256, unit_fingerprint, unit_state)

ATTEMPT = "20260923-v2-01"
RELEASE = "a" * 40
IMAGE = "sha256:" + "b" * 64
CAPTURE = "c" * 64
ARCHIVE = "d" * 64
FOREIGN = "e" * 64


def result(code=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def row(cid, role, *, running=False, attempt=ATTEMPT, image=IMAGE):
    return {"Id": cid, "Image": image,
            "Config": {"Labels": {LABELS["attempt"]: attempt, LABELS["role"]: role,
                                  LABELS["candidate"]: CANDIDATE, LABELS["release"]: RELEASE}},
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
            "State": {"Running": running, "Pid": 37 if running else 0,
                      "Status": "running" if running else "created", "OOMKilled": False}}


class FakeDocker:
    def __init__(self, containers=None):
        self.containers = containers or {}
        self.calls = []
        self.fail_archive_start = False

    def run(self, argv, *, timeout=60):
        self.calls.append(list(argv))
        if argv[:3] == ["docker", "inspect", "--type=container"]:
            cid = argv[3]
            return result(stdout=json.dumps([self.containers[cid]])) if cid in self.containers else result(
                1, stderr="Error: No such object: " + cid)
        if argv[:2] == ["docker", "stop"]:
            cid = argv[-1]
            self.containers[cid]["State"].update(Running=False, Pid=0, Status="exited")
            return result(stdout=cid)
        if argv[:3] == ["systemctl", "start", "technocore-capture-capture.service"]:
            self.containers[CAPTURE]["State"].update(Running=True, Pid=37, Status="running")
            return result()
        if argv[:3] == ["systemctl", "start", "technocore-capture-archive.service"]:
            return result(1) if self.fail_archive_start else result()
        if argv[:2] == ["docker", "build"]:
            return result(1)
        return result()


def layout(base):
    root = Path(base)
    for name in ("release", "state", "observation", "dropins", "etc", "shared"):
        (root / name).mkdir()
    return Layout(root / "release", root / "state", root / "observation",
                  root / "dropins", root / "etc", root / "shared")


def old_exec(name):
    return f"{{ path=/bin/{name} ; argv[]=/bin/{name} ; pid=0 ; status=0/0 }}"


def staged():
    unit = {"ExecStartPre": exec_fingerprint(old_exec("old-pre")),
            "ExecStart": exec_fingerprint(old_exec("old-start")),
            "ExecStartPost": exec_fingerprint(""),
            "ExecStop": exec_fingerprint(old_exec("old-stop")),
            "ExecStopPost": exec_fingerprint(""), "Restart": "no",
            "DropInPaths": sha256(b""), "DropInPathsList": [], "ActiveState": "inactive",
            "KillMode": "control-group", "TimeoutStartUSec": "3min",
            "TimeoutStopUSec": "1min 30s"}
    return {"attempt_id": ATTEMPT, "image_id": IMAGE, "release_commit": RELEASE,
            "container_ids": {"capture": CAPTURE, "archive": ARCHIVE},
            "baseline": {"units": {role: unit.copy() for role in ("capture", "archive", "monitor")},
                         "timer": "inactive", "network": {"network": "bridge", "network_id": "f" * 64},
                         "policy_sha256": "f" * 64, "mounts": {}},
            "metrics_baseline": {"gaps": 0, "messages": 2, "archive_processed_through": 2}}


class SystemdShowFields(unittest.TestCase):
    def test_exec_fingerprints_ignore_runtime_metadata_but_reject_argv_change(self):
        fields = ("ExecStartPre", "ExecStart", "ExecStartPost", "ExecStop", "ExecStopPost")
        for role in ("capture", "archive", "monitor"):
            for field in fields:
                with self.subTest(role=role, field=field):
                    before = "{ path=/bin/true ; argv[]=/bin/true --old ; ignore_errors=no ; pid=11 ; start_time=one ; stop_time=two ; code=exited ; status=0/0 }"
                    after = "{ path=/bin/true ; argv[]=/bin/true --old ; ignore_errors=no ; pid=22 ; start_time=three ; stop_time=four ; code=killed ; status=1/FAILURE }"
                    changed = after.replace("argv[]=/bin/true --old", "argv[]=/bin/true --new")
                    unit = {key: "" for key in fields}
                    unit.update({"DropInPaths": "", "Restart": "no", "ActiveState": "inactive",
                                 "KillMode": "control-group", "TimeoutStartUSec": "3min",
                                 "TimeoutStopUSec": "1min 30s"})
                    with patch.object(common, "unit_state", return_value=unit):
                        unit[field] = before
                        baseline = unit_fingerprint(None, role)
                        unit[field] = after
                        self.assertEqual(unit_fingerprint(None, role), baseline)
                        unit[field] = changed
                        self.assertNotEqual(unit_fingerprint(None, role)[field], baseline[field])
                    self.assertNotEqual(exec_fingerprint(before), exec_fingerprint(
                        after.replace("path=/bin/true", "path=/bin/false")))
                    self.assertNotEqual(exec_fingerprint(before), exec_fingerprint(
                        after.replace("ignore_errors=no", "ignore_errors=yes")))
        with self.assertRaisesRegex(V2Error, "SYSTEMD_EXEC_ARGV"):
            exec_fingerprint("{ path=/bin/true ; pid=22 ; status=0/0 }")

    def test_systemd_255_omitted_empty_exec_properties(self):
        common = {"Restart": "no", "DropInPaths": "", "ActiveState": "inactive",
                  "KillMode": "control-group", "TimeoutStartUSec": "3min",
                  "TimeoutStopUSec": "1min 30s"}
        required_exec = {
            "capture": {"ExecStartPre": old_exec("capture-pre"),
                        "ExecStart": old_exec("capture-start"),
                        "ExecStop": old_exec("capture-stop")},
            "archive": {"ExecStartPre": old_exec("archive-pre"),
                        "ExecStart": old_exec("archive-start"),
                        "ExecStop": old_exec("archive-stop")},
            "monitor": {"ExecStart": old_exec("monitor-start")},
        }

        class ShowRunner:
            def __init__(self, values):
                self.values = values

            def run(self, argv, *, timeout=60):
                return result(stdout="".join(f"{key}={value}\n" for key, value in
                                             self.values.items()))

        for role, exec_values in required_exec.items():
            with self.subTest(role=role):
                shown = {**common, **exec_values}
                runner = ShowRunner(shown)
                state = unit_state(runner, role)
                omitted = {"ExecStartPre", "ExecStart", "ExecStartPost", "ExecStop",
                           "ExecStopPost"} - exec_values.keys()
                self.assertEqual({key: state[key] for key in omitted},
                                 {key: "" for key in omitted})
                fingerprint = unit_fingerprint(runner, role)
                for key in omitted:
                    self.assertEqual(fingerprint[key], exec_fingerprint(""))
                for key in exec_values:
                    self.assertEqual(fingerprint[key], exec_fingerprint(exec_values[key]))
                for key in shown:
                    with self.subTest(role=role, missing=key):
                        missing = {k: v for k, v in shown.items() if k != key}
                        with self.assertRaisesRegex(V2Error, "SYSTEMD_SHOW_FIELDS"):
                            unit_state(ShowRunner(missing), role)


class PreflightResources(unittest.TestCase):
    def test_missing_release_namespace_uses_existing_opt_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            opt = Path(tmp) / "opt"
            opt.mkdir()
            release_namespace = opt / "technocore-capture"
            l = Layout(release_base=release_namespace / "upgrade-v2")
            self.assertFalse(release_namespace.exists())
            vfs = SimpleNamespace(f_bavail=2 * 1024**3, f_frsize=1, f_favail=10000)
            with patch.object(preflight.os, "statvfs", return_value=vfs) as statvfs, \
                    patch.object(preflight.Path, "read_text", return_value="MemAvailable: 1048576 kB\n"):
                result = preflight.resources(l)
            statvfs.assert_called_once_with(opt)
            self.assertEqual(result, {"disk_available": 2 * 1024**3,
                                      "inodes_available": 10000,
                                      "memory_available": 1024**3})
            self.assertFalse(release_namespace.exists())

    def test_existing_release_namespace_is_checked_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            namespace = Path(tmp) / "opt" / "technocore-capture"
            namespace.mkdir(parents=True)
            l = Layout(release_base=namespace / "upgrade-v2")
            vfs = SimpleNamespace(f_bavail=2 * 1024**3, f_frsize=1, f_favail=10000)
            with patch.object(preflight.os, "statvfs", return_value=vfs) as statvfs, \
                    patch.object(preflight.Path, "read_text", return_value="MemAvailable: 1048576 kB\n"):
                preflight.resources(l)
            statvfs.assert_called_once_with(namespace)

    def test_abnormal_release_namespace_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            opt = Path(tmp) / "opt"
            opt.mkdir()
            namespace = opt / "technocore-capture"
            l = Layout(release_base=namespace / "upgrade-v2")
            for abnormal in ("file", "symlink"):
                with self.subTest(abnormal=abnormal):
                    if abnormal == "file":
                        namespace.write_text("unexpected")
                    else:
                        namespace.symlink_to(opt, target_is_directory=True)
                    with patch.object(preflight.os, "statvfs") as statvfs:
                        with self.assertRaisesRegex(V2Error, "RESOURCE_RELEASE_PATH"):
                            preflight.resources(l)
                    statvfs.assert_not_called()
                    namespace.unlink()


class ExactAuthority(unittest.TestCase):
    def test_stale_and_foreign_ids_cannot_stop_current_writer(self):
        runner = FakeDocker({CAPTURE: row(CAPTURE, "capture", running=True),
                             FOREIGN: row(FOREIGN, "capture", running=True, attempt="20260923-v2-02")})
        stale = "0" * 64
        self.assertEqual(stop.exact_stop(runner, cid=stale, image=IMAGE, attempt=ATTEMPT,
                                         role="capture", release=RELEASE), "ABSENT")
        self.assertEqual(stop.exact_stop(runner, cid=FOREIGN, image=IMAGE, attempt=ATTEMPT,
                                         role="capture", release=RELEASE), "TARGET_MISMATCH")
        self.assertTrue(runner.containers[FOREIGN]["State"]["Running"])
        self.assertEqual(stop.exact_stop(runner, cid=CAPTURE, image=IMAGE, attempt=ATTEMPT,
                                         role="capture", release=RELEASE), "STOPPED")
        self.assertEqual([x for x in runner.calls if x[:2] == ["docker", "stop"]],
                         [["docker", "stop", "--time", "40", CAPTURE]])
        self.assertTrue(all("systemctl" not in x for x in runner.calls))

    def test_mismatch_image_and_restart_policy_fail_closed(self):
        bad = row(CAPTURE, "capture", running=True, image="sha256:" + "0" * 64)
        runner = FakeDocker({CAPTURE: bad})
        self.assertEqual(stop.exact_stop(runner, cid=CAPTURE, image=IMAGE, attempt=ATTEMPT,
                                         role="capture", release=RELEASE), "TARGET_MISMATCH")
        bad["Image"] = IMAGE
        bad["HostConfig"]["RestartPolicy"]["Name"] = "always"
        self.assertEqual(stop.exact_stop(runner, cid=CAPTURE, image=IMAGE, attempt=ATTEMPT,
                                         role="capture", release=RELEASE), "TARGET_MISMATCH")
        self.assertFalse(any(x[:2] == ["docker", "stop"] for x in runner.calls))

    def test_seam_is_closed_and_fatal_path_uses_only_bound_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            packet = SimpleNamespace(read_json=lambda p: {}, write_new=lambda *a: None,
                                     sync_dir=lambda *a: None, require=lambda *a: None)
            runner = FakeDocker({CAPTURE: row(CAPTURE, "capture", running=True),
                                 ARCHIVE: row(ARCHIVE, "archive", running=True)})
            binding = {"attempt": ATTEMPT, "release": RELEASE, "image": IMAGE,
                       "capture_id": CAPTURE, "archive_id": ARCHIVE}
            seam = monitor_runner.PacketSeam(packet, layout=l, attempt=ATTEMPT,
                                             runner=runner, binding=binding)
            with self.assertRaises(AttributeError):
                seam.stage50
            with self.assertRaises(V2Error):
                seam.stop_roles(("observer",))
            seam.stop_roles(("capture", "archive"))
            self.assertTrue(stop.stopped(seam.stop_results))
            self.assertEqual([x[-1] for x in runner.calls if x[:2] == ["docker", "stop"]],
                             [CAPTURE, ARCHIVE])
            self.assertFalse(any("systemctl" in x or "tc-cap-loop-01-lobby" in " ".join(x)
                                 for x in runner.calls))

    def test_monitor_exception_and_sigterm_path_bind_before_file_read(self):
        runner = FakeDocker({CAPTURE: row(CAPTURE, "capture", running=True),
                             ARCHIVE: row(ARCHIVE, "archive", running=True)})
        with patch.object(monitor_runner, "load_candidate", side_effect=monitor_runner.StopSignal), \
                contextlib.redirect_stdout(io.StringIO()):
            code = monitor_runner.run_bound(attempt=ATTEMPT, capture_id=CAPTURE,
                                            archive_id=ARCHIVE, image=IMAGE, release=RELEASE,
                                            runner=runner)
        self.assertEqual(code, 2)
        self.assertEqual([x[-1] for x in runner.calls if x[:2] == ["docker", "stop"]],
                         [CAPTURE, ARCHIVE])


class TransactionBoundaries(unittest.TestCase):
    def test_first_stage_creates_only_missing_release_parent_and_records_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            opt = Path(tmp) / "opt"
            opt.mkdir()
            l = Layout(release_base=opt / "technocore-capture" / "upgrade-v2",
                       state_base=l.state_base, observation_base=l.observation_base,
                       dropin_base=l.dropin_base, shared_etc=l.shared_etc,
                       shared_root=l.shared_root)
            source = Path(tmp) / "checkout"
            manifest = source / "deploy/production-capture-upgrade-v2/release-manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}")
            self.assertFalse(l.release_base.parent.exists())
            runner = FakeDocker()
            with patch.object(stage, "verify_source", return_value={}), \
                    patch.object(stage, "verify_checkout"), \
                    patch.object(stage, "snapshot", return_value={}), \
                    patch.object(stage, "copy_reviewed", return_value=l.release(ATTEMPT)):
                with self.assertRaisesRegex(V2Error, "IMAGE_BUILD_FAILED"):
                    stage.stage(runner, l, source_root=source, attempt=ATTEMPT,
                                release=RELEASE, human_approved=True)
            self.assertTrue(l.release_base.is_dir())
            self.assertEqual(l.release_base.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(read_json(l.state(ATTEMPT) / "terminal.json")["primary_failure"],
                             "IMAGE_BUILD_FAILED")
            self.assertTrue((l.state(ATTEMPT) / "attempt.json").is_file())

    def test_abnormal_release_parent_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            opt = Path(tmp) / "opt"
            opt.mkdir()
            parent = opt / "technocore-capture"
            release = parent / "upgrade-v2"
            for abnormal in ("file", "symlink", "wrong_owner"):
                with self.subTest(abnormal=abnormal):
                    if abnormal == "file":
                        parent.write_text("unexpected")
                    else:
                        parent.mkdir()
                        if abnormal == "symlink":
                            parent.rmdir()
                            parent.symlink_to(opt, target_is_directory=True)
                    original_lstat = Path.lstat

                    def lstat(path, *args, **kwargs):
                        row = original_lstat(path, *args, **kwargs)
                        if abnormal == "wrong_owner" and path == parent:
                            return SimpleNamespace(st_mode=row.st_mode, st_uid=-1)
                        return row

                    with patch.object(Path, "lstat", lstat):
                        with self.assertRaises((OSError, V2Error)):
                            stage._private_namespace(parent)
                            stage._private_namespace(release)
                    self.assertFalse(release.exists())
                    if parent.is_dir() and not parent.is_symlink():
                        parent.rmdir()
                    else:
                        parent.unlink()

    def test_stage_build_failure_has_no_shared_switch_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            source = Path(tmp) / "checkout"
            manifest = source / "deploy/production-capture-upgrade-v2/release-manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}")
            runner = FakeDocker()
            with patch.object(stage, "verify_source", return_value={}), \
                    patch.object(stage, "verify_checkout"), \
                    patch.object(stage, "snapshot", return_value={}), \
                    patch.object(stage, "copy_reviewed", return_value=l.release(ATTEMPT)):
                with self.assertRaises(V2Error):
                    stage.stage(runner, l, source_root=source, attempt=ATTEMPT,
                                release=RELEASE, human_approved=True)
            terminal = read_json(l.state(ATTEMPT) / "terminal.json")
            self.assertEqual(terminal["outcome"], "STAGE_FAILED")
            self.assertEqual(terminal["primary_failure"], "IMAGE_BUILD_FAILED")
            self.assertFalse(any(x[0] == "systemctl" or x[:2] == ["docker", "start"]
                                 for x in runner.calls))
            self.assertFalse(any(l.switch(role, ATTEMPT).exists()
                                 for role in ("capture", "archive", "monitor")))

    def test_partial_start_preserves_primary_failure_and_rolls_back_own_switches(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            state = l.state(ATTEMPT)
            state.mkdir()
            (state / "staged.json").write_text(json.dumps(staged()))
            runner = FakeDocker({CAPTURE: row(CAPTURE, "capture"), ARCHIVE: row(ARCHIVE, "archive")})
            runner.fail_archive_start = True
            baseline_unit = {"ExecStartPre": old_exec("old-pre"), "ExecStart": old_exec("old-start"),
                             "ExecStartPost": "", "ExecStop": old_exec("old-stop"),
                             "ExecStopPost": "", "Restart": "no",
                             "DropInPaths": "", "ActiveState": "inactive",
                             "KillMode": "control-group", "TimeoutStartUSec": "3min",
                             "TimeoutStopUSec": "1min 30s"}
            with patch.object(activate, "fresh_before_activation"), \
                    patch.object(activate, "verify_staged"), \
                    patch.object(activate, "verify_effective"), \
                    patch.object(activate, "unit_state", return_value=baseline_unit), \
                    patch.object(activate, "timer_state", return_value="inactive"):
                with self.assertRaises(V2Error):
                    activate.activate(runner, l, attempt=ATTEMPT, human_approved=True,
                                      clock=lambda: 1000)
            terminal = read_json(state / "terminal.json")
            self.assertEqual(terminal["primary_failure"], "ARCHIVE_START_FAILED")
            self.assertEqual(terminal["outcome"], "ACTIVATION_FAILED_ROLLED_BACK")
            self.assertEqual(terminal["stop_results"],
                             {"capture": "STOPPED", "archive": "ALREADY_STOPPED"})
            self.assertFalse(any(l.switch(role, ATTEMPT).exists()
                                 for role in ("capture", "archive", "monitor")))
            self.assertFalse(any(x[:3] == ["systemctl", "start", "tc-cap-loop-01-lobby-capture"]
                                 for x in runner.calls))
            self.assertNotIn(["systemctl", "start", "technocore-capture-monitor.timer"],
                             runner.calls)  # inactive baseline stays inactive

    def test_foreign_switch_blocks_rollback_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            path = l.switch("capture", ATTEMPT)
            path.parent.mkdir(parents=True)
            path.write_text("foreign")
            begin = {"switch_sha256": {"capture": "0" * 64},
                     "baseline_units": {}, "baseline_timer": "inactive"}
            runner = FakeDocker()
            self.assertEqual(activate.rollback_switch(runner, l, ATTEMPT, begin,
                stop_results={"capture": "STOPPED", "archive": "ALREADY_STOPPED"}),
                "RECONCILIATION_REQUIRED")
            self.assertEqual(path.read_text(), "foreign")
            self.assertFalse(any(x[0] == "systemctl" for x in runner.calls))

    def test_reconcile_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            state = l.state(ATTEMPT)
            state.mkdir()
            (state / "staged.json").write_text(json.dumps(staged()))
            runner = FakeDocker({CAPTURE: row(CAPTURE, "capture"),
                                 ARCHIVE: row(ARCHIVE, "archive")})
            with patch.object(reconcile, "unit_state", return_value={"ActiveState": "inactive"}), \
                    patch.object(reconcile, "timer_state", return_value="inactive"), \
                    patch.object(reconcile, "_named_container", return_value=None):
                before = (state / "staged.json").read_bytes()
                report = reconcile.reconcile(runner, l, ATTEMPT)
            self.assertEqual(report["containers"], {"capture": "STOPPED", "archive": "STOPPED"})
            self.assertEqual((state / "staged.json").read_bytes(), before)
            self.assertFalse(any(x[:2] in (["docker", "stop"], ["docker", "start"],
                                             ["systemctl", "start"], ["systemctl", "stop"])
                                 for x in runner.calls))

    def test_release_allowlist_rejects_evolved_current_bytes(self):
        # The V2 release remains pinned to its historical source. Issue #32
        # changes the current Core, so staging this checkout must fail closed.
        with self.assertRaisesRegex(V2Error, "RELEASE_FILE_HASH"):
            artifact.verify_source(ROOT)

class FreshnessAndIsolation(unittest.TestCase):
    def test_short_smoke_requires_fresh_post_activation_progress_and_exact_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            l.observation(ATTEMPT).mkdir()
            (l.observation(ATTEMPT) / "latest.json").write_text(json.dumps({
                "samples": 3, "observed_at": 1230, "failures": [],
                "stop_applied": False, "verdict": "PASS"}))
            registry = l.shared_root / "control/registry"
            registry.mkdir(parents=True)
            capture = {"observed_at": 1235, "producer": {"messages": 3, "gaps": 0}}
            archive = {"observed_at": 1235, "processed_through": 3}
            (registry / "lobby.capture.metrics.json").write_text(json.dumps(capture))
            (registry / "lobby.archive.metrics.json").write_text(json.dumps(archive))
            runner = FakeDocker({CAPTURE: row(CAPTURE, "capture", running=True),
                                 ARCHIVE: row(ARCHIVE, "archive", running=True)})
            result = activate.short_smoke(runner, l, ATTEMPT, staged(), 1000,
                clock=lambda: 1240, sleep=lambda _: self.fail("should not wait"))
            self.assertEqual(result["verdict"], "PASS")
            archive["processed_through"] = 2
            archive["producer"] = {"high_entry": 999}
            (registry / "lobby.archive.metrics.json").write_text(json.dumps(archive))
            self.assertFalse(activate._smoke_sample(runner, l, ATTEMPT, staged(), 1000, 1240))
            archive["processed_through"] = 3
            (registry / "lobby.archive.metrics.json").write_text(json.dumps(archive))
            capture["observed_at"] = 999  # Old Writer metrics cannot prove progress.
            (registry / "lobby.capture.metrics.json").write_text(json.dumps(capture))
            with self.assertRaisesRegex(V2Error, "SMOKE_METRICS_STALE"):
                activate.short_smoke(runner, l, ATTEMPT, staged(), 1000,
                    clock=lambda: 1240, sleep=lambda _: None)
            capture["observed_at"] = 1235
            capture["producer"]["gaps"] = 1
            (registry / "lobby.capture.metrics.json").write_text(json.dumps(capture))
            result = activate.short_smoke(runner, l, ATTEMPT, staged(), 1000,
                clock=lambda: 1240, sleep=lambda _: self.fail("should not wait"))
            self.assertEqual(result["verdict"], "PASS")

    def test_effective_command_rejects_extra_legacy_or_foreign_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            data = staged()
            switch = activate.switch_bytes(l, ATTEMPT, data)
            self.assertIn(b"ExecStartPre=\n", switch["capture"])
            self.assertIn(b"ExecStartPost=\n", switch["capture"])
            self.assertIn(b"ExecStopPost=\n", switch["capture"])
            self.assertIn(b"TimeoutStopSec=300s", switch["monitor"])
            self.assertIn(b"TimeoutStartSec=300s", switch["monitor"])
            self.assertIn(b"KillMode=process", switch["monitor"])
            for reset in (b"ExecStartPre=\n", b"ExecStartPost=\n",
                          b"ExecStop=\n", b"ExecStopPost=\n"):
                self.assertIn(reset, switch["monitor"])
            root = l.release(ATTEMPT)
            units = {}
            for role in ("capture", "archive"):
                cid = data["container_ids"][role]
                units[role] = {"Restart": "no", "DropInPaths": str(l.switch(role, ATTEMPT)),
                    "ExecStartPre": f"{{ argv[]=/usr/bin/python3 -I -B {root}/v2/writer_precheck.py {ATTEMPT} {role} {cid} ; }}",
                    "ExecStart": f"{{ argv[]=/usr/bin/docker start --attach {cid} ; }}",
                    "ExecStop": f"{{ argv[]=/usr/bin/docker stop --time 40 {cid} ; }}",
                    "ExecStartPost": "", "ExecStopPost": ""}
            units["monitor"] = {"Restart": "no", "DropInPaths": str(l.switch("monitor", ATTEMPT)),
                "KillMode": "process", "TimeoutStartUSec": "5min", "TimeoutStopUSec": "5min",
                "ExecStartPre": "", "ExecStartPost": "", "ExecStop": "",
                "ExecStopPost": "", "ExecStart": "{ argv[]=/usr/bin/python3 -I -B " + str(root) +
                f"/v2/monitor_runner.py --attempt {ATTEMPT} --capture-id {CAPTURE}" +
                f" --archive-id {ARCHIVE} --image {IMAGE} --release {RELEASE} ; }}"}
            with patch.object(activate, "unit_state", side_effect=lambda _, role: units[role]):
                activate.verify_effective(None, l, ATTEMPT, data, {r: [] for r in units})
                units["capture"]["ExecStart"] += " { argv[]=/usr/bin/true ; }"
                with self.assertRaisesRegex(V2Error, "EFFECTIVE_WRITER_COMMAND"):
                    activate.verify_effective(None, l, ATTEMPT, data, {r: [] for r in units})
                units["capture"]["ExecStart"] = f"{{ argv[]=/usr/bin/docker start --attach {CAPTURE} ; }}"
                units["capture"]["ExecStopPost"] = "{ argv[]=/usr/bin/true ; }"
                with self.assertRaisesRegex(V2Error, "EFFECTIVE_WRITER_COMMAND"):
                    activate.verify_effective(None, l, ATTEMPT, data, {r: [] for r in units})
                units["capture"]["ExecStopPost"] = ""
                units["monitor"]["DropInPaths"] += " /etc/systemd/system/technocore-capture-monitor.service.d/99-foreign.conf"
                with self.assertRaisesRegex(V2Error, "EFFECTIVE_DROPIN_PRECEDENCE"):
                    activate.verify_effective(None, l, ATTEMPT, data, {r: [] for r in units})
                units["monitor"]["DropInPaths"] = str(l.switch("monitor", ATTEMPT))
                units["monitor"]["KillMode"] = "control-group"
                with self.assertRaisesRegex(V2Error, "EFFECTIVE_MONITOR_TIMEOUT_OR_KILLMODE"):
                    activate.verify_effective(None, l, ATTEMPT, data, {r: [] for r in units})
                units["monitor"]["KillMode"] = "process"
                for key in ("ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost"):
                    units["monitor"][key] = "{ argv[]=/usr/bin/true ; }"
                    with self.assertRaisesRegex(V2Error, "EFFECTIVE_MONITOR_AUX_COMMAND"):
                        activate.verify_effective(None, l, ATTEMPT, data, {r: [] for r in units})
                    units["monitor"][key] = ""

    def test_candidate_notification_is_disabled_in_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            state = l.state(ATTEMPT)
            state.mkdir()
            (state / "staged.json").write_text(json.dumps(staged()))
            packet = SimpleNamespace(read_json=lambda p: {}, write_new=lambda *a: None,
                                     sync_dir=lambda *a: None, require=lambda *a: None)
            monitor = SimpleNamespace()
            monitor.tick = lambda: (monitor.notification_event({"failures": []}),
                                    {"failures": []})[1]
            runner = FakeDocker()
            with patch.object(monitor_runner, "load_candidate", return_value=(packet, monitor)):
                code = monitor_runner.run_bound(attempt=ATTEMPT, capture_id=CAPTURE,
                    archive_id=ARCHIVE, image=IMAGE, release=RELEASE, runner=runner, layout=l)
            self.assertEqual(code, 0)
            self.assertFalse((l.shared_root / "control/notification").exists())
            self.assertFalse(any(x[0] in ("curl", "wget") for x in runner.calls))

    def test_host_fd_inventory_rejects_unexpected_capture_writer_without_argv(self):
        from v2 import preflight
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "shared"
            (shared / "spool/data").mkdir(parents=True)
            target = shared / "spool/data/active"
            target.write_text("dummy")
            process = root / "proc/123"
            (process / "fd").mkdir(parents=True)
            (process / "fdinfo").mkdir()
            (process / "fd/3").symlink_to(target)
            (process / "fdinfo/3").write_text("flags: 0100002\n")
            with self.assertRaisesRegex(V2Error, "FOREIGN_CAPTURE_FD_WRITER"):
                preflight.process_writer_check(shared, root / "proc")

    def test_probe_ignores_unrelated_optional_inspect_fields(self):
        probe = {"Id": CAPTURE, "Image": IMAGE, "Mounts": [],
                 "Config": {"User": "65532:65532", "Entrypoint": ["python3"],
                            "Cmd": ["-I", "-B", "-m", "technocore_full_capture", "--help"]},
                 "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True,
                                "Privileged": False, "CapDrop": ["ALL"],
                                "SecurityOpt": ["no-new-privileges"],
                                "RestartPolicy": {"Name": "no"}},
                 "State": {"Running": False, "Status": "created"}}
        stage.verify_probe(probe, cid=CAPTURE, image=IMAGE)

class ReviewArtifacts(unittest.TestCase):
    def test_stage_image_can_read_private_staged_source_as_runtime_user(self):
        image = "deploy/production-capture-upgrade-v2/Containerfile.full-capture"
        candidate = (ROOT / "deploy/Containerfile.full-capture").read_text()
        runtime = (ROOT / image).read_text()
        self.assertEqual(runtime, candidate.replace("COPY ", "COPY --chown=65532:65532 "))
        self.assertEqual(runtime.count("COPY --chown=65532:65532 "), 2)
        self.assertIn("USER 65532:65532\n", runtime)
        old_ignore = (ROOT / "deploy/Containerfile.full-capture.dockerignore").read_text()
        new_ignore = (ROOT / (image + ".dockerignore")).read_text()
        self.assertEqual(new_ignore, old_ignore.replace(
            "!deploy/Containerfile.full-capture\n",
            "!deploy/production-capture-upgrade-v2/\n"
            "!deploy/production-capture-upgrade-v2/Containerfile.full-capture\n").replace(
            "!deploy/Containerfile.full-capture.dockerignore\n",
            "!deploy/production-capture-upgrade-v2/Containerfile.full-capture.dockerignore\n"))
        with self.assertRaisesRegex(V2Error, "RELEASE_FILE_HASH"):
            artifact.verify_source(ROOT)
        self.skipTest("historical V2 release bytes are unavailable in this evolved checkout")
        with tempfile.TemporaryDirectory() as tmp:
            release = artifact.copy_reviewed(ROOT, Path(tmp) / "release",
                                             artifact.verify_source(ROOT))
            source = release / "source"
            for path in ("src/technocore_full_capture/__init__.py",
                         "src/technocore_full_capture/__main__.py",
                         "src/technocore_observer/protocol.py"):
                target = source / path
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                self.assertEqual(target.stat().st_uid, os.geteuid())
                self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
                self.assertEqual(target.parent.stat().st_uid, os.geteuid())
                # COPY keeps these private modes; --chown makes the image's
                # fixed runtime UID the owner that can traverse and read them.
                self.assertTrue(target.stat().st_mode & stat.S_IRUSR)
                self.assertTrue(target.parent.stat().st_mode & stat.S_IXUSR)

    def test_staged_code_drift_is_rejected_before_activation(self):
        from v2.common import sha256
        with self.assertRaisesRegex(V2Error, "RELEASE_FILE_HASH"):
            artifact.verify_source(ROOT)
        self.skipTest("historical V2 release bytes are unavailable in this evolved checkout")
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            manifest = artifact.verify_source(ROOT)
            release = artifact.copy_reviewed(ROOT, l.release(ATTEMPT), manifest)
            digest = sha256((release / "release-manifest.json").read_bytes())
            artifact.verify_staged_copy(release, digest)
            helper = release / "v2/monitor_runner.py"
            helper.write_bytes(helper.read_bytes() + b"\n# tampered\n")
            with self.assertRaisesRegex(V2Error, "STAGED_FILE_CHANGED"):
                artifact.verify_staged_copy(release, digest)

    def test_policy_duplicate_keys_fail_before_candidate_validation(self):
        from v2 import preflight
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.json"
            policy.write_text('{"version":1,"version":2}')
            with self.assertRaisesRegex(V2Error, "POLICY_JSON"):
                preflight.policy_check(policy, ROOT)

    def test_rollback_restores_only_prior_monitor_timer_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            path = l.switch("monitor", ATTEMPT)
            path.parent.mkdir(parents=True)
            path.write_text("owned V2 switch")
            command = "{ path=/bin/monitor ; argv[]=/bin/monitor --safe ; pid=11 ; start_time=one ; status=0/0 }"
            baseline = {"ExecStartPre": "", "ExecStart": command, "ExecStop": "",
                        "ExecStartPost": "", "ExecStopPost": "",
                        "Restart": "no", "DropInPaths": "", "ActiveState": "inactive",
                        "KillMode": "control-group", "TimeoutStartUSec": "3min",
                        "TimeoutStopUSec": "1min 30s"}
            fingerprints = {key: exec_fingerprint(baseline[key]) for key in
                            ("ExecStartPre", "ExecStart", "ExecStartPost", "ExecStop",
                             "ExecStopPost")}
            fingerprints["DropInPaths"] = sha256(b"")
            begin = {"switch_sha256": {"monitor": sha256(path.read_bytes())},
                     "baseline_units": {"monitor": {**fingerprints, "Restart": "no",
                         "DropInPathsList": [],
                         "KillMode": "control-group", "TimeoutStartUSec": "3min",
                         "TimeoutStopUSec": "1min 30s"}}, "baseline_timer": "active"}
            runner = FakeDocker()
            observed = {**baseline, "ExecStart": command.replace(
                "pid=11 ; start_time=one ; status=0/0",
                "pid=22 ; start_time=two ; status=1/FAILURE")}
            with patch.object(activate, "unit_state", return_value=observed), \
                    patch.object(activate, "timer_state", side_effect=["inactive", "active"]):
                outcome = activate.rollback_switch(runner, l, ATTEMPT, begin,
                    stop_results={"capture": "STOPPED", "archive": "ALREADY_STOPPED"})
            self.assertEqual(outcome, "ROLLED_BACK_STOPPED_BASELINE")
            self.assertFalse(path.exists())
            self.assertEqual([x for x in runner.calls if x[:2] == ["systemctl", "start"]],
                             [["systemctl", "start", "technocore-capture-monitor.timer"]])
            self.assertLess(runner.calls.index(["systemctl", "stop", "technocore-capture-monitor.timer"]),
                            runner.calls.index(["systemctl", "daemon-reload"]))
            observed["ExecStart"] = observed["ExecStart"].replace("--safe", "--changed")
            with patch.object(activate, "unit_state", return_value=observed), \
                    patch.object(activate, "timer_state", return_value="inactive"):
                self.assertEqual(activate.rollback_switch(runner, l, ATTEMPT, begin,
                    stop_results={"capture": "STOPPED", "archive": "ALREADY_STOPPED"}),
                    "RECONCILIATION_REQUIRED")

class StartPrecheck(unittest.TestCase):
    def test_foreign_running_writer_blocks_exact_start_without_mutation(self):
        from v2 import writer_precheck
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            l.state(ATTEMPT).mkdir()
            (l.state(ATTEMPT) / "staged.json").write_text(json.dumps(staged()))
            foreign = row(FOREIGN, "capture", running=True, attempt="20260923-v2-02")
            foreign["Mounts"] = [{"Source": str(l.shared_root / "spool/data"), "RW": True}]
            class Inventory(FakeDocker):
                def run(self, argv, *, timeout=60):
                    if argv[:3] == ["docker", "ps", "-q"]:
                        self.calls.append(list(argv))
                        return result(stdout=FOREIGN + "\n")
                    return super().run(argv, timeout=timeout)
            runner = Inventory({FOREIGN: foreign, CAPTURE: row(CAPTURE, "capture")})
            with patch.object(writer_precheck, "require_host"), \
                    patch.object(writer_precheck, "policy_check", return_value="f" * 64), \
                    patch.object(writer_precheck, "old_writer_check", return_value={}):
                with self.assertRaisesRegex(V2Error, "FOREIGN_CAPTURE_WRITER_RUNNING"):
                    writer_precheck.check(runner, l, ATTEMPT, "capture", CAPTURE)
            self.assertFalse(any(x[:2] in (["docker", "start"], ["docker", "stop"],
                                             ["systemctl", "start"]) for x in runner.calls))

class HappyPathWithFakes(unittest.TestCase):
    def test_stage_creates_only_stopped_attempt_containers_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            source = Path(tmp) / "checkout"
            manifest = source / "deploy/production-capture-upgrade-v2/release-manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}")
            probe_id = "f" * 64
            class StageRunner(FakeDocker):
                def run(self, argv, *, timeout=60):
                    self.calls.append(list(argv))
                    if argv[:2] == ["docker", "build"]:
                        return result(stdout=IMAGE + "\n")
                    if argv[:2] == ["docker", "create"]:
                        name = argv[argv.index("--name") + 1]
                        cid = (CAPTURE if "-capture-" in name else
                               ARCHIVE if "-archive-" in name else probe_id)
                        self.containers[cid] = row(cid, "capture")
                        return result(stdout=cid + "\n")
                    if argv[:2] == ["docker", "start"]:
                        self.containers[probe_id]["State"].update(Status="exited", ExitCode=0)
                        return result()
                    if argv[:3] == ["docker", "inspect", "--type=container"]:
                        return result(stdout=json.dumps([self.containers[argv[-1]]]))
                    return result()
            runner = StageRunner()
            fake_packet = SimpleNamespace(profile=lambda role, net: ["--network", net],
                                          prod_mounts=lambda role: ["--mount", role])
            baseline = {"policy_sha256": "f" * 64}
            with patch.object(stage, "verify_source", return_value={}), \
                    patch.object(stage, "verify_checkout"), \
                    patch.object(stage, "snapshot", return_value=baseline), \
                    patch.object(stage, "copy_reviewed", return_value=l.release(ATTEMPT)), \
                    patch.object(stage, "verify_probe"), \
                    patch.object(stage, "verify_writer"), \
                    patch.object(stage, "network_check", return_value={"network": "bridge"}), \
                    patch.object(stage, "policy_check", return_value="f" * 64), \
                    patch.object(stage, "candidate_packet", return_value=fake_packet), \
                    patch.object(stage, "_baseline_metrics", return_value={"gaps": 0,
                        "messages": 2, "archive_processed_through": 2}):
                staged_row = stage.stage(runner, l, source_root=source, attempt=ATTEMPT,
                                         release=RELEASE, human_approved=True)
            self.assertEqual(staged_row["container_ids"],
                             {"capture": CAPTURE, "archive": ARCHIVE})
            self.assertEqual(read_json(l.state(ATTEMPT) / "staged.json")["image_id"], IMAGE)
            self.assertFalse((l.state(ATTEMPT) / "terminal.json").exists())
            builds = [x for x in runner.calls if x[:2] == ["docker", "build"]]
            self.assertEqual(len(builds), 1)
            self.assertEqual(builds[0][builds[0].index("-f") + 1], str(
                l.release(ATTEMPT) / "source/deploy/production-capture-upgrade-v2/Containerfile.full-capture"))
            self.assertEqual([x for x in runner.calls if x[:2] == ["docker", "start"]],
                             [["docker", "start", "--attach", probe_id]])
            self.assertFalse(any(x[0] == "systemctl" for x in runner.calls))
            self.assertFalse(any(l.switch(role, ATTEMPT).exists()
                                 for role in ("capture", "archive", "monitor")))

    def test_activation_accepts_only_after_bounded_smoke_without_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            state = l.state(ATTEMPT)
            state.mkdir()
            (state / "staged.json").write_text(json.dumps(staged()))
            runner = FakeDocker({CAPTURE: row(CAPTURE, "capture"),
                                 ARCHIVE: row(ARCHIVE, "archive")})
            def run_with_archive(argv, *, timeout=60):
                if argv[:3] == ["systemctl", "start", "technocore-capture-archive.service"]:
                    runner.calls.append(list(argv))
                    runner.containers[ARCHIVE]["State"].update(Running=True, Pid=38,
                                                               Status="running")
                    return result()
                return FakeDocker.run(runner, argv, timeout=timeout)
            runner.run = run_with_archive
            with patch.object(activate, "fresh_before_activation"), \
                    patch.object(activate, "verify_staged"), \
                    patch.object(activate, "verify_effective"), \
                    patch.object(activate, "unit_state", return_value={
                        "ActiveState": "inactive", "DropInPaths": ""}), \
                    patch.object(activate, "timer_state", return_value="inactive"), \
                    patch.object(activate, "short_smoke", return_value={"verdict": "PASS",
                        "duration_seconds": 240, "min_samples": 3}):
                outcome = activate.activate(runner, l, attempt=ATTEMPT,
                                            human_approved=True, clock=lambda: 1000)
            self.assertEqual(outcome, "ACCEPTED")
            self.assertEqual(read_json(state / "terminal.json")["outcome"], "ACCEPTED")
            self.assertFalse(any(x[:2] == ["docker", "stop"] for x in runner.calls))
            self.assertTrue(all(l.switch(role, ATTEMPT).exists()
                                for role in ("capture", "archive", "monitor")))

class IndependentReviewRegressions(unittest.TestCase):
    def test_docker_absence_requires_complete_exact_diagnostic(self):
        class Inspect:
            def __init__(self, message):
                self.message = message
            def run(self, argv, *, timeout=60):
                return result(1, stderr=self.message)
        self.assertIsNone(docker_inspect(Inspect("Error: No such container: " + CAPTURE), CAPTURE))
        for diagnostic in ("Error: No such object: " + CAPTURE + " and daemon unavailable",
                           "Error: No such container: " + FOREIGN,
                           "Cannot connect to the Docker daemon"):
            with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(
                    V2Error, "DOCKER_INSPECT_UNKNOWN"):
                docker_inspect(Inspect(diagnostic), CAPTURE)

    def test_writer_readiness_waits_for_exact_running_pid(self):
        waiting = row(ARCHIVE, "archive")
        running = row(ARCHIVE, "archive", running=True)
        states = [waiting, {**running, "State": {**running["State"], "Pid": 0}}, running]
        class Inspect:
            def run(self, argv, *, timeout=60):
                self_case.assertEqual(argv[-1], ARCHIVE)
                return result(stdout=json.dumps([states.pop(0)]))
        self_case = self
        elapsed = [0.0]
        samples = []
        activate.wait_writer_ready(Inspect(), staged(), "archive", samples,
            monotonic=lambda: elapsed[0], sleep=lambda seconds: elapsed.__setitem__(
                0, elapsed[0] + seconds))
        self.assertEqual([sample["pid"] for sample in samples], [0, 0, 37])
        self.assertEqual(elapsed[0], 2)

    def test_writer_readiness_timeout_and_terminal_state_keep_diagnostics(self):
        for terminal in (False, True):
            with self.subTest(terminal=terminal):
                state = row(ARCHIVE, "archive")
                if terminal:
                    state["State"].update(Status="exited", ExitCode=1)
                runner = FakeDocker({ARCHIVE: state})
                elapsed = [0.0]
                samples = []
                with self.assertRaisesRegex(V2Error, "WRITER_NOT_HEALTHY"):
                    activate.wait_writer_ready(runner, staged(), "archive", samples,
                        monotonic=lambda: elapsed[0], sleep=lambda seconds: elapsed.__setitem__(
                            0, elapsed[0] + seconds))
                last_state = samples[-1] if terminal else samples[-2]
                self.assertEqual(last_state["status"], "exited" if terminal else "created")
                self.assertEqual(last_state["exit_code"], 1 if terminal else None)
                self.assertEqual(elapsed[0], 0 if terminal else activate.WRITER_READY_SECONDS)
                if not terminal:
                    self.assertTrue(samples[-1]["deadline_exceeded"])
                self.assertLessEqual(len(samples), activate.WRITER_READY_SECONDS + 1)

    def test_writer_readiness_rejects_oom_identity_mismatch_and_missing_container(self):
        for condition, expected in (("oom", "WRITER_NOT_HEALTHY"),
                                    ("identity_mismatch", "RUNNING_TARGET_MISMATCH"),
                                    ("missing_container", "RUNNING_TARGET_MISMATCH")):
            with self.subTest(condition=condition):
                writer = row(ARCHIVE, "archive", running=True)
                if condition == "oom":
                    writer["State"]["OOMKilled"] = True
                elif condition == "identity_mismatch":
                    writer["Image"] = "sha256:" + "0" * 64
                containers = {} if condition == "missing_container" else {ARCHIVE: writer}
                runner = FakeDocker(containers)
                samples = []
                with self.assertRaisesRegex(V2Error, expected):
                    activate.wait_writer_ready(runner, staged(), "archive", samples,
                                               monotonic=lambda: 0, sleep=lambda _: self.fail(
                                                   "rejected readiness must not retry"))
                self.assertEqual(len(samples), 1)
                if condition == "oom":
                    self.assertTrue(samples[0]["oom_killed"])
                else:
                    self.assertEqual(samples[0]["target"], "MISMATCH_OR_ABSENT")
                self.assertEqual(runner.calls,
                                 [["docker", "inspect", "--type=container", ARCHIVE]])

    def test_activation_timeout_persists_archive_inspect_before_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            state = l.state(ATTEMPT)
            state.mkdir()
            (state / "staged.json").write_text(json.dumps(staged()))
            runner = FakeDocker({CAPTURE: row(CAPTURE, "capture"),
                                 ARCHIVE: row(ARCHIVE, "archive")})
            baseline = {"ExecStartPre": old_exec("old-pre"), "ExecStart": old_exec("old-start"),
                        "ExecStartPost": "", "ExecStop": old_exec("old-stop"), "ExecStopPost": "",
                        "Restart": "no", "DropInPaths": "", "ActiveState": "inactive",
                        "KillMode": "control-group", "TimeoutStartUSec": "3min",
                        "TimeoutStopUSec": "1min 30s"}
            elapsed = [0.0]
            with patch.object(activate, "fresh_before_activation"), \
                    patch.object(activate, "verify_staged"), \
                    patch.object(activate, "verify_effective"), \
                    patch.object(activate, "unit_state", return_value=baseline), \
                    patch.object(activate, "timer_state", return_value="inactive"):
                with self.assertRaisesRegex(V2Error, "WRITER_NOT_HEALTHY"):
                    activate.activate(runner, l, attempt=ATTEMPT, human_approved=True,
                        clock=lambda: 1000, monotonic=lambda: elapsed[0],
                        sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
            terminal = read_json(state / "terminal.json")
            self.assertEqual(terminal["primary_failure"], "WRITER_NOT_HEALTHY")
            self.assertEqual(terminal["writer_readiness"]["archive"][-2]["status"], "created")
            self.assertTrue(terminal["writer_readiness"]["archive"][-1]["deadline_exceeded"])
            self.assertEqual(terminal["writer_readiness"]["archive"][-1]["elapsed_seconds"], 15)
            self.assertEqual(terminal["stop_results"]["capture"], "STOPPED")
            self.assertFalse(any(call[:3] == ["systemctl", "start",
                    "technocore-capture-monitor.service"] for call in runner.calls))

    def test_failed_writer_unit_resets_only_after_stops_and_no_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            path = l.switch("capture", ATTEMPT)
            path.parent.mkdir(parents=True)
            path.write_text("owned")
            begin = {"switch_sha256": {"capture": sha256(path.read_bytes())},
                     "baseline_units": {}, "baseline_timer": "inactive"}
            runner = FakeDocker()
            failed = {"capture": True, "archive": False}
            original_run = runner.run
            def run(argv, *, timeout=60):
                if argv[:2] == ["systemctl", "reset-failed"]:
                    self.assertTrue(path.exists())
                    failed["capture"] = False
                return original_run(argv, timeout=timeout)
            runner.run = run
            def unit(_runner, role):
                return {"ActiveState": "failed" if failed.get(role) else "inactive"}
            with patch.object(activate, "unit_state", side_effect=unit), \
                    patch.object(activate, "timer_state", return_value="inactive"):
                outcome = activate.rollback_switch(runner, l, ATTEMPT, begin,
                    stop_results={"capture": "STOPPED", "archive": "ALREADY_STOPPED"})
            self.assertEqual(outcome, "ROLLED_BACK_STOPPED_BASELINE")
            self.assertFalse(path.exists())
            self.assertEqual([call for call in runner.calls if call[:2] == ["systemctl", "reset-failed"]],
                             [["systemctl", "reset-failed", "technocore-capture-capture.service"]])

    def test_pending_job_blocks_failed_unit_reset_and_switch_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            path = l.switch("capture", ATTEMPT)
            path.parent.mkdir(parents=True)
            path.write_text("owned")
            begin = {"switch_sha256": {"capture": sha256(path.read_bytes())},
                     "baseline_units": {}, "baseline_timer": "inactive"}
            runner = FakeDocker()
            original_run = runner.run
            def run(argv, *, timeout=60):
                if argv[:2] == ["systemctl", "list-jobs"]:
                    runner.calls.append(list(argv))
                    return result(stdout="42 technocore-capture-capture.service start waiting\n")
                return original_run(argv, timeout=timeout)
            runner.run = run
            def unit(_runner, role):
                return {"ActiveState": "failed" if role == "capture" else "inactive"}
            with patch.object(activate, "unit_state", side_effect=unit), \
                    patch.object(activate, "timer_state", return_value="inactive"):
                outcome = activate.rollback_switch(runner, l, ATTEMPT, begin,
                    stop_results={"capture": "STOPPED", "archive": "ALREADY_STOPPED"})
            self.assertEqual(outcome, "RECONCILIATION_REQUIRED")
            self.assertTrue(path.exists())
            self.assertFalse(any(call[:2] == ["systemctl", "reset-failed"] for call in runner.calls))
            self.assertTrue(any(call[:2] == ["systemctl", "list-jobs"] for call in runner.calls))

    def test_failed_unit_reset_failure_preserves_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            path = l.switch("capture", ATTEMPT)
            path.parent.mkdir(parents=True)
            path.write_text("owned")
            begin = {"switch_sha256": {"capture": sha256(path.read_bytes())},
                     "baseline_units": {}, "baseline_timer": "inactive"}
            runner = FakeDocker()
            original_run = runner.run
            def run(argv, *, timeout=60):
                if argv[:2] == ["systemctl", "reset-failed"]:
                    runner.calls.append(list(argv))
                    return result(1)
                return original_run(argv, timeout=timeout)
            runner.run = run
            def unit(_runner, role):
                return {"ActiveState": "failed" if role == "capture" else "inactive"}
            with patch.object(activate, "unit_state", side_effect=unit), \
                    patch.object(activate, "timer_state", return_value="inactive"):
                outcome = activate.rollback_switch(runner, l, ATTEMPT, begin,
                    stop_results={"capture": "STOPPED", "archive": "ALREADY_STOPPED"})
            self.assertEqual(outcome, "RECONCILIATION_REQUIRED")
            self.assertTrue(path.exists())
            self.assertEqual([call for call in runner.calls if call[:2] == ["systemctl", "reset-failed"]],
                             [["systemctl", "reset-failed", "technocore-capture-capture.service"]])

    def test_policy_window_and_hash_are_checked_from_same_bytes(self):
        from technocore_full_capture import production
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            with patch.object(production, "validate_config"):
                for start, end, now, allowed in ((100, 200, 150, True),
                                                 (200, 300, 150, False),
                                                 (100, 150, 150, False),
                                                 (100, None, 150, True)):
                    data = json.dumps({"start_at": start, "end_at": end}).encode()
                    path.write_bytes(data)
                    if allowed:
                        self.assertEqual(preflight.policy_check(path, ROOT, now=now), sha256(data))
                    else:
                        with self.assertRaisesRegex(V2Error, "POLICY_WINDOW_INACTIVE"):
                            preflight.policy_check(path, ROOT, now=now)

    def test_writer_precheck_rejects_policy_hash_drift_before_docker_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            l.state(ATTEMPT).mkdir()
            (l.state(ATTEMPT) / "staged.json").write_text(json.dumps(staged()))
            runner = FakeDocker({CAPTURE: row(CAPTURE, "capture")})
            with patch.object(writer_precheck, "require_host"), \
                    patch.object(writer_precheck, "policy_check", return_value="0" * 64):
                with self.assertRaisesRegex(V2Error, "PRECHECK_POLICY_CHANGED"):
                    writer_precheck.check(runner, l, ATTEMPT, "capture", CAPTURE)
            self.assertEqual(runner.calls, [])

    def test_rollback_never_removes_switch_while_monitor_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            path = l.switch("monitor", ATTEMPT)
            path.parent.mkdir(parents=True)
            path.write_text("owned")
            begin = {"switch_sha256": {"monitor": sha256(path.read_bytes())},
                     "baseline_units": {}, "baseline_timer": "active"}
            runner = FakeDocker()
            with patch.object(activate, "timer_state", return_value="inactive"), \
                    patch.object(activate, "unit_state", return_value={"ActiveState": "active"}):
                outcome = activate.rollback_switch(runner, l, ATTEMPT, begin,
                    stop_results={"capture": "STOPPED", "archive": "ALREADY_STOPPED"})
            self.assertEqual(outcome, "RECONCILIATION_REQUIRED")
            self.assertTrue(path.exists())
            self.assertFalse(any(call[1:] == ["daemon-reload"] for call in runner.calls))

    def test_activation_rechecks_inactive_monitor_after_timer_quiesce(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            state = l.state(ATTEMPT)
            state.mkdir()
            (state / "staged.json").write_text(json.dumps(staged()))
            runner = FakeDocker({CAPTURE: row(CAPTURE, "capture"),
                                 ARCHIVE: row(ARCHIVE, "archive")})
            count = 0
            baseline = {"ActiveState": "inactive", "DropInPaths": ""}
            def monitor_becomes_active(_runner, role):
                nonlocal count
                if role == "monitor":
                    count += 1
                    if count >= 3:
                        return {**baseline, "ActiveState": "active"}
                return baseline
            with patch.object(activate, "fresh_before_activation"), \
                    patch.object(activate, "verify_staged"), \
                    patch.object(activate, "unit_state", side_effect=monitor_becomes_active), \
                    patch.object(activate, "timer_state", return_value="inactive"):
                with self.assertRaisesRegex(V2Error, "MONITOR_SERVICE_NOT_INACTIVE"):
                    activate.activate(runner, l, attempt=ATTEMPT, human_approved=True)
            self.assertFalse(any(l.switch(role, ATTEMPT).exists()
                                 for role in ("capture", "archive", "monitor")))
            self.assertFalse(any(call[1:] == ["daemon-reload"] for call in runner.calls))

    def test_pre_rejects_dropins_that_follow_v2_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            units = {role: {"DropInPathsList": [
                str(l.switch(role, ATTEMPT).parent / (name + ".conf"))
                for name in ("95-prior", "97-gap", "98-policy")]} for role in
                ("capture", "archive", "monitor")}
            self.assertTrue(l.switch("monitor", ATTEMPT).name.startswith("99-zz-tc-v2-"))
            preflight.require_v2_dropin_last(l, ATTEMPT, units)
            units["monitor"]["DropInPathsList"].append(
                str(l.switch("monitor", ATTEMPT).parent / "99-zzz-foreign.conf"))
            with self.assertRaisesRegex(V2Error, "V2_DROPIN_PRECEDENCE"):
                preflight.require_v2_dropin_last(l, ATTEMPT, units)

    def _assert_activation_signal(self, signum, *, pending_job):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            state = l.state(ATTEMPT)
            state.mkdir()
            (state / "staged.json").write_text(json.dumps(staged()))
            runner = FakeDocker({CAPTURE: row(CAPTURE, "capture"),
                                 ARCHIVE: row(ARCHIVE, "archive")})
            baseline = {"ExecStartPre": old_exec("old-pre"), "ExecStart": old_exec("old-start"),
                        "ExecStartPost": "", "ExecStop": old_exec("old-stop"), "ExecStopPost": "",
                        "Restart": "no", "DropInPaths": "", "ActiveState": "inactive",
                        "KillMode": "control-group", "TimeoutStartUSec": "3min",
                        "TimeoutStopUSec": "1min 30s"}
            handlers, changes = {}, []
            actual_run = runner.run
            fired = False
            job_checks = 0
            def run_and_interrupt(argv, *, timeout=60):
                nonlocal fired, job_checks
                if argv[:2] == ["systemctl", "list-jobs"]:
                    job_checks += 1
                    runner.calls.append(list(argv))
                    pending = pending_job is True or (pending_job == "late" and job_checks >= 2)
                    return result(stdout=("42 technocore-capture-capture.service start waiting\n"
                                          if pending else ""))
                response = actual_run(argv, timeout=timeout)
                if argv[:3] == ["systemctl", "start", "technocore-capture-capture.service"] and not fired:
                    fired = True
                    handlers[signum](signum, None)
                return response
            def install(signum_, handler):
                changes.append((signum_, handler))
                handlers.setdefault(signum_, handler)
            runner.run = run_and_interrupt
            with patch.object(activate, "fresh_before_activation"), \
                    patch.object(activate, "verify_staged"), \
                    patch.object(activate, "unit_state", return_value=baseline), \
                    patch.object(activate, "timer_state", return_value="inactive"), \
                    patch.object(activate, "verify_effective"), \
                    patch.object(activate.signal, "signal", side_effect=install):
                with self.assertRaises(activate.ActivationSignal):
                    activate.activate(runner, l, attempt=ATTEMPT, human_approved=True,
                                      clock=lambda: 1000)
            terminal = read_json(state / "terminal.json")
            self.assertEqual(terminal["primary_failure"], "ACTIVATION_SIGNAL")
            self.assertEqual([x[-1] for x in runner.calls if x[:2] == ["docker", "stop"]],
                             [CAPTURE])
            self.assertLess(runner.calls.index(["systemctl", "stop",
                                                "technocore-capture-capture.service"]),
                            runner.calls.index(["docker", "stop", "--time", "40", CAPTURE]))
            self.assertEqual([x[0] for x in changes],
                             [signal.SIGTERM, signal.SIGHUP, signal.SIGTERM, signal.SIGHUP])
            if pending_job:
                self.assertEqual(terminal["outcome"], "RECONCILIATION_REQUIRED")
                self.assertEqual(terminal["final_observed_state"], "UNKNOWN_RECONCILE")
                self.assertTrue(l.switch("capture", ATTEMPT).exists())
            else:
                self.assertEqual(terminal["outcome"], "ACTIVATION_FAILED_ROLLED_BACK")
                self.assertEqual(terminal["final_observed_state"], "STOPPED_BASELINE")
                self.assertFalse(any(l.switch(role, ATTEMPT).exists()
                                     for role in ("capture", "archive", "monitor")))

    def test_sigterm_quiesces_units_before_exact_stop(self):
        self._assert_activation_signal(signal.SIGTERM, pending_job=False)

    def test_sighup_uses_same_exact_stop_and_rollback_path(self):
        self._assert_activation_signal(signal.SIGHUP, pending_job=False)

    def test_pending_writer_job_cannot_claim_stopped_baseline(self):
        self._assert_activation_signal(signal.SIGTERM, pending_job=True)

    def test_job_after_initial_quiesce_blocks_switch_removal(self):
        self._assert_activation_signal(signal.SIGTERM, pending_job="late")

    def test_active_writer_unit_blocks_rollback_even_when_docker_ids_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            path = l.switch("capture", ATTEMPT)
            path.parent.mkdir(parents=True)
            path.write_text("owned")
            begin = {"switch_sha256": {"capture": sha256(path.read_bytes())},
                     "baseline_units": {}, "baseline_timer": "inactive"}
            runner = FakeDocker()
            def state(_runner, role):
                return {"ActiveState": "active" if role == "capture" else "inactive"}
            with patch.object(activate, "unit_state", side_effect=state), \
                    patch.object(activate, "timer_state", return_value="inactive"):
                outcome = activate.rollback_switch(runner, l, ATTEMPT, begin,
                    stop_results={"capture": "STOPPED", "archive": "ALREADY_STOPPED"})
            self.assertEqual(outcome, "RECONCILIATION_REQUIRED")
            self.assertTrue(path.exists())

    def test_explicit_rollback_reconciles_failed_writers_after_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = layout(tmp)
            state = l.state(ATTEMPT)
            state.mkdir()
            data = staged()
            (state / "staged.json").write_text(json.dumps(data))
            for role, contents in activate.switch_bytes(l, ATTEMPT, data).items():
                path = l.switch(role, ATTEMPT)
                path.parent.mkdir(parents=True)
                path.write_bytes(contents)
            begin = {"switch_sha256": {role: sha256(l.switch(role, ATTEMPT).read_bytes())
                                       for role in ("capture", "archive", "monitor")},
                     "baseline_units": data["baseline"]["units"],
                     "baseline_dropins": {role: [] for role in ("capture", "archive", "monitor")},
                     "baseline_timer": "inactive"}
            (state / "activation-begin.json").write_text(json.dumps(begin))
            runner = FakeDocker({CAPTURE: row(CAPTURE, "capture", running=True),
                                 ARCHIVE: row(ARCHIVE, "archive", running=True)})
            baseline = {"ExecStartPre": old_exec("old-pre"), "ExecStart": old_exec("old-start"),
                        "ExecStartPost": "", "ExecStop": old_exec("old-stop"), "ExecStopPost": "",
                        "Restart": "no", "DropInPaths": "", "ActiveState": "inactive",
                        "KillMode": "control-group", "TimeoutStartUSec": "3min",
                        "TimeoutStopUSec": "1min 30s"}
            failed = {"capture": False, "archive": False}
            original_run = runner.run
            def run(argv, *, timeout=60):
                for role in failed:
                    unit = "technocore-capture-" + role + ".service"
                    if argv == ["systemctl", "stop", unit]:
                        failed[role] = True
                    elif argv == ["systemctl", "reset-failed", unit]:
                        self.assertTrue(l.switch(role, ATTEMPT).exists())
                        failed[role] = False
                return original_run(argv, timeout=timeout)
            runner.run = run
            def unit(_runner, role):
                return {**baseline, "ActiveState": "failed" if failed.get(role) else "inactive"}
            with patch.object(preflight, "require_host"), \
                    patch.object(activate, "verify_effective"), \
                    patch.object(activate, "unit_state", side_effect=unit), \
                    patch.object(activate, "timer_state", return_value="inactive"):
                outcome = activate.explicit_rollback(runner, l, attempt=ATTEMPT,
                                                     human_approved=True)
            self.assertEqual(outcome, "ROLLED_BACK_STOPPED_BASELINE")
            self.assertEqual(failed, {"capture": False, "archive": False})
            self.assertFalse(any(l.switch(role, ATTEMPT).exists()
                                 for role in ("capture", "archive", "monitor")))
            self.assertEqual([call for call in runner.calls if call[:2] == ["systemctl", "reset-failed"]],
                             [["systemctl", "reset-failed", "technocore-capture-capture.service"],
                              ["systemctl", "reset-failed", "technocore-capture-archive.service"]])
            self.assertLess(runner.calls.index(["systemctl", "stop",
                                                "technocore-capture-archive.service"]),
                            runner.calls.index(["docker", "stop", "--time", "40", CAPTURE]))
            self.assertLess(runner.calls.index(["docker", "stop", "--time", "40", ARCHIVE]),
                            runner.calls.index(["systemctl", "reset-failed",
                                                "technocore-capture-capture.service"]))
            self.assertEqual(read_json(state / "rollback.json")["outcome"], outcome)
