from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
import hashlib
import http.client
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scout import __main__ as cli
from scout.ranking import classify, rank
from scout.report import render_html, save_report
from scout.source import MAX_BYTES, Observation, ScoutError, fetch_room, parse_room, room_path


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "technocore-demo.json"


def observation():
    raw = FIXTURE.read_bytes()
    return Observation("scout-demo", 200, "2026-09-14T01:00:00+00:00", raw,
                       parse_room(raw, "scout-demo", 200), "demo")


def report_for(obs):
    return {"mode": "demo", "created_at": obs.fetched_at,
            "sources": [obs.metadata()], **rank([obs])}


class SourceTests(unittest.TestCase):
    def test_observed_schema_and_evidence_hash(self):
        obs = observation()
        self.assertEqual(obs.data["count"], 7)
        self.assertEqual(obs.metadata()["sha256"], hashlib.sha256(obs.raw).hexdigest())

    def test_read_path_rejects_write_paths_and_injection_before_network(self):
        bad = ["../rooms", "foo/say/bot/hello", "foo%2fsay", "foo?x=y", "https://evil.test",
               "//evil.test", "x#y", "x\r\nHeader: x", "X", "", "a" * 49, "ルーム"]
        with patch("scout.source.http.client.HTTPSConnection") as connection:
            for room in bad:
                with self.subTest(room=room), self.assertRaises(ScoutError):
                    fetch_room(room)
            connection.assert_not_called()
        self.assertEqual(room_path("room-1_ok", 200), "/r/room-1_ok?format=json&limit=200")

    def test_invalid_limits(self):
        for limit in (True, 0, 201, -1, "100"):
            with self.subTest(limit=limit), self.assertRaises(ScoutError):
                room_path("technocore", limit)

    def test_schema_errors_fail_closed(self):
        original = observation().data
        mutations = [
            lambda d: d.update(room="other"), lambda d: d.update(count=True),
            lambda d: d.update(count=0), lambda d: d.update(last_seq=0),
            lambda d: d.update(first_seq=False), lambda d: d.update(generation=True),
            lambda d: d["messages"][0].update(seq=True),
            lambda d: d["messages"][1].update(seq=1),
            lambda d: d["messages"][0].update(text="x" * 4097),
            lambda d: d["messages"][0].update(text=["help wanted"]),
            lambda d: d["messages"][0].update(text="\ud800"),
            lambda d: d["messages"][0].update(ts="yesterday"),
            lambda d: d["messages"][0].update(ts="2026-09-14T00:00:00"),
            lambda d: d["messages"][0].pop("from"),
        ]
        for mutate in mutations:
            data = deepcopy(original)
            mutate(data)
            with self.subTest(data=data), self.assertRaises(ScoutError):
                parse_room(json.dumps(data).encode(), "scout-demo", 200)

    def test_invalid_json_and_resource_limits(self):
        bad = [b"\xff", b"not json", b"[]", b'{"room":"a","room":"b"}',
               b'{"number": NaN}', b"[" * 2000, b" " * (MAX_BYTES + 1)]
        for raw in bad:
            with self.subTest(length=len(raw)), self.assertRaises(ScoutError):
                parse_room(raw, "scout-demo", 200)
        with self.assertRaises(ScoutError):
            parse_room(observation().raw, "scout-demo", 1)

    def test_valid_empty_and_optional_fields(self):
        for first in ({}, {"first_seq": None, "generation": 0}):
            data = {"room": "empty", "count": 0, "last_seq": 0, "messages": [], **first}
            self.assertEqual(parse_room(json.dumps(data).encode(), "empty", 10), data)
        data = observation().data
        data["messages"][0]["sig"] = "unverified-extra-data"
        self.assertEqual(parse_room(json.dumps(data).encode(), "scout-demo", 200), data)

    def make_connection(self, status=200, headers=None, raw=None):
        response = Mock()
        response.status = status
        response.isclosed.return_value = False
        response.getheader.side_effect = lambda name, default=None: (
            headers if headers is not None else {"Content-Type": "application/json; charset=utf-8"}
        ).get(name, default)
        payload = observation().raw if raw is None else raw
        response.read1.side_effect = io.BytesIO(payload).read
        connection = Mock()
        connection.getresponse.return_value = response
        return connection, response

    def test_only_fixed_https_get_no_auth_and_bounded_read(self):
        connection, response = self.make_connection()
        with patch("scout.source.http.client.HTTPSConnection", return_value=connection) as factory:
            obs = fetch_room("scout-demo", 200)
        factory.assert_called_once_with("technocore.chat", timeout=15)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("GET", "/r/scout-demo?format=json&limit=200"))
        self.assertEqual(set(kwargs), {"headers"})
        self.assertEqual(set(kwargs["headers"]), {"Accept", "Accept-Encoding", "User-Agent"})
        self.assertTrue(all(0 < call.args[0] <= 65536 for call in response.read1.call_args_list))
        connection.close.assert_called_once()
        self.assertEqual(obs.mode, "live")

    def test_http_errors_and_redirects_never_follow_or_retry(self):
        for status in (301, 302, 307, 308, 401, 403, 404, 429, 500):
            connection, response = self.make_connection(status, {
                "Location": "https://technocore.chat/r/foo/say/bot/forbidden",
                "Retry-After": "60", "Content-Type": "application/json"})
            with self.subTest(status=status), patch("scout.source.http.client.HTTPSConnection", return_value=connection):
                with self.assertRaisesRegex(ScoutError, f"HTTP {status}"):
                    fetch_room("scout-demo")
                connection.request.assert_called_once()
                response.read1.assert_not_called()
                connection.close.assert_called_once()

    def test_response_headers_size_and_truncation(self):
        for headers, raw in [
            ({"Content-Type": "text/html"}, b"<html>"),
            ({"Content-Type": "application/json", "Content-Encoding": "gzip"}, b"x"),
            ({"Content-Type": "application/json", "Content-Length": str(MAX_BYTES + 1)}, b"x"),
            ({"Content-Type": "application/json", "Content-Length": "invalid"}, b"x"),
            ({"Content-Type": "application/json", "Content-Length": "200"}, b"x"),
            ({"Content-Type": "application/json"}, b" " * (MAX_BYTES + 1)),
        ]:
            connection, _ = self.make_connection(headers=headers, raw=raw)
            with self.subTest(headers=headers), patch("scout.source.http.client.HTTPSConnection", return_value=connection):
                with self.assertRaises(ScoutError):
                    fetch_room("scout-demo")
                connection.close.assert_called_once()

    def test_timeout_and_dns_errors_close_connection(self):
        for error in (TimeoutError(), OSError("do not echo sensitive error details")):
            connection, _ = self.make_connection()
            connection.request.side_effect = error
            with patch("scout.source.http.client.HTTPSConnection", return_value=connection):
                with self.assertRaises(ScoutError) as caught:
                    fetch_room("scout-demo")
            self.assertNotIn("sensitive", str(caught.exception))
            connection.close.assert_called_once()

    def test_slow_body_deadline(self):
        connection, _ = self.make_connection()
        with patch("scout.source.http.client.HTTPSConnection", return_value=connection), patch("scout.source.time.monotonic", side_effect=[0, 1, 31]):
            with self.assertRaisesRegex(ScoutError, "30秒"):
                fetch_room("scout-demo")
        connection.close.assert_called_once()

    def test_real_http_response_with_connection_close(self):
        raw = observation().raw

        class Socket:
            def makefile(self, *args):
                return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                                  b'Connection: close\r\nContent-Length: '
                                  + str(len(raw)).encode() + b'\r\n\r\n' + raw)

        response = http.client.HTTPResponse(Socket())
        response.begin()
        connection = Mock()
        connection.getresponse.return_value = response
        with patch("scout.source.http.client.HTTPSConnection", return_value=connection):
            result = fetch_room("scout-demo")
        self.assertEqual(result.raw, raw)
        self.assertTrue(response.isclosed())
        self.assertGreaterEqual(connection.sock.settimeout.call_count, 1)


