"""Trusted one-shot offline service entrypoint, NOT an Activity/Worker tool.

Supervisor supplies a private root with policy.json and an empty runtime/.
stdin/stdout are the same connected anonymous socket; only task/results cross
it. One root is one service instance / one permitted task / one attempt. Never
make root creation or this entrypoint a capability of an untrusted requester.
"""
import fcntl
import os
from pathlib import Path
import resource
import secrets
import socket
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccw import isolation
from ccw.model import canonical, identifier, keys, read, require
from ccw.runtime_interface import (MAX_REQUEST_BYTES, MAX_RESULT_BYTES, parse_request,
                                   receive_frame, send_frame, task_digest)
from ccw.runtime_output import OutputRejected, protected_variants, validated_result
from ccw.secret_handoff import ExchangeFailure, exchange, private_directory, protect_process, save_new

FAILURE = {"version": 1, "status": "refused", "offline": True}


def _dummy_credential():
    # No external source, path, provider selection, auth method or resolver hook.
    return "ccw-dummy-" + secrets.token_hex(24)


def _execute(request, workspace, observation):
    value = _dummy_credential()
    try:
        command = [str(Path(sys.executable).resolve()), "-I", "-S", "-B",
                   str(Path(__file__).with_name("runtime_fixture.py")),
                   str(workspace), str(os.getpid())]
        raw = exchange(command, workspace,
                       input_bytes=canonical({"request": request}),
                       timeout=request["timeout_ms"] / 1000, limit=MAX_RESULT_BYTES,
                       retain_stdout=True, await_ready=True, observation=observation,
                       forbidden=protected_variants((value,)))
        observation["runtime_completed"] = True
        return validated_result(raw, request, protected=(value,))
    finally:
        value = None  # lifetime reduction, not Python memory zeroization


