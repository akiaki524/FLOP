"""Fixed-origin transport in a disposable, deadline-supervised subprocess.

The parent reads a bounded framed byte stream with nonblocking I/O; poll()+recv
on a multiprocessing pipe would not bound a partial large frame. No untrusted
pickle is received. Spawn avoids inheriting SQLite handles and producer locks.
"""

import base64
import ctypes
import json
import math
import multiprocessing
import os
import selectors
import signal
import struct
import time

from technocore_observer.http import SafeClient
from technocore_observer.protocol import MAX_BODY, ObserverError, Reply, validate_room
from .spool import canonical, require
from .__main__ import MAX_EXPORT

FRAME_LIMIT = MAX_BODY * 2


class RequestFailure(ObserverError):
    pass


def send_reply(channel, reply):
    require(isinstance(reply, Reply) and len(reply.body) <= MAX_BODY, "CAPTURE_TRANSPORT_BODY_LIMIT")
    value = {"status": reply.status, "content_type": reply.content_type[:512],
             "retry_after": reply.retry_after[:512] if reply.retry_after is not None else None,
             "truncated": reply.truncated, "observed_at": reply.observed_at,
             "body": base64.b64encode(reply.body).decode("ascii")}
    raw = canonical(value).encode("ascii")
    channel.sendall(struct.pack("!I", len(raw)) + raw)


def fetch(room, since, channel):
    # No endpoint override, redirects or proxy discovery.
    if isinstance(since, tuple):
        since, poll_wait = since
    else:
        poll_wait = 10
    send_reply(channel, SafeClient(room, poll_wait=poll_wait).poll(since))


def fetch_export(room, args, channel):
    # Reuse the fixed-origin downloader, supervised by the same hard deadline.
    from .__main__ import ExportClient, RetryCapture
    path, generation, max_bytes = args
    client = ExportClient(room)
    try:
        observed = client._download(
            path, {"generation": generation}, max_bytes=max_bytes, exclusive=True)
        value = {"generation": observed}
    except RetryCapture as exc:
        value = {"failure": exc.code, "delay": exc.delay}
    except ObserverError as exc:
        value = {"failure": {"GENERATION_CHANGE": "GENERATION_CHANGE",
                             "EXPORT_TOO_LARGE": "EXPORT_TOO_LARGE"}.get(
                                 str(exc), "EXPORT_PROTOCOL_FAILURE")}
    except OSError:
        # The destination is local spool storage. Do not mislabel its failures
        # as remote transport failures or serialize path/exception details.
        value = {"failure": "LOCAL_IO_FAILURE"}
    sizes = getattr(client, "last_size_metrics", {})
    for key in ("export_limit_bytes", "export_content_length_bytes", "export_received_bytes"):
        number = sizes.get(key)
        if key in sizes and (number is None or type(number) is int and 0 <= number <= 2**63 - 1):
            value[key] = number
    send_reply(channel, Reply(200, "application/json", canonical(value).encode()))


class PipeWriter:
    """Byte-only IPC over an OS pipe; no socket or untrusted deserialization."""
    def __init__(self, connection):
        self.connection = connection

    def sendall(self, data):
        remaining = memoryview(data)
        while remaining:
            written = os.write(self.connection.fileno(), remaining)
            remaining = remaining[written:]


def _child(target, room, since, channel, expires, parent_pid):
    # Linux is the repository's supported runtime. Do not leave a network
    # request alive after a SIGKILL of its capture parent.
    libc = ctypes.CDLL(None, use_errno=True)
    writer = PipeWriter(channel)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
        raw = b'{"error":"PARENT_DEATH_GUARD_FAILED"}'
        writer.sendall(struct.pack("!I", len(raw)) + raw)
        os._exit(2)
    remaining = expires - time.monotonic()
    if remaining <= 0:
        os._exit(2)
    signal.signal(signal.SIGALRM, lambda *_: os._exit(2))
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        target(room, since, writer)
    except Exception:
        # Never serialize remote exception messages into diagnostics.
        raw = b'{"error":"NETWORK_FAILURE"}'
        try:
            writer.sendall(struct.pack("!I", len(raw)) + raw)
        except OSError:
            pass
    finally:
        channel.close()


