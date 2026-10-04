"""Stage 00 collector: GET/read-only commands; stdout only; never create a receipt here."""

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import shlex
import subprocess
import time
import urllib.request

from policy import (OBSERVERS, POLICY, PILOT_ACCEPTANCE, PILOT_CLASSIFICATION,
                    STANDING_ACCEPTANCE, STANDING_CLASSIFICATION,
                    digest, freshness, public_config, validate_decisions)

SUPERVISOR_MAX_AGE = 180
# Observed successful runs take 6–16s; allow 45s without relaxing freshness.
SUPERVISOR_WAIT_SECONDS = 45
SUPERVISOR_POLL_SECONDS = 2
SUPERVISOR_JOURNAL = ["journalctl", "--unit", "technocore-observer-health.service", "--boot",
                      "--since", "-3min", "--lines", "100", "--no-pager", "--output", "json"]
PRODUCTION_CONTAINERS = {"tc-cap-loop-01-lobby-capture", "tc-cap-loop-01-lobby-archive"}


class Reader:
    def __init__(self):
        self.errors = []

    def command(self, args, *, optional=False, timeout=30):
        # No shell, exec, run, start, create, healthcheck execution, or arbitrary command input.
        permitted = {
            "docker": {"version", "info", "ps", "inspect", "stats", "logs", "network"},
            "systemctl": {"show", "list-unit-files", "list-units", "list-timers"},
            "findmnt": {"--json"}, "losetup": {"--list"},
            "nft": {"--json"}, "iptables-save": set(), "ip6tables-save": set(),
            "ip": {"-json"},
            "journalctl": {"--unit"},
        }
        assert args[0] in permitted
        assert not permitted[args[0]] or args[1] in permitted[args[0]]
        if args[:2] == ["docker", "network"]:
            assert args[2] in ("ls", "inspect")
        if args[0] == "nft":
            assert args[1:] == ["--json", "list", "ruleset"]
        if args[0] == "journalctl":
            assert args == SUPERVISOR_JOURNAL
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                               env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
            if r.returncode:
                raise RuntimeError("READ_COMMAND_FAILED")
            return r.stdout
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            self.errors.append({"command": args[0] + ":" + (args[1] if len(args) > 1 else "read"),
                                "optional": optional, "error": "UNAVAILABLE"})
            return None


def decoded(raw, default):
    try:
        return json.loads(raw) if raw is not None else default
    except ValueError:
        return default


def safe_name(x):
    return x if isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9_./:@+\-]{1,160}", x) else "REDACTED"


