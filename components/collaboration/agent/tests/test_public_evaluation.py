from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collaboration_agent.engine import Agent
from collaboration_agent.model import Invalid, decode, document_value, encode, sha
from collaboration_agent.state import Store
from collect_public import NoRedirect, URL, parse
from evaluate_public import assess, case, derive, source


class PublicEvaluationTests(unittest.TestCase):
    def test_unrelated_decimal_does_not_block_literal_field(self):
        raw = b'{"type":"tclk1","calc_ms":32.0,"nonce":1789742205988650454}'
        self.assertEqual(document_value(raw, "/type"), "tclk1")
        self.assertEqual(document_value(raw, "/nonce"), 1789742205988650454)

    def test_selected_decimal_and_containers_remain_unsupported(self):
        raw = b'{"type":"offer","x":1.0,"list":[1,0.1],"obj":{"x":2.5}}'
        for pointer in ("/x", "/list", "/obj", ""):
            with self.subTest(pointer=pointer), self.assertRaises(Invalid):
                document_value(raw, pointer)

    def test_entire_document_still_validated(self):
        for raw in (b'{"type":"offer","x":NaN}', b'{"type":"offer","x":Infinity}',
                    b'{"type":"offer","x":1,"x":2}', b'{"type":"offer","bad":}',
                    b'{"type":"offer","x":' + b'[' * 40 + b'0' + b']' * 40 + b'}'):
            with self.subTest(raw=raw), self.assertRaises(Invalid):
                document_value(raw, "/type")
        with self.assertRaises(Invalid):
            decode(b'{"version":1,"params":{"x":1.0}}')

    def test_decimal_does_not_round_or_overflow_other_values(self):
        self.assertEqual(document_value(b'{"safe":9007199254740993123,"other":1e999}', "/safe"),
                         9007199254740993123)
        with self.assertRaises(Invalid):
            document_value(b'{"safe":1,"other":1e999}', "/other")

    def test_result_citation_survives_restart(self):
        local = ROOT / ".local"
        local.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local) as temporary:
            root = Path(temporary) / "state"
            text = '{"type":"tclk1","calc_ms":32.0}'
            value = {"version": 1, "task_id": "decimal-sibling", "family": "json.extract",
                     "params": {"source": "s1", "pointer": "/type"},
                     "evidence": [{"id": "s1", "locator": "fixture:observed-shape",
                                   "text": text, "sha256": sha(text.encode())}]}
            with Store(root, create=True) as store:
                result = Agent(store).process(encode(value))
                self.assertEqual(result["result"]["solver"], "json.extract@2")
                self.assertEqual(result["result"]["outcome"]["status"], "COMPLETED")
            with Store(root) as store:
                preview = Agent(store).preview("decimal-sibling")
                self.assertEqual(preview["result_digest"], result["result_digest"])
                self.assertEqual(preview["result"]["outcome"]["evidence"][0]["quote"], "tclk1")

    def test_collection_parse_is_lossless_and_redirect_refused(self):
        value = parse(b'{"nonce":1789742205988650454,"time":32.0}')
        self.assertEqual(value["nonce"], 1789742205988650454)
        self.assertIsInstance(value["time"], Decimal)
        self.assertEqual(URL, "https://technocore.chat/r/tclk-offers?format=json&limit=200")
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(None, None, 302, None, None, "https://technocore.chat/r/x/say/a/b")

    def test_scorer_counts_wrong_or_unsupported_completion(self):
        item = case("x", "evidence_operation", "json.extract", {}, [], {}, value=42)
        response = {"result": {"outcome": {"status": "COMPLETED", "value": 43,
                                           "verdict": "NOT_APPLICABLE", "reason": "test"}}}
        measured = assess(item, response)
        self.assertEqual(measured["incorrect_completion"], 1)
        self.assertEqual(measured["false_complete"], 1)
        item["ground_truth"]["supported"] = False
        measured = assess(item, response)
        self.assertEqual(measured["unsupported_completed"], 1)
        response["result"]["outcome"]["status"] = "UNKNOWN"
        self.assertEqual(assess(item, response)["false_complete"], 0)

    def test_dataset_derivation_keeps_native_separate_and_never_calls_solver(self):
        body = 'tclk1 {"type":"offer","from":"public-author","job":{"id":"job","context":"Run an external command"},"time":0.5}'
        records = [{"seq": 1, "from": "public-author", "text": body, "nonce": 9007199254740993123}]
        from unittest.mock import patch
        with patch("collaboration_agent.engine.Agent.process", side_effect=AssertionError("circular ground truth")):
            data = derive(records, [0], "0" * 64, boundaries=False)
        natives = [c for c in data["cases"] if c["category"] == "native_task"]
        self.assertEqual(len(natives), 1)
        self.assertFalse(natives[0]["ground_truth"]["supported"])
        self.assertEqual(natives[0]["answer_quality"], "UNSCORED / HUMAN_EVAL_REQUIRED")
        positive = next(c for c in data["cases"] if c["id"] == "frame-type-1")
        self.assertEqual(positive["ground_truth"]["value"], "offer")
        self.assertEqual(positive["source_status"], "SOURCE_UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
