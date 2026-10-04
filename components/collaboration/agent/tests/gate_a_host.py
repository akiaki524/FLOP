"""Offline unit checks for the reversible Gate A host transaction.

These tests exercise the deployment transaction and rollback code against a
temporary tree.  ``systemctl``, account lookups, and all host paths are
replaced with in-memory/local test doubles; no host unit or credential is
installed by this module.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]


def load_gate_a():
    spec = importlib.util.spec_from_file_location(
        "gate_a_host_under_test", REPO / "deploy/connection/gate_a.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _SetupDouble:
    def sync_dir(self, _path):
        return None

    def reset_failed(self, _units, _run):
        return None


class GateAHostTransactionTest(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate_a()
        self.temp = tempfile.TemporaryDirectory(prefix="gate-a-host-test-")
        self.root = Path(self.temp.name)
        self.dest = self.root / "usr-local"
        self.dest_source = self.dest / "src/collaboration_agent"
        self.dest_source.mkdir(parents=True)
        self.state = self.root / "state"
        self.state.mkdir()
        (self.state / "ledger.json").write_bytes(b"historical state")
        self.config = self.root / "config.json"
        self.config.write_bytes(b'{"mode":"DUMMY_OFFLINE","externalWriteEnabled":false}\n')
        self.tx = self.root / "gate-a"
        self.fake = self.root / "gate-a-fake.cred"
        self.probe = self.dest / "gate_a_probe.py"
        self.probe_result = self.state / "gate-a-probe.json"
        self.probe_temp = self.state / "gate-a-probe.pending"
        self.diagnostic = self.state / "startup-diagnostic.json"
        self.dropins = [
            self.root / "units/collab-signer.service.d/gate-a.conf",
            self.root / "units/collab-transport.service.d/gate-a.conf",
        ]

        # Existing deployment material is restored byte-for-byte by rollback.
        self.existing_source = {
            "connection_runtime.mjs": b"existing-runtime\n",
            "tclk_pin.json": b'{"files":{}}\n',
        }
        for name, content in self.existing_source.items():
            (self.dest_source / name).write_bytes(content)
        self.sources = {
            name: b"new-" + content for name, content in self.existing_source.items()
        }

        self.fake_r = SimpleNamespace(
            CONFIG=self.config,
            DEST=self.dest,
            STATE=self.state,
            NAMES=["collab-signer.service", "collab-transport.service"],
            SOCKETS=[],
            SERVICES=["collab-signer.service", "collab-transport.service"],
            PROJECT="did:key:z6MkjQVeF7TCAC6H6LKQnYFannWhsfWzzHhDDr3UhbZgHSTL",
            setup=_SetupDouble(),
        )
        self.fake_r.sha = lambda data: hashlib.sha256(data).hexdigest()
        self.fake_r.trusted = lambda path, **_kwargs: Path(path)
        self.fake_r.atomic_write = self._atomic_write
        self.patchers = [
            patch.object(self.gate, "r", self.fake_r),
            patch.object(self.gate, "TX", self.tx),
            patch.object(self.gate, "FAKE", self.fake),
            patch.object(self.gate, "PROBE", self.probe),
            patch.object(self.gate, "PROBE_RESULT", self.probe_result),
            patch.object(self.gate, "PROBE_TEMP", self.probe_temp),
            patch.object(self.gate, "DIAGNOSTIC", self.diagnostic),
            patch.object(self.gate, "DROPINS", self.dropins),
            patch.object(self.gate, "props", side_effect=self._props),
            patch.object(self.gate, "run", side_effect=self._run),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(self._stop_patchers)
        self.commands = []

    def _stop_patchers(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    @staticmethod
    def _atomic_write(path, data, mode=0o600):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)

    def _props(self, _unit, *names):
        return {name: "inactive" for name in names if name == "ActiveState"}

    def _run(self, args, **_kwargs):
        self.commands.append(list(args))
        return SimpleNamespace(stdout=b"", returncode=0)

    def _prepare(self):
        # A pre-existing diagnostic is intentionally absent: state hashes still
        # cover historical state and rollback must leave it untouched.
        prepare = getattr(self, "_prepare_impl", self.gate.prepare)
        self.changes = prepare(1000, self.sources)
        self.gate.save(self.tx / "state-before.json", self.gate.state_hashes())

    def _apply_changes(self):
        for path, data in self.changes:
            if path in self.dropins:
                path.parent.mkdir(mode=0o755, parents=True)
            self._atomic_write(path, data, 0o644)
        self.fake.write_bytes(b"encrypted fake credential")
        self.fake.chmod(0o600)

    def test_real_config_forces_fixed_project_did_and_write_off(self):
        value = json.loads(self.gate.config_bytes(1000))
        self.assertEqual(value, {
            "mode": "REAL_ACCEPT_PREPARATION",
            "projectDid": self.fake_r.PROJECT,
            "humanUid": 1000,
            "externalWriteEnabled": False,
        })
        example = (self.gate.HERE / "real-accept/config.json.example").read_text()
        self.assertNotIn("ALLOW_TEST_DID", example)
        self.assertNotIn("expectedDidOverride", example)

    def test_rollback_restores_original_files_deletes_created_files_and_preserves_state(self):
        before_state = (self.state / "ledger.json").read_bytes()
        self._prepare()
        self._apply_changes()

        self.gate.rollback()

        for name, content in self.existing_source.items():
            self.assertEqual((self.dest_source / name).read_bytes(), content)
        self.assertEqual(self.config.read_bytes(), b'{"mode":"DUMMY_OFFLINE","externalWriteEnabled":false}\n')
        self.assertFalse(self.probe.exists())
        self.assertFalse(any(path.exists() for path in self.dropins))
        self.assertFalse(self.fake.exists())
        self.assertEqual((self.state / "ledger.json").read_bytes(), before_state)
        self.assertTrue((self.tx / "rollback.json").exists())
        self.assertTrue(any(command[:2] == ["systemctl", "stop"] for command in self.commands))

    def test_rollback_refuses_unexpected_changed_file_without_overwriting_it(self):
        self._prepare()
        self._apply_changes()
        target = self.dest_source / "connection_runtime.mjs"
        target.write_bytes(b"unexpected operator edit")

        with self.assertRaisesRegex(RuntimeError, "ROLLBACK_FILE_CHANGED"):
            self.gate.rollback()
        self.assertEqual(target.read_bytes(), b"unexpected operator edit")

    def test_rollback_is_idempotent_after_a_successful_restore(self):
        self._prepare()
        self._apply_changes()

        self.gate.rollback()
        self.gate.rollback()

        self.assertEqual(self.config.read_bytes(), b'{"mode":"DUMMY_OFFLINE","externalWriteEnabled":false}\n')
        self.assertFalse(self.fake.exists())
        self.assertFalse(any(path.exists() for path in self.dropins))

    def test_rollback_restores_after_partial_apply_and_removes_created_paths(self):
        self._prepare()
        # Simulate interruption after only the first source and config writes.
        for path, data in self.changes[:2]:
            self._atomic_write(path, data, 0o644)

        self.gate.rollback()

        self.assertEqual((self.dest_source / "connection_runtime.mjs").read_bytes(),
                         self.existing_source["connection_runtime.mjs"])
        self.assertEqual((self.dest_source / "tclk_pin.json").read_bytes(),
                         self.existing_source["tclk_pin.json"])
        self.assertEqual(self.config.read_bytes(), b'{"mode":"DUMMY_OFFLINE","externalWriteEnabled":false}\n')
        self.assertFalse(self.probe.exists())
        self.assertFalse(any(path.exists() for path in self.dropins))

    def test_execute_failure_runs_rollback_and_does_not_emit_credential_content(self):
        # This checks the finally path directly with all host operations mocked.
        calls = []
        rollback = self.gate.rollback
        def failing_rollback():
            calls.append("rollback")
            return rollback()

        self.gate.preflight = lambda: (1000, self.sources)
        self.gate.rollback = failing_rollback
        original_stop = self.gate.stop
        stop_count = [0]
        def fail_inside_execute():
            calls.append("stop")
            stop_count[0] += 1
            if stop_count[0] == 1:
                raise RuntimeError("ENCRYPT_FAILED")
            return original_stop()
        self.gate.stop = fail_inside_execute
        with patch.object(self.gate, "save", wraps=self.gate.save), patch("builtins.print") as printed:
            report = self.gate.execute()
        self.assertIn("rollback", calls)
        self.assertFalse(report["passed"])
        self.assertEqual(report["code"], "ENCRYPT_FAILED")
        self.assertEqual(report["rollback"], "DUMMY_CONFIG_ALL_STOPPED")
        printed.assert_not_called()
        serialized = json.dumps(report)
        self.assertNotIn(b"encrypted fake credential".decode(), serialized)
        self.assertNotIn(bytes(range(32)).hex(), serialized)


    def _rolled_back_attempt(self):
        self._prepare()
        self._apply_changes()
        self.gate.rollback()
        self.gate.save(self.tx / "result.json", {"passed": False, "code": "CREDENTIAL_PROBE_FAILED"})
        self.fake_r.ROOT = self.root

    def test_rerun_archives_rolled_back_attempt_without_changing_evidence(self):
        self._rolled_back_attempt()
        evidence = {p.name: p.read_bytes() for p in self.tx.iterdir()}

        self.gate.archive_completed()

        self.assertFalse(self.tx.exists())
        archived = self.root / "gate-a-attempt-1"
        self.assertEqual({p.name: p.read_bytes() for p in archived.iterdir()}, evidence)
        self._rolled_back_attempt()
        self.gate.archive_completed()
        self.assertTrue((self.root / "gate-a-attempt-2" / "result.json").exists())
        self.assertEqual({p.name: p.read_bytes() for p in archived.iterdir()}, evidence)

    def test_rerun_refuses_unfinished_or_uncleaned_transaction(self):
        self._rolled_back_attempt()
        (self.tx / "rollback.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "TRANSACTION_NOT_ROLLED_BACK"):
            self.gate.archive_completed()
        self.assertTrue(self.tx.exists())

        self.gate.rollback()
        self.fake.write_bytes(b"leftover")
        with self.assertRaisesRegex(RuntimeError, "CLEANUP_INCOMPLETE"):
            self.gate.archive_completed()
        self.assertTrue(self.tx.exists())
        self.assertFalse((self.root / "gate-a-attempt-1").exists())


def load_probe():
    spec = importlib.util.spec_from_file_location(
        "gate_a_probe_under_test", REPO / "deploy/connection/gate_a_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GateAProbePermissionTest(unittest.TestCase):
    SERVICE = 990
    U = 0xFFFFFFFF

    def setUp(self):
        self.probe = load_probe()
        p = self.probe
        # Shape systemd 255 writes: root-owned 0400 file plus a named-user read entry.
        self.systemd = [[p.ACL_USER_OBJ, 4, self.U], [p.ACL_USER, 4, self.SERVICE],
                        [p.ACL_GROUP_OBJ, 0, self.U], [p.ACL_MASK, 4, self.U], [p.ACL_OTHER, 0, self.U]]

    def check(self, mode, entries):
        return self.probe.only_owner_and_service(mode, entries, self.SERVICE)

    def replace(self, index, entry):
        entries = [list(e) for e in self.systemd]
        entries[index] = entry
        return entries

    def test_mode_bits_decide_only_without_acl(self):
        self.assertTrue(self.check(0o100400, None))
        self.assertFalse(self.check(0o100440, None))
        self.assertFalse(self.check(0o100404, None))

    def test_systemd_acl_mask_in_group_bits_is_not_group_access(self):
        self.assertTrue(self.check(0o100440, self.systemd))

    def test_any_other_principal_or_write_is_refused(self):
        p, U = self.probe, self.U
        self.assertFalse(self.check(0o100440, self.replace(1, [p.ACL_USER, 4, self.SERVICE + 1])))
        self.assertFalse(self.check(0o100440, self.replace(1, [p.ACL_USER, 6, self.SERVICE])))
        self.assertFalse(self.check(0o100440, self.replace(2, [p.ACL_GROUP_OBJ, 4, U])))
        self.assertFalse(self.check(0o100444, self.replace(4, [p.ACL_OTHER, 4, U])))
        self.assertFalse(self.check(0o100440, self.systemd + [[p.ACL_GROUP, 4, 0]]))
        self.assertFalse(self.check(0o100440, self.systemd[:-1]))
        self.assertFalse(self.check(0o100440, self.systemd + [[p.ACL_MASK, 4, U]]))
        self.assertFalse(self.check(0o100440, self.systemd + [[0x40, 4, U]]))

    def test_kernel_reports_acl_mask_as_group_bits_on_tmpfs(self):
        import os
        import struct
        p = self.probe
        try:
            directory = tempfile.mkdtemp(dir="/dev/shm")
        except OSError:
            self.skipTest("TMPFS_UNAVAILABLE")
        path = os.path.join(directory, "project-seed")
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o400))
            blob = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *e) for e in self.systemd)
            try:
                os.setxattr(path, p.ACL_XATTR, blob)
            except OSError:
                self.skipTest("TMPFS_ACL_UNSUPPORTED")
            metadata = p.metadata(path)
            self.assertEqual(metadata["mode"], "0o440")
            self.assertEqual(metadata["acl"], self.systemd)
            self.assertTrue(p.only_owner_and_service(os.stat(path).st_mode, metadata["acl"], self.SERVICE))
            self.assertFalse(p.only_owner_and_service(os.stat(path).st_mode, metadata["acl"], self.SERVICE + 1))
        finally:
            if os.path.exists(path):
                os.unlink(path)
            os.rmdir(directory)


if __name__ == "__main__":
    unittest.main()
