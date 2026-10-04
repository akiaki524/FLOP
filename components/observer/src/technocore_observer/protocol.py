"""Strict ordering and bounded parsing; stored records are normalized JSON."""

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass

# v0.12.1 reads a 1 MiB tail. Allow up to 3x Unicode JSON re-escaping
# plus 1 MiB for record/envelope overhead. This is our acceptance budget,
# not a promise that every future/unknown record shape fits.
READ_BUDGET = 1 << 20
POLL_LIMIT = 200
MAX_BODY = 4 * READ_BUDGET
MAX_JSON_STRUCTURE = 32768
MAX_JSON_DEPTH = 32
ROOM_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}\Z", re.ASCII)


class ObserverError(Exception):
    """Only fixed, safe error codes cross the display boundary."""


class ProtocolAnomaly(ObserverError):
    pass


def validate_room(room):
    if not isinstance(room, str) or not ROOM_PATTERN.fullmatch(room):
        raise ObserverError("INVALID_ROOM")
    return room


def sanitize_for_display(value, limit=1000):
    result = []
    for char in str(value)[:limit]:
        if unicodedata.category(char).startswith("C") or char in "\u2028\u2029":
            result.append(f"\\u{ord(char):04x}")
        else:
            result.append(char)
    if len(str(value)) > limit:
        result.append("…[truncated]")
    return "".join(result)


def json_dump(value):
    # ensure_ascii also preserves JSON strings containing unpaired surrogates.
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def is_integer(value):
    return type(value) is int


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolAnomaly("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _nonfinite(value):
    raise ProtocolAnomaly("NONFINITE_JSON_NUMBER")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ProtocolAnomaly("NONFINITE_JSON_NUMBER")
    return number


@dataclass(frozen=True)
class Reply:
    status: int
    content_type: str
    body: bytes
    retry_after: str | None = None
    truncated: bool = False
    observed_at: float = 0.0


def decode_reply(reply):
    if reply.truncated or len(reply.body) > MAX_BODY:
        raise ProtocolAnomaly("BODY_TOO_LARGE")
    media_type = reply.content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json" and not (
        media_type.startswith("application/") and media_type.endswith("+json")
    ):
        raise ProtocolAnomaly("WRONG_CONTENT_TYPE")
    _json_budget(reply.body)
    try:
        obj = json.loads(reply.body.decode("utf-8"), object_pairs_hook=_pairs,
                         parse_constant=_nonfinite, parse_float=_float)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProtocolAnomaly("INVALID_JSON") from exc
    if not isinstance(obj, dict):
        raise ProtocolAnomaly("INVALID_ENVELOPE")
    return obj


def _json_budget(body):
    """Allocation-free lexical budget before json.loads builds Python objects.

    Count string starts and structural separators, including hostile unknown
    fields. Syntax/UTF-8/duplicates remain the strict decoder's responsibility.
    """
    depth = structure = 0
    quoted = escaped = False
    for byte in body:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
            structure += 1
        elif byte in (91, 123):
            depth += 1
            structure += 1
        elif byte in (93, 125):
            depth -= 1
        elif byte in (44, 58):
            structure += 1
        if depth > MAX_JSON_DEPTH or structure > MAX_JSON_STRUCTURE:
            raise ProtocolAnomaly("JSON_COMPLEXITY_LIMIT")


def validate_envelope(obj, room):
    required = {"room", "count", "first_seq", "last_seq", "generation", "messages"}
    if not required.issubset(obj):
        raise ProtocolAnomaly("MISSING_ENVELOPE_FIELD")
    if obj["room"] != room:
        raise ProtocolAnomaly("ROOM_MISMATCH")
    for name in ("count", "generation"):
        if not is_integer(obj[name]) or obj[name] < 0:
            raise ProtocolAnomaly("INVALID_" + name.upper())
    messages = obj["messages"]
    if not isinstance(messages, list) or obj["count"] != len(messages):
        raise ProtocolAnomaly("COUNT_MISMATCH")
    # Empty envelopes may expose a retained-room cursor. It never advances ours.
    for name in ("first_seq", "last_seq"):
        if not messages and obj[name] is None:
            continue
        if not is_integer(obj[name]) or obj[name] < 0:
            raise ProtocolAnomaly("INVALID_" + name.upper())
    if "wait_held" in obj and type(obj["wait_held"]) is not bool:
        raise ProtocolAnomaly("INVALID_WAIT_HELD")
    previous = None
    for message in messages:
        if not isinstance(message, dict):
            raise ProtocolAnomaly("INVALID_MESSAGE")
        seq = message.get("seq")
        if not is_integer(seq) or seq < 1:
            raise ProtocolAnomaly("INVALID_SEQ")
        # SQLite's integer range is part of this implementation's safe boundary.
        if seq > 2**63 - 1:
            raise ProtocolAnomaly("SEQ_OUT_OF_RANGE")
        if previous is not None:
            if seq <= previous:
                raise ProtocolAnomaly("NON_ASCENDING_SEQ")
            if seq != previous + 1:
                raise ProtocolAnomaly("INTERNAL_SEQ_HOLE")
        previous = seq
    if obj["generation"] > 2**63 - 1:
        raise ProtocolAnomaly("GENERATION_OUT_OF_RANGE")
    if messages:
        if messages[0]["seq"] != obj["first_seq"]:
            raise ProtocolAnomaly("FIRST_SEQ_MISMATCH")
        if messages[-1]["seq"] != obj["last_seq"]:
            raise ProtocolAnomaly("LAST_SEQ_MISMATCH")
    return obj


