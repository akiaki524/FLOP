import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from support import HAVE_CRYPTO, Key, build_archive, page, tree_digest, ts, unsigned
from technocore_analyzer import evidence, signature


class ArchiveAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def read(self, path, room="lobby", known=None, progress=None, manifest=False):
        src = {"id": "arch", "kind": "full-capture-archive", "room": room, "path": str(path), "read_manifest": manifest}
        return evidence.adapter_for(src).read(known or {}, progress)

    def test_reads_observer_archive_with_locators_and_never_modifies_it(self):
        msgs = [unsigned(s, f"hello {s} <b>é", ts(s)) for s in range(1, 6)]
        arch = build_archive(self.root, "lobby", [page("lobby", msgs)])
        before = tree_digest(arch)
        result = self.read(arch, manifest=True)
        self.assertEqual(tree_digest(arch), before)  # source unchanged (bytes, mtime, mode)
        records = [r for u in result.units for r in u.records]
        self.assertEqual([r.seq for r in records], [1, 2, 3, 4, 5])
        self.assertEqual(records[0].message["text"], "hello 1 <b>é")  # exact, not normalized
        self.assertTrue(records[0].ref.startswith("arch:segment-"))
        self.assertNotIn(str(self.root), json.dumps(records[0].locator))  # no host path in locator
        self.assertEqual(records[0].epoch, 1)
        self.assertEqual(result.status, "READ")
        self.assertIn("NO_HTTP_RAW_IN_ARCHIVE", records[0].quality)

    def test_unchanged_shards_are_skipped_and_new_segments_found(self):
        arch = build_archive(self.root, "lobby", [page("lobby", [unsigned(1, "a", ts(1)), unsigned(2, "b", ts(2))])])
        first = self.read(arch)
        known = {u.unit: u.sha256 for u in first.units}
        # a gap then later pages → new segment; the known shard is skipped, not re-counted
        build_archive(self.root, "lobby", [page("lobby", [unsigned(5, "e", ts(5))])])
        second = self.read(arch, known=known)
        statuses = {u.unit: u.status for u in second.units}
        self.assertIn("SKIPPED_UNCHANGED", statuses.values())
        new = [r.seq for u in second.units for r in u.records]
        self.assertEqual(new, [5])

    def test_manifest_gap_and_late_observation(self):
        arch = build_archive(self.root, "lobby", [page("lobby", [unsigned(1, "a", ts(1))]),
                                                  page("lobby", [unsigned(4, "d", ts(4))]),
                                                  page("lobby", [unsigned(2, "b", ts(2))])])
        result = self.read(arch, manifest=True)
        kinds = sorted(c.kind for c in result.coverage)
        self.assertIn("GAP", kinds)
        self.assertIn("LATE_OBSERVATION", kinds)
        gap = next(c for c in result.coverage if c.kind == "GAP")
        self.assertEqual((gap.start_seq, gap.end_seq), (2, 3))
        self.assertEqual(result.manifest["_through_entry_read"], result.manifest["through_entry"])
        again = self.read(arch, manifest=True)
        self.assertEqual(len(again.coverage), len(result.coverage))  # chain re-verified on every read

    def rewrite_checkpoint(self, arch, **changes):
        cp_path = next(arch.glob("segment-*/capture.json"))
        cp = json.loads(cp_path.read_text())
        cp.update(changes)
        cp_path.chmod(0o600)
        cp_path.write_text(json.dumps(cp))
        return cp_path.parent

    def test_checkpoint_shard_count_is_bounded_by_the_files_present(self):
        from unittest import mock
        real = evidence._regular_read
        calls = []

        def counted(path, limit):
            calls.append(path)
            if len(calls) > 50:
                raise AssertionError("shard iteration is not bounded by the files present")
            return real(path, limit)
        arch = build_archive(self.root, "lobby", [page("lobby", [unsigned(1, "a", ts(1))])])
        segment = self.rewrite_checkpoint(arch, shards=10**12)
        with mock.patch.object(evidence, "_regular_read", side_effect=counted):
            result = self.read(arch)
        unit = next(u for u in result.units if u.unit.endswith("capture.json"))
        self.assertEqual((unit.status, unit.detail), ("QUARANTINED", "ARCHIVE_SET_MISMATCH"))
        self.assertEqual([r for u in result.units for r in u.records], [])
        self.assertLessEqual(len(calls), 2)
        # a hole in the published set is the same integrity failure, not a per-index probe
        (segment / "shard-000000000003.jsonl").write_bytes(b"x")
        self.rewrite_checkpoint(arch, shards=1)
        unit = next(u for u in self.read(arch).units if u.unit.endswith("capture.json"))
        self.assertEqual((unit.status, unit.detail), ("QUARANTINED", "ARCHIVE_SET_MISMATCH"))

    def rehash_shard(self, arch, edit_header=None, edit_payload=None):
        """Rewrite shard 1 with consistent header hashes, like a careful tamperer."""
        import hashlib
        shard = next(arch.glob("segment-*/shard-000000000001.jsonl"))
        raw = shard.read_bytes()
        newline = raw.find(b"\n")
        header, payload = json.loads(raw[:newline]), raw[newline + 1:]
        if edit_payload:
            payload = edit_payload(payload)
            lines = payload[:-1].split(b"\n")
            header.update(payload_sha256=hashlib.sha256(payload).hexdigest(), payload_bytes=len(payload),
                          last_sha256=hashlib.sha256(lines[-1] + b"\n").hexdigest())
        if edit_header:
            edit_header(header)
        shard.chmod(0o600)
        shard.write_bytes(json.dumps(header, separators=(",", ":")).encode() + b"\n" + payload)

    def test_non_integer_record_seq_is_quarantined(self):
        for name, literal in (("bool", b"true"), ("float", b"1.0")):
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                self.root = Path(d)
                arch = build_archive(self.root, "lobby", [page("lobby", [unsigned(1, "a", ts(1))])])
                self.rehash_shard(arch, edit_payload=lambda p: p.replace(b'"seq":1', b'"seq":' + literal, 1))
                result = self.read(arch)
                shard = next(u for u in result.units if u.unit.endswith("shard-000000000001.jsonl"))
                self.assertEqual((shard.status, shard.detail), ("QUARANTINED", "ARCHIVE_SEQUENCE_MISMATCH"))
                self.assertEqual([r for u in result.units for r in u.records], [])

    def test_non_integer_header_format_is_quarantined(self):
        for name, value in (("bool", True), ("float", 1.0)):
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                self.root = Path(d)
                arch = build_archive(self.root, "lobby", [page("lobby", [unsigned(1, "a", ts(1))])])
                self.rehash_shard(arch, edit_header=lambda h: h.update(format=value))
                shard = next(u for u in self.read(arch).units if u.unit.endswith("shard-000000000001.jsonl"))
                self.assertEqual((shard.status, shard.detail), ("QUARANTINED", "BAD_ARCHIVE_HEADER"))

    def test_non_integer_receipt_entry_id_is_quarantined(self):
        arch = self.manifest_arch()
        self.rewrite_manifest(arch, lambda item: {**item, "entry_id": float(item["entry_id"])}, True)
        self.assert_quarantined(self.read(arch, manifest=True), "MANIFEST_RECEIPT_ID")

    def test_type_invalid_shard_header_is_quarantined_not_an_adapter_error(self):
        arch = build_archive(self.root, "lobby", [page("lobby", [unsigned(1, "a", ts(1))]),
                                                  page("lobby", [unsigned(5, "e", ts(5))])])
        shard = sorted(arch.glob("segment-*/shard-000000000001.jsonl"))[0]
        raw = shard.read_bytes()
        newline = raw.find(b"\n")
        header = json.loads(raw[:newline])
        header["first_seq"] = str(header["first_seq"])  # syntactically valid, type-invalid
        shard.chmod(0o600)
        shard.write_bytes(json.dumps(header, sort_keys=True, separators=(",", ":")).encode() + raw[newline:])
        result = self.read(arch)
        bad = [u for u in result.units if u.status == "QUARANTINED"]
        self.assertEqual([u.detail for u in bad], ["BAD_ARCHIVE_HEADER"])
        self.assertTrue(bad[0].unit.endswith("shard-000000000001.jsonl"))
        self.assertEqual(len([r for u in result.units for r in u.records]), 1)  # sibling segment still read

    def manifest_arch(self):
        arch = build_archive(self.root, "lobby", [page("lobby", [unsigned(1, "a", ts(1))]),
                                                  page("lobby", [unsigned(4, "d", ts(4))]),
                                                  page("lobby", [unsigned(2, "b", ts(2))])])
        (arch / "manifest.sqlite").chmod(0o600)
        return arch

    def rewrite_manifest(self, arch, edit, rechain):
        """Tamper like an attacker who may also recompute the receipt chain."""
        import hashlib
        conn = sqlite3.connect(arch / "manifest.sqlite")
        rows = conn.execute("SELECT entry_id, document FROM receipts ORDER BY entry_id").fetchall()
        chain = "0" * 64
        for entry_id, document in rows:
            item = json.loads(document)
            new = edit(item)
            if new is not None:
                document = json.dumps(new, sort_keys=True, separators=(",", ":"))
                conn.execute("UPDATE receipts SET document=? WHERE entry_id=?", (document, entry_id))
            chain = hashlib.sha256((chain + document).encode()).hexdigest()
            if rechain:
                conn.execute("UPDATE receipts SET chain_hash=? WHERE entry_id=?", (chain, entry_id))
        if rechain:
            conn.execute("UPDATE state SET receipt_hash=?", (chain,))
        conn.commit()
        conn.close()

    def test_type_invalid_manifest_metadata_is_quarantined_not_an_adapter_error(self):
        import shutil
        base = self.manifest_arch()
        expected = sorted(r.seq for u in self.read(base).units for r in u.records)
        self.assertTrue(expected)
        mutations = {
            "shards": ("UPDATE segments SET shards='x'", "MANIFEST_SEGMENT_INVALID"),
            "first_seq": ("UPDATE segments SET first_seq='abc'", "MANIFEST_SEGMENT_INVALID"),
            "last_seq": ("UPDATE segments SET last_seq='z'", "MANIFEST_SEGMENT_INVALID"),
            "range": ("UPDATE segments SET first_seq=9, last_seq=1", "MANIFEST_RECORD_MISMATCH"),
        }
        for name, (sql, code) in mutations.items():
            with self.subTest(name):
                arch = self.root / f"copy-{name}"
                shutil.copytree(base, arch)
                conn = sqlite3.connect(arch / "manifest.sqlite")
                conn.execute(sql)
                conn.commit()
                conn.close()
                result = self.read(arch, manifest=True)  # must not raise
                self.assert_quarantined(result, code)
                self.assertTrue(result.manifest_quarantined)
                self.assertIsNone(result.manifest)
                self.assertEqual([c for c in result.coverage if c.ref.startswith("manifest.sqlite#")], [])
                records = [r for u in result.units for r in u.records]
                self.assertEqual(sorted(r.seq for r in records), expected)  # the Archive itself still reads
                self.assertTrue(all(r.epoch is None for r in records))

    def test_type_invalid_manifest_receipt_seq_is_quarantined(self):
        arch = self.manifest_arch()
        self.rewrite_manifest(arch, lambda item: {**item, "seq": "2"} if item["kind"] == "GAP" else None, True)
        expected = sorted(r.seq for u in self.read(arch).units for r in u.records)
        result = self.read(arch, manifest=True)
        self.assert_quarantined(result, "MANIFEST_RECEIPT_INVALID")
        self.assertEqual(sorted(r.seq for u in result.units for r in u.records), expected)

    def test_non_finite_number_in_manifest_receipt_is_quarantined_not_persisted(self):
        from support import write_config
        from technocore_analyzer import config as config_mod, engine
        for name, value in (("NaN", float("nan")), ("huge exponent", "1e999")):
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                self.root = Path(d)
                arch = self.manifest_arch()
                if isinstance(value, float):
                    edit = lambda item: {**item, "evidence": {"n": value}} if item["kind"] == "GAP" else None
                    self.rewrite_manifest(arch, edit, True)
                else:  # a literal the JSON encoder never writes, re-chained like a careful tamperer
                    self.rewrite_manifest(arch, lambda item: None, True)
                    import hashlib
                    conn = sqlite3.connect(arch / "manifest.sqlite")
                    rows = conn.execute("SELECT entry_id, document FROM receipts ORDER BY entry_id").fetchall()
                    chain = "0" * 64
                    for entry_id, document in rows:
                        if '"kind":"GAP"' in document:
                            document = document.replace('"evidence":{', '"evidence":{"n":1e999,', 1)
                            conn.execute("UPDATE receipts SET document=? WHERE entry_id=?", (document, entry_id))
                        chain = hashlib.sha256((chain + document).encode()).hexdigest()
                        conn.execute("UPDATE receipts SET chain_hash=? WHERE entry_id=?", (chain, entry_id))
                    conn.execute("UPDATE state SET receipt_hash=?", (chain,))
                    conn.commit()
                    conn.close()
                result = self.read(arch, manifest=True)
                unit = next(u for u in result.units if u.unit == "manifest.sqlite")
                self.assertEqual(unit.status, "QUARANTINED")
                self.assertEqual([c for c in result.coverage if c.ref.startswith("manifest.sqlite#")], [])
                cfg = config_mod.load(write_config(self.root / "a.json", sources=[
                    {"id": "arch", "kind": "full-capture-archive", "room": "lobby", "path": str(arch),
                     "read_manifest": True}]))
                out = engine.run(cfg)  # persisting Coverage must not fail the run
                self.assertGreater(out["records_cached"], 0)

    def assert_quarantined(self, result, code):
        unit = next(u for u in result.units if u.unit == "manifest.sqlite")
        self.assertEqual((unit.status, unit.detail), ("QUARANTINED", code))
        self.assertTrue(result.manifest_quarantined)
        self.assertFalse([c for c in result.coverage if c.ref.startswith("manifest.sqlite#")])
        self.assertEqual(result.status, "PARTIAL")

    def test_manifest_with_edited_receipt_is_quarantined(self):
        arch = self.manifest_arch()
        widen = lambda item: dict(item, end_seq=99) if item["kind"] == "GAP" else None
        self.rewrite_manifest(arch, widen, rechain=False)
        self.assert_quarantined(self.read(arch, manifest=True), "MANIFEST_RECEIPT_HASH")

    def test_manifest_rechained_without_state_is_quarantined(self):
        arch = self.manifest_arch()
        self.rewrite_manifest(arch, lambda item: dict(item, end_seq=99) if item["kind"] == "GAP" else None,
                              rechain=True)
        conn = sqlite3.connect(arch / "manifest.sqlite")
        conn.execute("UPDATE state SET gaps=gaps+1")
        conn.commit()
        conn.close()
        self.assert_quarantined(self.read(arch, manifest=True), "MANIFEST_TOTAL_MISMATCH")

    def test_fully_rechained_manifest_must_still_match_archive_records(self):
        arch = self.manifest_arch()
        self.rewrite_manifest(arch, lambda item: dict(item, record_sha256="0" * 64)
                              if item["kind"] == "MESSAGE" and item["seq"] == 1 else None, rechain=True)
        result = self.read(arch, manifest=True)
        self.assert_quarantined(result, "MANIFEST_RECORD_MISMATCH")
        self.assertTrue(all(r.epoch is None for u in result.units for r in u.records))

    def test_rechained_manifest_is_detected_on_incremental_read_of_unchanged_shards(self):
        arch = self.manifest_arch()
        first = self.read(arch, manifest=True)
        self.assertEqual(first.status, "READ")
        known = {u.unit: u.sha256 for u in first.units}
        self.rewrite_manifest(arch, lambda item: dict(item, record_sha256="0" * 64)
                              if item["kind"] == "MESSAGE" and item["seq"] == 1 else None, rechain=True)
        second = self.read(arch, known=known, manifest=True)
        shard_states = {u.status for u in second.units if u.unit.endswith(".jsonl")}
        self.assertEqual(shard_states, {"SKIPPED_UNCHANGED"})  # nothing re-inserted...
        self.assert_quarantined(second, "MANIFEST_RECORD_MISMATCH")  # ...yet the tampered receipt is caught

    def test_receipt_identity_change_without_matching_archive_line_is_detected(self):
        # Only the manifest's MESSAGE receipt (seq / segment) is altered and the chain re-computed;
        # the Archive shards stay byte-identical and are skipped as unchanged on the 2nd read.
        segment = lambda item: dict(item, segment="segment-99999999999999999999")
        cases = (("seq", lambda item: dict(item, seq=999), "MANIFEST_RECORD_MISMATCH"),
                 ("segment", segment, "MANIFEST_SEGMENT_MISSING"))
        for name, edit, code in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                self.root = Path(d)
                arch = self.manifest_arch()
                first = self.read(arch, manifest=True)
                self.assertEqual(first.status, "READ")
                known = {u.unit: u.sha256 for u in first.units}
                self.rewrite_manifest(arch, lambda item: edit(item) if item["kind"] == "MESSAGE" and item["seq"] == 1
                                      else None, rechain=True)
                second = self.read(arch, known=known, manifest=True)
                self.assertEqual({u.status for u in second.units if u.unit.endswith(".jsonl")}, {"SKIPPED_UNCHANGED"})
                self.assert_quarantined(second, code)
                # the same tamper is caught on a from-scratch read too
                self.assert_quarantined(self.read(arch, manifest=True), code)

    def test_receipt_without_its_segment_row_or_outside_its_range_is_quarantined(self):
        for name, sql, code in (
                ("segment row removed", "DELETE FROM segments", "MANIFEST_SEGMENT_MISSING"),
                ("seq outside segment range", "UPDATE segments SET last_seq=first_seq-1", "MANIFEST_RECORD_MISMATCH")):
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                self.root = Path(d)
                arch = self.manifest_arch()  # Archive and receipts intact; only the segments table is altered
                conn = sqlite3.connect(arch / "manifest.sqlite")
                conn.execute(sql)
                conn.commit()
                conn.close()
                self.assert_quarantined(self.read(arch, manifest=True), code)

    def test_archive_ahead_of_manifest_and_unverified_segments_are_not_invalid(self):
        arch = self.manifest_arch()
        result = self.read(arch, manifest=True)
        self.assertEqual(result.status, "READ")
        adapter = evidence.adapter_for({"id": "arch", "kind": "full-capture-archive", "room": "lobby", "path": str(arch)})
        manifest = adapter._manifest()
        segments, messages = manifest["segments"], manifest["messages"]
        self.assertTrue(messages)
        # Legitimate lead: the Archive holds lines whose receipts (and segment range) are not in the manifest yet
        last = max(seq for _, seq in messages)
        ahead = {k: v for k, v in messages.items() if k[1] != last}
        ahead_segments = {n: dict(r, last_seq=max(q for (sn, q) in ahead if sn == n)) if any(sn == n for sn, _ in ahead)
                          else r for n, r in segments.items()}
        result.coverage, result.manifest = [], {"x": 1}
        adapter._cross_check(result, ahead_segments, ahead)
        self.assertFalse(result.manifest_quarantined)
        # a receipt for a segment that could not be fully verified is not blamed on the manifest
        stream = next(iter(result.verified_streams))
        result.verified_streams.discard(stream)
        result.unverified_streams.add(stream)
        adapter._cross_check(result, segments, {(stream, segments[stream]["first_seq"]): "0" * 64})
        self.assertFalse(result.manifest_quarantined)

    def test_untouched_manifest_passes_incrementally(self):
        arch = self.manifest_arch()
        first = self.read(arch, manifest=True)
        second = self.read(arch, known={u.unit: u.sha256 for u in first.units}, manifest=True)
        self.assertEqual((second.status, second.manifest_quarantined), ("READ", False))
        self.assertEqual(len(second.coverage), len(first.coverage))

    def test_incremental_manifest_rejects_rewritten_coverage_history(self):
        arch = self.manifest_arch()
        first = self.read(arch, manifest=True)
        progress = {"through_entry": first.manifest["_through_entry_read"],
                    "chain_hash": first.manifest["_chain_hash_read"]}
        # Rewriting only a GAP can remain internally self-consistent and does not affect the
        # Archive MESSAGE cross-check; the saved receipt-chain anchor must still catch it.
        self.rewrite_manifest(
            arch,
            lambda item: dict(item, end_seq=item["end_seq"] + 10) if item["kind"] == "GAP" else None,
            rechain=True)
        self.assertEqual(self.read(arch, manifest=True).status, "READ")  # self-consistent from scratch
        self.assert_quarantined(self.read(arch, manifest=True, progress=progress), "MANIFEST_HISTORY_FORK")

    def test_incremental_manifest_rejects_history_rollback(self):
        arch = self.manifest_arch()
        first = self.read(arch, manifest=True)
        progress = {"through_entry": first.manifest["_through_entry_read"] + 1,
                    "chain_hash": "f" * 64}
        self.assert_quarantined(self.read(arch, manifest=True, progress=progress),
                                "MANIFEST_HISTORY_ROLLBACK")

    def test_wrong_room_manifest_is_quarantined_without_importing_coverage(self):
        arch = self.manifest_arch()
        conn = sqlite3.connect(arch / "manifest.sqlite")
        conn.execute("UPDATE state SET room='kibble'")  # a manifest that belongs to another room
        conn.commit()
        conn.close()
        result = self.read(arch, manifest=True)
        self.assert_quarantined(result, "MANIFEST_ROOM_MISMATCH")
        self.assertTrue(all(r.epoch is None for u in result.units for r in u.records))

    def test_checkpoint_schema_and_byte_totals(self):
        arch = build_archive(self.root, "lobby", [page("lobby", [unsigned(1, "a", ts(1))])])
        seg = next(p for p in arch.iterdir() if p.name.startswith("segment-"))
        cp_path = seg / "capture.json"
        cp_path.chmod(0o600)
        original = json.loads(cp_path.read_text())
        for edit, expect in ((lambda c: c.update(archive_bytes=c["archive_bytes"] + 1), "TIP_CHECKPOINT_MISMATCH"),
                             (lambda c: c.update(payload_bytes=c["payload_bytes"] + 1), "TIP_CHECKPOINT_MISMATCH"),
                             (lambda c: c.update(extra=1), "CHECKPOINT_UNREADABLE"),
                             (lambda c: c.update(status="BOGUS"), "CHECKPOINT_UNREADABLE"),
                             (lambda c: c.update(message_count=7), "CHECKPOINT_UNREADABLE")):
            cp = dict(original)
            edit(cp)
            cp_path.write_text(json.dumps(cp))
            result = self.read(arch)
            self.assertIn(expect, " ".join(f"{u.detail}" for u in result.units if u.status != "READ"), cp)
            self.assertEqual(result.status, "PARTIAL")
        cp_path.write_text(json.dumps(original))
        self.assertEqual(self.read(arch).status, "READ")

    def test_records_are_withheld_when_the_checkpoint_totals_fail(self):
        arch = build_archive(self.root, "lobby", [page("lobby", [unsigned(1, "a", ts(1)), unsigned(2, "b", ts(2))])])
        cp_path = next(arch.glob("segment-*/capture.json"))
        cp = json.loads(cp_path.read_text())
        cp_path.chmod(0o600)
        for name, edit in (("archive_bytes", lambda c: c.update(archive_bytes=c["archive_bytes"] + 1)),
                           ("payload_bytes", lambda c: c.update(payload_bytes=c["payload_bytes"] + 1)),
                           ("tip_sha256", lambda c: c.update(tip_sha256="f" * 64))):
            with self.subTest(name):
                bad = dict(cp)
                edit(bad)
                cp_path.write_text(json.dumps(bad))
                result = self.read(arch)
                self.assertEqual([r for u in result.units for r in u.records], [])  # nothing provisional survives
                shard = next(u for u in result.units if u.unit.endswith("shard-000000000001.jsonl"))
                self.assertEqual((shard.status, shard.detail), ("QUARANTINED", "ARCHIVE_CHECKPOINT_MISMATCH"))
                self.assertIn("TIP_CHECKPOINT_MISMATCH", [u.detail for u in result.units])
        cp_path.write_text(json.dumps(cp))
        self.assertEqual(len([r for u in self.read(arch).units for r in u.records]), 2)

    def test_archive_generation_break_across_shards_is_quarantined(self):
        import hashlib
        arch = build_archive(self.root, "lobby", [page("lobby", [unsigned(1, "a", ts(1))])])
        build_archive(self.root, "lobby", [page("lobby", [unsigned(2, "b", ts(2))])])  # 2nd shard, same segment
        seg = next(p for p in arch.iterdir() if p.name.startswith("segment-"))
        shard = seg / "shard-000000000002.jsonl"
        raw = shard.read_bytes()
        header, payload = raw.split(b"\n", 1)
        h = json.loads(header)
        h["generation"] = 99  # internally consistent shard, but breaks the Archive contract
        shard.chmod(0o600)
        new = (json.dumps(h, separators=(",", ":")) + "\n").encode() + payload
        shard.write_bytes(new)
        checkpoint = json.loads((seg / "capture.json").read_text())
        checkpoint["tip_sha256"] = hashlib.sha256(new).hexdigest()
        (seg / "capture.json").chmod(0o600)
        (seg / "capture.json").write_text(json.dumps(checkpoint))
        result = self.read(arch)
        statuses = {u.unit.split("/")[-1]: (u.status, u.detail) for u in result.units}
        self.assertEqual(statuses["shard-000000000002.jsonl"], ("QUARANTINED", "ARCHIVE_CHAIN_FAILURE"))
        self.assertEqual([r.seq for u in result.units for r in u.records], [1])

    def test_tampered_shard_is_quarantined_without_stopping_other_units(self):
        arch = build_archive(self.root, "lobby", [page("lobby", [unsigned(1, "a", ts(1))]),
                                                  page("lobby", [unsigned(3, "c", ts(3))])])
        segs = sorted(p for p in arch.iterdir() if p.name.startswith("segment-"))
        shard = segs[0] / "shard-000000000001.jsonl"
        shard.chmod(0o600)
        shard.write_bytes(shard.read_bytes().replace(b'"a"', b'"x"'))
        result = self.read(arch)
        statuses = sorted(u.status for u in result.units)
        self.assertIn("QUARANTINED", statuses)
        self.assertEqual([r.seq for u in result.units for r in u.records], [3])
        self.assertEqual(result.status, "PARTIAL")

    def test_missing_medium_is_unavailable_not_loss(self):
        result = self.read(self.root / "not-mounted")
        self.assertEqual(result.status, "UNAVAILABLE")
        self.assertEqual(result.reason, "SOURCE_PATH_NOT_AVAILABLE")


