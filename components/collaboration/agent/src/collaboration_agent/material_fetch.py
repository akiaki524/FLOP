"""Small HTTPS read transport with a closed path policy and bounded acquisition.

Linux/main-thread only. Direct sockets: no proxies, cookies, auth or redirects.
The resolved public IP is used for connect; TLS still verifies the original host.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import http.client
import ipaddress
from pathlib import Path
import re
import signal
import socket
import ssl
from urllib.parse import parse_qsl, urlencode, urlsplit

from .model import Invalid, check, encode, sha

POLICY = "public-material-v1"
MAX_RESPONSE = 131072
MAX_RUN_BYTES = 2097152
MAX_RUN_FETCHES = 32
MAX_TASK_FETCHES = 4
TIMEOUT = 15
# Only the explicitly opted-in, fixed offers export gets a longer total read
# window. Ordinary GETs and the socket idle/connect timeout remain 15 seconds.
EXPORT_TIMEOUT = 30
NAME = r"[a-z0-9][a-z0-9_-]{0,47}"


def public_name(name):
    return bool(re.fullmatch(NAME, name)) and not (name.startswith("p-") or "-p-" in name)


def allowed_url(reference):
    check(type(reference) is str and 0 < len(reference) <= 1000, "MALFORMED_REFERENCE")
    check(not any(ord(c) <= 32 or ord(c) >= 127 for c in reference), "MALFORMED_REFERENCE")
    check(not any(c in reference for c in ("%", "\\", "#")), "MALFORMED_REFERENCE")
    url = "https://technocore.chat" + reference if reference.startswith(("/kv/", "/r/")) else reference
    try:
        parts = urlsplit(url)
        check(parts.scheme == "https" and parts.username is None and parts.password is None
              and parts.port is None and not parts.fragment, "SOURCE_NOT_ALLOWED")
    except ValueError as exc:
        raise Invalid("MALFORMED_REFERENCE") from exc
    host, path = parts.netloc, parts.path
    check(".." not in path.split("/") and "//" not in path, "MALFORMED_REFERENCE")
    if host == "technocore.chat":
        match = re.fullmatch(r"/kv/(" + NAME + r")/(" + NAME + r")", path)
        if match and not parts.query:
            check(all(public_name(n) for n in match.groups()), "PRIVATE_SOURCE")
            return url, "technocore_note"
        match = re.fullmatch(r"/r/(" + NAME + r")", path)
        if match:
            check(public_name(match[1]), "PRIVATE_SOURCE")
            try:
                pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
            except ValueError as exc:
                raise Invalid("MALFORMED_REFERENCE") from exc
            params = dict(pairs)
            check(len(pairs) == len(params) and set(params) <= {"format", "limit", "since"}, "SOURCE_NOT_ALLOWED")
            check(params.get("format") == "json" and re.fullmatch(r"[0-9]{1,3}", params.get("limit", ""))
                  and 1 <= int(params["limit"]) <= 200, "MALFORMED_REFERENCE")
            check("since" not in params or re.fullmatch(r"0|[1-9][0-9]{0,15}", params["since"]), "MALFORMED_REFERENCE")
            return "https://technocore.chat" + path + "?" + urlencode(sorted(params.items())), "technocore_room"
        if not parts.query and path in {"/llms.txt", "/openapi.json", "/.well-known/agent.json"}:
            return url, "official_document"
    if host in {"github.com", "raw.githubusercontent.com"} and not parts.query:
        prefix = r"/flop-labs/(technocore-chat|tclk)/" + (r"blob/" if host == "github.com" else "")
        match = re.fullmatch(prefix + r"(main|[a-f0-9]{40})/(README\.md|SPEC\.md)", path)
        if match:
            repo, revision, filename = match.groups()
            return f"https://raw.githubusercontent.com/flop-labs/{repo}/{revision}/{filename}", "official_document"
    raise Invalid("SOURCE_NOT_ALLOWED")


@contextmanager
def deadline(seconds):
    check(signal.getitimer(signal.ITIMER_REAL)[0] == 0, "DEADLINE_UNAVAILABLE")
    previous = signal.getsignal(signal.SIGALRM)
    def expired(*_):
        raise TimeoutError("TIMEOUT")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def public_addresses(host):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    check(bool(addresses), "DNS_FAILURE")
    for _, _, _, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        check(ip.is_global and not ip.is_multicast and not ip.is_reserved, "PRIVATE_ADDRESS")
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=TIMEOUT, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        family, kind, proto, _, address = self.address
        raw = socket.socket(family, kind, proto)
        try:
            raw.settimeout(self.timeout)
            raw.connect(address)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def transport(url, cap, *, intake_export=False):
    """Return partial bytes on bounded failure so the acquisition remains auditable."""
    if intake_export:
        # Fixed public board only, opt-in by the bounded intake driver. The
        # Material resolver's allowed_url policy deliberately excludes exports.
        check(url == "https://technocore.chat/r/tclk-offers/export"
              and type(cap) is int and 0 < cap <= 10 * 1024 * 1024, "SOURCE_NOT_ALLOWED")
    else:
        url, _ = allowed_url(url)
    parts = urlsplit(url)
    body, headers, status, error = bytearray(), {}, None, None
    connection = None
    try:
        with deadline(EXPORT_TIMEOUT if intake_export else TIMEOUT):
            addresses = public_addresses(parts.hostname)
            connection = PinnedHTTPS(parts.hostname, addresses[0])
            target = parts.path + ("?" + parts.query if parts.query else "")
            connection.request("GET", target, headers={"Accept-Encoding": "identity",
                "Accept": "text/plain, application/json", "User-Agent": "Collaboration-Agent-Material/0.1"})
            response = connection.getresponse()
            status = response.status
            headers = {k.lower(): v for k, v in response.getheaders()
                       if k.lower() in {"content-type", "content-length", "content-encoding", "location",
                                        "date", "etag", "last-modified", "x-room-generation"}}
            # Only the fixed export consumer needs framing evidence. Expose the
            # HTTP parser's recognized chunking, not an untrusted raw header.
            if intake_export and getattr(response, "chunked", False) is True:
                headers["transfer-encoding"] = "chunked"
            if headers.get("content-encoding", "identity") != "identity":
                raise Invalid("ENCODING_NOT_ALLOWED")
            while len(body) <= cap:
                chunk = response.read1(min(8192, cap + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            if len(body) > cap:
                error = "TOO_LARGE"
            elif "content-length" in headers and int(headers["content-length"]) != len(body):
                error = "TRUNCATED_RESPONSE"
    except (TimeoutError, socket.timeout):
        error = "TIMEOUT"
    except socket.gaierror:
        # No HTTP response exists: do not confuse resolver/environment failure
        # with a missing Note. Never persist exception text or network secrets.
        error = "DNS_FAILURE"
    except Invalid as exc:
        error = str(exc)
    except (OSError, http.client.HTTPException, ValueError):
        error = "FETCH_FAILURE"
    finally:
        if connection:
            connection.close()
    return status, headers, bytes(body), error


class Fetcher:
    """One evaluation/run scope; exact URL reuse only, no durable fact cache."""
    def __init__(self, root, *, send=transport, max_fetches=MAX_RUN_FETCHES):
        self.root = Path(root)
        check(self.root.absolute() == self.root.resolve(), "symlink_path")
        self.root.mkdir(parents=True, exist_ok=False)
        self.send = send
        self.max_fetches = min(max_fetches, MAX_RUN_FETCHES)
        self.count = self.total_bytes = self.cache_hits = 0
        self.cache = {}

    def fetch(self, reference, task_budget):
        url, kind = allowed_url(reference)
        if url in self.cache:
            self.cache_hits += 1
            return dict(self.cache[url], cache_hit=True)
        check(task_budget[0] < MAX_TASK_FETCHES and self.count < self.max_fetches, "FETCH_BUDGET_EXCEEDED")
        check(self.total_bytes < MAX_RUN_BYTES, "FETCH_BUDGET_EXCEEDED")
        task_budget[0] += 1
        self.count += 1
        started = datetime.now(timezone.utc).isoformat()
        # Persist reservation before network; a run cannot be resumed/retried in this root.
        (self.root / f"attempt-{self.count}.json").write_bytes(encode({"url": url, "at": started, "method": "GET"}))
        cap = min(MAX_RESPONSE, MAX_RUN_BYTES - self.total_bytes - 1)
        status, headers, raw, error = self.send(url, cap)
        self.total_bytes += len(raw)
        if status in {301, 302, 303, 307, 308}:
            try:
                allowed_url(headers.get("location", ""))
                error = "REDIRECT_NOT_ALLOWED"
            except Invalid as exc:
                error = str(exc)
        elif status in {401, 403}:
            error = "AUTH_OR_ACCESS_REQUIRED"
        elif status == 404:
            error = "MISSING"
        elif status != 200 and not error:
            error = "HTTP_FAILURE"
        blob = sha(raw) + ".bin"
        path = self.root / blob
        if not path.exists():
            path.write_bytes(raw)
        else:
            check(path.read_bytes() == raw, "BLOB_CONFLICT")
        result = {"url": url, "resolver_type": kind, "fetch_policy": POLICY,
                  "fetched_at": started, "http_status": status, "headers": headers,
                  "raw_sha256": sha(raw), "stored_sha256": sha(raw), "raw_bytes": len(raw),
                  "blob": blob, "fetch_error": error, "cache_hit": False}
        self.cache[url] = result
        (self.root / f"response-{self.count}.json").write_bytes(encode(result))
        return result

    def raw(self, material):
        raw = (self.root / material["blob"]).read_bytes()
        check(sha(raw) == material["raw_sha256"], "FROZEN_DIGEST_MISMATCH")
        return raw
