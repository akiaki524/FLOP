"""Independent Room services over the existing Spool, Archive and read budget.

No network or runtime mutation occurs on import, plan, status or verify.
The operator starts each Room/role separately after the Production gate.
"""

import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import time

from technocore_observer.protocol import ObserverError, validate_room
from .capture_first import FastCapture
from .archive import DEFAULT_SHARD
from .deadline import DeadlineClient
from .pacing import BudgetPolicy, GracefulStop
from .scheduling import ReservationBudget
from .spool import Spool, canonical, require
from .spool_archive import ArchiveWorker


ROOMS = ("lobby", "tclk-offers", "kibble", "events", "sub_economy", "zk-desk-a",
         "close1", "d-close1-price", "d-close1-flow", "d-close1-positions",
         "d-close1-pnl", "d-close1-state")
IMPORTANT = frozenset(("tclk-offers", "kibble", "close1", "d-close1-price",
                       "d-close1-flow", "d-close1-positions", "d-close1-pnl", "d-close1-state"))
# Covers simultaneous bounded writes (including WAL, archive and recovery
# staging) after a free-space check. The configured floor remains available.
GLOBAL_WRITE_MARGIN = 1024 * 1024 * 1024


def load(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "MULTI_DUPLICATE_KEY")
            result[key] = value
        return result
    with open(path, encoding="utf-8") as file:
        return validate(json.load(file, object_pairs_hook=unique))


def validate(config):
    require(type(config) is dict and set(config) == {"version", "root", "budget", "rooms",
                                                     "global_min_free_bytes"}
            and config["version"] == 1, "MULTI_CONFIG")
    require(type(config["global_min_free_bytes"]) is int and config["global_min_free_bytes"] >= 0,
            "MULTI_GLOBAL_RESERVE")
    root = Path(config["root"])
    require(root.is_absolute() and ".." not in root.parts and str(root) == config["root"]
            and root != Path("/"), "MULTI_ROOT")
    budget = config["budget"]
    require(type(budget) is dict and set(budget) == {"read_limit_rpm", "external_rpm", "headroom_rpm",
                                                  "capture_rpm", "max_waiters", "external_waiters"}, "MULTI_BUDGET")
    for key in ("read_limit_rpm", "headroom_rpm"):
        require(type(budget[key]) in (int, float) and math.isfinite(budget[key]) and budget[key] > 0,
                "MULTI_BUDGET")
    for key in ("external_rpm", "capture_rpm"):
        require(budget[key] is None or type(budget[key]) in (int, float)
                and math.isfinite(budget[key]) and budget[key] > 0, "MULTI_BUDGET")
    if budget["external_rpm"] is not None and budget["capture_rpm"] is not None:
        BudgetPolicy(budget["read_limit_rpm"], budget["external_rpm"], budget["headroom_rpm"],
                     budget["capture_rpm"])
    require(type(budget["max_waiters"]) is int and 1 <= budget["max_waiters"] <= 4
            and (budget["external_waiters"] is None or type(budget["external_waiters"]) is int
                 and 0 <= budget["external_waiters"] < budget["max_waiters"]), "MULTI_WAITERS")
    rooms = config["rooms"]
    require(type(rooms) is list and len(rooms) == len(ROOMS), "MULTI_ROOMS")
    seen = set()
    for room in rooms:
        require(type(room) is dict and set(room) == {"room", "capture_owner", "evidence", "interval_seconds", "max_rpm",
                                                     "max_db_bytes"}, "MULTI_ROOM_POLICY")
        name = validate_room(room["room"])
        require(name in ROOMS and name not in seen, "MULTI_ROOMS")
        seen.add(name)
        require(room["capture_owner"] == ("external" if name == "lobby" else "local"),
                "MULTI_CAPTURE_OWNER")
        require(room["evidence"] == ("important" if name in IMPORTANT else "standard"), "MULTI_EVIDENCE")
        require(type(room["interval_seconds"]) in (int, float) and math.isfinite(room["interval_seconds"])
                and 0.1 <= room["interval_seconds"] <= 3600, "MULTI_ROOM_POLICY")
        require(type(room["max_rpm"]) in (int, float) and math.isfinite(room["max_rpm"])
                and 0 < room["max_rpm"] and (budget["capture_rpm"] is None
                or room["max_rpm"] <= budget["capture_rpm"]), "MULTI_ROOM_POLICY")
        require((room["max_db_bytes"] is None if name == "lobby" else
                 type(room["max_db_bytes"]) is int and room["max_db_bytes"] >= 16 * 1024 * 1024),
                "MULTI_ROOM_POLICY")
    require(seen == set(ROOMS), "MULTI_ROOMS")
    if budget["capture_rpm"] is not None:
        require(sum(r["max_rpm"] for r in rooms if r["capture_owner"] == "local")
                <= budget["capture_rpm"], "MULTI_ALLOCATION")
    return config


