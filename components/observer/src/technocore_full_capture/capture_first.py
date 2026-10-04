"""Opt-in Capture-first CLI. Legacy strict run/export probe remain unchanged.

Run capture, archive and local consumers as separate processes/services. All
directories are operator-precreated. No live operation happens on import.
"""

import argparse
import hashlib
import math
import signal
import sqlite3
import threading
import time

from .deadline import DeadlineClient, RequestFailure
from .pacing import BudgetPolicy, GracefulStop, add_budget_arguments
from .scheduling import ReservationBudget
from .spool import Spool, canonical, require
from .spool_archive import ArchiveWorker
from .recovery import recover
from technocore_observer.http import backoff, retry_after
from technocore_observer.protocol import POLL_LIMIT, ObserverError, decode_reply, validate_envelope


RECOVERY_MIN_INTERVAL = 60.0
RECOVERY_FAILURE_COOLDOWN = 120.0
RECOVERY_SLOW_SECONDS = 10.0


class FastCapture:
    def __init__(self, spool, client, budget, *, interval=1.0, clock=time.monotonic,
                 enable_recovery=True):
        require(math.isfinite(interval) and interval > 0, "CAPTURE_INVALID_INTERVAL")
        require(spool.producer, "SPOOL_PRODUCER_REQUIRED")
        self.spool, self.client, self.budget, self.interval = spool, client, budget, interval
        self.clock = clock
        self.enable_recovery = enable_recovery
        self.recovery_next = 0.0
        self.recovery_metrics = {"recovery_attempts_process": 0, "recovery_successes_process": 0,
                                 "recovery_failures_process": 0, "recovery_export_seconds": None,
                                 "recovery_duration_seconds": None}
        self.failures = 0
        self.last = {"capture_status": "WAITING"}

    def _retry(self, code, reply=None, delay=0):
        try:
            self.spool.failure(code, reply)
        except ObserverError as exc:
            if str(exc) != "SPOOL_WAL_LIMIT":
                raise
            return max(delay, self._wal_wait())
        self.failures += 1
        self.last = {"capture_status": "RETRYING", "error": code, **self.recovery_status()}
        return max(self.interval, delay, backoff(self.failures))

    def step(self):
        # Pause GETs under WAL pressure, allowing a released reader to recover
        # on the next capacity probe. Other local storage failures still stop.
        try:
            self.spool.capacity()
        except ObserverError as exc:
            if str(exc) != "SPOOL_WAL_LIMIT":
                raise
            return self._wal_wait()
        state = self.spool.state()
        try:
            with self.budget.request(self.spool.room):
                reply = self.client.poll(state["cursor"])
        except RequestFailure as exc:
            if str(exc) == "CAPTURE_STOP_REQUESTED":
                raise GracefulStop("CAPTURE_STOP_REQUESTED") from exc
            return self._retry(str(exc))
        if reply.status == 429:
            delay = max(retry_after(reply.retry_after)[0] or 0, backoff(self.failures + 1))
            self.budget.defer(delay)
            return self._retry("HTTP_429", reply, delay)
        if 500 <= reply.status <= 599:
            return self._retry("HTTP_5XX", reply)
        if reply.status != 200:
            self.spool.failure("HTTP_ENDPOINT_ANOMALY", reply)
            raise ObserverError("HTTP_ENDPOINT_ANOMALY")
        try:
            envelope = validate_envelope(decode_reply(reply), self.spool.room)
            require(envelope["count"] <= POLL_LIMIT, "SPOOL_PAGE_LIMIT")
            recovery = {}
            result = None
            fresh = [m for m in envelope["messages"] if m["seq"] > state["cursor"]]
            if (state["cursor"] > 0 and envelope["generation"] == state["generation"] and
                    fresh and fresh[0]["seq"] > state["cursor"] + 1):
                if not self.enable_recovery:
                    recovery = {"recovery_status": "DEFERRED_NO_SLOT"}
                elif self.clock() < self.recovery_next:
                    self.spool.failure("RECOVERY_COOLDOWN")
                    recovery = {"recovery_status": "COOLDOWN", "recovery_error": "RECOVERY_COOLDOWN"}
                else:
                    started = self.clock()
                    self.recovery_metrics["recovery_attempts_process"] += 1
                    self.recovery_metrics["recovery_export_seconds"] = None
                    # At most one attempt/minute, even when recovery succeeds.
                    # Failed or >=10s recovery opens the breaker for two minutes
                    # after completion (200 records / observed 20 msg/s = 10s).
                    self.recovery_next = started + RECOVERY_MIN_INTERVAL
                    failed = True
                    try:
                        with recover(self.client, self.budget, self.spool.room, state, envelope,
                                     spool=self.spool, clock=self.clock, telemetry=self.recovery_metrics) as proven:
                            result = self.spool.ingest_recovery(**proven,
                                request_epoch=state["epoch"], request_since=state["cursor"],
                                raw_body=reply.body if self.spool.raw_responses else None)
                        failed = False
                        self.recovery_metrics["recovery_successes_process"] += 1
                        recovery = {"recovery_status": "RECOVERED"}
                    except GracefulStop:
                        raise
                    except ObserverError as exc:
                        if str(exc).startswith(("SPOOL_", "RECOVERY_STALE")):
                            raise
                        self.spool.failure(str(exc))
                        recovery = {"recovery_status": "UNRECOVERABLE", "recovery_error": str(exc)}
                    finally:
                        duration = max(0, self.clock() - started)
                        self.recovery_metrics["recovery_duration_seconds"] = duration
                        if failed:
                            self.recovery_metrics["recovery_failures_process"] += 1
                        if failed or duration >= RECOVERY_SLOW_SECONDS:
                            self.recovery_next = self.clock() + RECOVERY_FAILURE_COOLDOWN
            if result is None:
                result = self.spool.ingest(envelope, request_epoch=state["epoch"], request_since=state["cursor"],
                                           response_sha256=hashlib.sha256(reply.body).hexdigest(),
                                           raw_body=reply.body if self.spool.raw_responses else None)
        except GracefulStop:
            raise
        except ObserverError as exc:
            if str(exc) == "SPOOL_WAL_LIMIT":
                return self._wal_wait()
            # Only protocol acceptance errors may retry. Storage, ownership,
            # cursor races and resource guards must stop visibly.
            if str(exc).startswith(("SPOOL_STORAGE", "SPOOL_WAL", "SPOOL_STALE", "SPOOL_PRODUCER")):
                raise
            return self._retry(str(exc), reply)
        self.failures = 0
        self.last = {"capture_status": "RUNNING", **result, **recovery, **self.recovery_status(), "record_encoding": "normalized_json",
                     "generation_binding": "OBSERVED_NOT_ATOMIC", "forward_pagination_supported": False}
        # Immediate means rejoin the FIFO now, not bypass budget/cooldown. A
        # replay-only full page cannot create a high-rate no-progress loop.
        return 0.0 if (result["generation_changed"] or
                       (envelope["count"] >= POLL_LIMIT and result["saved"] > 0)) else self.interval

    def recovery_status(self):
        remaining = max(0, self.recovery_next - self.clock())
        return {**self.recovery_metrics, "recovery_cooldown_remaining_seconds": remaining,
                "recovery_circuit_open": remaining > 0}

    def _wal_wait(self):
        # Do not grow the guarded WAL to persist a failure. CLI emits this
        # condition on every bounded retry; cursor and response stay unaccepted.
        self.failures += 1
        self.last = {"capture_status": "RETRYING", "error": "SPOOL_WAL_LIMIT", **self.recovery_status()}
        return max(self.interval, backoff(self.failures))


