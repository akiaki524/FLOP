"""Bounded streaming recovery joining retained export with a confirming poll."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile

from .archive import MAX_LINE, record, regular_open
from .deadline import RequestFailure
from .pacing import GracefulStop
from .spool import MAX_RECOVERY_MESSAGES, canonical, require
from technocore_observer.http import retry_after
from technocore_observer.protocol import POLL_LIMIT, ObserverError, decode_reply, validate_envelope

EXPORT_FAILURE_CODES = {
    "GENERATION_CHANGE": "RECOVERY_GENERATION_MISMATCH",
    "EXPORT_DEADLINE": "RECOVERY_EXPORT_TIMEOUT",
    "EXPORT_PROTOCOL_FAILURE": "RECOVERY_EXPORT_PROTOCOL_FAILURE",
    "LOCAL_IO_FAILURE": "RECOVERY_LOCAL_IO_FAILURE",
    "EXPORT_TOO_LARGE": "RECOVERY_EXPORT_TOO_LARGE",
}


@contextmanager
def recover(client, budget, room, state, envelope, *, spool, clock, telemetry):
    """Stage a proven prefix through after.last_seq; caller commits inside context.

    Only two bounded poll pages and one export record are held as objects.
    Private temporary files live on the guarded spool volume, never /tmp.
    Generation binding is observed, not atomic. No evidence is deleted.
    """
    require(hasattr(client, "export"), "RECOVERY_UNAVAILABLE")
    generation = state["generation"]
    try:
        telemetry.update(recovery_export_limit_bytes=None,
                         recovery_export_content_length_bytes=None,
                         recovery_export_received_bytes=None)
        limit = spool.recovery_limit()
        telemetry["recovery_export_limit_bytes"] = limit
        with tempfile.TemporaryDirectory(prefix=".recovery-", dir=spool.path) as directory:
            path = Path(directory) / "retained.ndjson"
            with getattr(budget, "recovery_request", budget.request)(room):
                started = clock()
                try:
                    reply = client.export(path, generation, max_bytes=limit)
                finally:
                    telemetry["recovery_export_seconds"] = max(0, clock() - started)
            require(reply.status == 200, "RECOVERY_EXPORT_HTTP_FAILURE")
            result = decode_reply(reply)
            for key in ("export_content_length_bytes", "export_received_bytes"):
                number = result.get(key)
                if type(number) is int and 0 <= number <= 2**63 - 1:
                    telemetry["recovery_" + key] = number
            if "failure" in result:
                failure = result["failure"]
                if failure == "HTTP_429":
                    delay = result.get("delay")
                    budget.defer(max(1, min(600, delay)) if type(delay) in (int, float) else 600)
                code = EXPORT_FAILURE_CODES.get(failure, "RECOVERY_EXPORT_FAILURE") \
                    if isinstance(failure, str) else "RECOVERY_EXPORT_FAILURE"
                require(False, code)
            require(type(result.get("generation")) is int and result["generation"] == generation,
                    "RECOVERY_GENERATION_MISMATCH")
            with getattr(budget, "recovery_request", budget.request)(room):
                after_reply = client.poll(0)
            if after_reply.status == 429:
                budget.defer(retry_after(after_reply.retry_after)[0] or 1)
            require(after_reply.status == 200, "RECOVERY_CONFIRMATION_FAILED")
            after = validate_envelope(decode_reply(after_reply), room)
            require(after["count"] <= POLL_LIMIT, "RECOVERY_CONFIRMATION_PAGE_LIMIT")
            require(after["generation"] == generation, "RECOVERY_GENERATION_MISMATCH")
            target = after["last_seq"]
            require(target is not None and target >= envelope["last_seq"], "RECOVERY_EXPORT_BOUNDARY_MISMATCH")
            require(target - state["cursor"] <= MAX_RECOVERY_MESSAGES, "RECOVERY_RANGE_LIMIT")
            expected = {m["seq"]: m for view in (envelope, after) for m in view["messages"]}
            require(all(expected[m["seq"]] == m for m in envelope["messages"]), "RECOVERY_CONTENT_MISMATCH")
            stage = Path(directory) / "proven.ndjson"
            digest = hashlib.sha256()
            first = last = None
            total = normalized = count = 0
            next_seq = state["cursor"] + 1
            with regular_open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as output:
                def append(message):
                    nonlocal normalized, count, next_seq
                    require(message["seq"] == next_seq, "RECOVERY_MISSING_RANGE")
                    raw = (canonical(message) + "\n").encode("ascii")
                    normalized += len(raw)
                    require(len(raw) <= MAX_LINE and normalized <= limit, "RECOVERY_NORMALIZED_LIMIT")
                    output.write(raw)
                    digest.update(raw)
                    count += 1
                    next_seq += 1

                with regular_open(path, os.O_RDONLY) as file:
                    for raw in iter(lambda: file.readline(MAX_LINE + 1), b""):
                        total += len(raw)
                        require(total <= limit, "RECOVERY_EXPORT_TOO_LARGE")
                        message = record(raw)
                        seq = message["seq"]
                        require(last is None or seq == last + 1, "RECOVERY_EXPORT_SEQUENCE_HOLE")
                        if first is None:
                            require(seq <= state["cursor"] + 1, "RECOVERY_OUTSIDE_RETENTION")
                        first = seq if first is None else first
                        last = seq
                        require(seq not in expected or expected[seq] == message, "RECOVERY_CONTENT_MISMATCH")
                        if seq > state["cursor"]:
                            append(message)
                require(first is not None and first <= state["cursor"] + 1 and
                        last >= state["cursor"] + 1, "RECOVERY_OUTSIDE_RETENTION")
                require(last <= target, "RECOVERY_EXPORT_BOUNDARY_MISMATCH")
                # Safe only when after's prefix overlaps or immediately follows
                # the export. A hole is never inferred away from the room head.
                for message in after["messages"]:
                    if message["seq"] > last:
                        append(message)
                require(next_seq == target + 1, "RECOVERY_MISSING_RANGE")
            yield {"path": stage, "generation": generation, "last_seq": target,
                   "count": count, "response_sha256": digest.hexdigest(),
                   "confirmation_raw_body": after_reply.body if spool.raw_responses else None}
    except RequestFailure as exc:
        if str(exc) == "CAPTURE_STOP_REQUESTED":
            raise GracefulStop("CAPTURE_STOP_REQUESTED") from exc
        raise ObserverError("RECOVERY_EXPORT_TIMEOUT" if str(exc) == "TOTAL_REQUEST_DEADLINE"
                            else "RECOVERY_EXPORT_TRANSPORT_FAILURE") from exc
    except OSError as exc:
        raise ObserverError("RECOVERY_LOCAL_IO_FAILURE") from exc
