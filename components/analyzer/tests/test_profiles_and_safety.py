import json
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from support import HAVE_CRYPTO, Key, ROOT, ts, unsigned
from technocore_analyzer import analysis, events, render, reports, semantic, signature
from technocore_analyzer.util import parse_rfc3339_ms

PROFILE = json.loads((ROOT / "examples" / "close-call.profile.json").read_text())


def rec(room, seq, message, stream="s"):
    status, _ = signature.verify_record(room, message)
    return {"ref": f"x:{room}#{seq}", "room": room, "stream": stream, "seq": seq, "generation": 1,
            "message": message, "sig_status": status, "record_sha256": str(seq), "also_refs": []}


def at(iso):
    return parse_rfc3339_ms(iso)


def sweep_post(kind, n, **extra):
    """Minimum official shapes from close-call-game.md (blob 4e3ed2e7)."""
    base = {"price": {"ref": {"px": "1", "time": "t", "tid": "1"}, "limits": ["1", "2"], "global": "1"},
            "flow": {"mints": [], "rooms": [], "settled": "0", "void": "0", "missed": []},
            "positions": {"open": "0", "longs": "0", "shorts": "0", "top": []},
            "pnl": {"mark": "0", "top": []},
            "state": {"root": "r", "owners": "0", "rooms": "0"}}[kind]
    return json.dumps({"t": kind, "n": n, **base, "file": "h", **extra})


