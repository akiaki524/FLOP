"""Cooperative, host-local GET budget. All users of one egress IP share it.

The lock covers the request body, and the next request waits a full spacing
after completion. No burst credits, daemon, remote input, or archive state.
Use a local filesystem with working flock/atomic rename/fsync, not NFS.
"""

from contextlib import contextmanager
from dataclasses import dataclass, asdict
import fcntl
import math
import os
from pathlib import Path
import threading
import time

from .archive import regular_open, sync_directory
from technocore_observer.protocol import ObserverError, Reply, decode_reply, json_dump


class GracefulStop(ObserverError):
    """No integrity verdict; exit and revalidate continuity on restart."""


def select_budget_mode(directory, mode, production_binding=None):
    """Fence legacy and reservation schedulers, including concurrent startup."""
    path = Path(directory)
    with regular_open(path / "budget-mode.lock", os.O_CREAT | os.O_WRONLY) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = path / "production-binding.json"
        if marker.exists() or marker.is_symlink():
            with regular_open(marker, os.O_RDONLY) as file:
                binding = decode_reply(Reply(200, "application/json", file.read(4097)))
            if production_binding is None or binding != production_binding or mode != "reservation":
                raise GracefulStop("CAPTURE_PRODUCTION_BUDGET_FENCE")
        elif production_binding is not None:
            raise GracefulStop("CAPTURE_PRODUCTION_BINDING_MISSING")
        legacy = (path / "budget.json").exists() or (path / "budget.lock").exists()
        modern = (path / "capture-budget.sqlite").exists()
        if (mode == "reservation" and legacy) or (mode == "legacy" and modern):
            raise GracefulStop("CAPTURE_BUDGET_MODE_MIXING")
        target = path / "budget-mode.json"
        if target.exists() or target.is_symlink():
            with regular_open(target, os.O_RDONLY) as file:
                saved = decode_reply(Reply(200, "application/json", file.read(257)))
            if saved != {"mode": mode}:
                raise GracefulStop("CAPTURE_BUDGET_MODE_MIXING")
        else:
            with regular_open(path / ".budget-mode.pending", os.O_CREAT | os.O_WRONLY | os.O_TRUNC) as file:
                file.write((json_dump({"mode": mode}) + "\n").encode("ascii"))
                file.flush()
                os.fsync(file.fileno())
            os.replace(path / ".budget-mode.pending", target)
            sync_directory(path)


@dataclass(frozen=True)
class BudgetPolicy:
    read_limit: float
    observer_reserve: float
    headroom: float
    capture_rpm: float

    def __post_init__(self):
        values = asdict(self).values()
        if (any(not math.isfinite(v) or v <= 0 for v in values)
                or self.capture_rpm + self.observer_reserve + self.headroom > self.read_limit):
            raise GracefulStop("CAPTURE_INVALID_READ_BUDGET")

    @property
    def spacing(self):
        return 60.0 / self.capture_rpm


class HostBudget:
    def __init__(self, directory, policy, stop=None, clock=time.monotonic, boot=None):
        self.path, self.policy = Path(directory), policy
        if not self.path.is_dir() or self.path.is_symlink():
            raise GracefulStop("CAPTURE_BUDGET_DIRECTORY_REQUIRED")
        select_budget_mode(self.path, "legacy")
        self.stop = stop if stop is not None else threading.Event()
        self.clock = clock
        # monotonic is shared across processes; boot identity prevents reusing
        # an old boot's deadline. A new boot gets one full spacing before GET.
        self.boot = boot or Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.requests = 0
        self.wait_seconds = 0.0
        self.cooldown = 0.0

    def _save(self, state):
        try:
            self._write(state)
        except (OSError, ObserverError) as exc:
            raise GracefulStop("CAPTURE_BUDGET_IO_FAILURE") from exc

    def _write(self, state):
        raw = (json_dump(state) + "\n").encode("ascii")
        with regular_open(self.path / ".budget.pending", os.O_WRONLY | os.O_CREAT | os.O_TRUNC) as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.replace(self.path / ".budget.pending", self.path / "budget.json")
        sync_directory(self.path)

    def _load(self):
        path = self.path / "budget.json"
        if not path.exists() and not path.is_symlink():
            state = {"policy": asdict(self.policy), "boot": self.boot,
                     "next": self.clock() + self.policy.spacing}
            self._save(state)
            return state
        try:
            with regular_open(path, os.O_RDONLY) as file:
                state = decode_reply(Reply(200, "application/json", file.read(4097)))
            if (set(state) != {"policy", "boot", "next"}
                    or state["policy"] != asdict(self.policy)
                    or not isinstance(state["boot"], str)
                    or type(state["next"]) not in (int, float)
                    or not math.isfinite(state["next"]) or state["next"] < 0):
                raise ValueError()
        except (ObserverError, OSError, ValueError, KeyError, TypeError) as exc:
            raise GracefulStop("CAPTURE_BUDGET_STATE_OR_POLICY_MISMATCH") from exc
        if state["boot"] != self.boot:
            state.update(boot=self.boot, next=self.clock() + self.policy.spacing)
            self._save(state)
        return state

    def _wait(self, seconds):
        if self.stop.wait(seconds):
            raise GracefulStop("CAPTURE_STOP_REQUESTED")
        self.wait_seconds += seconds

    def defer(self, seconds):
        # Called while this request owns the shared lock (429).
        self.cooldown = max(self.cooldown, seconds)

    @contextmanager
    def request(self):
        try:
            lock = regular_open(self.path / "budget.lock", os.O_WRONLY | os.O_CREAT)
        except (OSError, ObserverError) as exc:
            raise GracefulStop("CAPTURE_BUDGET_IO_FAILURE") from exc
        with lock:
            while True:
                if self.stop.is_set():
                    raise GracefulStop("CAPTURE_STOP_REQUESTED")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self._wait(0.1)
            state = self._load()
            delay = max(0.0, state["next"] - self.clock())
            if delay:
                self._wait(delay)
            # Persist a reservation BEFORE sending; SIGKILL cannot refund it.
            state["next"] = self.clock() + self.policy.spacing
            self._save(state)
            self.requests += 1
            self.cooldown = 0.0
            try:
                yield
            except BaseException:
                # A budget write failure must never mask a remote integrity
                # verdict (e.g. generation change) raised inside the request.
                state["next"] = self.clock() + max(self.policy.spacing, self.cooldown)
                try:
                    self._save(state)
                except GracefulStop:
                    pass
                raise
            else:
                state["next"] = self.clock() + max(self.policy.spacing, self.cooldown)
                self._save(state)
                # close releases flock even if state persistence fails


def add_budget_arguments(parser):
    parser.add_argument("--budget-dir", help="Pre-created host/IP-shared local directory")
    parser.add_argument("--read-limit-rpm", type=float)
    parser.add_argument("--observer-reserve-rpm", type=float)
    parser.add_argument("--headroom-rpm", type=float)
    parser.add_argument("--capture-rpm", type=float)


def configured_budget(args, stop):
    values = (args.read_limit_rpm, args.observer_reserve_rpm, args.headroom_rpm, args.capture_rpm)
    if not args.budget_dir or any(v is None for v in values):
        raise GracefulStop("CAPTURE_READ_BUDGET_REQUIRED")
    return HostBudget(args.budget_dir, BudgetPolicy(*values), stop)
