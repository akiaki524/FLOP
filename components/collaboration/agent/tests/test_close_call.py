import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
from contextlib import redirect_stdout
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import collaboration_agent.close_call as close_call_module
import collaboration_agent.close_call_cli as close_call_cli
from collaboration_agent.close_call import (
    OFFICIAL_RULES_COMMIT,
    PACKAGE_MANIFEST_SHA256,
    PROJECT_DID,
    REFEREE_ROOMS,
    CloseCallError,
    build_first_trade_plan,
    canonical_terms,
    collect_current_price,
    collect_registration_state,
    maker_preimage,
    owner_text,
    parse_decimal,
    registration_sign_request,
    taker_preimage,
    trade_text,
    verify_maker_packet,
    verify_launch_pin,
    within_five_percent,
    within_limits,
)

REFEREE = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
TEST_SIG = "A" * 85 + "Q"
MAKER = "did:key:z6MktwtqAzuD5F77tAMBMwNs1KybZeff61EehV9xB1ZpXQG7"
MAKER_SIG = "4eaZdjcXexORuRBUPbFTfnMK_9MQFRYQ0DLi3fZdQTlTv-5TnFETiFyAxff34QV6r4ki7w0aU3NcuQQyjTrMBg"
LAUNCH = {
    "version": 1,
    "source": "public-live-seed-mirror-for-test-vector",
    "refereeDid": REFEREE,
    "message": {
        "from": REFEREE,
        "nonce": "1790337922535",
        "text": "{\"for\":1,\"limits\":[\"214.84\",\"237.44\"],\"package\":\"bae09812e25eb6f1369c611f24964f7ea0acafddfc45301a16f33f941296dafa\",\"price\":\"226.14\",\"rooms\":[\"d-close1-flow\",\"d-close1-state\",\"d-close1-price\",\"d-close1-positions\",\"d-close1-pnl\"],\"season\":\"close-1\",\"t\":\"seed\",\"trade\":{\"tid\":626256716983248,\"time\":\"2026-09-25T11:59:42.666000Z\"}}",
        "sig": "j3_asvvwrt67C13PdoA2Q1p0QfO24av1hvkC_2Nc5FJet9dKey97CFuKv1ZW9G7Ki4hxw86K-2dfhIjAjhlsCw",
    },
}


class FakeResult:
    returncode = 0

    def __init__(self, stdout):
        self.stdout = stdout


def allow_all_runner(argv, input, text, capture_output, timeout, check):
    request = json.loads(input)
    if request["op"].endswith("batch"):
        count = len(request.get("messages", []))
        return FakeResult(json.dumps({"ok": [True] * count}))
    return FakeResult('{"ok":true}')


