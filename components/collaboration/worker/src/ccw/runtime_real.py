"""Trusted Real adapter building blocks. No credential lookup or fallback.

The offline harness uses only a synthetic credential and a private net namespace.
Production startup remains gated separately from these mechanics.
"""
import os
from pathlib import Path
import re
import sys
import time
import stat

from . import claude, claude_real
from .model import canonical, decode, digest, keys, read, require, sha
from .runtime_interface import (frozen_task, MAX_REQUEST_BYTES, MAX_RESULT_BYTES, task_digest,
                                REAL_RUNTIME_TIMEOUT_MS)
from .runtime_output import (OutputRejected, protected_variants, validate_unicode,
                             validated_result)
from .secret_handoff import exchange, private_directory, save_new, launch_guard

REAL_TIMEOUT_MS = REAL_RUNTIME_TIMEOUT_MS
REAL_RAW_BYTES = 262_144
MODEL = "claude-sonnet-4-6"
EFFORT = "high"
LIVE_BLOCKERS = ("Pinned CLI retries SSE overloaded_error despite CLAUDE_CODE_MAX_RETRIES=0",)


def code_manifest():
    names = ("__init__.py", "runtime_real.py", "runtime_real_launcher.py", "runtime_service.py",
             "runtime_output.py", "runtime_interface.py", "secret_handoff.py", "claude_real.py",
             "claude_real_launcher.py", "claude.py", "isolation.py", "model.py", "llm.py", "providers.py")
    return {name: sha(Path(__file__).with_name(name).read_bytes()) for name in names}


def contract(root, request, binary, nonce):
    """Human-side fixed contract; never derived from Activity output."""
    binary = Path(binary)
    require(binary.is_absolute() and binary.resolve() == binary, "binary path")
    return {"version": 1, "kind": "human-real-one-shot", "root": str(Path(root).resolve()),
            "nonce": nonce, "request_sha256": digest(request), "task_sha256": task_digest(request),
            "binary": str(binary), "binary_sha256": claude_real.PINNED_DIGEST,
            "binary_version": claude_real.PINNED_VERSION, "model": MODEL, "effort": EFFORT,
            "argv": arguments(), "code": code_manifest(), "timeout_ms": REAL_TIMEOUT_MS,
            "raw_bytes": REAL_RAW_BYTES, "result_bytes": MAX_RESULT_BYTES,
            "api_retries": 0, "structured_retries": 0, "output_profile": "strict-json-text-v1",
            "real_gate_blockers": list(LIVE_BLOCKERS),
            "credential": "human-memory-to-private-socket-to-cli-oauth-environment",
            "network": "unrestricted-tcp-including-loopback-lan", "provider_requests_hard_cap": None,
            "automatic_retry": False, "fallback": False, "max_attempts": 1}


def validate_policy(root, policy, request):
    keys(policy, "version task_sha256 real")
    require(type(policy["version"]) is int and policy["version"] == 2, "real policy version")
    spec = policy["real"]
    require(type(spec) is dict and type(spec.get("nonce")) is str
            and re.fullmatch(r"[a-f0-9]{32}", spec["nonce"]), "policy nonce")
    require(policy["task_sha256"] == task_digest(request)
            and spec == contract(root, request, spec["binary"], spec["nonce"]), "contract changed")


def validate_permit(root, policy):
    require(not LIVE_BLOCKERS, "Real gate closed: unresolved CLI internal retry")
    root = private_directory(root)
    require(policy.get("version") == 2 and policy.get("real", {}).get("root") == str(root), "permit root")
    require(not (root / "STOP").exists(), "stopped")
    path = root / "permit.json"
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_uid == os.getuid() and info.st_nlink == 1, "private Human permit")
    permit = read(path)
    keys(permit, "version contract_sha256 expires_at human_confirmed")
    require(type(permit["version"]) is int and permit["version"] == 1
            and permit["human_confirmed"] is True and permit["contract_sha256"] == digest(policy)
            and type(permit["expires_at"]) is int and time.time() < permit["expires_at"] <= time.time()+3600,
            "Human permit missing, stale or expired")
    return digest(permit)


