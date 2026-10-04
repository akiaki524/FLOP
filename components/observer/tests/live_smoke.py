"""Container-only, six-request Technocore v0.3 smoke diagnostic.

This module is deliberately outside the production CLI. Never run it directly
under the host UID. It gates networking on the same nonroot isolation checks as
the public canary probe, plus Docker configuration inspection by the host helper.
"""

import argparse
import contextlib
import errno
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import sys
import zipfile

from technocore_observer.cli import emit
from technocore_observer.http import SafeClient, ORIGIN
from technocore_observer.observer import BASELINE_VERSION, Observer
from technocore_observer.protocol import MAX_BODY, ObserverError, Reply, decode_reply, deployment_config, validate_envelope
from technocore_observer.storage import StateLock, Store, initialize

ROOM = "mb-047f3d88ef38"
EMPTY_ROOM = "observer-smoke-20260906-9bc137a4"
# Absolute path of the public repository canary on the host, supplied by the
# runner as container env. Empty when unset; the check below then fails closed.
CANARY = os.environ.get("OBSERVER_HOST_CANARY", "")
ARTIFACTS = Path("/state/artifacts")
CACHE_HEADERS = ("Cache-Control", "Age", "Date", "ETag", "Expires", "Vary",
                 "CF-Cache-Status", "X-Cache", "Content-Type", "Content-Length")