class DeadlineClient:
    def __init__(self, room, deadline=30.0, *, target=fetch, context=None, stop=None, poll_wait=10):
        self.room = validate_room(room)
        require(math.isfinite(deadline) and 0 < deadline <= 120, "CAPTURE_INVALID_DEADLINE")
        self.deadline, self.target, self.stop = deadline, target, stop
        require(type(poll_wait) is int and 0 <= poll_wait <= 10, "INVALID_POLL_WAIT")
        self.poll_wait = poll_wait
        self.context = context or multiprocessing.get_context("spawn")
        self.last_pid = None

    def poll(self, since):
        require(type(since) is int and 0 <= since <= 2**63 - 1, "INVALID_POLL_CURSOR")
        return self._request((since, self.poll_wait) if self.target is fetch else since, self.target)

    def export(self, path, generation, *, max_bytes=MAX_EXPORT):
        require(type(max_bytes) is int and 1 <= max_bytes <= MAX_EXPORT, "EXPORT_INVALID_LIMIT")
        return self._request((str(path), generation, max_bytes), fetch_export)

    def _request(self, since, target):
        expires = time.monotonic() + self.deadline
        parent, child = self.context.Pipe(duplex=False)
        process = self.context.Process(target=_child, args=(target, self.room, since, child, expires, os.getpid()))
        started = False
        try:
            process.start()
            started = True
            self.last_pid = process.pid
            child.close()
            os.set_blocking(parent.fileno(), False)
            buffer, expected = bytearray(), None
            with selectors.DefaultSelector() as selector:
                selector.register(parent.fileno(), selectors.EVENT_READ)
                while expected is None or len(buffer) < expected + 4:
                    remaining = expires - time.monotonic()
                    if remaining <= 0:
                        raise RequestFailure("TOTAL_REQUEST_DEADLINE")
                    if self.stop is not None and self.stop.is_set():
                        raise RequestFailure("CAPTURE_STOP_REQUESTED")
                    if not selector.select(min(remaining, 0.05)):
                        continue
                    data = os.read(parent.fileno(), 65536)
                    if not data:
                        code = "TOTAL_REQUEST_DEADLINE" if time.monotonic() >= expires - 0.01 else "NETWORK_FAILURE"
                        raise RequestFailure(code)
                    buffer.extend(data)
                    if expected is None and len(buffer) >= 4:
                        expected = struct.unpack("!I", buffer[:4])[0]
                        if expected > FRAME_LIMIT:
                            raise RequestFailure("TRANSPORT_FRAME_LIMIT")
                    if len(buffer) > FRAME_LIMIT + 4:
                        raise RequestFailure("TRANSPORT_FRAME_LIMIT")
            if time.monotonic() >= expires:
                raise RequestFailure("TOTAL_REQUEST_DEADLINE")
            value = json.loads(buffer[4:4 + expected])
            if "error" in value:
                raise RequestFailure("PARENT_DEATH_GUARD_FAILED" if value["error"] == "PARENT_DEATH_GUARD_FAILED" else "NETWORK_FAILURE")
            body = base64.b64decode(value.pop("body"), validate=True)
            require(len(body) <= MAX_BODY, "CAPTURE_TRANSPORT_BODY_LIMIT")
            reply = Reply(body=body, **value)
            if time.monotonic() >= expires:
                raise RequestFailure("TOTAL_REQUEST_DEADLINE")
            return reply
        finally:
            parent.close()
            child.close()
            if started:
                # Reaping overhead is separately bounded; HTTP is stopped first.
                if process.is_alive():
                    process.kill()
                process.join(0.5)
                if process.is_alive():
                    raise RequestFailure("TRANSPORT_REAP_FAILED")
                process.close()