def get_json(url):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    assert url in ("https://technocore.chat/config", "https://api.ipify.org?format=json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    with opener.open(req, timeout=20) as r:
        if r.status != 200:
            raise ValueError("GET_STATUS")
        raw = r.read(1024*1024 + 1)
        if len(raw) > 1024*1024:
            raise ValueError("GET_SIZE")
        return json.loads(raw)


def launch_class(text):
    low = text.lower()
    return [key for key, patterns in {
        "capture": ("fullcap", "full-capture", "full_capture", "capture_first", "technocore-capture"),
        "observer": ("technocore_observer", "technocore-observer"),
        "technocore_client": ("technocore.chat",),
    }.items() if any(p in low for p in patterns)]


def process_inventory():
    # Skip only our own ancestry, not unrelated Python/SSH processes.
    excluded, pid = set(), os.getpid()
    while pid > 1 and pid not in excluded:
        excluded.add(pid)
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            pid = int(fields[1])
        except (OSError, ValueError, IndexError):
            break
    matches, denied = [], 0
    for p in Path("/proc").iterdir():
        if not p.name.isdigit() or int(p.name) in excluded:
            continue
        try:
            raw = (p / "cmdline").read_bytes().decode("utf-8", "replace")
            tags = launch_class(raw)
            if tags:
                matches.append({"pid": int(p.name), "uid": p.stat().st_uid,
                                "comm": safe_name((p / "comm").read_text().strip()),
                                "class": tags, "argv_sha256": hashlib.sha256(raw.encode()).hexdigest()})
        except FileNotFoundError:
            pass
        except PermissionError:
            denied += 1
    return {"candidates": matches, "permission_denied": denied,
            "scope": "host-visible cmdline keyword matches; not proof of all same-NAT clients"}


def launch_inventory(reader):
    roots = ("/etc/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system",
             "/etc/cron.d", "/etc/cron.daily", "/etc/cron.hourly", "/var/spool/cron/crontabs")
    files, seen = [], set()
    candidates = [Path("/etc/crontab"), Path("/etc/rc.local")]
    for name in roots:
        root = Path(name)
        if root.exists():
            candidates.extend(root.rglob("*"))
    denied = 0
    for p in candidates:
        try:
            if not p.is_file() or p.resolve() in seen:
                continue
            seen.add(p.resolve())
            if p.stat().st_size > 1024*1024:
                continue
            raw = p.read_bytes()
            tags = launch_class(raw.decode("utf-8", "replace"))
            if tags:
                files.append({"path": safe_name(str(p)), "class": tags,
                              "sha256": hashlib.sha256(raw).hexdigest()})
        except PermissionError:
            denied += 1
        except OSError:
            pass
    units = []
    raw = reader.command(["systemctl", "list-unit-files", "--no-legend", "--no-pager"])
    live = reader.command(["systemctl", "list-units", "--all", "--plain", "--no-legend", "--no-pager"])
    names = {Path(x["path"]).name for x in files
             if Path(x["path"]).suffix in (".service", ".timer")}
    for line in ((raw or "") + "\n" + (live or "")).splitlines():
        name = line.split()[0] if line.split() else ""
        if launch_class(name):
            names.add(name)
    for name in sorted(names):
        units.append(unit_state(reader, name))
    return {"files": files, "units": units, "permission_denied": denied,
            "scope": "systemd definitions/drop-ins, cron and rc.local; no ExecStart/Environment printed"}


def unit_state(reader, name, *, timeout=30):
    props = ("Id", "LoadState", "ActiveState", "SubState", "UnitFileState", "Result",
             "ExecMainStatus", "FragmentPath", "Triggers", "TriggeredBy",
             "ExecMainStartTimestampMonotonic", "ExecMainExitTimestampMonotonic")
    args = ["systemctl", "show", name, "--no-pager"]
    for prop in props:
        args.extend(["--property", prop])
    raw = reader.command(args, timeout=timeout)
    return {k: v for k, sep, v in (line.partition("=") for line in (raw or "").splitlines())
            if sep and k in props}


def supervisor_health(reader):
    """Authority is the external supervisor's fresh successful completion.

    Monotonic systemd timestamps use this host's current boot clock; a saved
    Result=success alone is not proof of freshness. Journal detail is optional.
    """
    timer = unit_state(reader, "technocore-observer-health.timer")
    service = unit_state(reader, "technocore-observer-health.service")
    def in_flight():
        return (service.get("ActiveState") == "activating"
                or service.get("SubState") in ("start", "start-pre", "start-post", "running"))

    waiting_since = time.monotonic()
    deadline = waiting_since + SUPERVISOR_WAIT_SECONDS
    polls = 0
    while in_flight():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(SUPERVISOR_POLL_SECONDS, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        service = unit_state(reader, "technocore-observer-health.service", timeout=min(5, remaining))
        polls += 1
    wait = {"max_seconds": SUPERVISOR_WAIT_SECONDS, "polls": polls,
            "elapsed_seconds": time.monotonic() - waiting_since, "timed_out": in_flight()}
    if polls:
        timer = unit_state(reader, "technocore-observer-health.timer")
    raw = reader.command(SUPERVISOR_JOURNAL, optional=True, timeout=10)
    now = time.monotonic()
    try:
        completed = int(service.get("ExecMainExitTimestampMonotonic", "0")) / 1e6
        began = int(service.get("ExecMainStartTimestampMonotonic", "0")) / 1e6
    except (TypeError, ValueError):
        completed = began = 0
    age = now - completed if completed > 0 else None
    healthy = (timer.get("ActiveState") == "active" and service.get("Result") == "success"
               and service.get("ExecMainStatus") == "0" and age is not None
               and not in_flight() and 0 < began <= completed
               and 0 <= age <= SUPERVISOR_MAX_AGE)
    health, stamps = {}, {}
    for line in (raw or "").splitlines():
        obj = decoded(line, {})
        if not isinstance(obj, dict) or not isinstance(obj.get("MESSAGE"), str):
            continue
        try:
            stamp = int(obj.get("__MONOTONIC_TIMESTAMP", "0")) / 1e6
        except (TypeError, ValueError):
            continue
        # Only detail from the completed invocation is authoritative; never
        # combine an in-flight invocation's journal with an earlier exit.
        if not (0 < began <= stamp <= completed and 0 <= now - stamp <= SUPERVISOR_MAX_AGE):
            continue
        m = re.search(r"\b(rules|results)\b.*\bhealth=([A-Za-z0-9_-]+)\b.*\blag_streak=(\d+)\b.*\baction=([A-Za-z0-9_-]+)\b", obj["MESSAGE"])
        if m and stamp >= stamps.get(m[1], 0):
            stamps[m[1]] = stamp
            health[m[1]] = {"health": "OK" if m[2] == "OK" else "NOT_OK",
                            "lag_streak": int(m[3]), "action": "none" if m[4] == "none" else "OTHER"}
    if any(row != {"health": "OK", "lag_streak": 0, "action": "none"} for row in health.values()):
        healthy = False
    return {"timer": timer, "service": service, "health": health, "completion_wait": wait,
            "freshness": {"verdict": "PASS" if healthy else "FAIL",
                          "source": "external_health_supervisor",
                          "completed_monotonic_seconds": completed or None,
                          "completion_age_seconds": age, "max_age_seconds": SUPERVISOR_MAX_AGE,
                          "meaning": "successful supervisor execution; not container-log or remote-tail freshness"}}


def rule_value(value):
    """Only network semantics, not arbitrary literal matches or log text."""
    if type(value) in (int, bool) or value is None:
        return value
    if isinstance(value, list):
        return [rule_value(v) for v in value]
    if isinstance(value, dict):
        return {k: rule_value(v) for k, v in value.items()
                if k in ("payload", "protocol", "field", "meta", "key", "ct", "prefix", "addr", "len", "set", "range")}
    if isinstance(value, str):
        if value in {"ip", "ip6", "tcp", "udp", "icmp", "icmpv6", "saddr", "daddr", "sport", "dport",
                     "iif", "oif", "iifname", "oifname", "l4proto", "state", "established", "related",
                     "new", "invalid", "ACCEPT", "DROP", "REJECT", "MASQUERADE", "RETURN"}:
            return value
        try:
            return str(ipaddress.ip_network(value, strict=False))
        except ValueError:
            pass
        if re.fullmatch(r"(?:br-|veth|eth|ens|enp|docker|lo|@)[A-Za-z0-9_.:\-]*", value):
            return value
        if re.fullmatch(r"[0-9]+(?::[0-9]+)?", value): return value
    return "REDACTED_OR_UNKNOWN"


def firewall_evidence(reader):
    """Show structural evidence, never comments/log prefixes/arbitrary rule strings."""
    raw = reader.command(["nft", "--json", "list", "ruleset"], optional=True)
    result = {"nft": {"available": raw is not None}, "classification": "HUMAN_REVIEW_REQUIRED"}
    if raw is not None:
        obj = decoded(raw, {})
        chains, rules, sets = [], [], []
        for entry in obj.get("nftables", []):
            for kind in ("set", "map", "element"):
                if kind in entry:
                    value = entry[kind]
                    sets.append({"kind": kind,
                                 **{k: safe_name(value[k]) for k in ("family", "table", "name") if k in value},
                                 "elements": rule_value(value.get("elem", [])),
                                 "definition_sha256": digest({k: v for k, v in value.items() if k != "comment"})})
            if "chain" in entry:
                c = entry["chain"]
                chains.append({k: c[k] for k in ("family", "table", "name", "type", "hook", "policy", "prio") if k in c})
            if "rule" in entry:
                r = entry["rule"]
                # Match expressions are referenced by digest for Human inspection on host;
                # literal matches may contain arbitrary strings and are not echoed.
                rules.append({k: r[k] for k in ("family", "table", "chain", "handle") if k in r} | {
                    "expression_sha256": digest([x for x in r.get("expr", []) if "counter" not in x]),
                    "matches": [{"left": rule_value(x["match"].get("left")),
                                 "op": x["match"].get("op") if x["match"].get("op") in ("==", "!=", "in") else "OTHER",
                                 "right": rule_value(x["match"].get("right"))}
                                for x in r.get("expr", []) if "match" in x],
                    "actions": sorted({k for x in r.get("expr", []) for k in x
                                       if k in ("accept", "drop", "reject", "jump", "goto", "masquerade", "snat", "dnat")})})
        # Counters are excluded from this stable review digest.
        result["nft"].update(chains=chains, rules=rules, sets=sets)
    for tool in ("iptables-save", "ip6tables-save"):
        raw = reader.command([tool], optional=True)
        policies, rules = [], []
        for line in (raw or "").splitlines():
            if line.startswith(":"):
                cols = line.split()
                policies.append({"chain": safe_name(cols[0][1:]), "policy": safe_name(cols[1])})
            elif line.startswith("-A "):
                tokens = shlex.split(line)
                matches = {}
                for index, token in enumerate(tokens[:-1]):
                    if token in ("-p", "-s", "-d", "-i", "-o", "--dport", "--sport", "--ctstate"):
                        matches[token] = rule_value(tokens[index+1])
                # Do not display raw rule/comments/addresses outside an explicit reviewed schema.
                rules.append({"sha256": hashlib.sha256(line.encode()).hexdigest(),
                              "chain": safe_name(line.split()[1]),
                              "matches": matches,
                              "terminal": next((x for x in ("ACCEPT", "DROP", "REJECT", "MASQUERADE")
                                                if "-j " + x in line), "OTHER")})
        result[tool] = {"available": raw is not None, "policies": policies, "rules": rules}
    result["meaning"] = "bridge/NAT rules do not prove destination or HTTP method enforcement"
    return result


INSPECT = "{" + ",".join([
    '"id":{{json .Id}}', '"name":{{json .Name}}', '"image":{{json .Image}}',
    '"state":{{json .State}}',
    '"restart_count":{{json .RestartCount}}',
    '"network_mode":{{json .HostConfig.NetworkMode}}',
    '"networks":{{json .NetworkSettings.Networks}}',
    '"restart_policy":{{json .HostConfig.RestartPolicy}}',
    '"entrypoint":{{json .Config.Entrypoint}}', '"cmd":{{json .Config.Cmd}}',
]) + "}"


def collect(decisions=None, *, include_public_ip=False, reader=None, getter=get_json):
    reader = reader or Reader()
    now = time.time()
    evidence = {"stage": "00", "host": socket.gethostname(), "observed_at": now,
                "mutations": 0, "technocore_requests": {"config_get": 1, "poll_get": 0},
                "pilot": POLICY, "blockers": [], "human_gates": []}
    blocks = evidence["blockers"]
    if evidence["host"] != "node-01": blocks.append("WRONG_HOST")
    evidence["docker_version"] = decoded(reader.command(["docker", "version", "--format", "{{json .}}"]), {})
    # Only selected version data is retained (Client context/plugin details are not emitted).
    evidence["docker_version"] = {
        k: {f: v.get(f) for f in ("Version", "ApiVersion", "Os", "Arch")}
        for k, v in evidence["docker_version"].items() if k in ("Client", "Server") and isinstance(v, dict)}
    infofmt = '{"cgroup_version":{{json .CgroupVersion}},"cgroup_driver":{{json .CgroupDriver}}}'
    evidence["docker_cgroup"] = decoded(reader.command(["docker", "info", "--format", infofmt]), {})
    if evidence["docker_cgroup"].get("cgroup_version") != "2": blocks.append("CGROUP_V2_PROBE_REQUIRED")
    container_ids = (reader.command(["docker", "ps", "-aq", "--no-trunc"]) or "").split()
    supervisor = supervisor_health(reader)
    evidence["observer_supervisor"] = supervisor
    containers = []
    for cid in container_ids:
        x = decoded(reader.command(["docker", "inspect", "--format", INSPECT, cid]), {})
        if not x: continue
        # Health is optional in Docker inspect. Select fields in Python so a
        # missing map key cannot abort the entire Go template. Never emit Error
        # or Health.Log (which may include arbitrary process output).
        state = x.pop("state", None)
        if not isinstance(state, dict):
            blocks.append("CONTAINER_INSPECT_SCHEMA_UNAVAILABLE")
            continue
        for out, field in (("running", "Running"), ("status", "Status"),
                           ("pid", "Pid"), ("oom", "OOMKilled")):
            x[out] = state.get(field)
        health = state.get("Health")
        x["health"] = health.get("Status") if isinstance(health, dict) else None
        # argv is used only to classify, not printed or executed.
        tags = launch_class(json.dumps([x.get("name"), x.get("entrypoint"), x.get("cmd")]))
        x.pop("cmd", None); x.pop("entrypoint", None)
        x["name"] = x.get("name", "").lstrip("/")
        if x["name"] in PRODUCTION_CONTAINERS:
            blocks.append("PRODUCTION_CONTAINER_NAME_COLLISION:" + x["name"])
        x["class"] = tags
        x["networks"] = {k: {f: v.get(f) for f in ("NetworkID", "IPAddress", "GlobalIPv6Address", "Gateway")}
                         for k, v in (x.get("networks") or {}).items()}
        if x["name"] in OBSERVERS:
            logs = reader.command(["docker", "logs", "--timestamps", "--since", "10m", "--tail", "1000", cid], optional=True)
            x["container_log_freshness"] = freshness(logs or "", now)
            x["freshness"] = supervisor["freshness"].copy()
            if not x.get("running") or x["freshness"]["verdict"] != "PASS":
                blocks.append("OBSERVER_HEALTH_OR_FRESHNESS:" + x["name"])
        elif "capture" in tags and (x.get("running") or x.get("restart_policy", {}).get("Name") not in ("no", "")):
            blocks.append("LEGACY_CAPTURE_RUNNING_OR_RELAUNCHABLE")
        containers.append(x)
    if not set(OBSERVERS).issubset({x["name"] for x in containers}): blocks.append("OBSERVER_INVENTORY_MISSING")
    evidence["containers"] = containers
    stats = reader.command(["docker", "stats", "--no-stream", "--format", "{{json .}}", *OBSERVERS])
    evidence["observer_resource_usage"] = []
    for line in (stats or "").splitlines():
        x = decoded(line, {})
        evidence["observer_resource_usage"].append({k: x.get(k) for k in
            ("Name", "CPUPerc", "MemUsage", "MemPerc", "PIDs", "BlockIO", "NetIO")})
    networks = []
    for nid in (reader.command(["docker", "network", "ls", "-q", "--no-trunc"]) or "").split():
        items = decoded(reader.command(["docker", "network", "inspect", nid]), [])
        for n in items:
            options = n.get("Options") or {}
            networks.append({"name": n["Name"], "id": n["Id"], "driver": n["Driver"],
                             "internal": n["Internal"], "ipam": n.get("IPAM", {}).get("Config", []),
                             "bridge_name": options.get("com.docker.network.bridge.name"),
                             "masquerade": options.get("com.docker.network.bridge.enable_ip_masquerade"),
                             "enforcement": "UNPROVEN_FROM_NETWORK_METADATA"})
    evidence["networks"] = networks
    evidence["firewall"] = firewall_evidence(reader)
    evidence["firewall_evidence_sha256"] = digest(evidence["firewall"])
    evidence["processes"] = process_inventory()
    if any("capture" in p["class"] for p in evidence["processes"]["candidates"]):
        blocks.append("CAPTURE_PROCESS_CANDIDATE_REQUIRES_REVIEW")
    evidence["launch_routes"] = launch_inventory(reader)
    for u in evidence["launch_routes"]["units"]:
        if "capture" in launch_class(u.get("Id", "")) and "storage-guard" not in u.get("Id", ""):
            if u.get("ActiveState") == "active" or u.get("UnitFileState") in ("enabled", "enabled-runtime"):
                blocks.append("LEGACY_CAPTURE_LAUNCH_ENABLED_OR_ACTIVE")
    for suffix in ("timer", "service"):
        u = unit_state(reader, "technocore-fullcap-storage-guard." + suffix)
        evidence["storage_guard_" + suffix] = u
        if u.get("LoadState") != "loaded" or u.get("ActiveState") != "inactive":
            blocks.append("STORAGE_GUARD_NOT_INACTIVE")
        if suffix == "timer" and u.get("UnitFileState") != "disabled": blocks.append("STORAGE_GUARD_TIMER_NOT_DISABLED")
    raw = reader.command(["findmnt", "--json", "--mountpoint", "/srv/technocore-data",
                          "--output", "SOURCE,FSTYPE,TARGET,OPTIONS"])
    fs = decoded(raw, {}).get("filesystems", [])
    evidence["data_mount"] = fs
    if len(fs) != 1 or fs[0].get("source") != "/dev/sdb" or fs[0].get("fstype") != "ext4":
        blocks.append("DATA_MOUNT_CHANGED")
    disks = {}
    for path in ("/", "/srv/technocore-data"):
        try:
            v = os.statvfs(path)
            disks[path] = {"total": v.f_blocks*v.f_frsize, "available": v.f_bavail*v.f_frsize,
                           "inodes": v.f_files, "free_inodes": v.f_ffree, "available_inodes": v.f_favail}
        except OSError:
            blocks.append("DISK_STAT_UNAVAILABLE")
    evidence["disks"] = disks
    if disks.get("/srv/technocore-data", {}).get("available", 0) < 20736*1024**2:
        blocks.append("PARENT_CAPACITY")
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            mem[k] = int(v.split()[0])*1024
    evidence["host_resources"] = {"cpus": os.cpu_count(), "load": list(os.getloadavg()), "memory": mem}
    if mem.get("MemAvailable", 0) < (1024+256+384)*1024**2: blocks.append("HOST_MEMORY_HEADROOM")
    loops = decoded(reader.command(["losetup", "--list", "--json", "--output", "NAME,BACK-FILE"]), {})
    used = [x["name"] for x in loops.get("loopdevices", []) if x.get("name") in ("/dev/loop40", "/dev/loop41", "/dev/loop42")]
    evidence["loop40_42"] = {"used": used,
                              "all_unused": (not used) if "loopdevices" in loops else None}
    if used: blocks.append("FIXED_LOOP_IN_USE")
    try:
        evidence["technocore_config"] = public_config(getter("https://technocore.chat/config"))
        c = evidence["technocore_config"]
        if type(c.get("read_limit")) is not int or c["read_limit"] < 600: blocks.append("READ_LIMIT_NOT_CONFIRMED")
        if type(c.get("max_waiters")) is not int or c["max_waiters"] < 4: blocks.append("WAITER_LIMIT_NOT_CONFIRMED")
    except Exception:
        evidence["technocore_config"] = {"error": "CONFIG_GET_OR_SCHEMA_FAILED"}
        blocks.append("CONFIG_GET_OR_SCHEMA_FAILED")
    evidence["public_egress_ip"] = {"status": "NOT_REQUESTED", "scope": "host only; container/NAT equivalence not inferred"}
    if include_public_ip:
        try:
            ip = str(ipaddress.ip_address(getter("https://api.ipify.org?format=json")["ip"]))
            evidence["public_egress_ip"].update(status="OBSERVED", ip=ip)
        except Exception:
            evidence["public_egress_ip"]["status"] = "UNAVAILABLE"
    evidence["known_client_candidates"] = {
        "containers": [x["name"] for x in containers if x["class"]],
        "process_pids": [x["pid"] for x in evidence["processes"]["candidates"]],
        "scope": "candidates only; off-host/shared-NAT clients require Human inventory"}
    if evidence["processes"]["permission_denied"] or evidence["launch_routes"]["permission_denied"]:
        blocks.append("INVENTORY_VISIBILITY_INCOMPLETE")
    evidence["read_errors"] = reader.errors
    if any(not x["optional"] for x in reader.errors): blocks.append("REQUIRED_READ_UNAVAILABLE")
    evidence["human_gates"] = validate_decisions(decisions or {}, evidence)
    # Unknown keys and reference text could contain a pasted credential: never echo them.
    approved = decisions or {}
    original = approved.get("egress", {})
    evidence["human_attestation"] = {
        "clients": approved.get("clients", []) if not evidence["human_gates"] else [],
        "launch_inventory_reviewed": approved.get("launch_inventory_reviewed") is True,
        "egress": {"network": original.get("network") if not evidence["human_gates"] else None,
                   "network_id": original.get("network_id") if not evidence["human_gates"] else None,
                   "classification": original.get("classification") if not evidence["human_gates"] else "unconfirmed",
                   "same_egress_inventory_complete": original.get("same_egress_inventory_complete") is True,
                   "firewall_evidence_sha256": evidence["firewall_evidence_sha256"],
                   "evidence_reference_sha256": digest(original.get("evidence_reference"))},
    }
    if not evidence["human_gates"] and original.get("classification") in (PILOT_CLASSIFICATION, STANDING_CLASSIFICATION):
        attestation = evidence["human_attestation"]["egress"]
        attestation.update(STANDING_ACCEPTANCE if original["classification"] == STANDING_CLASSIFICATION else PILOT_ACCEPTANCE)
        attestation["network_id"] = next(n["id"] for n in networks if n["name"] == "bridge")
        # The hash binds a snapshot for later drift checks, not proof of egress
        # restriction or of HTTP method enforcement by the network.
        attestation["firewall_snapshot_meaning"] = "drift_detection_only_not_enforcement_proof"
    evidence["verdict"] = "PASS" if not blocks and not evidence["human_gates"] else "REVIEW_REQUIRED"
    evidence["production_start_authorized_by_this_report"] = False
    return evidence
