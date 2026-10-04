"""Read-only incremental capture, independent of the bounded Observer.

The official room read is newest-N, NOT forward pagination. Missing prefixes
block capture; they cannot be recovered by advancing since. Export remains a
separate bounded probe. Remote content only supplies archived message data.
"""

import argparse
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import errno
import hashlib
import http.client
import math
import os
from pathlib import Path
import signal
import threading
import time
import urllib.error
import urllib.request

from .archive import Archive, DEFAULT_SHARD, MAX_LINE, record, regular_open
from .pacing import GracefulStop, add_budget_arguments, configured_budget
from technocore_observer.http import ORIGIN, SafeClient, backoff, retry_after
from technocore_observer.protocol import MAX_BODY, POLL_LIMIT, ObserverError, decode_reply, json_dump, validate_envelope

MAX_EXPORT = 32 * 1024 * 1024
EXPORT_DEADLINE = 120.0
NETWORK_ERRORS = (OSError, urllib.error.URLError, http.client.HTTPException)


class CaptureFault(ObserverError):
    """Local fixed error code; no remote text reaches diagnostics."""


class RetryCapture(Exception):
    def __init__(self, code, delay=0):
        self.code, self.delay = code, delay


def check_status(status, header=None):
    if status == 429:
        raise RetryCapture("HTTP_429", retry_after(header)[0] or 0)
    if 500 <= status <= 599:
        raise RetryCapture("HTTP_5XX")
    if status != 200:
        raise CaptureFault("HTTP_ENDPOINT_ANOMALY")


@dataclass(frozen=True)
class Snapshot:
    path: Path
    generation: int
    before: dict
    after: dict


class ReadClient:
    """Budgeted fixed-origin reads; Observer transport is unchanged."""
    def __init__(self, room, budget=None):
        self.tail_client = SafeClient(room)
        self.room = self.tail_client.room
        self.budget = budget

    @contextmanager
    def request(self):
        with self.budget.request() if self.budget is not None else nullcontext():
            try:
                yield
            except RetryCapture as exc:
                if self.budget is not None and exc.code == "HTTP_429":
                    self.budget.defer(max(exc.delay, backoff(1)))
                raise

    def tail(self):
        with self.request():
            try:
                reply = self.tail_client.tail()
            except NETWORK_ERRORS as exc:
                raise RetryCapture("NETWORK_FAILURE") from exc
            check_status(reply.status, reply.retry_after)
        try:
            decoded = decode_reply(reply)
        except ObserverError as exc:
            # Only undecodable transport envelopes are retryable. Semantically
            # invalid envelopes and all snapshot integrity errors still block.
            if str(exc) == "INVALID_JSON":
                raise RetryCapture("TAIL_INVALID_JSON") from exc
            raise
        view = validate_envelope(decoded, self.room)
        if view["count"] > 1:
            raise CaptureFault("TAIL_LIMIT_EXCEEDED")
        return view

    def page(self, cursor):
        with self.request():
            try:
                reply = self.tail_client.poll(cursor)
            except NETWORK_ERRORS as exc:
                raise RetryCapture("NETWORK_FAILURE") from exc
            check_status(reply.status, reply.retry_after)
        try:
            decoded = decode_reply(reply)
        except ObserverError as exc:
            if str(exc) == "INVALID_JSON":
                raise RetryCapture("PAGE_INVALID_JSON") from exc
            raise
        view = validate_envelope(decoded, self.room)
        if view["count"] > POLL_LIMIT:
            raise CaptureFault("PAGE_LIMIT_EXCEEDED")
        return view


