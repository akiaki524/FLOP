"""Focused Candidate Monitor behavior that the V2 adapter must preserve."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/production-capture-human"))
import monitor


def healthy(now=1000):
    return {"observed_at": now,
            "observers": {"containers": {"observer": {
                "running": True, "oom_killed": False, "freshness": {"verdict": "PASS"}}},
                "timer": {"ActiveState": "active"},
                "service": {"Result": "success", "ExecMainStatus": "0"},
                "health": {"rules": {"health": "OK", "lag_streak": 0, "action": "none"},
                           "results": {"health": "OK", "lag_streak": 0, "action": "none"}}},
            "metrics": {role: {"service_status": "RUNNING", "observed_at": now,
                               "planning_runway_seconds": 8000,
                               "last_successful_response": now,
                               "producer": {"gaps": 0}}
                        for role in ("capture", "archive")},
            "gap_growth": {"delta": 0, "streak": 0, "streak_gaps": 0}}


class CandidateMonitorEvaluation(unittest.TestCase):
    def test_gap_growth_and_material_stop_boundary(self):
        with patch.object(monitor, "GAP_BASELINE", 2):
            first = monitor.gap_growth(6, {"samples": 0}, 1000)
        self.assertEqual((first["delta"], first["streak"]), (4, 1))
        second = monitor.gap_growth(12, {"samples": 1, "observed_at": 1000,
                                         "metrics": {"capture": {"producer": {"gaps": 6}}},
                                         "gap_growth": first}, 1100)
        self.assertEqual((second["streak"], second["streak_gaps"]), (2, 10))
        sample = healthy(1100)
        sample["gap_growth"] = second
        failures, findings = monitor.evaluate(sample, 800)
        self.assertIn("MATERIAL_CAPTURE_GAPS", failures)
        self.assertIn("CAPTURE_GAP_GROWTH:+6", findings)
        sample["gap_growth"] = {"delta": 9, "streak": 1, "streak_gaps": 9}
        self.assertNotIn("MATERIAL_CAPTURE_GAPS", monitor.evaluate(sample, 800)[0])

    def test_freshness_and_observer_findings_are_distinct(self):
        sample = healthy(1000)
        sample["observers"]["containers"]["observer"]["freshness"] = {"verdict": "FAIL"}
        self.assertEqual(monitor.evaluate(sample, 900)[0], [])  # 180-second grace
        failures, findings = monitor.evaluate(sample, 800)
        self.assertEqual(failures, [])
        self.assertIn("OBSERVER_HEALTH_OR_FRESHNESS:observer", findings)
        sample["metrics"]["capture"]["observed_at"] = 870
        sample["metrics"]["capture"]["last_successful_response"] = 800
        failures, _ = monitor.evaluate(sample, 800)
        self.assertIn("CAPTURE_SERVICE_OR_METRICS_STALE:capture", failures)
        self.assertIn("CAPTURE_RESPONSE_STALE", failures)

    def test_checkpoint_is_created_once_and_preserves_observer_downgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            report = {**healthy(1000), "samples": 1, "failures": [],
                      "findings": ["OBSERVER_SUPERVISOR_FAILED"],
                      "stop_required": False, "stop_applied": False,
                      "verdict": "PASS WITH FINDINGS"}
            with patch.object(monitor, "DIRECTORY", path):
                monitor.save_evaluation(report, {"start_at": 800, "observation_checkpoint_at": 1000})
                checkpoint = path / "checkpoint-24h.json"
                self.assertTrue(checkpoint.is_file())
                original = checkpoint.read_bytes()
                monitor.save_evaluation({**report, "samples": 2},
                                        {"start_at": 800, "observation_checkpoint_at": 1000})
                self.assertEqual(checkpoint.read_bytes(), original)

    def test_previous_observer_failure_downgrades_and_capture_failure_stops(self):
        previous = {"samples": 1, "failures": ["OBSERVER_SUPERVISOR_FAILED"],
                    "findings": [], "stop_applied": False, "observed_at": 999,
                    "metrics": {"capture": {"producer": {"gaps": 0}}}}
        recorded = []
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "latest.json").write_text("{}")
            with patch.object(monitor, "DIRECTORY", Path(tmp)), \
                patch.object(monitor.packet, "require_host"), \
                patch.object(monitor.packet, "mounted_check"), \
                patch.object(monitor.packet, "receipt", return_value={
                    "start_at": 800, "observation_checkpoint_at": 999999}), \
                patch.object(monitor.packet, "read_json", return_value=previous), \
                patch.object(monitor, "observers", return_value=healthy()["observers"]), \
                patch.object(monitor, "metrics", side_effect=lambda role: healthy()["metrics"][role]), \
                patch.object(monitor, "terminal_notification_count", return_value=0), \
                patch.object(monitor, "save_evaluation", side_effect=lambda *a: recorded.append("evidence")), \
                patch.object(monitor.packet, "stop_roles", side_effect=lambda *a: recorded.append("stop")), \
                patch.object(monitor, "replace_json"), \
                patch.object(monitor, "notification_event", return_value={"status": "ignored"}), \
                patch.object(monitor.time, "time", return_value=1000), \
                contextlib.redirect_stdout(io.StringIO()):
                report = monitor.tick()
        self.assertFalse(report["stop_applied"])
        self.assertIn("OBSERVER_SUPERVISOR_FAILED", report["findings"])
        self.assertEqual(recorded, ["evidence"])
        previous["failures"] = ["CAPTURE_RESPONSE_STALE"]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "latest.json").write_text("{}")
            with patch.object(monitor, "DIRECTORY", Path(tmp)), \
                patch.object(monitor.packet, "require_host"), \
                patch.object(monitor.packet, "mounted_check"), \
                patch.object(monitor.packet, "receipt", return_value={
                    "start_at": 800, "observation_checkpoint_at": 999999}), \
                patch.object(monitor.packet, "read_json", return_value=previous), \
                patch.object(monitor, "observers", return_value=healthy()["observers"]), \
                patch.object(monitor, "metrics", side_effect=lambda role: healthy()["metrics"][role]), \
                patch.object(monitor, "terminal_notification_count", return_value=0), \
                patch.object(monitor, "save_evaluation", side_effect=lambda *a: recorded.append("evidence2")), \
                patch.object(monitor.packet, "stop_roles", side_effect=lambda *a: recorded.append("stop2")), \
                patch.object(monitor, "replace_json"), \
                patch.object(monitor, "notification_event", return_value={"status": "ignored"}), \
                patch.object(monitor.time, "time", return_value=1000), \
                contextlib.redirect_stdout(io.StringIO()):
                report = monitor.tick()
        self.assertTrue(report["stop_applied"])
        self.assertEqual(recorded[-2:], ["evidence2", "stop2"])
