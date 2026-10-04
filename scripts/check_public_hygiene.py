#!/usr/bin/env python3
"""Fail CI when Git-tracked text contains public-release blockers.

This is intentionally narrow. Public protocol identities such as the Project
DID may legitimately appear in implementation/tests as security bindings and
are not treated as secrets. This is not a substitute for the Path-B
full-history scanner in scripts/check_public_history.py.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from public_check_config import arguments, identifier_literals
from pathlib import Path


MAX_BYTES = 20_000_000
PRIVATE_CONFIG_PATH = Path(".local/public-check-identifiers.json")

CLAUDE_SESSION_PREFIX = "https://claude.ai/code/" + "session_"
PRIVATE_NOTION_PREFIX = "https://app.notion.com/" + "p/"

LITERALS = {
    "private Claude session URL": CLAUDE_SESSION_PREFIX,
    "private Notion page URL": PRIVATE_NOTION_PREFIX,
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


def tracked_files() -> list[Path]:
    out = subprocess.check_output(["git", "ls-files", "-z"])
    return [Path(p.decode()) for p in out.split(b"\0") if p]


def check_file(path: Path, literals=None) -> list[str]:
    if path == PRIVATE_CONFIG_PATH:
        return ["private identifier configuration must not be Git-tracked"]
    try:
        if path.is_symlink():
            data = os.fsencode(os.readlink(path))
        else:
            data = path.read_bytes()
    except OSError as exc:
        return [f"{path}: unreadable tracked file: {exc}"]

    if b"\0" in data:
        return []
    if len(data) > MAX_BYTES:
        return [f"{path}: oversized text file exceeds public scan limit"]

    findings: list[str] = []
    text = data.decode("utf-8", errors="replace")

    for name, value in (LITERALS if literals is None else literals).items():
        if value in text:
            findings.append(f"{path}: {name}")

    for name, pattern in PATTERNS.items():
        if pattern.search(data):
            findings.append(f"{path}: {name}")

    return findings


def main(argv=None) -> int:
    paths = tracked_files()
    if PRIVATE_CONFIG_PATH in paths:
        print("Public hygiene check failed: private identifier configuration must not be Git-tracked", file=sys.stderr)
        return 1
    _, config = arguments(argv)
    literals = {**LITERALS, **identifier_literals(config)}
    findings: list[str] = []
    for path in paths:
        findings.extend(check_file(path, literals))

    if findings:
        print("Public hygiene check failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        print(
            "\nThis gate checks the current tree only. "
            "A full-history scan is additionally required only for an in-place "
            "Private-to-Public release.",
            file=sys.stderr,
        )
        return 1

    print("Public hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