@unittest.skipUnless(HAVE_CRYPTO, "cryptography required")
class CloseCallProfileTests(unittest.TestCase):
    def setUp(self):
        self.ref = Key(77)
        self.profile = dict(PROFILE, official_dids=[self.ref.did])
        self.msgs = []
        for n in (1, 2, 4):
            stamp = f"2026-09-25T12:{5 * n:02d}:00Z"
            for i, (room, kinds) in enumerate(self.profile["referee_room_posts"].items()):
                seq = n * 10 + i
                self.msgs.append(rec(room, seq, self.ref.message(room, seq, sweep_post(kinds[0], n), stamp)))

    def test_sweep_monitoring_window_and_official_attribution(self):
        out = events.close_call(self.profile, self.msgs, at("2026-09-25T12:26:00Z"))
        per = out["sweep_coverage"]["per_room"]["d-close1-price"]
        self.assertEqual(per["expected_through_sweep"], 5)
        self.assertEqual(per["missing_sweeps_sample"], [3, 5])
        self.assertEqual(out["referee_attribution"], {"OFFICIAL_KEY_MATCH": 15})
        self.assertEqual(out["phase"], "TRADING_OPEN")
        self.assertEqual(out["recalculation"]["status"], "RECALCULATION_NOT_SUPPORTED")
        after = events.close_call(self.profile, self.msgs, at("2026-10-20T00:00:00Z"))
        # after the lock the expected range stops at the lock sweep; no open-ended warnings
        self.assertEqual(after["sweep_coverage"]["per_room"]["d-close1-price"]["expected_through_sweep"], 2556)
        self.assertEqual(after["trading_opportunities"]["status"], "CLOSED_AT_TRADING_LOCK")
        self.assertTrue(after["phase"].startswith("ENDED"))
        self.assertIn("final_price_time + 24h", after["timeline"]["observation_end_origin"])

    def test_unconfirmed_key_is_inconclusive_not_observed(self):
        out = events.close_call(PROFILE, self.msgs, at("2026-09-25T12:26:00Z"))
        self.assertEqual(out["official_keys"]["status"], "OFFICIAL_KEY_UNCONFIRMED")
        self.assertEqual(out["sweep_coverage"]["status"], "INCONCLUSIVE_OFFICIAL_KEY_UNCONFIRMED")
        self.assertNotIn("per_room", out["sweep_coverage"])
        self.assertEqual(out["findings"], [])
        # a configured key that is not this signer: its posts never count as observed sweeps
        other = events.close_call(dict(PROFILE, official_dids=[Key(78).did]), self.msgs, at("2026-09-25T12:26:00Z"))
        self.assertEqual(other["sweep_coverage"]["per_room"]["d-close1-price"]["observed_sweeps"], 0)
        self.assertEqual(other["referee_attribution"], {"SIGNED_NOT_OFFICIAL_KEY": 15})

    def test_wrong_room_and_malformed_shape_are_not_counted(self):
        price, flow = "d-close1-price", "d-close1-flow"
        extra = [rec(flow, 900, self.ref.message(flow, 900, sweep_post("price", 3), "2026-09-25T12:15:00Z")),
                 rec(price, 901, self.ref.message(price, 901, json.dumps({"t": "price", "n": 5, "file": "h"}),
                                                  "2026-09-25T12:25:00Z")),
                 rec(price, 902, self.ref.message(price, 902, sweep_post("price", True), "2026-09-25T12:25:00Z"))]
        out = events.close_call(self.profile, self.msgs + extra, at("2026-09-25T12:26:00Z"))
        self.assertEqual(out["referee_posts_not_counted"], {"WRONG_ROOM_FOR_POST_TYPE": 1, "MALFORMED_MINIMUM_SHAPE": 2})
        self.assertEqual(out["sweep_coverage"]["per_room"][price]["missing_sweeps_sample"], [3, 5])
        self.assertEqual(out["sweep_coverage"]["per_room"][flow]["missing_sweeps_sample"], [3, 5])

    def test_conflicting_official_posts(self):
        room = "d-close1-price"
        dup = rec(room, 999, self.ref.message(room, 999, sweep_post("price", 1, global_="x"), "2026-09-25T12:05:30Z"))
        out = events.close_call(self.profile, self.msgs + [dup], at("2026-09-25T12:26:00Z"))
        self.assertEqual([f["category"] for f in out["findings"]], ["EVIDENCE_CONFLICT"])

    def test_non_json_number_in_one_post_does_not_fail_the_profile(self):
        room = "d-close1-price"
        for name, token in (("NaN", "NaN"), ("Infinity", "Infinity"), ("huge exponent", "1e999")):
            with self.subTest(name):
                bad = sweep_post("price", 3).replace('"global": "1"', '"global": "1", "x": ' + token)
                post = rec(room, 950, self.ref.message(room, 950, bad, "2026-09-25T12:15:00Z"))
                out = events.run_profiles([self.profile], self.msgs + [post], at("2026-09-25T12:26:00Z"))[0]
                self.assertNotEqual(out.get("status"), "FAILED")
                per = out["sweep_coverage"]["per_room"][room]
                self.assertEqual(per["missing_sweeps_sample"], [3, 5])  # the bad post is not counted as sweep 3

    def test_post_with_non_string_type_does_not_fail_the_profile_or_drop_the_lock_closure(self):
        after_lock = at("2026-10-20T00:00:00Z")
        good = rec("close1", 1, unsigned(1, "market-maker wanted", "2026-10-04T08:00:00Z"))
        baseline = events.run_profiles([PROFILE], [good], after_lock)[0]
        self.assertNotEqual(baseline.get("status"), "FAILED")
        self.assertEqual(baseline["closed_rooms_for_opportunities"], ["close1"])
        for name, text in (("list", '{"t":[]}'), ("dict", '{"t":{}}'), ("number", '{"t":7}'), ("null", '{"t":null}')):
            with self.subTest(name):
                bad = rec("close1", 2, unsigned(2, text, "2026-10-04T08:01:00Z"))
                out = events.run_profiles([PROFILE], [good, bad], after_lock)[0]
                self.assertNotEqual(out.get("status"), "FAILED")
                self.assertEqual(out["closed_rooms_for_opportunities"], ["close1"])
                self.assertEqual(out["trading_opportunities"]["player_posts_observed"],
                                 baseline["trading_opportunities"]["player_posts_observed"])

    def test_disabled_or_broken_profile_does_not_stop_core(self):
        out = events.run_profiles([dict(PROFILE, enabled=False), {"id": "x", "kind": "close-call"},
                                   {"id": "y", "kind": "other"}], self.msgs, 0)
        broken = events.run_profiles([dict(PROFILE, sweep_seconds="x")], self.msgs, 0)
        self.assertEqual(broken[0]["status"], "FAILED")
        self.assertEqual([o.get("status") for o in out], ["DISABLED (Core unaffected)", "INCOMPLETE_CONFIG", "UNSUPPORTED_PROFILE_KIND"])

    def test_trading_room_text_opportunities_close_at_lock(self):
        post = rec("close1", 1, unsigned(1, "Looking for collaborators to market-make NVDA", "2026-10-04T08:00:00Z"))
        tclk_empty = {"contracts": []}
        before = analysis.opportunities([post], tclk_empty, {}, at("2026-10-04T08:30:00Z"), 86400000)
        after = analysis.opportunities([post], tclk_empty, {}, at("2026-10-04T09:30:00Z"), 86400000, {"close1"})
        self.assertEqual(len(before), 1)
        self.assertEqual(after, [])