def serve(root, endpoint, *, real=False, credential_endpoint=None, api=False, api_real=False):
    """Dedicated main process only (exchange owns signal/subreaper state)."""
    lock = None
    consumed = False
    result = FAILURE
    observation = {"version": 2, "status": "refused", "offline": True,
                   "runtime_kind": "offline-fake", "attempt_consumed": True,
                   "runtime_starts": 0, "runtime_completed": False,
                   "runtime_ready_observed": False, "runtime_input_delivered": False,
                   "runtime_child_returncode": None,
                   "automatic_retry": False, "fallback": False,
                   "provider_usage": None, "provider_charge": None,
                   "external_actions_executed": False}
    if real:
        observation.update(offline=False, runtime_kind="real")
    if api:
        observation.update(offline=not api_real, runtime_kind="real-messages-api" if api_real else "offline-messages-api", provider_post_max=1,
                           provider_posts_observed=None, sdk_max_retries=0, transport_retries=0,
                           follow_redirects=False, trust_env=False,
                           usage_source=("unconfirmed; provider accounting not retrieved" if api_real else
                                         "unconfirmed; offline synthetic response is not provider accounting"))
    try:
        require(not (real and api), "separate CLI and API rails")
        require(not api_real or api, "API Real requires API rail")
        protect_process()
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.environ.clear()
        isolation.abi()
        root = private_directory(root)
        workspace = private_directory(root / "runtime")
        require(not list(workspace.iterdir()), "runtime workspace must be empty")
        policy = read(root / "policy.json")
        if not real and not api:
            keys(policy, "version task_sha256")
            require(type(policy["version"]) is int and policy["version"] == 1, "policy")
        identifier(policy["task_sha256"])
        lock = os.open(root / "service.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "lock")
        # Across processes sharing this trusted instance: max 1, reject, no queue.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(not (root / "consumed.json").exists(), "attempt already consumed")
        # A total admission deadline prevents slow/truncated frames holding the lock.
        endpoint.settimeout(1)
        started = time.monotonic()
        # socket timeout is per recv; a total wall deadline is enforced by alarm.
        import signal
        def admission_timeout(signum, frame):
            raise TimeoutError()
        previous = signal.signal(signal.SIGALRM, admission_timeout)
        signal.setitimer(signal.ITIMER_REAL, 1)
        try:
            request = parse_request(receive_frame(endpoint, MAX_REQUEST_BYTES))
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        require(time.monotonic() - started < 1, "admission timeout")
        require(task_digest(request) == policy["task_sha256"], "task not permitted")
        marker = {"version": 1, "state": "consumed", "offline": True}
        if real:
            from ccw import runtime_real
            runtime_real.validate_policy(root, policy, request)
            permit_sha = runtime_real.validate_permit(root, policy)
            require(credential_endpoint is not None, "private credential channel required")
            marker = {"version": 2, "state": "consumed", "offline": False,
                      "permit_sha256": permit_sha, "service_pid": os.getpid()}
        if api:
            from ccw import runtime_api
            policy_sha, permit_sha = runtime_api.admission(root, policy, request, real=api_real)
            require(credential_endpoint is not None, "private API credential channel required")
            marker = runtime_api.consumed_marker(policy, policy_sha, permit_sha, os.getpid())
            observation.update(contract_sha256=policy_sha, sdk_version=runtime_api.SDK_VERSION,
                               permit_sha256=permit_sha, profile=policy["api"]["profile"],
                               resource_budget_reserved={"attempts": 1, "provider_posts": 1,
                                   "input_bytes": policy["api"]["input_bytes_max"], "output_tokens": policy["api"]["output_tokens_max"]},
                               # Admission above verified the exact-policy Human permit for Real.
                               # This records authorization, not provider usage or charge.
                               budget_refunded=False, separate_api_billing=True, real_spending_authorized=api_real)
        # Consumption precedes credential creation; crashes/failures never restore it.
        save_new(root / "consumed.json", marker)
        consumed = True
        if real or api:
            credential_endpoint.settimeout(1)
            value = None
            try:
                value = receive_frame(credential_endpoint, 4096).decode("ascii")
                credential_endpoint.close()
                if api:
                    result = runtime_api.execute_offline(root, request, value, observation, real=api_real)
                else:
                    result = runtime_real.execute_permitted(root, request, value, observation)
            finally:
                value = None
        else:
            result = _execute(request, workspace, observation)
        require(len(canonical(result)) <= MAX_RESULT_BYTES, "result budget")
        observation.update(status="succeeded", failure=None)
        save_new(root / "evidence.json", observation)
    except BaseException as exc:
        result = FAILURE
        if consumed:
            try:
                reason = exc.reason if isinstance(exc, (OutputRejected, ExchangeFailure)) else "internal_failure"
                observation.update(status="refused", failure=reason)
                save_new(root / "evidence.json", observation)
            except BaseException:
                pass
    finally:
        if credential_endpoint is not None:
            credential_endpoint.close()
        if lock is not None:
            os.close(lock)
    try:
        endpoint.settimeout(1)
        send_frame(endpoint, canonical(result), MAX_RESULT_BYTES)
    except Exception:
        return 2
    return 0 if result is not FAILURE else 2


def main():
    real = len(sys.argv) == 4 and sys.argv[2] == "--credential-fd"
    api_real = len(sys.argv) == 4 and sys.argv[2] == "--api-real-credential-fd"
    api = api_real or (len(sys.argv) == 4 and sys.argv[2] == "--api-offline-credential-fd")
    require(len(sys.argv) == 2 or real or api, "supervisor root required")
    require(isolation.socketpair_peer(0) is not None, "supervisor socket required")
    import signal
    parent = os.getppid()
    isolation.checked(isolation.LIBC.prctl(1, signal.SIGKILL, 0, 0, 0), "supervisor death")
    require(os.getppid() == parent, "supervisor exited")
    secret_endpoint = None
    if real or api:
        fd = int(sys.argv[3])
        require(fd > 2 and isolation.socketpair_peer(fd) == os.getppid(), "private Human channel")
        secret_endpoint = socket.socket(fileno=fd)
    # The supervisor passes no other FDs and starts with env={}; clear again in serve.
    with socket.socket(fileno=os.dup(0)) as endpoint:
        return serve(Path(sys.argv[1]), endpoint, real=real, credential_endpoint=secret_endpoint, api=api, api_real=api_real)


if __name__ == "__main__":
    try:
        exit_code = main()
    except BaseException:
        # Never print exception context, environment or private runtime output.
        exit_code = 2
    sys.exit(exit_code)
