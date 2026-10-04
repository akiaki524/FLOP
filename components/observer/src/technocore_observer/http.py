"""Fixed-origin, GET-only, two-path transport. Never follows response URLs."""

import email.utils
import math
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .protocol import MAX_BODY, POLL_LIMIT, ObserverError, Reply, validate_room

ORIGIN = "https://technocore.chat"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SafeClient:
    def __init__(self, room, timeout=25.0, *, poll_wait=10):
        self.room = validate_room(room)
        if not math.isfinite(timeout) or timeout <= 10:
            raise ObserverError("INVALID_HTTP_TIMEOUT")
        if type(poll_wait) is not int or not 0 <= poll_wait <= 10:
            raise ObserverError("INVALID_POLL_WAIT")
        self.timeout = timeout
        self.poll_wait = poll_wait
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    def tail(self):
        return self._get("/r/" + self.room,
                         {"format": "json", "limit": 1, "n": time.time_ns()})

    def poll(self, since):
        if type(since) is not int or since < 0:
            raise ObserverError("INVALID_POLL_CURSOR")
        return self._get("/r/" + self.room, {
            "since": since, "limit": POLL_LIMIT, "wait": self.poll_wait,
            "format": "json", "n": time.time_ns(),
        })

    def config(self):
        return self._get("/config", {})

    def _get(self, path, params):
        if path not in ("/config", "/r/" + self.room):
            raise ObserverError("ENDPOINT_NOT_ALLOWED")
        if path == "/config":
            if params:
                raise ObserverError("QUERY_NOT_ALLOWED")
        else:
            tail_keys = {"format", "limit", "n"}
            poll_keys = tail_keys | {"since", "wait"}
            if set(params) not in (tail_keys, poll_keys):
                raise ObserverError("QUERY_NOT_ALLOWED")
            if params["format"] != "json" or type(params["n"]) is not int or params["n"] < 0:
                raise ObserverError("QUERY_NOT_ALLOWED")
            if set(params) == tail_keys:
                if type(params["limit"]) is not int or params["limit"] != 1:
                    raise ObserverError("QUERY_NOT_ALLOWED")
            elif (type(params["limit"]) is not int or params["limit"] != POLL_LIMIT
                  or type(params["wait"]) is not int or params["wait"] != self.poll_wait
                  or type(params["since"]) is not int or params["since"] < 0):
                raise ObserverError("QUERY_NOT_ALLOWED")
        url = ORIGIN + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, method="GET", headers={
            "Accept": "application/json", "Cache-Control": "no-cache",
            "User-Agent": "technocore-observer/0.1",
        })
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            # Read at most limit+1, including for error replies; never log bodies.
            body = response.read(MAX_BODY + 1)
            return Reply(
                status=response.code,
                content_type=response.headers.get("Content-Type", ""),
                body=body[:MAX_BODY],
                retry_after=response.headers.get("Retry-After"),
                truncated=len(body) > MAX_BODY,
                observed_at=time.time(),
            )


def backoff(failures, maximum=60.0):
    base = min(maximum, 2 ** min(max(failures - 1, 0), 16))
    return min(maximum, base + random.uniform(0, base * 0.2))


def retry_after(value, now=None):
    """Return (delay, clamped), or (None, False) for invalid headers."""
    if value is None:
        return None, False
    now = time.time() if now is None else now
    try:
        value = value.strip()
        if value.isascii() and value.isdecimal():
            # Avoid unbounded integer conversion on hostile header values.
            seconds = 601.0 if len(value.lstrip("0")) > 6 else float(value)
        else:
            dt = email.utils.parsedate_to_datetime(value)
            if dt.tzinfo is None:
                return None, False
            seconds = max(0.0, dt.timestamp() - now)
        if not math.isfinite(seconds):
            return None, False
    except (ValueError, TypeError, OverflowError):
        return None, False
    return min(seconds, 600.0), seconds > 600.0
