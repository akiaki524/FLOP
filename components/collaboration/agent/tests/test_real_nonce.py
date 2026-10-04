"""Offline parity and fail-closed tests for the bounded export nonce observer."""

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collaboration_agent.model import Invalid
from collaboration_agent.real_nonce import (
    MAX_EXPORT,
    PROFILE,
    PROJECT_DID,
    READ_BUDGET,
    URL,
    observe_project_nonce,
    parse_observation,
    tail_lines,
)


OTHER_DID = "did:key:z6Mk" + "f" * 44
TS = "2026-09-22T00:00:00Z"


def row(seq, *, sender=OTHER_DID, nonce=None, sig=None, text="inert", ts=TS):
    value = {"seq": seq, "ts": ts, "from": sender, "text": text}
    if nonce is not None:
        value["nonce"] = nonce
    if sig is not None:
        value["sig"] = sig
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def lines(*records):
    return b"".join(record + b"\n" for record in records)


def selected(seq, nonce, *, sender=PROJECT_DID, sig="A" * 86, text="tclk1 {}"):
    return row(seq, sender=sender, nonce=nonce, sig=sig, text=text)


def parse(raw, *, generation=1, profile=PROFILE):
    return parse_observation(raw, captured_at_ms=1_000,
                             generation=generation, profile=profile)


def headers(raw, *, generation="1", chunked=False, content_length=True):
    value = {
        "content-type": "application/x-ndjson; charset=utf-8",
        "x-room-generation": generation,
    }
    if chunked:
        value["transfer-encoding"] = "chunked"
    elif content_length:
        value["content-length"] = str(len(raw))
    return value


