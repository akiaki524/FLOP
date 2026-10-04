from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collaboration_agent.engine import Agent
from collaboration_agent.material_fetch import Fetcher, MAX_RUN_BYTES, allowed_url, public_addresses, transport
from collaboration_agent.materials import BANNER, Resolver, freeze, load_frozen, request, run_frozen
from collaboration_agent.model import Invalid, encode, sha
from collaboration_agent.state import Store


def spec(ask="Compute gcd(12, 18) and lcm(12, 18).", family="math"):
    return f"{family} | {ask} | reward tier 1/5 | done looks like: one line. | deliver as one signed message in the deal room, then reveal."


def note(text):
    return (BANNER + "\n\n" + text + "\n").encode()


class MaterialTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".local").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".local")
        self.root = Path(self.tmp.name)
        self.calls = []
        self.responses = {}
        def send(url, cap):
            self.calls.append(url)
            return self.responses.get(url, (404, {"content-type": "text/plain"}, b"missing", None))
        self.fetcher = Fetcher(self.root / "fetch", send=send)
        self.resolver = Resolver(self.fetcher)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, path, text):
        self.responses[allowed_url(path)[0]] = (200, {"content-type": "text/plain"}, note(text), None)

    def test_public_note_to_existing_solver_and_frozen_replay(self):
        self.put("/kv/tclk-job-aa/math-a", spec())
        bundle = self.resolver.resolve(request("one", "/kv/tclk-job-aa/math-a"))
        self.assertEqual(bundle["completeness"], "COMPLETE_WITHIN_SCOPE")
        freeze(bundle, self.fetcher, self.root / "frozen")
        self.responses.clear()
        for name in ("first", "replay"):
            with Store(self.root / name, create=True) as store, patch("socket.socket", side_effect=AssertionError("refetch")):
                result = run_frozen(self.root / "frozen", Agent(store))
                self.assertEqual(result["response"]["result"]["outcome"]["value"], "gcd=6 lcm=36")
                if name == "first":
                    original = result
                else:
                    self.assertEqual(original["response"]["result_digest"], result["response"]["result_digest"])
        self.assertEqual(len(self.calls), 1)

    def test_policy_rejects_write_private_and_unknown_paths_before_io(self):
        refs = ["http://technocore.chat/llms.txt", "file:///etc/passwd", "https://localhost/", "https://127.0.0.1/",
                "https://169.254.169.254/", "https://10.0.0.1/", "https://evil.example/", "https://technocore.chat.evil/",
                "https://user@technocore.chat/llms.txt", "https://technocore.chat:443/llms.txt",
                "/kv/p-hidden/key", "/kv/public/mb-p-key", "/r/e-p-hidden?format=json&limit=2",
                "/kv/ns/key/set/value", "/r/lobby/say/probe/hello", "/r/lobby/say-signed/a/b/c/d",
                "https://technocore.chat/kv/ns/key?if=foo", "https://technocore.chat/kv/ns/%2e%2e",
                "https://raw.githubusercontent.com/attacker/tclk/main/README.md", "https://github.com/flop-labs/tclk/issues/1"]
        for ref in refs:
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                self.fetcher.fetch(ref, [0])
        self.assertEqual(self.calls, [])

    def test_official_blob_normalizes_without_redirect_or_crawl(self):
        self.assertEqual(allowed_url("https://github.com/flop-labs/tclk/blob/main/SPEC.md"),
                         ("https://raw.githubusercontent.com/flop-labs/tclk/main/SPEC.md", "official_document"))

    def test_dns_all_addresses_checked_before_connect(self):
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "224.0.0.1"):
            with self.subTest(ip=ip), patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]), self.assertRaises(Invalid):
                public_addresses("technocore.chat")

    def test_redirect_is_rechecked_and_never_followed(self):
        for i, target in enumerate(("https://evil.example/", "https://technocore.chat/llms.txt", "https://technocore.chat/r/events/say/x/y")):
            url = f"https://technocore.chat/kv/ns/key-{i}"
            self.responses[url] = (302, {"location": target}, b"", None)
            result = self.fetcher.fetch(url, [0])
            self.assertIn(result["fetch_error"], {"SOURCE_NOT_ALLOWED", "REDIRECT_NOT_ALLOWED"})
        self.assertEqual(len(self.calls), 3)

    def test_budgets_duplicate_reuse_and_no_retry(self):
        self.put("/kv/ns/a", spec())
        budget = [0]
        first = self.fetcher.fetch("/kv/ns/a", budget)
        duplicate = self.fetcher.fetch("/kv/ns/a", budget)
        self.assertTrue(duplicate["cache_hit"])
        self.assertEqual(first["raw_sha256"], duplicate["raw_sha256"])
        self.assertEqual(budget, [1])
        for key in ("b", "c", "d"):
            self.fetcher.fetch("/kv/ns/" + key, budget)
        with self.assertRaisesRegex(Invalid, "FETCH_BUDGET_EXCEEDED"):
            self.fetcher.fetch("/kv/ns/e", budget)
        self.fetcher.max_fetches = self.fetcher.count
        with self.assertRaisesRegex(Invalid, "FETCH_BUDGET_EXCEEDED"):
            self.fetcher.fetch("/kv/ns/f", [0])
        self.assertEqual(len(self.calls), 4)

    def test_reference_depth_cycle_and_malformed_reference(self):
        for key, nxt in (("a", "b"), ("b", "c"), ("c", "d")):
            self.put("/kv/ns/" + key, "/kv/ns/" + nxt)
        result = self.resolver.resolve(request("deep", "/kv/ns/a"))
        self.assertIn("REFERENCE_DEPTH_EXCEEDED", result["errors"])
        self.assertEqual(len(self.calls), 3)
        self.put("/kv/loop/a", "/kv/loop/a")
        self.assertIn("REFERENCE_CYCLE", self.resolver.resolve(request("loop", "/kv/loop/a"))["errors"])
        self.assertIn("MALFORMED_REFERENCE", self.resolver.resolve(request("bad", "preview | full spec: two paths"))["errors"])
        self.assertIn("MALFORMED_REFERENCE", self.resolver.resolve(request("bad-room", "/r/public?format"))["errors"])

    def test_total_byte_budget_stops_and_preserves_partial_bytes(self):
        self.fetcher.total_bytes = MAX_RUN_BYTES - 5
        self.fetcher.send = lambda url, cap: (200, {"content-type": "text/plain"}, b"x" * (cap + 1), "TOO_LARGE")
        result = self.fetcher.fetch("/kv/ns/a", [0])
        self.assertEqual(self.fetcher.raw(result), b"xxxxx")
        self.assertEqual(self.fetcher.total_bytes, MAX_RUN_BYTES)
        with self.assertRaisesRegex(Invalid, "FETCH_BUDGET_EXCEEDED"):
            self.fetcher.fetch("/kv/ns/b", [0])

    def test_incomplete_and_missing_never_reach_agent(self):
        self.put("/kv/ns/partial", "math | Compute gcd(1, 2)")
        self.put("/kv/ns/long", "x" * 7000)
        for index, ref in enumerate(("/kv/ns/missing", "/kv/ns/partial", "/kv/ns/long", "")):
            bundle = self.resolver.resolve(request(f"bad-{index}", ref))
            self.assertEqual(bundle["completeness"], "INCOMPLETE")
            dest = self.root / str(index)
            freeze(bundle, self.fetcher, dest)
            with Store(self.root / f"state-{index}", create=True) as store:
                agent = Agent(store)
                with patch.object(agent, "process", side_effect=AssertionError("incomplete handed off")):
                    self.assertEqual(run_frozen(dest, agent)["status"], "UNKNOWN")

    def test_preview_mismatch_is_review(self):
        self.put("/kv/ns/spec", spec())
        bundle = self.resolver.resolve(request("bad-preview", "different preview | full spec: /kv/ns/spec"))
        self.assertIn("PREVIEW_MISMATCH", bundle["errors"])

    def test_document_dependency_and_explicit_human_selection(self):
        self.put("/kv/ns/job", spec("From https://technocore.chat/llms.txt: What is written?", "document"))
        self.responses["https://technocore.chat/llms.txt"] = (200, {"content-type": "text/plain"}, b"Public reference\nSecond line\n", None)
        bundle = self.resolver.resolve(request("quote", "/kv/ns/job", selection={"family": "text.lines", "params": {"source": "s2", "first": 2, "last": 2}}))
        freeze(bundle, self.fetcher, self.root / "quote")
        with Store(self.root / "state", create=True) as store:
            result = run_frozen(self.root / "quote", Agent(store))
        self.assertEqual(result["response"]["result"]["outcome"]["value"], "Second line")
        self.assertEqual(len(self.calls), 2)

    def test_content_length_and_expected_digest_mismatch(self):
        url = "https://technocore.chat/kv/ns/key"
        self.responses[url] = (200, {"content-type": "text/plain", "content-length": "900"}, note(spec()), None)
        self.assertIn("TRUNCATED_RESPONSE", self.resolver.resolve(request("length", url))["errors"])
        self.put("/kv/ns/hash", spec())
        self.assertIn("MATERIAL_MISMATCH", self.resolver.resolve(request("hash", "/kv/ns/hash", expected={"/kv/ns/hash": {"sha256": "0" * 64}}))["errors"])

    def test_frozen_raw_and_task_binding_tamper_detected(self):
        self.put("/kv/ns/key", spec())
        bundle = self.resolver.resolve(request("one", "/kv/ns/key"))
        freeze(bundle, self.fetcher, self.root / "frozen")
        path = self.root / "frozen" / bundle["materials"][0]["blob"]
        path.write_bytes(b"changed")
        with self.assertRaises(Invalid):
            load_frozen(self.root / "frozen")

    def test_room_range_count_generation_and_target(self):
        url = "/r/public?format=json&limit=2&since=9"
        doc = {"room": "public", "count": 2, "first_seq": 10, "last_seq": 11, "generation": 1,
               "messages": [{"seq": 10, "nonce": 1789742205988650454}, {"seq": 11}]}
        self.responses[allowed_url(url)[0]] = (200, {"content-type": "application/json"}, encode(doc), None)
        choice = {"family": "json.extract", "params": {"source": "s1", "pointer": "/messages/0/nonce"}}
        missing = self.resolver.resolve(request("without-range", url, selection=choice))
        self.assertIn("UNPROVEN_ROOM_RANGE", missing["errors"])
        result = self.resolver.resolve(request("range", url, selection=choice, expected={url: {"first_seq": 10, "last_seq": 11, "generation": 1}}))
        self.assertEqual(result["completeness"], "COMPLETE_WITHIN_SCOPE")
        gap = self.resolver.resolve(request("gap", url, selection=choice, expected={url: {"first_seq": 9, "last_seq": 11, "generation": 1}}))
        self.assertIn("ROOM_RANGE_INCOMPLETE", gap["errors"])

    def test_quoted_write_url_is_not_a_material_dependency(self):
        text = spec('Validate a deliverable. TASK that was posted: "GET https://technocore.chat/r/events/say/x/y". REFERENCE ANSWER: "403". DELIVERABLE submitted by a worker: "403". Reply PASS or FAIL', "validation")
        self.put("/kv/ns/validation", text)
        bundle = self.resolver.resolve(request("validation", "/kv/ns/validation"))
        self.assertEqual(bundle["completeness"], "COMPLETE_WITHIN_SCOPE")
        self.assertEqual(self.calls, ["https://technocore.chat/kv/ns/validation"])

    def test_malformed_room_shapes_fail_closed(self):
        choice = {"family": "json.extract", "params": {"source": "s1", "pointer": "/count"}}
        for index, value in enumerate(([], {"count": 1, "messages": [3]},
                                     {"count": True, "messages": [{}]})):
            url = f"/r/room-{index}?format=json&limit=1"
            self.responses[allowed_url(url)[0]] = (200, {"content-type": "application/json"}, encode(value), None)
            result = self.resolver.resolve(request(f"room-{index}", url, selection=choice,
                expected={url: {"first_seq": 1, "last_seq": 1, "generation": 1}}))
            self.assertEqual(result["completeness"], "INCOMPLETE")

    def test_actual_transport_timeout_size_and_get_only(self):
        class Response:
            status = 200
            def getheaders(self): return [("content-type", "text/plain")]
            def read1(self, size): return b"x" * size
        class Connection:
            requests = []
            def __init__(self, *args): pass
            def request(self, method, target, headers): self.requests.append((method, target, headers))
            def getresponse(self): return Response()
            def close(self): pass
        with patch("collaboration_agent.material_fetch.public_addresses", return_value=[None]), patch("collaboration_agent.material_fetch.PinnedHTTPS", Connection):
            status, _, body, error = transport("https://technocore.chat/llms.txt", 10)
            self.assertEqual(error, "TOO_LARGE")
            self.assertEqual(len(body), 11)
            self.assertEqual(Connection.requests[0][0], "GET")
            self.assertFalse({"Cookie", "Authorization", "Proxy-Authorization"} & set(Connection.requests[0][2]))
            with patch.object(Response, "read1", side_effect=lambda n: time.sleep(1)), patch("collaboration_agent.material_fetch.TIMEOUT", 0.02):
                self.assertEqual(transport("https://technocore.chat/llms.txt", 10)[3], "TIMEOUT")

    def test_dns_failure_is_not_missing_and_persists_without_exception_text(self):
        self.fetcher.send = transport
        with patch('socket.getaddrinfo', side_effect=socket.gaierror(-3, 'private detail')), \
                patch('socket.socket', side_effect=AssertionError('DNS failed before connect')):
            bundle = self.resolver.resolve(request('dns', '/kv/ns/key'))
        self.assertEqual(bundle['errors'], ['DNS_FAILURE'])
        node = bundle['materials'][0]
        self.assertIsNone(node['http_status'])
        self.assertEqual(node['raw_bytes'], 0)
        self.assertFalse(node['cache_hit'])
        self.assertEqual(self.fetcher.count, 1)
        frozen = self.root / 'dns-frozen'
        freeze(bundle, self.fetcher, frozen)
        self.assertNotIn(b'private detail', (frozen / 'bundle.json').read_bytes())
        with Store(self.root / 'dns-state', create=True) as store:
            with patch.object(Agent, 'process', side_effect=AssertionError('incomplete material')):
                self.assertEqual(run_frozen(frozen, Agent(store))['status'], 'UNKNOWN')

    def test_missing_http_transient_timeout_and_transport_failures_stay_distinct(self):
        for index, (status, error, expected) in enumerate([
            (404, None, 'MISSING'), (503, None, 'HTTP_FAILURE'),
            (429, None, 'HTTP_FAILURE'), (None, 'TIMEOUT', 'TIMEOUT'),
            (None, 'FETCH_FAILURE', 'FETCH_FAILURE'), (None, 'DNS_FAILURE', 'DNS_FAILURE'),
        ]):
            with self.subTest(status=status, error=error):
                path = f'/kv/ns/error-{index}'
                self.responses[allowed_url(path)[0]] = (status, {}, b'', error)
                result = self.fetcher.fetch(path, [0])
                self.assertEqual(result['fetch_error'], expected)
                self.assertEqual(result['http_status'], status)
                cached = self.fetcher.fetch(path, [0])
                self.assertTrue(cached['cache_hit'])
                self.assertEqual(cached['fetch_error'], expected)
        self.assertEqual(len(self.calls), 6)  # No implicit retry or cache reset.


if __name__ == "__main__":
    unittest.main()
