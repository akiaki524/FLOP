"""CLI: callers must provide OS-level secret isolation before live networking."""

import argparse
import contextlib
import json
import math
from pathlib import Path
import signal
import sqlite3
import sys
import threading

from .http import SafeClient
from .observer import NETWORK_ERRORS, Observer, watchdog
from .protocol import ObserverError, decode_reply, sanitize_for_display, validate_envelope, validate_room
from .storage import StateLock, Store, initialize, migrate_v1


def emit(value, error=False):
    # JSON escapes controls and BiDi; one additional display boundary applies to
    # every complete output line. Database records never pass through this.
    def display(item):
        if isinstance(item, str):
            return sanitize_for_display(item)
        if isinstance(item, dict):
            return {sanitize_for_display(key): display(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [display(val) for val in item]
        return item
    line = json.dumps(display(value), ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    print(sanitize_for_display(line, limit=len(line)), file=sys.stderr if error else sys.stdout,
          flush=True)


class Parser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        if message:
            # argparse may echo hostile argv values. Preserve its own line layout
            # only for help; errors are rendered through our fixed safe handler.
            for line in message.splitlines():
                print(sanitize_for_display(line), file=file or sys.stdout)

    def error(self, message):
        emit({"error": "INVALID_ARGUMENTS", "detail": sanitize_for_display(message)}, error=True)
        raise SystemExit(2)


def parser():
    p = Parser(description="Technocore read-only observer (OS-isolated live execution required)")
    sub = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("init", "run", "heartbeat", "check-config", "watchdog", "resync-plan", "resync", "migrate-plan", "migrate",
                 "retention-backup", "retention-plan", "retention-apply"):
        command = sub.add_parser(name)
        command.add_argument("--room", required=True)
        command.add_argument("--state-dir", type=Path, default=Path("state"))
        if name == "init":
            command.add_argument("--mode", choices=("tail",), required=True)
        if name == "run":
            command.add_argument("--check-config", action="store_true")
            command.add_argument("--busy-timeout-ms", type=int, default=5000)
        if name == "watchdog":
            command.add_argument("--stall-seconds", type=float, default=30)
        if name in ("resync-plan", "resync"):
            command.add_argument("--anchor-seq", type=int, required=True)
            command.add_argument("--generation", type=int, required=True)
            command.add_argument("--reason", required=True)
        if name.startswith("retention-"):
            command.add_argument("--backup-name", required=True)
        if name in ("retention-plan", "retention-apply"):
            command.add_argument("--before-unix", type=float, required=True)
        if name in ("resync", "migrate", "retention-apply"):
            command.add_argument("--approval", required=True)
    return p


def _execute(args):
    validate_room(args.room)
    if args.command in ("heartbeat", "watchdog"):
        with contextlib.closing(Store(args.state_dir, args.room, readonly=True)) as store:
            if args.command == "heartbeat":
                emit(store.heartbeat())
                return 0
            if not math.isfinite(args.stall_seconds) or args.stall_seconds <= 0:
                raise ObserverError("INVALID_STALL_THRESHOLD")
            result = watchdog(store, SafeClient(args.room), args.stall_seconds)
            emit(result)
            return 1 if result["result"] in ("LAGGING", "GENERATION_CHANGE", "OBSERVER_STOPPED") else 0
    with StateLock(args.state_dir, create=args.command == "init"):
        if args.command.startswith("retention-"):
            from .maintenance import backup, retain
            with contextlib.closing(Store(args.state_dir, args.room,
                    readonly=args.command != "retention-apply", recovery=True)) as store:
                if args.command == "retention-backup":
                    emit(backup(store, args.backup_name))
                else:
                    emit(retain(store, args.backup_name, args.before_unix, getattr(args, "approval", None)))
            return 0
        if args.command in ("migrate-plan", "migrate"):
            emit(migrate_v1(args.state_dir, args.room, getattr(args, "approval", None)))
            return 0
        if args.command in ("resync-plan", "resync"):
            with contextlib.closing(Store(args.state_dir, args.room,
                    readonly=args.command == "resync-plan", recovery=True)) as store:
                if args.command == "resync-plan":
                    emit(store.resync_plan(args.anchor_seq, args.generation, args.reason))
                else:
                    store.resync(args.anchor_seq, args.generation, args.reason, args.approval)
                    emit(store.heartbeat())
            return 0
        if args.command == "init":
            # Reject existing state before making any network request.
            if any(p.name.startswith("state.sqlite") for p in args.state_dir.iterdir()):
                raise ObserverError("INIT_PATH_ALREADY_EXISTS")
            reply = SafeClient(args.room).tail()
            if reply.status != 200:
                raise ObserverError("INIT_HTTP_FAILURE")
            envelope = validate_envelope(decode_reply(reply), args.room)
            if envelope["count"] > 1:
                raise ObserverError("INIT_LIMIT_EXCEEDED")
            initialize(args.state_dir, args.room, envelope, reply)
            emit({"status": "INITIALIZED", "room": args.room,
                  "init_anchor_seq": envelope["messages"][-1]["seq"]})
            return 0
        with contextlib.closing(Store(args.state_dir, args.room,
                busy_timeout=getattr(args, "busy_timeout_ms", 5000))) as store:
            observer = Observer(store, SafeClient(args.room))
            if args.command == "check-config" or args.check_config:
                emit(observer.check_config())
                if args.command == "check-config":
                    return 0
            stop = threading.Event()
            previous = {}
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda signum, frame: stop.set())
            try:
                while not stop.is_set():
                    keep_running, delay = observer.poll_once()
                    emit(store.heartbeat())
                    if not keep_running:
                        return 1
                    stop.wait(delay)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
            return 0


def main(argv=None):
    try:
        return _execute(parser().parse_args(argv))
    except ObserverError as exc:
        emit({"error": sanitize_for_display(str(exc)), "human_review_required": True}, error=True)
        return 1
    except BlockingIOError:
        emit({"error": "OBSERVER_ALREADY_LOCKED"}, error=True)
        return 1
    except sqlite3.Error as exc:
        # Never format server-controlled content or raw exception messages.
        emit({"error": "DATABASE_FAILURE", "class": type(exc).__name__,
              "sqlite_code": getattr(exc, "sqlite_errorname", None),
              "human_review_required": True}, error=True)
        return 1
    except NETWORK_ERRORS as exc:
        emit({"error": "IO_OR_NETWORK_FAILURE", "class": type(exc).__name__}, error=True)
        return 1
    except (ValueError, OverflowError, RecursionError) as exc:
        emit({"error": "DATA_PROCESSING_FAILURE", "class": type(exc).__name__}, error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
