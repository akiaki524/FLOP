"""The Multi-Room image entrypoint rejects an unreviewed loaded library."""

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from technocore_full_capture import sqlite_runtime


class SQLiteRuntimeTest(unittest.TestCase):
    def test_runtime_checks_source_id_and_actual_loaded_library(self):
        source = sqlite3.connect(":memory:").execute("select sqlite_source_id()").fetchone()[0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library = root / "libsqlite3.so.0"
            library.touch()
            maps = root / "maps"
            maps.write_text(f"7f00-7f01 r-xp 00000000 00:00 0 {library}\n")
            with patch.object(sqlite_runtime, "EXPECTED_VERSION", sqlite3.sqlite_version), \
                 patch.object(sqlite_runtime, "EXPECTED_SOURCE_ID", source), \
                 patch.object(sqlite_runtime, "LIBRARY_ROOT", root):
                self.assertEqual(sqlite_runtime.verify_runtime(maps)["loaded_library"], str(library))
                with patch.object(sqlite_runtime, "EXPECTED_SOURCE_ID", "wrong source"):
                    with self.assertRaisesRegex(RuntimeError, "MULTI_SQLITE_BUILD_UNVERIFIED"):
                        sqlite_runtime.verify_runtime(maps)
                maps.write_text("7f00-7f01 r-xp 00000000 00:00 0 /usr/lib/libsqlite3.so.0\n")
                with self.assertRaisesRegex(RuntimeError, "MULTI_SQLITE_BUILD_UNVERIFIED"):
                    sqlite_runtime.verify_runtime(maps)

    def test_preflight_fails_before_multi_room_import(self):
        with patch.object(sqlite_runtime, "verify_runtime", side_effect=RuntimeError("bad")):
            self.assertEqual(sqlite_runtime.main(["run", "--config", "unused"]), 2)


if __name__ == "__main__":
    unittest.main()