def authorize(root, confirmed):
    """Only Human terminal outside Agent ancestry; not called by tests/probes."""
    launch_guard()
    require(not LIVE_BLOCKERS, "Real gate closed: unresolved CLI internal retry")
    root = private_directory(root)
    policy = read(root / "policy.json")
    require(policy.get("version") == 2 and digest(policy) == confirmed, "confirm exact contract")
    require(not (root / "consumed.json").exists(), "attempt consumed")
    save_new(root / "permit.json", {"version": 1, "contract_sha256": digest(policy),
             "expires_at": int(time.time())+300, "human_confirmed": True})


def execute_permitted(root, request, credential, observation):
    policy = read(root / "policy.json")
    validate_policy(root, policy, request)
    permit_sha = validate_permit(root, policy)
    consumed = read(root / "consumed.json")
    require(consumed == {"version": 2, "state": "consumed", "offline": False,
                        "permit_sha256": permit_sha, "service_pid": os.getpid()}, "consumed before launch")
    workspace = root / "runtime"
    claude.prepare(workspace)
    command = [str(Path(sys.executable).resolve()), "-I", "-S", "-B",
               str(Path(__file__).with_name("runtime_real_launcher.py")), str(root), str(os.getpid())]
    before = claude_real.inventory(workspace / "claude-home")
    try:
        return launch(command, workspace, request, credential, observation)
    finally:
        status = ephemeral_inventory_status(before)
        observation["auth_inventory_status"] = status
        require(status == "observed", "runtime inventory changed unexpectedly")


def ephemeral_inventory_status(before):
    """Fresh OAuth-env home: allow measured config creation, never a token file.

    The older persistent-login inventory expects .claude.json to exist before
    launch. This profile starts empty and permits its first creation instead.
    Metadata only; content safety is not inferred from a filename.
    """
    try:
        require(before["entries"] == {}, "empty initial auth directory")
        after = claude_real.inventory(Path(before["path"]))
        claude_real.inventory_diff(before, after)  # identity must be unchanged
        claude_real.validate_auth_inventory(after)
        require(set(after["entries"]) <= {".claude.json", "sessions"}, "unexpected auth artifact")
        return "observed"
    except Exception:
        return "invalid"


def arguments():
    args = claude_real.argv({"kind": "REAL_CLAUDE_NATIVE", "binary": "claude"}, MODEL, EFFORT)
    index = args.index("--json-schema")
    del args[index:index + 2]
    args[args.index("--system-prompt") + 1] = (claude.SYSTEM +
        " Return only a JSON object matching this schema, with no markdown fences: " +
        canonical(claude_real.schema()).decode("ascii"))
    return args


def environment(workspace):
    env = claude_real.environment(workspace, workspace / "claude-home")
    env.update(CLAUDE_CODE_MAX_RETRIES="0", MAX_STRUCTURED_OUTPUT_RETRIES="0")
    env["CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK"] = "1"
    return env


def prompt(request):
    return canonical({"request": "Review this saved technical document; cite exact lines.",
                      "scope": {"include": ["technical review"], "exclude": ["external actions"]},
                      **frozen_task(request)})


def map_output(raw, request, *, protected=(), offline=False):
    if type(raw) is not bytes or len(raw) > REAL_RAW_BYTES:
        raise OutputRejected("oversized_output")
    try:
        data = decode(raw.decode("utf-8", errors="strict"))
        validate_unicode(data)
        for content in (raw, str(data).encode("utf-8")):
            if any(value in content for value in protected_variants(protected)):
                raise OutputRejected("secret_withheld")
        # Single text answer profile: CLI StructuredOutput retries are not used.
        # Do not salvage failed envelopes or accept two competing final answers.
        require(type(data) is dict and "structured_output" not in data
                and type(data.get("result")) is str, "single text result required")
        report = decode(data["result"])
        validate_unicode(report)
        normalized = {**data, "structured_output": report}
        report, _ = claude_real.parse(canonical(normalized), frozen_task(request), {"model": MODEL})
        return validated_result(canonical(report), request, protected, real=True, offline=offline)
    except OutputRejected:
        raise
    except Exception:
        raise OutputRejected("malformed_output") from None


