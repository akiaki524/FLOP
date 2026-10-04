"""Bounded, inert input formats and deterministic final message bytes."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unicodedata

LIMIT = 512_000
HEX = re.compile(r"[a-f0-9]{64}\Z")
TOKEN = re.compile(r"[A-Za-z0-9_.:/-]{1,160}\Z")


class Invalid(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Invalid(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def terminal_json(value):
    """Readable Japanese, while escaping terminal/bidi and line controls."""
    rendered = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    return "".join(json.dumps(c, ensure_ascii=True)[1:-1] if c != "\n" and unicodedata.category(c) in {"Cc", "Cf", "Zl", "Zp"}
                   else c for c in rendered)


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def decode(raw):
    require(len(raw) <= LIMIT, "JSON too large")
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(Invalid("non-finite JSON")))
    except (RecursionError, UnicodeError, json.JSONDecodeError) as exc:
        raise Invalid("malformed JSON") from exc


def read(path):
    path = Path(path)
    require(path.absolute() == path.resolve(), "symlink paths are forbidden")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "regular single-link file required")
        return decode(stream.read(LIMIT + 1))


def write_new(path, value):
    path = Path(path)
    data = canonical(value)
    require(len(data) <= LIMIT, "output too large")
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish complete bytes exclusively; never replace an existing artifact.
        os.link(temporary, path, follow_symlinks=False)
    finally:
        os.unlink(temporary)
    sync_dir(path.parent)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def keys(value, expected):
    require(type(value) is dict and set(value) == set(expected.split()), "unexpected object fields")


def string(value, limit=4000):
    require(type(value) is str and 0 < len(value) <= limit, "nonempty bounded text required")
    require(not any(ord(c) < 32 and c not in "\n\t" for c in value), "control characters forbidden")
    return value


def identifier(value):
    require(type(value) is str and HEX.fullmatch(value), "invalid SHA-256 identifier")
    return value


def bundle(value):
    keys(value, "version request scope destination sources")
    require(type(value["version"]) is int and value["version"] == 1, "unsupported version")
    string(value["request"])
    keys(value["scope"], "include exclude")
    for field in ("include", "exclude"):
        items = value["scope"][field]
        require(type(items) is list and 1 <= len(items) <= 20, "explicit scope boundaries required")
        for item in items:
            string(item, 500)
    keys(value["destination"], "room recipient")
    for item in value["destination"].values():
        require(type(item) is str and TOKEN.fullmatch(item), "invalid destination")
    sources = value["sources"]
    require(type(sources) is list and 1 <= len(sources) <= 10, "1..10 selected sources required")
    seen = set()
    for source in sources:
        keys(source, "id origin locator text sha256")
        require(type(source["id"]) is str and re.fullmatch(r"s[1-9][0-9]?", source["id"]), "invalid source id")
        require(source["id"] not in seen, "duplicate source id")
        seen.add(source["id"])
        require(source["origin"] in ("human", "scout"), "unknown source origin")
        string(source["locator"], 1000)
        string(source["text"], 30_000)
        require(source["sha256"] == sha(source["text"].encode("utf-8")), "source digest mismatch")
    require(len(canonical(value)) < LIMIT // 2, "bundle too large")
    return value


def review(value, frozen):
    keys(value, "provider summary findings unverified")
    require(value["provider"] in ("fixture-v1", "codex-cli-offline-v1", "claude-cli-offline-v1", "claude-cli-real-v1", "anthropic-messages-offline-v1", "anthropic-messages-real-v1"), "unknown provider")
    string(value["summary"])
    require(type(value["findings"]) is list and len(value["findings"]) <= 20, "too many findings")
    sources = {s["id"]: s for s in frozen["sources"]}
    for finding in value["findings"]:
        keys(finding, "severity observation evidence suggestion")
        require(finding["severity"] in ("info", "low", "medium", "high"), "invalid severity")
        string(finding["observation"])
        string(finding["suggestion"])
        evidence = finding["evidence"]
        keys(evidence, "source_id sha256 start_line end_line quote")
        require(evidence["source_id"] in sources, "unknown evidence source")
        source = sources[evidence["source_id"]]
        require(evidence["sha256"] == source["sha256"], "evidence digest mismatch")
        first, last = evidence["start_line"], evidence["end_line"]
        lines = source["text"].splitlines()
        require(type(first) is int and type(last) is int and 1 <= first <= last <= len(lines), "invalid evidence range")
        require(evidence["quote"] == "\n".join(lines[first - 1:last]), "quote differs from frozen evidence")
    require(type(value["unverified"]) is list and 1 <= len(value["unverified"]) <= 20, "unverified matters required")
    for item in value["unverified"]:
        string(item)
    return value


def payload(frozen, report):
    bundle(frozen)
    review(report, frozen)
    return {"protocol": "ccw.mock.v1", "action": "post-review", "task_id": digest(frozen),
            "destination": frozen["destination"],
            "body": canonical({"request": frozen["request"], "scope": frozen["scope"],
                               "sources": [{k: s[k] for k in ("id", "locator", "sha256")} for s in frozen["sources"]],
                               "review": report}).decode("ascii")}


def handoff(frozen, report):
    message = payload(frozen, report)
    return {"bundle": frozen, "review": report, "payload": message, "payload_sha256": digest(message)}


def validate_handoff(value, trusted):
    keys(value, "bundle review payload payload_sha256" + (" llm" if "llm" in value else ""))
    require(value["bundle"] == trusted, "task differs from Human-selected snapshot")
    core = {k: v for k, v in value.items() if k != "llm"}
    require(core == handoff(trusted, value["review"]), "handoff or final bytes changed")
    return value


def layout(root):
    root = Path(root).absolute()
    require(root == root.resolve(), "root must not contain symlinks")
    require(root.is_dir(), "run human init first")
    for name in ("worker", "human"):
        path = root / name
        require(not path.is_symlink() and path.is_dir(), "invalid role directory")
    return root
