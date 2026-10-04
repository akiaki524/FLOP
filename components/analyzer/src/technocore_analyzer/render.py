"""Safe display of external strings in Markdown / HTML / terminal.

Observed text is data. URLs are defanged (never fetched, previewed or expanded; some
Technocore GETs write), control and bidi characters are removed, and Markdown/HTML
metacharacters are escaped.
"""

from __future__ import annotations

import re

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]")
_URL = re.compile(r"(?i)\b(https?|ftp|wss?)://")
_WWW = re.compile(r"(?i)\bwww\.")
_MD = re.compile(r"([\\`*_{}\[\]()<>#+!|~>])")
_HTML = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}


def defang(text: str) -> str:
    text = _URL.sub(lambda m: m.group(1).lower().replace("http", "hxxp").replace("ftp", "fxp") + "[:]//", text)
    return _WWW.sub("www[.]", text)


def safe_md(value, limit: int = 280) -> str:
    """One-line, escaped, defanged Markdown text."""
    if value is None:
        return "—"
    text = str(value)
    text = _CONTROL.sub("", text).replace("\r", " ").replace("\n", " ⏎ ")
    text = text.encode("utf-8", "replace").decode("utf-8")
    if len(text) > limit:
        text = text[:limit] + "…"
    text = defang(text)
    text = "".join(_HTML.get(c, c) for c in text)
    return _MD.sub(r"\\\1", text)


# Secret-shaped strings. Detection is best-effort and never a guarantee.
_SECRETS = (
    ("PEM_PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)),
    ("ANTHROPIC_OR_OPENAI_KEY", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}")),
    ("GITHUB_TOKEN", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("SLACK_TOKEN", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("AWS_ACCESS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("DISCORD_WEBHOOK", re.compile(r"(?i)discord(?:app)?\.com/api/webhooks/\S+")),
    ("URL_CREDENTIAL_PARAM", re.compile(r"(?i)([?&](?:token|key|secret|sig|signature|auth|password|access_token)=)[^&\s]+")),
    ("URL_USERINFO", re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")),
    ("SEED_PHRASE_HINT", re.compile(r"(?i)\b(?:seed phrase|mnemonic|private key|秘密鍵)\b\s*[:=]?\s*\S.{0,200}")),
    ("LONG_HEX_SECRET", re.compile(r"(?i)\b(?:priv(?:ate)?|secret|seed)[_ -]?(?:key)?\s*[:=]\s*(?:0x)?[0-9a-f]{64}\b")),
)


def redact_secrets(text: str):
    """Return (text, [kinds]) with secret-shaped spans replaced by [REDACTED:<kind>]."""
    kinds = []
    for kind, pattern in _SECRETS:
        def sub(match, kind=kind):
            kinds.append(kind)
            if kind in ("URL_CREDENTIAL_PARAM", "URL_USERINFO"):
                return match.group(1) + f"[REDACTED:{kind}]" + ("@" if kind == "URL_USERINFO" else "")
            return f"[REDACTED:{kind}]"
        text = pattern.sub(sub, text)
    return text, kinds
