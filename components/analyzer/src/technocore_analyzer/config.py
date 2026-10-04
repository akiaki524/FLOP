"""Analyzer configuration.

The observed-room list is not canonical here: rooms and archive locations are derived
from the Observer's own config files (read-only). ``sources`` may add inputs the Observer
config cannot express (e.g. an Online-Backup snapshot of the lobby ``state.sqlite``).
``path_map`` rewrites an Observer path prefix to where a transferred copy is mounted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .util import AnalyzerError, digest

DEFAULTS = {
    "window_hours": 24,
    "interests": {"keywords": [], "exclude": []},
    "rules": {"rate": {"min_count": 20, "ratio": 5.0, "baseline_windows": 7},
              "duplicate": {"min_repeats": 5, "min_chars": 16},
              "cross_room": {"min_rooms": 3}},
    "event_profiles": [],
    "llm": {"runtime": "none", "allowed_rooms": [], "max_invocations_per_day": 0,
            "max_invocations_per_run": 0, "max_attempts_per_task": 2, "sample_size": 20,
            "bundle_limits": {"max_items": 20, "max_item_chars": 600, "max_input_chars": 12000}},
    "limits": {"max_new_records_per_run": 500000, "nice": 10},
}


def _merge(base, extra):
    out = dict(base)
    for key, value in extra.items():
        out[key] = _merge(base[key], value) if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return out


def _mapped(path, path_map):
    for original, replacement in path_map.items():
        if path == original or path.startswith(original.rstrip("/") + "/"):
            return replacement.rstrip("/") + path[len(original.rstrip("/")):]
    return path


def _real(path):
    return Path(os.path.realpath(path))


def _inside(a, b):
    return a == b or b in a.parents


def check_write_boundary(config, protected):
    """Analyzer writes only its own state/output (fail closed, before anything is opened).

    The state DB and its directory (WAL/SHM sidecars) must not be, or be inside, an Evidence
    source or Observer root; the output directory must not overlap one in either direction.
    """
    state_db, state_dir = _real(config["state_db"]), _real(Path(config["state_db"]).parent)
    output = _real(config["output_dir"])
    for item in map(_real, protected):
        if _inside(state_db, item) or _inside(state_dir, item):
            raise AnalyzerError("WRITE_BOUNDARY_STATE_DB_INSIDE_EVIDENCE")
        if _inside(output, item) or _inside(item, output):
            raise AnalyzerError("WRITE_BOUNDARY_OUTPUT_OVERLAPS_EVIDENCE")
    if _inside(state_db, output):
        raise AnalyzerError("WRITE_BOUNDARY_STATE_DB_INSIDE_OUTPUT")


def derive_from_observer(entry, base_dir):
    path = Path(entry["path"])
    if not path.is_absolute():
        path = base_dir / path
    with open(path, encoding="utf-8") as file:
        data = json.load(file)
    kind, prefix = entry.get("kind"), entry.get("source_prefix", "observer")
    path_map, provenance = entry.get("path_map", {}), entry.get("provenance", "captured")
    rooms, sources = {}, []
    roots = []
    if kind == "multi-room":
        roots.append(_mapped(data["root"], path_map))
        roots.append(data["root"])
        for room in data["rooms"]:
            rooms[room["room"]] = {"evidence": room.get("evidence"), "capture_owner": room.get("capture_owner"),
                                   "from_observer_config": str(entry["path"])}
            if room.get("capture_owner") == "local":
                archive = f"{data['root'].rstrip('/')}/{room['room']}/archive"
                sources.append({"id": f"{prefix}-{room['room']}", "kind": "full-capture-archive",
                                "room": room["room"], "path": _mapped(archive, path_map),
                                "provenance": provenance, "read_manifest": entry.get("read_manifest", False)})
    elif kind == "production":
        # The scheduler's control and budget stores are Observer-owned too.
        for key in ("control_dir", "budget_dir"):
            if isinstance(data.get(key), str):
                roots.extend([data[key], _mapped(data[key], path_map)])
        for room in data["rooms"]:
            rooms[room["room"]] = {"class": room.get("class"), "from_observer_config": str(entry["path"])}
            archive = room["archive_dir"]
            spool = room.get("spool_dir") or archive
            roots.extend([archive, spool, _mapped(archive, path_map), _mapped(spool, path_map)])
            sources.append({"id": f"{prefix}-{room['room']}", "kind": "full-capture-archive",
                            "room": room["room"], "path": _mapped(archive, path_map),
                            "provenance": provenance, "read_manifest": entry.get("read_manifest", False)})
    else:
        raise AnalyzerError("UNSUPPORTED_OBSERVER_CONFIG_KIND")
    return rooms, sources, roots


def load(path):
    path = Path(path)
    with open(path, encoding="utf-8") as file:
        raw = json.load(file)
    config = _merge(DEFAULTS, raw)
    base = path.parent
    rooms, sources, protected = {}, [], []
    for entry in config.get("observer_configs", []):
        r, s, roots = derive_from_observer(entry, base)
        rooms.update(r)
        sources.extend(s)
        protected.extend(roots)
    for extra in config.get("sources", []):
        if extra["kind"] not in ("full-capture-archive", "observer-state-sqlite"):
            raise AnalyzerError("UNSUPPORTED_SOURCE_KIND")
        extra = dict(extra)
        if not Path(extra["path"]).is_absolute():
            extra["path"] = str(base / extra["path"])
        sources.append(extra)
        rooms.setdefault(extra["room"], {"from_observer_config": None, "note": "explicit source only"})
    ids = [s["id"] for s in sources]
    if len(ids) != len(set(ids)):
        raise AnalyzerError("DUPLICATE_SOURCE_ID")
    for key in ("state_db", "output_dir"):
        if key not in config:
            raise AnalyzerError(f"CONFIG_MISSING_{key.upper()}")
        if not Path(config[key]).is_absolute():
            config[key] = str(base / config[key])
    for src in sources:
        # A snapshot file's directory is Observer territory too (no sidecars/outputs there).
        protected.append(src["path"] if src["kind"] == "full-capture-archive" else str(Path(src["path"]).parent))
    # Relative entries mean the same thing as state_db/output_dir: relative to the config file.
    protected.extend(str(base / p) if not Path(p).is_absolute() else p for p in config.get("protected_paths", []))
    check_write_boundary(config, protected)
    llm = config["llm"]
    if llm.get("runtime") == "fixture" and llm.get("fixture_dir") and not Path(llm["fixture_dir"]).is_absolute():
        llm["fixture_dir"] = str(base / llm["fixture_dir"])
    config["rooms"] = rooms
    config["resolved_sources"] = sources
    config["config_sha256"] = digest({k: v for k, v in config.items() if k != "config_sha256"})
    return config
