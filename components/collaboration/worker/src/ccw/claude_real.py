"""Real Claude contracts and isolated output mapping; live use remains disabled.

Runtime preparation uses only the native installation and fresh empty homes.
Inventory never opens credential contents. This module does not implement login.
"""
import os
from pathlib import Path
import re
import stat
import sys

from . import claude
from .model import Invalid, canonical, decode, digest, read, require, review, sha, write_new

VERSION = "ccw-claude-b4-1"
EGRESS = "human-accepts-unrestricted-tcp-v1"
LIVE_GATE = "DISABLED: B3 offline implementation; Independent Review and separate Human live gate required"
LOGIN_GATE = "DISABLED: dedicated login requires separate Human approval and callback capability review"
RESOLVER_OPTIONS = "use-vc timeout:2 attempts:1"
PINNED_VERSION = "2.1.274 (Claude Code)"
PINNED_DIGEST = "15e2d05148f801b5774032faad87e624ecd172e9903288bda448b892eb58fa07"
# Syntax only: neither availability nor permission to switch models.
MODEL_ID = r"claude-(?:(?:sonnet|opus|haiku)-[0-9]+(?:[.-][0-9]+)*|[0-9]+(?:[.-][0-9]+)*-(?:sonnet|opus|haiku)(?:-[0-9]{8})?)"
# Reviewed accounting-only identifiers; never passed as --fallback-model.
AUXILIARY_MODELS = ("claude-haiku-4-5", "claude-haiku-4-5-20251001")


def schema():
    result = claude.schema()
    result["properties"]["provider"]["enum"] = ["claude-cli-real-v1"]
    return result


def environment(workspace, auth_home):
    result = claude.environment(workspace)
    result["CLAUDE_CONFIG_DIR"] = str(auth_home)
    # Process-local TCP DNS, no host resolver mutation or UDP permission.
    # Bun/c-ares honoring this option remains a separate runtime verification.
    result["RES_OPTIONS"] = RESOLVER_OPTIONS
    return result


def environment_policy():
    result = claude.environment_policy()
    result["keys"] = sorted([*result["keys"], "RES_OPTIONS"])
    result["credential_source"] = "dedicated-auth-home; real-gate-disabled"
    return result


def argv(spec, model, effort):
    args = claude.argv(spec, model, effort)
    args[args.index("--json-schema") + 1] = canonical(schema()).decode("ascii")
    args[args.index("--system-prompt") + 1] = claude.SYSTEM.replace(
        "Do not fetch links or use tools.",
        "Do not fetch links or use external tools. Use only StructuredOutput to return the required JSON.")
    return args


def inventory(path):
    """Metadata only, no hashes or reads of auth contents, no symlink traversal."""
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path, "resolved dedicated auth home required")
    root = path.lstat()
    require(stat.S_ISDIR(root.st_mode) and root.st_uid == os.getuid()
            and stat.S_IMODE(root.st_mode) == 0o700, "private owned auth directory required")
    entries = {}
    def walk(directory, depth=0):
        require(depth <= 8, "auth inventory depth exceeded")
        for child in sorted(directory.iterdir()):
            require(len(entries) < 256, "auth inventory entry limit")
            info = child.lstat()
            regular, folder = stat.S_ISREG(info.st_mode), stat.S_ISDIR(info.st_mode)
            require((regular or folder) and info.st_uid == os.getuid()
                    and info.st_mode & 0o077 == 0 and (folder or info.st_nlink == 1),
                    "unsafe auth inventory entry")
            entries[str(child.relative_to(path))] = {"kind": "directory" if folder else "file",
                "mode": stat.S_IMODE(info.st_mode), "size": info.st_size,
                "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
                "inode": info.st_ino, "device": info.st_dev}
            if folder:
                walk(child, depth + 1)
    walk(path)
    return {"path": str(path), "owner_uid": root.st_uid, "mode": 0o700,
            "device": root.st_dev, "inode": root.st_ino, "entries": entries,
            "contents_read": False}


