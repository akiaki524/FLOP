"""Observe the pinned venue replay window through one byte-exact export.

The Human read ingress owns acquisition/coverage; the signer revalidates the
compact packet and selected signature without gaining a network capability.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time

from .material_fetch import transport
from .model import Invalid

PROJECT_DID = "did:key:z6MkjQVeF7TCAC6H6LKQnYFannWhsfWzzHhDDr3UhbZgHSTL"
ROOM = "tclk-offers"
URL = "https://technocore.chat/r/tclk-offers/export"
KIND = "PROJECT_DID_PUBLIC_NONCE_OBSERVATION"
PROFILE = "technocore-e4c4f73f3b28612d7161170b11e08e580b02123a"
READ_BUDGET = 1 << 20
MAX_EXPORT = 10 << 20
_DID = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}\Z")
_NONCE = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")


def _require(condition, reason):
    if not condition:
        raise Invalid(reason)


def _timestamp_ms(value):
    _require(type(value) is str and value, "NONCE_OBSERVATION_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Invalid("NONCE_OBSERVATION_TIMESTAMP_INVALID") from exc
    _require(parsed.tzinfo is not None, "NONCE_OBSERVATION_TIMESTAMP_INVALID")
    utc = parsed.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = utc - epoch
    result = delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000
    _require(0 <= result <= 8_640_000_000_000_000,
             "NONCE_OBSERVATION_TIMESTAMP_INVALID")
    return result


def _nonce(value):
    _require(type(value) in (str, int) and type(value) is not bool,
             "NONCE_OBSERVATION_NONCE_INVALID")
    text = str(value)
    _require(_NONCE.fullmatch(text) is not None,
             "NONCE_OBSERVATION_NONCE_INVALID")
    return text


def tail_lines(raw):
    """Equivalent record set/order to pinned reverse_lines on this snapshot.

    At a nonzero cutoff the first part is discarded even at a record start:
    reverse_lines cannot see its preceding newline. Count bytes before decoding.
    """
    start = max(0, len(raw) - READ_BUDGET)
    parts = raw[start:].split(b"\n")
    if start:
        parts = parts[1:]
    return [line for line in reversed(parts) if line]


def _server_record(raw):
    # orjson's supported integer range. Outside it, do not guess its numeric
    # fallback; STOP this unsupported source representation instead.
    def integer(text):
        value = int(text)
        _require(-(1 << 63) <= value < (1 << 64),
                 "NONCE_OBSERVATION_UNSUPPORTED_NUMBER")
        return value

    def floating(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("nonfinite")
        return value

    try:
        # Like the server: duplicate keys use their last value. Strings/floats
        # are not integer nonces. Invalid JSON/UTF-8 is skipped, not empty history.
        rec = json.loads(raw.decode("utf-8"), parse_int=integer,
                         parse_float=floating,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        # orjson also rejects escaped lone surrogates.
        json.dumps(rec, ensure_ascii=False).encode("utf-8")
    except Invalid:
        raise
    except (ValueError, UnicodeError):
        return None
    except RecursionError as exc:
        raise Invalid("NONCE_OBSERVATION_UNSUPPORTED_STRUCTURE") from exc
    return rec if isinstance(rec, dict) and isinstance(rec.get("seq"), int) else None


def parse_observation(raw, did=PROJECT_DID, *, captured_at_ms, generation,
                      profile=PROFILE):
    """Parse a fully acquired export, never a JSON read page or partial body."""
    _require(profile == PROFILE, "NONCE_OBSERVATION_PROFILE_UNSUPPORTED")
    _require(type(did) is str and _DID.fullmatch(did) is not None,
             "NONCE_OBSERVATION_DID_INVALID")
    _require(type(captured_at_ms) is int and 0 <= captured_at_ms <= 9007199254740991,
             "NONCE_OBSERVATION_CAPTURE_TIME_INVALID")
    # Existing activation/reconciliation state is generation 1. No epoch reset.
    _require(type(generation) is int and generation == 1,
             "NONCE_OBSERVATION_GENERATION_UNSUPPORTED")
    _require(type(raw) is bytes and len(raw) < MAX_EXPORT,
             "NONCE_OBSERVATION_INCOMPLETE_COVERAGE")
    _require(not raw or raw.endswith(b"\n"),
             "NONCE_OBSERVATION_INCOMPLETE_COVERAGE")
    lines = tail_lines(raw)
    selected = None
    for line in lines:
        if did.encode("ascii") not in line:
            continue
        row = _server_record(line)
        if row is None or row.get("from") != did or not isinstance(row.get("nonce"), int):
            continue
        # First qualifying record is authoritative; never fall back to an older
        # verifiable record if the selected envelope is unsupported/unverifiable.
        nonce = _nonce(row["nonce"])
        _require(type(row["seq"]) is int and 0 < row["seq"] <= 9007199254740991
                 and type(row.get("text")) is str and len(row["text"]) <= 4096
                 and type(row.get("sig")) is str,
                 "NONCE_OBSERVATION_RECORD_INVALID")
        selected = {"room": ROOM, "seq": row["seq"],
                    "timestampMs": _timestamp_ms(row.get("ts")),
                    "sender": did, "nonce": nonce, "signature": row["sig"],
                    "line": row["text"]}
        break
    observed = None if selected is None else selected["nonce"]
    return {
        "version": 2, "kind": KIND, "did": did, "room": ROOM,
        "observedNonce": observed, "observedNone": observed is None,
        "verifiedAtMs": captured_at_ms,
        "records": [] if selected is None else [selected],
        "source": {"url": URL, "method": "GET", "httpStatus": 200,
                   "generation": generation, "rawBytes": len(raw),
                   "rawSha256": hashlib.sha256(raw).hexdigest(),
                   "capturedAt": captured_at_ms},
        "coverage": {"complete": True, "basis": PROFILE,
                     "tailStart": max(0, len(raw) - READ_BUDGET),
                     "tailBytes": min(len(raw), READ_BUDGET),
                     "lineCount": len(lines)},
    }


def acquire_export(*, send=transport, clock_ms=None):
    """Acquire and frame-check one fixed export; no retry.

    Generation and HTTP framing are live response evidence.  ``PROFILE`` is a
    static compatibility pin consumed by the parsers, not an attestation of the
    currently deployed server implementation.
    """
    captured_at_ms = (int(time.time() * 1000) if clock_ms is None else clock_ms())
    status, headers, raw, error = send(URL, MAX_EXPORT, intake_export=True)
    _require(error is None, "NONCE_OBSERVATION_INCOMPLETE_COVERAGE")
    _require(status == 200, "NONCE_OBSERVATION_HTTP_FAILURE")
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    _require(content_type == "application/x-ndjson", "NONCE_OBSERVATION_CONTENT_TYPE")
    _require("location" not in headers, "NONCE_OBSERVATION_REDIRECT")
    _require(headers.get("content-encoding", "identity") == "identity",
             "NONCE_OBSERVATION_ENCODING")
    # A close-delimited EOF alone cannot distinguish truncation from completion.
    length = headers.get("content-length")
    chunked = headers.get("transfer-encoding") == "chunked"
    _require((length is None and chunked) or
             (not chunked and type(length) is str and length.isascii()
              and length.isdecimal() and int(length) == len(raw)),
             "NONCE_OBSERVATION_INCOMPLETE_COVERAGE")
    _require(headers.get("x-room-generation") == "1",
             "NONCE_OBSERVATION_GENERATION_UNSUPPORTED")
    return raw, captured_at_ms


def observe_project_nonce(*, did=PROJECT_DID, send=transport, clock_ms=None):
    """One fixed export through the existing bounded read transport; no retry."""
    raw, captured_at_ms = acquire_export(send=send, clock_ms=clock_ms)
    return parse_observation(raw, did, captured_at_ms=captured_at_ms, generation=1)