class SignedOnlyRoomTests(unittest.TestCase):
    def test_all_missing_or_partial_signature_material_is_reported_in_mb_rooms(self):
        did = "did:key:z6Mk" + "a" * 44
        messages = {
            "UNSIGNED": {"from": "nick", "text": "x", "ts": ts(1)},
            "MATERIAL_ABSENT": {"from": did, "text": "x", "ts": ts(1)},           # did-shaped, no sig/nonce
            "MALFORMED": {"from": did, "text": "x", "ts": ts(1), "sig": "A" * 86},  # sig without nonce
        }
        for status, message in messages.items():
            record = rec("mb-p-deal", 1, message)
            self.assertEqual(record["sig_status"], status)
            record["generation"] = 1
            found = analysis.security_findings([record])
            self.assertEqual([f["category"] for f in found], ["SECURITY_FINDING_CANDIDATE"], status)
        plain = rec("lobby", 1, dict(messages["MATERIAL_ABSENT"]))
        plain["generation"] = 1
        self.assertEqual(analysis.security_findings([plain]), [])  # only signed-only rooms


class GapRecoveryTests(unittest.TestCase):
    def item(self, kind, start, end, stream="epoch-1"):
        return {"source_id": "s", "room": "lobby", "stream": stream, "kind": kind, "start_seq": start,
                "end_seq": end, "detail": {}, "ref": f"r{kind}{start}"}

    def gap(self, items):
        out = analysis.coverage_advisories({}, items, [], {"coverage": []}, {}, [], True)
        return next(a for a in out if a["kind"] == "GAP")

    def test_repeated_late_receipts_for_one_seq_do_not_complete_a_gap(self):
        gap = self.gap([self.item("GAP", 2, 3), self.item("LATE_OBSERVATION", 2, None),
                        self.item("LATE_OBSERVATION", 2, None)])
        self.assertEqual((gap["late_observed_seq"], gap["resolution"]), ([2], "PARTIALLY_RECOVERED"))
        done = self.gap([self.item("GAP", 2, 3), self.item("LATE_OBSERVATION", 2, None),
                         self.item("LATE_OBSERVATION", 3, None), self.item("LATE_OBSERVATION", 3, None)])
        self.assertEqual((done["late_observed_seq"], done["resolution"]), ([2, 3], "RECOVERED_BY_LATE_OBSERVATION"))
        other = self.gap([self.item("GAP", 2, 3), self.item("LATE_OBSERVATION", 2, None, stream="epoch-2")])
        self.assertEqual((other["late_observed_seq"], other["resolution"]), ([], "OPEN"))  # per stream


