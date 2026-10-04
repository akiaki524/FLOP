"""Test helpers. Archives are produced by the Observer's own Spool/ArchiveWorker code, so
the Adapter is exercised against the real on-disk format, not an Analyzer-invented one.
Synthetic data only; no network."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
OBSERVER_SRC = ROOT.parent / "observer" / "src"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(OBSERVER_SRC))

from technocore_analyzer import signature, tclk  # noqa: E402

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
except BaseException:  # pragma: no cover
    Ed25519PrivateKey = None

HAVE_CRYPTO = signature.HAVE_CRYPTO
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return out


class Key:
    def __init__(self, seed: int):
        self.private = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"seed%d" % seed).digest())
        public = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.did = "did:key:z" + b58(b"\xed\x01" + public)

    def sign(self, room, nonce, text):
        sig = self.private.sign(f"{room}|{nonce}|{text}".encode("utf-8"))
        return base64.urlsafe_b64encode(sig).decode().rstrip("=")

    def message(self, room, seq, text, ts, nonce=None):
        nonce = seq * 1000 + 7 if nonce is None else nonce
        return {"seq": seq, "ts": ts, "from": self.did, "text": text, "nonce": nonce,
                "sig": self.sign(room, nonce, text)}


def unsigned(seq, text, ts, nick="guest"):
    return {"seq": seq, "ts": ts, "from": nick, "text": text}


def ts(minute: int, day: int = 1, hour: int = 0) -> str:
    return f"2026-09-{day:02d}T{hour:02d}:{minute:02d}:00Z"


def page(room, messages, generation=1, since=0):
    return {"room": room, "generation": generation, "count": len(messages), "messages": messages,
            "first_seq": messages[0]["seq"] if messages else None,
            "last_seq": messages[-1]["seq"] if messages else since}


def build_archive(root: Path, room: str, pages, *, step_after_each=True):
    """Write pages through the Observer's Spool + ArchiveWorker. Returns archive dir."""
    from technocore_full_capture.spool import Spool
    from technocore_full_capture.spool_archive import ArchiveWorker
    spool_dir, archive_dir = root / room / "spool", root / room / "archive"
    spool_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    with Spool(spool_dir, room, producer=True, create=not (spool_dir / "spool.sqlite").exists(),
               min_free_bytes=0) as spool:
        for p in pages:
            spool.ingest(p)
        with ArchiveWorker(spool, archive_dir, min_free_bytes=0) as worker:
            while worker.step()["processed"]:
                pass
    return archive_dir


def tree_digest(path: Path):
    out = {}
    for item in sorted(path.rglob("*")):
        if item.is_file():
            st = item.stat()
            out[str(item.relative_to(path))] = (hashlib.sha256(item.read_bytes()).hexdigest(), st.st_mtime_ns, st.st_mode)
    return out


def offer(frm, nonce="9f2c81d04c9e1f7a", **over):
    fields = {"type": "offer", "from": frm, "role": "payer", "amount": "1000", "asset": "FLOP", "lock": "hash",
              "rails": ["flop-htlc", "paper"], "claimByMs": 1801000000000, "refundAfterMs": 1802000000000,
              "expiresMs": 1800000000000, "nonce": nonce}
    fields.update(over)
    fields["id"] = tclk.offer_id(fields)
    return fields


def accept(off, frm, preimage=b"\x01" * 32, nonce="0011223344556677"):
    statement = "0x" + hashlib.sha256(preimage).hexdigest()
    core = {"from": frm, "ref": off["id"], "statement": statement, "nonce": nonce}
    return {"type": "accept", **core, "contract": tclk.contract_id(off, core)}


def line(frame):
    return tclk.PREFIX + tclk.canonical_json(frame)


def write_config(path: Path, **cfg):
    base = {"state_db": "state/analyzer.sqlite", "output_dir": "out"}
    base.update(cfg)
    path.write_text(json.dumps(base), encoding="utf-8")
    return path