def emit(value):
    print(canonical(value), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capture", "archive", "verify", "status", "read"))
    parser.add_argument("--room", required=True)
    parser.add_argument("--spool-dir", required=True)
    parser.add_argument("--archive-dir")
    parser.add_argument("--consumer", default="archive")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--deadline", type=float, default=30.0)
    parser.add_argument("--min-free-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--max-db-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument("--max-wal-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--once", action="store_true")
    add_budget_arguments(parser)
    args = parser.parse_args(argv)
    stop, handlers = threading.Event(), {}
    try:
        require(math.isfinite(args.interval) and args.interval > 0, "CAPTURE_INVALID_INTERVAL")
        if args.command == "capture":
            require(math.isfinite(args.deadline) and 10 < args.deadline <= 120, "CAPTURE_INVALID_DEADLINE")
            values = (args.read_limit_rpm, args.observer_reserve_rpm, args.headroom_rpm, args.capture_rpm)
            require(args.budget_dir and all(value is not None for value in values), "CAPTURE_READ_BUDGET_REQUIRED")
            policy = BudgetPolicy(*values)
        if args.command in {"archive", "verify"}:
            require(args.archive_dir is not None, "CAPTURE_ARCHIVE_DIRECTORY_REQUIRED")
        for sig in (signal.SIGTERM, signal.SIGINT):
            handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        with Spool(args.spool_dir, args.room, producer=args.command == "capture", create=args.command == "capture",
                   min_free_bytes=args.min_free_bytes, max_db_bytes=args.max_db_bytes, max_wal_bytes=args.max_wal_bytes) as spool:
            if args.command == "status":
                emit({"producer": spool.state(), "retention": spool.retention_watermark()})
            elif args.command == "read":
                rows, state = spool.read(args.after, args.limit)
                emit({"entries": rows, "producer_high_entry": state["high_entry"], "ack_advanced": False})
            elif args.command in {"archive", "verify"}:
                with ArchiveWorker(spool, args.archive_dir, name=args.consumer, min_free_bytes=args.min_free_bytes) as worker:
                    if args.command == "verify":
                        emit(worker.verify())
                    else:
                        while not stop.is_set():
                            result = worker.step(args.limit)
                            emit(result)
                            if args.once:
                                break
                            if result["processed"] == 0:
                                stop.wait(args.interval)
            else:
                with ReservationBudget(args.budget_dir, policy, stop=stop) as budget:
                    client = DeadlineClient(args.room, args.deadline, stop=stop)
                    worker = FastCapture(spool, client, budget, interval=args.interval)
                    while not stop.is_set():
                        delay = worker.step()
                        emit({**worker.last, "producer": spool.state(), "requests": budget.requests,
                              "budget_wait_seconds": budget.wait_seconds,
                              "budget_contention_retries": budget.contention_retries})
                        if args.once:
                            break
                        stop.wait(delay)
        return 0
    except GracefulStop as exc:
        emit({"capture_status": "STOPPED", "error": str(exc)})
        return 0 if str(exc) == "CAPTURE_STOP_REQUESTED" else 2
    except ObserverError as exc:
        emit({"capture_status": "STOPPED", "error": str(exc)})
        return 2
    except (sqlite3.Error, OSError):
        emit({"capture_status": "STOPPED_STORAGE_OR_LOCAL_IO", "error": "LOCAL_IO_FAILURE"})
        return 2
    finally:
        for sig, previous in handlers.items():
            signal.signal(sig, previous)


if __name__ == "__main__":
    raise SystemExit(main())
