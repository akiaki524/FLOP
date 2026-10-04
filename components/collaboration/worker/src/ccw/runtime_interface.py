"""Credential-free, one-request interface. A trusted supervisor supplies the socket.

No service launcher, credential resolver, filesystem path, model, tool, URL
fetcher or environment options are part of this requester API.
"""
from dataclasses import dataclass
import socket
import struct

from .model import canonical, decode, keys, require, sha, string

MAX_REQUEST_BYTES = 65_536
MAX_RESULT_BYTES = 32_768
MAX_TIMEOUT_MS = 5_000
REAL_RUNTIME_TIMEOUT_MS = 60_000  # trusted supervisor profile; not requester controlled
OPERATION = "review_saved_document"


class RuntimeRefused(RuntimeError):
    def __init__(self):
        super().__init__("runtime request refused; no fallback or retry")


@dataclass(frozen=True)
class ReviewRequest:
    locator: str
    text: str
    timeout_ms: int = 2_000

    def encode(self) -> bytes:
        try:
            raw = canonical({"version": 1, "operation": OPERATION,
                             "task": {"locator": self.locator, "text": self.text},
                             "timeout_ms": self.timeout_ms})
            parse_request(raw)
            return raw
        except Exception:
            raise RuntimeRefused() from None


@dataclass(frozen=True)
class ReviewResult:
    task_sha256: str
    report: dict
    offline: bool = True
    runtime_kind: str = "offline-fake"

    def preview_text(self) -> str:
        """Plain ASCII JSON only: no HTML/Markdown/terminal capability renderer."""
        return canonical({"untrusted_output": True, "display_only": True,
                          "runtime_kind": self.runtime_kind,
                          "external_actions_executed": False,
                          "task_sha256": self.task_sha256, "report": self.report}).decode("ascii")


def parse_request(raw: bytes) -> dict:
    require(type(raw) is bytes and len(raw) <= MAX_REQUEST_BYTES, "request budget")
    data = decode(raw)
    keys(data, "version operation task timeout_ms")
    require(type(data["version"]) is int and data["version"] == 1, "version")
    require(data["operation"] == OPERATION, "operation")
    require(type(data["timeout_ms"]) is int and 50 <= data["timeout_ms"] <= MAX_TIMEOUT_MS,
            "timeout budget")
    keys(data["task"], "locator text")
    string(data["task"]["locator"], 1_000)
    string(data["task"]["text"], 30_000)
    # Service admission must cover the bytes re-encoded for the runtime too.
    # In particular, UTF-8 wire input can expand under ensure_ascii=True.
    require(len(canonical(data)) <= MAX_REQUEST_BYTES, "canonical request budget")
    return data


def task_digest(request: dict) -> str:
    return sha(canonical(request["task"]))


def receive_frame(endpoint: socket.socket, limit: int) -> bytes:
    def exact(count):
        result = bytearray()
        while len(result) < count:
            chunk = endpoint.recv(count - len(result))
            if not chunk:
                raise RuntimeRefused()
            result.extend(chunk)
        return bytes(result)
    size = struct.unpack("!I", exact(4))[0]
    if not 0 < size <= limit:
        raise RuntimeRefused()
    return exact(size)


def send_frame(endpoint: socket.socket, raw: bytes, limit: int):
    if not 0 < len(raw) <= limit:
        raise RuntimeRefused()
    endpoint.sendall(struct.pack("!I", len(raw)) + raw)


class RuntimeClient:
    """A single use connection, with no startup/configuration/credential API.

    The supervisor, not an Activity prompt, chooses the peer and its policy.
    Service-side enforcement remains authoritative if this client is bypassed.
    """
    def __init__(self, endpoint: socket.socket, *, runtime_kind="offline-fake"):
        require(runtime_kind in ("offline-fake", "offline-native", "real", "offline-messages-api", "real-messages-api"), "runtime profile")
        self._endpoint = endpoint
        self._used = False
        self._runtime_kind = runtime_kind

    def review(self, request: ReviewRequest) -> ReviewResult:
        try:
            if self._used or type(request) is not ReviewRequest:
                raise RuntimeRefused()
            self._used = True
            raw = request.encode()
            timeout = MAX_TIMEOUT_MS if self._runtime_kind == "offline-fake" else REAL_RUNTIME_TIMEOUT_MS
            self._endpoint.settimeout(timeout / 1000 + 3)
            send_frame(self._endpoint, raw, MAX_REQUEST_BYTES)
            data = decode(receive_frame(self._endpoint, MAX_RESULT_BYTES))
            require(len(canonical(data)) <= MAX_RESULT_BYTES, "canonical result budget")
            keys(data, "version status offline task_sha256 report")
            require(type(data["version"]) is int and data["version"] == 1
                    and data["status"] == "succeeded" and data["offline"] is (self._runtime_kind not in ("real", "real-messages-api"))
                    and data["task_sha256"] == task_digest(parse_request(raw)), "result")
            # Reuse the existing credential-free review/quote validator.
            from .model import review
            # Validate actual string contents, not repr() or ASCII JSON escapes.
            def unicode_scalars(value):
                if type(value) is str:
                    value.encode("utf-8", errors="strict")
                elif type(value) is dict:
                    for key, item in value.items():
                        unicode_scalars(key)
                        unicode_scalars(item)
                elif type(value) is list:
                    for item in value:
                        unicode_scalars(item)
            unicode_scalars(data["report"])
            review(data["report"], frozen_task(parse_request(raw)))
            provider = ("anthropic-messages-real-v1" if self._runtime_kind == "real-messages-api" else
                        "anthropic-messages-offline-v1" if self._runtime_kind == "offline-messages-api" else
                        "claude-cli-offline-v1" if self._runtime_kind == "offline-fake" else "claude-cli-real-v1")
            require(data["report"]["provider"] == provider, "provider")
            return ReviewResult(data["task_sha256"], data["report"], data["offline"], self._runtime_kind)
        except Exception:
            raise RuntimeRefused() from None
        finally:
            self._endpoint.close()


def frozen_task(request: dict) -> dict:
    """Fixed review purpose, one saved source; locator is never dereferenced."""
    task = request["task"]
    return {"sources": [{"id": "s1", "sha256": sha(task["text"].encode("utf-8")),
                         "text": task["text"], "locator": task["locator"]}]}