def room_paths(config, name):
    root = Path(config["root"])
    return root / name / "spool", root / name / "archive"


def storage_floor(config):
    return config["global_min_free_bytes"] + (GLOBAL_WRITE_MARGIN if config["global_min_free_bytes"] else 0)


def check_storage(config, *paths):
    root = Path(config["root"])
    require(all(os.stat(path).st_dev == os.stat(root).st_dev for path in paths),
            "MULTI_STORAGE_FILESYSTEM_MISMATCH")
    info = os.statvfs(root)
    require(info.f_bavail * info.f_frsize >= storage_floor(config), "MULTI_GLOBAL_STORAGE_LOW")


def require_free(path, floor, extra, code):
    info = os.statvfs(path)
    require(info.f_bavail * info.f_frsize >= floor + extra, code)


class ReservedArchiveWorker(ArchiveWorker):
    """Apply the shared floor to manifest and shard admission in this runtime."""
    def __init__(self, spool, directory, *, min_free_bytes=0, **kwargs):
        self.reserve_path = Path(directory)
        self.reserve_floor = min_free_bytes
        self.reserve_shard_bytes = kwargs.get("shard_bytes", DEFAULT_SHARD)
        self._capacity()
        super().__init__(spool, directory, min_free_bytes=min_free_bytes, **kwargs)

    def _capacity(self):
        require_free(self.reserve_path, self.reserve_floor,
                     self.reserve_shard_bytes + 1024 * 1024, "MANIFEST_STORAGE_LOW")

    def _initialize(self, manifest):
        self._capacity()
        return super()._initialize(manifest)

    def _record(self, rows, segment=None):
        self._capacity()
        return super()._record(rows, segment)

    def step(self, limit=200):
        self._capacity()
        return super().step(limit)


class ReservedReservationBudget(ReservationBudget):
    """Apply the shared floor to scheduler control writes in this runtime."""
    def __init__(self, directory, policy, *, min_free_bytes=0, **kwargs):
        self.reserve_path = Path(directory)
        self.reserve_floor = min_free_bytes
        self._capacity()
        super().__init__(directory, policy, **kwargs)

    def _capacity(self):
        require_free(self.reserve_path, self.reserve_floor, 1024 * 1024,
                     "CAPTURE_BUDGET_STORAGE_LOW")

    @contextmanager
    def transaction(self):
        self._capacity()
        with super().transaction():
            yield


def plan(config):
    return {"rooms": [{"room": r["room"], "capture_owner": r["capture_owner"], "evidence": r["evidence"],
                       "interval_seconds": r["interval_seconds"], "max_rpm": r["max_rpm"],
                       "max_db_bytes": r["max_db_bytes"]}
                      for r in config["rooms"]],
            "budget": config["budget"], "global_min_free_bytes": config["global_min_free_bytes"],
            "requires_runtime_inventory": not budget_ready(config),
            "services": [name + ":" + role for name in ROOMS if name != "lobby"
                         for role in ("capture", "archive")],
            "network_requests": 0}


def initialize(config):
    root = Path(config["root"])
    require(root.is_dir() and not root.is_symlink() and not any(root.iterdir()), "MULTI_FRESH_ROOT_REQUIRED")
    check_storage(config, root)
    root.chmod(0o700)
    (root / "budget").mkdir(mode=0o700)
    for room in config["rooms"]:
        name = room["room"]
        if room["capture_owner"] == "external":
            continue
        (root / name).mkdir(mode=0o700)
        spool_path, archive_path = room_paths(config, name)
        spool_path.mkdir(mode=0o700)
        archive_path.mkdir(mode=0o700)
        with Spool(spool_path, name, producer=True, create=True, raw_responses=room["evidence"] == "important",
                   max_db_bytes=room["max_db_bytes"], min_free_bytes=storage_floor(config)) as spool:
            with ReservedArchiveWorker(spool, archive_path, min_free_bytes=storage_floor(config)):
                pass
    marker = {"format": 1, "rooms": list(ROOMS), "origin": "https://technocore.chat"}
    path = root / "multi-room.json"
    with open(path, "x", encoding="ascii") as file:
        file.write(canonical(marker) + "\n")
        file.flush()
        os.fsync(file.fileno())
    return {"initialized": True, "rooms": list(ROOMS), "network_requests": 0}