def deployment_config(obj):
    """Normalize only documented/read-observed config fields, without defaults.

    Live 0.12.1 exposes values under settings. The flat form remains readable for
    earlier fixtures/tools. max_wait is a ceiling, not a claimed default wait.
    Unknown/withheld settings are never recursively searched or interpreted.
    """
    version = obj.get("version")
    if not isinstance(version, str):
        raise ProtocolAnomaly("CONFIG_VERSION_MISSING")
    settings = obj.get("settings", obj)
    if not isinstance(settings, dict):
        raise ProtocolAnomaly("INVALID_CONFIG_SETTINGS")
    prefix = "settings." if "settings" in obj else ""
    fields = {
        "stillborn_seconds": ("stillborn_seconds",),
        "max_waiters_total": ("max_waiters_total",),
        "max_waiters_per_ip": ("max_waiters_per_ip",),
        "static_cache_seconds": ("static_cache_seconds",),
        "reads_per_minute_per_ip": ("rate_read", "reads_per_minute_per_ip"),
        "writes_per_minute_per_ip": ("rate_write", "writes_per_minute_per_ip"),
        "room_ring_bytes": ("room_ring_bytes",),
        "retention_seconds": ("retention_seconds",),
        "ephemeral_ttl_seconds": ("ephemeral_ttl_seconds",),
        "long_poll_seconds": ("long_poll_seconds",),
        "max_wait_seconds": ("max_wait", "max_wait_seconds"),
    }
    values, sources, unavailable, flags = {}, {}, [], []
    for name, keys in fields.items():
        key = next((key for key in keys if key in settings), None)
        value = settings[key] if key is not None else None
        sources[name] = prefix + key if key is not None else None
        valid = is_integer(value) and value >= 0
        if name == "retention_seconds" and valid:
            valid = value <= 2**31 - 1
        values[name] = value if valid else None
        if not valid:
            unavailable.append(name)
            if key is not None:
                flags.append("INVALID_CONFIG_SETTING:" + name)
    return {"version": version, "settings": values, "setting_sources": sources,
            "unavailable_settings": unavailable, "validation_flags": flags}


def content_values(message):
    flags = []
    for field in ("text", "from"):
        if field not in message:
            flags.append("MISSING_" + field.upper())
        elif not isinstance(message[field], str):
            flags.append("NON_STRING_" + field.upper())
    if "ts" not in message:
        flags.append("MISSING_TS")
    elif not isinstance(message["ts"], str):
        flags.append("INVALID_TS_TYPE")
    if "nonce" in message and not is_integer(message["nonce"]):
        flags.append("NON_INTEGER_NONCE")
    if "sig" in message and not isinstance(message["sig"], str):
        flags.append("NON_STRING_SIG")
    if set(message) - {"seq", "ts", "from", "text", "nonce", "sig"}:
        flags.append("UNKNOWN_FIELDS")
    text = message.get("text")
    text_hash = None
    stored_text = None
    if isinstance(text, str):
        try:
            encoded = text.encode("utf-8")
            stored_text = text
        except UnicodeEncodeError:
            # SQLite TEXT cannot encode lone surrogates; normalized JSON keeps them.
            flags.append("INVALID_UNICODE_TEXT")
            encoded = text.encode("utf-8", "surrogatepass")
        text_hash = hashlib.sha256(encoded).hexdigest()
    return {
        "ts_value": json_dump(message["ts"]) if "ts" in message else None,
        "from_value": json_dump(message["from"]) if "from" in message else None,
        "text_value": stored_text,
        "raw_record_json": json_dump(message),
        "validation_flags": json_dump(flags),
        "text_sha256": text_hash,
    }
