"""tclk/1 Protocol Profile (deterministic; no LLM decides protocol state).

Source: flop-labs/tclk SPEC.md blob 99e3e677295354b88fe49efa69f84b1b78a1036c and the
reference implementation at commit 5cc4ab93efbc8999a3a7e1471b639deca25998ea
(src/frames.ts, machine.ts, transcript.ts, rails.ts). Apache-2.0; this module is an
independent Python port of the documented rules, not a copy of that code, and is not
claimed conformant merely because the reference exists (see tests/test_tclk.py).

Protocol state stays within the official enum ``proposed / accepted / locked / claimed /
refunded / cancelled``. Analyzer-side facts (record verdicts, coverage, deadline state,
rail verification) are separate fields and never extend that enum.
"""

from __future__ import annotations

import hashlib
import json
import re

from . import signature
from .util import parse_rfc3339_ms

PROFILE_ID = "tclk"
PROFILE_VERSION = "tclk/1@spec-99e3e677"
PREFIX = "tclk1 "
OTHER_VERSION = re.compile(r"tclk([0-9]+) ")
DOMAIN = "FLOP::tclk::v1"
OFFER_ROOM = "tclk-offers"
DEAL_ROOM = re.compile(r"mb-p-tclk-([0-9a-f]{16})\Z")
MAX_FRAME_CHARS = 4096
STATUSES = ("proposed", "accepted", "locked", "claimed", "refunded", "cancelled")
TERMINAL = {"claimed", "refunded", "cancelled"}

HEX32 = re.compile(r"0x[0-9a-f]{64}\Z")
HEX33 = re.compile(r"0x[0-9a-f]{66}\Z")
DID = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}\Z")
AMOUNT = re.compile(r"[1-9][0-9]*\Z")
ASSET = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
LEGACY_RAIL = re.compile(r"(?:[a-z0-9][a-z0-9._-]{0,63}|PaperRail)\Z")
CANONICAL_RAIL = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
CANONICAL_RAILS = {"btc-htlc", "evm-htlc", "flop-htlc", "memory", "near-htlc", "paper", "x402"}
RAIL_ALIASES = {"paperrail": "paper", "paper-rail": "paper"}
NONVALUE_RAILS = {"paper", "memory"}
NONCE = re.compile(r"[0-9a-f]{8,64}\Z")
SCALAR_HEX = re.compile(r"0x(?:[0-9a-f]{2}){1,32}\Z")
JOB_PROTO = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}\Z")
STATEMENT = re.compile(r"0x(?:[0-9a-f]{64}|[0-9a-f]{66})\Z")
MAX_SAFE = 2**53 - 1
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

FIELDS = {
    "offer": ({"type", "from", "role", "amount", "asset", "lock", "rails", "claimByMs",
               "refundAfterMs", "expiresMs", "paymentKey", "job", "nonce", "id"},
              ["type", "from", "role", "amount", "asset", "lock", "rails", "claimByMs",
               "refundAfterMs", "expiresMs", "nonce", "id"]),
    "accept": ({"type", "from", "ref", "statement", "contract", "paymentKey", "nonce"},
               ["type", "from", "ref", "statement", "contract", "nonce"]),
    "lock": ({"type", "from", "contract", "rail", "ref", "presig"},
             ["type", "from", "contract", "rail", "ref"]),
    "reveal": ({"type", "from", "contract", "ref", "secret"}, ["type", "from", "contract", "secret"]),
    "refund": ({"type", "from", "contract", "ref", "reason"}, ["type", "from", "contract"]),
    "cancel": ({"type", "from", "contract", "reason"}, ["type", "from", "contract"]),
    "receipt": ({"type", "from", "contract", "outcome", "rail", "ref"},
                ["type", "from", "contract", "outcome"]),
    "heartbeat": ({"type", "from", "contract", "nonce", "note"}, ["type", "from", "contract", "nonce"]),
}


class FrameError(ValueError):
    """The frame is rejected by a conforming decoder or guard."""


class Unverifiable(Exception):
    """A check needs a capability this runtime lacks (e.g. secp256k1). Not a rejection."""


# ── canonical encoding (mirrors JSON.stringify + sorted keys + ASCII escape) ─────────

def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                      allow_nan=False)


def _domain_hash(tag: str, payload: str) -> str:
    return "0x" + hashlib.sha256(f"{DOMAIN}|{tag}|{payload}".encode("ascii")).hexdigest()


