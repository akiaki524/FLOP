"""Fixed offline documents -> service -> Activity -> Human preview; no fetching."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from ccw.model import canonical, decode
from ccw.runtime_interface import ReviewRequest, RuntimeClient, parse_request, task_digest

MATERIAL = ReviewRequest(
    "FLOP Network Yellow Paper v0.5.0\n"
    "§6.2 Agent autonomy — operating without per-action consensus",
    """Agent autonomy — operating without per-action consensus

pallet_session_keys + pallet_agent_wallet let an owner pre-authorize a delegate agent with a lifetime cap, per-tx and daily caps (epoch-reset), a pallet/destination allowlist, and a circuit breaker.

The session-key lifetime MUST be ≤ 864,000 blocks (SessionKeysMaxDuration). This is approximately 10 elapsed days only at uninterrupted 1 s target cadence; missed or delayed blocks extend the elapsed lifetime.

Within its bounds, a delegate MUST be able to act with no per-action consensus — only deterministic on-chain checks — so agents run unattended, submitting asynchronously; validators order.

The owner MUST be able to revoke at any time. Spending MUST be blocked at the session cap, the daily cap, and the circuit breaker; a captured session key MUST be bounded by these caps.""")


def selected_request(material):
    return MATERIAL if material else ReviewRequest(
        "saved:technical-note", "# Recovery\nTODO: document timeout handling.\n")


def activity(material=False):
    # A separate credential-free requester process. It receives one connection,
    # not a service root, credential, resolver or runtime startup capability.
    request = selected_request(material)
    with socket.socket(fileno=os.dup(0)) as endpoint:
        result = RuntimeClient(endpoint).review(request)
    assert not any(name in sys.modules for name in (
        "ccw.secret_handoff", "ccw.runtime_service", "ccw.runtime_fixture"))
    sys.stdout.buffer.write(canonical(vars(result)))


def run(material=False):
    from test_runtime_service import launch, prepare
    (REPO / ".local").mkdir(exist_ok=True)
    area = Path(tempfile.mkdtemp(prefix="runtime-service-smoke-", dir=REPO / ".local"))
    root = area / "service"
    # Trusted offline supervisor fixes this task before handing over a connection.
    request = selected_request(material)
    raw = request.encode()
    prepare(root, request)
    (area / "request.json").write_bytes(raw)
    with launch(root) as (endpoint, child):
        result = subprocess.run([sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
                                 "--activity-material" if material else "--activity"],
                                stdin=endpoint, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env={}, close_fds=True, timeout=8)
        assert result.returncode == 0 and result.stderr == b""
        assert child.wait(timeout=5) == 0
        assert child.stderr.read() == b""
    preview = decode(result.stdout)
    assert preview["offline"] is True
    assert preview["task_sha256"] == task_digest(parse_request(raw))
    preview.update(source_label=request.locator, real_inference=False,
                   task_purpose="保存資料を既存の決定的TODOレビューへ通してHuman Previewする。",
                   interpretation="Offline Fixture。実Claude inferenceではなく、Security評価・要約品質は検証しない。")
    (area / "human-preview.json").write_bytes(canonical(preview))
    from ccw.runtime_interface import ReviewResult
    (area / "human-preview.txt").write_text(
        ReviewResult(preview["task_sha256"], preview["report"]).preview_text() + "\n", encoding="ascii")
    if material:
        assert preview["report"]["findings"] == []  # Source contains no TODO; fixture unchanged.
    else:
        assert preview["report"]["findings"][0]["evidence"]["quote"] == "TODO: document timeout handling."
    evidence = decode((root / "evidence.json").read_bytes())
    assert evidence["runtime_starts"] == 1 and evidence["automatic_retry"] is False
    assert (root / "consumed.json").exists()
    assert list((root / "runtime").iterdir()) == []
    summary = {"status": "PASS", "real_credential": False, "real_inference": False,
               "external_network": False, "offline_preview": True,
               "source_label": request.locator, "task_sha256": preview["task_sha256"],
               "request_bytes": len(raw), "canonical_bytes": len(canonical(parse_request(raw)))}
    (area / "summary.json").write_bytes(canonical(summary))
    print(json.dumps({**summary, "evidence": str(area)}))


if __name__ == "__main__":
    if sys.argv[1:] == ["--activity"]:
        activity()
    elif sys.argv[1:] == ["--activity-material"]:
        activity(material=True)
    elif sys.argv[1:] == ["--material"]:
        run(material=True)
    else:
        assert not sys.argv[1:]
        run()
