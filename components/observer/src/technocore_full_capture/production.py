"""Limited-production lifecycle around Capture-first. Import/plan never network.

Directories and isolation are operator-provisioned; no install, mount, deletion,
automatic restore or deployment is performed here. Only `run capture` can GET.
"""

import argparse
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import threading
import time

from .archive import regular_open, sync_directory
from .capture_first import FastCapture, emit
from .deadline import DeadlineClient
from .pacing import BudgetPolicy, GracefulStop
from .scheduling import ReservationBudget
from .spool import Spool, canonical, digest, require
from .spool_archive import ArchiveWorker, validate_source
from technocore_observer.protocol import ObserverError, validate_room

MIB = 1024 ** 2
ALLOWANCE = 64 * MIB
HISTORY_SEGMENT_BYTES = 64 * MIB  # Provisional engineering parameter, not a safety boundary.
HISTORY_RECORD_LIMIT = 4096
HISTORY_INTERVAL = 30
LATENCY_BOUNDS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 20, 25, 30)


@contextmanager
def operation(stage):
    """Attach a static operation name, never an exception message or path."""
    try:
        yield
    except (ObserverError, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        if not hasattr(exc, "production_stage"):
            exc.production_stage = stage
        raise


def diagnostic(exc, stage):
    stage = getattr(exc, "production_stage", stage)
    sqlite_name = getattr(exc, "sqlite_errorname", None)
    # SQLite supplies these constants; never serialize a driver exception body.
    if sqlite_name not in vars(sqlite3) or not str(sqlite_name).startswith("SQLITE_"):
        sqlite_name = None
    if isinstance(exc, sqlite3.Error):
        if sqlite_name == "SQLITE_READONLY_ROLLBACK":
            code = "PRODUCTION_MANIFEST_RECOVERY_REQUIRED" if stage == "manifest_read" else "PRODUCTION_SQLITE_RECOVERY_REQUIRED"
        elif getattr(exc, "sqlite_errorcode", 0) & 255 == sqlite3.SQLITE_BUSY:
            code = "PRODUCTION_MANIFEST_BUSY" if stage == "manifest_read" else "PRODUCTION_SQLITE_BUSY"
        else:
            code = "PRODUCTION_MANIFEST_SQLITE" if stage == "manifest_read" else "PRODUCTION_SQLITE_FAILURE"
    elif isinstance(exc, OSError):
        code = "PRODUCTION_FILESYSTEM_FAILURE"
    elif stage in ("config_load", "config_validate"):
        code = "PRODUCTION_CONFIG_FAILURE"
    else:
        code = "PRODUCTION_STATE_FAILURE"
    # Only explicitly known internal codes may escape ObserverError. A regex
    # alone would also admit uppercase credentials/private content.
    known = {"PRODUCTION_IDENTITY_FENCE", "PRODUCTION_SPOOL_IDENTITY", "PRODUCTION_STALE_SPOOL",
             "PRODUCTION_SOURCE_DIVERGED", "PRODUCTION_ARCHIVE_IDENTITY", "PRODUCTION_CRASH_LOOP",
             "PRODUCTION_RESTART_BACKOFF", "PRODUCTION_CLOCK_ROLLBACK", "PRODUCTION_INSTANCE_ALREADY_RUNNING",
             "PRODUCTION_STORAGE_STOP", "PRODUCTION_CONTROL_STORAGE_STOP", "PRODUCTION_RUNWAY_STOP",
             "PRODUCTION_OUTSIDE_WINDOW", "PRODUCTION_HISTORY_RECORD_LIMIT",
             "PRODUCTION_MANIFEST_HEALTH", "PRODUCTION_SERVICE",
             "MANIFEST_SOURCE_MISMATCH", "MANIFEST_AHEAD_OF_SOURCE", "MANIFEST_SOURCE_DIVERGED",
             "MANIFEST_PENDING_SOURCE", "MANIFEST_ACK_RECEIPT_MISMATCH", "CAPTURE_STOP_REQUESTED"}
    if isinstance(exc, ObserverError) and str(exc) in known:
        code = str(exc)
    return {"service_status": "STOPPED", "error": code, "error_code": code,
            "exception_class": type(exc).__name__, "sqlite_errorname": sqlite_name, "stage": stage}


class MetricsHistory:
    """Append-only segments on the control volume; caller holds the role lock."""
    def __init__(self, registry, r, role):
        self.base = registry.path / (r["room"] + "." + role + ".metrics.jsonl")
        # The legacy JSONL is segment zero. Never rename or replace old files.
        pattern = re.compile(re.escape(self.base.name) + r"\.([0-9]{6,})")
        with operation("metrics_history"):
            self.segment = max((int(match[1]) for path in self.base.parent.iterdir()
                                if (match := pattern.fullmatch(path.name))), default=0)
        self.path = self.segment_path(self.segment)
        self.last = None

    def segment_path(self, segment):
        return self.base if segment == 0 else self.base.with_name(self.base.name + f".{segment:06d}")

    def append(self, metrics, *, force=False):
        now = time.monotonic()
        if not force and self.last is not None and now - self.last < HISTORY_INTERVAL:
            return
        # Fixed numeric fields only: exclude step details, payload and paths.
        keys = ("observed_at", "sampled_at", "process_start_count", "process_restart_count", "processed_through",
                "archive_lag_entries", "archive_lag_seconds", "db_bytes", "logical_db_bytes", "wal_bytes",
                "disk_free_bytes", "free_inodes", "control_volume_bytes", "control_free_bytes", "control_free_inodes",
                "planning_runway_seconds", "last_successful_response",
                "deadline_failures_process", "http_429_process", "budget_wait_seconds_process", "requests_process",
                "recovery_attempts_process", "recovery_successes_process", "recovery_failures_process",
                "recovery_export_seconds", "recovery_duration_seconds", "recovery_cooldown_remaining_seconds",
                "recovery_export_limit_bytes", "recovery_export_content_length_bytes", "recovery_export_received_bytes")
        record = {k: metrics[k] for k in keys if k in metrics and
                  (metrics[k] is None or type(metrics[k]) in (int, float))}
        record.update(version=1, room=metrics["room"], role=metrics["role"], service_status=metrics["service_status"])
        record["producer"] = {k: metrics["producer"][k] for k in ("messages", "gaps", "cursor", "high_entry")}
        if "get_latency_histogram_process" in metrics:
            record["get_latency_histogram_process"] = metrics["get_latency_histogram_process"]
        record["storage"] = {k: metrics["storage"][k] for k in
                             ("volume_bytes", "disk_free_bytes", "free_inodes", "planning_runway_seconds", "db_bytes", "wal_bytes")
                             if type(metrics.get("storage", {}).get(k)) in (int, float)}
        raw = (canonical(record) + "\n").encode("ascii")
        require(len(raw) + 1 <= HISTORY_RECORD_LIMIT, "PRODUCTION_HISTORY_RECORD_LIMIT")
        with operation("metrics_history"), ExitStack() as stack:
            flags = os.O_RDWR | os.O_APPEND | (os.O_CREAT if self.segment == 0 else 0)
            file = stack.enter_context(regular_open(self.path, flags))
            size = os.fstat(file.fileno()).st_size
            # Separate a torn final line without rewriting a single old byte.
            separator = b"\n" if size and os.pread(file.fileno(), 1, size - 1) != b"\n" else b""
            if size and size + len(separator) + len(raw) > HISTORY_SEGMENT_BYTES:
                # Exclusive creation fails closed on collisions. A crash before
                # the first record leaves an empty segment restart can reuse.
                next_path = self.segment_path(self.segment + 1)
                file = stack.enter_context(regular_open(next_path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_APPEND))
                self.segment += 1
                self.path = next_path
                separator = b""
            file.write(separator + raw)
            file.flush()
            os.fsync(file.fileno())
            sync_directory(self.base.parent)
        self.last = now


def number(value, minimum, maximum):
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def fields(value, names):
    require(isinstance(value, dict) and set(value) == set(names.split()), "PRODUCTION_CONFIG_FIELDS")


def capacity_model(room):
    s = room["storage"]
    n = room["planned_messages_sec"] * 86400
    return {"spool_growth_bytes_sec": room["planned_messages_sec"] * s["spool_bytes_message"] * 2,
            "archive_growth_bytes_sec": room["planned_messages_sec"] * s["archive_bytes_message"] * 2,
            "spool_required_bytes": math.ceil(n * s["spool_bytes_message"] * 2) + s["wal_bytes"] + s["reserve_bytes"] + ALLOWANCE,
            "archive_required_bytes": math.ceil(n * s["archive_bytes_message"] * 2) + s["reserve_bytes"] + ALLOWANCE}


def validate_config(c):
    names = "version production_id control_dir budget_dir start_at end_at deadline deletion_enabled budget clients rooms"
    continuous = c.get("version") == 2
    fields(c, names + (" observation_checkpoint_seconds" if continuous else ""))
    require(type(c["version"]) is int and c["version"] in (1, 2) and c["deadline"] == 30 and c["deletion_enabled"] is False,
            "PRODUCTION_POLICY")
    validate_room(c["production_id"])
    require(number(c["start_at"], 1, 1e12), "PRODUCTION_WINDOW")
    if continuous:
        require(c["end_at"] is None and type(c["observation_checkpoint_seconds"]) is int
                and c["observation_checkpoint_seconds"] == 86400, "PRODUCTION_OBSERVATION_CHECKPOINT")
    else:
        require(number(c["end_at"], 1, 1e12) and 0 < c["end_at"] - c["start_at"] <= 86400,
                "PRODUCTION_WINDOW")
    fields(c["budget"], "read_limit observer_reserve headroom capture_rpm max_waiters")
    b = c["budget"]
    require(all(number(b[k], 1, 100000) for k in
                ("read_limit", "observer_reserve", "headroom", "capture_rpm")), "PRODUCTION_BUDGET")
    BudgetPolicy(b["read_limit"], b["observer_reserve"], b["headroom"], b["capture_rpm"])
    require(type(b["max_waiters"]) is int and 1 <= b["max_waiters"] <= 4, "PRODUCTION_WAITERS")
    require(isinstance(c["clients"], list) and c["clients"], "PRODUCTION_CLIENT_INVENTORY")
    for client in c["clients"]:
        fields(client, "name rpm waiters")
        validate_room(client["name"])
        require(number(client["rpm"], 0, b["observer_reserve"]) and type(client["waiters"]) is int
                and 0 <= client["waiters"] <= b["max_waiters"], "PRODUCTION_CLIENT_ALLOCATION")
    require(len({x["name"] for x in c["clients"]}) == len(c["clients"])
            and sum(x["rpm"] for x in c["clients"]) <= b["observer_reserve"], "PRODUCTION_RESERVE")
    require(isinstance(c["rooms"], list) and 1 <= len(c["rooms"]) <= 2, "PRODUCTION_SELECT_ROOMS")
    require(len(c["rooms"]) + sum(x["waiters"] for x in c["clients"]) <= b["max_waiters"], "PRODUCTION_WAITERS")
    paths = [c["control_dir"], c["budget_dir"]]
    for r in c["rooms"]:
        fields(r, "room class interval max_rpm spool_dir archive_dir planned_messages_sec storage")
        validate_room(r["room"])
        require(r["class"] in ("high", "low") and number(r["interval"], 0.1, 60)
                and number(r["max_rpm"], 1, b["capture_rpm"])
                and number(r["planned_messages_sec"], 0.001, 10000), "PRODUCTION_ROOM_POLICY")
        s = r["storage"]
        fields(s, "spool_volume_bytes archive_volume_bytes db_bytes wal_bytes reserve_bytes min_inodes spool_bytes_message archive_bytes_message")
        require(all(type(v) is int and v > 0 for v in s.values()), "PRODUCTION_STORAGE_POLICY")
        require(s["wal_bytes"] >= 4 * MIB and s["reserve_bytes"] >= 512 * MIB
                and s["min_inodes"] >= 4096, "PRODUCTION_STORAGE_RESERVE")
        model = capacity_model(r)
        require(s["spool_volume_bytes"] >= model["spool_required_bytes"]
                and s["archive_volume_bytes"] >= model["archive_required_bytes"]
                and s["db_bytes"] >= model["spool_required_bytes"] - s["reserve_bytes"] - s["wal_bytes"]
                and s["db_bytes"] + s["wal_bytes"] + s["reserve_bytes"] + ALLOWANCE <= s["spool_volume_bytes"],
                "PRODUCTION_CAPACITY_MODEL")
        paths.extend((r["spool_dir"], r["archive_dir"]))
    require(len({r["room"] for r in c["rooms"]}) == len(c["rooms"])
            and sum(r["class"] == "high" for r in c["rooms"]) <= 1
            and sum(r["max_rpm"] for r in c["rooms"]) <= b["capture_rpm"], "PRODUCTION_ROOM_ALLOCATION")
    for p in paths:
        require(isinstance(p, str) and Path(p).is_absolute() and str(Path(p)) == p
                and re.fullmatch(r"/[A-Za-z0-9_./-]+", p) is not None
                and ".." not in Path(p).parts and p != "/", "PRODUCTION_PATH")
    require(all(Path(a) not in Path(b).parents for a in paths for b in paths if a != b)
            and len(set(paths)) == len(paths), "PRODUCTION_PATH_OVERLAP")
    return c


def load(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "PRODUCTION_DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    with regular_open(Path(path), os.O_RDONLY) as file:
        return json.load(file, object_pairs_hook=unique)


def atomic(path, document):
    path = Path(path)
    pending = path.with_name(path.name + ".pending")
    with regular_open(pending, os.O_CREAT | os.O_WRONLY | os.O_TRUNC) as file:
        file.write((canonical(document) + "\n").encode("ascii"))
        file.flush()
        os.fsync(file.fileno())
    os.replace(pending, path)
    sync_directory(path.parent)


def private_directory(path):
    p = Path(path)
    require(p.resolve() == p and p.is_dir(), "PRODUCTION_DIRECTORY")
    st = p.stat()
    require(st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o700, "PRODUCTION_OWNERSHIP")
    return p


@contextmanager
def lock(path):
    with regular_open(Path(path), os.O_CREAT | os.O_WRONLY) as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ObserverError("PRODUCTION_INSTANCE_ALREADY_RUNNING") from exc
        yield


def spool_open(r, *, producer=False, create=False):
    return Spool(r["spool_dir"], r["room"], producer=producer, create=create,
                 min_free_bytes=r["storage"]["reserve_bytes"], max_db_bytes=r["storage"]["db_bytes"],
                 max_wal_bytes=r["storage"]["wal_bytes"])


@contextmanager
def manifest_open(r):
    with operation("manifest_read"), manifest_connection(r, "ro") as conn:
        yield conn


@contextmanager
def manifest_connection(r, mode):
    path = Path(r["archive_dir"]) / "manifest.sqlite"
    with regular_open(path, os.O_RDONLY):
        pass
    conn = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, isolation_level=None, timeout=0.25)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def recover_manifest(r):
    # Only Archive authority calls this. Hold the same lock as ArchiveWorker;
    # SQLite's rw read recovers a hot rollback journal, with no manual deletion.
    with operation("archive_recovery"), lock(Path(r["archive_dir"]) / "archive-worker.lock"):
        with manifest_connection(r, "rw") as conn:
            require(conn.execute("PRAGMA quick_check").fetchone()[0] == "ok", "PRODUCTION_MANIFEST_HEALTH")


def storage_probe(r, role, *, enforce_boundary=True):
    key = "spool" if role == "capture" else "archive"
    path, s = Path(r[key + "_dir"]), r["storage"]
    info = os.statvfs(path)
    total, free = info.f_blocks * info.f_frsize, info.f_bavail * info.f_frsize
    if enforce_boundary:
        require(path.stat().st_dev != Path("/").stat().st_dev
                and Path(r["spool_dir"]).stat().st_dev != Path(r["archive_dir"]).stat().st_dev,
                "PRODUCTION_DEDICATED_FILESYSTEM_REQUIRED")
        require(total <= s[key + "_volume_bytes"], "PRODUCTION_UNBOUNDED_VOLUME")
    require(free >= s["reserve_bytes"] + ALLOWANCE and info.f_favail >= s["min_inodes"],
            "PRODUCTION_STORAGE_STOP")
    growth = capacity_model(r)[key + "_growth_bytes_sec"]
    runway = (free - s["reserve_bytes"] - ALLOWANCE) / growth
    result = {"volume_bytes": total, "disk_free_bytes": free, "free_inodes": info.f_favail,
              "planning_runway_seconds": max(0, runway)}
    if role == "capture":
        result.update(db_bytes=file_size(path / "spool.sqlite"), wal_bytes=file_size(path / "spool.sqlite-wal"))
    return result


def file_size(path):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def binding(c):
    st = Path(c["budget_dir"]).stat()
    return {"production_id": c["production_id"], "config_sha256": digest(canonical(c)),
            "budget_path": c["budget_dir"], "budget_device": st.st_dev, "budget_inode": st.st_ino}


def control_capacity(c, *, active_budget=False):
    """Metadata/budget WAL must not have an unbounded host-root volume either."""
    root_device = Path("/").stat().st_dev
    for key in ("control_dir", "budget_dir"):
        path = Path(c[key])
        info = os.statvfs(path)
        require(path.stat().st_dev != root_device and info.f_blocks * info.f_frsize <= 256 * MIB,
                "PRODUCTION_BOUNDED_CONTROL_REQUIRED")
        if key == "control_dir" or active_budget:
            require(info.f_bavail * info.f_frsize >= 16 * MIB and info.f_favail >= 1024,
                    "PRODUCTION_CONTROL_STORAGE_STOP")


class Registry:
    def __init__(self, c):
        self.c, self.path = validate_config(c), private_directory(c["control_dir"])
        private_directory(c["budget_dir"])
        control_capacity(c)
        self.bound = binding(c)

    def check(self):
        state = load(self.path / "identity.json")
        require(state["binding"] == self.bound and load(Path(self.c["budget_dir"]) / "production-binding.json") == self.bound,
                "PRODUCTION_IDENTITY_FENCE")
        return state

    def source(self, r, spool, *, recover=False):
        saved = self.check()["rooms"][r["room"]]
        # Read floor first: a concurrent producer may only advance the spool.
        floor = load(self.path / (r["room"] + ".floor.json"))
        state = spool.state()
        require(state["spool_id"] == saved["spool_id"], "PRODUCTION_SPOOL_IDENTITY")
        require(state["high_entry"] >= floor["high_entry"], "PRODUCTION_STALE_SPOOL")
        if floor["high_entry"]:
            rows, _ = spool.read(floor["high_entry"] - 1, 1)
            require(rows and digest(canonical(rows[0])) == floor["tip_hash"], "PRODUCTION_SOURCE_DIVERGED")
        if recover:
            recover_manifest(r)
        with manifest_open(r) as manifest:
            archive = validate_source(spool, manifest)
            require(archive["archive_id"] == saved["archive_id"], "PRODUCTION_ARCHIVE_IDENTITY")
        return archive

    def floor(self, r, spool):
        state = spool.state()
        rows, _ = spool.read(state["high_entry"] - 1, 1) if state["high_entry"] else ([], None)
        atomic(self.path / (r["room"] + ".floor.json"),
               {"high_entry": state["high_entry"], "tip_hash": digest(canonical(rows[0])) if rows else None})

    @contextmanager
    def service(self, r, role, *, now=None, preflight=None):
        self.check()
        with lock(self.path / (r["room"] + "." + role + ".lock")):
            p = self.path / (r["room"] + "." + role + ".lifecycle.json")
            old = load(p) if p.exists() else {"starts": [], "start_count": 0, "next_read": 0, "boot": None}
            now = time.time() if now is None else now
            require(not old["starts"] or now >= old["starts"][-1], "PRODUCTION_CLOCK_ROLLBACK")
            starts = [x for x in old["starts"] if now - x < 600]
            require(len(starts) < 3, "PRODUCTION_CRASH_LOOP")
            require(not starts or now - starts[-1] >= 30, "PRODUCTION_RESTART_BACKOFF")
            # Failed dependency/config/storage checks are not process starts.
            if preflight is not None:
                preflight()
            old.update(starts=starts + [now], start_count=old["start_count"] + 1)
            atomic(p, old)
            yield old, p


def initialize(c):
    registry = Registry(c)
    with lock(registry.path / "initialize.lock"):
        require(not (registry.path / "identity.json").exists(), "PRODUCTION_ALREADY_INITIALIZED")
        budget_path = Path(c["budget_dir"])
        require(not any(budget_path.iterdir()), "PRODUCTION_FRESH_BUDGET_REQUIRED")
        identities = {}
        with ExitStack() as stack:
            stack.enter_context(lock(budget_path / "budget-mode.lock"))
            require({p.name for p in budget_path.iterdir()} == {"budget-mode.lock"}, "PRODUCTION_FRESH_BUDGET_REQUIRED")
            for r in c["rooms"]:
                for role in ("capture", "archive"):
                    private_directory(r["spool_dir"] if role == "capture" else r["archive_dir"])
                    info = storage_probe(r, role)
                    key = "spool" if role == "capture" else "archive"
                    require(info["disk_free_bytes"] >= capacity_model(r)[key + "_required_bytes"],
                            "PRODUCTION_INITIAL_CAPACITY")
                require(not any(Path(r["spool_dir"]).iterdir()) and not any(Path(r["archive_dir"]).iterdir()),
                        "PRODUCTION_FRESH_STORAGE_REQUIRED")
                spool = stack.enter_context(spool_open(r, producer=True, create=True))
                worker = stack.enter_context(ArchiveWorker(spool, r["archive_dir"], min_free_bytes=r["storage"]["reserve_bytes"]))
                identities[r["room"]] = {"spool_id": spool.state()["spool_id"], "archive_id": worker.state()["archive_id"]}
                registry.floor(r, spool)
            atomic(budget_path / "production-binding.json", registry.bound)
            atomic(registry.path / "identity.json", {"binding": registry.bound, "rooms": identities})
    return {"initialized": True, "rooms": list(identities), "network_requests": 0}


class RoomBudget:
    """Persist per-Room spacing around the shared, charged FIFO admission."""
    def __init__(self, budget, lifecycle, path, r, stop):
        self.budget, self.state, self.path, self.r, self.stop = budget, lifecycle, path, r, stop
        if self.state["boot"] != budget.boot:
            self.state.update(boot=budget.boot, next_read=time.monotonic() + 60)
            atomic(self.path, self.state)

    @contextmanager
    def request(self, room):
        delay = max(0, self.state["next_read"] - time.monotonic())
        started = time.monotonic()
        interrupted = self.stop.wait(delay)
        self.budget.wait_seconds += time.monotonic() - started
        if interrupted:
            raise GracefulStop("CAPTURE_STOP_REQUESTED")
        with self.budget.request(room):
            self.state["next_read"] = time.monotonic() + 60 / self.r["max_rpm"]
            atomic(self.path, self.state)
            yield

    def defer(self, seconds):
        self.budget.defer(seconds)


class TimedClient:
    def __init__(self, client):
        self.client, self.latency = client, None
        self.counts = [0] * (len(LATENCY_BOUNDS) + 1)

    def poll(self, since):
        return self._timed(self.client.poll, since)

    def export(self, path, generation, **kwargs):
        # Export duration belongs to recovery telemetry, not normal GET latency.
        return self.client.export(path, generation, **kwargs)

    def _timed(self, method, *args):
        started = time.monotonic()
        try:
            return method(*args)
        finally:
            self.latency = time.monotonic() - started
            index = next((i for i, bound in enumerate(LATENCY_BOUNDS) if self.latency <= bound), len(LATENCY_BOUNDS))
            self.counts[index] += 1

    def histogram(self):
        return {"upper_seconds": [*LATENCY_BOUNDS, None], "counts": list(self.counts)}


def snapshot(registry, r, role, spool, lifecycle, *, extra=None):
    # Archive failures must not stop Capture. Read archive progress from the
    # spool consumer ack; manifest integrity is checked at service startup.
    spool.conn.execute("BEGIN")
    try:
        state = spool.state()
        consumer = spool.consumer("archive")
        ack = consumer["ack_entry"]
        lag = state["high_entry"] - ack
        oldest = spool.conn.execute("SELECT b.received_at FROM entries e JOIN batches b USING(batch_id) WHERE e.entry_id=?",
                                   (ack + 1,)).fetchone() if lag else None
        logical = spool.conn.execute("PRAGMA page_count").fetchone()[0] * spool.conn.execute("PRAGMA page_size").fetchone()[0]
    finally:
        spool.conn.execute("ROLLBACK")
    info = os.statvfs(r["spool_dir"])
    control = os.statvfs(registry.path)
    result = {"room": r["room"], "role": role, "observed_at": time.time(), "producer": state,
              "last_successful_response": state["last_response_at"], "processed_through": ack,
              "archive_lag_entries": lag, "archive_lag_seconds": max(0, time.time() - oldest[0]) if oldest else 0,
              "db_bytes": file_size(Path(r["spool_dir"]) / "spool.sqlite"), "logical_db_bytes": logical,
              "wal_bytes": file_size(Path(r["spool_dir"]) / "spool.sqlite-wal"),
              "disk_free_bytes": info.f_bavail * info.f_frsize, "free_inodes": info.f_favail,
              "process_start_count": lifecycle["start_count"], "process_restart_count": lifecycle["start_count"] - 1,
              "generation_binding": "OBSERVED_NOT_ATOMIC", "deletion_enabled": False, **(extra or {})}
    result.update(control_volume_bytes=control.f_blocks * control.f_frsize,
                  control_free_bytes=control.f_bavail * control.f_frsize,
                  control_free_inodes=control.f_favail)
    growth = capacity_model(r)["spool_growth_bytes_sec"]
    result["planning_runway_seconds"] = max(0, min(
        (r["storage"]["db_bytes"] - logical) / growth,
        (result["disk_free_bytes"] - r["storage"]["reserve_bytes"] - ALLOWANCE) / growth))
    # Terminal status can be published later without pretending counters were
    # freshly sampled during a long budget wait or failed cycle.
    result["sampled_at"] = result["observed_at"]
    atomic(registry.path / (r["room"] + "." + role + ".metrics.json"), result)
    return result


class SignalStop:
    """Signal handlers only assign a flag, never reenter Event/Condition locks."""
    def __init__(self):
        self.requested = False

    def set(self):
        self.requested = True

    def is_set(self):
        return self.requested

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))
        return True