class ExportClient(ReadClient):
    """Bounded export for probes and capture-first gap recovery, not polling."""

    def snapshot(self, path):
        before = self.tail()
        with self.request():
            generation = self._download(path, before)
        after = self.tail()
        if after["generation"] != generation:
            raise CaptureFault("GENERATION_CHANGE")
        return Snapshot(Path(path), generation, before, after)

    def _download(self, path, before, *, max_bytes=None, exclusive=False):
        max_bytes = MAX_EXPORT if max_bytes is None else max_bytes
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_EXPORT:
            raise CaptureFault("EXPORT_INVALID_LIMIT")
        self.last_size_metrics = {"export_limit_bytes": max_bytes,
                                  "export_content_length_bytes": None,
                                  "export_received_bytes": 0}
        # No query, credentials, redirects, dynamic origin, or content URLs.
        request = urllib.request.Request(ORIGIN + "/r/" + self.room + "/export",
                                         method="GET", headers={
            "Accept": "application/x-ndjson", "Accept-Encoding": "identity",
            "Cache-Control": "no-cache", "User-Agent": "technocore-full-capture/1",
        })
        try:
            try:
                response = self.tail_client._opener.open(request, timeout=25)
            except urllib.error.HTTPError as exc:
                response = exc
        except NETWORK_ERRORS as exc:
            raise RetryCapture("NETWORK_FAILURE") from exc
        started = time.monotonic()
        deadline = started + EXPORT_DEADLINE
        with response:
            check_status(response.code, response.headers.get("Retry-After"))
            media = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            generation = response.headers.get("X-Room-Generation", "")
            if (media != "application/x-ndjson"
                    or response.headers.get("Content-Encoding", "identity") != "identity"
                    or not generation.isascii() or not generation.isdecimal()
                    or len(generation) > 19 or int(generation) > 2**63 - 1):
                raise CaptureFault("EXPORT_HEADER_ANOMALY")
            if int(generation) != before["generation"]:
                raise CaptureFault("GENERATION_CHANGE")
            length = response.headers.get("Content-Length")
            if length is not None:
                if not length.isascii() or not length.isdecimal():
                    raise CaptureFault("EXPORT_LENGTH_ANOMALY")
                digits = length.lstrip("0") or "0"
                # Do not parse unbounded remote integers or invent a metric
                # when the declaration exceeds our safe integer range.
                if len(digits) > 19 or int(digits) > 2**63 - 1:
                    raise CaptureFault("EXPORT_TOO_LARGE")
                raw_length_size = len(length)
                length = int(digits)
                self.last_size_metrics["export_content_length_bytes"] = length
                if length > max_bytes:
                    raise CaptureFault("EXPORT_TOO_LARGE")
                if raw_length_size > 10:
                    # Keep the existing header acceptance bound, including
                    # unusually long zero-padded declarations below the cap.
                    raise CaptureFault("EXPORT_LENGTH_ANOMALY")
            total = 0
            flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
            with regular_open(path, flags) as file:
                while True:
                    if time.monotonic() > deadline:
                        raise RetryCapture("EXPORT_DEADLINE")
                    try:
                        # One underlying read permits a deadline check between
                        # chunks even when a peer trickles bytes very slowly.
                        chunk = response.read1(min(65536, max_bytes + 1 - total))
                    except NETWORK_ERRORS as exc:
                        raise RetryCapture("NETWORK_FAILURE") from exc
                    if not chunk:
                        break
                    total += len(chunk)
                    self.last_size_metrics["export_received_bytes"] = total
                    if total > max_bytes:
                        raise CaptureFault("EXPORT_TOO_LARGE")
                    file.write(chunk)
            if length is not None and total != length:
                raise RetryCapture("EXPORT_TRUNCATED")
        self.last_fetch_metrics = {"export_bytes": total,
                                   "fetch_seconds": time.monotonic() - started,
                                   "export_header": "application/x-ndjson; identity",
                                   "export_generation": int(generation)}
        return int(generation)


def head(view):
    return view["last_seq"] or 0


