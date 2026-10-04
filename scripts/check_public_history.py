#!/usr/bin/env python3
"""Scan every blob and commit reachable from every local Git ref.

Run this only for the in-place publication path from a trusted full clone.
It intentionally checks history that current-tree CI cannot see. Public
protocol identities may legitimately remain in implementation/security
bindings and are not treated as secrets.
"""

from __future__ import annotations

import re
import subprocess
import sys
from public_check_config import arguments, identifier_literals
from collections.abc import Iterable


MAX_BLOB_BYTES = 50_000_000

CLAUDE_SESSION_PREFIX = "https://claude.ai/code/" + "session_"
PRIVATE_NOTION_PREFIX = "https://app.notion.com/" + "p/"

LITERALS = {
    "private Claude session URL": CLAUDE_SESSION_PREFIX.encode(),
    "private Notion page URL": PRIVATE_NOTION_PREFIX.encode(),
}

PATTERNS = {
    "private-key PEM header": re.compile(
        rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    "GitHub classic token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "GitHub fine-grained token": re.compile(
        rb"\bgithub_pat_[A-Za-z0-9_]{20,}\b"
    ),
    "OpenAI-style secret key": re.compile(
        rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"
    ),
    "Anthropic-style secret key": re.compile(
        rb"\bsk-ant-[A-Za-z0-9_-]{20,}\b"
    ),
    "AWS access key id": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
}


def git(*args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.check_output(["git", *args], input=input_bytes)


def reachable_objects() -> dict[str, str]:
    rows = git("rev-list", "--objects", "--all").decode(
        "utf-8", errors="surrogateescape"
    )
    result: dict[str, str] = {}
    for line in rows.splitlines():
        sha, sep, path = line.partition(" ")
        result.setdefault(sha, path if sep else "")
    return result


def object_types(shas: Iterable[str]) -> dict[str, tuple[str, int]]:
    payload = "".join(f"{sha}\n" for sha in shas).encode()
    out = git(
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_bytes=payload,
    ).decode()
    result: dict[str, tuple[str, int]] = {}
    for line in out.splitlines():
        sha, kind, size = line.split()
        result[sha] = (kind, int(size))
    return result


def cat_blob(sha: str) -> bytes:
    return git("cat-file", "blob", sha)


def find_bytes(data: bytes, literals=None) -> list[str]:
    found: list[str] = []
    for name, value in (LITERALS if literals is None else literals).items():
        if value in data:
            found.append(name)
    for name, pattern in PATTERNS.items():
        if pattern.search(data):
            found.append(name)
    return found


def scan_commits(literals=None) -> list[str]:
    fmt = "%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x00"
    data = git("log", "--all", f"--format={fmt}")
    findings: list[str] = []
    fields = data.split(b"\0")
    for i in range(0, len(fields) - 1, 6):
        if i + 5 >= len(fields):
            break
        sha, author_name, author_email, committer_name, committer_email, body = fields[
            i : i + 6
        ]
        payload = b"\n".join(
            [author_name, author_email, committer_name, committer_email, body]
        )
        for name in find_bytes(payload, literals):
            findings.append(f"commit {sha.decode(errors='replace')}: {name}")
    return findings


def main(argv=None) -> int:
    _, config = arguments(argv)
    literals = {**LITERALS, **identifier_literals(config, binary=True)}
    objects = reachable_objects()
    types = object_types(objects)
    findings = scan_commits(literals)

    for sha, path in objects.items():
        kind, size = types.get(sha, ("", 0))
        if kind != "blob":
            continue
        where = path or "<unpathed blob>"
        if size > MAX_BLOB_BYTES:
            findings.append(
                f"blob {sha} {where}: exceeds full-history scan limit; manual review required"
            )
            continue
        data = cat_blob(sha)
        if b"\0" in data:
            continue
        for name in find_bytes(data, literals):
            findings.append(f"blob {sha} {where}: {name}")

    if findings:
        print("Full-history public scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1

    print("Full-history public scan passed across all local refs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