class RadarCaptureStateTests(unittest.TestCase):
    def state(self, statuses):
        cfg = {"lobby": {}}
        sources = {f"s{i}": {"status": st, "room": "lobby", "source_id": f"s{i}", "kind": "full-capture-archive"}
                   for i, st in enumerate(statuses)}
        row = analysis.radar([], cfg, sources, at("2026-09-02T00:00:00Z"), 86400000)[0]
        return row["capture_state"]

    def test_failed_or_mixed_unavailable_sources_are_never_reported_as_read(self):
        self.assertEqual(self.state(["READ"]), "READ")
        self.assertTrue(self.state(["FAILED"]).startswith("SOURCE_FAILED"))
        self.assertTrue(self.state(["UNAVAILABLE"]).startswith("SOURCE_UNAVAILABLE"))
        self.assertEqual(self.state(["READ", "UNAVAILABLE"]), "PARTIALLY_READ")
        self.assertEqual(self.state(["READ", "FAILED"]), "PARTIALLY_READ")
        self.assertEqual(self.state(["READ", "PARTIAL"]), "PARTIALLY_READ")


class AnomalyTests(unittest.TestCase):
    def test_baseline_insufficient_gives_no_finding(self):
        key_did = "did:key:z6Mk" + "a" * 44
        msgs = [{"ref": f"r{i}", "room": "lobby", "seq": i, "generation": 1, "sig_status": "VALID", "also_refs": [],
                 "message": {"from": key_did, "text": f"msg {i}", "ts": ts(i % 60, 1, 10)}} for i in range(40)]
        found, notes = analysis.anomaly_findings(msgs, {}, at("2026-09-01T12:00:00Z"), 86400000,
                                                 {"lobby": at("2026-09-01T10:00:00Z")})
        self.assertEqual(found, [])
        self.assertEqual(notes[0]["note"].split(":")[0], "BASELINE_INSUFFICIENT")

    def test_template_protocol_lines_are_not_duplicate_spam(self):
        msgs = [{"ref": f"r{i}", "room": "d-close1-price", "seq": i, "generation": 1, "sig_status": "UNSIGNED",
                 "also_refs": [], "message": {"from": "n", "text": json.dumps({"t": "price", "n": 1}), "ts": ts(i)}}
                for i in range(10)]
        found, _ = analysis.anomaly_findings(msgs, {}, at("2026-09-01T01:00:00Z"), 86400000, {})
        self.assertEqual(found, [])


