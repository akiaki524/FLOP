"""Strict inert input and evidence formats; no acquisition or code evaluation."""

import hashlib
import json
from decimal import Decimal
import os
from pathlib import Path
import re
import stat

MAX_INPUT = 256_000
MAX_TEXT = 32_000
TOKEN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,95}\Z")


class Invalid(ValueError):
    pass


def check(condition, reason):
    if not condition:
        raise Invalid(reason)


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def digest(value):
    return sha(encode(value))


def _object(pairs):
    obj = {}
    for key, value in pairs:
        check(key not in obj, "duplicate_json_key")
        obj[key] = value
    return obj


def _constant(value):
    raise Invalid("non_finite_number")


def _bounded(value, depth=0, *, allow_decimals=False):
    check(depth <= 32, "json_depth_limit")
    if isinstance(value, dict):
        for item in value.values():
            _bounded(item, depth + 1, allow_decimals=allow_decimals)
    elif isinstance(value, list):
        for item in value:
            _bounded(item, depth + 1, allow_decimals=allow_decimals)
    elif isinstance(value, Decimal) and allow_decimals:
        check(value.is_finite(), "non_finite_number")
    elif isinstance(value, (float, Decimal)):
        # This contract deliberately uses integers, strings, bool and null only.
        raise Invalid("floating_point_unsupported")


def _decode(raw, *, document=False):
    check(type(raw) is bytes and len(raw) <= MAX_INPUT, "input_size_limit")
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                         parse_constant=_constant, parse_float=Decimal if document else float)
        _bounded(obj, allow_decimals=document)
        if not document:
            check(len(encode(obj)) <= MAX_INPUT, "canonical_size_limit")
        return obj
    except (UnicodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, Invalid):
            raise
        raise Invalid("malformed_json") from exc


def decode(raw):
    """Task contract stays integer-only, including nested params."""
    return _decode(raw)


def document_value(raw, pointer):
    """Select integer-only JSON data without rounding unrelated decimal tokens.

    Decimals are inert internal parse values, never returned or persisted as answers.
    Whole-document syntax, duplicate-key, non-finite and depth checks still apply.
    """
    value = pointer_value(_decode(raw, document=True), pointer)
    _bounded(value)
    check(len(encode(value)) <= MAX_INPUT, "canonical_size_limit")
    return value


def regular(path):
    path = Path(path).absolute()
    check(path == path.resolve(), "symlink_path")
    info = path.stat()
    check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "non_regular_file")
    return path


def read_input(path):
    path = regular(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "non_regular_file")
        raw = stream.read(MAX_INPUT + 1)
    check(len(raw) <= MAX_INPUT, "input_size_limit")
    return raw


def fields(obj, names):
    check(type(obj) is dict and set(obj) == set(names.split()), "unexpected_fields")


def token(value):
    check(type(value) is str and TOKEN.fullmatch(value), "invalid_identifier")


def text(value, limit=MAX_TEXT, empty=False):
    check(type(value) is str and (empty or len(value) > 0) and len(value) <= limit,
          "invalid_text")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise Invalid("invalid_unicode") from exc


def validate_task(task):
    fields(task, "version task_id family params evidence")
    check(type(task["version"]) is int and task["version"] == 1, "unsupported_version")
    token(task["task_id"])
    token(task["family"])
    check(type(task["params"]) is dict, "invalid_params")
    check(type(task["evidence"]) is list and 1 <= len(task["evidence"]) <= 8,
          "evidence_count")
    seen = set()
    for source in task["evidence"]:
        fields(source, "id locator text sha256")
        token(source["id"])
        check(source["id"] not in seen, "duplicate_source_id")
        seen.add(source["id"])
        text(source["locator"], 1000)
        text(source["text"], empty=True)
        check(source["sha256"] == sha(source["text"].encode("utf-8")), "source_digest_mismatch")
    return task


def fingerprint(task):
    # Renaming a task cannot cause a second solver execution.
    return digest({k: v for k, v in task.items() if k != "task_id"})


def source_for(task, source_id):
    token(source_id)
    for source in task["evidence"]:
        if source["id"] == source_id:
            return source
    raise Invalid("missing_source")


def pointer_value(value, pointer):
    text(pointer, 512, empty=True)
    check(pointer == "" or pointer.startswith("/"), "invalid_pointer")
    for part in pointer.split("/")[1:]:
        check(re.search(r"~(?![01])", part) is None, "invalid_pointer_escape")
        key = part.replace("~1", "/").replace("~0", "~")
        if type(value) is dict:
            if key not in value:
                raise LookupError("pointer_not_found")
            value = value[key]
        elif type(value) is list:
            check(re.fullmatch(r"0|[1-9][0-9]{0,5}", key) is not None, "invalid_array_index")
            if int(key) >= len(value):
                raise LookupError("pointer_not_found")
            value = value[int(key)]
        else:
            raise LookupError("pointer_not_found")
    return value


def citation(source, selector, quote):
    return {"source_id": source["id"], "sha256": source["sha256"],
            "locator": source["locator"], "selector": selector, "quote": quote}


def outcome(status, reason, *, value=None, evidence=None, verdict="UNDECIDED"):
    return {"status": status, "reason": reason, "value": value,
            "verdict": verdict, "evidence": evidence or []}


def validate_outcome(result, task):
    fields(result, "status reason value verdict evidence")
    check(result["status"] in ("COMPLETED", "UNKNOWN", "HUMAN_REVIEW"), "invalid_status")
    token(result["reason"])
    check(result["verdict"] in ("MATCH", "MISMATCH", "NOT_APPLICABLE", "UNDECIDED"),
          "invalid_verdict")
    check(type(result["evidence"]) is list, "invalid_citations")
    if result["status"] != "COMPLETED":
        check(result["value"] is None and result["verdict"] == "UNDECIDED", "unverified_answer")
    else:
        check(len(result["evidence"]) > 0, "missing_citations")
    for ref in result["evidence"]:
        fields(ref, "source_id sha256 locator selector quote")
        source = source_for(task, ref["source_id"])
        check(ref["sha256"] == source["sha256"] and ref["locator"] == source["locator"],
              "citation_source_mismatch")
        selector = ref["selector"]
        check(type(selector) is dict, "invalid_selector")
        if selector == {"kind": "text"}:
            expected = source["text"]
        elif selector.get("kind") == "lines":
            fields(selector, "kind first last")
            first, last = selector["first"], selector["last"]
            lines = source["text"].splitlines()
            check(type(first) is int and type(last) is int and 1 <= first <= last <= len(lines),
                  "invalid_line_range")
            expected = "\n".join(lines[first - 1:last])
        elif selector.get("kind") == "json_pointer":
            fields(selector, "kind pointer")
            expected = document_value(source["text"].encode("utf-8"), selector["pointer"])
        else:
            raise Invalid("invalid_selector")
        check(encode(ref["quote"]) == encode(expected), "citation_quote_mismatch")
    check(len(encode(result)) <= MAX_INPUT, "result_size_limit")
    return result
