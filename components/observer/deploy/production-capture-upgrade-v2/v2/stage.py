"""One-shot isolated STAGE: only new Attempt files, image, probe, stopped containers."""
import importlib.util
import json
import os
import stat
from pathlib import Path
import re
import sys
import time

from .artifact import copy_reviewed, verify_checkout, verify_source
from .common import (CANDIDATE, LABELS, ROLES, V2Error, command, container_id,
                     docker_inspect, read_json, require, write_json_once)
from .preflight import network_check, policy_check, snapshot
from .stop import identity

IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


def candidate_packet(source_root):
    helper = Path(source_root) / "deploy/production-capture-human"
    sys.path.insert(0, str(helper))
    spec = importlib.util.spec_from_file_location("v2_candidate_packet", helper / "packet.py")
    require(spec is not None and spec.loader is not None, "CANDIDATE_PACKET_IMPORT")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_argv(packet, *, attempt, release, role, network, image):
    require(role in ROLES, "ROLE")
    name = f"tc-cap-v2-{role}-{attempt}"
    labels = [item for key, value in (("attempt", attempt), ("role", role),
                                       ("candidate", CANDIDATE), ("release", release))
              for item in ("--label", LABELS[key] + "=" + value)]
    return (["docker", "create", "--name", name, *labels,
             *packet.profile(role, network if role == "capture" else "none"),
             *packet.prod_mounts(role), "--entrypoint", "python3", image,
             "-I", "-B", "-m", "technocore_full_capture.production", "run",
             "--config", "/policy.json", "--room", "lobby", "--role", role])


def verify_writer(packet, row, *, cid, image, attempt, role, release, network):
    require(row is not None and identity(row, cid=cid, image=image, attempt=attempt,
                                         role=role, release=release), "WRITER_IDENTITY")
    require(row.get("State", {}).get("Running") is False and row.get("State", {}).get("Status") == "created",
            "WRITER_NOT_CREATED_STOPPED")
    h = row.get("HostConfig", {})
    normalized = {"id": cid, "image": row.get("Image"),
                  "user": row.get("Config", {}).get("User"),
                  "entrypoint": row.get("Config", {}).get("Entrypoint"),
                  "cmd": row.get("Config", {}).get("Cmd"),
                  "mounts": [{key: mount.get(key) for key in ("Source", "Destination", "RW", "Type")}
                             for mount in row.get("Mounts", [])],
                  "limits": h, "running": False, "pid": 0,
                  "status": "created", "exit_code": row.get("State", {}).get("ExitCode")}
    # Reviewed Candidate check is pure validation; no V1 stage/lifecycle authority.
    try:
        packet.check_container(normalized, role, image, network)
    except (ValueError, KeyError, TypeError) as exc:
        raise V2Error("WRITER_CANDIDATE_CONTRACT") from exc


def verify_probe(row, *, cid, image):
    require(row is not None and row.get("Id") == cid and row.get("Image") == image,
            "PROBE_IDENTITY")
    h, config, state = row.get("HostConfig", {}), row.get("Config", {}), row.get("State", {})
    require(state.get("Running") is False and state.get("Status") == "created" and
            h.get("NetworkMode") == "none" and h.get("ReadonlyRootfs") is True and
            h.get("Privileged") is False and h.get("CapDrop") == ["ALL"] and
            not h.get("CapAdd") and "no-new-privileges" in (h.get("SecurityOpt") or []) and
            not h.get("Mounts") and not row.get("Mounts") and
            h.get("RestartPolicy", {}).get("Name") == "no" and
            config.get("User") == "65532:65532" and config.get("Entrypoint") == ["python3"] and
            config.get("Cmd") == ["-I", "-B", "-m", "technocore_full_capture", "--help"],
            "PROBE_CONTRACT")


def _baseline_metrics(layout):
    registry = layout.shared_root / "control/registry"
    capture = read_json(registry / "lobby.capture.metrics.json")
    archive = read_json(registry / "lobby.archive.metrics.json")
    gaps = capture.get("producer", {}).get("gaps")
    messages = capture.get("producer", {}).get("messages")
    processed = archive.get("processed_through")
    require(all(type(x) in (int, float) and x >= 0 for x in (gaps, messages, processed)),
            "STAGE_METRICS_BASELINE")
    return {"gaps": gaps, "messages": messages, "archive_processed_through": processed}