class SnapshotCapture:
    """Legacy snapshot harness for offline regressions and probe validation.

    No CLI ingestion path uses this class. Kept to compare old evidence and to
    validate independent byte-exact export observations without cursor mutation.
    """
    def __init__(self, archive, client, interval=5.0):
        if not math.isfinite(interval) or interval <= 0:
            raise CaptureFault("INVALID_INTERVAL")
        self.archive, self.client, self.interval = archive, client, interval

    def _validate(self, snapshot):
        a, s = self.archive, self.archive.state
        for view in (snapshot.before, snapshot.after):
            validate_envelope(view, a.room)
            if view["count"] > 1 or view["generation"] != snapshot.generation:
                raise CaptureFault("GENERATION_CHANGE")
        if s["generation"] is not None and snapshot.generation != s["generation"]:
            raise CaptureFault("GENERATION_CHANGE")
        first = last = None
        total = 0
        found_cursor = False
        expected_tails = {m["seq"]: m for v in (snapshot.before, snapshot.after) for m in v["messages"]}
        with regular_open(snapshot.path, os.O_RDONLY) as file:
            for raw in iter(lambda: file.readline(MAX_LINE + 1), b""):
                total += len(raw)
                if total > MAX_EXPORT:
                    raise CaptureFault("EXPORT_TOO_LARGE")
                obj = record(raw)
                seq = obj["seq"]
                if last is not None and seq != last + 1:
                    raise CaptureFault("EXPORT_SEQUENCE_HOLE")
                first = seq if first is None else first
                last = seq
                if seq in expected_tails and obj != expected_tails[seq]:
                    raise CaptureFault("EXPORT_TAIL_CONTENT_MISMATCH")
                if seq == s["cursor"]:
                    found_cursor = True
                    if hashlib.sha256(raw).hexdigest() != s["cursor_sha256"]:
                        raise CaptureFault("CURSOR_CONTENT_CHANGED")
        if head(snapshot.after) < head(snapshot.before):
            raise CaptureFault("REMOTE_CURSOR_REGRESSION")
        if last is None:
            # An idle ephemeral room can expose a retained high-water mark.
            # It is safe only if BOTH empty tails prove exactly our cursor in
            # the same generation. Unknown/newer/null heads cannot prove loss
            # absent and must remain a persistent review stop.
            if (s["cursor"] is not None and all(v["count"] == 0 and
                    head(v) == s["cursor"] for v in (snapshot.before, snapshot.after))):
                return None
            if s["cursor"] is not None or head(snapshot.before) or head(snapshot.after):
                raise CaptureFault("EMPTY_SNAPSHOT_UNCONFIRMED")
            return None
        if last < head(snapshot.before) or last > head(snapshot.after):
            raise CaptureFault("EXPORT_SNAPSHOT_BOUNDARY_MISMATCH")
        if s["cursor"] is not None:
            if first > s["cursor"] + 1:
                raise CaptureFault("GAP_REQUIRES_REVIEW")
            if last < s["cursor"]:
                raise CaptureFault("REMOTE_CURSOR_REGRESSION")
            if first <= s["cursor"] <= last and not found_cursor:
                raise CaptureFault("GAP_REQUIRES_REVIEW")
        return last

    def _ingest(self, snapshot):
        a = self.archive
        # Fully validate the bounded snapshot before any publication, including
        # trailing partial lines and gaps beyond a potential rotation boundary.
        self._validate(snapshot)
        a.checkpoint("snapshot_validated")
        lines, size = [], 0
        with regular_open(snapshot.path, os.O_RDONLY) as file:
            for raw in iter(lambda: file.readline(MAX_LINE + 1), b""):
                obj = record(raw)
                if a.state["cursor"] is not None and obj["seq"] <= a.state["cursor"]:
                    continue
                if lines and size + len(raw) > a.shard_bytes:
                    a.append(lines, snapshot.generation)
                    lines, size = [], 0
                lines.append(raw)
                size += len(raw)
            a.append(lines, snapshot.generation)
        a.state.update(status="RUNNING" if a.state["shards"] else "WAITING", error=None,
                       failures=0, last_success_at=time.time(), updated_at=time.time())
        a.save_state()
        # Catch-up does not bypass the room's cycle floor or the host budget.
        return self.interval

    def step(self):
        a = self.archive
        if a.state["status"] == "BLOCKED":
            raise CaptureFault("CAPTURE_REVIEW_REQUIRED")
        try:
            return self._fetch_and_ingest()
        except RetryCapture as exc:
            failures = a.state["failures"] + 1
            a.state.update(status="RETRY", error=exc.code, failures=failures, updated_at=time.time())
            a.save_state()
            return max(self.interval, exc.delay, backoff(failures))
        except GracefulStop:
            raise
        except ObserverError as exc:
            if str(exc) == "CAPTURE_STORAGE_LOW":
                raise GracefulStop("CAPTURE_STORAGE_LOW") from exc
            # Protocol errors have fixed codes in the shared strict validator.
            a.block(str(exc))
            raise
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                # Never persist speculative in-memory adoption on an I/O
                # failure. Restart validates any published successor normally.
                raise GracefulStop("CAPTURE_STORAGE_EXHAUSTED") from exc
            a.block("LOCAL_STORAGE_FAILURE")
            raise CaptureFault("LOCAL_STORAGE_FAILURE") from exc

    def _fetch_and_ingest(self):
        a = self.archive
        a.capacity(MAX_EXPORT + a.shard_bytes + 8192)
        return self._ingest(self.client.snapshot(a.path / ".fetch.pending"))


