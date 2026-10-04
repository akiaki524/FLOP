"""Offline Stage C public-read acquisition tests; no sockets or real GETs."""

import json
import io
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collaboration_agent.first_accept_read import (
    acquire_snapshot,
    main,
    parse_records,
)
from collaboration_agent.model import Invalid
from collaboration_agent.real_nonce import MAX_EXPORT, PROFILE, URL


FIXTURE_DID = "did:key:z6Mk" + "f" * 44
OTHER_DID = "did:key:z6Mk" + "e" * 44
TS = "2026-09-23T00:00:00Z"


def row(seq, *, sender=OTHER_DID, nonce=1, signature="A" * 86,
        line="ordinary public line", timestamp=TS):
    return json.dumps({"seq": seq, "ts": timestamp, "from": sender,
                       "text": line, "nonce": nonce, "sig": signature},
                      ensure_ascii=True, separators=(",", ":")).encode("ascii")


def export(*rows):
    return b"".join(value + b"\n" for value in rows)


def headers(raw, *, generation="1", content_type="application/x-ndjson",
            chunked=False):
    result = {"content-type": content_type,
              "x-room-generation": generation}
    if chunked:
        result["transfer-encoding"] = "chunked"
    else:
        result["content-length"] = str(len(raw))
    return result


class FirstAcceptReadTests(unittest.TestCase):
    def acquire(self, raw, *, response_headers=None, expected_did=FIXTURE_DID):
        calls = []

        def send(url, cap, *, intake_export):
            calls.append((url, cap, intake_export))
            return 200, response_headers or headers(raw), raw, None

        packet = acquire_snapshot(send=send, clock_ms=lambda: 1_800_000_000_000,
                                  expected_did=expected_did)
        self.assertEqual(calls, [(URL, MAX_EXPORT, True)])
        return packet

    def test_one_get_builds_observation_and_lossless_records(self):
        raw = export(
            row(7, nonce=9_007_199_254_740_993),
            row(8, sender=FIXTURE_DID, nonce=9_999_999_999_999_999_999),
        )
        packet = self.acquire(raw)
        self.assertEqual(set(packet), {"version", "observation", "records"})
        self.assertEqual(packet["version"], 1)
        self.assertEqual(packet["observation"]["coverage"]["basis"], PROFILE)
        self.assertEqual(packet["observation"]["observedNonce"],
                         "9999999999999999999")
        self.assertEqual([record["nonce"] for record in packet["records"]],
                         ["9007199254740993", "9999999999999999999"])
        self.assertEqual([record["seq"] for record in packet["records"]], [7, 8])
        self.assertEqual(packet["records"][0]["timestampMs"], 1_790_121_600_000)

    def test_malformed_envelopes_are_skipped_for_node_candidate_validation(self):
        malformed_json = b'{"seq":2'
        invalid_sender = row(3, sender="not-a-did")
        string_nonce = json.dumps({"seq": 4, "ts": TS, "from": OTHER_DID,
                                   "text": "x", "nonce": "4", "sig": "x"},
                                  separators=(",", ":")).encode("ascii")
        invalid_timestamp = row(5, timestamp="not-a-time")
        valid_but_unverified = row(6, signature="not-verified-in-python",
                                   line="tclk1 malformed-or-unsupported")
        packet = self.acquire(export(malformed_json, invalid_sender, string_nonce,
                                     invalid_timestamp, valid_but_unverified))
        self.assertEqual(len(packet["records"]), 1)
        self.assertEqual(packet["records"][0]["seq"], 6)
        self.assertTrue(packet["observation"]["observedNone"])

    def test_profile_generation_and_http_framing_fail_closed(self):
        raw = export(row(1))
        bad_headers = (
            headers(raw, generation="2"),
            {"content-type": "application/json", "content-length": str(len(raw)),
             "x-room-generation": "1"},
            {"content-type": "application/x-ndjson", "x-room-generation": "1"},
            {**headers(raw), "content-length": str(len(raw) + 1)},
            {**headers(raw), "content-encoding": "gzip"},
        )
        for response_headers in bad_headers:
            with self.subTest(response_headers=response_headers), self.assertRaises(Invalid):
                self.acquire(raw, response_headers=response_headers)

    def test_incomplete_record_framing_fails_closed(self):
        with self.assertRaisesRegex(Invalid, "NONCE_OBSERVATION_INCOMPLETE_COVERAGE"):
            self.acquire(row(1))

    def test_chunked_complete_is_accepted(self):
        raw = export(row(1))
        self.assertEqual(len(self.acquire(raw, response_headers=headers(
            raw, chunked=True))["records"]), 1)

    def test_byte_bounded_exports_have_no_record_count_cutoff(self):
        for count in (4096, 4097, 10000):
            with self.subTest(count=count):
                raw = export(*(row(index + 1) for index in range(count)))
                self.assertLess(len(raw), MAX_EXPORT)
                packet = self.acquire(raw)
                self.assertEqual([r["seq"] for r in packet["records"]],
                                 list(range(1, count + 1)))

    def test_export_at_or_above_byte_limit_fails_closed(self):
        for size in (MAX_EXPORT, MAX_EXPORT + 1):
            with self.subTest(size=size), self.assertRaisesRegex(
                    Invalid, "NONCE_OBSERVATION_INCOMPLETE_COVERAGE"):
                self.acquire(b" " * (size - 1) + b"\n")

    def test_duplicate_keys_follow_server_last_value_and_order_is_preserved(self):
        duplicate = (b'{"seq":9,"ts":"' + TS.encode("ascii") + b'","from":"' +
                     OTHER_DID.encode("ascii") + b'","text":"old","text":"last",'
                     b'"nonce":8,"nonce":9,"sig":"x"}')
        records = parse_records(export(row(8), duplicate))
        self.assertEqual([record["seq"] for record in records], [8, 9])
        self.assertEqual(records[1]["line"], "last")
        self.assertEqual(records[1]["nonce"], "9")

    def test_cli_has_no_url_or_did_override(self):
        output = io.StringIO()
        # The production command accepts no parameters at all, so neither the
        # fixed URL nor the fixed Project DID can be overridden.
        with patch("collaboration_agent.first_accept_read.acquire_snapshot") as acquire, \
                redirect_stdout(output):
            self.assertEqual(main(["--url=evil", "--did=evil"]), 2)
        acquire.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()),
                         {"status": "HUMAN_STOP",
                          "reason": "FIRST_ACCEPT_PUBLIC_READ_FAILED"})


if __name__ == "__main__":
    unittest.main()
