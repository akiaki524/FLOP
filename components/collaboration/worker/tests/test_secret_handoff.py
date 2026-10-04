"""Dummy-only boundary and non-recording regression tests."""
import copy
import json
import os
import signal
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from support import REPO, cli, sample
from secret_handoff_probe import FakeSource, REFERENCE, assert_absent, descendant_alive
from ccw import claude, claude_real, secret_handoff as handoff
from ccw.human import Human, initialize
from ccw.llm import Broker
from ccw.model import canonical
from test_claude import limits


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.area = Path(tempfile.mkdtemp(prefix="test-handoff-", dir=REPO / ".local"))
        self.attempt = self.area / "attempt"
        self.attempt.mkdir(mode=0o700)
        self.source = FakeSource(self.area)

    def execute(self, source=None):
        source = source or self.source
        public = handoff.plan(source, self.attempt, kind="OFFLINE_FIXTURE_HANDOFF")
        return handoff.run_offline_fixture(source, self.attempt, handoff.digest(public))

    def op_source(self, home="resolver-home"):
        binary = str(Path(sys.executable).resolve())  # Never actually executed as op.
        resolver_home = self.area / home
        resolver_home.mkdir(mode=0o700)
        return handoff.OnePasswordSource(binary, handoff.file_digest(binary), "dummy",
                                         REFERENCE, str(resolver_home))

    def test_confined_consumer_receives_secret_and_cannot_persist_or_access_store(self):
        result = self.execute()
        self.assertEqual(result["state"], "dummy-consumer-succeeded")
        self.assertFalse(result["real_op_read"])
        self.assertEqual(list((self.attempt / "consumer").iterdir()), [])
        assert_absent(self.source.value, self.area, [canonical(result)])
        with self.assertRaises(handoff.HandoffError):
            self.execute()
        self.assertEqual(self.source.calls, 1)

    def test_confirmation_binds_source_consumer_and_attempt_before_resolution(self):
        public = handoff.plan(self.source, self.attempt, kind="OFFLINE_FIXTURE_HANDOFF")
        for change in ("source", "consumer", "attempt", "process"):
            bad = copy.deepcopy(public)
            bad[change] = "changed"
            with self.assertRaises(handoff.HandoffError):
                handoff.run_offline_fixture(self.source, self.attempt, handoff.digest(bad))
        self.assertEqual(self.source.calls, 0)
        self.assertFalse(list(self.attempt.iterdir()))

    def test_resolution_faults_consume_attempt_no_launch_fallback_or_retry(self):
        for mode in ("failure", "empty", "invalid", "oversized", "timeout", "descendant"):
            with self.subTest(mode=mode):
                self.attempt = self.area / mode
                self.attempt.mkdir(mode=0o700)
                home = self.area / f"home-{mode}"
                home.mkdir(mode=0o700)
                source = FakeSource(home, mode)
                with self.assertRaises(handoff.HandoffError) as error:
                    self.execute(source)
                if mode == "descendant":
                    self.assertFalse(descendant_alive(home))
                self.assertTrue((self.attempt / "consumed.json").exists())
                self.assertFalse((self.attempt / "consumer").exists())
                with self.assertRaises(handoff.HandoffError):
                    self.execute(source)
                self.assertEqual(source.calls, 1)
                assert_absent(source.value, self.area, [str(error.exception).encode()])

    def test_isolation_unavailable_prevents_resolution(self):
        with patch("ccw.isolation.abi", side_effect=OSError("unavailable")):
            with self.assertRaises(handoff.HandoffError):
                self.execute()
        self.assertEqual(self.source.calls, 0)
        self.assertTrue((self.attempt / "failure.json").exists())

    def test_consumer_failure_discards_raw_output_and_prevents_retry(self):
        original = handoff.exchange
        def fail_consumer(argv, cwd, **kwargs):
            if kwargs.get("retain_stdout") is False:
                raise RuntimeError(self.source.value.decode())
            return original(argv, cwd, **kwargs)
        with patch.object(handoff, "exchange", side_effect=fail_consumer):
            with self.assertRaises(handoff.HandoffError) as error:
                self.execute()
        assert_absent(self.source.value, self.area, [str(error.exception).encode()])
        with self.assertRaises(handoff.HandoffError):
            self.execute()
        self.assertEqual(self.source.calls, 1)

    def test_op_exact_read_contract_no_inherited_credentials_or_query(self):
        source = self.op_source()
        binary, resolver_home = source.binary, Path(source.resolver_home)
        with patch.object(handoff, "exchange", return_value=self.source.value) as call:
            self.assertEqual(source.resolve(), self.source.value)
        args, kwargs = call.call_args
        self.assertEqual(args[0], [binary, "read", REFERENCE, "--account", "dummy", "--no-newline",
                                   "--cache=false"])
        self.assertEqual(source.public()["argv"], args[0])
        self.assertFalse(source.public()["cli_flags_verified_on_local_binary"])
        self.assertEqual(kwargs["env"], {"HOME": str(resolver_home), "OP_BIOMETRIC_UNLOCK_ENABLED": "true"})
        self.assertEqual(set(kwargs["env"]), set(source.public()["environment_keys"]))
        self.assertNotIn("input_bytes", kwargs)
        self.assertNotIn(self.source.value.decode(), repr(source))
        for ref in ("op://existing-vault/item/password", REFERENCE + "?attribute=otp", REFERENCE + "/extra"):
            with self.assertRaises(handoff.HandoffError):
                handoff.OnePasswordSource(binary, source.binary_digest, "dummy", ref, str(self.area)).public()
        wrong = handoff.OnePasswordSource(binary, "0" * 64, "dummy", REFERENCE, str(resolver_home))
        with patch.object(handoff, "exchange") as call, self.assertRaises(handoff.HandoffError):
            wrong.resolve()
        call.assert_not_called()
        (resolver_home / "unexpected.txt").write_text("public contamination canary")
        with patch.object(handoff, "exchange") as call, self.assertRaises(handoff.HandoffError):
            source.resolve()
        call.assert_not_called()

    def test_real_entry_accepts_only_reviewed_source_and_offline_entry_never_op(self):
        class Subclass(handoff.OnePasswordSource):
            def resolve(self):
                raise AssertionError("must not be reached")
        op = self.op_source()
        sub = Subclass(*[getattr(op, name) for name in
                         ("binary", "binary_digest", "account", "reference", "resolver_home")])
        for source in (self.source, sub):
            with self.subTest(source=type(source).__name__), self.assertRaises(handoff.HandoffError):
                # Even a correct digest of the substituted source is not accepted.
                handoff.run_dummy(source, self.attempt, "0" * 64)
        with self.assertRaises(handoff.HandoffError):
            handoff.plan(sub, self.attempt)
        public = handoff.plan(op, self.attempt, kind="OFFLINE_FIXTURE_HANDOFF")
        with self.assertRaises(handoff.HandoffError):
            handoff.run_offline_fixture(op, self.attempt, handoff.digest(public))
        self.assertEqual(self.source.calls, 0)
        self.assertFalse(list(self.attempt.iterdir()))

    def test_real_entry_refuses_agent_session_before_consuming_or_reading(self):
        op = self.op_source()
        confirmed = handoff.digest(handoff.plan(op, self.attempt))
        cases = {"no-tty": ({}, False, []), "agent-env": ({"CLAUDECODE": "1"}, True, []),
                 "agent-ancestor": ({}, True, ["ancestor:codex"])}
        for name, (environ, tty, ancestry) in cases.items():
            with self.subTest(name), patch.dict(os.environ, environ, clear=True), \
                    patch("os.isatty", return_value=tty), \
                    patch.object(handoff, "agent_markers", wraps=handoff.agent_markers) as markers, \
                    patch.object(handoff, "exchange") as call, self.assertRaises(handoff.HandoffError):
                if ancestry:
                    markers.side_effect = lambda: ancestry
                handoff.run_dummy(op, self.attempt, confirmed)
            call.assert_not_called()
        self.assertFalse(list(self.attempt.iterdir()))

    def test_real_entry_end_to_end_with_stubbed_op_process_only(self):
        op = self.op_source()
        confirmed = handoff.digest(handoff.plan(op, self.attempt))
        original = handoff.exchange
        def stub_op(argv, cwd, **kwargs):
            if argv == op.argv():
                self.assertEqual(kwargs["env"], {"HOME": op.resolver_home,
                                                 "OP_BIOMETRIC_UNLOCK_ENABLED": "true"})
                return self.source.value
            return original(argv, cwd, **kwargs)
        with patch.object(handoff, "launch_guard"), patch.object(handoff, "exchange", side_effect=stub_op):
            result = handoff.run_dummy(op, self.attempt, confirmed)
        self.assertTrue(result["real_op_read"])
        # Launcher protection is irreversible for this (test) process.
        self.assertEqual(handoff.prctl(handoff.PR_GET_DUMPABLE), 0)
        assert_absent(self.source.value, self.area, [canonical(result)])
        with patch.object(handoff, "launch_guard"), self.assertRaises(handoff.HandoffError):
            handoff.run_dummy(op, self.attempt, confirmed)

    def test_exchange_kills_descendants_and_fails_even_after_clean_exit(self):
        script = ("import os,sys,time\n"
                  "if os.fork()==0:\n"
                  " os.setsid()\n"
                  " if os.fork()==0:\n"
                  "  open(sys.argv[2],'w').write(str(os.getpid())); time.sleep(30)\n"
                  " os._exit(0)\n"
                  "if sys.argv[1]=='daemon':\n"
                  " while not os.path.exists(sys.argv[2]): time.sleep(0.01)\n")
        for mode in ("daemon", "clean"):
            with self.subTest(mode):
                marker = self.area / f"{mode}.pid"
                call = lambda: handoff.exchange([sys.executable, "-c", script, mode, str(marker)],
                                                self.area, timeout=5)
                if mode == "daemon":
                    with self.assertRaises(handoff.HandoffError):
                        call()
                    pid = int(marker.read_text())
                    self.assertFalse(Path(f"/proc/{pid}").exists())
                else:
                    # A descendant that has not even announced itself yet is
                    # still found through the subreaper and fails the exchange.
                    with self.assertRaises(handoff.HandoffError):
                        call()
        self.assertEqual(handoff.exchange([sys.executable, "-c", "print('ok')"], self.area), b"ok\n")
        subreaper = handoff.ctypes.c_int(1)
        handoff.prctl(handoff.PR_GET_CHILD_SUBREAPER, handoff.ctypes.addressof(subreaper))
        self.assertEqual(subreaper.value, 0)

    def test_exchange_observes_incomplete_handoff_and_signal_exit(self):
        observation = {}
        script = ("import os, signal, sys, time\n"
                  "sys.stdout.buffer.write(b'R'); sys.stdout.buffer.flush()\n"
                  "time.sleep(0.1)\n"
                  "os.kill(os.getpid(), signal.SIGTERM)\n")
        with self.assertRaises(handoff.ExchangeFailure):
            handoff.exchange([sys.executable, "-c", script], self.area,
                             input_bytes=b"x" * (1024 * 1024), await_ready=True,
                             observation=observation)
        self.assertEqual(observation["runtime_starts"], 1)
        self.assertTrue(observation["runtime_ready_observed"])
        self.assertFalse(observation["runtime_input_delivered"])
        self.assertEqual(observation["runtime_child_returncode"], -signal.SIGTERM)

    def test_consumer_receives_value_only_after_ready_byte(self):
        consumer = str(REPO / "src" / "ccw" / "secret_consumer.py")
        workspace = self.area / "consumer"
        workspace.mkdir(mode=0o700)
        command = [sys.executable, "-I", "-S", "-B", consumer, str(workspace), str(os.getpid())]
        pairs = [socket.socketpair() for _ in range(3)]
        with subprocess.Popen(command, stdin=pairs[0][1], stdout=pairs[1][1], stderr=pairs[2][1],
                              env={}) as child:
            for ours, theirs in pairs:
                theirs.close()
            self.assertEqual(pairs[1][0].recv(1), b"R")
            with self.assertRaises(PermissionError):
                Path(f"/proc/{child.pid}/mem").open("rb").read(1)
            pairs[0][0].sendall(self.source.value)
            pairs[0][0].close()
            while pairs[1][0].recv(4096):
                pass
            self.assertEqual(child.wait(timeout=10), 0)
        for ours, _ in pairs:
            ours.close()
        # Reopenable pipe stdio is refused before any ready byte is sent.
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env={}) as child:
            child.stdin.close()
            self.assertEqual(child.stdout.read(), b"")
            self.assertEqual(child.wait(timeout=10), 2)
        # Without the ready byte the launcher never writes the value.
        with self.assertRaises(handoff.HandoffError):
            handoff.exchange([sys.executable, "-c", "import sys; sys.stdin.read()"], self.area,
                             input_bytes=self.source.value, await_ready=True, timeout=1)

    def test_termination_during_spawn_and_cleanup_is_deferred_and_handlers_restored(self):
        original_spawn, original_cleanup = handoff.subprocess.Popen, handoff.terminate_descendants
        handlers = {sig: signal.getsignal(sig) for sig in handoff.TERMINATION_SIGNALS}
        spawned = []
        def spawn(*args, **kwargs):
            child = original_spawn(*args, **kwargs)
            spawned.append(child)
            if phase == "spawn":
                os.kill(os.getpid(), signal.SIGTERM)
            return child
        def cleanup(before):
            os.kill(os.getpid(), signal.SIGHUP)
            return original_cleanup(before)
        for phase in ("spawn", "cleanup"):
            script = "import time; time.sleep(30)" if phase == "spawn" else "print('ok')"
            with self.subTest(phase=phase), patch.object(handoff.subprocess, "Popen", side_effect=spawn), \
                    patch.object(handoff, "terminate_descendants", side_effect=cleanup):
                with self.assertRaises(handoff.HandoffError):
                    handoff.exchange([sys.executable, "-c", script], self.area)
        self.assertEqual(len(spawned), 2)
        self.assertTrue(all(child.returncode is not None for child in spawned))
        self.assertEqual({sig: signal.getsignal(sig) for sig in handlers}, handlers)

    def assert_environment_policy_matches(self):
        # Only keys enter the assertion/output. Neither builder is used by the
        # public contract, and no environment values become evidence.
        for adapter, args in ((claude, (self.area,)),
                              (claude_real, (self.area, self.area / "unused-auth"))):
            actual = set(adapter.environment(*args))
            public = adapter.environment_policy()
            self.assertEqual(actual, set(public["keys"]))
            self.assertEqual(len(public["keys"]), len(set(public["keys"])))
            self.assertFalse(public["inherit_parent"])
            self.assertFalse(public["values_recorded"])

    def test_actual_environment_keys_match_public_policy(self):
        with patch.dict(os.environ, {"UNEXPECTED_PARENT_KEY": "offline-canary"}):
            self.assert_environment_policy_matches()

    def test_environment_drift_mutations_fail_the_same_key_assertion(self):
        for adapter, args in ((claude, (self.area,)),
                              (claude_real, (self.area, self.area / "unused-auth"))):
            actual, public = adapter.environment(*args), adapter.environment_policy()
            for change in ("actual-add", "actual-remove", "public-add", "public-remove"):
                mutated_actual, mutated_public = dict(actual), copy.deepcopy(public)
                if change == "actual-add":
                    mutated_actual["UNDECLARED_KEY"] = "dummy-only"
                elif change == "actual-remove":
                    mutated_actual.pop("HOME")
                elif change == "public-add":
                    mutated_public["keys"].append("UNDECLARED_KEY")
                else:
                    mutated_public["keys"].remove("HOME")
                with self.subTest(adapter=adapter.__name__, change=change), \
                        patch.object(adapter, "environment", return_value=mutated_actual), \
                        patch.object(adapter, "environment_policy", return_value=mutated_public):
                    with self.assertRaises(AssertionError):
                        self.assert_environment_policy_matches()

    def test_ccw_preview_ledger_evidence_export_never_use_environment_values(self):
        root = self.area / "ccw"
        initialize(root)
        human, broker = Human(root), Broker(root)
        self.addCleanup(human.db.close)
        self.addCleanup(broker.db.close)
        task = human.stage(sample())["task_id"]
        # Simulate a future private environment builder containing a secret.
        # Public contracts/evidence must not call it at all. Child has a fresh
        # interpreter and continues to run the normal credential-free B1 fixture.
        with patch.object(claude, "environment", return_value={"PRIVATE": self.source.value.decode()}) as env:
            preview = broker.preview(task, **limits())
            broker.approve(preview, preview["approval_digest"], preview["terms"]["ttl"])
            attempt = broker.run_claude(task)["attempt_id"]
            env.assert_not_called()
        artifact = broker.artifact(attempt)
        cli("worker", root, "intake", task)
        cli("worker", root, "import-llm", task, attempt)
        cli("worker", root, "export", task)
        final = human.preview(task)
        handoff.save_new(self.area / "human-preview.json", final)
        assert_absent(self.source.value, self.area, [canonical(preview), canonical(artifact), canonical(final)])
        self.assertNotIn("environment", artifact["evidence"])

    def test_real_contract_and_login_plan_never_call_private_environment_builder(self):
        root = self.area / "real"
        auth = self.area / "auth"
        auth.mkdir(mode=0o700)
        spec = {"auth_home": str(auth), "binary": "/unused", "kind": "REAL",
                "runtime_profile": "test-only"}
        with patch.object(claude_real, "target", return_value=spec), patch.object(
                claude_real, "environment", return_value={"PRIVATE": self.source.value.decode()}) as env:
            contract = claude_real.contract(sample(), "claude-sonnet-4-6", "high", root)
            login = claude_real.login_plan(root)
            env.assert_not_called()
        assert_absent(self.source.value, self.area, [canonical(contract), canonical(login)])
        self.assertFalse(login["login_authorized"])
        self.assertIn("DISABLED", contract["real_gate"])


if __name__ == "__main__":
    unittest.main()