class RankingTests(unittest.TestCase):
    def test_all_categories_have_grounded_reasons(self):
        result = rank([observation()])
        self.assertEqual(result["messages_scanned"], 7)
        self.assertEqual(result["candidates_total"], 5)
        categories = {r["category"] for c in result["candidates"] for r in c["reasons"]}
        self.assertEqual(categories, {"help_request", "collaboration", "contribution", "useful_agent", "project_activity"})
        for candidate in result["candidates"]:
            self.assertFalse(candidate["identity_verified"])
            for reason in candidate["reasons"]:
                self.assertIn(reason["matched_text"], candidate["text"])
        self.assertEqual(result["candidates"][0]["author_claim"], "demo-jp")

    def test_deduplicate_without_score_inflation_and_preserve_evidence(self):
        candidates = rank([observation()])["candidates"]
        builder = next(c for c in candidates if c["author_claim"] == "demo-builder")
        self.assertEqual([e["seq"] for e in builder["evidence"]], [2, 3])
        self.assertEqual(builder["score"], 7)
        self.assertEqual(builder["evidence"][1]["json_pointer"], "/messages/2")

    def test_keyword_top_empty_and_low_signal(self):
        self.assertEqual(rank([observation()], ["NONEXISTENT"])["candidates_total"], 0)
        self.assertEqual(rank([observation()], ["API"])["candidates_total"], 2)
        limited = rank([observation()], top=1)
        self.assertEqual(limited["candidates_total"], 5)
        self.assertEqual(limited["candidates_shown"], 1)
        self.assertEqual(classify("heartbeat online ping faucet airdrop"), [])
        self.assertEqual(classify("agent online"), [])

    def test_external_instructions_are_only_data(self):
        with patch("subprocess.run") as run, patch("scout.source.http.client.HTTPSConnection") as network:
            result = rank([observation()])
        attack = next(c for c in result["candidates"] if c["author_claim"] == "demo-untrusted")
        self.assertTrue(attack["instruction_like_text"])
        self.assertIn("sudo", attack["text"])
        run.assert_not_called()
        network.assert_not_called()


