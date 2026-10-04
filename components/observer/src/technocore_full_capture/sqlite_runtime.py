"""Fail closed before Multi-Room opens state unless Python loaded reviewed SQLite."""

import json
from pathlib import Path
import sqlite3
import sys

EXPECTED_VERSION = "3.53.4"
EXPECTED_SOURCE_ID = (
    "2026-07-24 19:02:57 "
    "bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc"
)
LIBRARY_ROOT = Path("/opt/sqlite/lib")


def loaded_libraries(maps_text):
    """Report actual process mappings, including an unexpected second SQLite."""
    return sorted({line.split()[-1] for line in maps_text.splitlines()
                   if "/libsqlite3.so" in line and len(line.split()) >= 6})


def verify_runtime(maps_path=Path("/proc/self/maps")):
    with sqlite3.connect(":memory:") as conn:
        source_id = conn.execute("select sqlite_source_id()").fetchone()[0]
    libraries = loaded_libraries(maps_path.read_text())
    expected_root = LIBRARY_ROOT.resolve()
    if (sqlite3.sqlite_version != EXPECTED_VERSION or source_id != EXPECTED_SOURCE_ID
            or len(libraries) != 1
            or not Path(libraries[0]).resolve().is_relative_to(expected_root)):
        raise RuntimeError("MULTI_SQLITE_BUILD_UNVERIFIED")
    return {"sqlite_version": sqlite3.sqlite_version, "sqlite_source_id": source_id,
            "loaded_library": libraries[0]}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        provenance = verify_runtime()
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"SQLite preflight failed: {exc}", file=sys.stderr, flush=True)
        return 2
    if args == ["--sqlite-provenance"]:
        print(json.dumps(provenance, sort_keys=True), flush=True)
        return 0
    from .multi_room import main as multi_room_main
    return multi_room_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