def _private_namespace(path):
    path.mkdir(mode=0o700, exist_ok=True)
    row = path.lstat()
    require(stat.S_ISDIR(row.st_mode) and row.st_uid == os.geteuid() and
            stat.S_IMODE(row.st_mode) & 0o022 == 0, "V2_NAMESPACE_OWNERSHIP")


def stage(runner, layout, *, source_root, attempt, release, human_approved=False):
    require(human_approved, "HUMAN_STAGE_GATE")
    manifest = verify_source(source_root)
    verify_checkout(source_root, release, runner)
    pre = snapshot(runner, layout, source_root, attempt)
    require(stat.S_ISDIR(layout.release_base.parent.parent.lstat().st_mode),
            "V2_NAMESPACE_ANCESTOR")
    _private_namespace(layout.release_base.parent)
    _private_namespace(layout.release_base)
    _private_namespace(layout.state_base)
    state = layout.state(attempt)
    require(not state.exists(), "ATTEMPT_ALREADY_USED")
    state.mkdir(mode=0o700)
    progress = {"image_id": None, "probe_id": None, "container_ids": {}}
    write_json_once(state / "attempt.json", {"attempt_id": attempt,
        "release_commit": release, "candidate_commit": CANDIDATE,
        "manifest_sha256": __import__("hashlib").sha256(
            (Path(source_root) / "deploy/production-capture-upgrade-v2/release-manifest.json").read_bytes()).hexdigest(),
        "created_at": time.time()})
    try:
        release_dir = copy_reviewed(source_root, layout.release(attempt), manifest)
        source = release_dir / "source"
        # A pinned base must already be local; Docker must not fetch it during Stage.
        base = "python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
        command(runner, ["docker", "image", "inspect", base], "BASE_IMAGE_NOT_LOCAL")
        image = command(runner, ["docker", "build", "--quiet", "--pull=false", "--network=none",
                                  "-f", str(source / "deploy/production-capture-upgrade-v2/Containerfile.full-capture"),
                                  str(source)],
                        "IMAGE_BUILD_FAILED", timeout=900).strip()
        require(IMAGE_ID.fullmatch(image) is not None, "IMAGE_ID")
        progress["image_id"] = image
        probe_name = "tc-cap-v2-probe-" + attempt
        probe_id = command(runner, ["docker", "create", "--name", probe_name,
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--restart", "no",
            "--user", "65532:65532", "--entrypoint", "python3", image,
            "-I", "-B", "-m", "technocore_full_capture", "--help"],
            "PROBE_CREATE_FAILED").strip()
        container_id(probe_id)
        progress["probe_id"] = probe_id
        verify_probe(docker_inspect(runner, probe_id), cid=probe_id, image=image)
        command(runner, ["docker", "start", "--attach", probe_id], "PROBE_FAILED", timeout=120)
        after = docker_inspect(runner, probe_id)
        require(after is not None and after.get("State", {}).get("Running") is False and
                after.get("State", {}).get("ExitCode") == 0, "PROBE_EXIT")
        # Recheck the two shared inputs immediately before creating stopped writers.
        network = network_check(runner, layout)["network"]
        require(policy_check(layout.shared_etc / "policy.json", source) == pre["policy_sha256"],
                "POLICY_CHANGED_DURING_STAGE")
        packet = candidate_packet(source)
        for role in ROLES:
            cid = command(runner, create_argv(packet, attempt=attempt, release=release,
                role=role, network=network, image=image), "WRITER_CREATE_FAILED").strip()
            container_id(cid)
            progress["container_ids"][role] = cid
            verify_writer(packet, docker_inspect(runner, cid), cid=cid, image=image,
                          attempt=attempt, role=role, release=release, network=network)
        baseline = _baseline_metrics(layout)
        obs = layout.observation(attempt)
        require(not obs.exists(), "OBSERVATION_ATTEMPT_EXISTS")
        obs.mkdir(mode=0o700)
        staged = {"attempt_id": attempt, "release_commit": release,
                  "candidate_commit": CANDIDATE, **progress, "baseline": pre,
                  "metrics_baseline": baseline, "observation_path": str(obs)}
        write_json_once(state / "staged.json", staged)
        return staged
    except BaseException as exc:
        code = exc.code if isinstance(exc, V2Error) else "STAGE_UNEXPECTED"
        write_json_once(state / "terminal.json", {"outcome": "STAGE_FAILED",
            "primary_failure": code, "progress": progress, "shared_switch_mutation": False})
        raise
