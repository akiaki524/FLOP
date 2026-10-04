import hashlib
import unittest

from support import HAVE_CRYPTO, Key, accept, line, offer, ts, unsigned
from technocore_analyzer import signature, tclk

# Golden vectors from flop-labs/tclk tests/vectors.test.ts @ 5cc4ab93efbc8999a3a7e1471b639deca25998ea
PAYER = "did:key:z6Mk" + "f" * 44
PAYEE = "did:key:z6Mk" + "g" * 44
OFFER_ID = "0xd001fbbf4fa36d9ab8ea88df02a8b3303539e9d59f7ff9d9bfeb679318e9ce75"
CONTRACT_ID = "0x2768bf32b455317879796093ff2e5882371cbec238611ca71f555a7fcbe58e1c"
NON_ASCII_OFFER_ID = "0xfdad69c602bef151596e3e914cc3ca05b1ccd009211b57c4fdbf0ba0e0d4635b"
OFFER_LINE = ('tclk1 {"amount":"1000000","asset":"FLOP","claimByMs":1756703600000,"expiresMs":1756700600000,'
              '"from":"did:key:z6Mkffffffffffffffffffffffffffffffffffffffffffff",'
              f'"id":"{OFFER_ID}",'
              '"job":{"context":"ctx-1","id":"task-3f","proto":"a2a"},"lock":"hash",'
              '"nonce":"9f2c81d04c9e1f7a","rails":["flop-htlc","x402"],"refundAfterMs":1756707200000,'
              '"role":"payer","type":"offer"}')
ACCEPT_LINE = (f'tclk1 {{"contract":"{CONTRACT_ID}",'
               '"from":"did:key:z6Mkgggggggggggggggggggggggggggggggggggggggggggg",'
               f'"nonce":"0011223344556677","ref":"{OFFER_ID}",'
               '"statement":"0xabababababababababababababababababababababababababababababababab",'
               '"type":"accept"}')


class FrameTypeRobustness(unittest.TestCase):
    def test_non_string_type_is_an_ordinary_rejection(self):
        for bad in ('[]', '{}', '5', 'null', 'true', '[[1]]', '{"a":1}'):
            with self.assertRaises(tclk.FrameError, msg=bad):
                tclk.decode_frame('tclk1 {"type":%s}' % bad)
        with self.assertRaises(tclk.FrameError):
            tclk.validate_frame({"type": ["offer"]})


class GoldenVectors(unittest.TestCase):
    def test_offer_and_contract_ids_match_reference(self):
        fields = {"type": "offer", "from": PAYER, "role": "payer", "amount": "1000000", "asset": "FLOP",
                  "lock": "hash", "rails": ["flop-htlc", "x402"], "claimByMs": 1756703600000,
                  "refundAfterMs": 1756707200000, "expiresMs": 1756700600000,
                  "job": {"proto": "a2a", "id": "task-3f", "context": "ctx-1"}, "nonce": "9f2c81d04c9e1f7a"}
        self.assertEqual(tclk.offer_id(fields), OFFER_ID)
        full = {**fields, "id": OFFER_ID}
        self.assertEqual(line(full), OFFER_LINE)
        self.assertEqual(tclk.decode_frame(OFFER_LINE), full)
        core = {"from": PAYEE, "ref": OFFER_ID, "statement": "0x" + "ab" * 32, "nonce": "0011223344556677"}
        self.assertEqual(tclk.contract_id(full, core), CONTRACT_ID)
        self.assertEqual(line({"type": "accept", **core, "contract": CONTRACT_ID}), ACCEPT_LINE)

    def test_non_ascii_offer_id_hashes_escaped_form(self):
        fields = {"type": "offer", "from": PAYER, "role": "payer", "lock": "hash", "amount": "100", "asset": "FLOP",
                  "rails": ["flop-htlc"], "claimByMs": 1756703600000, "refundAfterMs": 1756707200000,
                  "expiresMs": 1756700600000, "job": {"proto": "a2a", "id": "tâche-1"}, "nonce": "9f2c81d04c9e1f7a"}
        self.assertEqual(tclk.offer_id(fields), NON_ASCII_OFFER_ID)
        self.assertIn("\\u00e2", line({**fields, "id": NON_ASCII_OFFER_ID}))

    def test_decode_is_fail_closed(self):
        bad = OFFER_LINE.replace('"asset":"FLOP"', '"asset":"FLOP","extra":1')
        with self.assertRaises(tclk.FrameError):
            tclk.decode_frame(bad)
        with self.assertRaises(tclk.FrameError):
            tclk.decode_frame(OFFER_LINE.replace("1000000", "1000001"))  # id no longer matches
        with self.assertRaises(tclk.FrameError):
            tclk.decode_frame(OFFER_LINE.replace("1756703600000", "true"))
        self.assertEqual(tclk.line_version("tclk2 {}"), "tclk/2")
        self.assertIsNone(tclk.line_version("hello"))


