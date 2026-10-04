"""One synchronous polling worker; no recovery, execution, or signing paths."""

import http.client
import random
import sqlite3
import time
import urllib.error

from .http import backoff, retry_after
from .protocol import POLL_LIMIT, ObserverError, ProtocolAnomaly, decode_reply, deployment_config, validate_envelope, is_integer
from .storage import GapReviewRequired, RUNNABLE, noop_checkpoint

NETWORK_ERRORS = (OSError, urllib.error.URLError, http.client.HTTPException)
BASELINE_VERSION = "0.12.1"


class Observer:
    def __init__(self, store, client, checkpoint=noop_checkpoint):
        self.store = store
        self.client = client
        self.checkpoint = checkpoint
        self.retention_seconds = None

    def poll_once(self):
        """Return (continue_running, recommended_delay). Never advances on failure."""
        if self.store.state()["status"] not in RUNNABLE:
            raise ObserverError("STATE_REQUIRES_HUMAN_REVIEW")
        self.store.start_poll()
        s = self.store.state()
        try:
            reply = self.client.poll(s["poll_seq"])
        except NETWORK_ERRORS:
            self.store.failure("NETWORK_FAILURE")
            return True, backoff(s["consecutive_failures"] + 1)
        self.checkpoint("http_received")
        if reply.status != 200:
            return self._http_failure(reply)
        try:
            obj = decode_reply(reply)
            # Recognize a valid room/generation claim before validating sequence;
            # a restart may also reset seq. Never ingest that response normally.
            generation = obj.get("generation")
            if (obj.get("room") == self.store.room and is_integer(generation)
                    and generation >= 0 and generation != s["server_generation"]):
                self.store.generation_change(reply, generation)
                return False, 0
            envelope = validate_envelope(obj, self.store.room)
            if any(message["seq"] <= s["poll_seq"] for message in envelope["messages"]):
                raise ProtocolAnomaly("OLD_RECORD_IN_NORMAL_POLL")
            if len(envelope["messages"]) > POLL_LIMIT:
                raise ProtocolAnomaly("RESPONSE_LIMIT_EXCEEDED")
            try:
                self.store.save(envelope, self.checkpoint)
            except sqlite3.IntegrityError as exc:
                raise ProtocolAnomaly("UNEXPECTED_CONSTRAINT_COLLISION") from exc
        except GapReviewRequired as exc:
            self.store.failure(str(exc), reply, terminal=True)
            return False, 0
        except ProtocolAnomaly as exc:
            status = self.store.failure(str(exc), reply, protocol=True,
                                        terminal=str(exc) == "FORWARD_JUMP_REQUIRES_RESYNC")
            return status != "ERROR", backoff(s["consecutive_failures"] + 1)
        # Missing wait_held is handled conservatively for an empty response.
        delay = 0
        if envelope.get("wait_held") is False or (
            not envelope["messages"] and envelope.get("wait_held") is not True
        ):
            delay = 10 + random.uniform(0, 1)
        return True, delay

    def _http_failure(self, reply):
        s = self.store.state()
        if reply.status == 429:
            delay, clamped = retry_after(reply.retry_after)
            self.store.failure("HTTP_429", reply, delay_clamped=clamped)
            fallback = backoff(s["consecutive_failures"] + 1)
            # Retry-After is a minimum; Retry-After: 0 must not create a hot loop.
            return True, max(delay or 0, fallback)
        if 500 <= reply.status <= 599:
            self.store.failure("HTTP_5XX", reply)
            return True, backoff(s["consecutive_failures"] + 1)
        self.store.failure("HTTP_ENDPOINT_ANOMALY", reply, terminal=True)
        return False, 0

    def check_config(self):
        """Explicit deployment check. Configuration never changes the read protocol."""
        # Includes run --check-config after restart at an exhausted budget.
        self.store._capacity()
        reply = self.client.config()
        if reply.status != 200:
            self._http_failure(reply)
            raise ObserverError("CONFIG_HTTP_FAILURE")
        try:
            config = deployment_config(decode_reply(reply))
            version = config["version"]
        except ProtocolAnomaly as exc:
            self.store.failure(str(exc), reply, protocol=True)
            raise
        self.retention_seconds = config["settings"]["retention_seconds"]
        changed = version != BASELINE_VERSION
        with self.store.transaction():
            self.store.event("VERSION_CHANGED" if changed else "VERSION_CONFIRMED",
                             {"baseline": BASELINE_VERSION, "observed": version})
        return {"event": "VERSION_CHANGED" if changed else "VERSION_CONFIRMED",
                "baseline": BASELINE_VERSION, "observed": version,
                "retention_seconds": self.retention_seconds,
                "deployment_config": config}


def watchdog(store, client, stall_seconds=30):
    """Independent network sample and fresh SQLite snapshot; no DB mutations."""
    reply = client.tail()
    if reply.status != 200:
        raise ObserverError("WATCHDOG_HTTP_FAILURE")
    envelope = validate_envelope(decode_reply(reply), store.room)
    if envelope["count"] > 1:
        raise ProtocolAnomaly("WATCHDOG_LIMIT_EXCEEDED")
    s = store.heartbeat()
    latest = envelope["messages"][-1]["seq"] if envelope["messages"] else None
    age = time.time() - s["last_valid_response_at"] if s["last_valid_response_at"] else None
    result = "OK"
    if envelope["generation"] != s["server_generation"]:
        result = "GENERATION_CHANGE"
    elif s["status"] in ("ERROR", "NEEDS_RESYNC"):
        result = "OBSERVER_STOPPED"
    elif latest is None:
        result = "EMPTY_OR_REAPED"
    elif latest > s["poll_seq"]:
        result = "LAGGING" if age is None or age >= stall_seconds else "BEHIND"
    return {"room": store.room, "result": result, "latest_observed_seq": latest,
            "observed_generation": envelope["generation"],
            "server_generation": s["server_generation"], "poll_seq": s["poll_seq"],
            "last_valid_response_age_seconds": age, "status": s["status"]}