def inventory_diff(before, after):
    require(before["path"] == after["path"] and before["inode"] == after["inode"]
            and before["device"] == after["device"], "auth directory replaced")
    old, new = before["entries"], after["entries"]
    return {"added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
            "changed": sorted(k for k in old.keys() & new.keys() if old[k] != new[k])}


def validate_auth_inventory(snapshot):
    """Only reviewed quiescent artifacts; never inspect credential contents.

    sessions is a PID/peer-key registry, NOT an innocuous transcript cache.
    Only its empty 0700 directory is accepted after the CLI has exited.
    """
    entries = snapshot["entries"]
    require(set(entries) <= {".credentials.json", ".claude.json", "sessions"},
            "unexpected auth/config entries; Human review required")
    for name, item in entries.items():
        folder = name == "sessions"
        require(item["kind"] == ("directory" if folder else "file")
                and item["mode"] == (0o700 if folder else 0o600),
                "unexpected auth artifact kind/mode; Human review required")
    return snapshot


def login_plan(root):
    """Reviewable Human-only proposal. No login, token reads or launch permit."""
    spec = target(root)
    auth = validate_auth_inventory(inventory(Path(spec["auth_home"])))
    require(set(auth["entries"]) <= {"sessions"}, "first login requires credential-free auth home")
    workspace = root / "human" / "claude-runtime" / "login-workspace"
    require(not workspace.exists() and not workspace.is_symlink(), "login workspace must be unused")
    return {"kind": "DEDICATED_LOGIN_REVIEW_ONLY", "login_authorized": False,
        "inference_authorized": False, "gate": LOGIN_GATE, "target": spec,
        "argv": [spec["binary"], "--safe-mode", "--setting-sources", "",
                 "--settings", '{"disableAllHooks":true,"fastMode":false}',
                 "auth", "login", "--claudeai"],
        "workspace": str(workspace), "environment_policy": environment_policy(),
        "auth_before": auth, "code_manifest": code_manifest(),
        "io": "private Human terminal via bounded pipes; never Codex transcript or recorded OAuth output",
        "limits": {"launcher_starts": 1, "automatic_retry": False, "wall_seconds": 300},
        "blockers": ["pinned CLI starts 127.0.0.1 callback listener even for manual code entry; current bind/listen/accept denied",
                     "Bun TCP DNS and TLS/OAuth behavior unverified"],
        "permission_proposal": "separately review login-only callback; do not broaden inference profile or permit browser/shell exec",
        "egress_decision": "Human selected option b for first review only; no login/network/credential permission granted",
        "recovery": "stop/reap once; preserve private auth/workspace; no automatic retry; Human reviews provider revocation separately"}


def code_manifest():
    result = claude.code_manifest()
    for name in ("claude_real.py", "claude_real_launcher.py"):
        result[name] = sha(Path(__file__).with_name(name).read_bytes())
    return result


def probe(binary, binary_digest, workspace, flag):
    from .llm import capture
    require(flag in ("--version", "--help", "--empty-auth-status", "--login-help", "--review-help"),
            "non-inference diagnostic only")
    claude.prepare(workspace)
    errors = bytearray()
    try:
        return capture([sys.executable, "-I", "-S", "-B",
            str(Path(__file__).with_name("claude_real_launcher.py")), str(workspace), str(os.getpid()),
            str(binary), binary_digest, flag], workspace, 20, 128000, input_bytes=b"", diagnostics=errors.extend,
            accepted_returncodes=(0, 1) if flag == "--empty-auth-status" else (0,))
    finally:
        # Only no-auth/version/help stderr; production capture never enables this.
        (workspace / "probe-stderr.txt").write_bytes(errors)


