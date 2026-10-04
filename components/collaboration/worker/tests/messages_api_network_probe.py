"""Offline Real Messages network-path probe.

This uses a private user/network/mount namespace, a local TCP DNS server and a
local TLS server.  It never has a route to an external provider.  Test-only
patches trust a generated test CA; the production release predicate,
permit, consumption, credential handoff, launcher confinement, SDK transport,
request guard and Output Boundary code remain in the subprocess path.
"""
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time

REPO = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
sys.path.insert(0, str(REPO / "src"))

from ccw.model import canonical, decode, digest, require, sha
from ccw.runtime_interface import (MAX_RESULT_BYTES, ReviewRequest, RuntimeClient, parse_request,
                                   receive_frame, send_frame)
from ccw.secret_handoff import exchange, save_new

sys.path.insert(0, str(REPO / "tests"))
from runtime_service_smoke import MATERIAL

REQUEST = MATERIAL
KEY = "ccw-dummy-" + "6" * 48


def run(command):
    result = subprocess.run(command, env={}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    require(result.returncode == 0, "offline fixture command")


def certificates(area):
    certs = area / "certs"
    certs.mkdir(mode=0o700)
    ca_key, ca_cert = certs / "ca.key", certs / "ca.crt"
    run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30",
         "-subj", "/CN=CCW Offline Test CA", "-keyout", str(ca_key), "-out", str(ca_cert)])
    for name, hostname in (("server", "api.anthropic.com"), ("wrong", "wrong.invalid")):
        key, csr, cert = certs / f"{name}.key", certs / f"{name}.csr", certs / f"{name}.crt"
        run(["/usr/bin/openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={hostname}",
             "-addext", f"subjectAltName=DNS:{hostname}", "-keyout", str(key), "-out", str(csr)])
        run(["/usr/bin/openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_cert),
             "-CAkey", str(ca_key), "-CAcreateserial", "-days", "30", "-copy_extensions", "copyall",
             "-out", str(cert)])
    return certs


def dns_name(raw, offset):
    labels = []
    while raw[offset]:
        length = raw[offset]
        offset += 1
        labels.append(raw[offset:offset + length].decode("ascii"))
        offset += length
    return ".".join(labels), offset + 1


