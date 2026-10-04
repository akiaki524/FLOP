"""The only network boundary: bounded, anonymous reads of a fixed service."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import json
import re
import time


HOST = "technocore.chat"
MAX_BYTES = 4 * 1024 * 1024
MAX_MESSAGES = 200
TIMEOUT = 15
BODY_DEADLINE = 30
ROOM = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}", re.ASCII)


class ScoutError(Exception):
    """An expected failure that must not be reported as an empty success."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def room_path(room, limit):
    if not isinstance(room, str) or ROOM.fullmatch(room) is None:
        raise ScoutError("ルーム名は小文字英数字で始まる1〜48文字の英数字・_・-に限定されます。")
    if type(limit) is not int or not 1 <= limit <= MAX_MESSAGES:
        raise ScoutError("limitは1〜200の整数にしてください。")
    return f"/r/{room}?format=json&limit={limit}"


def _integer(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ScoutError("JSONに重複キーがあります。")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ScoutError("JSONに非標準の数値があります。")


def parse_room(raw, room, limit):
    """Validate the observed API contract; never interpret text as instructions."""
    room_path(room, limit)
    if len(raw) > MAX_BYTES:
        raise ScoutError("応答が4 MiBの上限を超えました。")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ScoutError("応答が有効なUTF-8 JSONではありません。") from exc
    if not isinstance(data, dict) or data.get("room") != room:
        raise ScoutError("応答のroomが要求と一致しません。")
    messages = data.get("messages")
    if (not isinstance(messages, list) or len(messages) > limit
            or not _integer(data.get("count")) or data["count"] != len(messages)
            or not _integer(data.get("last_seq"))):
        raise ScoutError("応答の件数またはsequence情報が不正です。")
    if "generation" in data and not _integer(data["generation"]):
        raise ScoutError("応答のgenerationが不正です。")
    previous = -1
    for message in messages:
        if (not isinstance(message, dict) or not _integer(message.get("seq"))
                or message["seq"] <= previous):
            raise ScoutError("メッセージのsequenceが不正・重複・順序逆転しています。")
        for field, maximum in (("ts", 64), ("from", 256), ("text", 4096)):
            value = message.get(field)
            if not isinstance(value, str) or not 1 <= len(value) <= maximum:
                raise ScoutError("メッセージの必須文字列が不正です。")
            try:
                value.encode("utf-8")
            except UnicodeError as exc:
                raise ScoutError("メッセージに不正なUnicodeがあります。") from exc
        try:
            timestamp = datetime.fromisoformat(message["ts"].replace("Z", "+00:00"))
            if timestamp.utcoffset() is None:
                raise ValueError("missing timezone")
        except ValueError as exc:
            raise ScoutError("メッセージの時刻が不正です。") from exc
        previous = message["seq"]
    if messages and data["last_seq"] < messages[-1]["seq"]:
        raise ScoutError("last_seqがメッセージと矛盾しています。")
    if "first_seq" in data:
        first = data["first_seq"]
        if ((messages and (not _integer(first) or first != messages[0]["seq"]))
                or (not messages and first is not None)):
            raise ScoutError("first_seqがメッセージと矛盾しています。")
    return data


@dataclass(frozen=True)
class Observation:
    room: str
    limit: int
    fetched_at: str
    raw: bytes
    data: dict
    mode: str = "live"

    def metadata(self):
        return {
            "room": self.room,
            "mode": self.mode,
            "source_url": f"https://{HOST}{room_path(self.room, self.limit)}" if self.mode == "live" else None,
            "fetched_at": self.fetched_at,
            "sha256": hashlib.sha256(self.raw).hexdigest(),
            "evidence_file": f"source-{self.room}.json",
            "bytes": len(self.raw),
            "requested_limit": self.limit,
            "count": self.data["count"],
            "first_seq": self.data.get("first_seq"),
            "last_seq": self.data["last_seq"],
            "generation": self.data.get("generation"),
        }


def fetch_room(room, limit=100):
    path = room_path(room, limit)  # Validate BEFORE creating a connection.
    connection = http.client.HTTPSConnection(HOST, timeout=TIMEOUT)
    response = None
    try:
        # http.client does not use .netrc, proxy environment variables, cookies,
        # or redirects. No arbitrary URLs, request bodies or caller headers.
        connection.request("GET", path, headers={
            "Accept": "application/json", "Accept-Encoding": "identity",
            "User-Agent": "collaboration-scout/0.1 (read-only)",
        })
        transport_socket = connection.sock
        response = connection.getresponse()
        if response.status != 200:
            retry = response.getheader("Retry-After", "")
            suffix = f" 再試行は少なくとも{retry}秒後。" if re.fullmatch(r"[0-9]{1,6}", retry) else ""
            raise ScoutError(f"HTTP {response.status}。自動再試行・redirectは行いません。{suffix}")
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ScoutError("応答のContent-Typeがapplication/jsonではありません。")
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise ScoutError("圧縮応答は受け付けません。")
        length = response.getheader("Content-Length")
        if length is not None and (not re.fullmatch(r"[0-9]{1,10}", length) or int(length) > MAX_BYTES):
            raise ScoutError("応答のContent-Lengthが不正または上限超過です。")
        deadline = time.monotonic() + BODY_DEADLINE
        chunks = []
        received = 0
        while received <= MAX_BYTES:
            if response.isclosed():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ScoutError("応答本文の受信が30秒の上限を超えました。")
            transport_socket.settimeout(min(TIMEOUT, remaining))
            # A read1 call performs at most one underlying buffered read.
            # No bytes from content can initiate another request.
            chunk = response.read1(min(65536, MAX_BYTES + 1 - received))
            if time.monotonic() > deadline:
                raise ScoutError("応答本文の受信が30秒の上限を超えました。")
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
        raw = b"".join(chunks)
        if length is not None and len(raw) != int(length):
            raise ScoutError("応答が途中で終了しました。")
        data = parse_room(raw, room, limit)
        return Observation(room, limit, utc_now(), raw, data)
    except (OSError, http.client.HTTPException) as exc:
        # Do not echo remote error bodies, URLs, or environment details.
        raise ScoutError("通信に失敗しました（DNS・TLS・接続・タイムアウトを確認してください）。") from exc
    finally:
        if response is not None:
            response.close()
        connection.close()
