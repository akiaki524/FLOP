"""Offline DNS/TCP probe in an unprivileged user + mount + network namespace.

No host fallback, routes, credentials, Claude invocation, TLS or provider requests.
Only private-namespace loopback is brought up; resolv.conf is bind-mounted there.
"""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading

from support import REPO
from ccw import claude, claude_real
from ccw.claude_real_launcher import confine
from ccw.model import canonical


def client(area, port, runtime_env=False):
    env = claude_real.environment(area, area / "claude-home") if runtime_env else {}
    null = os.open("/dev/null", os.O_RDONLY)
    if null != 3:
        os.dup2(null, 3)
        os.close(null)
    confine(area, area / "claude-home", os.getppid(), online=True)
    os.close(3)
    os.environ.update(env)
    result = {}
    try:
        result["dns"] = sorted({v[4][0] for v in socket.getaddrinfo(
            "vendor.ccw.test", port, socket.AF_INET, socket.SOCK_STREAM)})
    except OSError as exc:
        result["dns_error"] = exc.errno
    for name, address, target_port in (("resolved_vendor", result.get("dns", [None])[0], port),
            ("direct_loopback_bypass", "127.0.0.2", port), ("lan_no_route", "192.0.2.1", port)):
        if address is None:
            result[name] = "not_resolved"
            continue
        phase = "socket"
        try:
            with socket.socket() as stream:
                # Python settimeout uses FIONBIO, outside the native policy.
                # Keep blocking I/O; the parent bounds this entire child to 8s.
                phase = "connect"
                stream.connect((address, target_port))
                phase = "send"
                stream.sendall(b"CCW_FAKE_ONLY")
                phase = "recv"
                result[name] = stream.recv(32).decode("ascii")
        except OSError as exc:
            result[name] = {"errno": exc.errno, "phase": phase}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.close()
        result["udp"] = "unexpected_allowed"
    except OSError as exc:
        result["udp"] = {"errno": exc.errno}
    with socket.socket() as handle:
        for name, operation in (("callback_bind", lambda: handle.bind(("127.0.0.1", 0))),
                                ("callback_listen", lambda: handle.listen(1)),
                                ("callback_accept", handle.accept)):
            try:
                operation()
                result[name] = "unexpected_allowed"
            except OSError as exc:
                result[name] = {"errno": exc.errno}
    print(json.dumps(result))


def namespace(area, host_net):
    assert os.readlink("/proc/self/ns/net") != host_net
    assert socket.if_nameindex() == [(1, "lo")]
    libc = ctypes.CDLL(None, use_errno=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as handle:
        fcntl.ioctl(handle.fileno(), 0x8914, struct.pack("16sH22s", b"lo", 0x49, b""))
    resolver = area / "resolv.conf"
    resolver.write_text("nameserver 127.0.0.1\noptions timeout:1 attempts:1\n")
    assert libc.mount(os.fsencode(resolver), b"/etc/resolv.conf", None, 4096, None) == 0
    counts = {"tcp_dns": 0, "udp_dns": 0, "endpoint": 0}
    def answer(query):
        end = 12
        while query[end]:
            end += query[end] + 1
        end += 5
        question = query[12:end]
        assert question[:-4] == b"\x06vendor\x03ccw\x04test\x00"
        return (query[:2] + b"\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00" + question
                + b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x01\x00\x04\x7f\x00\x00\x02")
    def exact(stream, count):
        result = b""
        while len(result) < count:
            part = stream.recv(count-len(result))
            if not part:
                raise ValueError("short fake DNS query")
            result += part
        return result
    def tcp_dns(listener):
        while True:
            stream, _ = listener.accept()
            with stream:
                query = exact(stream, int.from_bytes(exact(stream, 2), "big"))
                response = answer(query)
                counts["tcp_dns"] += 1
                stream.sendall(len(response).to_bytes(2, "big") + response)
    def udp_dns(listener):
        while True:
            query, address = listener.recvfrom(4096)
            counts["udp_dns"] += 1
            listener.sendto(answer(query), address)
    def endpoint(listener):
        while True:
            stream, _ = listener.accept()
            with stream:
                assert stream.recv(32) == b"CCW_FAKE_ONLY"
                counts["endpoint"] += 1
                stream.sendall(b"FAKE_VENDOR_OK")
    listeners = []
    for address, port, kind, serve in (("127.0.0.1", 53, socket.SOCK_STREAM, tcp_dns),
            ("127.0.0.1", 53, socket.SOCK_DGRAM, udp_dns),
            ("127.0.0.2", 0, socket.SOCK_STREAM, endpoint)):
        listener = socket.socket(socket.AF_INET, kind)
        listener.bind((address, port))
        if kind == socket.SOCK_STREAM:
            listener.listen(8)
        listeners.append(listener)
        threading.Thread(target=serve, args=(listener,), daemon=True).start()
    port = listeners[-1].getsockname()[1]
    results = {}
    for mode in ("ordinary", "use-vc", "runtime-env"):
        resolver.write_text("nameserver 127.0.0.1\noptions timeout:1 attempts:1" +
                            (" use-vc" if mode == "use-vc" else "") + "\n")
        workspace = area / mode
        workspace.mkdir(mode=0o700)
        claude.prepare(workspace)
        process = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()),
            "--client", str(workspace), str(port), mode], env={}, input=b"", capture_output=True, timeout=8)
        assert process.returncode == 0, process.stderr.decode()
        results[mode] = json.loads(process.stdout)
    (area / "observations.json").write_bytes(canonical({"results": results, "counts": counts}))
    assert "dns_error" in results["ordinary"]
    assert results["use-vc"]["dns"] == ["127.0.0.2"]
    assert results["use-vc"]["resolved_vendor"] == "FAKE_VENDOR_OK"
    assert results["runtime-env"]["dns"] == ["127.0.0.2"]
    assert results["runtime-env"]["resolved_vendor"] == "FAKE_VENDOR_OK"
    assert counts["udp_dns"] == 0 and counts["tcp_dns"] > 0
    for value in results.values():
        assert value["direct_loopback_bypass"] == "FAKE_VENDOR_OK"
        assert value["udp"] == {"errno": 1}
        assert value["lan_no_route"]["errno"] == 101
        for name in ("callback_bind", "callback_listen", "callback_accept"):
            assert value[name] == {"errno": 1}
    print(json.dumps({"status": "PASS", "results": results, "counts": counts,
        "network_namespace_isolated": True, "host_resolver_modified": False,
        "external_traffic_possible": False, "bun_resolver": "UNTESTED",
        "egress_a": "NOT_IMPLEMENTED", "tcp_only_is_vendor_restricted": False}))


def main():
    area = Path(tempfile.mkdtemp(prefix="b3-network-", dir=REPO / ".local"))
    process = subprocess.run(["unshare", "--user", "--map-root-user", "--net", "--mount",
        "--propagation", "private", sys.executable, "-B", str(Path(__file__).resolve()),
        "--namespace", str(area), os.readlink("/proc/self/ns/net")],
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=25)
    (area / "stdout.json").write_bytes(process.stdout)
    (area / "stderr.txt").write_bytes(process.stderr)
    print(area)
    if process.returncode:
        print(process.stderr.decode(), file=sys.stderr)
        raise SystemExit(process.returncode)
    print(process.stdout.decode())


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--namespace":
        namespace(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) == 5 and sys.argv[1] == "--client":
        client(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4] == "runtime-env")
    else:
        main()