class DNSHandler(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            length = self.request.recv(2)
            if len(length) != 2:
                return
            wanted = int.from_bytes(length, "big")
            raw = b""
            while len(raw) < wanted:
                part = self.request.recv(wanted - len(raw))
                if not part:
                    return
                raw += part
            name, end = dns_name(raw, 12)
            qtype = int.from_bytes(raw[end:end + 2], "big")
            self.server.queries.append({"name": name, "type": qtype})
            question = raw[12:end + 4]
            answer = b""
            if name == "api.anthropic.com" and qtype == 1:
                answer = b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x00\x00\x04\x7f\x00\x00\x01"
            header = raw[:2] + b"\x81\x80\x00\x01" + (b"\x00\x01" if answer else b"\x00\x00") + b"\x00\x00\x00\x00"
            reply = header + question + answer
            self.request.sendall(len(reply).to_bytes(2, "big") + reply)


class DNSServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class TLSServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    def handle_error(self, request, client_address):
        pass


def tls_server(cert, key, calls, handshakes, response_case="success"):
    report = {"provider": "anthropic-messages-real-v1", "summary": "Offline TLS fixture.",
              "findings": [], "unverified": ["No provider inference was performed."]}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *args):
            pass
        def parse_request(self):
            valid = super().parse_request()
            if valid:
                self.record = {"method": self.command, "path": self.path.split("?", 1)[0]}
                calls.append(self.record)
            return valid
        def __getattr__(self, name):
            if name.startswith("do_"):
                return self.auxiliary
            raise AttributeError(name)
        def auxiliary(self):
            self.send_error(405)
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = decode(raw)
            self.record.update(model=body.get("model"), max_tokens=body.get("max_tokens"),
                wire_body_sha256=sha(raw), payload_sha256=digest(body),
                stream=body.get("stream"), dummy_auth_received=self.headers.get("x-api-key") == KEY,
                credential_in_body=KEY.encode() in raw, tls_version=self.connection.version(),
                cipher=self.connection.cipher()[0])
            if response_case == "redirect":
                self.send_response(307)
                self.send_header("Location", "https://api.anthropic.com/v1/messages")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if response_case == "secret":
                report["summary"] = KEY
            response = canonical({"id": "msg_offline_tls", "type": "message", "role": "assistant",
                "model": "claude-sonnet-4-6", "content": [{"type": "text", "text": canonical(report).decode()}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 20}})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = TLSServer(("127.0.0.1", 443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    context.set_servername_callback(lambda sock, name, ctx: handshakes.append({"sni": name}))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def prepare_real(root, host_net, *, attested=True):
    from ccw import runtime_api as api
    request = parse_request(REQUEST.encode())
    root.mkdir(mode=0o700)
    (root / "runtime").mkdir(mode=0o700)
    spec = api.contract(root, request, 443, host_net, secrets.token_hex(16), profile=api.REAL_PROFILE, real=True)
    policy = {"version": 3, "task_sha256": api.task_digest(request), "api": spec}
    save_new(root / "policy.json", policy)
    # Synthetic artifact ONLY inside loopback-only namespace; never authorize().
    api.namespace_check(host_net)
    permit = {"version": 2, "contract_sha256": digest(policy),
              "expires_at": int(time.time()) + 300, "human_confirmed": True,
              "execution_net_namespace": os.readlink("/proc/self/ns/net"),
              "human_attestation": api.pilot.required_attestation(digest(policy))}
    if not attested:
        permit["human_attestation"]["console_workspace_scope_expiry_and_usd1_limit_checked"] = False
    save_new(root / "permit.json", permit)
    return policy


def service_process(root, cafile, patched):
    ours, theirs = socket.socketpair()
    key_out, key_in = socket.socketpair()
    command = [sys.executable, "-I", "-S", "-B", str(SELF), "--service", str(root),
               str(key_in.fileno()), str(cafile) if cafile else "-", "1" if patched else "0"]
    poison = {"HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9",
              "ANTHROPIC_BASE_URL": "https://wrong.invalid", "ANTHROPIC_API_KEY": "environment-poison"}
    child = subprocess.Popen(command, stdin=theirs, stdout=theirs, stderr=subprocess.PIPE,
                             env=poison, pass_fds=(key_in.fileno(),))
    theirs.close()
    key_in.close()
    send_frame(key_out, KEY.encode(), 4096)
    key_out.close()
    return ours, child


def request_once(root, cafile, patched, *, client=False):
    peer, child = service_process(root, cafile, patched)
    if client:
        result = RuntimeClient(peer, runtime_kind="real-messages-api").review(REQUEST)
        preview = decode(result.preview_text())
        require(preview["untrusted_output"] and not preview["external_actions_executed"], "untrusted preview")
        response = {"status": "succeeded", "preview_untrusted": True}
    else:
        send_frame(peer, REQUEST.encode(), 65536)
        response = decode(receive_frame(peer, MAX_RESULT_BYTES))
        peer.close()
    child.wait(timeout=70)
    require(child.stderr.read() == b"", "service diagnostics")
    return response, child.returncode


def service_bootstrap(root, fd, cafile, patched):
    from unittest.mock import patch
    from ccw import runtime_api as api, runtime_service
    original_exchange = api.exchange
    policy = api.read(root / "policy.json")
    api.namespace_check(policy["api"]["host_net"])

    def launcher_exchange(command, workspace, **kwargs):
        replacement = [command[0], "-I", "-S", "-B", str(SELF), "--launcher",
                       command[-2], command[-1], cafile]
        return original_exchange(replacement, workspace, **kwargs)

    sys.argv = [str(Path(runtime_service.__file__)), str(root), "--api-real-credential-fd", str(fd)]
    if not patched:
        return runtime_service.main()
    with patch.object(api, "exchange", side_effect=launcher_exchange):
        return runtime_service.main()


def launcher_bootstrap(root, parent, cafile):
    from unittest.mock import patch
    from ccw import runtime_api as api, runtime_api_launcher as launcher
    original_context = ssl.create_default_context
    policy = api.read(root / "policy.json")
    api.namespace_check(policy["api"]["host_net"])

    def test_context(*args, **kwargs):
        if cafile != "-":
            kwargs["cafile"] = cafile
        return original_context(*args, **kwargs)

    sys.argv = [str(Path(launcher.__file__)), str(root), str(parent)]
    with patch.object(ssl, "create_default_context", side_effect=test_context):
        launcher.main()
    return 0


def namespace(area, host_net):
    require(os.readlink("/proc/self/ns/net") != host_net, "private network namespace")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as handle:
        fcntl.ioctl(handle.fileno(), 0x8914, struct.pack("16sH22s", b"lo", 0x49, b""))
    require(socket.if_nameindex() == [(1, "lo")], "loopback only")
    resolv = area / "resolv.conf"
    # The production launcher, rather than this fixture, must force TCP DNS.
    resolv.write_text("nameserver 127.0.0.1\n")
    run(["/usr/bin/mount", "--bind", str(resolv), "/etc/resolv.conf"])
    certs = certificates(area)
    dns = DNSServer(("127.0.0.1", 53), DNSHandler)
    dns.queries = []
    dns_thread = threading.Thread(target=dns.serve_forever, daemon=True)
    dns_thread.start()
    summary = {"status": "RUNNING", "real_provider_contacted": False, "real_credential": False,
               "fixed_endpoint": "https://api.anthropic.com/v1/messages", "cases": {}}

    closed_root = area / "human-condition-unconfirmed"
    prepare_real(closed_root, host_net, attested=False)
    before_dns = len(dns.queries)
    response, code = request_once(closed_root, certs / "ca.crt", False)
    require(response["status"] == "refused" and code == 2 and not (closed_root / "consumed.json").exists()
            and len(dns.queries) == before_dns, "unconfirmed Human condition closed before network")
    summary["cases"]["human_condition_unconfirmed"] = {"accepted": False, "consumed": False, "runtime_starts": 0,
                                              "provider_posts": 0, "dns_queries": 0}

    scenarios = (("untrusted_ca", "server", None, "success", False),
                 ("hostname_mismatch", "wrong", certs / "ca.crt", "success", False),
                 ("redirect_blocked", "server", certs / "ca.crt", "redirect", False),
                 ("secret_withheld", "server", certs / "ca.crt", "secret", False),
                 ("verified_tls", "server", certs / "ca.crt", "success", True))
    for name, identity, cafile, response_case, succeeds in scenarios:
        calls, handshakes = [], []
        server, thread = tls_server(certs / f"{identity}.crt", certs / f"{identity}.key", calls, handshakes,
                                    response_case=response_case)
        root = area / name
        prepare_real(root, host_net)
        query_start = len(dns.queries)
        response, code = request_once(root, cafile, True, client=succeeds)
        evidence = decode((root / "evidence.json").read_bytes())
        accepted = response["status"] == "succeeded"
        posts = sum(call["method"] == "POST" for call in calls)
        auxiliary = len(calls) - posts
        require(accepted == succeeds and (code == 0) == succeeds, "TLS outcome")
        require((root / "consumed.json").exists() and evidence["runtime_starts"] == 1
                and evidence["usage_source"] == "unconfirmed; provider accounting not retrieved", "Real evidence")
        require(evidence["real_spending_authorized"] is True
                and evidence["provider_usage"] is None and evidence["provider_charge"] is None,
                "Human authorization is not provider accounting")
        expected_posts = 0 if name in ("untrusted_ca", "hostname_mismatch") else 1
        require(posts == expected_posts and auxiliary == 0, "POST/auxiliary bound")
        queries = dns.queries[query_start:]
        require(any(q == {"name": "api.anthropic.com", "type": 1} for q in queries), "fixed DNS name")
        if expected_posts:
            from ccw import messages_pilot as pilot
            call = calls[0]
            require(call["path"] == "/v1/messages" and call["model"] == "claude-sonnet-4-6"
                    and call["max_tokens"] == 4096 and call["stream"] is False
                    and call["dummy_auth_received"] and not call["credential_in_body"]
                    and call["tls_version"] in ("TLSv1.2", "TLSv1.3"), "fixed request and TLS")
            require(call["wire_body_sha256"] == pilot.WIRE_BODY_SHA256
                    and call["payload_sha256"] == pilot.PAYLOAD_SHA256, "Decision Packet payload identity")
        before = (root / "evidence.json").read_bytes()
        again, again_code = request_once(root, cafile, True)
        require(again["status"] == "refused" and again_code == 2 and len(calls) == expected_posts
                and (root / "evidence.json").read_bytes() == before, "consumed prevents retry")
        require(KEY.encode() not in canonical(response) and KEY.encode() not in before, "credential withheld")
        summary["cases"][name] = {"accepted": accepted, "consumed": True,
            "runtime_starts": evidence["runtime_starts"], "provider_posts": posts,
            "auxiliary_provider_requests": auxiliary, "dns_queries": queries, "tls_handshakes": handshakes,
            "calls": calls}
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    dns.shutdown()
    dns.server_close()
    dns_thread.join(timeout=2)
    summary["totals"] = {"runtime_starts": sum(v["runtime_starts"] for v in summary["cases"].values()),
                         "provider_posts": sum(v["provider_posts"] for v in summary["cases"].values())}
    summary["status"] = "PASS_OFFLINE_REAL_NETWORK_PATH"
    (area / "summary.json").write_bytes(canonical(summary))
    print(json.dumps({"status": summary["status"], "totals": summary["totals"], "area": str(area)}))


def main():
    mode = sys.argv[1:2]
    if mode == ["--namespace"]:
        namespace(Path(sys.argv[2]), sys.argv[3])
        return 0
    if mode == ["--service"]:
        return service_bootstrap(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5] == "1")
    if mode == ["--launcher"]:
        return launcher_bootstrap(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
    (REPO / ".local").mkdir(exist_ok=True)
    area = Path(tempfile.mkdtemp(prefix="messages-api-network-", dir=REPO / ".local")).resolve()
    print(area, flush=True)
    return subprocess.run(["/usr/bin/unshare", "--user", "--map-root-user", "--net", "--mount",
        "--propagation", "private", sys.executable, "-B", str(SELF), "--namespace", str(area),
        os.readlink("/proc/self/ns/net")], env={"PATH": "/usr/bin:/bin"}, timeout=300).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        trace = exc.__traceback__
        while trace.tb_next:
            trace = trace.tb_next
        print(json.dumps({"probe_failure_type": type(exc).__name__, "function": trace.tb_frame.f_code.co_name,
                          "line": trace.tb_lineno}), file=sys.stderr)
        raise SystemExit(2)
