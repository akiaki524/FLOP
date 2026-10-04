"""Small deterministic helpers shared by every Analyzer module."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

SCHEMA_VERSION = "technocore-analyzer/1"
ANALYZER_VERSION = "0.1.0"

RFC3339 = re.compile(
    r"(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):([0-5]\d):([0-5]\d)"
    r"(?:\.(\d+))?(Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z")


class AnalyzerError(Exception):
    """A local, code-only error. Messages never contain observed text."""


def canonical(value) -> str:
    """Analyzer-owned canonical JSON (sorted keys, ASCII, compact). Not the tclk form."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                      allow_nan=False)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def digest(value) -> str:
    return sha256_text(canonical(value))


def parse_rfc3339_ms(value):
    """Unix ms for a timezone-qualified RFC 3339 string, else None. Never uses 'now'."""
    if not isinstance(value, str):
        return None
    match = RFC3339.fullmatch(value)
    if match is None:
        return None
    year, month, day, hour, minute, second, frac, zone = match.groups()
    try:
        tz = timezone.utc
        if zone != "Z":
            sign = 1 if zone[0] == "+" else -1
            hours, minutes = int(zone[1:3]), int(zone[4:6])
            from datetime import timedelta
            tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
        stamp = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second),
                         tzinfo=tz)
    except ValueError:
        return None
    millis = int((frac or "0")[:3].ljust(3, "0"))
    return int(stamp.timestamp()) * 1000 + millis


def ms_to_iso(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def normalize_for_similarity(text: str) -> str:
    """Derived search form only: never used for signatures or quotation."""
    return " ".join(text.casefold().split())
