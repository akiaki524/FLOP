"""Offline checks for the Gate B Real credential transaction.

The tests use a temporary tree and in-memory host doubles.  They do not read a
real credential, invoke sudo/systemd, or start a production service.
"""
import hashlib
import importlib.util
import json
import base64
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]


def load_gate_b():
    spec = importlib.util.spec_from_file_location(
        "gate_b_host_under_test", REPO / "deploy/connection/gate_b.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _SetupDouble:
    def sync_dir(self, _path):
        return None

    def reset_failed(self, _units, _run):
        return None


class GateBHostTransactionTest(unittest.TestCase):
    PROJECT = "did:key:z6MkjQVeF7TCAC6H6LKQnYFannWhsfWzzHhDDr3UhbZgHSTL"

    def setUp(self):
        self.gate = load_gate_b()
        self.temp = tempfile.TemporaryDirectory(prefix="gate-b-host-test-")
        self.root = Path(self.temp.name)
        self.dest = self.root / "usr-local"
        self.dest_source = self.dest / "src/collaboration_agent"
        self.dest_source.mkdir(parents=True)
        self.state = self.root / "state"
        (self.state / "ledger").mkdir(parents=True)
        self.old_ledger = self.state / "ledger/old-ledger.json"
        self.old_ledger.write_bytes(b"historical ledger\n")
        self.config = self.root / "config.json"
        self.config.write_bytes(
            b'{"mode":"DUMMY_OFFLINE","externalWriteEnabled":false}\n')
        self.units = self.root / "units"
        self.tx = self.root / "gate-b"
        self.credential = self.root / "project-seed.cred"
        self.sources = {
            "connection_runtime.mjs": b"new-runtime\n",
            "tclk_pin.json": b'{"files":{}}\n',
        }
        self.existing_source = {
            "connection_runtime.mjs": b"existing-runtime\n",
            "tclk_pin.json": b'{"files":{"old":"sha"}}\n',
        }
        for name, content in self.existing_source.items():
            (self.dest_source / name).write_bytes(content)
        self.dropins = [
            self.units / "collab-signer.service.d/gate-b.conf",
            self.units / "collab-transport.service.d/gate-b.conf",
        ]
        self.fake_r = SimpleNamespace(
            CONFIG=self.config,
            DEST=self.dest,
            STATE=self.state,
            ROOT=self.root,
            UNITS=self.units,
            SOCKETS=["collab-worker.socket", "collab-gate.socket",
                     "collab-transport.socket"],
            NAMES=["collab-signer.service", "collab-transport.service",
                   "collab-worker.service", "collab-approval.service"],
            SERVICES=["collab-signer.service", "collab-transport.service",
                      "collab-worker.service", "collab-approval.service"],
            PROJECT=self.PROJECT,
            setup=_SetupDouble(),
        )
        self.fake_r.sha = lambda data: hashlib.sha256(data).hexdigest()
        self.fake_r.trusted = lambda path, **_kwargs: Path(path)
        self.fake_r.atomic_write = self._atomic_write
        self.commands = []
        self.patches = [
            patch.object(self.gate, "r", self.fake_r),
            patch.object(self.gate.g, "r", self.fake_r),
            patch.object(self.gate, "TX", self.tx),
            patch.object(self.gate, "CREDENTIAL", self.credential),
            patch.object(self.gate, "DROPINS", self.dropins),
            patch.object(self.gate, "run", side_effect=self._run),
            patch.object(self.gate, "props", side_effect=self._props),
            patch.object(self.gate.g, "state_hashes", side_effect=self._state_hashes),
            patch.object(self.gate.g, "config_bytes", side_effect=self._config_bytes),
        ]
        for patcher in self.patches:
            patcher.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    @staticmethod
    def _atomic_write(path, data, mode=0o600):
        path = Path(path)
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)

    def _props(self, _unit, *names):
        return {name: "inactive" for name in names if name == "ActiveState"}

    def _run(self, args, **_kwargs):
        self.commands.append(list(args))
        return SimpleNamespace(stdout=b"", returncode=0)

    def _state_hashes(self):
        return {
            str(path.relative_to(self.state)): self.fake_r.sha(path.read_bytes())
            for path in self.state.rglob("*") if path.is_file()
        }

    def _config_bytes(self, human):
        return (json.dumps({
            "mode": "REAL_ACCEPT_PREPARATION",
            "projectDid": self.PROJECT,
            "humanUid": human,
            "externalWriteEnabled": False,
        }) + "\n").encode()

    def _prepare(self):
        changes = self.gate.prepare(1000, self.sources)
        return changes

    def _apply(self, changes, count=None):
        for path, data in changes if count is None else changes[:count]:
            if path in self.dropins:
                path.parent.mkdir(mode=0o755, parents=True)
            self._atomic_write(path, data, 0o644)

    def test_partial_apply_rollback_restores_files_and_removes_credential(self):
        changes = self._prepare()
        self._apply(changes, count=2)
        self.credential.write_bytes(b"encrypted-real-credential")
        self.gate.rollback()

        for name, content in self.existing_source.items():
            self.assertEqual((self.dest_source / name).read_bytes(), content)
        self.assertEqual(self.config.read_bytes(),
                         b'{"mode":"DUMMY_OFFLINE","externalWriteEnabled":false}\n')
        self.assertFalse(self.credential.exists())
        self.assertFalse(any(path.exists() for path in self.dropins))
        self.assertTrue((self.tx / "rollback.json").exists())

    def test_rollback_is_idempotent(self):
        changes = self._prepare()
        self._apply(changes)
        self.credential.write_bytes(b"encrypted-real-credential")

        self.gate.rollback()
        self.gate.rollback()

        self.assertFalse(self.credential.exists())
        self.assertFalse(any(path.exists() for path in self.dropins))
        self.assertEqual(self.config.read_bytes(),
                         b'{"mode":"DUMMY_OFFLINE","externalWriteEnabled":false}\n')

    def test_existing_ledger_is_unchanged_and_new_real_ledger_is_retained(self):
        before = self.old_ledger.read_bytes()
        changes = self._prepare()
        self._apply(changes)
        new_ledger = self.state / "ledger/identity-real.json"
        new_ledger.write_bytes(b"new real identity ledger\n")
        self.credential.write_bytes(b"encrypted-real-credential")

        self.gate.rollback()

        self.assertEqual(self.old_ledger.read_bytes(), before)
        self.assertEqual(new_ledger.read_bytes(), b"new real identity ledger\n")

    def test_unknown_edit_is_refused_after_credential_removal(self):
        changes = self._prepare()
        self._apply(changes)
        self.credential.write_bytes(b"encrypted-real-credential")
        target = self.dest_source / "connection_runtime.mjs"
        target.write_bytes(b"unexpected operator edit\n")

        with self.assertRaisesRegex(RuntimeError, "ROLLBACK_FILE_CHANGED"):
            self.gate.rollback()
        self.assertFalse(self.credential.exists())
        self.assertEqual(target.read_bytes(), b"unexpected operator edit\n")

    def test_stop_preserves_order_and_attempts_remaining_groups_after_failure(self):
        failures = {"collab-worker.socket"}

        def failing_run(args, **_kwargs):
            self.commands.append(list(args))
            if args[:2] == ["systemctl", "stop"] and failures.intersection(args):
                raise RuntimeError("STOP_FIRST_GROUP_FAILED")
            return SimpleNamespace(stdout=b"", returncode=0)

        with patch.object(self.gate, "run", side_effect=failing_run):
            with self.assertRaisesRegex(RuntimeError, "STOP_FAILED"):
                self.gate.stop()
        stops = [command for command in self.commands if command[:2] == ["systemctl", "stop"]]
        self.assertEqual(stops, [
            ["systemctl", "stop", *self.fake_r.SOCKETS],
            ["systemctl", "stop", "collab-transport.service"],
            ["systemctl", "stop", "collab-signer.service"],
            ["systemctl", "stop", "collab-worker.service", "collab-approval.service"],
        ])

    def test_handoff_failure_runs_final_cleanup_without_serializing_secret(self):
        changes = [(self.config, b"real config")]
        rollback_calls = []
        saved = []

        def fake_rollback():
            rollback_calls.append(True)

        def fake_save(path, value):
            saved.append((Path(path), value))

        fake_process = SimpleNamespace(returncode=1)
        with patch.object(self.gate, "anonymous_stdin"), \
             patch.object(self.gate, "preflight", return_value=(1000, self.sources)), \
             patch.object(self.gate, "prepare", return_value=changes), \
             patch.object(self.gate, "write_off"), \
             patch.object(self.gate, "rollback", side_effect=fake_rollback), \
             patch.object(self.gate, "save", side_effect=fake_save), \
             patch.object(self.gate.subprocess, "run", return_value=fake_process):
            report = self.gate.execute()

        self.assertFalse(report["passed"])
        self.assertEqual(report["code"], "GATE_B_FAILED")
        self.assertEqual(report["rollback"], "DUMMY_CONFIG_ALL_STOPPED")
        self.assertEqual(rollback_calls, [True])
        self.assertNotIn("encrypted-real-credential", json.dumps(report))
        self.assertNotIn("seed", json.dumps(report))
        self.assertTrue(saved)

    def test_true_write_flag_stops_and_removes_credential_before_preflight(self):
        self.config.write_text(json.dumps({
            "mode": "REAL_ACCEPT_PREPARATION",
            "projectDid": self.PROJECT,
            "externalWriteEnabled": True,
        }))
        self.credential.write_bytes(b"encrypted-real-credential")
        stopped = []
        with patch.object(self.gate, "stop", side_effect=lambda: stopped.append(True)), \
             patch.object(self.gate.g, "preflight") as gate_a_preflight, \
             patch.object(self.gate.subprocess, "run") as handoff:
            with self.assertRaisesRegex(RuntimeError, "EXTERNAL_WRITE_ENABLED_STOPPED"):
                self.gate.preflight()
        self.assertEqual(stopped, [True])
        self.assertFalse(self.credential.exists())
        gate_a_preflight.assert_not_called()
        handoff.assert_not_called()

    def test_write_off_requires_boolean_false_and_fixed_real_mode(self):
        for value in (0, "false", None, [], ""):
            self.config.write_text(json.dumps({
                "mode": "REAL_ACCEPT_PREPARATION",
                "projectDid": self.PROJECT,
                "externalWriteEnabled": value,
            }))
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "WRITE_OFF_REQUIRED"):
                    self.gate.write_off()

        self.config.write_text(json.dumps({
            "mode": "REAL_ACCEPT_PREPARATION",
            "projectDid": self.PROJECT,
            "externalWriteEnabled": False,
        }))
        self.gate.write_off()

    def test_observe_fails_closed_for_enabled_external_write_or_wrong_did(self):
        for config in (
                {"mode": "REAL_ACCEPT_PREPARATION", "projectDid": self.PROJECT,
                 "externalWriteEnabled": True},
                {"mode": "REAL_ACCEPT_PREPARATION", "projectDid": "did:key:wrong",
                 "externalWriteEnabled": False}):
            self.config.write_text(json.dumps(config))
            with self.subTest(config=config), patch.object(self.gate, "ipc") as ipc:
                with self.assertRaisesRegex(RuntimeError, "WRITE_OFF_REQUIRED"):
                    self.gate.observe()
            ipc.assert_not_called()

    def test_cli_rejects_identity_override_before_host_or_secret_access(self):
        with patch.object(self.gate, "anonymous_stdin") as anonymous, \
             patch.object(self.gate, "preflight") as preflight, \
             patch.object(self.gate.os, "geteuid", return_value=0), \
             patch.object(self.gate.sys, "argv",
                          ["gate_b.py", "run", "--expected-did", self.PROJECT]):
            with self.assertRaisesRegex(RuntimeError, "ARGUMENTS_REFUSED"):
                self.gate.main()
        anonymous.assert_not_called()
        preflight.assert_not_called()

    def test_anonymous_stdin_rejects_non_pipe(self):
        file_stat = SimpleNamespace(st_mode=0o100600, st_nlink=1)
        with patch.object(self.gate.os, "fstat", return_value=file_stat), \
             patch.object(self.gate.os, "readlink", return_value="/tmp/seed"):
            with self.assertRaisesRegex(RuntimeError, "ANONYMOUS_INPUT_REQUIRED"):
                self.gate.anonymous_stdin()