class ReportTests(unittest.TestCase):
    def test_html_does_not_activate_external_content(self):
        obs = replace(observation(), mode="live")
        report = report_for(obs)
        report["candidates"][0]["author_claim"] = '<img src="https://evil.test" onerror="alert(1)">\x1b\u202e'
        rendered = render_html(report)
        tags = []

        class Tags(HTMLParser):
            def handle_starttag(self, tag, attrs):
                tags.append((tag, dict(attrs)))

        Tags().feed(rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("\\u001b\\u202e", rendered)
        self.assertFalse(any(tag in ("script", "img", "iframe", "form", "style") for tag, _ in tags))
        links = [attrs["href"] for tag, attrs in tags if tag == "a"]
        self.assertEqual(links, [obs.metadata()["source_url"]])
        self.assertIn("default-src 'none'", rendered)

    def test_demo_has_no_live_source_link(self):
        obs = observation()
        self.assertIsNone(obs.metadata()["source_url"])
        self.assertNotIn('<a href=', render_html(report_for(obs)))

    def test_new_runs_preserve_bytes_and_existing_data(self):
        obs = observation()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            root.mkdir()
            existing = root / "keep.txt"
            existing.write_text("existing data")
            first = save_report(report_for(obs), [obs], root)
            second = save_report(report_for(obs), [obs], root)
            self.assertNotEqual(first, second)
            self.assertEqual(existing.read_text(), "existing data")
            self.assertEqual((first / "source-scout-demo.json").read_bytes(), obs.raw)
            self.assertTrue((first / "COMPLETE").is_file())
            self.assertEqual(json.loads((first / "report.json").read_text())["candidates_total"], 5)

    def test_refuses_symlink_output(self):
        obs = observation()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "target").mkdir()
            (root / "data").symlink_to(root / "target", target_is_directory=True)
            with self.assertRaises(ScoutError):
                save_report(report_for(obs), [obs], root / "data")
            self.assertEqual(list((root / "target").iterdir()), [])

    def test_write_failure_has_no_complete_marker(self):
        obs = observation()
        with tempfile.TemporaryDirectory() as directory:
            with patch("scout.report.render_html", side_effect=OSError("disk full")):
                with self.assertRaises(ScoutError):
                    save_report(report_for(obs), [obs], Path(directory))
            self.assertEqual(list(Path(directory).glob("*/COMPLETE")), [])


class CliTests(unittest.TestCase):
    def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_demo_end_to_end_without_network(self):
        with tempfile.TemporaryDirectory(prefix="scout test ") as directory:
            root = Path(directory)
            (root / "fixtures").mkdir()
            (root / "fixtures" / FIXTURE.name).write_bytes(FIXTURE.read_bytes())
            with patch.object(cli, "ROOT", root), patch.object(cli, "fetch_room") as fetch:
                code, stdout, stderr = self.run_cli(["--demo"])
            self.assertEqual(code, 0, stderr)
            self.assertIn("観測7件 / 候補5件", stdout)
            fetch.assert_not_called()
            self.assertEqual(len(list((root / "data").glob("*/COMPLETE"))), 1)

    def test_validation_precedes_network(self):
        cases = [["--room", "../write"], ["--demo", "--room", "technocore"],
                 ["--keyword", " "], sum((["--room", f"room{i}"] for i in range(6)), [])]
        for args in cases:
            with self.subTest(args=args), patch.object(cli, "fetch_room") as fetch, patch.object(cli, "save_report") as save:
                code, _, _ = self.run_cli(args)
                self.assertEqual(code, 1)
                fetch.assert_not_called()
                save.assert_not_called()

    def test_partial_fetch_failure_produces_no_success(self):
        with patch.object(cli, "fetch_room", side_effect=[observation(), ScoutError("HTTP 429")]), patch.object(cli, "save_report") as save:
            code, stdout, stderr = self.run_cli(["--room", "one", "--room", "two"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("429", stderr)
        save.assert_not_called()

    def test_duplicate_rooms_are_read_once(self):
        with patch.object(cli, "fetch_room", return_value=observation()) as fetch, patch.object(cli, "save_report", return_value=Path("data/example")):
            code, _, stderr = self.run_cli(["--room", "technocore", "--room", "technocore"])
        self.assertEqual(code, 0, stderr)
        fetch.assert_called_once_with("technocore", 100)

    def test_interrupt_returns_130(self):
        with patch.object(cli, "fetch_room", side_effect=KeyboardInterrupt):
            code, stdout, stderr = self.run_cli([])
        self.assertEqual(code, 130)
        self.assertEqual(stdout, "")
        self.assertIn("中断", stderr)


if __name__ == "__main__":
    unittest.main()
