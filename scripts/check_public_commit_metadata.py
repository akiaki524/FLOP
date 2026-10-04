#!/usr/bin/env python3
"""Keep Project-owner commit metadata publication-safe without policing contributors."""

from __future__ import annotations

import subprocess
import sys
from public_check_config import arguments, identifier_literals


OWNER_NAMES = {"akiaki524"}
ALLOWED_EXACT = {
    "noreply@github.com",
    "noreply@anthropic.com",
    "noreply@openai.com",
}
ALLOWED_SUFFIX = "@users.noreply.github.com"


def publication_safe(email: str) -> bool:
    return email in ALLOWED_EXACT or email.endswith(ALLOWED_SUFFIX)


def metadata(revision: str) -> str:
    return subprocess.check_output(
        ["git", "log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce", revision]
    ).decode("utf-8", errors="replace")


def main(argv: list[str]) -> int:
    args, config = arguments(argv[1:], revisions=True)
    argv = [argv[0], *args.revisions]
    usernames = {v.lower() for v in config['usernames']}
    emails = {v.lower() for v in config['emails']}
    owner_names = OWNER_NAMES | {v.lower() for v in config['owner_names']}
    if len(argv) == 2:
        head = argv[1]
        raw = subprocess.check_output(
            ["git", "log", "-1", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce", head]
        ).decode("utf-8", errors="replace")
        label = head
    elif len(argv) == 3:
        base, head = argv[1], argv[2]
        raw = metadata(f"{base}..{head}")
        label = f"{base}..{head}"
    else:
        print(
            "usage: check_public_commit_metadata.py HEAD | BASE HEAD",
            file=sys.stderr,
        )
        return 2

    findings: list[str] = []
    for line in raw.split("\n"):
        fields = line.split("\0")
        if len(fields) != 5:
            continue
        sha, author_name, author_email, committer_name, committer_email = fields
        author_name, committer_name = author_name.lower(), committer_name.lower()
        author_email, committer_email = author_email.lower(), committer_email.lower()

        if usernames.intersection((author_name, committer_name)):
            findings.append(f"{sha}: known former personal username is present")
            continue

        # The known former private address must never re-enter any publication-bound
        # commit, regardless of the displayed author/committer name.
        if emails.intersection((author_email, committer_email)):
            findings.append(f"{sha}: known former private email is present")
            continue

        if author_name in owner_names and not publication_safe(author_email):
            findings.append(
                f"{sha}: Project-owner author email is not an approved no-reply identity"
            )
        if committer_name in owner_names and not publication_safe(committer_email):
            findings.append(
                f"{sha}: Project-owner committer email is not an approved no-reply identity"
            )

    if findings:
        print("Public commit-metadata check failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1

    print(f"Public commit-metadata check passed for {label}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