def run(c, room, role):
    registry = Registry(c)
    r = next((r for r in c["rooms"] if r["room"] == room), None)
    require(r is not None and role in ("capture", "archive"), "PRODUCTION_SERVICE")
    require(c["start_at"] <= time.time() and (c["end_at"] is None or time.time() < c["end_at"]),
            "PRODUCTION_OUTSIDE_WINDOW")
    stop, handlers = SignalStop(), {}
    timer = None
    if c["end_at"] is not None:
        timer = threading.Timer(max(0, c["end_at"] - time.time()), stop.set)
        timer.daemon = True
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        if timer is not None:
            timer.start()
        with ExitStack() as stack:
            spool = None

            def preflight():
                nonlocal spool
                with operation("startup_preflight"):
                    private_directory(r["spool_dir"])
                    private_directory(r["archive_dir"])
                    storage_probe(r, role)
                    spool = stack.enter_context(spool_open(r, producer=role == "capture"))
                    registry.source(r, spool, recover=role == "archive")

            lifecycle, lifecycle_path = stack.enter_context(registry.service(r, role, preflight=preflight))
            b = c["budget"]
            budget = client = None
            if role == "capture":
                budget = stack.enter_context(ReservationBudget(c["budget_dir"], BudgetPolicy(
                    b["read_limit"], b["observer_reserve"], b["headroom"], b["capture_rpm"]),
                    stop=stop, production_binding=registry.bound))
                client = TimedClient(DeadlineClient(room, 30, stop=stop))
                worker = FastCapture(spool, client, RoomBudget(budget, lifecycle, lifecycle_path, r, stop), interval=r["interval"])
            else:
                worker = stack.enter_context(ArchiveWorker(spool, r["archive_dir"], min_free_bytes=r["storage"]["reserve_bytes"]))
            history = MetricsHistory(registry, r, role)
            latest_path = registry.path / (r["room"] + "." + role + ".metrics.json")
            if latest_path.exists():
                # Preserve the last completed cycle of a killed process before
                # replacing latest. Process IDs/counts allow deduplication.
                with operation("metrics_history"):
                    history.append(load(latest_path), force=True)
            history.append(snapshot(registry, r, role, spool, lifecycle, extra={"service_status": "STARTING"}), force=True)
            stack.callback(terminal_metrics, registry, r, role, stop, history)
            previous, previous_at = spool.state(), time.monotonic()
            deadline_count = rate_count = 0
            while not stop.is_set():
                control_capacity(c, active_budget=role == "capture")
                guard = storage_probe(r, role)
                require(guard["planning_runway_seconds"] >= 7200, "PRODUCTION_RUNWAY_STOP")
                if role == "capture":
                    logical = spool.conn.execute("PRAGMA page_count").fetchone()[0] * spool.conn.execute("PRAGMA page_size").fetchone()[0]
                    require((r["storage"]["db_bytes"] - logical) / capacity_model(r)["spool_growth_bytes_sec"] >= 7200,
                            "PRODUCTION_RUNWAY_STOP")
                if client:
                    client.latency = None
                    with operation("capture_step"):
                        delay = worker.step()
                    registry.floor(r, spool)
                    detail = worker.last
                    deadline_count += detail.get("error") == "TOTAL_REQUEST_DEADLINE"
                    rate_count += detail.get("error") == "HTTP_429"
                else:
                    with operation("archive_step"):
                        detail = worker.step()
                    delay = 1 if detail["processed"] == 0 else 0
                state, now = spool.state(), time.monotonic()
                metrics = snapshot(registry, r, role, spool, lifecycle, extra={
                    "service_status": "RUNNING", "step": detail, "storage": guard,
                    **(worker.recovery_status() if client else {}),
                    "new_gap": state["gaps"] - previous["gaps"], "total_gap": state["gaps"],
                    "messages_sec": (state["messages"] - previous["messages"]) / max(now - previous_at, 1e-9),
                    "get_latency_seconds": client.latency if client else None,
                    "get_latency_histogram_process": client.histogram() if client else {"upper_seconds": [], "counts": []},
                    "deadline_failures_process": deadline_count, "http_429_process": rate_count,
                    "budget_wait_seconds_process": budget.wait_seconds if budget else 0,
                    "requests_process": budget.requests if budget else 0})
                history.append(metrics)
                require(metrics["planning_runway_seconds"] >= 7200 if role == "capture" else True,
                        "PRODUCTION_RUNWAY_STOP")
                emit(metrics)
                previous, previous_at = state, now
                stop.wait(delay)
            emit({"service_status": "STOPPED", "room": room, "role": role})
    finally:
        if timer is not None:
            timer.cancel()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def terminal_metrics(registry, r, role, stop, history=None):
    path = registry.path / (r["room"] + "." + role + ".metrics.json")
    try:
        previous = load(path)
        final = {**previous, "observed_at": time.time(),
                 "service_status": "STOPPED" if stop.is_set() else "STOPPED_ERROR"}
        atomic(path, final)
        if history is not None:
            history.append(final, force=True)
    except (OSError, ValueError, ObserverError) as exc:
        # A failed telemetry filesystem must not mask the primary storage error.
        emit({**diagnostic(exc, "terminal_metrics"), "room": r["room"], "role": role,
              "service_status": "METRICS_WRITE_FAILED"})


