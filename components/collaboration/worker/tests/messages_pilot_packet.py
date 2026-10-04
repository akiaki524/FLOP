"""Reconstruct the fixed candidate packet locally; never issues a permit."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_service_smoke import MATERIAL, REPO
from ccw import runtime_api as api
from ccw.model import canonical, digest, sha, require
from ccw.runtime_interface import parse_request
from ccw.secret_handoff import save_new


def sdk_body():
    """Pinned SDK serialization through production guards and a no-network double."""
    import ssl
    import time
    from unittest.mock import patch
    from ccw.runtime_api_launcher import verify_dependencies
    from ccw.runtime_output import OutputRejected
    verify_dependencies()
    sys.path.insert(0, str(api.VENV / "lib/python3.12/site-packages"))
    import httpx2
    captured = []

    class CaptureTransport(httpx2.BaseTransport):
        def __init__(self, **kwargs):
            pass

        def handle_request(self, outgoing):
            captured.append(outgoing.content)
            return httpx2.Response(400, json={"error": {"type": "invalid_request_error", "message": "synthetic"}})

    request = parse_request(MATERIAL.encode())
    spec = api.contract(REPO / ".local/serialization-only", request, 443,
                        os.readlink("/proc/self/ns/net"), "0" * 32, profile=api.REAL_PROFILE, real=True)
    with patch.object(httpx2, "HTTPTransport", CaptureTransport):
        try:
            api.sdk_request(request, "ccw-dummy-" + "0" * 48, spec, ssl.create_default_context(),
                            permit_expires_at=int(time.time()) + 300)
        except OutputRejected as exc:
            require(exc.reason == "provider_http_error", "synthetic status")
    require(len(captured) == 1 and sha(captured[0]) == api.pilot.WIRE_BODY_SHA256, "SDK wire identity")
    sys.stdout.buffer.write(captured[0])


def main():
    request = parse_request(MATERIAL.encode())
    payload = api.message_arguments(request, api.profile_values(api.REAL_PROFILE), real=True)
    require(digest(request) == api.pilot.REQUEST_SHA256 and digest(payload) == api.pilot.PAYLOAD_SHA256,
            "previously reviewed request/payload changed")
    capture = subprocess.run([str(api.VENV / "bin/python"), "-I", "-S", "-B", str(Path(__file__).resolve()),
                              "--sdk-body"], env={}, capture_output=True, timeout=30)
    require(capture.returncode == 0 and capture.stderr == b"", "offline SDK capture")
    wire_body = capture.stdout
    require(sha(wire_body) == api.pilot.WIRE_BODY_SHA256, "Decision Packet SDK body changed")
    area = Path(tempfile.mkdtemp(prefix="messages-pilot-candidate-", dir=REPO / ".local")).resolve()
    (area / "runtime").mkdir(mode=0o700)
    spec = api.contract(area, request, 443, os.readlink("/proc/self/ns/net"), secrets.token_hex(16),
                        profile=api.REAL_PROFILE, real=True)
    policy = {"version": 3, "task_sha256": api.task_digest(request), "api": spec}
    api.validate_policy(area, policy, request, real=True)
    api.require_real_release(spec)
    instruction = json.loads(payload["messages"][0]["content"])["instruction"]
    summary = {"status": "CANDIDATE_ONLY_NOT_REAL_GO", "root": str(area),
               "source_sha256": sha(MATERIAL.text.encode()), "task_sha256": api.task_digest(request),
               "request_sha256": digest(request), "payload_sha256": digest(payload),
               "fixed_instruction_sha256": sha(instruction.encode()),
               "wire_body_sha256": sha(wire_body), "wire_body_bytes": len(wire_body),
               "source_bytes": len(MATERIAL.text.encode()),
               "user_content_bytes": len(payload["messages"][0]["content"].encode()),
               "policy_sha256": digest(policy), "code_identity_sha256": digest(spec["code"]),
               "request_bytes": len(canonical(request)), "payload_bytes": len(canonical(payload)),
               "real_permit_issued": False, "real_credential_used": False, "network_requests": 0}
    for filename, value in (("request.json", request), ("payload.json", payload),
                            ("policy.json", policy), ("summary.json", summary)):
        save_new(area / filename, value)
    for filename, raw in (("source.txt", MATERIAL.text.encode()), ("fixed-prompt.txt", instruction.encode()),
                          ("sdk-http-body.json", wire_body),
                          ("user-content.txt", payload["messages"][0]["content"].encode())):
        path = area / filename
        with path.open("xb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
    require(not (area / "permit.json").exists() and not (area / "consumed.json").exists(), "not authorized")
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    if sys.argv[1:] == ["--sdk-body"]:
        sdk_body()
    else:
        main()
