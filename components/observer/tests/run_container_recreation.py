"""One-command Docker-local recreation test; no build/pull/registry/live traffic.

Creates a derived fixture image using export/import of the already-local base.
The export container never starts. No host directory is mounted or extracted.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile
import time
import uuid

import run_durability as h

MAX_ARCHIVE = 512 * 1024 * 1024


def add_fixture(archive_path):
    """Append reviewed public code and an owned /state to a base rootfs archive."""
    h.require(archive_path.stat().st_size <= MAX_ARCHIVE, "BASE_ARCHIVE_SIZE_LIMIT")
    with tarfile.open(archive_path, "a") as archive:
        # Reject collisions and never extract base-image paths to the host.
        for member in archive.getmembers():
            name = member.name.removeprefix("./").rstrip("/")
            h.require(not name.startswith("/") and ".." not in name.split("/"), "BASE_ARCHIVE_PATH")
            h.require(name not in ("app", "state") and not name.startswith(("app/", "state/")), "BASE_IMAGE_COLLISION")
        for name in ("app", "app/src", "app/src/technocore_observer", "app/tests", "state"):
            entry = tarfile.TarInfo(name)
            entry.type = tarfile.DIRTYPE
            entry.mode = 0o700 if name == "state" else 0o755
            entry.uid = entry.gid = 65532 if name == "state" else 0
            archive.addfile(entry)
        sources = sorted((h.ROOT / "src" / "technocore_observer").glob("*.py")) + [h.PAYLOAD]
        for path in sources:
            h.require(path.is_file() and not path.is_symlink(), "SOURCE_PATH_REJECTED")
            data = path.read_bytes()
            entry = tarfile.TarInfo("app/" + str(path.relative_to(h.ROOT)))
            entry.mode = 0o444
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
        # Nonempty image directory makes Docker's initial volume copy explicit.
        data = b"public offline durability fixture; no code or secrets\n"
        entry = tarfile.TarInfo("state/.durability-volume")
        entry.uid = entry.gid = 65532
        entry.mode = 0o600
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))


class RecreationBackend(h.DockerBackend):
    validation_profile = h.LOCAL_PROFILE

    def __init__(self, directory, identity):
        super().__init__(directory, None, identity)
        self.preparation_name = "observer-durability-base-" + identity
        self.image_tag = "observer-durability-local:" + identity
        self.preparation_id = None
        self.all_containers = []

    def prepare_local_image(self):
        base = json.loads(self.docker(["image", "inspect", h.BASE]))[0]
        h.require(base["Id"] == h.BASE and base.get("Os") == "linux"
                  and not base["Config"].get("Volumes"), "LOCAL_BASE_REJECTED")
        # No image resolution command, builder, pull, registry, or runtime root.
        args = ["create", "--name=" + self.preparation_name, "--pull=never", "--network=none",
                "--user=65532:65532", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true", "--memory=256m", "--pids-limit=32",
                "--restart=no", "--log-driver=none", "--label=" + h.PURPOSE + "=" + self.identity,
                "--entrypoint=python3", h.BASE, "-I", "-B", "-c", "raise SystemExit(0)"]
        self.preparation_id = self.docker(args).decode().strip()
        config = json.loads(self.docker(["container", "inspect", self.preparation_name]))[0]
        h.require(config["Id"] == self.preparation_id and config["Image"] == h.BASE
                  and config["State"]["Status"] == "created" and not config.get("Mounts")
                  and config["HostConfig"]["NetworkMode"] == "none"
                  and config["HostConfig"]["ReadonlyRootfs"] is True
                  and config["Config"]["User"] == "65532:65532", "EXPORT_CONTAINER_REJECTED")
        self.all_containers.append({"name": self.preparation_name, "id": self.preparation_id, "role": "never-started-base-export"})
        self.log.append({"event": "base_export_created", "container": self.preparation_name, "id": self.preparation_id})
        archive = self.directory / "fixture-rootfs.tar"
        self.docker(["export", "--output=" + str(archive), self.preparation_name])
        archive.chmod(0o600)
        add_fixture(archive)
        result = self.docker(["image", "import", "--change=USER 65532:65532",
            "--change=WORKDIR /app", "--change=ENV HOME=/nonexistent PYTHONDONTWRITEBYTECODE=1 PATH=/usr/local/bin:/usr/bin:/bin",
            "--change=ENTRYPOINT " + json.dumps(self.entrypoint),
            "--change=LABEL " + h.PURPOSE + "=" + self.identity, str(archive), self.image_tag])
        self.image = result.decode().strip()
        h.require(re.fullmatch(r"sha256:[0-9a-f]{64}", self.image), "IMPORTED_IMAGE_ID_REQUIRED")
        self.log.append({"event": "local_image_imported", "base_image": h.BASE,
                         "image_id": self.image, "tag": self.image_tag, "external_requests": 0})
        self.preserve("image-prepared")
        # Preparation container is retained; no cleanup before report persistence.

    def prepare(self):
        self.prepare_local_image()
        existing = self.docker(["volume", "ls", "--format", "{{.Name}}"])
        h.require(self.volume not in existing.decode().splitlines(), "FRESH_VOLUME_REQUIRED")
        super().prepare()
        self.preserve("fresh-volume-created")

    def verify(self, name):
        super().verify(name)
        config = json.loads(self.docker(["container", "inspect", name]))[0]
        found = [c for c in self.all_containers if c["name"] == name]
        if found:
            h.require(found[0]["id"] == config["Id"], "CONTAINER_ID_CHANGED")
        else:
            self.all_containers.append({"name": name, "id": config["Id"], "role": "fixture"})

    def preserve(self, stage):
        h.private_write(self.directory / (stage + ".json"), json.dumps({
            "events": self.log, "volume": self.volume_fingerprint, "volume_name": self.volume,
            "image_id": self.image, "containers": self.all_containers,
            "external_requests": 0}, ensure_ascii=True, indent=2).encode())

    def cleanup(self, success):
        # Stop only. B, base-export container, image and volume remain identifiable.
        super().cleanup(False)


def recreation(matrix):
    case = "named-volume-recreation"
    first, before = matrix.seed(case)
    first.finish()
    # Docker backend saves all A observations before the required removal of A.
    second = matrix.backend.recreate(first)
    after = matrix.reopen(second, case)
    h.same(before, after)
    h.expected(after, 201, 103, "DEGRADED", 5, [(104, 199)])
    result = second.rpc(case, "poll", fixture="resume")
    h.require(result["first_since"] == 201, "RECREATION_RESUME_SINCE")
    h.expected(result["summary"], 203, 103, "DEGRADED", 7, [(104, 199)])
    h.preserved(before, result["summary"])
    matrix.backup(second, case, result["summary"])
    second.finish()
    if isinstance(matrix.backend, RecreationBackend):
        matrix.backend.verify(second.name)
    matrix.mark(case)


def local_results(events, cases):
    """Aggregate actual A/B observations; a policy replay alone is not durability proof."""
    probes = [event for event in events if "preflight" in event]
    policies = [h.preflight_policy(event["preflight"], h.LOCAL_PROFILE) for event in probes]
    configs = [event for event in events if event.get("event") == "container_configuration_checked"]
    verified = {event["container"] for event in events if event.get("event") == "container_verified"}
    ready = {event["container"] for event in events if event.get("event") == "process_ready"}
    probed = {event["container"] for event in probes}
    failed = (any(not policy["accepted"] or event.get("preflight_policy") != policy
                  or event.get("host_preflight_policy") != policy for event, policy in zip(probes, policies))
              or any(not event.get("checks") or not all(value is True for value in event["checks"].values()) for event in configs))
    isolation = "FAIL" if failed else "PASS" if len(probed & verified & ready) >= 2 else "PENDING"
    complete = {"case": "named-volume-recreation", "status": "PASS"} in cases
    result = {"validation_profile": h.LOCAL_PROFILE, "isolation_boundary": isolation,
              "persistent_storage_durability": "PASS" if complete and isolation == "PASS" else "PENDING",
              "production_hardening_gate": "PENDING"}
    for flag in ("noexec", "nosuid", "nodev"):
        key = "mount_hardening_" + flag
        values = [policy[key] for policy in policies]
        result[key] = "WARN" if "WARN" in values else "PASS" if values and all(v == "PASS" for v in values) else "PENDING"
    result["hardening_warning_checks"] = sorted({key for policy in policies for key in policy["hardening_warnings"]})
    return result


def success_status(result):
    h.require(result["isolation_boundary"] == result["persistent_storage_durability"] == "PASS",
              "LOCAL_DURABILITY_EVIDENCE_INCOMPLETE")
    return "LOCAL_DURABILITY_PASS_WITH_HARDENING_WARNINGS" if result["hardening_warning_checks"] else "LOCAL_DURABILITY_PASS"


def execute():
    os.umask(0o077)
    identity = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    directory = h.ROOT / "docs" / ("durability-results-recreation-" + identity)
    print(json.dumps({"create_path": str(directory)}), flush=True)
    directory.mkdir(mode=0o700)
    backend = RecreationBackend(directory, identity)
    matrix = h.Matrix(backend)
    sources = h.manifest()
    sources.update({"tests/" + p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (Path(__file__), h.ROOT / "tests" / "test_container_recreation.py")})
    h.private_write(directory / "source-hashes.json", json.dumps(sources, indent=2).encode())
    report = {"status": "INCOMPLETE", "scope": "offline named-volume recreation only",
              "observer_spec": "0.3", "technocore_baseline": "0.12.1",
              "external_requests": 0, "containers": backend.all_containers, "events": backend.log,
              "volume_name": backend.volume, "cases": matrix.cases, "full_durability_gate": "PENDING"}
    report.update(local_results(backend.log, matrix.cases))
    started = time.monotonic()
    try:
        backend.prepare()
        recreation(matrix)
        h.require(all(hashlib.sha256((h.ROOT / name).read_bytes()).hexdigest() == value
                      for name, value in sources.items()), "SOURCE_CHANGED_DURING_SUITE")
        report.update(local_results(backend.log, matrix.cases))
        report["status"] = success_status(report)
    except Exception as exc:
        report["status"] = "STOPPED"
        report["error_class"] = type(exc).__name__
        if type(exc) is RuntimeError and re.fullmatch(r"[A-Z_]+", str(exc)):
            report["error_code"] = str(exc)
    finally:
        report.update(local_results(backend.log, matrix.cases))
        report.update({"image_id": backend.image, "image_tag": backend.image_tag,
                       "volume": backend.volume_fingerprint})
        # If evidence cannot be saved, do not delete any resource.
        h.private_write(directory / "before-cleanup-report.json", json.dumps(report, indent=2).encode())
        try:
            backend.cleanup(False)
        except Exception as exc:
            report["status"] = "STOPPED"
            report["cleanup_error_class"] = type(exc).__name__
            if type(exc) is RuntimeError and re.fullmatch(r"[A-Z_]+", str(exc)):
                report["cleanup_error_code"] = str(exc)
        report.update(local_results(backend.log, matrix.cases))
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["retention_policy"] = "retain volume/image/stopped containers; remove A only after evidence saved"
        report["remaining_container_names"] = list(backend.names) + (
            [backend.preparation_name] if backend.preparation_id else [])
        h.private_write(directory / "report.json", json.dumps(report, ensure_ascii=True, indent=2).encode())
    print(json.dumps({"status": report["status"], "external_requests": 0, "artifacts": str(directory)}), flush=True)
    return 0 if report["status"] in ("LOCAL_DURABILITY_PASS", "LOCAL_DURABILITY_PASS_WITH_HARDENING_WARNINGS") else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.execute:
        return execute()
    print(json.dumps({"command": "python3 -B tests/run_container_recreation.py --execute",
        "external_requests": 0, "base": h.BASE, "image_preparation": "local export/import; no build/pull",
        "network": "none", "container_user": "65532:65532", "persistent_mount": "fresh local named volume at /state",
        "validation_profile": h.LOCAL_PROFILE,
        "mount_gate": "rw and all core checks required; noexec/nosuid/nodev absence recorded as local hardening warnings",
        "production_hardening_gate": "PENDING", "max_seconds": 900,
        "cleanup": "A removed after evidence saved; B, image and volume retained"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