def recover_archive(c, room):
    """Offline Archive-authority preflight; no GET, lifecycle charge or drain."""
    registry = Registry(c)
    r = next((r for r in c["rooms"] if r["room"] == room), None)
    require(r is not None, "PRODUCTION_SERVICE")
    with lock(registry.path / (room + ".archive.lock")):
        storage_probe(r, "archive")
        with spool_open(r) as spool:
            registry.source(r, spool, recover=True)
    return {"room": room, "manifest_health": "READY", "network_requests": 0}


def file_hash(path):
    h = hashlib.sha256()
    with regular_open(path, os.O_RDONLY) as file:
        for data in iter(lambda: file.read(MIB), b""):
            h.update(data)
    return h.hexdigest()


def backup_capacity(c, target):
    sources = [Path(r[k]) for r in c["rooms"] for k in ("spool_dir", "archive_dir")]
    require(all(target.stat().st_dev != p.stat().st_dev for p in sources), "PRODUCTION_BACKUP_VOLUME")
    # Conservative full allocated envelopes; operator can use a larger backup
    # volume without weakening the data-volume boundaries.
    required = sum(r["storage"]["spool_volume_bytes"] + r["storage"]["archive_volume_bytes"] for r in c["rooms"])
    info = os.statvfs(target)
    require(info.f_bavail * info.f_frsize >= required + ALLOWANCE and info.f_favail >= 4096,
            "PRODUCTION_BACKUP_CAPACITY")


