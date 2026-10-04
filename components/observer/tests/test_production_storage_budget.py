"""Production Observer storage defaults; boundary tests use smaller patched limits."""

import unittest

from test_observer import DatabaseCase
from technocore_observer import storage


class ProductionStorageBudgetTests(unittest.TestCase):
    def test_production_defaults_are_bounded(self):
        self.assertEqual(storage.MAX_DB_BYTES, 10 * 1024**3)
        self.assertEqual(storage.MAX_WAL_BYTES, 128 * 1024**2)
        self.assertEqual(storage.MAX_EVIDENCE_BYTES, 128 * 1024**2)
        self.assertEqual(storage.MAX_EVIDENCE_ROWS, 65_536)
        self.assertEqual(storage.MAX_EVENT_ROWS, 262_144)
        self.assertEqual(storage.MIN_DISK_FREE, 10 * 1024**3)
        self.assertEqual(storage.STORAGE_WARNING_UTILIZATION, 0.8)
        self.assertEqual(storage.EVIDENCE_PREFIX_BYTES, 16 * 1024)


class ProductionPageLimitTests(unittest.TestCase):
    setUp = DatabaseCase.setUp

    def test_default_is_sqlite_page_ceiling_without_allocating_ten_gib(self):
        page_size = self.store.conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = self.store.conn.execute("PRAGMA page_count").fetchone()[0]
        max_pages = self.store.conn.execute("PRAGMA max_page_count").fetchone()[0]
        self.assertEqual(max_pages, storage.MAX_DB_BYTES // page_size)
        self.assertLess(page_count * page_size, storage.MAX_DB_BYTES)
