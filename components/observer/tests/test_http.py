"""Loopback-only HTTP integration. Production has no endpoint override option."""

import contextlib
import http.server
import socket
import threading
import time
import unittest
import urllib.parse
import urllib.request
from unittest.mock import patch

from test_observer import envelope, reply
from technocore_observer.http import NoRedirect, SafeClient
from technocore_observer.protocol import MAX_BODY, decode_reply, validate_envelope


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append(self.path)
        item = self.server.reply
        if item == "reset":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if item == "timeout":
            time.sleep(0.15)
            return
        self.send_response(item.status)
        self.send_header("Content-Type", item.content_type)
        if 300 <= item.status < 400:
            self.send_header("Location", "/forbidden-write-lane")
        if item.retry_after:
            self.send_header("Retry-After", item.retry_after)
        self.end_headers()
        try:
            self.wfile.write(item.body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.server.requests = []
        cls.server.reply = reply(envelope([101]))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.requests.clear()
        self.server.reply = reply(envelope([101]))
        # Only the test module can replace the literal production origin.
        self.origin = patch("technocore_observer.http.ORIGIN", f"http://127.0.0.1:{self.server.server_port}")
        self.origin.start()
        self.addCleanup(self.origin.stop)

    def test_query_and_proxy_disabled(self):
        with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1",
                                       "NO_PROXY": "", "no_proxy": ""}):
            client = SafeClient("test-room")
            result = client.poll(100)
        self.assertEqual(validate_envelope(decode_reply(result), "test-room")["last_seq"], 101)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.server.requests[0]).query)
        self.assertEqual({key: value for key, value in query.items() if key != "n"},
                         {"since": ["100"], "limit": ["200"], "wait": ["10"], "format": ["json"]})
        self.assertTrue(query["n"][0].isdecimal())
        client.poll(101)
        self.assertNotEqual(self.server.requests[0], self.server.requests[1])

    def test_redirects_are_never_followed_and_statuses_preserved(self):
        for status in (301, 302, 303, 307, 308, 400, 404, 429, 500, 503):
            with self.subTest(status=status):
                self.server.requests.clear()
                self.server.reply = reply(status=status, retry_after="30")
                result = SafeClient("test-room").poll(100)
                self.assertEqual(result.status, status)
                self.assertEqual(len(self.server.requests), 1)
                self.assertNotIn("forbidden", self.server.requests[0])
                self.assertEqual(result.retry_after, "30")

    def test_actual_body_limit(self):
        from technocore_observer.protocol import Reply
        self.server.reply = Reply(200, "application/json", b"x" * (MAX_BODY + 1))
        result = SafeClient("test-room").poll(100)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.body), MAX_BODY)

    def test_timeout_and_reset(self):
        for behavior in ("timeout", "reset"):
            with self.subTest(behavior=behavior):
                self.server.reply = behavior
                client = SafeClient("test-room")
                client.timeout = 0.02  # Test-only acceleration; CLI cannot set this.
                with self.assertRaises((OSError, __import__("http.client").client.HTTPException)):
                    client.poll(100)

    def test_tls_verification_and_no_proxy_handler(self):
        client = SafeClient("test-room")
        https = next(h for h in client._opener.handlers if isinstance(h, urllib.request.HTTPSHandler))
        import ssl
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(https._context.check_hostname)
        self.assertTrue(any(isinstance(h, NoRedirect) for h in client._opener.handlers))
        self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in client._opener.handlers))


if __name__ == "__main__":
    unittest.main()
