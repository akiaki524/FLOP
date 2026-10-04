"""B2 synthetic contract/result and real OS boundary tests. No Claude inference."""
import copy
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from support import REPO, cli, sample
from ccw import claude, claude_real as real, isolation as iso
from ccw.claude_real_launcher import confine, sealed_binary, SEALS
from ccw.human import initialize, Human
from ccw.llm import Broker, capture
from ccw.model import Invalid, canonical, digest, sha, write_new

MODEL = "claude-sonnet-4-6"  # Synthetic full-ID comparison fixture, not account availability.


def boundary(workspace, auth, paths, parser=False):
    results = {}
    null = os.open("/dev/null", os.O_RDONLY)
    if null != 3:
        os.dup2(null, 3)
        os.close(null)
    if parser:
        iso.enter(workspace, readonly=True)
    else:
        confine(workspace, auth, os.getppid(), online=True)
        os.close(3)
    for label, path in paths.items():
        try:
            Path(path).read_bytes()
            results[label] = "READ"
        except OSError as exc:
            results[label] = exc.errno
    if not parser:
        results["auth_read"] = (auth / ".credentials.json").read_text() == "SYNTHETIC"
        (auth / ".credentials.json").write_text("SYNTHETIC_REFRESH")
        (workspace / "tmp" / "ok").write_text("positive write")
        results["write"] = True
        for domain in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX, socket.AF_NETLINK):
            for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM, socket.SOCK_RAW):
                try:
                    handle = socket.socket(domain, kind)
                    handle.close()  # No connect/send/bind, even to loopback.
                    results[f"socket-{domain}-{kind}"] = "CREATED_NO_CONNECT"
                except OSError as exc:
                    results[f"socket-{domain}-{kind}"] = exc.errno
        # Safe stand-ins for pathname bus/interop sockets: never touch real WSL
        # or user D-Bus. A passing case fails before connect, at socket creation.
        for name in ("pathname-unix", "user-dbus", "wsl-interop"):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as handle:
                    handle.connect(str(workspace / "tmp" / name))
                results[name] = "UNEXPECTED"
            except OSError as exc:
                results[name] = exc.errno
        for label, fn in (("fork", os.fork), ("socketpair", socket.socketpair),
                          ("fcntl_owner", lambda: fcntl.fcntl(0, fcntl.F_SETOWN, os.getppid())),
                          ("fcntl_async", lambda: fcntl.fcntl(0, fcntl.F_SETFL, os.O_ASYNC)),
                          ("parent_signal", lambda: os.kill(os.getppid(), 0)),
                          ("exec", lambda: os.execve("/bin/true", ["true"], {}))):
            try:
                fn()
                results[label] = "UNEXPECTED"
            except OSError as exc:
                results[label] = exc.errno
        for label, number, args in (("clone3", 435, (0, 0)), ("ptrace", 101, (0, 0, 0, 0)),
                ("process_vm", 310, (os.getppid(), 0, 0, 0, 0, 0)), ("pidfd", 434, (os.getppid(), 0)),
                ("memfd", 319, (ctypes.c_char_p(b"attack"), 0)), ("io_uring", 425, (0, 0)),
                ("foreign_execveat", 322, (9, ctypes.c_char_p(b""), 0, 0, 0x1000))):
            ctypes.set_errno(0)
            iso.LIBC.syscall(number, *args)
            results[label] = ctypes.get_errno()
        values = []
        thread = threading.Thread(target=lambda: values.append("thread-ran"))
        thread.start()
        thread.join(2)
        results["thread"] = values
    sys.stdout.buffer.write(canonical(results))


class RealTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="test-b2-", dir=REPO / ".local"))
        self.root = self.base / "run"
        initialize(self.root)
        self.human = Human(self.root)
        self.broker = Broker(self.root)
        self.addCleanup(self.human.db.close)
        self.addCleanup(self.broker.db.close)
        self.task = self.human.stage(sample())["task_id"]
        self.auth = self.root / "human" / "synthetic-auth"
        self.auth.mkdir(mode=0o700)
        self.spec = {"kind": "REAL_CLAUDE_NATIVE", "binary": "/synthetic-pinned-claude",
            "binary_digest": "a" * 64, "version": "2.1.274 (Claude Code)",
            "auth_home": str(self.auth), "auth_owner_uid": os.getuid(), "runtime_profile": "claude-native-b2-v1"}

    def preview(self, **changes):
        args = dict(provider="claude-real", model=MODEL, effort="high", max_attempts=1,
            timeout=5, max_request_bytes=256000, max_response_bytes=128000,
            max_total_request_bytes=256000, max_cost_microusd=0, ttl=300)
        args.update(changes)
        return self.broker.preview(self.task, **args)

    def result(self):
        return {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
            "session_id": "synthetic-session", "uuid": "synthetic-message", "duration_ms": 120,
            "duration_api_ms": 100, "stop_reason": "end_turn", "result": "",
            "total_cost_usd": 0.02, "permission_denials": [],
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "modelUsage": {MODEL: {"inputTokens": 10, "outputTokens": 20}},
            "structured_output": {"provider": "claude-cli-real-v1", "summary": "synthetic mapping only",
                "findings": [], "unverified": ["Not real inference"]}}

    def test_real_contract_approval_and_unconditional_live_fence(self):
        with patch.object(real, "target", return_value=self.spec), patch.object(real, "launch") as launch:
            with self.assertRaises(Invalid):
                self.broker.run_claude(self.task)
            preview = self.preview()
            self.broker.approve(preview, preview["approval_digest"], 300)
            with self.assertRaisesRegex(Invalid, "B3 offline"):
                self.broker.run_claude(self.task)
            self.assertFalse(preview["terms"]["real_authorized"])
            self.assertFalse(preview["terms"]["money_cap_enforced"])
            self.assertEqual(self.broker.status(self.task)["attempts"], [])
            self.assertFalse((self.root / "runner").exists())
            launch.assert_not_called()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "SYNTHETIC", "WSL_INTEROP": "SYNTHETIC",
                                     "HTTPS_PROXY": "SYNTHETIC"}):
            env = real.environment(self.base, self.auth)
            self.assertFalse(set(env) & {"ANTHROPIC_API_KEY", "WSL_INTEROP", "HTTPS_PROXY", "PATH"})

    def test_contract_changes_and_direct_approve_edits_rejected(self):
        with patch.object(real, "target", return_value=self.spec):
            original = self.preview()
            for field, value in (("binary", "/replacement"), ("binary_digest", "b" * 64),
                                 ("version", "new"), ("runtime_profile", "wide")):
                with self.subTest(field=field):
                    changed = dict(self.spec, **{field: value})
                    with patch.object(real, "target", return_value=changed):
                        self.assertNotEqual(original["approval_digest"], self.preview()["approval_digest"])
                        with self.assertRaises(Invalid):
                            self.broker.approve(original, original["approval_digest"], 300)
            for change in ({"model": "claude-opus-4-6"}, {"effort": "low"}, {"timeout": 6}):
                self.assertNotEqual(original["approval_digest"], self.preview(**change)["approval_digest"])
            modified = copy.deepcopy(original)
            modified["request"]["auth"]["mode"] = "api-key"
            modified["terms"]["request_digest"] = digest(modified["request"])
            modified["approval_digest"] = digest(modified["terms"])
            with self.assertRaises(Invalid):
                self.broker.approve(modified, modified["approval_digest"], 300)
            for alias in ("sonnet", "opus", "fable", "claude-sonnet-latest"):
                with self.assertRaises(Invalid):
                    self.preview(model=alias)

    def test_sealed_object_survives_path_replacement_and_refuses_mutation(self):
        path = self.base / "binary"
        path.write_bytes(Path("/usr/bin/true").read_bytes())
        fd, identity = sealed_binary(path)
        try:
            self.assertEqual(fcntl.fcntl(fd, fcntl.F_GET_SEALS), SEALS)
            path.rename(self.base / "old-binary")
            path.write_bytes(Path("/usr/bin/false").read_bytes())
            self.assertEqual(sha(os.read(fd, 1000000)), identity)
            with self.assertRaises(OSError):
                os.write(fd, b"mutation")
            with self.assertRaises(OSError):
                os.ftruncate(fd, 0)
            process = subprocess.run([f"/proc/self/fd/{fd}"], pass_fds=(fd,), env={}, capture_output=True)
            self.assertEqual(process.returncode, 0)  # still true, not substituted false
            with self.assertRaises(Invalid):
                sealed_binary(path, identity)
        finally:
            os.close(fd)

    def test_real_mapper_and_missing_usage_are_explicit(self):
        data = self.result()
        report, metrics = real.parse(canonical(data), self.broker.snapshot(self.task), {"model": MODEL})
        self.assertIsNone(metrics["provider_request_ids"])
        self.assertIsNone(metrics["internal_retries"])
        self.assertEqual(metrics["cli_session_id"], "synthetic-session")
        del data["usage"]
        _, missing = real.parse(canonical(data), self.broker.snapshot(self.task), {"model": MODEL})
        self.assertIn("usage", missing["missing_fields"])
        self.assertIsNone(missing["usage"]["input_tokens"]["value"])
        workspace = self.base / "parse"
        workspace.mkdir(mode=0o700)
        self.assertEqual(real.parse_isolated(canonical(self.result()), self.broker.snapshot(self.task),
                                            {"model": MODEL}, workspace), (report, metrics))

    def test_real_mapper_rejects_unknown_malformed_tools_and_model(self):
        mutations = [lambda d: d.update(extra="unknown"), lambda d: d.pop("modelUsage"),
            lambda d: d.update(modelUsage={"sonnet": {}}), lambda d: d.update(num_turns=True),
            lambda d: d.update(is_error=True), lambda d: d.update(usage={"future_tokens": 2}),
            lambda d: d.update(usage={"input_tokens": -1}),
            lambda d: d.update(usage={"server_tool_use": {"web_fetch_requests": 1}}),
            lambda d: d.update(usage={"cache_creation": {"unknown": 1}}),
            lambda d: d.update(permission_denials=[{"tool_name": "Bash"}]),
            lambda d: d["modelUsage"][MODEL].update(provider="gateway"),
            lambda d: d["structured_output"].update(extra="unknown")]
        for mutate in mutations:
            data = self.result()
            mutate(data)
            with self.assertRaises(Invalid):
                real.parse(canonical(data), self.broker.snapshot(self.task), {"model": MODEL})
        with self.assertRaises(Invalid):
            real.parse(b'{"type":"result","type":"result"}', self.broker.snapshot(self.task), {"model": MODEL})

    def test_auth_inventory_metadata_diff_and_links_rejected(self):
        before = real.inventory(self.auth)
        path = self.auth / ".credentials.json"
        path.write_text("SYNTHETIC")
        path.chmod(0o600)
        after = real.inventory(self.auth)
        self.assertEqual(real.inventory_diff(before, after)["added"], [".credentials.json"])
        self.assertNotIn("SYNTHETIC", canonical(after).decode())
        (self.auth / "link").symlink_to(path)
        with self.assertRaises(Invalid):
            real.inventory(self.auth)

    def test_online_profile_boundaries_and_parser_auth_denial(self):
        workspace = self.base / "runtime"
        workspace.mkdir(mode=0o700)
        claude.prepare(workspace)
        credential = self.auth / ".credentials.json"
        credential.write_text("SYNTHETIC")
        credential.chmod(0o600)
        before = real.inventory(self.auth)
        paths = {}
        for name in ("human", "signer", "llm-db", "ordinary-home", "other-repository"):
            path = self.base / ("canary-" + name)
            path.write_text("SYNTHETIC")
            paths[name] = str(path)
        command = [sys.executable, "-B", str(Path(__file__).resolve()), "boundary", str(workspace),
                   str(self.auth), canonical(paths).decode()]
        result = json.loads(capture(command, workspace, 5, 128000, input_bytes=b""))
        for key in paths:
            self.assertIn(result[key], (errno.EACCES, errno.EPERM))
        for domain in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX, socket.AF_NETLINK):
            for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM, socket.SOCK_RAW):
                expected = "CREATED_NO_CONNECT" if domain in (2, 10) and kind == 1 else errno.EPERM
                self.assertEqual(result[f"socket-{domain}-{kind}"], expected)
        for key in ("fork", "socketpair", "parent_signal", "exec", "ptrace", "process_vm", "pidfd",
                    "memfd", "io_uring", "foreign_execveat", "pathname-unix", "user-dbus", "wsl-interop",
                    "fcntl_owner", "fcntl_async"):
            self.assertEqual(result[key], errno.EPERM)
        self.assertEqual(result["clone3"], errno.ENOSYS)
        self.assertEqual(result["thread"], ["thread-ran"])
        self.assertTrue(result["auth_read"] and result["write"])
        after = real.inventory(self.auth)
        self.assertEqual(real.inventory_diff(before, after)["changed"], [".credentials.json"])
        paths["auth"] = str(credential)
        command[3] = "parser-boundary"
        command[-1] = canonical(paths).decode()
        denied = json.loads(capture(command, workspace, 5, 128000))
        self.assertTrue(all(v in (errno.EACCES, errno.EPERM) for v in denied.values()))
        write_new(self.base / "boundary.json", {"runtime": result, "parser": denied,
            "auth_before": before, "auth_after": after, "network_sends": 0})

    def test_real_profile_timeout_kills_without_retry(self):
        workspace = self.base / "timeout"
        workspace.mkdir(mode=0o700)
        claude.prepare(workspace)
        pids = []
        with self.assertRaisesRegex(Invalid, "timeout"):
            capture([sys.executable, "-B", str(Path(__file__).resolve()), "linger", str(workspace), str(self.auth)],
                    workspace, 1, 128000, input_bytes=b"", started=pids.append)
        self.assertEqual(len(pids), 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(pids[0], 0)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "linger":
        null = os.open("/dev/null", os.O_RDONLY)
        if null != 3:
            os.dup2(null, 3)
            os.close(null)
        confine(Path(sys.argv[2]), Path(sys.argv[3]), os.getppid(), online=True)
        time.sleep(10)
    elif sys.argv[1:] == ["socket-host-probe"]:
        for domain in (socket.AF_INET, socket.AF_INET6):
            try:
                handle = socket.socket(domain, socket.SOCK_STREAM)
                handle.close()
                print(json.dumps({"domain": domain, "created": True, "connect_or_send": False}))
            except OSError as exc:
                print(json.dumps({"domain": domain, "created": False, "errno": exc.errno}))
    elif len(sys.argv) == 5 and sys.argv[1] in ("boundary", "parser-boundary"):
        boundary(Path(sys.argv[2]), Path(sys.argv[3]), json.loads(sys.argv[4]), sys.argv[1] == "parser-boundary")
    else:
        unittest.main()
