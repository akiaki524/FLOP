"""Offline recovery storage/capacity tests; no HTTP or production access."""

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from technocore_full_capture.archive import MAX_LINE
from technocore_full_capture.spool import (
    MAX_RECOVERY_STAGING_BYTES, Spool, canonical,
)
from technocore_observer.protocol import ObserverError


def message(seq, payload="fixture"):
    return {"seq": seq, "text": payload, "from": "fixture"}


class RecoveryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="capture-recovery-storage-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spool_dir = self.root / "spool"
        self.spool_dir.mkdir()

    def open_spool(self, **kwargs):
        defaults = {"min_free_bytes": 0, "max_db_bytes": 64 * 1024 * 1024,
                    "max_wal_bytes": 64 * 1024 * 1024}
        defaults.update(kwargs)
        spool = Spool(self.spool_dir, "test-room", producer=True, create=True, **defaults)
        self.addCleanup(spool.close)
        return spool

    def stage(self, records):
        directory = tempfile.TemporaryDirectory(dir=self.spool_dir, prefix=".recovery-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "normalized.ndjson"
        raw = b"".join((canonical(item) + "\n").encode("ascii") for item in records)
        path.write_bytes(raw)
        return path, raw

    def ingest(self, spool, path, raw, first, last, **kwargs):
        return spool.ingest_recovery(
            path, generation=1, request_epoch=1, request_since=first - 1,
            last_seq=last, count=last - first + 1,
            response_sha256=hashlib.sha256(raw).hexdigest(), **kwargs)

    def test_limit_is_runtime_derived_and_below_container_memory_fraction(self):
        spool = self.open_spool()
        self.assertLessEqual(spool.recovery_limit(), MAX_RECOVERY_STAGING_BYTES)
        self.assertEqual(MAX_RECOVERY_STAGING_BYTES, 8 * 1024 * 1024)

    def test_capacity_insufficient_fails_before_recovery(self):
        spool = self.open_spool(max_db_bytes=1024 * 1024)
        with self.assertRaisesRegex(ObserverError, "RECOVERY_CAPACITY_INSUFFICIENT"):
            spool.recovery_limit()

    def test_live_free_space_and_reserve_bound_limit(self):
        spool = self.open_spool()
        stat = os.statvfs(self.spool_dir)
        fake = type(stat)((stat.f_bsize, stat.f_frsize, stat.f_blocks, stat.f_bfree,
                           1, stat.f_files, stat.f_ffree, stat.f_favail,
                           stat.f_flag, stat.f_namemax))
        with patch("technocore_full_capture.spool.os.statvfs", return_value=fake):
            with self.assertRaisesRegex(ObserverError, "RECOVERY_CAPACITY_INSUFFICIENT"):
                spool.recovery_limit()

    def test_live_wal_headroom_bounds_limit(self):
        spool = self.open_spool(max_wal_bytes=4 * 1024 * 1024)
        real_fstat = os.fstat
        def near_full(fd):
            result = real_fstat(fd)
            values = list(result)
            values[6] = spool.max_wal_bytes - MAX_LINE
            return os.stat_result(values)
        with patch("technocore_full_capture.spool.os.fstat", side_effect=near_full):
            with self.assertRaisesRegex(ObserverError, "RECOVERY_CAPACITY_INSUFFICIENT"):
                spool.recovery_limit()

    def test_streamed_ingest_is_atomic_and_uses_spool_local_staging(self):
        spool = self.open_spool()
        spool.ingest({"room": "test-room", "generation": 1, "count": 1,
                      "messages": [message(1)], "first_seq": 1, "last_seq": 1})
        path, raw = self.stage([message(seq) for seq in range(2, 502)])
        self.assertEqual(path.parents[1], self.spool_dir)
        result = self.ingest(spool, path, raw, 2, 501)
        self.assertEqual(result["saved"], 500)
        self.assertEqual(spool.state()["cursor"], 501)
        self.assertEqual(spool.state()["gaps"], 0)
        self.assertEqual(spool.conn.execute(
            "SELECT disposition FROM batches ORDER BY batch_id DESC LIMIT 1").fetchone()[0],
                         "RECOVERED_EXPORT")

    def test_hash_or_sequence_failure_rolls_back_whole_batch(self):
        for records, sha in (([message(2), message(4)], None),
                             ([message(2), message(3)], "0" * 64)):
            with self.subTest(records=records, sha=sha):
                # A fresh directory is needed because each Spool owns one DB.
                child = self.root / ("case-%d" % records[-1]["seq"])
                child.mkdir()
                spool = Spool(child, "test-room", producer=True, create=True,
                              min_free_bytes=0, max_db_bytes=64 * 1024 * 1024,
                              max_wal_bytes=64 * 1024 * 1024)
                self.addCleanup(spool.close)
                spool.ingest({"room": "test-room", "generation": 1, "count": 1,
                              "messages": [message(1)], "first_seq": 1, "last_seq": 1})
                directory = tempfile.TemporaryDirectory(dir=child, prefix=".recovery-")
                self.addCleanup(directory.cleanup)
                path = Path(directory.name) / "normalized.ndjson"
                raw = b"".join((canonical(item) + "\n").encode() for item in records)
                path.write_bytes(raw)
                before = spool.state()
                digest = sha or hashlib.sha256(raw).hexdigest()
                with self.assertRaises(ObserverError):
                    spool.ingest_recovery(path, generation=1, request_epoch=1,
                                          request_since=1, last_seq=3, count=2,
                                          response_sha256=digest)
                self.assertEqual(spool.state(), before)
                self.assertEqual(spool.conn.execute("SELECT count(*) FROM batches").fetchone()[0], 1)

    def test_stale_generation_and_request_are_rejected_without_mutation(self):
        spool = self.open_spool()
        spool.ingest({"room": "test-room", "generation": 1, "count": 1,
                      "messages": [message(1)], "first_seq": 1, "last_seq": 1})
        path, raw = self.stage([message(2)])
        before = spool.state()
        for generation, epoch, since, code in (
                (2, 1, 1, "SPOOL_STALE_RECOVERY"),
                (1, 2, 1, "SPOOL_STALE_REQUEST"),
                (1, 1, 0, "SPOOL_STALE_REQUEST")):
            with self.subTest(code=code), self.assertRaisesRegex(ObserverError, code):
                spool.ingest_recovery(path, generation=generation, request_epoch=epoch,
                                      request_since=since, last_seq=since + 1, count=1,
                                      response_sha256=hashlib.sha256(raw).hexdigest())
            self.assertEqual(spool.state(), before)

    def test_oversized_staging_is_rejected_at_derived_limit(self):
        spool = self.open_spool()
        spool.ingest({"room": "test-room", "generation": 1, "count": 1,
                      "messages": [message(1)], "first_seq": 1, "last_seq": 1})
        path, raw = self.stage([message(2)])
        with patch.object(spool, "recovery_limit", return_value=len(raw) - 1):
            with self.assertRaisesRegex(ObserverError, "RECOVERY_NORMALIZED_LIMIT"):
                self.ingest(spool, path, raw, 2, 2)

    def test_maximum_boundary_is_accepted_without_whole_file_read(self):
        spool = self.open_spool()
        spool.ingest({"room": "test-room", "generation": 1, "count": 1,
                      "messages": [message(1)], "first_seq": 1, "last_seq": 1})
        path, raw = self.stage([message(seq, "x" * 512) for seq in range(2, 2002)])
        # If ingest used read()/read_bytes(), this wrapper would fail. readline
        # is bounded to MAX_LINE+1 and holds one canonical record at a time.
        class BoundedReader:
            def __init__(self, wrapped): self.wrapped = wrapped
            def __enter__(self): return self
            def __exit__(self, *args): self.wrapped.close()
            def readline(self, size):
                self_case.assertLessEqual(size, MAX_LINE + 1)
                return self.wrapped.readline(size)
        self_case = self
        from technocore_full_capture import spool as spool_module
        regular = spool_module.regular_open
        def bounded_open(candidate, flags):
            wrapped = regular(candidate, flags)
            return BoundedReader(wrapped) if flags == os.O_RDONLY else wrapped
        with patch.object(spool_module, "regular_open", side_effect=bounded_open), \
                patch.object(spool, "recovery_limit", return_value=len(raw)):
            self.ingest(spool, path, raw, 2, 2001)
        self.assertEqual(spool.state()["cursor"], 2001)


if __name__ == "__main__":
    unittest.main()
