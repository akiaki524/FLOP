"""Human launcher boundary tests. Dummy bytes only; no provider connection."""
import contextlib
import io
import os
from pathlib import Path
import pty
import select
import sys
import tempfile
import threading
import time
import termios
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ccw import messages_real_human as human
from ccw.runtime_interface import parse_request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_service_smoke import MATERIAL
REQUEST = parse_request(MATERIAL.encode())


class TestMessagesRealHuman(unittest.TestCase):
    def terminal_attempt(self, input_bytes, *, typeahead=b"", expect_drained=False):
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        original[3] |= termios.ECHO | termios.ECHONL
        termios.tcsetattr(slave, termios.TCSANOW, original)
        outcome = {}
        thread = None
        try:
            if typeahead:
                os.write(master, typeahead)

            def read_key():
                try:
                    outcome["key"] = human._terminal_key()
                except BaseException as exc:
                    outcome["error"] = exc

            with patch.object(human.os, "open", side_effect=lambda *args: os.dup(slave)):
                thread = threading.Thread(target=read_key, daemon=True)
                thread.start()
                output = bytearray()
                deadline = time.monotonic() + 3
                while b"Messages API key (hidden): " not in output:
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, "terminal prompt timed out")
                    ready, _, _ = select.select([master], [], [], remaining)
                    self.assertTrue(ready, "terminal prompt timed out")
                    output.extend(os.read(master, 4096))

                hidden = termios.tcgetattr(slave)
                os.write(master, input_bytes)
                thread.join(3)
                self.assertFalse(thread.is_alive(), "terminal input timed out")
                restored = termios.tcgetattr(slave)
                if expect_drained:
                    self.assertFalse(select.select([slave], [], [], 0)[0],
                                     "unread terminal input remains for parent shell")
                while select.select([master], [], [], 0)[0]:
                    output.extend(os.read(master, 4096))
                return outcome, hidden, restored, original, bytes(output)
        finally:
            os_close(master)
            if thread is not None:
                thread.join(1)
            os_close(slave)

    def test_failure_prints_fixed_classification_only(self):
        error = io.StringIO()
        dummy = "sk-" + "ant-api03-DUMMYRAWRESPONSE"
        with patch.object(human, "main", side_effect=RuntimeError(dummy)), \
             contextlib.redirect_stderr(error):
            self.assertEqual(human.entrypoint(), 2)
        self.assertNotIn(dummy, error.getvalue())
        self.assertEqual(error.getvalue(),
            "Real Messages API attempt refused or failed; status may be unknown. Do not retry.\n")

    def test_terminal_key_hidden_and_restored(self):
        key = b"sk-" + b"ant-api03-" + b"D" * 24
        outcome, hidden, restored, original, output = self.terminal_attempt(key + b"\n")
        self.assertEqual(outcome.get("key"), key)
        self.assertNotIn("error", outcome)
        self.assertEqual(hidden[3] & (termios.ECHO | termios.ECHONL), 0)
        self.assertEqual(restored, original)
        self.assertNotIn(key, output)

    def test_typeahead_is_not_accepted_as_key(self):
        stale = b"sk-" + b"ant-api03-" + b"S" * 24
        fresh = b"sk-" + b"ant-api03-" + b"F" * 24
        outcome, _, restored, original, _ = self.terminal_attempt(
            fresh + b"\n", typeahead=stale + b"\n")
        self.assertEqual(outcome.get("key"), fresh)
        self.assertNotIn("error", outcome)
        self.assertEqual(restored, original)

    def test_oversize_terminal_key_is_rejected(self):
        key = b"K" * (human.MAX_KEY_BYTES + 1)
        outcome, _, restored, original, _ = self.terminal_attempt(key + b"\n")
        self.assertNotIn("key", outcome)
        self.assertIsInstance(outcome.get("error"), ValueError)
        self.assertEqual(restored, original)

    def test_second_line_is_discarded_after_valid_key(self):
        key = b"sk-" + b"ant-api03-" + b"D" * 24
        outcome, _, restored, original, _ = self.terminal_attempt(
            key + b"\nsecond-line-for-shell\n", expect_drained=True)
        self.assertEqual(outcome.get("key"), key)
        self.assertEqual(restored, original)

    def test_second_line_is_discarded_after_oversize_key(self):
        key = b"K" * (human.MAX_KEY_BYTES + 1)
        outcome, _, restored, original, _ = self.terminal_attempt(
            key + b"\nsecond-line-for-shell\n", expect_drained=True)
        self.assertIsInstance(outcome.get("error"), ValueError)
        self.assertEqual(restored, original)

    def test_key_byte_limit_boundary(self):
        accepted = b"K" * human.MAX_KEY_BYTES
        outcome, _, _, _, _ = self.terminal_attempt(accepted + b"\n")
        self.assertEqual(outcome.get("key"), accepted)
        rejected = accepted + b"K"
        outcome, _, _, _, _ = self.terminal_attempt(rejected + b"\n")
        self.assertIsInstance(outcome.get("error"), ValueError)

    def test_closed_terminal_input_is_rejected(self):
        with patch.object(human.os, "open", return_value=0), \
             patch.object(human.os, "isatty", return_value=True), \
             patch.object(human.termios, "tcgetattr", return_value=[0, 0, 0, 0, 0, 0, []]), \
             patch.object(human.termios, "tcsetattr"), \
             patch.object(human.os, "write"), \
             patch.object(human.os, "read", side_effect=[b"K"] * 16 + [b""]), \
             patch.object(human.os, "close"):
            with self.assertRaisesRegex(ValueError, "terminal closed"):
                human._terminal_key()

    def test_preflight_refusal_never_reads_key(self):
        with tempfile.TemporaryDirectory() as area:
            root = Path(area)
            (root / "runtime").mkdir()
            (root / "request.json").write_bytes(MATERIAL.encode())
            (root / "policy.json").write_text('{"api":{"real_enabled":true}}')
            with patch.object(human, "launch_guard") as guard, \
                 patch.object(human, "protect_process"), \
                 patch.object(human, "private_directory", return_value=root), \
                 patch.object(human.runtime_api, "admission", side_effect=ValueError("closed")), \
                 patch.object(human, "_terminal_key") as key:
                with self.assertRaises(ValueError):
                    human.run(root)
            guard.assert_called_once_with()
            key.assert_not_called()

    def test_launch_guard_refusal_never_reads_key(self):
        with patch.object(human, "launch_guard", side_effect=ValueError("guard refused")), \
             patch.object(human, "_terminal_key") as key:
            with self.assertRaisesRegex(ValueError, "guard refused"):
                human.run("unused-root")
            key.assert_not_called()

    def test_separate_sockets_and_exact_request(self):
        with tempfile.TemporaryDirectory() as area:
            root = Path(area)
            (root / "runtime").mkdir()
            (root / "request.json").write_bytes(MATERIAL.encode())
            (root / "policy.json").write_text('{"api":{"real_enabled":true}}')
            dummy = b"sk-" + b"ant-api03-" + b"D" * 24
            observed = {}

            class Child:
                def __init__(self, command, **kwargs):
                    observed["command"] = command
                    observed["kwargs"] = kwargs
                    observed["activity_fd"] = kwargs["stdin"].fileno()
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
                def poll(self):
                    return 0
                def wait(self, timeout=None):
                    return 0

            class Client:
                def __init__(self, endpoint, *, runtime_kind):
                    self.endpoint = endpoint
                    self.kind = runtime_kind
                def review(self, request):
                    self.assert_request(request)
                    observed["request"] = request.encode()
                    observed["kind"] = self.kind
                    return type("Result", (), {"preview_text": lambda self: "HUMAN_PREVIEW"})()
                def assert_request(self, request):
                    assert request.locator == REQUEST["task"]["locator"]
                    assert request.text == REQUEST["task"]["text"]
                    assert request.timeout_ms == REQUEST["timeout_ms"]

            output = io.StringIO()
            def capture_key(endpoint, raw, limit):
                observed["key"] = raw
                observed["key_fd"] = endpoint.fileno()
                assert limit == 4096
            with patch.object(human, "launch_guard"), patch.object(human, "protect_process"), \
                 patch.object(human, "private_directory", return_value=root), \
                 patch.object(human.runtime_api, "admission"), \
                 patch.object(human, "_terminal_key", return_value=bytearray(dummy)), \
                 patch.object(human, "send_frame", side_effect=capture_key), \
                 patch.object(human.subprocess, "Popen", Child), \
                 patch.object(human, "RuntimeClient", Client), contextlib.redirect_stdout(output):
                human.run(root)
            self.assertEqual(observed["key"], dummy)
            self.assertEqual(observed["request"], MATERIAL.encode())
            self.assertEqual(observed["kind"], "real-messages-api")
            self.assertEqual(observed["command"][-2], "--api-real-credential-fd")
            self.assertNotIn(dummy.decode(), str(observed["command"]))
            self.assertEqual(observed["kwargs"]["env"], {})
            self.assertNotEqual(observed["activity_fd"], observed["kwargs"]["pass_fds"][0])
            self.assertNotEqual(observed["key_fd"], observed["activity_fd"])
            self.assertEqual(output.getvalue(), "HUMAN_PREVIEW\n")


def os_close(fd):
    try:
        os.close(fd)
    except OSError:
        pass