def rec(room, seq, message, stream="s1"):
    status, _ = signature.verify_record(room, message)
    return {"ref": f"src:{room}/{stream}#{seq}", "room": room, "stream": stream, "seq": seq,
            "message": message, "sig_status": status, "record_sha256": hashlib.sha256(str(message).encode()).hexdigest()}


@unittest.skipUnless(HAVE_CRYPTO, "cryptography required for signed transcripts")
class Transcript(unittest.TestCase):
    def setUp(self):
        self.payer, self.payee, self.other = Key(1), Key(2), Key(3)
        self.pre = b"\x42" * 32
        self.off = offer(self.payer.did)
        self.acc = accept(self.off, self.payee.did, self.pre)
        self.deal = tclk.deal_room(self.acc["contract"])
        self.base = [
            rec("tclk-offers", 1, self.payer.message("tclk-offers", 1, line(self.off), ts(0))),
            rec("tclk-offers", 2, self.payee.message("tclk-offers", 2, line(self.acc), ts(1))),
        ]

    def frame(self, key, kind, seq, room=None, minute=5, **fields):
        body = {"type": kind, "from": key.did, "contract": self.acc["contract"], **fields}
        room = room or self.deal
        return rec(room, seq, key.message(room, seq, line(body), ts(minute)))

    def analyze(self, extra, rooms=None):
        return tclk.analyze(self.base + extra, rooms or {"tclk-offers", self.deal}, 1786000000000)

    def status(self, result):
        return result["contracts"][0]["protocol_status"]

    def test_normal_claim_flow_and_receipt_does_not_prove_payment(self):
        result = self.analyze([
            self.frame(self.payer, "lock", 1, rail="flop-htlc", ref="escrow-1"),
            self.frame(self.payee, "heartbeat", 2, nonce="aabbccdd"),
            self.frame(self.payee, "reveal", 3, secret="0x" + self.pre.hex(), ref="escrow-1"),
            self.frame(self.payer, "receipt", 4, outcome="claimed"),
        ])
        view = result["contracts"][0]
        self.assertEqual(view["protocol_status"], "claimed")
        self.assertIn(view["protocol_status"], tclk.STATUSES)
        self.assertTrue(view["settlement_evidence"].startswith("NONE"))
        self.assertTrue(view["rail_verification"].startswith("NOT_CHECKED"))
        self.assertIn("paper", view["value_backing_note"])

    def test_rejections_never_destroy_valid_state(self):
        result = self.analyze([
            self.frame(self.other, "lock", 1, rail="flop-htlc", ref="x"),               # non-party
            self.frame(self.payer, "lock", 2, rail="btc-htlc", ref="x"),                # rail not offered
            self.frame(self.payer, "lock", 3, rail="flop-htlc", ref="escrow-1"),
            self.frame(self.payee, "reveal", 4, secret="0x" + "00" * 32),               # wrong secret
        ])
        self.assertEqual(self.status(result), "locked")
        verdicts = [r["verdict"] for r in result["records"]]
        self.assertEqual(verdicts.count(tclk.REJECTED), 3)
        wrong = [r for r in result["records"] if r["verdict"] == tclk.REJECTED]
        self.assertTrue(all(r["protocol_invalid"] for r in wrong))

    def test_wrong_room_forged_from_and_replay(self):
        lock = {"type": "lock", "from": self.payer.did, "contract": self.acc["contract"], "rail": "flop-htlc", "ref": "e"}
        forged = {"type": "lock", "from": self.payer.did, "contract": self.acc["contract"], "rail": "flop-htlc", "ref": "e"}
        result = self.analyze([
            rec("tclk-offers", 3, self.payer.message("tclk-offers", 3, line(lock), ts(4))),   # wrong room
            rec(self.deal, 1, self.other.message(self.deal, 1, line(forged), ts(5))),          # from mismatch
            rec("tclk-offers", 4, self.payee.message("tclk-offers", 4, line(self.acc), ts(6), nonce=99)),  # re-sent accept
        ])
        by_seq = {(r["room"], r["seq"]): r for r in result["records"]}
        self.assertEqual(by_seq[("tclk-offers", 3)]["verdict"], tclk.REJECTED)
        forged_v = by_seq[(self.deal, 1)]
        self.assertEqual(forged_v["attributable_did"], self.other.did)   # only the real signer
        self.assertEqual(by_seq[("tclk-offers", 4)]["verdict"], tclk.REPLAY)
        self.assertFalse(by_seq[("tclk-offers", 4)]["protocol_invalid"])
        self.assertEqual(self.status(result), "accepted")

    def test_bad_type_frame_does_not_stop_analysis_of_other_records(self):
        extra = [rec("tclk-offers", 50 + i, self.other.message("tclk-offers", 50 + i, 'tclk1 {"type":%s}' % t, ts(2)))
                 for i, t in enumerate(("[]", "{}", "7"))]
        result = self.analyze(extra + [self.frame(self.payer, "lock", 1, rail="flop-htlc", ref="e")])
        self.assertEqual(self.status(result), "locked")  # the normal contract is still reconstructed
        bad = [r for r in result["records"] if r["seq"] >= 50]
        self.assertEqual({(r["verdict"], r["protocol_invalid"]) for r in bad}, {(tclk.REJECTED, True)})

    def test_unobserved_lock_is_insufficient_history_not_a_violation(self):
        reveal = self.frame(self.payee, "reveal", 3, secret="0x" + self.pre.hex())
        result = self.analyze([reveal])
        step = next(r for r in result["records"] if r["type"] == "reveal")
        self.assertEqual((step["verdict"], step["protocol_invalid"]), (tclk.INSUFFICIENT, False))
        self.assertIn("lock frame was not observed", step["reason"])
        self.assertEqual(self.status(result), "accepted")  # last confirmed state is kept
        self.assertEqual(result["contracts"][0]["coverage_state"], "INCONCLUSIVE")
        self.assertIn("TCLK_TRANSITION_NOT_OBSERVED", [c["kind"] for c in result["coverage"]])
        refund = self.frame(self.payer, "refund", 4, minute=59)
        self.assertEqual(next(r for r in self.analyze([refund])["records"] if r["type"] == "refund")["verdict"],
                         tclk.INSUFFICIENT)

    def test_real_violations_with_sufficient_history_stay_protocol_invalid(self):
        lock = self.frame(self.payer, "lock", 1, rail="flop-htlc", ref="escrow-1")
        cases = {
            "wrong secret": self.frame(self.payee, "reveal", 2, secret="0x" + "00" * 32),
            "wrong party": self.frame(self.payer, "reveal", 2, secret="0x" + self.pre.hex()),
        }
        for name, frame in cases.items():
            result = self.analyze([lock, frame])
            step = next(r for r in result["records"] if r["type"] == "reveal")
            self.assertEqual((step["verdict"], step["protocol_invalid"]), (tclk.REJECTED, True), name)
        # reveal recorded BEFORE the lock, although the lock is observed later: a genuine order violation
        early = self.frame(self.payee, "reveal", 1, secret="0x" + self.pre.hex())
        late_lock = self.frame(self.payer, "lock", 2, rail="flop-htlc", ref="escrow-1")
        result = self.analyze([early, late_lock])
        step = next(r for r in result["records"] if r["type"] == "reveal")
        self.assertEqual((step["verdict"], step["protocol_invalid"]), (tclk.REJECTED, True))
        # terminal state then another reveal: rejected, not "missing history"
        done = self.analyze([lock, self.frame(self.payee, "reveal", 2, secret="0x" + self.pre.hex()),
                             self.frame(self.payee, "reveal", 3, secret="0x" + self.pre.hex(), ref="other")])
        self.assertEqual(self.status(done), "claimed")

    def test_inapplicable_lock_does_not_make_history_look_sufficient(self):
        reveal = self.frame(self.payee, "reveal", 9, secret="0x" + self.pre.hex())
        other_room = "mb-p-tclk-0000000000000000"
        cases = {
            "lock in the wrong deal room": self.frame(self.payer, "lock", 1, room=other_room, rail="flop-htlc", ref="e"),
            "lock signed by a non-payer": self.frame(self.other, "lock", 1, rail="flop-htlc", ref="e"),
            "lock naming a rail that was not offered": self.frame(self.payer, "lock", 1, rail="btc-htlc", ref="e"),
        }
        for name, lock in cases.items():
            result = self.analyze([lock, reveal])
            step = next(r for r in result["records"] if r["type"] == "reveal")
            self.assertEqual((step["verdict"], step["protocol_invalid"]), (tclk.INSUFFICIENT, False), name)
            self.assertEqual(self.status(result), "accepted", name)
            bad_lock = next(r for r in result["records"] if r["type"] == "lock")
            self.assertNotEqual(bad_lock["verdict"], tclk.ACCEPTED, name)

    def at(self, key, kind, seq, when, **fields):
        body = {"type": kind, "from": key.did, "contract": self.acc["contract"], **fields}
        return rec(self.deal, seq, key.message(self.deal, seq, line(body), when))

    def receipt_step(self, result, outcome):
        return next(r for r in result["records"] if r["type"] == "receipt")

    def assert_insufficient_receipt(self, result, name, status="accepted"):
        step = self.receipt_step(result, None)
        self.assertEqual((step["verdict"], step["protocol_invalid"]), (tclk.INSUFFICIENT, False), name)
        self.assertEqual(self.status(result), status, name)  # last confirmed state kept
        gaps = [c for c in result["coverage"] if c["kind"] == "TCLK_TRANSITION_NOT_OBSERVED" and c["ref"] == step["ref"]]
        self.assertEqual(len(gaps), 1, name)

    def test_reveal_that_could_not_claim_does_not_count_as_observed_prerequisite(self):
        late = "2027-06-01T00:00:00Z"  # after refundAfterMs (reveal deadline)
        good = "0x" + self.pre.hex()
        cases = {
            "secret does not open the statement": self.frame(self.payee, "reveal", 1, secret="0x" + "00" * 32),
            "wrong party": self.frame(self.payer, "reveal", 1, secret=good),
            "reveal deadline passed": self.at(self.payee, "reveal", 1, late, secret=good),
            "rail ref that no observed lock names": self.frame(self.payee, "reveal", 1, secret=good, ref="nope"),
        }
        for name, reveal in cases.items():
            with self.subTest(name):
                result = self.analyze([reveal, self.frame(self.payee, "receipt", 2, minute=7, outcome="claimed")])
                self.assert_insufficient_receipt(result, name)
        # mismatched ref while a lock IS observed: the lock applies, the reveal cannot claim
        result = self.analyze([self.frame(self.payer, "lock", 1, rail="flop-htlc", ref="escrow-1"),
                               self.frame(self.payee, "reveal", 2, secret=good, ref="other"),
                               self.frame(self.payee, "receipt", 3, minute=7, outcome="claimed")])
        self.assert_insufficient_receipt(result, "ref mismatch with observed lock", status="locked")
        # control: a reveal that could claim still counts (existing classification unchanged)
        result = self.analyze([self.frame(self.payee, "reveal", 1, secret=good),
                               self.frame(self.payee, "receipt", 2, minute=7, outcome="claimed")])
        step = self.receipt_step(result, None)
        self.assertEqual((step["verdict"], step["protocol_invalid"]), (tclk.REJECTED, True))

    def test_refund_before_its_window_does_not_count_as_prerequisite(self):
        # a refund before its window cannot establish "refunded"
        result = self.analyze([self.frame(self.payer, "refund", 1),
                               self.frame(self.payer, "receipt", 2, minute=7, outcome="refunded")])
        self.assert_insufficient_receipt(result, "early refund")

    def test_lock_after_refund_window_does_not_count_as_prerequisite(self):
        late = "2027-06-01T00:00:00Z"  # refund window open
        result = self.analyze([self.at(self.payer, "lock", 1, late, rail="flop-htlc", ref="e"),
                               self.at(self.payer, "refund", 2, "2027-06-01T00:05:00Z")])
        step = next(r for r in result["records"] if r["type"] == "refund")
        self.assertEqual((step["verdict"], step["protocol_invalid"]), (tclk.INSUFFICIENT, False))
        self.assertEqual(self.status(result), "accepted")

    def test_replay_cutoff_uses_only_records_observed_by_then(self):
        lock = self.frame(self.payer, "lock", 1, minute=5, rail="flop-htlc", ref="escrow-1")
        reveal = self.frame(self.payee, "reveal", 2, minute=6, secret="0x" + self.pre.hex())
        cache = self.base + [lock, reveal]
        at = lambda minute: tclk.parse_rfc3339_ms(ts(minute))
        states = {m: tclk.analyze(cache, {"tclk-offers", self.deal}, at(m), at(m))["contracts"][0]["protocol_status"]
                  for m in (0, 3, 5, 6, 10)}
        self.assertEqual(states, {0: "proposed", 3: "accepted", 5: "locked", 6: "claimed", 10: "claimed"})
        live = tclk.analyze(cache, {"tclk-offers", self.deal}, at(3))  # no cutoff: everything stays
        self.assertEqual(live["contracts"][0]["protocol_status"], "claimed")
        self.assertEqual(len(cache), 4)  # the cache itself is untouched

    def test_streams_order_numerically_across_epochs(self):
        offer_rec = dict(self.base[0], stream="epoch-2")
        accept_rec = dict(self.base[1], stream="epoch-10", seq=1)  # epoch-10 > epoch-2, lexically smaller
        result = tclk.analyze([accept_rec, offer_rec], {"tclk-offers"}, 0)
        self.assertEqual(result["contracts"][0]["protocol_status"], "accepted")
        self.assertEqual(result["orphans"], [])

    def test_orphan_frames_are_insufficient_history_not_violation(self):
        result = tclk.analyze([self.frame(self.payer, "lock", 1, rail="flop-htlc", ref="e")], {self.deal}, 0)
        self.assertEqual(result["records"][0]["verdict"], tclk.INSUFFICIENT)
        self.assertFalse(result["records"][0]["protocol_invalid"])
        self.assertTrue(any(c["kind"] == "TCLK_PREREQUISITE_NOT_OBSERVED" for c in result["coverage"]))

    def test_deal_room_not_observed_is_coverage(self):
        result = tclk.analyze(self.base, {"tclk-offers"}, 0)
        self.assertEqual(result["contracts"][0]["coverage_state"], "DEAL_ROOM_NOT_OBSERVED")

    def test_unsigned_invalid_signature_missing_time_and_unknown_version(self):
        bad_sig = self.payer.message("tclk-offers", 9, line(self.off), ts(0))
        bad_sig["text"] = bad_sig["text"].replace("1000", "1001")
        no_time = self.payer.message("tclk-offers", 10, line(offer(self.payer.did, nonce="abcdef0123")), None)
        del no_time["ts"]
        records = [rec("tclk-offers", 8, unsigned(8, line(self.off), ts(0))), rec("tclk-offers", 9, bad_sig),
                   rec("tclk-offers", 10, no_time),
                   rec("tclk-offers", 11, self.payer.message("tclk-offers", 11, "tclk2 {}", ts(0)))]
        result = tclk.analyze(records, {"tclk-offers"}, 0)
        verdicts = [r["verdict"] for r in result["records"]]
        self.assertEqual(verdicts, [tclk.IGNORED_UNSIGNED, tclk.SIG_REJECTED, tclk.UNVERIFIABLE, tclk.UNSUPPORTED_VERSION])
        self.assertIsNone(result["records"][1]["attributable_did"])
        self.assertEqual(result["contracts"], [])

    def test_deadline_uses_record_time_not_now(self):
        late = offer(self.payer.did, nonce="deadbeef01", expiresMs=1756700000000)  # 2025-09
        acc = accept(late, self.payee.did)
        records = [rec("tclk-offers", 1, self.payer.message("tclk-offers", 1, line(late), ts(0))),
                   rec("tclk-offers", 2, self.payee.message("tclk-offers", 2, line(acc), ts(1)))]
        result = tclk.analyze(records, {"tclk-offers"}, 0)
        step = result["records"][1]
        self.assertEqual(step["reason"], "offer has expired")
        self.assertFalse(step["protocol_invalid"])

    def test_point_lock_witness(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        y = 12345678901234567890
        point = "0x" + ec.derive_private_key(y, ec.SECP256K1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint).hex()
        self.assertTrue(tclk.valid_statement("point", point))
        self.assertTrue(tclk.verify_secret("point", point, "0x" + y.to_bytes(32, "big").hex()))
        self.assertFalse(tclk.verify_secret("point", point, "0x" + (y + 1).to_bytes(32, "big").hex()))
        self.assertFalse(tclk.valid_statement("point", "0x02" + "00" * 32))


if __name__ == "__main__":
    unittest.main()
