"""Human packet data checks. No subprocess, network, filesystem mutations or imports of runtime."""

import hashlib
import json
import math
import re

SOURCE_COMMIT = "337001c261f530f90db6ec2c68fc417de76cc7a7"
OBSERVERS = (
    "technocore-observer-d-sonnet-2-results-tail-20260919",
    "technocore-observer-live",
)
ROOT = "/srv/technocore-capture"
IMAGES = "/srv/technocore-data/production-capture-volumes"
ETC = "/etc/technocore-capture"
NOTIFY_ETC = "/etc/technocore-capture-notify"
INSTALL = "/opt/technocore-capture-standing-20260920"
GIB = 1024**3
MIB = 1024**2
VOLUMES = {"spool": (40, 10*GIB), "archive": (41, 8*GIB), "control": (42, 256*MIB)}
POLICY = {
    "notification": "Capture STOP dedicated outbox/notifier",
    "monitor_owner": "kou",
    "unattended_failure": "GAPとして許容",
    "RTO": "次回Human確認時",
}
PILOT_CLASSIFICATION = "application_get_only_24h_pilot"
PILOT_ACCEPTANCE = {
    "host": "node-01", "production_id": "tc-cap-loop-01", "duration_seconds": 86400,
    "accepted_scope": "24h Limited Production Pilot only",
    "network_level_destination_restriction": "none_or_unproven",
    "get_only_enforcement": "reviewed_application_boundary",
    "shared_nat_risk_accepted": True, "external_launch_risk_accepted": True,
    "allocation_basis": "human_operational_reservation",
    "bind_observed_network_and_firewall": True,
}
PILOT_CLIENTS = [
    {"name": "observer-live", "rpm": 180, "waiters": 1},
    {"name": "observer-results-tail", "rpm": 180, "waiters": 1},
]
STANDING_CLASSIFICATION = "standing_application_get_only"
STANDING_ACCEPTANCE = {
    **{k: v for k, v in PILOT_ACCEPTANCE.items() if k not in ("duration_seconds", "accepted_scope")},
    "accepted_scope": "node-01 delegated capture workstream",
    "observation_checkpoint_seconds": 86400,
    "permission_expires_at_checkpoint": False,
}


def require(condition, code):
    if not condition:
        raise ValueError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def freshness(logs, now):
    """Never return raw log lines, exception strings, bodies, or unknown strings."""
    latest = None
    keys = ("last_valid_response_at", "last_poll_at", "poll_seq", "resolved_seq",
            "server_generation", "consecutive_failures", "open_gap_count", "disk_free_bytes")
    for line in logs.splitlines():
        # docker logs --timestamps emits RFC3339 then JSON.
        _, _, body = line.partition(" ")
        try:
            obj = json.loads(body)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict) or not numeric(obj.get("last_valid_response_at")):
            continue
        status = obj.get("effective_status", obj.get("status"))
        item = {k: obj[k] for k in keys if numeric(obj.get(k))}
        item["status"] = status if status in {
            "RUNNING", "INITIALIZED", "DEGRADED", "ERROR", "NEEDS_RESYNC", "STOPPED"
        } else "UNKNOWN"
        latest = item
    if latest is None:
        return {"verdict": "UNKNOWN", "reason": "NO_RECOGNIZED_HEARTBEAT_IN_BOUNDED_LOGS"}
    age = now - latest["last_valid_response_at"]
    latest["last_valid_response_age_seconds"] = age
    latest["verdict"] = "PASS" if (
        0 <= age <= 120 and latest["status"] in {"RUNNING", "INITIALIZED", "DEGRADED"}
    ) else "FAIL"
    latest["meaning"] = "accepted-response freshness; not a remote-tail completeness assertion"
    return latest


def public_config(obj):
    """Allowlist public protocol fields; no arbitrary response values escape."""
    settings = obj.get("settings", obj)
    require(isinstance(settings, dict), "CONFIG_SETTINGS")
    keys = ("rate_read", "reads_per_minute_per_ip", "max_waiters_per_ip", "max_waiters_total",
            "rate_write", "max_wait", "room_ring_bytes", "retention_seconds",
            "ephemeral_ttl_seconds", "fsync")
    safe = {k: settings[k] for k in keys if type(settings.get(k)) in (int, bool)}
    version = obj.get("version")
    safe["version"] = version if isinstance(version, str) and re.fullmatch(r"[0-9.]{1,32}", version) else "UNKNOWN"
    safe["read_limit"] = safe.get("rate_read", safe.get("reads_per_minute_per_ip"))
    safe["max_waiters"] = safe.get("max_waiters_per_ip")
    safe["response_sha256"] = digest(obj)
    safe["scope"] = "current /config, known public fields only; unknown fields not printed"
    return safe