def prepare_runtime(root, *, from_prepared_root=None):
    """Copy only a pinned ELF; never reuse/copy the source root's auth state."""
    from .claude_real_launcher import PROFILE, sealed_binary
    if from_prepared_root is None:
        installed = Path.home() / ".local/bin/claude"
        source = installed.resolve(strict=True)
        require(source.parent == Path.home() / ".local/share/claude/versions", "unexpected native install location")
    else:
        previous = Path(from_prepared_root)
        require(previous.is_absolute() and previous.resolve() == previous, "resolved prepared root required")
        source = Path(target(previous)["binary"])  # Metadata/help/hash verified; no auth read.
    area = root / "human" / "claude-runtime"
    require(root.resolve() == root and (root / "human").resolve() == root / "human", "unsafe root")
    area.mkdir(mode=0o700)  # Never overwrite/reuse an existing runtime or auth home.
    pinned = area / "claude"
    fd, identity = sealed_binary(source, PINNED_DIGEST)
    try:
        out = os.open(pinned, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o500)
        try:
            while chunk := os.read(fd, 1024 * 1024):
                pending = memoryview(chunk)
                while pending:
                    pending = pending[os.write(out, pending):]
            os.fsync(out)
        finally:
            os.close(out)
    finally:
        os.close(fd)
    outputs = {}
    for flag in ("--version", "--help"):
        workspace = area / flag[2:]
        workspace.mkdir(mode=0o700)
        outputs[flag] = probe(pinned, identity, workspace, flag)
        (area / (flag[2:] + ".txt")).write_bytes(outputs[flag])
    version = outputs["--version"].decode("ascii").strip()
    require(version == PINNED_VERSION and identity == PINNED_DIGEST, "unreviewed CLI version/binary")
    (area / "auth").mkdir(mode=0o700)
    prepared = {"kind": "REAL_CLAUDE_NATIVE", "binary": str(pinned), "binary_digest": identity,
        "version": version, "source_at_preparation": str(source), "identity": "sha256-sealed-memfd-execveat",
        "version_stdout_digest": sha(outputs["--version"]), "help_stdout_digest": sha(outputs["--help"]),
        "runtime_profile": PROFILE, "auth_home": str(area / "auth"), "auth_owner_uid": os.getuid()}
    write_new(area / "target.json", prepared)
    return prepared


def target(root):
    from .claude_real_launcher import PROFILE, sealed_binary
    area = root / "human" / "claude-runtime"
    require(area.resolve() == area, "unsafe runtime directory")
    spec = read(area / "target.json")
    require(spec["kind"] == "REAL_CLAUDE_NATIVE" and spec["binary"] == str(area / "claude")
            and spec["auth_home"] == str(area / "auth") and spec["auth_owner_uid"] == os.getuid()
            and spec["runtime_profile"] == PROFILE
            and spec["identity"] == "sha256-sealed-memfd-execveat", "Real target policy mismatch")
    require(spec["version"] == PINNED_VERSION and spec["binary_digest"] == PINNED_DIGEST,
            "unreviewed CLI version/binary")
    fd, _ = sealed_binary(spec["binary"], spec["binary_digest"])
    os.close(fd)
    require(sha((area / "version.txt").read_bytes()) == spec["version_stdout_digest"]
            and (area / "version.txt").read_text().strip() == spec["version"]
            and sha((area / "help.txt").read_bytes()) == spec["help_stdout_digest"], "version/help evidence changed")
    return spec