def backup(c, destination):
    """Stopped snapshot. Does not overwrite, restore, contact network or prune."""
    registry = Registry(c)
    target = private_directory(destination)
    require(not any(target.iterdir()), "PRODUCTION_EMPTY_BACKUP_REQUIRED")
    protected = [Path(c["control_dir"]), Path(c["budget_dir"])] + [Path(r[k]) for r in c["rooms"] for k in ("spool_dir", "archive_dir")]
    require(all(target != p and p not in target.parents and target not in p.parents for p in protected), "PRODUCTION_BACKUP_OVERLAP")
    backup_capacity(c, target)
    inventory = {"binding": registry.check()["binding"], "created_at": time.time(), "rooms": {}, "files": {}}
    with ExitStack() as stack:
        # All roles are excluded before touching any Room. Existing local ack
        # consumers must also be quiesced by the operator (a deployment gate).
        for r in c["rooms"]:
            for role in ("capture", "archive"):
                stack.enter_context(lock(registry.path / (r["room"] + "." + role + ".lock")))
        for r in c["rooms"]:
            spool = stack.enter_context(spool_open(r, producer=True))
            registry.source(r, spool)
            worker = stack.enter_context(ArchiveWorker(spool, r["archive_dir"], min_free_bytes=r["storage"]["reserve_bytes"]))
            # Complete a pending plan and its ack, but do not drain an unbounded
            # backlog as a side effect of backup.
            worker.step(1)
            worker.verify()
            room_dir = target / r["room"]
            room_dir.mkdir(mode=0o700)
            out = room_dir / "spool.sqlite"
            conn = sqlite3.connect(out)
            try:
                spool.conn.backup(conn)
                require(conn.execute("PRAGMA quick_check").fetchone()[0] == "ok", "PRODUCTION_BACKUP_SQLITE")
            finally:
                conn.close()
            archive_dir = room_dir / "archive"
            archive_dir.mkdir(mode=0o700)
            for source in sorted(Path(r["archive_dir"]).rglob("*")):
                require(not source.is_symlink(), "PRODUCTION_BACKUP_SYMLINK")
                relative = source.relative_to(r["archive_dir"])
                dest = archive_dir / relative
                if source.is_dir():
                    dest.mkdir(mode=0o700)
                elif not source.name.endswith(".lock"):
                    with regular_open(source, os.O_RDONLY) as src, regular_open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as dst:
                        for data in iter(lambda: src.read(MIB), b""):
                            dst.write(data)
                        dst.flush()
                        os.fsync(dst.fileno())
            inventory["rooms"][r["room"]] = {"spool": spool.state(), "archive": worker.state(),
                                                "floor": load(registry.path / (r["room"] + ".floor.json"))}
        for path in sorted(target.rglob("*")):
            if path.is_file():
                with regular_open(path, os.O_RDONLY) as file:
                    os.fsync(file.fileno())
                inventory["files"][str(path.relative_to(target))] = file_hash(path)
        for path in sorted(target.rglob("*"), reverse=True):
            if path.is_dir():
                sync_directory(path)
        atomic(target / "inventory.json", inventory)
    return {"backup_complete": True, "files": len(inventory["files"]), "inventory_sha256": file_hash(target / "inventory.json")}