def validate_decisions(decisions, evidence):
    """Human attestation stays explicitly distinct from collected measurements."""
    issues = []
    e = decisions.get("egress", {})
    nets = {n["name"]: n for n in evidence.get("networks", [])}
    network = e.get("network")
    n = nets.get(network)
    pilot = e.get("classification") == PILOT_CLASSIFICATION
    standing = e.get("classification") == STANDING_CLASSIFICATION
    acceptance = STANDING_ACCEPTANCE if standing else PILOT_ACCEPTANCE
    application_boundary = pilot or standing
    if application_boundary and (any(type(e.get(k)) is not type(v) or e.get(k) != v
                      for k, v in acceptance.items())
                  or evidence.get("host") != "node-01" or network != "bridge"
                  or e.get("same_egress_inventory_complete") is not False):
        issues.append("PILOT_ACCEPTANCE_REQUIRED_OR_INVALID")
    if not n or n.get("driver") != "bridge" or network in ("host", "none") or (network == "bridge" and not application_boundary):
        issues.append("HUMAN_SELECT_EXISTING_NETWORK")
    if not application_boundary and e.get("classification") != "technocore_get_only_boundary":
        issues.append("EGRESS_BOUNDARY_NOT_PROVEN_NO_POLICY_ADDED")
    if not (isinstance(e.get("evidence_reference"), str) and e["evidence_reference"].strip()):
        issues.append("EGRESS_POLICY_EVIDENCE_REFERENCE_REQUIRED")
    # This pilot explicitly binds the chosen existing bridge to the fresh read.
    # Explicit supplied IDs/hashes must still match; null means bind observed.
    if n and (not n.get("id") or (e.get("network_id") != n.get("id")
                                and not (application_boundary and e.get("network_id") is None))):
        issues.append("EGRESS_NETWORK_ID_MISMATCH")
    if (e.get("firewall_evidence_sha256") != digest(evidence.get("firewall", {}))
            and not (application_boundary and e.get("firewall_evidence_sha256") is None)):
        issues.append("EGRESS_FIREWALL_REVIEW_NOT_BOUND")
    if not application_boundary and e.get("same_egress_inventory_complete") is not True:
        issues.append("SAME_EGRESS_INVENTORY_INCOMPLETE")
    if decisions.get("launch_inventory_reviewed") is not True:
        issues.append("LAUNCH_INVENTORY_REVIEW_REQUIRED")
    clients = decisions.get("clients", [])
    try:
        require(isinstance(clients, list) and clients, "CLIENTS")
        names = set()
        for client in clients:
            require(set(client) == {"name", "rpm", "waiters"}, "CLIENT_FIELDS")
            require(isinstance(client["name"], str) and re.fullmatch(
                r"[a-z0-9][a-z0-9_-]{0,47}", client["name"]), "CLIENT_NAME")
            require(numeric(client["rpm"]) and 0 <= client["rpm"] <= 360, "CLIENT_RPM")
            require(type(client["waiters"]) is int and 0 <= client["waiters"] <= 3, "CLIENT_WAITERS")
            require(client["name"] not in names, "CLIENT_DUPLICATE")
            names.add(client["name"])
        require(sum(x["rpm"] for x in clients) <= 360, "CLIENT_TOTAL_RPM")
        require(sum(x["waiters"] for x in clients) <= 3, "CLIENT_TOTAL_WAITERS")
        if application_boundary:
            require(sorted(clients, key=lambda x: x["name"]) == PILOT_CLIENTS,
                    "PILOT_CLIENT_RESERVATIONS")
    except (ValueError, TypeError, KeyError):
        issues.append("CLIENT_ALLOCATION_REQUIRED_OR_INVALID")
    return issues


def decision_template():
    return {
        "egress": {"network": None, "network_id": None,
                   "classification": "unconfirmed",
                   "evidence_reference": None, "firewall_evidence_sha256": None,
                   "same_egress_inventory_complete": False},
        "clients": [], "launch_inventory_reviewed": False,
    }


def draft(clients, *, continuous=False):
    return {
        "version": 2 if continuous else 1, "production_id": "tc-cap-loop-01",
        **({"observation_checkpoint_seconds": 86400} if continuous else {}),
        "control_dir": ROOT + "/control/registry", "budget_dir": ROOT + "/control/budget",
        "start_at": None, "end_at": None, "deadline": 30, "deletion_enabled": False,
        "budget": {"read_limit": 600, "observer_reserve": 360, "headroom": 120,
                   "capture_rpm": 120, "max_waiters": 4},
        "clients": clients,
        "rooms": [{"room": "lobby", "class": "high", "interval": 1, "max_rpm": 108,
                   "spool_dir": ROOT + "/spool/data", "archive_dir": ROOT + "/archive/data",
                   "planned_messages_sec": 20,
                   "storage": {"spool_volume_bytes": 10*GIB, "archive_volume_bytes": 8*GIB,
                               "db_bytes": 8*GIB, "wal_bytes": 128*MIB,
                               "reserve_bytes": 512*MIB, "min_inodes": 4096,
                               "spool_bytes_message": 2048, "archive_bytes_message": 1536}}],
    }