def contract(frozen, model, effort, root, *, approved_inventory=None):
    # Human supplies an explicit complete ID. Aliases/latest/context suffixes and
    # fallback models are not resolved silently by CCW. This is syntax, not a
    # claim that any particular ID is available in the user's subscription.
    require(type(model) is str and len(model) <= 80 and re.fullmatch(MODEL_ID, model)
            and ("sonnet" in model or "opus" in model),
            "explicit Sonnet/Opus full model ID required; aliases refused")
    require(effort in ("low", "medium", "high"), "explicit ordinary effort required")
    spec = target(root)
    auth = inventory(Path(spec["auth_home"])) if approved_inventory is None else approved_inventory
    validate_auth_inventory(auth)
    args = argv(spec, model, effort)
    return {"mode": "CLAUDE_REAL_DISABLED_B4", "provider": "claude-real", "model": model, "effort": effort,
        "task_digest": digest(frozen), "prompt": canonical({k: frozen[k] for k in ("request", "scope", "sources")}).decode("ascii"),
        "schema": schema(), "target": spec, "argv": args, "code_manifest": code_manifest(),
        "auth": {"provider": "anthropic", "mode": "subscription-oauth", "verified": False,
                 "inventory_before": auth, "extra_usage": "Human must verify OFF"},
        "cwd_policy": str(root / "runner") + "/<single-attempt-uuid>; fresh empty workspace",
        "environment_policy": environment_policy(),
        "boundary": spec["runtime_profile"], "egress": {"strategy": EGRESS,
            "human_accepted": False, "socket_domains": ["AF_INET", "AF_INET6"],
            "planning_decision": "option b selected for first review; separate live authorization absent",
            "dns": {"mechanism": "RES_OPTIONS", "value": RESOLVER_OPTIONS,
                    "bun_verified": False, "host_resolver_changed": False},
            "socket_type": "TCP stream only", "endpoint_restricted": False,
            "residual_risk": "arbitrary TCP including loopback/LAN; runtime can read dedicated credentials"},
        "limits": {"launcher_starts": 1, "automatic_retry": False, "cpu_seconds": 30,
            "cli_max_turns_requested": 1, "provider_requests": None, "internal_retries": None,
            "token_cap_enforced": False, "money_cap_enforced": False},
        "model_comparison": "requested ID must be a usage key; canonicalModel is pricing metadata; auxiliary keys are accounting only, roles unproven",
        "auxiliary_usage_ids": list(AUXILIARY_MODELS), "automatic_model_fallback": False,
        "structured_output": "internal read-only final-answer tool; no Bash/WebFetch/Agent capability",
        "real_gate": LIVE_GATE, "code_version": VERSION}


def launch(request, workspace, timeout, limit, stop, started):
    """Online launch plumbing, fenced by Broker AND the launcher entry point."""
    from .llm import capture
    require(request["code_manifest"] == code_manifest(), "Real launcher dependency changed")
    spec = request["target"]
    require(inventory(Path(spec["auth_home"])) == request["auth"]["inventory_before"],
            "auth inventory changed after approval")
    require(set(p.name for p in workspace.iterdir()) == {"home", "claude-home", "tmp"}
            and all(not list((workspace / n).iterdir()) for n in ("home", "claude-home", "tmp")),
            "Real workspace contamination")
    command = [sys.executable, "-I", "-S", "-B", str(Path(__file__).with_name("claude_real_launcher.py")),
        "online", str(workspace), str(os.getpid()), spec["binary"], spec["binary_digest"],
        spec["auth_home"], request["model"], request["effort"]]
    return capture(command, workspace, timeout, limit, input_bytes=request["prompt"].encode("ascii"),
                   stop=stop, started=started, accepted_returncodes=(0, 1))


RESULT_FIELDS = {"type", "subtype", "is_error", "duration_ms", "duration_api_ms", "num_turns",
    "result", "stop_reason", "total_cost_usd", "usage", "modelUsage", "permission_denials",
    "structured_output", "uuid", "session_id", "deferred_tool_use", "errors", "api_error_status",
    "terminal_reason", "origin", "ttft_ms", "fast_mode_state", "fast_mode_disabled_reason",
    "subagent_stats", "queued_turn_count", "result_index", "api_error_code"}
TIMING_FIELDS = {"ttft_ms", "ttft_stream_ms", "time_to_request_ms", "first_content_frame_ms",
    "first_stream_post_ms", "first_stream_post_ack_ms", "time_to_request_from_spawn_ms"}
WALL_FIELDS = {"request_sent_wall_ms", "first_stream_post_wall_ms", "time_origin_ms"}
RESULT_FIELDS |= TIMING_FIELDS | WALL_FIELDS | {"warm_spare_claimed", "user_message_uuid", "user_message_uuids"}
COUNTERS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
USAGE_FIELDS = set(COUNTERS) | {"server_tool_use", "service_tier", "cache_creation", "inference_geo",
                              "iterations", "speed", "output_tokens_details"}