class ObserverStateAdapterTests(unittest.TestCase):
    def make_db(self, path, journal="DELETE", version=None, room="lobby", state=True):
        from technocore_observer import storage
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA journal_mode={journal}")
        for statement in storage.SCHEMA:
            conn.execute(statement)
        conn.execute(f"PRAGMA application_id={storage.APPLICATION_ID}")
        conn.execute(f"PRAGMA user_version={storage.SCHEMA_VERSION if version is None else version}")
        msg = {"seq": 7, "ts": ts(7), "from": "nick", "text": "hi"}
        if state:
            conn.execute("INSERT INTO state VALUES(1,?,3,1,1,7,1,'RUNNING',NULL,NULL,NULL,NULL,0,0)", (room,))
        conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("lobby", 1, 3, 7, json.dumps(ts(7)), "nick", "hi", json.dumps(msg), "[]", "poll", 1.0,
                      "untrusted", None, None))
        conn.execute("INSERT INTO gaps VALUES(1,'lobby',1,3,3,6,1.0,'OPEN',NULL)")
        conn.commit()
        conn.close()

    def read(self, path, **extra):
        src = {"id": "lobby-snap", "kind": "observer-state-sqlite", "room": "lobby", "path": str(path), **extra}
        return evidence.adapter_for(src).read({}, None)

    def test_declared_snapshot_reads_messages_and_gaps_without_touching_directory(self):
        for journal in ("DELETE", "WAL"):  # the running Observer writer uses WAL; init uses DELETE
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "state.sqlite"
                self.make_db(path, journal)
                before = tree_digest(Path(d))
                result = self.read(path, snapshot=True)
                self.assertEqual(result.status, "READ", journal)
                self.assertEqual([r.seq for u in result.units for r in u.records], [7])
                self.assertEqual(result.coverage[0].kind, "GAP")
                self.assertEqual(tree_digest(Path(d)), before)  # no -wal/-shm/-journal created

    def test_snapshot_contract_is_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.sqlite"
            self.make_db(path)  # no -wal file, yet nothing proves it is not live
            self.assertEqual((self.read(path).status, self.read(path).reason),
                             ("DEFERRED", "SNAPSHOT_CONTRACT_NOT_DECLARED"))
            for suffix in ("-wal", "-journal", "-shm"):
                side = Path(str(path) + suffix)
                side.write_bytes(b"")
                self.assertEqual(self.read(path, snapshot=True).reason, "SNAPSHOT_HAS_SQLITE_SIDECARS")
                side.unlink()

    def test_wrong_room_snapshot_is_not_a_quiet_room(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.sqlite"
            self.make_db(path, room="kibble")  # state.room says kibble; source is configured as lobby
            result = self.read(path, snapshot=True)
            self.assertEqual((result.status, result.reason), ("DEFERRED", "SNAPSHOT_ROOM_MISMATCH"))
            self.assertEqual(result.units, [])
            self.assertEqual(result.coverage, [])
            path.unlink()
            self.make_db(path, state=False)
            self.assertEqual(self.read(path, snapshot=True).reason, "SNAPSHOT_STATE_MISSING")

    def test_snapshot_rows_of_another_room_are_not_a_quiet_room(self):
        for table in ("messages", "gaps"):
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "state.sqlite"
                self.make_db(path)
                conn = sqlite3.connect(path)
                conn.execute(f"UPDATE {table} SET room='kibble'")  # state.room stays lobby
                conn.commit()
                conn.close()
                result = self.read(path, snapshot=True)
                self.assertEqual((result.status, result.reason, result.units), ("DEFERRED", "SNAPSHOT_ROWS_FOR_OTHER_ROOM", []))

    def test_snapshot_version_and_foreign_file_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.sqlite"
            self.make_db(path, version=1)
            self.assertEqual(self.read(path, snapshot=True).reason, "SNAPSHOT_NOT_OBSERVER_STATE_V2")
            path.unlink()
            path.write_bytes(b"not a database" * 20)
            self.assertEqual(self.read(path, snapshot=True).reason, "SNAPSHOT_NOT_SQLITE")


class SnapshotMalformedRowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        (Path(self.temp.name) / "snap").mkdir()
        self.snap = Path(self.temp.name) / "snap" / "state.sqlite"

    def build(self, raw_values):
        from technocore_observer import storage
        conn = sqlite3.connect(self.snap)
        for statement in storage.SCHEMA:
            conn.execute(statement)
        conn.execute(f"PRAGMA application_id={storage.APPLICATION_ID}")
        conn.execute(f"PRAGMA user_version={storage.SCHEMA_VERSION}")
        conn.execute("INSERT INTO state VALUES(1,?,3,1,1,7,1,'RUNNING',NULL,NULL,NULL,NULL,0,0)", ("lobby",))
        good = {"seq": 1, "ts": ts(1, 1, 12), "from": "nick", "text": "fine"}
        rows = [(1, json.dumps(good))] + [(n, raw) for n, raw in enumerate(raw_values, start=2)]
        for seq, raw in rows:
            conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("lobby", 1, 3, seq, json.dumps(good["ts"]), "nick", "x", raw, "[]", "poll", 1.0,
                          "untrusted", None, None))
        conn.commit()
        conn.close()

    def read(self):
        src = {"id": "s", "kind": "observer-state-sqlite", "room": "lobby", "path": str(self.snap), "snapshot": True}
        return evidence.adapter_for(src).read({}, None)

    def test_non_object_and_invalid_json_rows_are_excluded_and_reported(self):
        malformed = ["[]", '"text"', "123", "null", "true", "{bad", "NaN", '{"seq":NaN}']
        self.build(malformed)
        result = self.read()  # must not raise, and the source is not failed as a whole
        self.assertEqual(result.status, "READ")
        records = [r for u in result.units for r in u.records]
        self.assertEqual([r.seq for r in records], [1])  # only the valid object is Evidence
        self.assertTrue(all(isinstance(r.message, dict) for r in records))
        items = [c for c in result.coverage if c.kind == "ROW_MALFORMED"]
        self.assertEqual(len(items), len(malformed))
        self.assertEqual(len({c.key for c in items}), len(malformed))
        for c in items:
            self.assertIn("excluded, not repaired", c.detail["note"])

    def test_raw_record_seq_must_be_the_row_seq(self):
        self.build([])
        conn = sqlite3.connect(self.snap)
        row = "INSERT INTO messages VALUES('lobby',1,3,?,'null','n','x',?,'[]','poll',1.0,'untrusted',NULL,NULL)"
        base = {"ts": ts(1, 1, 12), "from": "n", "text": "t"}
        conn.execute(row, (2, json.dumps({**base, "seq": 3})))       # names another sequence
        conn.execute(row, (3, json.dumps(base)))                     # omits seq
        conn.execute(row, (4, json.dumps({**base, "seq": 4.0})))     # not an integer
        conn.execute(row, (5, json.dumps({**base, "seq": True})))
        conn.execute(row, (6, json.dumps({**base, "seq": 6})))       # consistent
        conn.commit()
        conn.close()
        read = self.read()
        self.assertEqual(sorted(r.seq for u in read.units for r in u.records), [1, 6])
        self.assertEqual(sum(c.kind == "ROW_MALFORMED" for c in read.coverage), 4)

    def test_mistyped_snapshot_rows_and_gaps_are_excluded(self):
        self.build([])
        conn = sqlite3.connect(self.snap)
        good = json.dumps({"seq": 5, "ts": ts(5, 1, 12), "from": "nick", "text": "ok"})
        conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("lobby", 1, 3, "abc", "null", "n", "x", good, "[]", "poll", 1.0, "untrusted", None, None))
        conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("lobby", 1, 3, 6, "null", "n", "x", good, "[]", "poll", "later", "untrusted", None, None))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(gaps)")]
        values = {"room": "lobby", "observer_epoch": 1, "server_generation": 3, "start_seq": 5, "end_seq": "zzz",
                  "detected_at": 1.0, "status": "OPEN"}
        conn.execute(f"INSERT INTO gaps({','.join(values)}) VALUES({','.join('?' * len(values))})", tuple(values.values()))
        conn.commit()
        conn.close()
        result = self.read()
        self.assertEqual(result.status, "READ")
        self.assertEqual([r.seq for u in result.units for r in u.records], [1])
        kinds = sorted(c.kind for c in result.coverage)
        self.assertEqual(kinds, ["ROW_MALFORMED"] * 3)  # no GAP with a non-integer bound is adopted

    def test_non_json_safe_gap_and_message_values_are_malformed_rows_not_run_failures(self):
        from support import write_config
        from technocore_analyzer import config as config_mod, engine
        from technocore_analyzer.store import Store
        self.build([])
        conn = sqlite3.connect(self.snap)
        conn.execute("PRAGMA ignore_check_constraints=ON")  # a snapshot is not bound by Observer's CHECKs
        gap = ("INSERT INTO gaps(room,observer_epoch,server_generation,start_seq,end_seq,detected_at,status) "
               "VALUES('lobby',1,3,?,?,?,?)")
        conn.execute(gap, (10, 12, 1.5, "OPEN"))                   # normal gap
        conn.execute(gap, (20, 21, b"\x00\x01", "OPEN"))          # BLOB detected_at
        conn.execute(gap, (30, 31, float("inf"), "OPEN"))          # Infinity
        conn.execute(gap, (40, 41, "yesterday", "OPEN"))           # text where a time belongs
        conn.execute(gap, (50, 51, 1.0, b"OPEN"))                  # BLOB status
        row = "INSERT INTO messages VALUES('lobby',1,3,?,'null','n','x',?,'[]','poll',?,'untrusted',NULL,NULL)"
        ok = lambda seq, extra="": '{"seq":%d,"ts":"2026-09-01T12:0%d:00Z","from":"n","text":"t"%s}' % (seq, seq, extra)
        conn.execute(row, (2, ok(2), float("inf")))               # Infinity ingested_at
        conn.execute(row, (3, ok(3, ',"x":1e999'), 1.0))          # overflows to Infinity when parsed
        conn.commit()
        conn.close()
        read = self.read()
        self.assertEqual(read.status, "READ")
        self.assertEqual([r.seq for u in read.units for r in u.records], [1])
        gaps = [c for c in read.coverage if c.kind == "GAP"]
        self.assertEqual([(g.start_seq, g.end_seq) for g in gaps], [(10, 12)])
        self.assertEqual(sum(c.kind == "ROW_MALFORMED" for c in read.coverage), 6)
        root = Path(self.temp.name)
        cfg = config_mod.load(write_config(root / "a.json", sources=[
            {"id": "s", "kind": "observer-state-sqlite", "room": "lobby", "path": str(self.snap), "snapshot": True}]))
        out = engine.run(cfg)  # put_coverage / record persistence must not raise
        self.assertEqual(out["status"], "SUCCEEDED")
        store = Store(cfg["state_db"])
        try:
            kinds = sorted(c["kind"] for c in store.coverage_items())
        finally:
            store.close()
        self.assertEqual(kinds, ["GAP"] + ["ROW_MALFORMED"] * 6)
        advisories = json.loads((Path(out["output"]) / "coverage.json").read_text())["advisories"]
        self.assertEqual(sorted((a["table"], a["rows"]) for a in advisories if a["kind"] == "ROW_MALFORMED"),
                         [("gaps", 4), ("messages", 2)])

    def test_malformed_snapshot_rows_do_not_stop_a_run(self):
        from support import write_config
        from technocore_analyzer import config as config_mod, engine
        self.build(["[]", '"text"', "123", "null"])
        root = Path(self.temp.name)
        cfg = config_mod.load(write_config(root / "a.json", sources=[
            {"id": "s", "kind": "observer-state-sqlite", "room": "lobby", "path": str(self.snap), "snapshot": True}]))
        out = engine.run(cfg)
        self.assertEqual(out["records_cached"], 1)
        run_dir = Path(out["output"])
        sources = json.loads((run_dir / "situation.json").read_text())["sources"]
        self.assertEqual([s["status"] for s in sources], ["READ"])
        advisories = json.loads((run_dir / "coverage.json").read_text())["advisories"]
        malformed = [a for a in advisories if a["kind"] == "ROW_MALFORMED"]
        self.assertEqual([(a["table"], a["rows"]) for a in malformed], [("messages", 4)])


class SignatureTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_CRYPTO, "cryptography required")
    def test_statuses(self):
        key = Key(9)
        good = key.message("lobby", 1, "hello é", ts(1))
        self.assertEqual(signature.verify_record("lobby", good)[0], signature.VALID)
        self.assertEqual(signature.verify_record("other", good)[0], signature.INVALID)  # room is signed
        moved = dict(good, seq=99, ts=ts(9))
        self.assertEqual(signature.verify_record("lobby", moved)[0], signature.VALID)  # seq/ts not signed
        self.assertEqual(signature.verify_record("lobby", {"from": key.did, "text": "x"})[0], signature.MATERIAL_ABSENT)
        self.assertEqual(signature.verify_record("lobby", {"from": "nick", "text": "x"})[0], signature.UNSIGNED)
        self.assertEqual(signature.verify_record("lobby", dict(good, nonce="007"))[0], signature.MALFORMED)
        self.assertEqual(signature.verify_record("lobby", dict(good, sig="short"))[0], signature.MALFORMED)
        self.assertEqual(signature.verify_record("lobby", dict(good, **{"from": "did:web:x"}))[0], signature.UNSUPPORTED)
        self.assertIsNone(signature.attributable_did(signature.INVALID, good))


if __name__ == "__main__":
    unittest.main()


