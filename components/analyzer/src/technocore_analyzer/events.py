"""Time-limited Event Profiles. Nothing event-specific lives in the Core.

``close-call`` is configured entirely from the event profile config (rooms, times, sweep
interval, official keys). Removing or disabling the profile leaves every permanent feature
unchanged. Official recomputation of balances/prices is not implemented: it needs the
per-sweep flow files and the fold package, which stored chat Evidence does not contain,
so results are reported as ``RECALCULATION_NOT_SUPPORTED`` / partial comparison only.
"""

from __future__ import annotations

from collections import defaultdict
import json
import math

from . import findings as F
from . import signature as S
from .util import digest, ms_to_iso, parse_rfc3339_ms

HOUR_MS = 3600 * 1000
PLAYER_TYPES = {"owner", "room", "trade"}
# Minimum shapes of the official referee posts (close-call-game.md "Messages and signing",
# blob 4e3ed2e7a4efaf47b8e8fbfa43b2955fd2964145; docs/close-1-referee.md adds fields such as
# price `applied`/`for`/`age_s` and flow `unlisted`, so extra keys are allowed).
REFEREE_SHAPES = {
    "price": {"n": int, "ref": dict, "limits": list, "global": str, "file": str},
    "flow": {"n": int, "mints": list, "rooms": list, "settled": str, "void": str, "missed": list, "file": str},
    "positions": {"n": int, "open": str, "longs": str, "shorts": str, "top": list, "file": str},
    "pnl": {"n": int, "mark": str, "top": list, "file": str},
    "state": {"n": int, "root": str, "owners": str, "rooms": str, "file": str},
    "seed": {"season": str, "price": str, "trade": dict, "package": str, "rooms": list},
    "final": {"season": str, "price": str, "trade": dict},
}
SWEEP_TYPES = {"price", "flow", "positions", "pnl", "state"}


def shape_ok(obj):
    shape = REFEREE_SHAPES.get(obj.get("t"))
    if shape is None:
        return False
    for key, kind in shape.items():
        value = obj.get(key)
        if type(value) is not kind if kind is int else not isinstance(value, kind):
            return False
    return obj.get("n", 1) >= 1 if "n" in shape else True


CONFLICT_RULE = {"id": "EVENT-CLOSECALL-CONFLICT-001", "version": "1",
                 "condition": "two different referee posts for the same room and sweep, both from an official key",
                 "scope": "configured referee rooms", "required_evidence": "both posts",
                 "benign": ["official correction/re-post", "Observer duplicate with re-serialization"]}


def _reject(name):
    raise ValueError("NON_JSON_CONSTANT")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("NON_FINITE_NUMBER")
    return value