def process_checks(offline):
    checks = {"nonroot_uid_gid": os.getuid() == 65532 and os.getgid() == 65532}
    with open("/proc/self/status", encoding="ascii") as handle:
        status = dict(line.rstrip().split(":", 1) for line in handle if ":" in line)
    checks["no_capabilities"] = all(int(status[name].strip(), 16) == 0 for name in
                                   ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"))
    checks["no_new_privileges"] = status["NoNewPrivs"].strip() == "1"
    checks["seccomp_filter"] = status["Seccomp"].strip() == "2"
    if offline:
        checks["loopback_only"] = set(name for _, name in socket.if_nameindex()) <= {"lo"}
    # The host supplies its real absolute HOME path; a missing or relative value
    # fails the check closed rather than passing it unconditionally.
    host_home = os.environ.get("OBSERVER_HOST_HOME", "")
    checks["host_home_absent"] = (
        host_home.startswith("/") and host_home != "/" and not os.path.lexists(host_home))
    checks["windows_mount_absent"] = not os.path.lexists("/mnt/c")
    checks["control_socket_absent"] = not any(os.path.lexists(path) for path in
        ("/run/docker.sock", "/var/run/docker.sock", "/run/desktop/docker.sock"))
    if not CANARY.startswith("/"):
        checks["public_canary_unreadable"] = False
    else:
        try:
            with open(CANARY, "rb"):
                pass
        except OSError as exc:
            checks["public_canary_unreadable"] = exc.errno in (errno.ENOENT, errno.EACCES)
        else:
            checks["public_canary_unreadable"] = False
    try:
        fd = os.open("/observer-root-write-probe", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        checks["root_write_denied"] = exc.errno in (errno.EROFS, errno.EACCES)
    else:
        os.close(fd)
        checks["root_write_denied"] = False
    info = os.stat("/state")
    checks["private_state_directory"] = info.st_uid == 65532 and stat.S_IMODE(info.st_mode) == 0o700
    return checks


def private_write(path, data):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path, obj):
    private_write(path, json.dumps(obj, ensure_ascii=True, allow_nan=False, indent=2).encode())


def export_artifacts():
    """Private binary transport to the helper; never a terminal/log display."""
    if os.getuid() != 65532:
        raise RuntimeError("ISOLATED_CONTAINER_REQUIRED")
    names = ["report.json", "smoke-inbox.sqlite"] + [f"response-{i}.body" for i in range(1, 7)]
    with zipfile.ZipFile(sys.stdout.buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            path = ARTIFACTS / name
            if path.is_file() and not path.is_symlink():
                archive.write(path, arcname=name)


class Budget:
    def __init__(self):
        self.requests = []
        self.last_finished = None

    def begin(self, path, params):
        if len(self.requests) >= 6:
            raise RuntimeError("SMOKE_REQUEST_BUDGET_EXCEEDED")
        if self.last_finished is not None:
            time.sleep(max(0, 2 - (time.monotonic() - self.last_finished)))
        item = {"number": len(self.requests) + 1, "method": "GET", "path": path,
                "query": params, "started_at": time.time()}
        self.requests.append(item)
        emit({"stage": "request", "number": item["number"], "path": path})
        return item


class DiagnosticClient(SafeClient):
    """Additional limit=201 diagnostic, using SafeClient's secured opener.

    Production SafeClient's allowlist and limit remain unchanged.
    """
    def __init__(self, room, budget):
        if room not in (ROOM, EMPTY_ROOM):
            raise RuntimeError("SMOKE_ROOM_NOT_ALLOWED")
        super().__init__(room)
        self.budget = budget

    def history(self, limit):
        if limit not in (200, 201) or self.room != ROOM:
            raise RuntimeError("SMOKE_LIMIT_NOT_ALLOWED")
        return self._get("/r/" + self.room, {"since": 0, "limit": limit,
                         "format": "json", "n": time.time_ns()})

    def _get(self, path, params):
        if path == "/config":
            if params or self.room != ROOM:
                raise RuntimeError("SMOKE_QUERY_NOT_ALLOWED")
        elif path == "/r/" + self.room:
            keys = set(params)
            if keys not in ({"format", "limit", "n"},
                            {"since", "format", "limit", "n"},
                            {"since", "format", "limit", "wait", "n"}):
                raise RuntimeError("SMOKE_QUERY_NOT_ALLOWED")
            if params["format"] != "json" or type(params["n"]) is not int or params["n"] < 0:
                raise RuntimeError("SMOKE_QUERY_NOT_ALLOWED")
            if "since" not in params:
                if params["limit"] != 1:
                    raise RuntimeError("SMOKE_QUERY_NOT_ALLOWED")
            else:
                if type(params["since"]) is not int or params["since"] < 0:
                    raise RuntimeError("SMOKE_QUERY_NOT_ALLOWED")
                if "wait" in params:
                    if params["limit"] != 200 or params["wait"] != 10 or self.room != ROOM:
                        raise RuntimeError("SMOKE_QUERY_NOT_ALLOWED")
                elif params["since"] != 0 or params["limit"] not in (200, 201) or self.room != ROOM:
                    raise RuntimeError("SMOKE_QUERY_NOT_ALLOWED")
        else:
            raise RuntimeError("SMOKE_PATH_NOT_ALLOWED")
        item = self.budget.begin(path, params)
        url = ORIGIN + path + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(url, method="GET", headers={
            "Accept": "application/json", "Cache-Control": "no-cache",
            "User-Agent": "technocore-observer-smoke/0.1"})
        started = time.monotonic()
        try:
            try:
                response = self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                body = response.read(MAX_BODY + 1)
                result = Reply(response.code, response.headers.get("Content-Type", ""),
                               body[:MAX_BODY], response.headers.get("Retry-After"),
                               len(body) > MAX_BODY, time.time())
                item.update({"http_status": result.status, "content_type": result.content_type,
                             "headers": {name: response.headers[name] for name in CACHE_HEADERS
                                         if name in response.headers},
                             "body_sha256": hashlib.sha256(result.body).hexdigest(),
                             "body_bytes": len(result.body), "truncated": result.truncated})
                private_write(ARTIFACTS / f"response-{item['number']}.body", result.body)
            if result.status != 200:
                raise RuntimeError("SMOKE_HTTP_STOP")
            if result.truncated:
                raise RuntimeError("SMOKE_BODY_TOO_LARGE")
            return result
        finally:
            item["elapsed_seconds"] = round(time.monotonic() - started, 3)
            self.budget.last_finished = time.monotonic()


def summarize(obj):
    return {"room": obj["room"], "count": obj["count"], "first_seq": obj["first_seq"],
            "last_seq": obj["last_seq"], "generation": obj["generation"],
            "wait_held": obj.get("wait_held")}


def fake_reply(seqs):
    obj = {"room": ROOM, "count": len(seqs), "first_seq": seqs[0], "last_seq": seqs[-1],
           "generation": 7, "messages": [{"seq": seq, "text": "public offline fixture"} for seq in seqs]}
    return obj, Reply(200, "application/json", json.dumps(obj).encode(), observed_at=time.time())


def offline_check(state_dir=Path("/state")):
    class Fake:
        def poll(self, since):
            if since != 100:
                raise RuntimeError("OFFLINE_CURSOR_MISMATCH")
            return fake_reply([200, 201])[1]
    directory = state_dir / "offline-inbox"
    with StateLock(directory, create=True):
        anchor, raw = fake_reply([100])
        initialize(directory, ROOM, anchor, raw)
        with contextlib.closing(Store(directory, ROOM)) as store:
            continuing, _ = Observer(store, Fake()).poll_once()
            heartbeat = store.heartbeat()
            passed = continuing and (heartbeat["poll_seq"], heartbeat["resolved_seq"],
                                      heartbeat["open_gap_count"], heartbeat["status"]) == (201, 100, 1, "DEGRADED")
    if not passed:
        raise RuntimeError("OFFLINE_OBSERVER_FAILED")
    return {"observer_transaction_and_gap": "PASS", "heartbeat": heartbeat}


def live_check(report, budget):
    client = DiagnosticClient(ROOM, budget)
    config = deployment_config(decode_reply(client.config()))         # GET 1
    report["config"] = {"version": config["version"], **config["settings"]}
    report["config_setting_sources"] = config["setting_sources"]
    report["config_unavailable_settings"] = config["unavailable_settings"]
    report["config_validation_flags"] = config["validation_flags"]
    report["expected_version"] = BASELINE_VERSION
    report["version_matches_baseline"] = config.get("version") == BASELINE_VERSION
    anchor_reply = client.tail()                                      # GET 2
    anchor = validate_envelope(decode_reply(anchor_reply), ROOM)
    if anchor["count"] != 1:
        raise RuntimeError("SMOKE_ANCHOR_NOT_ONE_MESSAGE")
    report["anchor"] = summarize(anchor)
    for limit in (200, 201):                                          # GET 3, 4
        obj = validate_envelope(decode_reply(client.history(limit)), ROOM)
        report[f"limit_{limit}"] = summarize(obj)
        if obj["generation"] != anchor["generation"]:
            raise RuntimeError("SMOKE_GENERATION_CHANGE")
        if obj["count"] > 200:
            raise RuntimeError("SMOKE_LIMIT_NOT_CLAMPED")
    report["limit_201_observation"] = (
        "returned_200_consistent_with_clamp" if report["limit_201"]["count"] == 200
        else "inconclusive_fewer_than_200_records")
    directory = Path("/state/live-inbox")
    with StateLock(directory, create=True):
        initialize(directory, ROOM, anchor, anchor_reply)
        with contextlib.closing(Store(directory, ROOM)) as store:
            observer = Observer(store, client)
            continuing, _ = observer.poll_once()                     # GET 5, wait=10
            report["heartbeat"] = store.heartbeat()
            if not continuing or report["heartbeat"]["consecutive_failures"]:
                raise RuntimeError("SMOKE_POLL_FAILED")
            # Use the already-saved response, never another request.
            raw = (ARTIFACTS / "response-5.body").read_bytes()
            polled = validate_envelope(decode_reply(Reply(200, "application/json", raw)), ROOM)
            report["long_poll"] = summarize(polled)
            report["empty_long_poll"] = "observed" if polled["count"] == 0 else "inconclusive_new_messages_arrived"
            # Offline backup API preserves the SQLite state, including evidence.
            import sqlite3
            fd = os.open(ARTIFACTS / "smoke-inbox.sqlite", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            with contextlib.closing(sqlite3.connect(ARTIFACTS / "smoke-inbox.sqlite")) as backup:
                store.conn.backup(backup)
    empty_client = DiagnosticClient(EMPTY_ROOM, budget)
    obj = validate_envelope(decode_reply(empty_client.tail()), EMPTY_ROOM)  # GET 6
    report["presumed_nonexistent_room"] = summarize(obj)
    report["empty_envelope_field_types"] = {name: type(value).__name__ for name, value in obj.items()}
    report["nonexistent_room_observation"] = "empty_observed" if obj["count"] == 0 else "inconclusive_room_not_empty"
    report["limits"] = [
        "No write or additional requests to force quietness, waiter exhaustion, or 429.",
        "A returned count of 200 alone does not independently prove more than 200 retained records exist.",
        "A randomly named empty room is not proof of historical nonexistence.",
        "Bridge networking has no OS-level domain allowlist; the diagnostic enforces the fixed origin and paths."]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("offline", "live"))
    args = parser.parse_args()
    # Refuse all IO/network work outside the dedicated container UID first.
    if os.getuid() != 65532 or os.getgid() != 65532:
        emit({"status": "ISOLATED_CONTAINER_REQUIRED"}, error=True)
        return 1
    checks = process_checks(args.mode == "offline")
    if not all(checks.values()):
        emit({"status": "ISOLATION_FAILED", "checks": checks}, error=True)
        return 1
    ARTIFACTS.mkdir(mode=0o700)
    budget = Budget()
    report = {"mode": args.mode, "process_checks": checks, "requests": budget.requests,
              "status": "INCOMPLETE", "started_at": time.time()}
    try:
        if args.mode == "offline":
            report.update(offline_check())
        else:
            live_check(report, budget)
        report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "STOPPED"
        report["error_class"] = type(exc).__name__
        # Raw errors can contain untrusted data; only fixed internal codes here.
        if type(exc) is RuntimeError and str(exc).startswith(("SMOKE_", "OFFLINE_")):
            report["error_code"] = str(exc)
        elif isinstance(exc, ObserverError):
            report["error_code"] = str(exc)
    finally:
        report["finished_at"] = time.time()
        write_json(ARTIFACTS / "report.json", report)
    emit({"stage": "diagnostic_finished", "mode": args.mode,
          "status": report["status"], "requests": len(budget.requests)})
    return 0 if report["status"] == "PASS" else 1