class VerifierUnavailableTests(unittest.TestCase):
    def test_unavailable_is_never_valid_and_is_rechecked_later(self):
        from unittest import mock
        from technocore_analyzer.store import Store
        did = "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"
        msg = {"seq": 1, "ts": ts(1), "from": did, "text": "x", "nonce": 1, "sig": "A" * 86}
        with mock.patch.object(signature, "HAVE_CRYPTO", False):
            self.assertEqual(signature.verify_record("lobby", msg)[0], signature.UNAVAILABLE)
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "a.sqlite")
            rec = evidence.EvidenceRecord("s", "k", "captured", "f", "lobby", "st", 1, 1, 1, msg, "h", "scope",
                                          "s:ref", {}, None, [])
            store.ingest_record(rec, signature.UNAVAILABLE, None, "run1")
            changed = store.recheck_signatures(lambda room, m: ("INVALID", "d"))
            self.assertEqual(changed, 1)
            self.assertEqual(store.records()[0]["sig_status"], "INVALID")
            self.assertEqual(store.ingest_record(rec, "INVALID", None, "run2"), "SAME")
            changed_msg = dict(msg, text="y")
            rec2 = evidence.EvidenceRecord("s", "k", "captured", "f", "lobby", "st", 1, 1, 1, changed_msg, "h2",
                                           "scope", "s:ref2", {}, None, [])
            self.assertEqual(store.ingest_record(rec2, "INVALID", None, "run3"), "CONFLICT")
            self.assertEqual(store.records()[0]["message"]["text"], "x")  # never overwritten
            store.close()