def _json_obj(text):
    if not isinstance(text, str):
        return None
    try:
        # NaN/Infinity (and 1e999) are not JSON values a referee writes; such a post is not an
        # object here, rather than failing the whole profile when its content is digested.
        value = json.loads(text, parse_constant=_reject, parse_float=_finite_float)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def close_call(profile, messages, as_of):
    rooms_trading = set(profile.get("trading_rooms", []))
    rooms_referee = list(profile.get("referee_rooms", []))
    official = set(profile.get("official_dids", []))
    t_first = parse_rfc3339_ms(profile.get("first_sweep"))
    t_lock = parse_rfc3339_ms(profile.get("trading_lock"))
    t_final = parse_rfc3339_ms(profile.get("final_price_time"))
    sweep_ms = int(profile.get("sweep_seconds", 0)) * 1000
    basis = profile.get("observation_end_basis", "final_price_time")
    t_basis = parse_rfc3339_ms(profile.get(basis))
    post_hours = profile.get("post_end_hours", 24)
    missing_config = [k for k, v in (("first_sweep", t_first), ("trading_lock", t_lock),
                                     ("final_price_time", t_final), ("sweep_seconds", sweep_ms or None),
                                     (basis, t_basis),
                                     ("referee_room_posts", profile.get("referee_room_posts") or None))
                      if v is None]
    timeline = {
        "opening": profile.get("opening"), "first_sweep": profile.get("first_sweep"),
        "trading_lock": profile.get("trading_lock"), "final_price_time": profile.get("final_price_time"),
        "observation_end": ms_to_iso(t_basis + post_hours * HOUR_MS) if t_basis is not None else None,
        "observation_end_origin": f"{basis} + {post_hours}h (collection period, not a deletion deadline)",
        "claim_window_days": profile.get("claim_window_days"),
        "config_source": profile.get("config_source"),
        "note": "trading lock, final price, result finalisation and claim window are distinct times",
    }
    if t_lock is not None and as_of < t_lock:
        phase = "TRADING_OPEN"
    elif t_final is not None and as_of < t_final:
        phase = "TRADING_LOCKED_AWAITING_FINAL_PRICE"
    elif t_basis is not None and as_of < t_basis + post_hours * HOUR_MS:
        phase = "POST_EVENT_OBSERVATION"
    else:
        phase = "ENDED (history, late evidence, corrections and report tracking remain available)"
    # Room -> accepted post types comes from the profile (official room table), never the Core.
    room_posts = {room: set(types) for room, types in profile.get("referee_room_posts", {}).items()}
    rooms_referee = list(room_posts) or rooms_referee
    posts = defaultdict(lambda: defaultdict(list))
    attribution = defaultdict(int)
    rejected = defaultdict(int)
    player_posts = []
    for rec in messages:
        obj = _json_obj(rec["message"].get("text"))
        if obj is None:
            continue
        kind = obj.get("t")
        if not isinstance(kind, str):
            continue  # {"t":[]} / {"t":{}} is JSON but not a post type; never reaches membership tests
        if rec["room"] in rooms_referee and kind in REFEREE_SHAPES:
            if kind not in room_posts.get(rec["room"], ()):
                rejected["WRONG_ROOM_FOR_POST_TYPE"] += 1
                continue
            if not shape_ok(obj):
                rejected["MALFORMED_MINIMUM_SHAPE"] += 1
                continue
            did = S.attributable_did(rec["sig_status"], rec["message"])
            if did and did in official:
                attr = "OFFICIAL_KEY_MATCH"
            elif did and not official:
                attr = "SIGNED_OFFICIAL_KEY_UNCONFIRMED"
            elif did:
                attr = "SIGNED_NOT_OFFICIAL_KEY"
            else:
                attr = "NOT_ATTRIBUTABLE"
            attribution[attr] += 1
            if kind in SWEEP_TYPES:
                posts[rec["room"]][obj["n"]].append((rec, obj, attr))
        elif rec["room"] in rooms_trading and kind in PLAYER_TYPES:
            player_posts.append(rec)
    # Only posts from a confirmed official key count as observed referee evidence.
    usable = {"OFFICIAL_KEY_MATCH"}
    sweeps = {"monitored": False}
    findings = []
    if not missing_config and not official:
        sweeps = {"monitored": False, "status": "INCONCLUSIVE_OFFICIAL_KEY_UNCONFIRMED",
                  "candidate_posts_by_room": {room: len(posts[room]) for room in rooms_referee},
                  "note": "signed posts from an unconfirmed key are not counted as referee sweeps"}
    elif not missing_config:
        window_end = min(as_of, t_lock)
        expected_last = (window_end - t_first) // sweep_ms + 1 if window_end >= t_first else 0
        per_room = {}
        for room in rooms_referee:
            if not room_posts.get(room, set()) & SWEEP_TYPES:
                continue
            have = {n for n, items in posts[room].items() if any(a in usable for _, _, a in items)}
            missing = [n for n in range(1, expected_last + 1) if n not in have]
            per_room[room] = {"expected_through_sweep": expected_last, "observed_sweeps": len(have),
                              "missing_sweeps_count": len(missing), "missing_sweeps_sample": missing[:20]}
            for n, items in posts[room].items():
                official_items = [(r, o) for r, o, a in items if a == "OFFICIAL_KEY_MATCH"]
                contents = {digest(o) for _, o in official_items}
                if len(contents) > 1:
                    findings.append(F.make(
                        category="EVIDENCE_CONFLICT", rule=CONFLICT_RULE, subject=f"closecall:{profile['id']}:{room}:{n}",
                        claim=f"different official referee posts for {room} sweep {n}", expected="one post per room and sweep",
                        observed=f"{len(contents)} distinct contents", evidence_refs=[r["ref"] for r, _ in official_items],
                        severity="UNKNOWN", detection_method="EVENT_PROFILE", coverage={"room": room, "sweep": n},
                        alternatives=CONFLICT_RULE["benign"],
                        evidence_facets=F.facets(direct="posts observed", signature="VALID official key",
                                                 coverage="configured referee rooms"),
                        details={"profile": profile["id"]}))
        cross = []
        for n in range(max(1, expected_last - 11), expected_last + 1):
            present = [room for room in per_room
                       if any(a in usable for _, _, a in posts[room].get(n, []))]
            if present and len(present) != len(per_room):
                cross.append({"sweep": n, "rooms_with_post": present})
        sweeps = {"monitored": True,
                  "monitoring_window": [profile.get("first_sweep"), profile.get("trading_lock")],
                  "after_lock": "no missing-sweep warnings are produced after the scheduled period",
                  "per_room": per_room, "recent_cross_room_mismatch": cross,
                  "note": "missing = not observed from a usable key; may be Observer gap, not referee absence"}
    closed = t_lock is not None and as_of >= t_lock
    return {
        "profile_id": profile["id"], "kind": "close-call", "enabled": True,
        "status": "INCOMPLETE_CONFIG" if missing_config else "ACTIVE",
        "profile_version": profile.get("profile_version", "1"),
        "phase": phase, "timeline": timeline, "missing_config": missing_config,
        "official_keys": {"configured": sorted(official),
                          "status": "CONFIGURED" if official else "OFFICIAL_KEY_UNCONFIRMED",
                          "coverage": None if official else
                          "referee attribution INCONCLUSIVE until a Human sets the key from the official launch record"},
        "referee_attribution": dict(attribution),
        "referee_posts_not_counted": dict(rejected),
        "sweep_coverage": sweeps,
        "recalculation": {"status": "RECALCULATION_NOT_SUPPORTED",
                          "reason": "needs per-sweep flow files, initial state and fold package; chat Evidence carries hashes only",
                          "consequence": "no official-equivalent mismatch is ever asserted; partial comparison only"},
        "trading_opportunities": {
            "status": "CLOSED_AT_TRADING_LOCK" if closed else "OPEN_UNTIL_TRADING_LOCK",
            "player_posts_observed": len(player_posts),
            "note": "continued observation after the lock never extends the trading period"},
        "findings": findings,
        "closed_rooms_for_opportunities": sorted(rooms_trading) if closed else [],
    }


PROFILES = {"close-call": close_call}


def run_profiles(profiles, messages, as_of):
    out = []
    for profile in profiles:
        if not profile.get("enabled", True):
            out.append({"profile_id": profile.get("id"), "kind": profile.get("kind"), "enabled": False,
                        "status": "DISABLED (Core unaffected)"})
            continue
        handler = PROFILES.get(profile.get("kind"))
        if handler is None:
            out.append({"profile_id": profile.get("id"), "kind": profile.get("kind"), "enabled": True,
                        "status": "UNSUPPORTED_PROFILE_KIND"})
            continue
        try:
            out.append(handler(profile, messages, as_of))
        except (KeyError, TypeError, ValueError) as exc:
            # One broken profile never stops the Core or other profiles.
            out.append({"profile_id": profile.get("id"), "kind": profile.get("kind"), "enabled": True,
                        "status": "FAILED", "error": type(exc).__name__})
    return out