class RenderAndBoundaryTests(unittest.TestCase):
    def test_safe_markdown(self):
        out = render.safe_md("<img src=x onerror=alert(1)> [x](https://t.example/r/lobby/say/hi) \x1b[31m‮")
        self.assertNotIn("<img", out)
        self.assertNotIn("https://", out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("‮", out)
        self.assertIn("hxxps", out)

    def test_redaction(self):
        synthetic_key = "sk-" + "ant-abcdefghijklmnopqrstu"
        text, kinds = render.redact_secrets(f"key {synthetic_key} and https://h/x?token=SECRET&a=1")
        self.assertNotIn("SECRET", text)
        self.assertNotIn("abcdefghijklmnop", text)
        self.assertEqual(sorted(set(kinds)), ["ANTHROPIC_OR_OPENAI_KEY", "URL_CREDENTIAL_PARAM"])

    def test_public_evidence_conflict_hides_source_and_unit_identifiers(self):
        source_id = "private-prod-host"
        unit = "segment-private/shard-000000000001.jsonl"
        finding = analysis.evidence_findings([], [{
            "source_id": source_id, "unit": unit,
            "previous_sha256": "a" * 64, "observed_sha256": "b" * 64,
        }], [], [])[0]
        internal, public = reports.report_candidate(finding, {}, {})
        self.assertIn(source_id, internal["target_and_impact"])
        self.assertIn(unit, internal["target_and_impact"])
        public_text = json.dumps(public)
        self.assertNotIn(source_id, public_text)
        self.assertNotIn(unit, public_text)
        self.assertTrue(public["target_and_impact"].startswith("evidence-integrity:"))

    def test_bundle_is_finite_digest_bound_and_redacted(self):
        items = [{"ref": f"r{i}", "room": "kibble", "record_sha256": "h", "sig_status": "UNSIGNED",
                  "message": {"text": "ghp_" + "a" * 30 + " please help", "ts": ts(1)}} for i in range(30)]
        bundle, record = semantic.build_bundle("opportunity_classification", items, {"interests": []},
                                               {"max_items": 5, "max_item_chars": 50, "max_input_chars": 1000})
        self.assertEqual(len(bundle["items"]), 5)
        self.assertNotIn("ghp_", json.dumps(bundle))
        self.assertEqual(bundle["limits"]["items_omitted"], 25)
        self.assertEqual(record["transformations"][0]["redactions"], ["GITHUB_TOKEN"])
        self.assertIn("untrusted observed data", bundle["instructions"])

    CONFIRMED = {"confirmed_by": "human", "confirmed_at": "2026-09-29", "auth_route": "subscription-login",
                 "extra_usage_disabled": True, "auto_reload_disabled": True}

    def test_validate_output_never_raises_on_wrong_types(self):
        bundle, _ = semantic.build_bundle("opportunity_classification", [
            {"ref": "r1", "room": "k", "record_sha256": "h", "sig_status": "VALID",
             "message": {"text": "hire me", "ts": ts(1)}}], {}, {})
        good = {"evidence_id": "r1", "category": "job", "relevance": "low", "rationale": "r"}
        for bad in ([], {}, 3, None, ["r1"], {"r1": 1}):
            for key in ("evidence_id", "category", "relevance", "rationale"):
                ok, report = semantic.validate_output(bundle, {"items": [dict(good, **{key: bad})]})
                self.assertEqual((ok, report["outcome"]), (False, "SCHEMA_MISMATCH"), (key, bad))
        self.assertEqual(semantic.validate_output(bundle, {"items": [dict(good, evidence_id="nope")]})[1]["outcome"],
                         "FABRICATED_REFERENCE")
        self.assertTrue(semantic.validate_output(bundle, {"items": [good]})[0])

    def test_claude_preflight_fails_closed(self):
        rt = semantic.ClaudeCodeCliRuntime({}, env={"PATH": "/usr/bin"})
        self.assertFalse(rt.preflight()["can_run"])
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_PROFILE", "CLAUDE_CODE_USE_BEDROCK"):
            rt = semantic.ClaudeCodeCliRuntime({"human_billing_confirmation": self.CONFIRMED,
                                                "auth_status_expectation": {"k": "v"}}, env={key: "sk-x"})
            pre = rt.preflight()
            self.assertFalse(pre["can_run"])
            self.assertNotIn("sk-x", json.dumps(pre))
        rt = semantic.ClaudeCodeCliRuntime({"human_billing_confirmation": self.CONFIRMED}, env={"PATH": "/usr/bin"})
        self.assertFalse(rt.preflight()["can_run"])  # no pinned auth-status expectation
        for field, bad in (("confirmed_by", ""), ("confirmed_by", "   "), ("confirmed_by", None),
                           ("confirmed_at", ""), ("confirmed_at", "   "), ("confirmed_at", None)):
            confirmation = dict(self.CONFIRMED)
            confirmation[field] = bad
            rt = semantic.ClaudeCodeCliRuntime({"human_billing_confirmation": confirmation,
                                                "auth_status_expectation": {"k": "v"}},
                                               env={"PATH": "/usr/bin"})
            self.assertFalse(rt.preflight()["can_run"], (field, bad))
        rt = semantic.ClaudeCodeCliRuntime({"human_billing_confirmation": self.CONFIRMED,
                                            "auth_status_expectation": {"k": "v"}}, env={"PATH": "/usr/bin"})
        pre = rt.preflight()
        self.assertTrue(pre["can_run"])
        self.assertEqual(pre["status"], "READY_PENDING_AUTH_PROBE")
        cmd = rt.command({"output_schema": semantic.OUTPUT_SCHEMA})
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        for flag in ("--safe-mode", "--restricted", "--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], "mcp__*")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(cmd[cmd.index("--permission-prompts") + 1], "none")
        for forbidden in ("--bare", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                          "--fallback-model"):
            self.assertNotIn(forbidden, cmd)
        with self.assertRaises(ValueError):
            semantic.runtime_for({"runtime": "openai"})

    def test_direct_run_cannot_bypass_gate(self):
        calls = []
        rt = semantic.ClaudeCodeCliRuntime({}, env={"PATH": "/usr/bin"}, runner=lambda *a, **k: calls.append(a))
        bundle, _ = semantic.build_bundle("opportunity_classification", [], {}, {})
        with mock.patch.object(semantic.subprocess, "Popen", side_effect=AssertionError("must not start")):
            self.assertEqual(rt.run(bundle)["outcome"], "BLOCKED_PREFLIGHT")
        self.assertEqual(calls, [])

    def test_cli_runtime_with_fake_executable(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "claude"
            fake.write_text(textwrap.dedent("""\
                #!/usr/bin/env python3
                import json, os, sys, time
                home = os.environ["HOME"]
                if sys.argv[1:4] == ["auth", "status", "--json"]:
                    print(json.dumps({"loginKind": open(os.path.join(home, "route")).read(), "email": "x@example"}))
                    sys.exit(0)
                open(os.path.join(home, "invoked"), "a").write("1")
                data = json.loads(sys.stdin.read())
                ids = [i["evidence_id"] for i in data["items"]]
                if os.path.exists(os.path.join(home, "sleep")):
                    time.sleep(30)
                if os.path.exists(os.path.join(home, "quota")):
                    sys.stderr.write("Claude usage limit reached"); sys.exit(1)
                out = {"items": [{"evidence_id": ids[0], "category": "job", "relevance": "low", "rationale": "r"}]}
                print(json.dumps({"type": "result", "is_error": False, "structured_output": out}))
                """))
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            home = Path(d) / "home"
            home.mkdir()
            (home / "route").write_text("api-key")
            bundle, _ = semantic.build_bundle("opportunity_classification", [
                {"ref": "r1", "room": "k", "record_sha256": "h", "sig_status": "VALID",
                 "message": {"text": "hire me", "ts": ts(1)}}], {}, {})
            rt = semantic.ClaudeCodeCliRuntime({"executable": str(fake), "timeout_seconds": 2,
                                                "human_billing_confirmation": self.CONFIRMED,
                                                "auth_status_expectation": {"loginKind": "subscription"}},
                                               env={"PATH": os.environ["PATH"], "HOME": str(home),
                                                    "ANTHROPIC_MODEL_NOT_A_CREDENTIAL": "x"})
            blocked = rt.run(bundle)
            self.assertEqual(blocked["outcome"], "AUTH_ROUTE_UNVERIFIED")
            self.assertEqual(blocked["probe"], {"probe": "MISMATCH", "mismatched_keys": ["loginKind"]})
            self.assertNotIn("example", json.dumps(blocked))  # probe values are never kept
            self.assertFalse((home / "invoked").exists())
            (home / "route").write_text("subscription")
            result = rt.run(bundle)
            self.assertEqual(result["outcome"], "COMPLETED")
            self.assertTrue(semantic.validate_output(bundle, result["output"])[0])
            (home / "quota").write_text("1")
            self.assertEqual(rt.run(bundle)["outcome"], "QUOTA_EXHAUSTED")
            (home / "quota").unlink()
            (home / "sleep").write_text("1")
            timed = rt.run(bundle)
            self.assertEqual(timed["outcome"], "TIMEOUT")
            self.assertFalse(timed["process_left"])

if __name__ == "__main__":
    unittest.main()