def verify_backup(destination, expected_hash):
    target = private_directory(destination)
    require(file_hash(target / "inventory.json") == expected_hash, "PRODUCTION_BACKUP_INVENTORY")
    inventory = load(target / "inventory.json")
    actual = {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file() and p != target / "inventory.json"}
    require(actual == set(inventory["files"]), "PRODUCTION_BACKUP_FILESET")
    for name, sha in inventory["files"].items():
        p = Path(name)
        require(not p.is_absolute() and ".." not in p.parts and (target / p).resolve() == target / p,
                "PRODUCTION_BACKUP_PATH")
        require(file_hash(target / p) == sha, "PRODUCTION_BACKUP_HASH")
    return {"backup_verified": True, "files": len(actual)}


def verify_restore(c, *, drain=False):
    registry = Registry(c)
    results = {}
    with ExitStack() as stack:
        for r in c["rooms"]:
            for role in ("capture", "archive"):
                stack.enter_context(lock(registry.path / (r["room"] + "." + role + ".lock")))
        for r in c["rooms"]:
            spool = stack.enter_context(spool_open(r, producer=True))
            registry.source(r, spool)
            with manifest_open(r) as manifest:
                validate_source(spool, manifest, full=True)
            require(spool.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok", "PRODUCTION_RESTORE_SQLITE")
            worker = stack.enter_context(ArchiveWorker(spool, r["archive_dir"], min_free_bytes=r["storage"]["reserve_bytes"]))
            if drain:
                while True:
                    storage_probe(r, "archive")
                    if worker.step()["caught_up"]:
                        break
            else:
                worker.step(1)
            results[r["room"]] = worker.verify()
    return {"restore_verified": True, "rooms": results, "network_requests": 0}


def plan(c):
    validate_config(c)
    return {"mode": "OFFLINE_PLAN", "production_id": c["production_id"],
            "deployment_approved": False, "rooms": {r["room"]: capacity_model(r) for r in c["rooms"]},
            "service_limits": {"capture": {"cpus": 0.5, "memory_mib": 256, "pids": 32},
                               "archive": {"cpus": 0.5, "memory_mib": 384, "pids": 32}},
            "deadline_seconds": 30, "stop_timeout_seconds": 40, "restart_delay_seconds": 30,
            "restart_policy": "on unexpected failure only; exit 2 terminal; 3 starts/600 sec",
            "network_requests": 0}


def container_spec(c, r, role):
    """Reviewable argv template only. Never calls Docker or resolves an image."""
    limits = plan(c)["service_limits"][role]
    paths = [(c["control_dir"], "rw"), (r["spool_dir"], "rw"),
             (r["archive_dir"], "ro" if role == "capture" else "rw")]
    if role == "capture":
        paths.append((c["budget_dir"], "rw"))
    # Archive does not consume budget but checks the fixed production marker.
    else:
        paths.append((c["budget_dir"], "ro"))
    argv = ["docker", "create", "--name", c["production_id"] + "-" + r["room"] + "-" + role,
            "--user", "65532:65532", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", str(limits["pids"]),
            "--cpus", str(limits["cpus"]), "--memory", str(limits["memory_mib"]) + "m",
            "--memory-swap", str(limits["memory_mib"]) + "m", "--restart", "no",
            "--stop-timeout", "40", "--network", "none" if role == "archive" else "HUMAN_APPROVED_EGRESS_NETWORK",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m,mode=700,uid=65532,gid=65532",
            "--log-driver", "local", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
            "--device-write-bps", "HUMAN_BOUND_BLOCK_DEVICE:10mb",
            "--device-read-bps", "HUMAN_BOUND_BLOCK_DEVICE:10mb",
            "--device-write-iops", "HUMAN_BOUND_BLOCK_DEVICE:200",
            "--device-read-iops", "HUMAN_BOUND_BLOCK_DEVICE:200",
            "--mount", "type=bind,src=HUMAN_BOUND_CONFIG,dst=/policy.json,readonly"]
    for path, mode in paths:
        argv.extend(("--mount", "type=bind,src=" + path + ",dst=" + path + (",readonly" if mode == "ro" else "")))
    argv.extend(("--entrypoint", "python3", "HUMAN_BOUND_IMAGE_DIGEST", "-I", "-B", "-m",
                 "technocore_full_capture.production", "run", "--config", "/policy.json", "--room", r["room"], "--role", role))
    return argv


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "initialize", "run", "status", "backup", "verify-backup", "verify-restore", "drain", "recover-archive"))
    parser.add_argument("--config")
    parser.add_argument("--room")
    parser.add_argument("--role", choices=("capture", "archive"))
    parser.add_argument("--destination")
    parser.add_argument("--inventory-sha256")
    args = parser.parse_args(argv)
    os.umask(0o077)
    stage = args.command
    try:
        if args.command == "verify-backup":
            require(bool(args.destination and args.inventory_sha256), "PRODUCTION_BACKUP_ARGUMENTS")
            result = verify_backup(args.destination, args.inventory_sha256)
        else:
            require(bool(args.config), "PRODUCTION_CONFIG_REQUIRED")
            with operation("config_load"):
                c = load(args.config)
            with operation("config_validate"):
                validate_config(c)
            stage = args.command
            if args.command == "plan":
                result = plan(c)
                result["container_argv_templates"] = [container_spec(c, r, role) for r in c["rooms"] for role in ("capture", "archive")]
            elif args.command == "initialize":
                result = initialize(c)
            elif args.command == "recover-archive":
                result = recover_archive(c, args.room)
            elif args.command == "run":
                run(c, args.room, args.role)
                return 0
            elif args.command == "backup":
                require(bool(args.destination), "PRODUCTION_BACKUP_ARGUMENTS")
                result = backup(c, args.destination)
            elif args.command in ("verify-restore", "drain"):
                result = verify_restore(c, drain=args.command == "drain")
            else:
                registry = Registry(c)
                registry.check()
                result = {r["room"]: {role: load(registry.path / (r["room"] + "." + role + ".metrics.json"))
                                      for role in ("capture", "archive")} for r in c["rooms"]}
        emit(result)
        return 0
    except GracefulStop as exc:
        emit(diagnostic(exc, stage))
        return 0 if str(exc) == "CAPTURE_STOP_REQUESTED" else 2
    except ObserverError as exc:
        emit(diagnostic(exc, stage))
        return 2
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        emit(diagnostic(exc, stage))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