class CloseCallTests(unittest.TestCase):
    def test_room_batch_splits_at_serialized_utf8_limit_and_preserves_order(self):
        messages = [
            {"from": REFEREE, "nonce": str(index), "sig": TEST_SIG,
             "text": '\u3042"\\\n' * 200}
            for index in range(200)
        ]
        messages[73]["sig"] = "invalid"
        payload = {"op": "verify-room-batch", "room": REFEREE_ROOMS[0],
                   "messages": messages}
        self.assertGreater(
            len(close_call_module.compact_json(payload).encode("utf-8")), 64 * 1024,
        )
        calls = []

        def batch_runner(argv, input, text, capture_output, timeout, check):
            self.assertLessEqual(len(input.encode("utf-8")), 64 * 1024)
            request = json.loads(input)
            self.assertEqual(request["op"], "verify-room-batch")
            self.assertEqual(request["room"], REFEREE_ROOMS[0])
            calls.append(request["messages"])
            return FakeResult(json.dumps({
                "ok": [int(message["nonce"]) % 3 != 0 for message in request["messages"]],
            }))

        results = close_call_module.verify_room_messages(
            REFEREE_ROOMS[0], messages, runner=batch_runner,
        )
        self.assertGreater(len(calls), 1)
        self.assertEqual([message for call in calls for message in call], messages)
        self.assertEqual(results, [index % 3 != 0 and index != 73 for index in range(200)])

    def test_room_batch_later_helper_failure_fails_closed(self):
        messages = [
            {"from": REFEREE, "nonce": str(index), "sig": TEST_SIG, "text": "x" * 4096}
            for index in range(200)
        ]
        for response, error in (
            ('{"ok":[]}', "CRYPTO_HELPER_FAILED"),
            ('{"ok":false}', "CRYPTO_HELPER_FAILED"),
            ('invalid json', "CRYPTO_HELPER_UNAVAILABLE"),
        ):
            with self.subTest(response=response):
                calls = []

                def batch_runner(argv, input, text, capture_output, timeout, check):
                    calls.append(json.loads(input))
                    if len(calls) == 2:
                        return FakeResult(response)
                    return FakeResult(json.dumps({"ok": [True] * len(calls[-1]["messages"])}))

                with self.assertRaisesRegex(CloseCallError, error):
                    close_call_module.verify_room_messages(
                        REFEREE_ROOMS[0], messages, runner=batch_runner,
                    )
                self.assertEqual(len(calls), 2)

    def test_launch_seed_known_vector(self):
        checked = verify_launch_pin(LAUNCH)
        self.assertEqual(checked["refereeDid"], REFEREE)
        self.assertEqual(checked["seed"]["package"], PACKAGE_MANIFEST_SHA256)
        self.assertEqual(sorted(checked["seed"]["rooms"]), sorted(REFEREE_ROOMS))

    def test_owner_registration_is_exact_and_typed(self):
        self.assertEqual(
            owner_text(),
            '{"t":"owner","season":"close-1","key":"' + PROJECT_DID + '"}',
        )
        request = registration_sign_request()
        self.assertEqual(request["action"], "CLOSE_CALL_OWNER_REGISTER")
        self.assertEqual(request["binding"]["room"], "close1")
        self.assertEqual(request["binding"]["rulesCommit"], OFFICIAL_RULES_COMMIT)
        self.assertEqual(request["preview"]["utf8"], owner_text())
        self.assertEqual(
            request["preview"]["sha256"],
            hashlib.sha256(owner_text().encode("utf-8")).hexdigest(),
        )
        self.assertNotIn("private", json.dumps(request).lower())

    def test_wrong_did_rejected(self):
        with self.assertRaisesRegex(CloseCallError, "WRONG_DID"):
            owner_text(REFEREE)

    def test_decimal_and_official_limit_primitives(self):
        from decimal import Decimal

        self.assertEqual(str(parse_decimal("225.03")), "225.03")
        self.assertTrue(within_limits(Decimal("100.00"), ["95.00", "105.00"]))
        self.assertTrue(within_five_percent(Decimal("95.00"), Decimal("100.00")))
        self.assertTrue(within_five_percent(Decimal("105.00"), Decimal("100.00")))
        self.assertFalse(within_five_percent(Decimal("94.99"), Decimal("100.00")))
        with self.assertRaises(CloseCallError):
            parse_decimal("1e2")

    def test_trade_protocol_serialization_only(self):
        terms = {
            "id": "a7f3",
            "maker": REFEREE,
            "px": "181.20",
            "qty": "2",
            "side": "sell",
            "taker": "any",
            "until": 1236,
        }
        encoded = canonical_terms(terms)
        self.assertEqual(
            encoded,
            '{"id":"a7f3","maker":"' + REFEREE
            + '","px":"181.20","qty":"2","side":"sell","taker":"any","until":1236}',
        )
        self.assertEqual(maker_preimage(terms), "close-1|terms|" + encoded)
        self.assertEqual(
            taker_preimage(terms),
            "close-1|accept|" + encoded + "|" + PROJECT_DID,
        )
        sig = TEST_SIG
        value = json.loads(trade_text(terms, PROJECT_DID, sig, sig))
        self.assertEqual(value["t"], "trade")
        self.assertEqual(value["maker_sig"], sig)
        self.assertEqual(value["taker_sig"], sig)

    def _maker_packet(self, **terms_overrides):
        terms = {
            "id": "first1",
            "maker": MAKER,
            "px": "226.14",
            "qty": "2.00",
            "side": "sell",
            "taker": "any",
            "until": 102,
        }
        terms.update(terms_overrides)
        return {"terms": terms, "maker_sig": MAKER_SIG}

    def _price_message(
        self, *, n=100, px="226.14", limits=None,
        ref_time="2026-09-27T00:08:20Z", seq=20,
    ):
        record = {
            "t": "price",
            "n": n,
            "ref": {"px": px, "time": ref_time, "tid": 812345},
            "limits": limits or ["214.84", "237.44"],
            "global": "226.10",
            "file": "f" * 64,
        }
        return {
            "from": REFEREE,
            "nonce": str(2000 + seq),
            "sig": TEST_SIG,
            "text": json.dumps(record, separators=(",", ":")),
            "seq": seq,
            "ts": "2026-09-27T00:10:00Z",
        }

    def _price_send(self, messages):
        calls = []

        def send(url, cap):
            calls.append(url)
            self.assertIn("/r/d-close1-price?", url)
            return (
                200,
                {"content-type": "application/json"},
                json.dumps({"messages": messages}).encode(),
                None,
            )

        return send, calls

    def test_real_maker_signature_verification_and_mutation_rejection(self):
        checked = verify_maker_packet(self._maker_packet())
        self.assertEqual(checked["terms"]["maker"], MAKER)
        self.assertEqual(
            checked["termsSha256"],
            hashlib.sha256(checked["canonicalTerms"].encode()).hexdigest(),
        )
        with self.assertRaisesRegex(CloseCallError, "INVALID_MAKER_SIGNATURE"):
            verify_maker_packet(self._maker_packet(qty="2.01"))

    def test_first_trade_valid_sell_builds_long_plan(self):
        send, calls = self._price_send([self._price_message()])
        pin = json.loads(json.dumps(LAUNCH))
        pin["message"]["sig"] = TEST_SIG
        plan = build_first_trade_plan(
            maker_packet=self._maker_packet(),
            launch_pin=pin,
            send=send,
            runner=allow_all_runner,
            now=datetime(2026, 9, 27, 0, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(plan["status"], "PLAN")
        self.assertEqual(plan["direction"], "LONG")
        self.assertEqual(plan["readiness"]["mint"], "UNKNOWN")
        self.assertEqual(plan["readiness"]["availableFunds"], "UNKNOWN")
        self.assertEqual(plan["risk"]["notional"], "452.2800")
        self.assertEqual(plan["risk"]["baseFee"], "4.522800")
        self.assertEqual(plan["risk"]["baseRequiredFunds"], "456.802800")
        self.assertEqual(calls, [
            "https://technocore.chat/r/d-close1-price?format=json&limit=200",
        ])
        request = plan["signRequest"]
        self.assertEqual(request["action"], "CLOSE_CALL_TAKER_LONG")
        self.assertEqual(request["expectedDid"], PROJECT_DID)
        self.assertEqual(request["subject"]["makerSide"], "SELL")
        self.assertEqual(request["binding"]["makerDid"], MAKER)
        self.assertEqual(request["binding"]["makerSig"], MAKER_SIG)
        self.assertEqual(request["binding"]["authenticatedPrice"]["refereeDid"], REFEREE)
        self.assertEqual(request["binding"]["authenticatedPrice"]["sweep"], 100)
        self.assertEqual(request["operation"]["steps"], [
            "TAKER_COUNTERSIGN",
            "CONSTRUCT_FINAL_TRADE_JSON",
            "SIGN_CLOSE1_ROOM_MESSAGE",
        ])
        self.assertEqual(request["operation"]["roomEnvelope"]["nonce"], "SIGNER_ALLOCATES")
        self.assertNotIn("private", json.dumps(request).lower())

    def test_first_trade_rejects_wrong_direction_parties_and_packet_shape(self):
        cases = (
            (self._maker_packet(side="buy"), "MAKER_SIDE_NOT_SELL"),
            (self._maker_packet(maker=PROJECT_DID), "SELF_MAKER_NOT_ALLOWED"),
            (self._maker_packet(taker=REFEREE), "TAKER_NOT_PROJECT_DID"),
        )
        for packet, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(CloseCallError, code):
                verify_maker_packet(packet, runner=allow_all_runner)

        extra = self._maker_packet()
        extra["note"] = "untrusted"
        with self.assertRaisesRegex(CloseCallError, "MAKER_PACKET_INVALID"):
            verify_maker_packet(extra, runner=allow_all_runner)
        malformed = self._maker_packet()
        malformed["terms"]["extra"] = True
        with self.assertRaisesRegex(CloseCallError, "INVALID_TERMS"):
            verify_maker_packet(malformed, runner=allow_all_runner)

    def test_invalid_maker_signature_rejected(self):
        def reject_maker(argv, input, text, capture_output, timeout, check):
            request = json.loads(input)
            return FakeResult('{"ok":false}' if request["op"] == "verify-maker-terms" else '{"ok":true}')

        with self.assertRaisesRegex(CloseCallError, "INVALID_MAKER_SIGNATURE"):
            verify_maker_packet(self._maker_packet(), runner=reject_maker)

    def test_first_trade_price_and_until_readiness_checks(self):
        pin = json.loads(json.dumps(LAUNCH))
        pin["message"]["sig"] = TEST_SIG
        now = datetime(2026, 9, 27, 0, 10, tzinfo=timezone.utc)

        outside_send, _ = self._price_send([
            self._price_message(limits=["220.00", "225.00"], px="223.00"),
        ])
        with self.assertRaisesRegex(CloseCallError, "TRADE_PRICE_OUTSIDE_CURRENT_LIMITS"):
            build_first_trade_plan(
                maker_packet=self._maker_packet(), launch_pin=pin,
                send=outside_send, runner=allow_all_runner, now=now,
            )

        valid_send, _ = self._price_send([self._price_message()])
        with self.assertRaisesRegex(CloseCallError, "UNTIL_BEFORE_NEXT_SWEEP"):
            build_first_trade_plan(
                maker_packet=self._maker_packet(until=100), launch_pin=pin,
                send=valid_send, runner=allow_all_runner, now=now,
            )

    def test_price_evidence_stale_is_explicit_and_missing_invalid_fails(self):
        launch = {"refereeDid": REFEREE}
        stale_send, _ = self._price_send([
            self._price_message(ref_time="2026-09-26T23:00:00Z"),
        ])
        price = collect_current_price(
            launch=launch, send=stale_send, runner=allow_all_runner,
            now=datetime(2026, 9, 27, 0, 10, tzinfo=timezone.utc),
        )
        self.assertTrue(price["stale"])
        self.assertEqual(price["referenceAgeSeconds"], "4200")
        self.assertIn("last reference standing", price["warning"])

        missing_send, _ = self._price_send([])
        with self.assertRaisesRegex(CloseCallError, "PRICE_EVIDENCE_UNAVAILABLE"):
            collect_current_price(
                launch=launch, send=missing_send, runner=allow_all_runner,
                now=datetime(2026, 9, 27, 0, 10, tzinfo=timezone.utc),
            )

        def reject_price(argv, input, text, capture_output, timeout, check):
            request = json.loads(input)
            if request["op"] == "verify-room-batch":
                return FakeResult('{"ok":[false]}')
            return FakeResult('{"ok":true}')

        invalid_send, _ = self._price_send([self._price_message()])
        with self.assertRaisesRegex(CloseCallError, "PRICE_EVIDENCE_UNAVAILABLE"):
            collect_current_price(
                launch=launch, send=invalid_send, runner=reject_price,
                now=datetime(2026, 9, 27, 0, 10, tzinfo=timezone.utc),
            )

    def test_latest_valid_price_selected_and_conflict_fails_closed(self):
        old = self._price_message(n=99, seq=19)
        latest = self._price_message(n=100, seq=20)
        send, _ = self._price_send([latest, old])
        price = collect_current_price(
            launch={"refereeDid": REFEREE}, send=send,
            runner=allow_all_runner,
            now=datetime(2026, 9, 27, 0, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(price["sweep"], 100)

        malformed_newer = self._price_message(n=101, seq=21)
        malformed_record = json.loads(malformed_newer["text"])
        malformed_record["limits"] = ["bad", "237.44"]
        malformed_newer["text"] = json.dumps(
            malformed_record, separators=(",", ":"),
        )
        malformed_send, _ = self._price_send([old, latest, malformed_newer])
        with self.assertRaisesRegex(CloseCallError, "PRICE_EVIDENCE_INVALID"):
            collect_current_price(
                launch={"refereeDid": REFEREE}, send=malformed_send,
                runner=allow_all_runner,
                now=datetime(2026, 9, 27, 0, 10, tzinfo=timezone.utc),
            )

        conflict = self._price_message(n=100, px="226.15", seq=21)
        conflict_record = json.loads(conflict["text"])
        conflict_record["limits"] = ["214.85", "237.45"]
        conflict["text"] = json.dumps(conflict_record, separators=(",", ":"))
        ambiguous_send, _ = self._price_send([latest, conflict])
        with self.assertRaisesRegex(CloseCallError, "PRICE_SWEEP_AMBIGUOUS"):
            collect_current_price(
                launch={"refereeDid": REFEREE}, send=ambiguous_send,
                runner=allow_all_runner,
                now=datetime(2026, 9, 27, 0, 10, tzinfo=timezone.utc),
            )

    def test_first_trade_source_has_no_sign_or_post_implementation(self):
        source = Path(close_call_module.__file__).read_text()
        crypto = Path(close_call_module.__file__).with_name("close_call_crypto.mjs").read_text()
        self.assertNotIn("requests.post", source)
        self.assertNotIn("urlopen", source)
        self.assertNotIn("crypto.sign", crypto)

    def _message(self, sender, record, seq):
        return {
            "from": sender,
            "nonce": str(1000 + seq),
            "sig": TEST_SIG,
            "text": json.dumps(record, separators=(",", ":")),
            "seq": seq,
            "ts": "2026-09-26T00:00:00Z",
        }

    def _registration_fixture(
        self, *, minted=False, owner=True, omitted=0,
        flow_sender=REFEREE, owner_sender=PROJECT_DID,
        room_owner=REFEREE,
    ):
        flow = {
            "t": "flow",
            "n": 100,
            "mints": [PROJECT_DID] if minted else [],
            "settled": [],
            "void": [],
            "omitted": {"mints": omitted},
        }
        close1 = []
        if owner:
            close1.append({
                "from": owner_sender,
                "nonce": "999",
                "sig": TEST_SIG,
                "text": owner_text(),
                "seq": 9,
                "ts": "2026-09-26T00:00:00Z",
            })
        rooms = {
            "d-close1-flow": [self._message(flow_sender, flow, 1)],
            "close1": close1,
        }
        calls = []

        def send(url, cap):
            calls.append(url)
            if "/kv/room-owners/" in url:
                if room_owner is None:
                    return 404, {"content-type": "text/plain"}, b"", None
                return 200, {"content-type": "text/plain"}, (room_owner + "\n").encode(), None
            room = url.split("/r/", 1)[1].split("?", 1)[0]
            if room not in rooms:
                raise AssertionError("registration path read unrelated room: " + room)
            return (
                200,
                {"content-type": "application/json"},
                json.dumps({"messages": rooms[room]}).encode(),
                None,
            )

        pin = json.loads(json.dumps(LAUNCH))
        pin["message"]["sig"] = TEST_SIG
        return pin, send, calls

    def test_registration_ready_only_from_explicit_mint_evidence(self):
        pin, send, calls = self._registration_fixture(minted=True)
        value = collect_registration_state(
            launch_pin=pin, send=send, runner=allow_all_runner,
        )
        self.assertEqual(value["registration"]["status"], "READY_MINTED")
        self.assertTrue(value["refereeRoomOwnership"]["verified"])
        self.assertFalse(any("/r/d-close1-price?" in call for call in calls))
        self.assertFalse(any("close1-offers" in call for call in calls))

    def test_registration_observed_is_not_promoted_to_mint(self):
        pin, send, _ = self._registration_fixture(minted=False, owner=True, omitted=12)
        value = collect_registration_state(
            launch_pin=pin, send=send, runner=allow_all_runner,
        )
        self.assertEqual(value["registration"]["status"], "REGISTRATION_OBSERVED")
        self.assertEqual(value["registration"]["mintEvidence"], "NOT_OBSERVED")
        self.assertEqual(value["registration"]["flowOmittedMints"], 12)

    def test_recent_absence_stays_unknown(self):
        pin, send, _ = self._registration_fixture(minted=False, owner=False, omitted=12)
        value = collect_registration_state(
            launch_pin=pin, send=send, runner=allow_all_runner,
        )
        self.assertEqual(value["registration"]["status"], "UNKNOWN")
        self.assertIn("cannot prove", value["registration"]["limitation"])

    def test_non_flow_record_cannot_prove_mint(self):
        pin, send, _ = self._registration_fixture(minted=True, owner=False)
        original_send = send

        def altered_send(url, cap):
            status, headers, raw, error = original_send(url, cap)
            if "/r/d-close1-flow?" in url and status == 200:
                payload = json.loads(raw.decode("utf-8"))
                record = json.loads(payload["messages"][0]["text"])
                record["t"] = "state"
                payload["messages"][0]["text"] = json.dumps(
                    record, separators=(",", ":"),
                )
                raw = json.dumps(payload).encode()
            return status, headers, raw, error

        value = collect_registration_state(
            launch_pin=pin, send=altered_send, runner=allow_all_runner,
        )
        self.assertEqual(value["registration"]["status"], "UNKNOWN")

    def test_invalid_flow_signature_cannot_prove_mint(self):
        pin, send, _ = self._registration_fixture(minted=True, owner=False)

        def reject_batch_runner(argv, input, text, capture_output, timeout, check):
            request = json.loads(input)
            if request["op"] == "verify-room-batch":
                return FakeResult(json.dumps({
                    "ok": [False] * len(request.get("messages", [])),
                }))
            return FakeResult('{"ok":true}')

        value = collect_registration_state(
            launch_pin=pin, send=send, runner=reject_batch_runner,
        )
        self.assertEqual(value["registration"]["status"], "UNKNOWN")

    def test_non_referee_flow_sender_cannot_prove_mint(self):
        pin, send, _ = self._registration_fixture(
            minted=True, owner=False, flow_sender=PROJECT_DID,
        )
        value = collect_registration_state(
            launch_pin=pin, send=send, runner=allow_all_runner,
        )
        self.assertEqual(value["registration"]["status"], "UNKNOWN")

    def test_invalid_owner_signature_or_sender_is_not_registration_evidence(self):
        pin, send, _ = self._registration_fixture(minted=False, owner=True)

        def reject_owner_runner(argv, input, text, capture_output, timeout, check):
            request = json.loads(input)
            if (
                request["op"] == "verify-room"
                and request.get("room") == "close1"
            ):
                return FakeResult('{"ok":false}')
            if request["op"] == "verify-room-batch":
                return FakeResult(json.dumps({
                    "ok": [True] * len(request.get("messages", [])),
                }))
            return FakeResult('{"ok":true}')

        value = collect_registration_state(
            launch_pin=pin, send=send, runner=reject_owner_runner,
        )
        self.assertEqual(value["registration"]["status"], "UNKNOWN")

        pin2, send2, _ = self._registration_fixture(
            minted=False, owner=True, owner_sender=REFEREE,
        )
        value2 = collect_registration_state(
            launch_pin=pin2, send=send2, runner=allow_all_runner,
        )
        self.assertEqual(value2["registration"]["status"], "UNKNOWN")

    def test_room_ownership_mismatch_is_visible_in_data_layer(self):
        pin, send, _ = self._registration_fixture(
            minted=False, owner=False, room_owner=PROJECT_DID,
        )
        value = collect_registration_state(
            launch_pin=pin, send=send, runner=allow_all_runner,
        )
        self.assertFalse(value["refereeRoomOwnership"]["verified"])
        self.assertTrue(all(
            owner == PROJECT_DID
            for owner in value["refereeRoomOwnership"]["owners"].values()
        ))

    def test_launch_signature_is_required(self):
        def reject_launch_runner(argv, input, text, capture_output, timeout, check):
            request = json.loads(input)
            if (
                request["op"] == "verify-room"
                and request.get("room") == "d-close1-price"
            ):
                return FakeResult('{"ok":false}')
            return allow_all_runner(
                argv, input, text, capture_output, timeout, check,
            )

        bad = json.loads(json.dumps(LAUNCH))
        bad["message"]["sig"] = TEST_SIG
        with self.assertRaisesRegex(CloseCallError, "LAUNCH_PIN_SIGNATURE"):
            verify_launch_pin(bad, runner=reject_launch_runner)

    def test_launch_package_rooms_and_referee_binding_are_checked(self):
        bad_package = json.loads(json.dumps(LAUNCH))
        record = json.loads(bad_package["message"]["text"])
        record["package"] = "0" * 64
        bad_package["message"]["text"] = json.dumps(record, separators=(",", ":"))
        bad_package["message"]["sig"] = TEST_SIG
        with self.assertRaisesRegex(CloseCallError, "LAUNCH_PIN_PACKAGE"):
            verify_launch_pin(bad_package, runner=allow_all_runner)

        bad_rooms = json.loads(json.dumps(LAUNCH))
        record = json.loads(bad_rooms["message"]["text"])
        record["rooms"] = record["rooms"][:-1]
        bad_rooms["message"]["text"] = json.dumps(record, separators=(",", ":"))
        bad_rooms["message"]["sig"] = TEST_SIG
        with self.assertRaisesRegex(CloseCallError, "LAUNCH_PIN_ROOMS"):
            verify_launch_pin(bad_rooms, runner=allow_all_runner)

        bad_from = json.loads(json.dumps(LAUNCH))
        bad_from["message"]["from"] = PROJECT_DID
        bad_from["message"]["sig"] = TEST_SIG
        with self.assertRaisesRegex(CloseCallError, "LAUNCH_PIN_REFEREE"):
            verify_launch_pin(bad_from, runner=allow_all_runner)

    def test_trade_protocol_rejects_invalid_until_taker_and_min_qty(self):
        base = {
            "id": "x1",
            "maker": REFEREE,
            "px": "100.00",
            "qty": "0.10",
            "side": "buy",
            "taker": "any",
            "until": 1,
        }
        bad_until = dict(base, until=2557)
        with self.assertRaisesRegex(CloseCallError, "INVALID_UNTIL"):
            canonical_terms(bad_until)

        bad_taker = dict(base, taker="not-a-did")
        with self.assertRaisesRegex(CloseCallError, "INVALID_TAKER"):
            canonical_terms(bad_taker)

        bad_qty = dict(base, qty="0.09")
        with self.assertRaisesRegex(CloseCallError, "DECIMAL_BELOW_MINIMUM"):
            canonical_terms(bad_qty)

    def _run_register_plan(self, observed):
        output = io.StringIO()
        with (
            patch.object(close_call_cli, "load_launch_pin", return_value={}),
            patch.object(
                close_call_cli, "collect_registration_state",
                return_value=observed,
            ),
            patch.object(
                close_call_cli, "registration_sign_request",
                wraps=registration_sign_request,
            ) as sign_request,
            redirect_stdout(output),
        ):
            result = close_call_cli.main([
                "--launch-pin", "ignored.json", "register-plan",
            ])
        return result, json.loads(output.getvalue()), sign_request

    def test_register_plan_status_branches_control_sign_request(self):
        ownership = {
            "verified": True,
            "owners": {room: REFEREE for room in REFEREE_ROOMS},
        }

        ready = {
            "registration": {"status": "READY_MINTED"},
            "refereeRoomOwnership": ownership,
        }
        result, payload, sign_request = self._run_register_plan(ready)
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "NO_ACTION")
        sign_request.assert_not_called()

        observed = {
            "registration": {"status": "REGISTRATION_OBSERVED"},
            "refereeRoomOwnership": ownership,
        }
        result, payload, sign_request = self._run_register_plan(observed)
        self.assertEqual(result, 2)
        self.assertEqual(payload["status"], "HUMAN_REVIEW")
        sign_request.assert_not_called()

        unknown = {
            "registration": {"status": "UNKNOWN"},
            "refereeRoomOwnership": ownership,
        }
        result, payload, sign_request = self._run_register_plan(unknown)
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "PLAN")
        self.assertEqual(
            payload["signRequest"]["action"], "CLOSE_CALL_OWNER_REGISTER",
        )
        sign_request.assert_called_once()

    def test_register_plan_stops_when_referee_ownership_not_verified(self):
        observed = {
            "registration": {"status": "UNKNOWN"},
            "refereeRoomOwnership": {
                "verified": False,
                "owners": {"d-close1-price": None},
            },
        }
        output = io.StringIO()
        with (
            patch.object(close_call_cli, "load_launch_pin", return_value={}),
            patch.object(close_call_cli, "collect_registration_state", return_value=observed),
            patch.object(close_call_cli, "registration_sign_request") as sign_request,
            redirect_stdout(output),
        ):
            result = close_call_cli.main([
                "--launch-pin", "ignored.json", "register-plan",
            ])
        self.assertEqual(result, 2)
        sign_request.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "HUMAN_REVIEW")
        self.assertEqual(payload["reason"], "REFEREE_ROOM_OWNERSHIP_NOT_VERIFIED")
        self.assertFalse(payload["retry"])

    def test_first_trade_cli_uses_read_only_plan_path(self):
        output = io.StringIO()
        expected = {"status": "PLAN", "externalEffects": "NONE"}
        with (
            patch.object(close_call_cli, "load_launch_pin", return_value={"pin": True}),
            patch.object(close_call_cli, "load_maker_packet", return_value={"packet": True}),
            patch.object(
                close_call_cli, "build_first_trade_plan", return_value=expected,
            ) as build,
            patch.object(close_call_cli, "collect_registration_state") as registration,
            redirect_stdout(output),
        ):
            result = close_call_cli.main([
                "--launch-pin", "launch.json", "first-trade-plan",
                "--maker-packet", "maker.json",
            ])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), expected)
        build.assert_called_once_with(
            maker_packet={"packet": True}, launch_pin={"pin": True},
        )
        registration.assert_not_called()

    def test_wrong_contest_launch_pin_rejected(self):
        bad = json.loads(json.dumps(LAUNCH))
        record = json.loads(bad["message"]["text"])
        record["season"] = "close-2"
        bad["message"]["text"] = json.dumps(record, separators=(",", ":"))
        bad["message"]["sig"] = TEST_SIG
        with self.assertRaisesRegex(CloseCallError, "LAUNCH_PIN_SEED"):
            verify_launch_pin(bad, runner=allow_all_runner)

    def test_no_llm_dependency_in_event_module(self):
        source = Path(close_call_module.__file__).read_text().lower()
        self.assertNotIn("openai", source)
        self.assertNotIn("anthropic", source)


if __name__ == "__main__":
    unittest.main()