def check_root(config):
    root = Path(config["root"])
    require(root.is_dir() and not root.is_symlink(), "MULTI_ROOT_MISSING")
    with open(root / "multi-room.json", encoding="ascii") as file:
        marker = json.load(file)
    require(marker == {"format": 1, "rooms": list(ROOMS), "origin": "https://technocore.chat"},
            "MULTI_ROOT_MISMATCH")
    return root


def budget_ready(config):
    budget = config["budget"]
    return all(budget[key] is not None for key in ("external_rpm", "capture_rpm", "external_waiters"))


class SignalStop:
    """A signal handler only writes a flag; waits run outside the handler."""
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


class RoomBudget:
    """Shared request starts plus bounded in-flight HTTP and Room spacing."""
    def __init__(self, budget, directory, room, slots, max_rpm, stop):
        self.budget, self.directory, self.room = budget, directory, room
        self.slots, self.max_rpm, self.stop = slots, max_rpm, stop
        self.next_start = time.monotonic()
        self.recovery_available = slots >= 2
        if hasattr(budget, "conn"):
            with budget.transaction():
                budget.conn.execute("""CREATE TABLE IF NOT EXISTS slot_queue (
                    ticket INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL,
                    kind TEXT NOT NULL, owner TEXT NOT NULL, boot TEXT NOT NULL,
                    expires REAL NOT NULL, UNIQUE(room,kind))""")

    @contextmanager
    def request(self, room):
        with self._request(room, range(self.slots - 1 if self.recovery_available else 1), "ordinary"):
            yield

    @contextmanager
    def recovery_request(self, room):
        require(self.recovery_available, "MULTI_RECOVERY_SLOT_UNAVAILABLE")
        with self._request(room, (self.slots - 1,), "recovery"):
            yield

    def _slot(self, indexes, kind):
        # The persistent queue makes slot admission FIFO across processes.
        # Expiring tickets prevent a dead producer from blocking later Rooms.
        if not hasattr(self.budget, "conn"):
            return self._try_slots(indexes)
        slot = None
        try:
            with self.budget.transaction():
                conn = self.budget.conn
                now = time.monotonic()
                conn.execute("DELETE FROM slot_queue WHERE boot<>? OR expires<=?",
                             (self.budget.boot, now))
                row = conn.execute("SELECT owner FROM slot_queue WHERE room=? AND kind=?",
                                   (self.room, kind)).fetchone()
                require(row is None or row["owner"] == self.budget.owner,
                        "MULTI_ROOM_ALREADY_WAITING")
                if row is None:
                    conn.execute("INSERT INTO slot_queue(room,kind,owner,boot,expires) VALUES(?,?,?,?,?)",
                                 (self.room, kind, self.budget.owner, self.budget.boot, now + 2))
                else:
                    conn.execute("UPDATE slot_queue SET expires=? WHERE room=? AND kind=?",
                                 (now + 2, self.room, kind))
                head = conn.execute("SELECT room FROM slot_queue WHERE kind=? ORDER BY ticket LIMIT 1",
                                    (kind,)).fetchone()[0]
                if head == self.room:
                    slot = self._try_slots(indexes)
                    if slot is not None:
                        conn.execute("DELETE FROM slot_queue WHERE room=? AND kind=?", (self.room, kind))
            return slot
        except BaseException:
            if slot is not None:
                slot.close()
            raise

    def _try_slots(self, indexes):
        for index in indexes:
            candidate = open(self.directory / f"waiter-{index}.lock", "a+b")
            try:
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return candidate
            except BlockingIOError:
                candidate.close()
        return None

    @contextmanager
    def _request(self, room, indexes, kind):
        require(room == self.room, "MULTI_ROOM_MISMATCH")
        if self.stop.wait(max(0, self.next_start - time.monotonic())):
            raise GracefulStop("CAPTURE_STOP_REQUESTED")
        slot = None
        try:
            while slot is None:
                if self.stop.is_set():
                    raise GracefulStop("CAPTURE_STOP_REQUESTED")
                slot = self._slot(indexes, kind)
                if slot is None and self.stop.wait(0.05):
                    raise GracefulStop("CAPTURE_STOP_REQUESTED")
            with self.budget.request(room):
                self.next_start = time.monotonic() + 60 / self.max_rpm
                yield
        finally:
            if slot is not None:
                slot.close()

    def defer(self, seconds):
        self.budget.defer(seconds)