def launch(command, workspace, request, credential, observation, *, offline=False,
           timeout_ms=REAL_TIMEOUT_MS):
    """Private transport; callers must complete admission/consumption first.

    No credential enters argv, model prompt, response or evidence. The first
    stdin line is consumed by the trusted launcher, not by the CLI.
    """
    require(type(credential) is str and 16 <= len(credential) <= 4096
            and re.fullmatch(r"[A-Za-z0-9_.-]+", credential), "credential format")
    require(type(timeout_ms) is int and 50 <= timeout_ms <= REAL_TIMEOUT_MS, "runtime timeout")
    raw = exchange(command, workspace, input_bytes=credential.encode("ascii") + b"\n" + prompt(request),
                   timeout=timeout_ms / 1000, limit=REAL_RAW_BYTES, await_ready=True,
                   observation=observation, forbidden=protected_variants((credential,)),
                   accepted_returncodes=(0, 1))
    observation["runtime_completed"] = True
    return map_output(raw, request, protected=(credential,), offline=offline)


def main():
    """Human-only preparation / permit / one-shot supervisor. Never a Worker tool."""
    import argparse
    import getpass
    import secrets
    import socket
    import subprocess
    from .runtime_interface import (parse_request, ReviewRequest, RuntimeClient, send_frame)
    from .secret_handoff import protect_process
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("root", type=Path)
    prepare.add_argument("request", type=Path)
    prepare.add_argument("binary", type=Path)
    permit = sub.add_parser("permit")
    permit.add_argument("root", type=Path)
    permit.add_argument("--confirm", required=True)
    run = sub.add_parser("run")
    run.add_argument("root", type=Path)
    run.add_argument("request", type=Path)
    args = parser.parse_args()
    root = args.root.absolute()
    if args.action == "prepare":
        request = parse_request(canonical(read(args.request)))
        spec = contract(root, request, args.binary.absolute(), secrets.token_hex(16))
        require(sha(args.binary.read_bytes()) == claude_real.PINNED_DIGEST, "pin changed")
        root.mkdir(mode=0o700)
        (root / "runtime").mkdir(mode=0o700)
        policy = {"version": 2, "task_sha256": task_digest(request), "real": spec}
        save_new(root / "policy.json", policy)
        print(canonical({"policy": policy, "confirm_sha256": digest(policy), "authorized": False}).decode())
        return
    if args.action == "permit":
        authorize(root, args.confirm)
        print("Human permit issued for one attempt; expires in 300 seconds")
        return
    launch_guard()
    protect_process()
    root = private_directory(root)
    request = parse_request(canonical(read(args.request)))
    policy = read(root / "policy.json")
    validate_policy(root, policy, request)
    validate_permit(root, policy)
    require(not (root / "consumed.json").exists(), "attempt consumed")
    # Human owns the master copy. No vault, file, cached login or inherited env lookup.
    credential = getpass.getpass("Human-held setup-token (hidden): ")
    require(type(credential) is str and re.fullmatch(r"[A-Za-z0-9_.-]{16,4096}", credential), "credential format")
    try:
        ours, theirs = socket.socketpair()
        secret_out, secret_in = socket.socketpair()
        try:
            command = [str(Path(sys.executable).resolve()), "-I", "-S", "-B",
                       str(Path(__file__).with_name("runtime_service.py")), str(root),
                       "--credential-fd", str(secret_in.fileno())]
            with subprocess.Popen(command, stdin=theirs, stdout=theirs, stderr=subprocess.DEVNULL,
                    pass_fds=(secret_in.fileno(),), env={}, start_new_session=True) as child:
                theirs.close()
                secret_in.close()
                try:
                    secret_out.settimeout(1)
                    send_frame(secret_out, credential.encode("ascii"), 4096)
                    secret_out.close()
                    credential = None
                    result = RuntimeClient(ours, runtime_kind="real").review(
                        ReviewRequest(request["task"]["locator"], request["task"]["text"], request["timeout_ms"]))
                    child.wait(timeout=2)
                    print(result.preview_text())
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
        finally:
            for endpoint in (ours, theirs, secret_out, secret_in):
                endpoint.close()
    finally:
        credential = None


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        print("Real runtime stopped; do not retry an uncertain attempt", file=sys.stderr)
        sys.exit(2)