def offer_id(fields: dict) -> str:
    return _domain_hash("offer", canonical_json({k: v for k, v in fields.items() if k != "id"}))


def contract_id(offer: dict, accept_core: dict) -> str:
    core = {k: v for k, v in accept_core.items() if v is not None}
    return _domain_hash("contract", canonical_json({"offer": offer, "accept": core}))


def deal_room(contract: str) -> str:
    return f"mb-p-tclk-{contract[2:18]}"


# ── curve helpers (optional cryptography) ────────────────────────────────────────────

def _point_valid(statement: str) -> bool:
    if not signature.HAVE_CRYPTO:
        raise Unverifiable("secp256k1 check needs 'cryptography'")
    from cryptography.hazmat.primitives.asymmetric import ec
    raw = bytes.fromhex(statement[2:])
    if len(raw) != 33 or raw[0] not in (2, 3):
        return False
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), raw)
    except ValueError:
        return False
    return True


def _point_witness(statement: str, secret: str) -> bool:
    if not signature.HAVE_CRYPTO:
        raise Unverifiable("secp256k1 check needs 'cryptography'")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    y = int(secret[2:], 16)
    if not 1 <= y < SECP256K1_N:
        return False
    point = ec.derive_private_key(y, ec.SECP256K1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
    return "0x" + point.hex() == statement.lower()


def verify_secret(lock: str, statement: str, secret: str) -> bool:
    if lock == "hash":
        return "0x" + hashlib.sha256(bytes.fromhex(secret[2:])).hexdigest() == statement.lower()
    if lock == "point":
        return _point_witness(statement, secret)
    return False


def valid_statement(lock: str, statement: str) -> bool:
    if lock == "hash":
        return bool(HEX32.fullmatch(statement))
    if lock == "point":
        return bool(HEX33.fullmatch(statement)) and _point_valid(statement)
    return False


def normalize_rail(value):
    if not isinstance(value, str) or value.strip() == "":
        raise FrameError("rail id must be a non-empty string")
    spelling = "".join(c.lower() if "A" <= c <= "Z" else c for c in value.strip())
    if not CANONICAL_RAIL.fullmatch(spelling):
        raise FrameError("malformed rail id")
    spelling = RAIL_ALIASES.get(spelling, spelling)
    if spelling not in CANONICAL_RAILS:
        raise FrameError("unknown rail id")
    return spelling


def offer_includes_rail(offered, selected) -> bool:
    if selected in offered:
        return True
    try:
        target = normalize_rail(selected)
    except FrameError:
        return False
    for rail in offered:
        try:
            if normalize_rail(rail) == target:
                return True
        except FrameError:
            continue
    return False


# ── decoding (fail-closed) ───────────────────────────────────────────────────────────

def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _parse_float(raw):
    # JSON.parse yields one Number type; keep exact integers as int like JS would.
    value = float(raw)
    return int(value) if value.is_integer() and abs(value) <= MAX_SAFE else value


def _reject_constant(name):
    raise FrameError("frame is not valid JSON")


def _string(frame, name, pattern=None):
    value = frame.get(name)
    if not isinstance(value, str) or value == "":
        raise FrameError(f"{name} must be a non-empty string")
    if pattern is not None and not pattern.fullmatch(value):
        raise FrameError(f"{name} is malformed")
    return value


def _ms(frame, name):
    value = frame.get(name)
    if type(value) is not int or not 0 < value <= MAX_SAFE:
        raise FrameError(f"{name} must be a positive unix-ms integer")
    return value


def _keys(record, allowed, required, what):
    for key in record:
        if key not in allowed:
            raise FrameError(f"unknown field on {what}")
    for key in required:
        if key not in record:
            raise FrameError(f"missing field on {what}: {key}")


def _payment_key(frame, name):
    key = _string(frame, name, HEX33)
    if not _point_valid(key):
        raise FrameError(f"{name} is not a valid secp256k1 point")


def validate_frame(frame) -> dict:
    if not isinstance(frame, dict):
        raise FrameError("frame must be an object")
    kind = frame.get("type")
    if not isinstance(kind, str) or kind not in FIELDS:  # list/dict types are unhashable
        raise FrameError("unknown frame type")
    allowed, required = FIELDS[kind]
    _keys(frame, allowed, required, kind)
    _string(frame, "from", DID)
    if kind == "offer":
        if frame["role"] not in ("payer", "payee"):
            raise FrameError("role must be payer|payee")
        _string(frame, "amount", AMOUNT)
        _string(frame, "asset", ASSET)
        if frame["lock"] not in ("hash", "point"):
            raise FrameError("lock must be hash|point")
        if not isinstance(frame["rails"], list) or not frame["rails"]:
            raise FrameError("rails must be a non-empty array")
        for rail in frame["rails"]:
            if not isinstance(rail, str) or not LEGACY_RAIL.fullmatch(rail):
                raise FrameError("rail is malformed")
        claim_by, refund_after = _ms(frame, "claimByMs"), _ms(frame, "refundAfterMs")
        _ms(frame, "expiresMs")
        if claim_by >= refund_after:
            raise FrameError("claimByMs must be strictly before refundAfterMs")
        if "paymentKey" in frame:
            _payment_key(frame, "paymentKey")
        if frame["lock"] == "point" and "paymentKey" not in frame:
            raise FrameError("point locks require paymentKey")
        if "job" in frame:
            job = frame["job"]
            if not isinstance(job, dict):
                raise FrameError("job must be an object")
            _keys(job, {"proto", "id", "context"}, ["proto", "id"], "job")
            _string(job, "proto", JOB_PROTO)
            _string(job, "id")
            if "context" in job:
                _string(job, "context")
        _string(frame, "nonce", NONCE)
        if frame["id"] != offer_id(frame):
            raise FrameError("offer id mismatch")
    elif kind == "accept":
        _string(frame, "ref", HEX32)
        _string(frame, "statement", STATEMENT)
        _string(frame, "contract", HEX32)
        if "paymentKey" in frame:
            _payment_key(frame, "paymentKey")
        _string(frame, "nonce", NONCE)
    elif kind == "lock":
        _string(frame, "contract", HEX32)
        _string(frame, "rail", LEGACY_RAIL)
        _string(frame, "ref")
        if "presig" in frame:
            presig = frame["presig"]
            if not isinstance(presig, dict):
                raise FrameError("presig must be an object")
            _keys(presig, {"nonce", "s"}, ["nonce", "s"], "presig")
            _string(presig, "nonce", HEX33)
            _string(presig, "s", SCALAR_HEX)
    elif kind == "reveal":
        _string(frame, "contract", HEX32)
        if "ref" in frame:
            _string(frame, "ref")
        _string(frame, "secret", HEX32)
    elif kind in ("refund", "cancel"):
        _string(frame, "contract", HEX32)
        if "ref" in frame:
            _string(frame, "ref")
        if "reason" in frame:
            _string(frame, "reason")
    elif kind == "receipt":
        _string(frame, "contract", HEX32)
        if frame["outcome"] not in ("claimed", "refunded", "cancelled"):
            raise FrameError("outcome must be claimed|refunded|cancelled")
        if "rail" in frame:
            _string(frame, "rail", LEGACY_RAIL)
        if "ref" in frame:
            _string(frame, "ref")
    elif kind == "heartbeat":
        _string(frame, "contract", HEX32)
        _string(frame, "nonce", NONCE)
        if "note" in frame:
            _string(frame, "note")
    return frame


def line_version(text):
    """'tclk/1' for a tclk1 line, 'tclk/<n>' for another prefix, None for other text."""
    if not isinstance(text, str):
        return None
    if text.startswith(PREFIX):
        return "tclk/1"
    match = OTHER_VERSION.match(text)
    return f"tclk/{match.group(1)}" if match else None


def decode_frame(text: str) -> dict:
    if not text.startswith(PREFIX):
        raise FrameError("not a tclk/1 line")
    if _utf16_len(text) > MAX_FRAME_CHARS:
        raise FrameError("frame exceeds the 4096-char cap")
    try:
        parsed = json.loads(text[len(PREFIX):], parse_float=_parse_float,
                            parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise FrameError("frame is not valid JSON") from None
    return validate_frame(parsed)


# ── contract state machine ──────────────────────────────────────────────────────────

def open_contract(offer: dict) -> dict:
    return {"status": "proposed", "offer": offer,
            "payerDid": offer["from"] if offer["role"] == "payer" else None,
            "payeeDid": offer["from"] if offer["role"] == "payee" else None,
            "contract": None, "statement": None, "rail": None, "railRef": None}


def _is_party(state, did):
    return did in (state["offer"]["from"], state["payerDid"], state["payeeDid"])


def apply_frame(state: dict, frame: dict, now_ms: int):
    """Return (next_state, ok, reason). Never mutates ``state``; invalid → unchanged."""
    def reject(reason):
        return state, False, reason
    kind, status, offer = frame["type"], state["status"], state["offer"]
    if kind == "offer":
        return reject("contract is already open")
    if kind == "accept":
        if status != "proposed":
            return reject(f"accept in status {status}")
        if frame["ref"] != offer["id"]:
            return reject("accept.ref names a different offer")
        if frame["from"] == offer["from"]:
            return reject("cannot accept own offer")
        if now_ms >= offer["expiresMs"]:
            return reject("offer has expired")
        expected = contract_id(offer, {"from": frame["from"], "ref": frame["ref"],
                                       "statement": frame["statement"],
                                       "paymentKey": frame.get("paymentKey"), "nonce": frame["nonce"]})
        if frame["contract"] != expected:
            return reject("contract id mismatch")
        if offer["lock"] == "point" and "paymentKey" not in frame:
            return reject("point locks require the acceptor's paymentKey")
        if not valid_statement(offer["lock"], frame["statement"]):
            return reject(f"statement does not fit a {offer['lock']} lock")
        payer_accepts = offer["role"] == "payee"
        return {**state, "status": "accepted", "contract": frame["contract"],
                "statement": frame["statement"],
                "payerDid": frame["from"] if payer_accepts else state["payerDid"],
                "payeeDid": state["payeeDid"] if payer_accepts else frame["from"]}, True, None
    if kind == "lock":
        if status != "accepted":
            return reject(f"lock in status {status}")
        if frame["contract"] != state["contract"]:
            return reject("lock names a different contract")
        if frame["from"] != state["payerDid"]:
            return reject("only the payer locks")
        if now_ms >= offer["refundAfterMs"]:
            return reject("refund window is already open")
        if not offer_includes_rail(offer["rails"], frame["rail"]):
            return reject("rail was not offered")
        return {**state, "status": "locked", "rail": frame["rail"], "railRef": frame["ref"]}, True, None
    if kind == "reveal":
        if status != "locked":
            return reject(f"reveal in status {status}")
        if frame["contract"] != state["contract"]:
            return reject("reveal names a different contract")
        if "ref" in frame and frame["ref"] != state["railRef"]:
            return reject("reveal names a different rail ref")
        if frame["from"] != state["payeeDid"]:
            return reject("only the payee reveals")
        if now_ms >= offer["refundAfterMs"]:
            return reject("refund window is open")
        if not verify_secret(offer["lock"], state["statement"], frame["secret"]):
            return reject("secret does not open the statement")
        return {**state, "status": "claimed"}, True, None
    if kind == "refund":
        if status != "locked":
            return reject(f"refund in status {status}")
        if frame["contract"] != state["contract"]:
            return reject("refund names a different contract")
        if "ref" in frame and frame["ref"] != state["railRef"]:
            return reject("refund names a different rail ref")
        if frame["from"] != state["payerDid"]:
            return reject("only the payer refunds")
        if now_ms < offer["refundAfterMs"]:
            return reject("refund window not open yet")
        return {**state, "status": "refunded"}, True, None
    if kind == "cancel":
        if status not in ("proposed", "accepted"):
            return reject(f"cancel in status {status}")
        if status == "accepted" and frame["contract"] != state["contract"]:
            return reject("cancel names a different contract")
        if not _is_party(state, frame["from"]):
            return reject("cancel from a non-party")
        return {**state, "status": "cancelled"}, True, None
    if kind == "receipt":
        if status not in TERMINAL:
            return reject("receipt before a terminal status")
        if frame["contract"] != state["contract"]:
            return reject("receipt names a different contract")
        if not _is_party(state, frame["from"]):
            return reject("receipt from a non-party")
        if frame["outcome"] != status:
            return reject("receipt outcome does not match status")
        if "rail" in frame and state["rail"] is not None and frame["rail"] != state["rail"]:
            return reject("receipt rail does not match contract rail")
        if "ref" in frame and state["railRef"] is not None and frame["ref"] != state["railRef"]:
            return reject("receipt ref does not match contract railRef")
        if status == "cancelled" and ("rail" in frame or "ref" in frame):
            return reject("receipt on cancelled contract cannot name a settlement rail")
        return state, True, None
    if kind == "heartbeat":
        if status not in ("accepted", "locked"):
            return reject(f"heartbeat in status {status}")
        if frame["contract"] != state["contract"]:
            return reject("heartbeat names a different contract")
        if not _is_party(state, frame["from"]):
            return reject("heartbeat from a non-party")
        return state, True, None
    return reject("unknown frame type")


# ── transcript analysis over observed records ────────────────────────────────────────

# Verdicts are Analyzer facts about one record; they are not protocol states.
ACCEPTED = "APPLIED"
ACKNOWLEDGED = "APPLIED_NO_TRANSITION"      # valid receipt / heartbeat
REJECTED = "REJECTED"                       # conforming guard/decoder refused it
REPLAY = "REJECTED_DUPLICATE_OR_REPLAY"
IGNORED_UNSIGNED = "IGNORED_UNSIGNED"       # unsigned frame is data, not a commitment
SIG_REJECTED = "REJECTED_SIGNATURE"         # not attributable to any DID
UNVERIFIABLE = "UNVERIFIABLE"               # verifier/material/time/curve unavailable
INSUFFICIENT = "INSUFFICIENT_HISTORY"       # prerequisite records not observed
UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
# Reasons that indicate the transcript, not the sender, lacks what the guard needs.
_NOT_A_VIOLATION = {"offer has expired"}


def stream_key(stream):
    """Numeric-aware stream order: epoch-2 before epoch-10 (segment names are zero-padded)."""
    head, _, tail = str(stream).rpartition("-")
    return (head, int(tail), "") if tail.isdigit() else (str(stream), -1, str(stream))


def _order_key(record, block_rank):
    """Within one room and one server generation, the server's seq is the order: a stream (Observer
    segment/epoch, snapshot epoch) is only a local partition of it, and which source's copy of a
    duplicate record was kept must not move it. Generations stay separate blocks (seq restarts
    there); blocks keep the previous stream-based order."""
    return (record["room"], block_rank[_block(record)], str(record.get("generation")),
            record["seq"] if record["seq"] is not None else -1)


def _block(record):
    gen = record.get("generation")
    return (record["room"], gen) if type(gen) is int else (record["room"], None, record["stream"])


def analyze(records, observed_rooms, as_of_ms, replay_cutoff_ms=None):
    """Fold every observed tclk record. ``records`` are dicts from the Analyzer store.

    Returns {"contracts": [...], "records": [...], "orphans": [...], "coverage": [...]}.
    """
    frames = []
    for rec in records:
        version = line_version(rec["message"].get("text"))
        if version is None:
            continue
        if replay_cutoff_ms is not None:
            t = parse_rfc3339_ms(rec["message"].get("ts"))
            if t is not None and t > replay_cutoff_ms:
                continue  # not yet observed at the replay time (kept in the cache, not in this state)
        frames.append((rec, version))
    block_rank = {}
    for rec, _ in frames:
        key, rank = _block(rec), stream_key(rec["stream"])
        block_rank[key] = min(block_rank.get(key, rank), rank)
    frames.sort(key=lambda item: _order_key(item[0], block_rank))
    verdicts, seen_signed = [], {}
    contracts, by_contract, orphans, coverage = {}, {}, [], []

    def verdict(rec, outcome, reason=None, kind=None, contract=None, attributable=None, finding=False):
        entry = {"ref": rec["ref"], "room": rec["room"], "stream": rec["stream"], "seq": rec["seq"],
                 "ts": rec["message"].get("ts"), "type": kind, "verdict": outcome, "reason": reason,
                 "contract": contract, "signature": rec["sig_status"],
                 "attributable_did": attributable, "protocol_invalid": finding}
        verdicts.append(entry)
        return entry

    deal_frames = {}
    for rec, version in frames:
        msg, room = rec["message"], rec["room"]
        if version != "tclk/1":
            verdict(rec, UNSUPPORTED_VERSION, f"{version} is not supported; not coerced to tclk/1")
            continue
        status = rec["sig_status"]
        if status == signature.UNSIGNED:
            verdict(rec, IGNORED_UNSIGNED, "unsigned frame is data, not a commitment")
            continue
        if status in (signature.INVALID, signature.MALFORMED, signature.UNSUPPORTED, signature.UNCHECKABLE):
            verdict(rec, SIG_REJECTED, f"signature {status}; not attributed to the claimed DID")
            continue
        if status != signature.VALID:
            verdict(rec, UNVERIFIABLE, f"signature {status}")
            continue
        signer = msg["from"]
        identity = (room, msg.get("nonce"), msg.get("sig"), msg.get("text"))
        prior = seen_signed.get(identity)
        if prior is not None and (prior["seq"], prior["stream"]) != (rec["seq"], rec["stream"]):
            verdict(rec, REPLAY, "same signed text already observed", attributable=signer)
            continue
        if prior is not None:
            continue  # the same stored record reached us through another source
        seen_signed[identity] = rec
        try:
            frame = decode_frame(msg["text"])
        except Unverifiable as exc:
            verdict(rec, UNVERIFIABLE, str(exc), attributable=signer)
            continue
        except FrameError as exc:
            verdict(rec, REJECTED, str(exc), attributable=signer, finding=True)
            continue
        if frame["from"] != signer:
            # The frame names a DID the record's signer does not hold. Only the signer
            # is attributable; the named DID is not implicated.
            verdict(rec, REJECTED, f"{frame['type']}.from does not match the record sender",
                    kind=frame["type"], attributable=signer, finding=True)
            continue
        ts_ms = parse_rfc3339_ms(msg.get("ts"))
        if ts_ms is None:
            verdict(rec, UNVERIFIABLE, "record time missing or malformed; never replaced by now",
                    kind=frame["type"], attributable=signer)
            continue
        if room == OFFER_ROOM and frame["type"] == "offer":
            if frame["id"] in contracts:
                verdict(rec, REPLAY, "offer id already open", kind="offer", attributable=signer)
                continue
            contracts[frame["id"]] = {"state": open_contract(frame), "offer_ref": rec["ref"],
                                      "offer_room": room, "offer_stream": rec["stream"],
                                      "steps": [], "rejections": 0, "replays": 0,
                                      "unverifiable": 0, "inconclusive": False}
            contracts[frame["id"]]["steps"].append(
                verdict(rec, ACCEPTED, None, kind="offer", contract=frame["id"], attributable=signer))
            continue
        if room == OFFER_ROOM and frame["type"] == "accept":
            entry = contracts.get(frame["ref"])
            if entry is None:
                orphans.append(verdict(rec, INSUFFICIENT, "referenced offer not observed before this accept",
                                       kind="accept", contract=frame.get("contract"), attributable=signer))
                continue
            _step(entry, frame, rec, ts_ms, signer, verdict)
            if entry["state"]["contract"]:
                by_contract[entry["state"]["contract"]] = entry
            continue
        if room == OFFER_ROOM:
            contract = frame.get("contract")
            entry = by_contract.get(contract) or contracts.get(contract)
            if entry is None:
                orphans.append(verdict(rec, INSUFFICIENT, "contract offer/accept not observed",
                                       kind=frame["type"], contract=contract, attributable=signer))
            elif entry["state"]["contract"] is not None:
                entry["rejections"] += 1
                entry["steps"].append(verdict(rec, REJECTED, f"{frame['type']} must be posted in the derived deal room",
                                              kind=frame["type"], contract=contract,
                                              attributable=signer, finding=True))
            else:
                _step(entry, frame, rec, ts_ms, signer, verdict)
            continue
        if DEAL_ROOM.fullmatch(room):
            deal_frames.setdefault(room, []).append((rec, frame, ts_ms, signer))
            continue
        verdict(rec, REJECTED, "tclk frame outside tclk-offers and deal rooms cannot advance state",
                kind=frame["type"], attributable=signer)

    present = set()  # (contract, type) frames that COULD have established a prerequisite transition
    applicable = [(entry, f, t) for room, items in deal_frames.items() for _, f, t, _ in items
                  for entry in [by_contract.get(f.get("contract"))]
                  if entry is not None and room == deal_room(entry["state"]["contract"])]
    lock_refs = {}  # contract -> rail refs of locks that could have been applied
    for entry, f, t in applicable:
        if f["type"] == "lock" and _can_establish(entry, f, t, None):
            lock_refs.setdefault(f["contract"], set()).add(f["ref"])
    for entry, f, t in applicable:
        if _can_establish(entry, f, t, lock_refs.get(f["contract"], set())):
            present.add((f["contract"], f["type"]))
    for room, items in deal_frames.items():
        for rec, frame, ts_ms, signer in items:
            entry = by_contract.get(frame.get("contract"))
            if entry is None:
                orphans.append(verdict(rec, INSUFFICIENT, "contract offer/accept not observed",
                                       kind=frame["type"], contract=frame.get("contract"),
                                       attributable=signer))
                continue
            expected = deal_room(entry["state"]["contract"])
            if room != expected:
                entry["rejections"] += 1
                entry["steps"].append(verdict(rec, REJECTED, f"{frame['type']} must be posted in the derived deal room",
                                              kind=frame["type"], contract=frame["contract"],
                                              attributable=signer, finding=True))
                continue
            _step(entry, frame, rec, ts_ms, signer, verdict, present)

    views = []
    for offer_key, entry in contracts.items():
        views.append(_contract_view(offer_key, entry, observed_rooms, as_of_ms))
        state = entry["state"]
        if state["contract"] and deal_room(state["contract"]) not in observed_rooms:
            coverage.append({"kind": "DEAL_ROOM_NOT_OBSERVED", "room": deal_room(state["contract"]),
                             "contract": state["contract"],
                             "limits": "lock and later frames cannot be observed; status is last observed only"})
    for entry in contracts.values():
        for gap in entry.get("missing", []):
            coverage.append({"kind": "TCLK_TRANSITION_NOT_OBSERVED", "ref": gap["ref"], "contract": gap["contract"],
                             "limits": f"a {gap['frame']} frame was seen but the {gap['missing_transition']} frame that "
                                       "must precede it was not observed; protocol status stays at the last confirmed "
                                       "state and no violation is asserted"})
    for orphan in orphans:
        coverage.append({"kind": "TCLK_PREREQUISITE_NOT_OBSERVED", "room": orphan["room"],
                         "ref": orphan["ref"], "contract": orphan["contract"],
                         "limits": "frame cannot be judged without its offer/accept history"})
    return {"profile": PROFILE_VERSION, "contracts": views, "records": verdicts, "orphans": orphans,
            "coverage": coverage}


# A rejection that only says "a required earlier transition has not happened" cannot be told
# apart from "Analyzer did not observe that transition" unless the transition is observed
# elsewhere in the contract's frames. (frame type, status) -> frame type that would have led there.
_MISSING_TRANSITION = {("lock", "proposed"): "accept", ("reveal", "proposed"): "accept",
                       ("refund", "proposed"): "accept", ("reveal", "accepted"): "lock",
                       ("refund", "accepted"): "lock"}
_RECEIPT_TERMINAL = {"claimed": "reveal", "refunded": "refund", "cancelled": "cancel"}


# The status a prerequisite frame is applied from.
_PRIOR_STATUS = {"lock": "accepted", "reveal": "locked", "refund": "locked", "cancel": "accepted"}


def _can_establish(entry, frame, ts_ms, lock_refs):
    """A frame satisfies "the prerequisite transition was observed" only if every state guard
    would accept it from the status that transition starts in: the same pure `apply_frame`
    (party, contract, deadline, rail, secret) evaluated on a hypothetical copy, never the real
    state. A reveal/refund naming a rail ref needs an applicable observed lock with that ref,
    otherwise the ref cannot be checked and the frame does not count. A frame that guards would
    reject (or that cannot be verified here) must not turn a missing history into a violation."""
    prior = _PRIOR_STATUS.get(frame["type"])
    if prior is None:
        return False
    state = {**entry["state"], "status": prior}
    if frame["type"] in ("reveal", "refund") and "ref" in frame:
        if lock_refs is None or frame["ref"] not in lock_refs:
            return False
        state["railRef"] = frame["ref"]
    try:
        _, ok, _ = apply_frame(state, frame, ts_ms)
    except Unverifiable:
        return False
    return ok


def _missing_transition(frame, status):
    if frame["type"] == "receipt" and status not in TERMINAL:
        return _RECEIPT_TERMINAL.get(frame.get("outcome"))
    return _MISSING_TRANSITION.get((frame["type"], status))


def _step(entry, frame, rec, ts_ms, signer, verdict, present=frozenset()):
    try:
        new, ok, reason = apply_frame(entry["state"], frame, ts_ms)
    except Unverifiable as exc:
        entry["unverifiable"] += 1
        entry["inconclusive"] = True
        entry["steps"].append(verdict(rec, UNVERIFIABLE, str(exc), kind=frame["type"],
                                      contract=frame.get("contract"), attributable=signer))
        return
    if ok:
        entry["state"] = new
        entry.setdefault("applied", set()).add(canonical_json(frame))
        outcome = ACKNOWLEDGED if frame["type"] in ("receipt", "heartbeat") else ACCEPTED
        entry["steps"].append(verdict(rec, outcome, None, kind=frame["type"],
                                      contract=frame.get("contract"), attributable=signer))
        return
    duplicate = _same_as_applied(entry, frame)
    if duplicate:
        entry["replays"] += 1
        entry["steps"].append(verdict(rec, REPLAY, reason, kind=frame["type"],
                                      contract=frame.get("contract"), attributable=signer))
        return
    needed = _missing_transition(frame, entry["state"]["status"]) if reason and " in status " in reason \
        or reason == "receipt before a terminal status" else None
    if needed is not None and (frame.get("contract"), needed) not in present:
        # Not judged: the last confirmed protocol state is kept and the gap is reported as coverage.
        entry["inconclusive"] = True
        entry.setdefault("missing", []).append({"ref": rec["ref"], "contract": frame.get("contract"),
                                               "frame": frame["type"], "missing_transition": needed})
        entry["steps"].append(verdict(rec, INSUFFICIENT, f"{reason}; the {needed} frame was not observed",
                                      kind=frame["type"], contract=frame.get("contract"), attributable=signer))
        return
    entry["rejections"] += 1
    entry["steps"].append(verdict(rec, REJECTED, reason, kind=frame["type"],
                                  contract=frame.get("contract"), attributable=signer,
                                  finding=reason not in _NOT_A_VIOLATION))


def _same_as_applied(entry, frame):
    return canonical_json(frame) in entry.get("applied", ())


def _contract_view(offer_key, entry, observed_rooms, as_of_ms):
    state, offer = entry["state"], entry["state"]["offer"]
    contract = state["contract"]
    room = deal_room(contract) if contract else None
    rails = offer["rails"]
    nonvalue = sorted({r for r in rails if _maybe_rail(r) in NONVALUE_RAILS})
    return {
        "offer_id": offer_key,
        "contract_id": contract,
        "protocol_status": state["status"],
        "protocol_status_basis": "reconstructed from observed, signature-valid frames only",
        "offer_ref": entry["offer_ref"],
        "role_of_offerer": offer["role"],
        "offerer_did": offer["from"],
        "payer_did": state["payerDid"],
        "payee_did": state["payeeDid"],
        "amount": offer["amount"], "asset": offer["asset"], "lock": offer["lock"], "rails": rails,
        "job": offer.get("job"),
        "deadlines_ms": {"expires": offer["expiresMs"], "claim_by": offer["claimByMs"],
                         "refund_after": offer["refundAfterMs"]},
        "deadline_state": {
            "as_of_ms": as_of_ms,
            "offer_expired": as_of_ms >= offer["expiresMs"],
            "claim_by_passed": as_of_ms >= offer["claimByMs"],
            "refund_window_open": as_of_ms >= offer["refundAfterMs"],
        },
        "deal_room": room,
        "deal_room_observed": (room in observed_rooms) if room else None,
        "coverage_state": ("INCONCLUSIVE" if entry["inconclusive"] else
                           "DEAL_ROOM_NOT_OBSERVED" if room and room not in observed_rooms else
                           "OBSERVED_ROOMS_ONLY"),
        "rail": state["rail"], "rail_ref": state["railRef"],
        "rail_verification": "NOT_CHECKED (Analyzer does not query settlement rails)",
        "value_backing_note": ("rails include non-value rehearsal rail(s): " + ",".join(nonvalue)
                               if nonvalue else None),
        "settlement_evidence": "NONE — chat frames (incl. receipt/heartbeat) never prove payment or delivery",
        "rejections": entry["rejections"], "replays": entry["replays"],
        "unverifiable_steps": entry["unverifiable"],
        "steps": [{k: s[k] for k in ("ref", "type", "verdict", "reason", "seq", "room")} for s in entry["steps"]],
    }


def _maybe_rail(rail):
    try:
        return normalize_rail(rail)
    except FrameError:
        return rail