def run(config, name, role, *, once=False, client=None, stop=None):
    root = check_root(config)
    require(role in ("capture", "archive"), "MULTI_ROLE")
    room = next((r for r in config["rooms"] if r["room"] == name), None)
    require(room is not None, "MULTI_ROOM_MISMATCH")
    require(room["capture_owner"] == "local", "MULTI_EXTERNAL_PRODUCER")
    if role == "capture":
        require(budget_ready(config), "MULTI_RUNTIME_INVENTORY_REQUIRED")
    spool_path, archive_path = room_paths(config, name)
    check_storage(config, root / "budget", spool_path, archive_path)
    stop = stop or SignalStop()
    with Spool(spool_path, name, producer=role == "capture", raw_responses=room["evidence"] == "important",
               max_db_bytes=room["max_db_bytes"], min_free_bytes=storage_floor(config)) as spool:
        if role == "archive":
            with ReservedArchiveWorker(spool, archive_path, min_free_bytes=storage_floor(config)) as worker:
                while not stop.is_set():
                    result = worker.step()
                    if once:
                        return result
                    stop.wait(1 if result["processed"] == 0 else 0)
        else:
            b = config["budget"]
            policy = BudgetPolicy(b["read_limit_rpm"], b["external_rpm"], b["headroom_rpm"], b["capture_rpm"])
            with ReservedReservationBudget(root / "budget", policy, stop=stop,
                                           min_free_bytes=storage_floor(config)) as shared:
                budget = RoomBudget(shared, root / "budget", name,
                                    b["max_waiters"] - b["external_waiters"], room["max_rpm"], stop)
                worker = FastCapture(spool, client or DeadlineClient(name, 30, stop=stop, poll_wait=0), budget,
                                     interval=room["interval_seconds"],
                                     enable_recovery=budget.recovery_available)
                while not stop.is_set():
                    delay = worker.step()
                    if once:
                        return {"room": name, "capture": worker.last, "state": spool.state()}
                    stop.wait(delay)


def status(config, name):
    check_root(config)
    room = next((r for r in config["rooms"] if r["room"] == name), None)
    require(room is not None, "MULTI_ROOM_MISMATCH")
    require(room["capture_owner"] == "local", "MULTI_EXTERNAL_PRODUCER")
    source, target = room_paths(config, name)
    with Spool(source, name, max_db_bytes=room["max_db_bytes"],
               min_free_bytes=storage_floor(config)) as spool:
        uri = (target / "manifest.sqlite").as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as manifest:
            manifest.row_factory = sqlite3.Row
            archive = dict(manifest.execute("SELECT * FROM state WHERE singleton=1").fetchone())
        info = os.statvfs(source)
        return {"room": name, "state": spool.state(), "archive": archive,
                "archive_lag_entries": spool.state()["high_entry"] - archive["through_entry"],
                "evidence": room["evidence"], "global_min_free_bytes": config["global_min_free_bytes"],
                "filesystem_free_bytes": info.f_bavail * info.f_frsize}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "initialize", "run", "status"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--room")
    parser.add_argument("--role", choices=("capture", "archive"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    stop = SignalStop()
    handlers = {}
    try:
        config = load(args.config)
        if args.command == "plan":
            result = plan(config)
        elif args.command == "initialize":
            result = initialize(config)
        else:
            require(args.room is not None, "MULTI_ROOM_REQUIRED")
            if args.command == "status":
                result = status(config, args.room)
            else:
                require(args.role is not None, "MULTI_ROLE")
                for sig in (signal.SIGINT, signal.SIGTERM):
                    handlers[sig] = signal.signal(sig, lambda *_: stop.set())
                result = run(config, args.room, args.role, once=args.once, stop=stop)
        if result is not None:
            print(canonical(result), flush=True)
        return 0
    except GracefulStop as exc:
        code = str(exc)
        print(canonical({"status": "STOPPED", "reason": code}), flush=True)
        return 0 if code == "CAPTURE_STOP_REQUESTED" else 2
    except sqlite3.Error as exc:
        print(canonical({"status": "STOPPED_ERROR", "error": "MULTI_SQLITE_FAILURE",
                         "sqlite_code": getattr(exc, "sqlite_errorcode", None),
                         "sqlite_name": getattr(exc, "sqlite_errorname", None)}), flush=True)
        return 2
    except (ObserverError, OSError, ValueError, KeyError, TypeError) as exc:
        code = str(exc) if isinstance(exc, ObserverError) else "MULTI_LOCAL_FAILURE"
        print(canonical({"status": "STOPPED_ERROR", "error": code}), flush=True)
        return 2
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