class FullCapture(SnapshotCapture):
    """Strict consecutive incremental reads, one bounded page per paced cycle.

    Empty archives require seq=1: no implicit tail anchor skips old messages.
    Existing durable snapshot archives resume from their recorded cursor.
    """

    def _fetch_and_ingest(self):
        a, s = self.archive, self.archive.state
        a.capacity(MAX_BODY + a.shard_bytes + 8192)
        cursor = s["cursor"] if s["cursor"] is not None else 0
        before = self.client.tail()
        page = self.client.page(cursor)
        after = self.client.tail()
        for view in (before, page, after):
            validate_envelope(view, a.room)
            if (view["generation"] != page["generation"] or
                    (s["generation"] is not None and view["generation"] != s["generation"])):
                raise CaptureFault("GENERATION_CHANGE")
        if before["count"] > 1 or after["count"] > 1 or page["count"] > POLL_LIMIT:
            raise CaptureFault("PAGE_LIMIT_EXCEEDED")
        if head(after) < head(before):
            raise CaptureFault("REMOTE_CURSOR_REGRESSION")
        messages = page["messages"]
        if messages:
            if page["first_seq"] <= cursor:
                # since is exclusive. Reject even identical overlap rather than
                # trusting an older record or mixing raw/canonical tip hashes.
                raise CaptureFault("PAGE_DUPLICATE_BOUNDARY")
            if page["first_seq"] != cursor + 1:
                raise CaptureFault("GAP_REQUIRES_REVIEW")
            if page["last_seq"] > head(after) or head(before) > page["last_seq"]:
                raise CaptureFault("PAGE_TAIL_BOUNDARY_MISMATCH")
            tails = {m["seq"]: m for v in (before, after) for m in v["messages"]}
            if any(m["seq"] in tails and m != tails[m["seq"]] for m in messages):
                raise CaptureFault("PAGE_TAIL_CONTENT_MISMATCH")
        else:
            # Empty last_seq echoes since upstream; it proves no high-water mark.
            # Require the pre-read tail to prove cursor and the post-read tail
            # to remain observable. Later appends are checked next cycle.
            if (page["first_seq"] is not None or page["last_seq"] != cursor
                    or head(before) != cursor or head(after) < cursor
                    or (cursor and (not before["count"] or not after["count"]))
                    or (not cursor and page["generation"] != 0)):
                raise CaptureFault("EMPTY_PAGE_UNCONFIRMED")
        # Validate every normalized record before publishing any part of a page.
        # JSON values are preserved, but original export bytes are NOT claimed.
        raw_lines = [(json_dump(message) + "\n").encode("ascii") for message in messages]
        for raw in raw_lines:
            record(raw)
        a.checkpoint("page_validated")
        lines, size = [], 0
        for raw in raw_lines:
            if lines and size + len(raw) > a.shard_bytes:
                a.append(lines, page["generation"])
                lines, size = [], 0
            lines.append(raw)
            size += len(raw)
        a.append(lines, page["generation"])
        s.update(status="RUNNING" if s["shards"] else "WAITING", error=None,
                 failures=0, last_success_at=time.time(), updated_at=time.time())
        a.save_state()
        return self.interval


def emit(value):
    print(json_dump(value), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "status", "verify"))
    parser.add_argument("--room", required=True)
    parser.add_argument("--archive-dir", required=True,
                        help="Pre-created dedicated room directory on the capture volume")
    parser.add_argument("--shard-bytes", type=int, default=DEFAULT_SHARD)
    parser.add_argument("--min-free-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--interval", type=float, default=5.0)
    add_budget_arguments(parser)
    args = parser.parse_args(argv)
    stop = threading.Event()
    try:
        if args.command != "run" and not (Path(args.archive_dir) / "capture.json").is_file():
            raise CaptureFault("CAPTURE_CHECKPOINT_MISSING")
        with Archive(args.archive_dir, args.room, args.shard_bytes, args.min_free_bytes) as archive:
            if args.command == "verify":
                emit(archive.verify(emit))
                return 0
            if args.command == "status":
                emit(archive.metrics())
                return 0
            if archive.state["status"] == "BLOCKED":
                raise CaptureFault("CAPTURE_REVIEW_REQUIRED")
            budget = configured_budget(args, stop)
            worker = FullCapture(archive, ReadClient(args.room, budget), args.interval)
            previous = {}
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
            try:
                while not stop.is_set():
                    started = time.monotonic()
                    delay = worker.step()
                    emit({**archive.metrics(), "minimum_cycle_wait_seconds": args.interval,
                          "ingestion": "incremental_newest_window",
                          "record_encoding": "normalized_json",
                          "forward_pagination_supported": False,
                          "cycle_seconds_including_budget": time.monotonic() - started,
                          "host_capture_rpm": budget.policy.capture_rpm,
                          "process_requests": budget.requests,
                          "process_budget_wait_seconds": budget.wait_seconds})
                    stop.wait(delay)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        return 0
    except GracefulStop as exc:
        emit({"status": "STOPPED", "error": str(exc), "restart_requires_continuity_check": True})
        return 0 if str(exc) == "CAPTURE_STOP_REQUESTED" else 2
    except ObserverError as exc:
        emit({"status": "BLOCKED", "error": str(exc)})
        return 1
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            emit({"status": "STOPPED", "error": "CAPTURE_STORAGE_EXHAUSTED",
                  "restart_requires_continuity_check": True})
            return 2
        emit({"status": "BLOCKED", "error": "LOCAL_CAPTURE_FAILURE"})
        return 1
    except (ValueError, KeyError, TypeError):
        emit({"status": "BLOCKED", "error": "LOCAL_CAPTURE_FAILURE"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
