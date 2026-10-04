"""Regressions for the observed 0.12.1 shapes; existing P0 tests stay unchanged."""

import contextlib
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from technocore_observer.observer import Observer
from technocore_observer.protocol import (
    ProtocolAnomaly, Reply, content_values, decode_reply, deployment_config,
    json_dump, validate_envelope,
)
from technocore_observer.storage import StateLock, Store, initialize

EVIDENCE = ROOT / "docs" / "smoke-results-20260906-085129-5ac4541a" / "live"


class ShapeTests(unittest.TestCase):
    def test_nested_settings_and_units_are_not_confused(self):
        config = deployment_config({"version": "0.12.1", "settings": {
            "stillborn_seconds": 43200, "rate_read": 600, "rate_write": 300,
            "max_wait": 10, "max_waiters_total": 64, "max_waiters_per_ip": 4,
            "static_cache_seconds": 300, "ephemeral_ttl_seconds": 900},
            "units": {"rate_read": "requests per minute per client IP"}})
        self.assertEqual(config["settings"]["reads_per_minute_per_ip"], 600)
        self.assertEqual(config["setting_sources"]["reads_per_minute_per_ip"], "settings.rate_read")
        self.assertEqual(config["settings"]["max_wait_seconds"], 10)
        self.assertIsNone(config["settings"]["long_poll_seconds"])
        self.assertEqual(set(config["unavailable_settings"]), {"room_ring_bytes", "retention_seconds", "long_poll_seconds"})
        self.assertEqual(config["validation_flags"], [])

    def test_invalid_settings_fail_or_remain_unavailable_without_defaults(self):
        with self.assertRaisesRegex(ProtocolAnomaly, "INVALID_CONFIG_SETTINGS"):
            deployment_config({"version": "0.12.1", "settings": []})
        config = deployment_config({"version": "0.12.1", "settings": {"retention_seconds": True},
                                    "retention_seconds": 604800})
        self.assertIsNone(config["settings"]["retention_seconds"])
        self.assertIn("INVALID_CONFIG_SETTING:retention_seconds", config["validation_flags"])

    def test_actual_content_shape_has_no_false_flags(self):
        record = {"seq": 1, "ts": "2026-09-05T23:00:00Z", "from": "did:public-test",
                  "text": "unchanged\n\u202e\x1b", "nonce": 123, "sig": "unverified"}
        values = content_values(record)
        self.assertEqual(json.loads(values["validation_flags"]), [])
        self.assertEqual(json.loads(values["raw_record_json"]), record)
        self.assertEqual(values["text_value"], record["text"])
        self.assertEqual(json.loads(values["ts_value"]), record["ts"])
        self.assertEqual(values["text_sha256"], hashlib.sha256(record["text"].encode()).hexdigest())

    def test_content_drift_still_preserves_every_record(self):
        for nonce in (True, "123", None, {}, []):
            with self.subTest(nonce=nonce):
                record = {"seq": 1, "ts": 123, "from": "public", "text": "preserve", "nonce": nonce}
                values = content_values(record)
                self.assertIn("INVALID_TS_TYPE", json.loads(values["validation_flags"]))
                self.assertIn("NON_INTEGER_NONCE", json.loads(values["validation_flags"]))
                self.assertEqual(json.loads(values["raw_record_json"]), record)

    def test_config_retention_is_read_from_settings_and_cleared_when_unavailable(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            directory = Path(root)
            obj = {"room": "fixture", "count": 1, "first_seq": 1, "last_seq": 1,
                   "generation": 1, "messages": [{"seq": 1}]}
            with StateLock(directory):
                initialize(directory, "fixture", obj, Reply(200, "application/json", json_dump(obj).encode()))
                with contextlib.closing(Store(directory, "fixture")) as store:
                    client = Mock()
                    observer = Observer(store, client)
                    client.config.return_value = Reply(200, "application/json", b'{"version":"0.12.1","settings":{"retention_seconds":120}}')
                    self.assertEqual(observer.check_config()["retention_seconds"], 120)
                    client.config.return_value = Reply(200, "application/json", b'{"version":"0.12.1","settings":{}}')
                    self.assertIsNone(observer.check_config()["retention_seconds"])


@unittest.skipUnless(EVIDENCE.is_dir(), "Local private smoke evidence is not distributed with this repository")
class SavedResponseTests(unittest.TestCase):
    def read_reply(self, number):
        report = json.loads((EVIDENCE / "report.json").read_text())
        item = report["requests"][number - 1]
        body = (EVIDENCE / f"response-{number}.body").read_bytes()
        self.assertEqual(hashlib.sha256(body).hexdigest(), item["body_sha256"])
        return Reply(item["http_status"], item["content_type"], body)

    def test_saved_config_values(self):
        config = deployment_config(decode_reply(self.read_reply(1)))
        self.assertEqual(config["version"], "0.12.1")
        expected = {"stillborn_seconds": 43200, "max_waiters_total": 64, "max_waiters_per_ip": 4,
                    "static_cache_seconds": 300, "reads_per_minute_per_ip": 600,
                    "writes_per_minute_per_ip": 300, "ephemeral_ttl_seconds": 900, "max_wait_seconds": 10}
        for field, value in expected.items():
            self.assertEqual(config["settings"][field], value)
        self.assertEqual(config["validation_flags"], [])

    def test_saved_200_records_round_trip_through_actual_observer(self):
        raw = self.read_reply(3)
        obj = validate_envelope(decode_reply(raw), "mb-047f3d88ef38")
        self.assertEqual(obj["count"], 200)
        # Explicit synthetic offline anchor at 525; never modifies live state or
        # claims this fixture is a tail response obtained from Technocore.
        seq = obj["first_seq"] - 1
        anchor = {"room": obj["room"], "count": 1, "first_seq": seq, "last_seq": seq,
                  "generation": obj["generation"], "messages": [{"seq": seq}]}
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            directory = Path(root)
            with StateLock(directory):
                initialize(directory, obj["room"], anchor, Reply(200, "application/json", json_dump(anchor).encode()))
                with contextlib.closing(Store(directory, obj["room"])) as store:
                    client = Mock()
                    client.poll.return_value = raw
                    self.assertTrue(Observer(store, client).poll_once()[0])
                    client.poll.assert_called_once_with(seq)
                    rows = store.conn.execute("SELECT * FROM messages ORDER BY seq").fetchall()
                    self.assertEqual(len(rows), 200)
                    self.assertEqual([json.loads(row["raw_record_json"]) for row in rows], obj["messages"])
                    self.assertTrue(all(json.loads(row["validation_flags"]) == [] for row in rows))
                    self.assertEqual([row["text_value"] for row in rows], [record["text"] for record in obj["messages"]])
                    heartbeat = store.heartbeat()
                    self.assertEqual((heartbeat["poll_seq"], heartbeat["resolved_seq"], heartbeat["open_gap_count"]), (725, 725, 0))


if __name__ == "__main__":
    unittest.main()
