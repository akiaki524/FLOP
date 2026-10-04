"""Technocore signed-lane verification: Ed25519 ``did:key`` over ``room|nonce|text``.

``seq`` and ``ts`` are venue metadata, not signed fields. Verification uses the exact
stored text (no Unicode normalization). ``cryptography`` is an optional dependency;
without it every signed record stays ``VERIFIER_UNAVAILABLE`` (never ``VALID``) and is
re-checked on a later run.

A valid signature attributes the text to a key. It does not prove who operates the key,
official affiliation, publication on Technocore, or that the content is true.
"""

from __future__ import annotations

import base64
import re

DID_RE = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}\Z")
SIG_RE = re.compile(r"[A-Za-z0-9_-]{85}[AQgw]\Z")
NONCE_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}

VALID = "VALID"
INVALID = "INVALID"
MALFORMED = "MALFORMED"
MATERIAL_ABSENT = "MATERIAL_ABSENT"
UNSIGNED = "UNSIGNED"
UNSUPPORTED = "UNSUPPORTED_IDENTITY"
UNAVAILABLE = "VERIFIER_UNAVAILABLE"
UNCHECKABLE = "UNCHECKABLE"
RECHECK = {UNAVAILABLE}

try:  # optional: signature verification and secp256k1 checks
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    HAVE_CRYPTO = True
except (KeyboardInterrupt, SystemExit):
    raise
except BaseException:  # ImportError, or a broken native binding (pyo3 panics are BaseException)
    HAVE_CRYPTO = False


def verifier_name():
    if not HAVE_CRYPTO:
        return None
    import cryptography
    return f"cryptography-{cryptography.__version__}-ed25519"


def did_public_key(did: str):
    if not isinstance(did, str) or not DID_RE.fullmatch(did):
        return None
    n = 0
    for ch in did[len("did:key:z"):]:
        n = n * 58 + _B58_INDEX[ch]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    if len(raw) != 34 or raw[:2] != b"\xed\x01":
        return None
    return raw[2:]


def nonce_text(nonce):
    """Exact decimal nonce text, or None. A leading-zero string is not canonical."""
    if type(nonce) is int and nonce >= 0:
        return str(nonce)
    if isinstance(nonce, str) and NONCE_RE.fullmatch(nonce):
        return nonce
    return None


def verify_record(room: str, message: dict) -> tuple[str, str | None]:
    """Return (status, detail). Never raises on hostile input."""
    sender, text = message.get("from"), message.get("text")
    has_sig, has_nonce = "sig" in message, "nonce" in message
    looks_did = isinstance(sender, str) and sender.startswith("did:")
    if not has_sig and not has_nonce:
        if looks_did:
            return MATERIAL_ABSENT, "did-shaped from without sig/nonce in the stored record"
        return UNSIGNED, None
    if not has_sig or not has_nonce:
        return MALFORMED, "sig and nonce must both be present"
    if not looks_did:
        return MALFORMED, "signature material without a did sender"
    key = did_public_key(sender)
    if key is None:
        return UNSUPPORTED, "only Ed25519 did:key (z6Mk) is verifiable"
    sig, nonce = message.get("sig"), nonce_text(message.get("nonce"))
    if not isinstance(sig, str) or not SIG_RE.fullmatch(sig):
        return MALFORMED, "signature is not canonical base64url"
    if nonce is None:
        return MALFORMED, "nonce is not canonical decimal"
    if not isinstance(text, str) or not isinstance(room, str):
        return UNCHECKABLE, "text missing or not a string"
    try:
        signed = f"{room}|{nonce}|{text}".encode("utf-8")
    except UnicodeEncodeError:
        return UNCHECKABLE, "text is not encodable as UTF-8 (lone surrogate)"
    if not HAVE_CRYPTO:
        return UNAVAILABLE, "install 'cryptography' to verify"
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(base64.urlsafe_b64decode(sig + "=="), signed)
    except (InvalidSignature, ValueError):
        # A mismatch is not proof of forgery: storage or conversion faults are possible.
        return INVALID, "signature does not verify over room|nonce|text"
    return VALID, None


def attributable_did(status: str, message: dict):
    """Only a VALID signature attributes a record to a DID's history."""
    return message.get("from") if status == VALID else None