class RealNonceTests(unittest.TestCase):
    def test_complete_snapshot_under_budget_without_project_did_is_none(self):
        raw = lines(row(7), row(8))
        packet = parse(raw)
        self.assertTrue(packet["observedNone"])
        self.assertIsNone(packet["observedNonce"])
        self.assertEqual(packet["records"], [])
        self.assertEqual(packet["coverage"]["tailStart"], 0)
        self.assertEqual(packet["coverage"]["tailBytes"], len(raw))
        self.assertEqual(packet["coverage"]["lineCount"], 2)

    def test_complete_snapshot_under_budget_extracts_exact_nonce(self):
        nonce = 9007199254740993123
        raw = lines(row(7), selected(8, nonce))
        packet = parse(raw)
        self.assertEqual(packet["observedNonce"], str(nonce))
        self.assertFalse(packet["observedNone"])
        self.assertEqual(packet["records"][0]["nonce"], str(nonce))
        self.assertEqual(packet["records"][0]["seq"], 8)
        self.assertEqual(packet["coverage"]["basis"], PROFILE)

    def test_tail_discards_old_high_nonce_when_room_exceeds_budget(self):
        old = selected(100, 9999999999999999999)
        filler = b"x" * (READ_BUDGET + 128) + b"\n"
        raw = old + b"\n" + filler
        self.assertGreater(len(raw), READ_BUDGET)
        packet = parse(raw)
        self.assertTrue(packet["observedNone"])
        self.assertIsNone(packet["observedNonce"])
        self.assertGreater(packet["coverage"]["tailStart"], 0)

    def test_cutoff_inside_oldest_candidate_drops_that_partial_line(self):
        old = selected(100, 999)
        prefix = b"prefix-before-old\n"
        # Keep a valid newline after old, then use only unrelated padding. The
        # byte cutoff lands strictly inside old, so reverse_lines drops its
        # partial fragment rather than exposing it to the JSON parser.
        offset = max(1, len(old) // 2)
        after_len = READ_BUDGET - len(old) + offset
        suffix = b"\n" + b"x" * (after_len - 2) + b"\n"
        raw = prefix + old + suffix
        self.assertGreater(len(raw), READ_BUDGET)
        cutoff = len(raw) - READ_BUDGET
        self.assertEqual(cutoff, len(prefix) + offset)
        self.assertEqual(tail_lines(raw), [suffix[1:-1]])
        self.assertTrue(parse(raw)["observedNone"])

    def test_cutoff_exactly_at_record_start_drops_that_record(self):
        old = selected(100, 999)
        prefix = b"prefix-before-old\n"
        # Old plus suffix is exactly one budget. The cutoff is the first
        # byte of old; its preceding newline is outside the window.
        suffix = b"\n" + b"x" * (READ_BUDGET - len(old) - 2) + b"\n"
        self.assertEqual(len(old + suffix), READ_BUDGET)
        raw = prefix + old + suffix
        self.assertEqual(len(raw) - READ_BUDGET, len(prefix))
        self.assertEqual(tail_lines(raw), [suffix[1:-1]])
        self.assertTrue(parse(raw)["observedNone"])

    def test_exact_budget_at_offset_zero_includes_oldest_record(self):
        old = selected(100, 999)
        raw = old + b"\n" + b"x" * (READ_BUDGET - len(old) - 2) + b"\n"
        self.assertEqual(len(raw), READ_BUDGET)
        self.assertIn(old, tail_lines(raw))
        self.assertEqual(parse(raw)["observedNonce"], "999")

    def test_multibyte_utf8_cutoff_is_byte_based_and_drops_record(self):
        value = {"seq": 100, "ts": TS, "from": PROJECT_DID,
                 "text": "payload-é", "nonce": 999, "sig": "A" * 86}
        old = json.dumps(value, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
        needle = "é".encode("utf-8")
        target = old.find(needle) + 1  # second byte of the UTF-8 codepoint
        prefix = b"prefix-before-old\n"
        after_len = READ_BUDGET - len(old) + target
        suffix = b"\n" + b"x" * (after_len - 2) + b"\n"
        raw = prefix + old + suffix
        self.assertEqual(len(raw) - READ_BUDGET, len(prefix) + target)
        self.assertEqual(tail_lines(raw), [suffix[1:-1]])
        self.assertTrue(parse(raw)["observedNone"])

    def test_newest_qualifying_record_wins_even_when_nonce_decreases(self):
        raw = lines(selected(10, 900), selected(11, 100))
        packet = parse(raw)
        self.assertEqual(packet["observedNonce"], "100")
        self.assertEqual(packet["records"][0]["seq"], 11)

    def test_monotonic_multiple_nonces_selects_newest_exact_value(self):
        raw = lines(selected(10, 100), selected(11, 101), selected(12, 102))
        packet = parse(raw)
        self.assertEqual(packet["observedNonce"], "102")
        self.assertEqual(packet["records"][0]["seq"], 12)

    def test_nonce_above_2_to_53_and_maximum_19_digits_are_lossless(self):
        for value in (9_007_199_254_740_993, 9_999_999_999_999_999_999):
            with self.subTest(value=value):
                packet = parse(lines(selected(8_000_001, value)))
                self.assertEqual(packet["observedNonce"], str(value))
                self.assertEqual(packet["records"][0]["nonce"], str(value))

    def test_seq_does_not_have_to_start_at_one(self):
        packet = parse(lines(row(7_000_000), selected(7_000_001, 42)))
        self.assertEqual(packet["observedNonce"], "42")
        self.assertEqual(packet["records"][0]["seq"], 7_000_001)

    def test_did_mention_in_text_is_not_a_sender_match(self):
        mention = row(7, text=f"mention {PROJECT_DID}")
        packet = parse(lines(mention))
        self.assertTrue(packet["observedNone"])

    def test_escaped_did_bytes_are_skipped_before_json_sender_check(self):
        escaped = (
            b'{"seq":7,"ts":"' + TS.encode("ascii") +
            b'","from":"did\\u003Akey\\u003Az6MkjQVeF7TCAC6H6LKQnYFannWhsfWzzHhDDr3UhbZgHSTL",'
            b'"text":"x","nonce":123,"sig":"' + b"A" * 86 + b'"}\n'
        )
        self.assertNotIn(PROJECT_DID.encode("ascii"), escaped)
        self.assertTrue(parse(escaped)["observedNone"])

    def test_malformed_json_is_skipped_and_does_not_destroy_valid_tail(self):
        valid = selected(7, 17)
        # The malformed relevant record is newer and therefore encountered
        # first by the reverse scan; it must be skipped before valid is used.
        malformed = (
            b'{"seq":8,"ts":"' + TS.encode("ascii") + b'","from":"' +
            PROJECT_DID.encode("ascii") +
            b'","text":"x","nonce":999,"sig":"' + b"A" * 86 + b'"'
        )
        packet = parse(valid + b"\n" + malformed + b"\n")
        self.assertEqual(packet["observedNonce"], "17")

    def test_string_or_float_nonce_is_not_an_integer_guard_candidate(self):
        string_nonce = row(7, sender=PROJECT_DID, nonce="99", sig="A" * 86)
        float_nonce = (
            b'{"seq":8,"ts":"' + TS.encode("ascii") + b'","from":"' +
            PROJECT_DID.encode("ascii") +
            b'","text":"x","nonce":1.5,"sig":"' + b"A" * 86 + b'"}\n'
        )
        packet = parse(string_nonce + b"\n" + float_nonce)
        self.assertTrue(packet["observedNone"])

    def test_selected_bool_negative_and_20_digit_nonce_stop(self):
        for value in (True, -1, 10_000_000_000_000_000_000):
            with self.subTest(value=value), self.assertRaises(Invalid):
                parse(lines(selected(7, value)))

    def test_selected_record_without_signature_stops(self):
        with self.assertRaises(Invalid):
            parse(lines(row(7, sender=PROJECT_DID, nonce=17)))

    def test_duplicate_keys_use_last_value_like_server_parser(self):
        raw = (
            b'{"seq":7,"ts":"' + TS.encode("ascii") +
            b'","from":"' + OTHER_DID.encode("ascii") +
            b'","from":"' + PROJECT_DID.encode("ascii") +
            b'","text":"x","nonce":17,"sig":"' + b"A" * 86 + b'"}\n'
        )
        packet = parse(raw)
        self.assertEqual(packet["observedNonce"], "17")

    def test_generation_and_profile_mismatch_stop(self):
        raw = lines(selected(7, 17))
        with self.assertRaises(Invalid):
            parse(raw, generation=2)
        with self.assertRaises(Invalid):
            parse(raw, profile="unsupported-profile")

    def test_incomplete_snapshot_and_cap_stop(self):
        with self.assertRaises(Invalid):
            parse(lines(selected(7, 17)).rstrip(b"\n"))
        with self.assertRaises(Invalid):
            parse(b"x" * MAX_EXPORT)

    def test_observation_requires_complete_framing_and_fixed_export_metadata(self):
        raw = lines(selected(7, 17))
        calls = []

        def send(url, cap, *, intake_export):
            calls.append((url, cap, intake_export))
            return 200, headers(raw), raw, None

        packet = observe_project_nonce(send=send, clock_ms=lambda: 99)
        self.assertEqual(packet["observedNonce"], "17")
        self.assertEqual(calls, [(URL, MAX_EXPORT, True)])

        with self.assertRaises(Invalid):
            observe_project_nonce(
                send=lambda *_args, **_kwargs: (200, headers(raw, content_length=False), raw, None),
                clock_ms=lambda: 99,
            )
        with self.assertRaises(Invalid):
            observe_project_nonce(
                send=lambda *_args, **_kwargs: (200, headers(raw), raw[:-1], None),
                clock_ms=lambda: 99,
            )

    def test_chunked_framing_is_accepted_but_failed_read_or_generation_is_not(self):
        raw = lines(selected(7, 17))
        packet = observe_project_nonce(
            send=lambda *_args, **_kwargs: (200, headers(raw, chunked=True), raw, None),
            clock_ms=lambda: 99,
        )
        self.assertEqual(packet["observedNonce"], "17")
        for response in (
            (200, headers(raw, generation="2"), raw, None),
            (200, headers(raw), raw, "TIMEOUT"),
            (302, {**headers(raw), "location": "https://evil.invalid"}, raw, None),
            (500, headers(raw), raw, None),
        ):
            with self.subTest(response=response), self.assertRaises(Invalid):
                observe_project_nonce(send=lambda *_args, response=response, **_kwargs: response,
                                      clock_ms=lambda: 99)


if __name__ == "__main__":
    unittest.main()