class GateBMeasurementTest(unittest.TestCase):
    """Run the allowlisted measurement in a fully fake /proc and journal tree."""

    SEED = bytes(range(32))
    credential_size = 32
    credential_mode = 0o100400
    credential_reads = 0

    class _FakePath:
        def __init__(self, value):
            self.value = str(value)

        def __truediv__(self, child):
            return self.__class__(self.value.rstrip("/") + "/" + str(child))

        @property
        def parent(self):
            return self.__class__(self.value.rsplit("/", 1)[0] or "/")

        def __str__(self):
            return self.value

        def __fspath__(self):
            return self.value

        def stat(self):
            return SimpleNamespace(
                st_size=GateBMeasurementTest.credential_size,
                st_uid=0,
                st_mode=GateBMeasurementTest.credential_mode)

        def read_bytes(self):
            if self.value == "/run/credentials/collab-signer.service/project-seed":
                GateBMeasurementTest.credential_reads += 1
                return GateBMeasurementTest.SEED
            if self.value.startswith("/proc/") and self.value.endswith("/status"):
                return b"Name:\tsigner\nNoNewPrivs:\t1\n"
            if self.value.startswith("/proc/"):
                return GateBMeasurementTest.process_data
            raise AssertionError("unexpected fake path: " + self.value)

        def read_text(self):
            return self.read_bytes().decode()

    @classmethod
    def setUpClass(cls):
        cls.measure_path = REPO / "deploy/connection/gate_b_measure.py"

    def _run_measurement(self, *, process_data=b"safe process metadata\n", journal=b"{}\n",
                         credential_size=32, credential_mode=0o100400):
        self.__class__.process_data = process_data
        self.__class__.credential_size = credential_size
        self.__class__.credential_mode = credential_mode
        self.__class__.credential_reads = 0
        output = []
        fake_journal = SimpleNamespace(stdout=journal)
        fake_pwd = SimpleNamespace(
            getpwnam=lambda _name: SimpleNamespace(pw_uid=1000))
        globals_for_script = {
            "Path": self._FakePath,
            "pwd": fake_pwd,
            "base64": base64,
            "json": json,
            "metadata": lambda _path: {"acl": []},
            "only_owner_and_service": lambda _mode, _acl, _uid: True,
        }
        with patch.object(sys, "argv",
                          ["measure.py", "123", "signer-id", "456", "transport-id"]), \
             patch("subprocess.run", return_value=fake_journal), \
             patch("builtins.print", side_effect=lambda value: output.append(value)):
            result = runpy.run_path(str(self.measure_path), init_globals=globals_for_script)
        self.assertIsNotNone(result)
        self.assertEqual(len(output), 1)
        return json.loads(output[0]), output[0]

    def test_clean_measurement_reports_all_security_checks_true(self):
        result, output = self._run_measurement()
        for key in (
                "credential_32_bytes", "credential_owner_allowed",
                "credential_isolated", "no_new_privileges",
                "seed_absent_env_argv", "seed_absent_journal"):
            self.assertIs(result[key], True)
        self.assertEqual(self.credential_reads, 1)
        self.assertNotIn(self.SEED.hex(), output)

    def test_invalid_credential_size_fails_before_reading_credential(self):
        with self.assertRaises(SystemExit) as raised:
            self._run_measurement(credential_size=31)
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(self.credential_reads, 0)

    def test_process_metadata_forms_are_refused_without_leaking_output(self):
        forms = (
            self.SEED,
            self.SEED.hex().encode(),
            base64.b64encode(self.SEED),
            base64.urlsafe_b64encode(self.SEED).rstrip(b"="),
        )
        for form in forms:
            with self.subTest(form=form):
                result, output = self._run_measurement(process_data=b"prefix" + form + b"suffix")
                self.assertFalse(result["seed_absent_env_argv"])
                self.assertTrue(result["seed_absent_journal"])
                self.assertNotIn(form.decode("latin1"), output)

    def test_json_escaped_and_binary_journal_messages_are_refused_without_output(self):
        forms = (
            self.SEED.hex().encode(),
            base64.b64encode(self.SEED),
            base64.urlsafe_b64encode(self.SEED).rstrip(b"="),
        )
        for form in forms:
            journal = (json.dumps({"MESSAGE": form.decode()}) + "\n").encode()
            with self.subTest(form=form):
                result, output = self._run_measurement(journal=journal)
                self.assertFalse(result["seed_absent_journal"])
                self.assertNotIn(form.decode(), output)

        binary_journal = (json.dumps({"MESSAGE": list(self.SEED)}) + "\n").encode()
        result, output = self._run_measurement(journal=binary_journal)
        self.assertFalse(result["seed_absent_journal"])
        self.assertNotIn(self.SEED.hex(), output)


if __name__ == "__main__":
    unittest.main()
