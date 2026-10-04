"""HTTP framing tests use stdlib HTTPResponse over bytes; no socket/network."""
import http.client
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from collaboration_agent.material_fetch import transport
from collaboration_agent.real_nonce import observe_project_nonce
from collaboration_agent.model import Invalid


class NonceExportFramingTests(unittest.TestCase):
    def observe(self, payload):
        class Sock:
            def makefile(self, *_):
                return io.BytesIO(payload)

        response = http.client.HTTPResponse(Sock())
        response.begin()
        with patch('collaboration_agent.material_fetch.public_addresses', return_value=['192.0.2.1']), \
             patch('collaboration_agent.material_fetch.PinnedHTTPS') as connection:
            connection.return_value.getresponse.return_value = response
            try:
                return observe_project_nonce(clock_ms=lambda: 123)
            finally:
                connection.return_value.request.assert_called_once()
                args = connection.return_value.request.call_args
                self.assertEqual(args.args, ('GET', '/r/tclk-offers/export'))
                self.assertEqual(args.kwargs['headers']['Accept-Encoding'], 'identity')
                connection.return_value.close.assert_called_once()

    def test_chunked_complete(self):
        packet = self.observe(b'HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\n'
                              b'X-Room-Generation: 1\r\nTransfer-Encoding: chunked\r\n\r\n'
                              b'3\r\n{}\n\r\n0\r\n\r\n')
        self.assertTrue(packet['observedNone'])

    def test_content_length_complete(self):
        self.assertTrue(self.observe(b'HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\n'
                        b'X-Room-Generation: 1\r\nContent-Length: 3\r\n\r\n{}\n')['observedNone'])

    def test_truncated_chunked_and_length_and_unframed_stop(self):
        base = b'HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\nX-Room-Generation: 1\r\n'
        for payload in (
            base + b'Transfer-Encoding: chunked\r\n\r\n3\r\n{}\n\r\n',
            base + b'Transfer-Encoding: chunked\r\n\r\n5\r\n{}\n',
            base + b'Content-Length: 4\r\n\r\n{}\n',
            base + b'\r\n{}\n',
            base + b'Transfer-Encoding: chunked\r\nContent-Length: 3\r\n\r\n3\r\n{}\n\r\n0\r\n\r\n',
            base + b'Content-Length: 3\r\nContent-Encoding: gzip\r\n\r\n{}\n',
        ):
            with self.subTest(payload=payload), self.assertRaises(Invalid):
                self.observe(payload)


if __name__ == '__main__':
    unittest.main()
