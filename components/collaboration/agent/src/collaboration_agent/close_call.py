"""Deterministic Close Call / close-1 protocol and planning adapter.

No LLM, no Secret custody, no Real write.  The current runtime slice verifies
the launched event, observes owner/mint evidence, and builds the exact owner
registration signing request.  It also builds one read-only, Human-selected
first-taker-long plan.  Strategy, counterparty discovery, signing and posting
are deliberately out of scope.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Iterable

from .material_fetch import transport as _default_transport

CONTEST_ID = "close-1"
PROJECT_DID = "did:key:z6MkjQVeF7TCAC6H6LKQnYFannWhsfWzzHhDDr3UhbZgHSTL"
OFFICIAL_RULES_COMMIT = "66c1da36538e4b1c685417d2f66922906b13fea0"
PACKAGE_MANIFEST_SHA256 = "bae09812e25eb6f1369c611f24964f7ea0acafddfc45301a16f33f941296dafa"
TRADING_ROOM = "close1"
REFEREE_ROOMS = (
    "d-close1-flow",
    "d-close1-state",
    "d-close1-price",
    "d-close1-positions",
    "d-close1-pnl",
)
PRICE_ROOM = "d-close1-price"
FLOW_ROOM = "d-close1-flow"
LOCK_SWEEP = 2556
MIN_QTY = Decimal("0.10")
BASE_FEE_RATE = Decimal("0.01")
EXPECTED_SWEEP_SECONDS = Decimal("300")
DID_RE = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}\Z")
SIG_RE = re.compile(r"[A-Za-z0-9_-]{86}\Z")
AMOUNT_RE = re.compile(r"[0-9]{1,7}(?:\.[0-9]{1,2})?\Z")
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
NONCE_RE = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")


class CloseCallError(ValueError):
    pass


def need(condition: bool, code: str) -> None:
    if not condition:
        raise CloseCallError(code)


def compact_json(value, *, sort_keys=False) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=sort_keys,
        allow_nan=False,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_decimal(value: str, *, minimum: Decimal | None = None) -> Decimal:
    need(type(value) is str and AMOUNT_RE.fullmatch(value) is not None, "INVALID_DECIMAL")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise CloseCallError("INVALID_DECIMAL") from exc
    need(number.is_finite() and number > 0, "INVALID_DECIMAL")
    if minimum is not None:
        need(number >= minimum, "DECIMAL_BELOW_MINIMUM")
    return number


def parse_time(value: str) -> datetime:
    need(type(value) is str and value, "INVALID_TIMESTAMP")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CloseCallError("INVALID_TIMESTAMP") from exc
    need(parsed.tzinfo is not None, "INVALID_TIMESTAMP")
    return parsed.astimezone(timezone.utc)


def _crypto_call(payload: dict, *, runner=subprocess.run) -> bool:
    helper = Path(__file__).with_name("close_call_crypto.mjs")
    need(helper.is_file(), "CRYPTO_HELPER_MISSING")
    raw = compact_json(payload)
    need(len(raw.encode("utf-8")) <= 64 * 1024, "CRYPTO_REQUEST_TOO_LARGE")
    try:
        result = runner(
            ["node", str(helper)], input=raw, text=True, capture_output=True,
            timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CloseCallError("CRYPTO_HELPER_UNAVAILABLE") from exc
    need(result.returncode in (0, 2), "CRYPTO_HELPER_FAILED")
    try:
        answer = json.loads(result.stdout)
    except ValueError as exc:
        raise CloseCallError("CRYPTO_HELPER_FAILED") from exc
    return answer.get("ok") is True


def verify_room_message(room: str, message: dict, *, runner=subprocess.run) -> bool:
    if room not in (*REFEREE_ROOMS, TRADING_ROOM) or not isinstance(message, dict):
        return False
    sender = message.get("from")
    nonce = str(message.get("nonce", ""))
    sig = message.get("sig")
    text = message.get("text")
    if not (
        type(sender) is str and DID_RE.fullmatch(sender)
        and NONCE_RE.fullmatch(nonce)
        and type(sig) is str and SIG_RE.fullmatch(sig)
        and type(text) is str and len(text) <= 4096
    ):
        return False
    return _crypto_call({
        "op": "verify-room",
        "room": room,
        "message": {"from": sender, "nonce": nonce, "sig": sig, "text": text},
    }, runner=runner)


def verify_room_messages(room: str, messages: list[dict], *, runner=subprocess.run) -> list[bool]:
    need(room in (*REFEREE_ROOMS, TRADING_ROOM), "ROOM_NOT_ALLOWED")
    need(isinstance(messages, list) and len(messages) <= 200, "CRYPTO_BATCH_INVALID")
    prepared = []
    shapes = []
    for message in messages:
        sender = message.get("from") if isinstance(message, dict) else None
        nonce = str(message.get("nonce", "")) if isinstance(message, dict) else ""
        sig = message.get("sig") if isinstance(message, dict) else None
        text = message.get("text") if isinstance(message, dict) else None
        good = bool(
            type(sender) is str and DID_RE.fullmatch(sender)
            and NONCE_RE.fullmatch(nonce)
            and type(sig) is str and SIG_RE.fullmatch(sig)
            and type(text) is str and len(text) <= 4096
        )
        shapes.append(good)
        prepared.append({
            "from": sender or "", "nonce": nonce, "sig": sig or "", "text": text or "",
        })
    batches = []
    batch = []
    raw = compact_json({"op": "verify-room-batch", "room": room, "messages": batch})
    for message in prepared:
        candidate = compact_json({
            "op": "verify-room-batch", "room": room, "messages": batch + [message],
        })
        if len(candidate.encode("utf-8")) > 64 * 1024:
            need(bool(batch), "CRYPTO_REQUEST_TOO_LARGE")
            batches.append((raw, len(batch)))
            batch = []
            candidate = compact_json({
                "op": "verify-room-batch", "room": room, "messages": [message],
            })
            need(len(candidate.encode("utf-8")) <= 64 * 1024, "CRYPTO_REQUEST_TOO_LARGE")
        batch.append(message)
        raw = candidate
    batches.append((raw, len(batch)))
    helper = Path(__file__).with_name("close_call_crypto.mjs")
    verified = []
    for raw, count in batches:
        try:
            result = runner(
                ["node", str(helper)], input=raw, text=True, capture_output=True,
                timeout=3, check=False,
            )
            need(result.returncode in (0, 2), "CRYPTO_HELPER_FAILED")
            values = json.loads(result.stdout).get("ok")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise CloseCallError("CRYPTO_HELPER_UNAVAILABLE") from exc
        need(isinstance(values, list) and len(values) == count, "CRYPTO_HELPER_FAILED")
        verified.extend(values)
    return [shape and value is True for shape, value in zip(shapes, verified)]


def owner_text(did: str = PROJECT_DID) -> str:
    need(DID_RE.fullmatch(did) is not None and did == PROJECT_DID, "WRONG_DID")
    return compact_json({"t": "owner", "season": CONTEST_ID, "key": did})


# Official trade protocol primitives. No strategy or execution policy lives here.
def canonical_terms(terms: dict) -> str:
    need(
        type(terms) is dict
        and set(terms) == {"id", "maker", "px", "qty", "side", "taker", "until"},
        "INVALID_TERMS",
    )
    need(type(terms["id"]) is str and ID_RE.fullmatch(terms["id"]), "INVALID_TRADE_ID")
    need(type(terms["maker"]) is str and DID_RE.fullmatch(terms["maker"]), "INVALID_MAKER")
    parse_decimal(terms["px"])
    parse_decimal(terms["qty"], minimum=MIN_QTY)
    need(terms["side"] in ("buy", "sell"), "INVALID_SIDE")
    need(
        terms["taker"] == "any"
        or (type(terms["taker"]) is str and DID_RE.fullmatch(terms["taker"])),
        "INVALID_TAKER",
    )
    need(type(terms["until"]) is int and 1 <= terms["until"] <= LOCK_SWEEP, "INVALID_UNTIL")
    return compact_json(terms, sort_keys=True)


def maker_preimage(terms: dict) -> str:
    return f"{CONTEST_ID}|terms|{canonical_terms(terms)}"


def taker_preimage(terms: dict, taker: str = PROJECT_DID) -> str:
    need(DID_RE.fullmatch(taker) is not None, "INVALID_TAKER")
    return f"{CONTEST_ID}|accept|{canonical_terms(terms)}|{taker}"


def trade_text(terms: dict, taker: str, maker_sig: str, taker_sig: str) -> str:
    canonical_terms(terms)
    need(DID_RE.fullmatch(taker) is not None, "INVALID_TAKER")
    need(
        SIG_RE.fullmatch(maker_sig or "") is not None
        and SIG_RE.fullmatch(taker_sig or "") is not None,
        "INVALID_TRADE_SIGNATURE",
    )
    need(terms["taker"] in ("any", taker), "TAKER_BINDING")
    return compact_json({
        "t": "trade", "season": CONTEST_ID, "terms": terms, "taker": taker,
        "maker_sig": maker_sig, "taker_sig": taker_sig,
    })


def load_maker_packet(path: str | Path) -> dict:
    source = Path(path)
    try:
        need(source.is_file() and not source.is_symlink(), "MAKER_PACKET_UNAVAILABLE")
        raw = source.read_bytes()
        need(len(raw) <= 64 * 1024, "MAKER_PACKET_TOO_LARGE")

        def strict_object(pairs):
            value = {}
            for key, item in pairs:
                need(key not in value, "MAKER_PACKET_DUPLICATE_KEY")
                value[key] = item
            return value

        value = json.loads(raw.decode("utf-8"), object_pairs_hook=strict_object)
    except CloseCallError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise CloseCallError("MAKER_PACKET_UNAVAILABLE") from exc
    need(type(value) is dict, "MAKER_PACKET_INVALID")
    return value


def verify_maker_packet(packet: dict, *, runner=subprocess.run) -> dict:
    need(
        type(packet) is dict and set(packet) == {"terms", "maker_sig"},
        "MAKER_PACKET_INVALID",
    )
    terms = packet["terms"]
    canonical = canonical_terms(terms)
    maker = terms["maker"]
    maker_sig = packet["maker_sig"]
    need(maker != PROJECT_DID, "SELF_MAKER_NOT_ALLOWED")
    need(terms["side"] == "sell", "MAKER_SIDE_NOT_SELL")
    need(terms["taker"] in ("any", PROJECT_DID), "TAKER_NOT_PROJECT_DID")
    need(type(maker_sig) is str and SIG_RE.fullmatch(maker_sig), "INVALID_MAKER_SIGNATURE")
    need(_crypto_call({
        "op": "verify-maker-terms",
        "maker": maker,
        "canonicalTerms": canonical,
        "signature": maker_sig,
    }, runner=runner), "INVALID_MAKER_SIGNATURE")
    return {
        "terms": json.loads(canonical),
        "canonicalTerms": canonical,
        "makerSig": maker_sig,
        "termsSha256": sha256_text(canonical),
    }


def within_limits(px: Decimal, limits: list[str]) -> bool:
    need(isinstance(limits, list) and len(limits) == 2, "PRICE_LIMITS_INVALID")
    low, high = parse_decimal(limits[0]), parse_decimal(limits[1])
    return low <= px <= high


def within_five_percent(px: Decimal, reference: Decimal) -> bool:
    return abs(px - reference) <= reference * Decimal("0.05")


def registration_sign_request() -> dict:
    text = owner_text()
    return {
        "version": 1,
        "kind": "CLOSE_CALL_TYPED_SIGN_REQUEST",
        "action": "CLOSE_CALL_OWNER_REGISTER",
        "expectedDid": PROJECT_DID,
        "subject": {"season": CONTEST_ID, "did": PROJECT_DID},
        "binding": {
            "contest": CONTEST_ID,
            "rulesCommit": OFFICIAL_RULES_COMMIT,
            "packageManifestSha256": PACKAGE_MANIFEST_SHA256,
            "room": TRADING_ROOM,
            "noncePolicy": "SIGNER_ALLOCATES_ROOM_NONCE",
        },
        "preview": {"sha256": sha256_text(text), "utf8": text},
    }


def _room_url(room: str, limit: int) -> str:
    need(room in (*REFEREE_ROOMS, TRADING_ROOM), "ROOM_NOT_ALLOWED")
    need(type(limit) is int and 1 <= limit <= 200, "INVALID_LIMIT")
    return f"https://technocore.chat/r/{room}?format=json&limit={limit}"


def _decode_room(raw: bytes) -> list[dict]:
    need(type(raw) is bytes and len(raw) <= 512 * 1024, "ROOM_RESPONSE_TOO_LARGE")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise CloseCallError("ROOM_RESPONSE_INVALID") from exc
    messages = value.get("messages") if isinstance(value, dict) else value
    need(isinstance(messages, list), "ROOM_RESPONSE_INVALID")
    result = []
    for row in messages:
        if not isinstance(row, dict) or not all(k in row for k in ("from", "nonce", "sig", "text")):
            continue
        result.append({
            "from": row["from"], "nonce": str(row["nonce"]), "sig": row["sig"],
            "text": row["text"], "seq": row.get("seq"), "ts": row.get("ts"),
        })
    return result


def read_room(room: str, limit: int, *, send=None) -> list[dict]:
    send = send or _default_transport
    need(callable(send), "READ_TRANSPORT_UNAVAILABLE")
    status, headers, raw, error = send(_room_url(room, limit), 512 * 1024)
    need(error is None and status == 200, "ROOM_READ_FAILED")
    ctype = headers.get("content-type", "") if isinstance(headers, dict) else ""
    need("json" in ctype.lower() or not ctype, "ROOM_CONTENT_TYPE")
    return _decode_room(raw)


def read_room_owner(room: str, *, send=None) -> str | None:
    send = send or _default_transport
    need(callable(send), "READ_TRANSPORT_UNAVAILABLE")
    status, _, raw, error = send(
        f"https://technocore.chat/kv/room-owners/{room}", 16 * 1024,
    )
    if error is not None or status != 200:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return None
    for line in text.splitlines():
        candidate = line.strip()
        if DID_RE.fullmatch(candidate):
            return candidate
    return None


def parse_json_record(message: dict) -> dict | None:
    try:
        value = json.loads(message["text"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def verify_launch_pin(pin: dict, *, runner=subprocess.run) -> dict:
    need(
        type(pin) is dict
        and set(pin) == {"version", "source", "refereeDid", "message"},
        "LAUNCH_PIN_INVALID",
    )
    need(pin["version"] == 1 and type(pin["source"]) is str and pin["source"], "LAUNCH_PIN_INVALID")
    referee = pin["refereeDid"]
    need(type(referee) is str and DID_RE.fullmatch(referee), "LAUNCH_PIN_REFEREE")
    message = pin["message"]
    need(isinstance(message, dict) and message.get("from") == referee, "LAUNCH_PIN_REFEREE")
    need(verify_room_message(PRICE_ROOM, message, runner=runner), "LAUNCH_PIN_SIGNATURE")
    record = parse_json_record(message)
    need(
        record is not None and record.get("t") == "seed"
        and record.get("season") == CONTEST_ID,
        "LAUNCH_PIN_SEED",
    )
    need(record.get("package") == PACKAGE_MANIFEST_SHA256, "LAUNCH_PIN_PACKAGE")
    need(sorted(record.get("rooms", [])) == sorted(REFEREE_ROOMS), "LAUNCH_PIN_ROOMS")
    return {"refereeDid": referee, "seed": record, "source": pin["source"]}


def load_launch_pin(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise CloseCallError("LAUNCH_PIN_UNAVAILABLE") from exc


def _verified_referee_posts(
    room: str, messages: Iterable[dict], referee: str, *, runner=subprocess.run,
) -> list[tuple[dict, dict]]:
    selected = [message for message in messages if message.get("from") == referee]
    checks = verify_room_messages(room, selected, runner=runner) if selected else []
    result = []
    for message, ok in zip(selected, checks):
        if ok:
            record = parse_json_record(message)
            if record is not None:
                result.append((message, record))
    return result


def _decimal_seconds(delta) -> Decimal:
    micros = (
        delta.days * 86400 * 1_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    return Decimal(micros) / Decimal(1_000_000)


def _price_record(record: dict, *, now: datetime) -> dict | None:
    if record.get("t") != "price":
        return None
    n, ref, limits = record.get("n"), record.get("ref"), record.get("limits")
    if not (type(n) is int and 1 <= n <= LOCK_SWEEP and type(ref) is dict):
        return None
    if not {"px", "time", "tid"}.issubset(ref):
        return None
    if type(ref["tid"]) not in (str, int) or type(ref["tid"]) is bool:
        return None
    try:
        ref_px = parse_decimal(ref["px"])
        ref_time = parse_time(ref["time"])
        need(isinstance(limits, list) and len(limits) == 2, "PRICE_LIMITS_INVALID")
        low, high = parse_decimal(limits[0]), parse_decimal(limits[1])
        need(low <= high and low <= ref_px <= high, "PRICE_LIMITS_INVALID")
        age = _decimal_seconds(now - ref_time)
        need(age >= 0, "PRICE_REFERENCE_FROM_FUTURE")
    except CloseCallError:
        return None
    return {
        "sweep": n,
        "ref": {"px": ref["px"], "time": ref["time"], "tid": ref["tid"]},
        "limits": list(limits),
        "referenceAgeSeconds": format(age, "f"),
        "stale": age > EXPECTED_SWEEP_SECONDS,
        "expectedFreshnessSeconds": format(EXPECTED_SWEEP_SECONDS, "f"),
    }


def collect_current_price(
    *, launch: dict, send=None, runner=subprocess.run, now: datetime | None = None,
) -> dict:
    referee = launch["refereeDid"]
    current = now or datetime.now(timezone.utc)
    need(current.tzinfo is not None, "CURRENT_TIME_INVALID")
    current = current.astimezone(timezone.utc)
    messages = read_room(PRICE_ROOM, 200, send=send)
    posts = _verified_referee_posts(PRICE_ROOM, messages, referee, runner=runner)
    price_posts = [item for item in posts if item[1].get("t") == "price"]
    need(price_posts, "PRICE_EVIDENCE_UNAVAILABLE")
    need(all(
        type(record.get("n")) is int and 1 <= record["n"] <= LOCK_SWEEP
        for _, record in price_posts
    ), "PRICE_EVIDENCE_INVALID")
    latest_n = max(record["n"] for _, record in price_posts)
    latest = []
    for message, record in price_posts:
        if record["n"] != latest_n:
            continue
        parsed = _price_record(record, now=current)
        need(parsed is not None, "PRICE_EVIDENCE_INVALID")
        latest.append((message, record, parsed))
    records = {compact_json(item[1], sort_keys=True) for item in latest}
    need(len(records) == 1, "PRICE_SWEEP_AMBIGUOUS")
    message, record, result = latest[-1]
    result["authenticated"] = True
    result["refereeDid"] = referee
    result["recordSha256"] = sha256_text(compact_json(record, sort_keys=True))
    result["roomMessage"] = {
        "seq": message.get("seq"),
        "ts": message.get("ts"),
        "nonce": message.get("nonce"),
    }
    if result["stale"]:
        result["warning"] = (
            "underlying reference trade is older than one expected sweep; "
            "official rules keep the last reference standing"
        )
    return result


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _first_trade_sign_request(packet: dict, price: dict) -> dict:
    terms = packet["terms"]
    canonical = packet["canonicalTerms"]
    accept = taker_preimage(terms, PROJECT_DID)
    return {
        "version": 1,
        "kind": "CLOSE_CALL_TYPED_SIGN_REQUEST",
        "action": "CLOSE_CALL_TAKER_LONG",
        "expectedDid": PROJECT_DID,
        "subject": {
            "season": CONTEST_ID,
            "did": PROJECT_DID,
            "direction": "LONG",
            "makerSide": "SELL",
        },
        "binding": {
            "contest": CONTEST_ID,
            "rulesCommit": OFFICIAL_RULES_COMMIT,
            "packageManifestSha256": PACKAGE_MANIFEST_SHA256,
            "room": TRADING_ROOM,
            "noncePolicy": "SIGNER_ALLOCATES_ROOM_NONCE",
            "canonicalTerms": canonical,
            "termsSha256": packet["termsSha256"],
            "makerDid": terms["maker"],
            "makerSig": packet["makerSig"],
            "authenticatedPrice": {
                "refereeDid": price["refereeDid"],
                "sweep": price["sweep"],
                "ref": price["ref"],
                "limits": price["limits"],
                "referenceAgeSeconds": price["referenceAgeSeconds"],
                "stale": price["stale"],
                "recordSha256": price["recordSha256"],
            },
        },
        "operation": {
            "atomic": True,
            "steps": [
                "TAKER_COUNTERSIGN",
                "CONSTRUCT_FINAL_TRADE_JSON",
                "SIGN_CLOSE1_ROOM_MESSAGE",
            ],
            "takerPreimage": {"sha256": sha256_text(accept), "utf8": accept},
            "finalTrade": {
                "t": "trade",
                "season": CONTEST_ID,
                "terms": terms,
                "taker": PROJECT_DID,
                "maker_sig": packet["makerSig"],
                "taker_sig": "SIGNER_GENERATES",
            },
            "roomEnvelope": {
                "room": TRADING_ROOM,
                "nonce": "SIGNER_ALLOCATES",
                "text": "SIGNER_CONSTRUCTS_EXACT_FINAL_TRADE_JSON",
                "preimageFormat": (
                    "close1|<SIGNER_ALLOCATED_NONCE>|<EXACT_FINAL_TRADE_JSON>"
                ),
            },
        },
    }


def build_first_trade_plan(
    *, maker_packet: dict, launch_pin: dict, send=None,
    runner=subprocess.run, now: datetime | None = None,
) -> dict:
    packet = verify_maker_packet(maker_packet, runner=runner)
    launch = verify_launch_pin(launch_pin, runner=runner)
    price = collect_current_price(
        launch=launch, send=send, runner=runner, now=now,
    )
    terms = packet["terms"]
    px = parse_decimal(terms["px"])
    qty = parse_decimal(terms["qty"], minimum=MIN_QTY)
    need(within_limits(px, price["limits"]), "TRADE_PRICE_OUTSIDE_CURRENT_LIMITS")
    need(price["sweep"] < LOCK_SWEEP, "TRADING_LOCK_REACHED")
    need(terms["until"] >= price["sweep"] + 1, "UNTIL_BEFORE_NEXT_SWEEP")

    notional = px * qty
    base_fee = notional * BASE_FEE_RATE
    warnings = [
        "actual clawback may exceed base 1% depending on sweep close",
        "mint and available funds are UNKNOWN; this plan does not claim settlement readiness",
    ]
    if price["stale"]:
        warnings.append(price["warning"])
    return {
        "version": 1,
        "status": "PLAN",
        "contest": CONTEST_ID,
        "projectDid": PROJECT_DID,
        "direction": "LONG",
        "makerSide": "SELL",
        "launch": {
            "verified": True,
            "refereeDid": launch["refereeDid"],
            "package": PACKAGE_MANIFEST_SHA256,
            "rooms": list(REFEREE_ROOMS),
            "source": launch["source"],
        },
        "priceEvidence": price,
        "readiness": {"mint": "UNKNOWN", "availableFunds": "UNKNOWN"},
        "risk": {
            "px": terms["px"],
            "qty": terms["qty"],
            "notional": _decimal_text(notional),
            "baseFeeRate": _decimal_text(BASE_FEE_RATE),
            "baseFee": _decimal_text(base_fee),
            "baseRequiredFunds": _decimal_text(notional + base_fee),
        },
        "warnings": warnings,
        "signRequest": _first_trade_sign_request(packet, price),
        "externalEffects": "NONE",
        "next": "ADD_SIGNER_CAPABILITY_THEN_HUMAN_GATE_BEFORE_ONE_REAL_POST",
    }


def _observed_mints(flow_posts: list[tuple[dict, dict]]) -> tuple[set[str], int]:
    mints: set[str] = set()
    omitted = 0
    for _, record in flow_posts:
        if record.get("t") != "flow":
            continue
        values = record.get("mints")
        if isinstance(values, list):
            for did in values:
                if isinstance(did, str) and DID_RE.fullmatch(did):
                    mints.add(did)
        info = record.get("omitted")
        if isinstance(info, dict) and type(info.get("mints")) is int and info["mints"] > 0:
            omitted += info["mints"]
    return mints, omitted


def _registration_observation(
    did: str, trading_messages: list[dict], mints: set[str], omitted_mints: int,
    *, runner=subprocess.run,
) -> dict:
    if did in mints:
        return {"status": "READY_MINTED", "evidence": ["FLOW_MINT_LIST"]}

    expected = owner_text(did)
    for message in trading_messages:
        if (
            message.get("from") == did and message.get("text") == expected
            and verify_room_message(TRADING_ROOM, message, runner=runner)
        ):
            return {
                "status": "REGISTRATION_OBSERVED",
                "evidence": ["SIGNED_OWNER_RECORD"],
                "mintEvidence": "NOT_OBSERVED",
                "flowOmittedMints": omitted_mints,
            }

    return {
        "status": "UNKNOWN",
        "evidence": [],
        "flowOmittedMints": omitted_mints,
        "limitation": "rolling room history and omitted mint lists cannot prove non-registration",
    }


def collect_registration_state(
    *, launch_pin: dict, send=None, runner=subprocess.run,
) -> dict:
    """Read only what is required for owner-registration readiness.

    This path deliberately does not read price, positions, pnl, state, community
    negotiation rooms, or any trade strategy input.
    """
    launch = verify_launch_pin(launch_pin, runner=runner)
    referee = launch["refereeDid"]

    flow_messages = read_room(FLOW_ROOM, 60, send=send)
    trading_messages = read_room(TRADING_ROOM, 200, send=send)
    flow_posts = _verified_referee_posts(
        FLOW_ROOM, flow_messages, referee, runner=runner,
    )
    mints, omitted_mints = _observed_mints(flow_posts)

    owners = {room: read_room_owner(room, send=send) for room in REFEREE_ROOMS}
    ownership_ok = all(owners.get(room) == referee for room in REFEREE_ROOMS)

    return {
        "version": 1,
        "contest": CONTEST_ID,
        "projectDid": PROJECT_DID,
        "launch": {
            "verified": True,
            "refereeDid": referee,
            "package": PACKAGE_MANIFEST_SHA256,
            "source": launch["source"],
            "rooms": list(REFEREE_ROOMS),
        },
        "refereeRoomOwnership": {"verified": ownership_ok, "owners": owners},
        "registration": _registration_observation(
            PROJECT_DID, trading_messages, mints, omitted_mints, runner=runner,
        ),
    }