MODEL_FIELDS = {"inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens",
                "webSearchRequests", "costUSD", "contextWindow", "maxOutputTokens", "thinkingTokens",
                "canonicalModel", "provider", "costBasis"}
FAST_REASONS = {"free", "preference", "extra_usage_disabled", "network_error", "unknown",
    "not_first_party", "disabled_by_env", "model_not_allowed", "sdk_opt_in_required", "pending"}


def zero_subagents(value):
    fields = {"spawned", "requested", "started_in_background", "by_type", "max_depth",
              "spawned_by_subagents", "completed", "failed", "killed", "refused"}
    require(type(value) is dict and set(value) == fields, "invalid subagent stats shape")
    nested = {"requested": {"background", "foreground", "unset"},
              "killed": {"parent", "user", "system"},
              "refused": {"depth_limit", "concurrency_limit", "budget"}, "by_type": set()}
    for key, item in value.items():
        if key in nested:
            require(type(item) is dict and set(item) == nested[key], "invalid subagent stats detail")
            values = item.values()
        else:
            values = [item]
        require(all(type(v) is int and v == 0 for v in values), "subagent activity reported")


def post_inventory(before):
    """Retain safe failure evidence even if a post-run tree cannot be inventoried."""
    try:
        after = inventory(Path(before["path"]))
        changes = inventory_diff(before, after)
        status = "observed"
        try:
            validate_auth_inventory(before)
            validate_auth_inventory(after)
        except Invalid:
            status = "review_required"
        # The only normal topology change accepted is creation of EMPTY sessions/.
        if set(changes["added"]) - {"sessions"} or changes["removed"]:
            status = "review_required"
        if "sessions" in before["entries"] and "sessions" in after["entries"]:
            old, new = before["entries"]["sessions"], after["entries"]["sessions"]
            if any(old[k] != new[k] for k in ("inode", "device")):
                status = "review_required"
        return {"before": before, "after": after, "diff": changes, "status": status}
    except (Invalid, OSError):
        return {"before": before, "after": None, "diff": None,
                "status": "invalid", "reason": "auth inventory unsafe or unavailable; preserve private workspace"}


