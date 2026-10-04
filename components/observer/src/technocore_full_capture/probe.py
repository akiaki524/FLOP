"""Bounded live READ-ONLY conformance observation; no retry, write API or resync.

At most two tail/export/tail samples (six GETs). Explicit budget required.
Scratch directory is pre-created and separate from every archive directory.
Only metadata crosses stdout; message bodies remain in the bounded scratch.
"""

import argparse
import hashlib
import os
from pathlib import Path
import signal
import threading
import time
from types import SimpleNamespace

from .__main__ import ExportClient, SnapshotCapture, RetryCapture, emit, head
from .archive import MAX_LINE, record, regular_open
from .pacing import GracefulStop, add_budget_arguments, configured_budget
from technocore_observer.protocol import ObserverError

UNVERIFIED = "UNVERIFIED / requires production read-only observation"


def observe(client, path, previous=None):
    started = time.monotonic()
    snapshot = client.snapshot(path)
    state = previous or {"generation": None, "cursor": None, "cursor_sha256": None}
    worker = SnapshotCapture(SimpleNamespace(room=client.room, state=state), client)
    latest = worker._validate(snapshot)  # same strict validator; never publish
    first = None
    largest = count = tail_matches = 0
    last_hash = None
    expected = {m["seq"] for v in (snapshot.before, snapshot.after) for m in v["messages"]}
    with regular_open(path, os.O_RDONLY) as file:
        for raw in iter(lambda: file.readline(MAX_LINE + 1), b""):
            obj = record(raw)
            first = obj["seq"] if first is None else first
            largest = max(largest, len(raw))
            count += 1
            tail_matches += obj["seq"] in expected
            last_hash = hashlib.sha256(raw).hexdigest()
    next_state = ({"generation": snapshot.generation, "cursor": latest,
                   "cursor_sha256": last_hash} if latest is not None else state)
    result = {**getattr(client, "last_fetch_metrics", {}),
              "sample_seconds_including_pacing": time.monotonic() - started,
              "generation_bracket": [snapshot.before["generation"], snapshot.generation,
                                     snapshot.after["generation"]],
              "tail_before": head(snapshot.before), "tail_after": head(snapshot.after),
              "first_seq": first, "last_seq": latest, "records": count,
              "observed_max_line_bytes": largest, "canonical_tail_matches": tail_matches,
              "canonical_content": "OBSERVED" if tail_matches else UNVERIFIED,
              "advanced_during_snapshot": head(snapshot.after) > head(snapshot.before),
              "empty_observed": count == 0,
              "retained_boundary_advanced": bool(previous and previous["cursor"] is not None and first is not None and
                                                  first > previous["cursor"]),
              "snapshot_continuity": "OBSERVED", "compaction_cause": UNVERIFIED,
              "ephemeral_ttl_expiry": UNVERIFIED}
    return result, next_state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room", required=True)
    parser.add_argument("--scratch-dir", required=True)
    parser.add_argument("--samples", type=int, choices=(1, 2), default=2)
    parser.add_argument("--sample-interval", type=float, default=5)
    add_budget_arguments(parser)
    args = parser.parse_args(argv)
    stop = threading.Event()
    budget = None
    previous_handlers = {}
    try:
        directory = Path(args.scratch_dir)
        if (not directory.is_dir() or directory.is_symlink()
                or any(directory.iterdir())):
            raise GracefulStop("PROBE_EMPTY_SCRATCH_DIRECTORY_REQUIRED")
        # Reuse interval validation; no live request occurs on invalid input.
        SnapshotCapture(None, None, args.sample_interval)
        budget = configured_budget(args, stop)
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        client = ExportClient(args.room, budget)
        state = None
        for i in range(args.samples):
            result, state = observe(client, directory / ".probe-fetch.pending", state)
            emit({"sample": i + 1, **result})
            if i + 1 < args.samples and stop.wait(args.sample_interval):
                raise GracefulStop("CAPTURE_STOP_REQUESTED")
        emit({"status": "OBSERVATION_COMPLETE", "requests": budget.requests,
              "write_requests": 0, "automatic_retries": 0,
              "production_conformance": "limited to observed samples",
              "unobserved_compaction_or_ttl": UNVERIFIED})
        return 0
    except (RetryCapture, ObserverError) as exc:
        emit({"status": "OBSERVATION_STOPPED", "error": getattr(exc, "code", str(exc)),
              "requests": budget.requests if budget else 0, "automatic_retries": 0})
        return 2
    except OSError:
        emit({"status": "OBSERVATION_STOPPED", "error": "PROBE_LOCAL_IO_FAILURE"})
        return 2
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