class ParseRejected(Invalid):
    """Parser-owned diagnostic, never raw CLI exception/error text."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


REJECTION_REASONS = frozenset({
    "unknown Real result field", "unsuccessful Real result", "invalid turn count",
    "reported model missing/mismatched", "unknown model usage field", "invalid canonical pricing model",
    "unapproved model provider", "invalid cost basis", "invalid model usage", "model web search use reported",
    "unknown/invalid usage field", "invalid usage counter", "unknown nested usage field", "invalid nested usage",
    "server tool use reported", "unreviewed iteration usage", "unapproved speed reported", "invalid usage label",
    "unexpected queue/result index", "unapproved fast mode", "invalid fast mode reason",
    "invalid subagent stats shape", "invalid subagent stats detail", "subagent activity reported",
    "API error code reported", "unexpected tool permission denial", "tool deferral or API error reported",
    "unexpected terminal reason", "unexpected result origin", "invalid CLI identifier", "invalid CLI metric",
    "invalid ttft metric", "invalid result text", "unexpected stop reason",
    "invalid JSON/schema/quote or parser failure",
    "error_during_execution", "error_max_turns", "error_max_budget_usd", "error_max_structured_output_retries",
})


def rejection_reason(exc):
    # Do not echo attacker-controlled JSON keys, provider error text or traceback.
    reason = str(exc)
    return reason if reason in REJECTION_REASONS else "invalid JSON/schema/quote or parser failure"


def parse(raw, frozen, request):
    """Strict, version-reviewed subset of official result output, never auth JSON."""
    from .llm import schema_check
    data = decode(raw)
    require(type(data) is dict and set(data) <= RESULT_FIELDS, "unknown Real result field")
    if data.get("subtype") in ("error_during_execution", "error_max_turns", "error_max_budget_usd",
                               "error_max_structured_output_retries"):
        raise Invalid(data["subtype"])
    require(data.get("type") == "result" and data.get("subtype") == "success"
            and data.get("is_error") is False, "unsuccessful Real result")
    # Output observation, NOT a provider-request cap or evidence of external tools.
    require(type(data.get("num_turns")) is int and 1 <= data["num_turns"] <= 1024, "invalid turn count")
    models = data.get("modelUsage")
    require(type(models) is dict and request["model"] in models
            and set(models) <= {request["model"], *AUXILIARY_MODELS}, "reported model missing/mismatched")
    per_model = models[request["model"]]
    for entry in models.values():
        require(type(entry) is dict and set(entry) <= MODEL_FIELDS, "unknown model usage field")
        for key, value in entry.items():
            if key == "canonicalModel":
                require(type(value) is str and len(value) <= 80 and re.fullmatch(MODEL_ID, value),
                        "invalid canonical pricing model")
            elif key == "provider":
                require(value == "firstParty", "unapproved model provider")
            elif key == "costBasis":
                require(type(value) is str and value in ("list", "managed", "unknown"), "invalid cost basis")
            else:
                require(type(value) in ((int, float) if key == "costUSD" else (int,)) and 0 <= value <= 1e12,
                        "invalid model usage")
        require(entry.get("webSearchRequests", 0) == 0, "model web search use reported")
    usage = data.get("usage")
    require(usage is None or type(usage) is dict and set(usage) <= USAGE_FIELDS, "unknown/invalid usage field")
    usage = usage or {}
    for key in COUNTERS:
        require(usage.get(key) is None or type(usage[key]) is int and 0 <= usage[key] <= 10**12,
                "invalid usage counter")
    for key, fields in (("server_tool_use", {"web_search_requests", "web_fetch_requests"}),
                        ("cache_creation", {"ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"}),
                        ("output_tokens_details", {"thinking_tokens"})):
        nested = usage.get(key)
        if nested is not None:
            require(type(nested) is dict and set(nested) <= fields, "unknown nested usage field")
            require(all(type(v) is int and 0 <= v <= 10**12 for v in nested.values()), "invalid nested usage")
            if key == "server_tool_use":
                require(all(v == 0 for v in nested.values()), "server tool use reported")
    require(usage.get("iterations") in (None, []), "unreviewed iteration usage")
    require(usage.get("speed") in (None, "standard"), "unapproved speed reported")
    for key in ("service_tier", "inference_geo"):
        require(usage.get(key) is None or type(usage[key]) is str and len(usage[key]) <= 80, "invalid usage label")
    for key in ("queued_turn_count", "result_index"):
        require(key not in data or type(data[key]) is int and data[key] == 0, "unexpected queue/result index")
    require("fast_mode_state" not in data or data["fast_mode_state"] == "off", "unapproved fast mode")
    require("fast_mode_disabled_reason" not in data or type(data["fast_mode_disabled_reason"]) is str
            and data["fast_mode_disabled_reason"] in FAST_REASONS, "invalid fast mode reason")
    if "subagent_stats" in data:
        zero_subagents(data["subagent_stats"])
    require("api_error_code" not in data, "API error code reported")
    require(data.get("permission_denials", []) == [], "unexpected tool permission denial")
    require(data.get("deferred_tool_use") is None and data.get("errors") in (None, [])
            and data.get("api_error_status") is None, "tool deferral or API error reported")
    require(data.get("terminal_reason") in (None, "completed"), "unexpected terminal reason")
    require(data.get("origin") in (None, {"kind": "human"}), "unexpected result origin")
    for key in ("session_id", "uuid"):
        require(data.get(key) is None or type(data[key]) is str and
                re.fullmatch(r"[a-zA-Z0-9-]{1,80}", data[key]), "invalid CLI identifier")
    for key in ("duration_ms", "duration_api_ms", "total_cost_usd"):
        value = data.get(key)
        types = (int, float) if key == "total_cost_usd" else (int,)
        require(value is None or type(value) in types and 0 <= value <= 1e12, "invalid CLI metric")
    for key in TIMING_FIELDS | WALL_FIELDS:
        require(key not in data or type(data[key]) in ((int,) if key in TIMING_FIELDS else (int, float))
                and 0 <= data[key] <= 10**15, "invalid ttft metric")
    require("warm_spare_claimed" not in data or type(data["warm_spare_claimed"]) is bool, "invalid CLI metric")
    require("user_message_uuid" not in data or type(data["user_message_uuid"]) is str
            and re.fullmatch(r"[a-zA-Z0-9-]{1,80}", data["user_message_uuid"]), "invalid CLI identifier")
    require("user_message_uuids" not in data or type(data["user_message_uuids"]) is list
            and len(data["user_message_uuids"]) <= 1 and all(type(v) is str and re.fullmatch(
                r"[a-zA-Z0-9-]{1,80}", v) for v in data["user_message_uuids"]), "invalid CLI identifier")
    require(data.get("result") is None or type(data["result"]) is str, "invalid result text")
    require(data.get("stop_reason") in (None, "end_turn", "stop_sequence", "tool_use"), "unexpected stop reason")
    report = data.get("structured_output")
    schema_check(report, schema())
    review(report, frozen)
    metrics = {key: {"value": usage.get(key), "source": "cli.result.usage." + key,
                    "definition": "CLI main-loop raw counter; excludes auxiliary calls; no summation"} for key in COUNTERS}
    metrics["reasoning_tokens"] = {"value": per_model.get("thinkingTokens"),
        "source": "cli.result.modelUsage." + request["model"] + ".thinkingTokens" if "thinkingTokens" in per_model else None,
        "definition": "subset of outputTokens if available; never added to output"}
    return report, {"requested_model": request["model"], "reported_models": list(models),
        "cli_session_id": data.get("session_id"), "provider_request_ids": None,
        "provider_requests": None, "internal_retries": None, "cli_turns": data["num_turns"],
        "usage": metrics, "model_usage": models, "plan_remaining": None, "plan_reset_at": None,
        "reported_primary_model": None, "requested_model_usage_key": request["model"],
        "auxiliary_usage_keys": sorted(set(models) - {request["model"]}),
        "model_roles_verified": False, "automatic_model_fallback_authorized": False,
        "canonical_pricing_models": {k: v.get("canonicalModel") for k, v in models.items()},
        "cost_basis": {k: v.get("costBasis") for k, v in models.items()},
        "result_metadata": {k: data.get(k) for k in ("ttft_ms", "fast_mode_state",
            "fast_mode_disabled_reason", "subagent_stats", "queued_turn_count", "result_index")},
        "subscription_actual_charge": None, "missing_fields": sorted(RESULT_FIELDS - data.keys()),
        "missing_usage_fields": [key for key in COUNTERS if key not in usage], "unknown_fields": [],
        "api_equivalent_cost": {"value": data.get("total_cost_usd"), "source": "cli.result.total_cost_usd",
                                "definition": "CLI estimate, NOT subscription billing"}}


def parse_isolated(raw, frozen, request, workspace):
    """No auth/approval/target data are passed to the result parser process."""
    from .llm import capture
    parser = workspace / "parser"
    parser.mkdir(mode=0o700)
    write_new(parser / "input.json", {"raw": raw.decode("utf-8"), "frozen": frozen, "model": request["model"]})
    output = capture([sys.executable, "-I", "-S", "-B", str(Path(__file__).with_name("claude_real_launcher.py")),
                      "parse", str(parser)], parser, 5, 256000)
    result = decode(output)
    require(type(result) is dict and type(result.get("ok")) is bool, "invalid parser envelope")
    if not result["ok"]:
        require(set(result) == {"ok", "reason"} and result["reason"] in REJECTION_REASONS,
                "invalid parser rejection")
        raise ParseRejected(result["reason"])
    require(set(result) == {"ok", "report", "measurements"}, "invalid parser success")
    return result["report"], result["measurements"]
