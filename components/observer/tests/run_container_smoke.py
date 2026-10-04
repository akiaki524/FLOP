"""Plan or execute isolated Observer offline + bounded live smoke, without pulls.

Run from the normal Ubuntu terminal that can reach Docker Desktop. The Codex
session's namespace currently cannot reach that socket. No sudo fallback exists.
"""

import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import time
import uuid
import zipfile

import container_isolation as isolation

ROOT = Path(__file__).resolve().parents[1]
BASE_IMAGE = "sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]
ROOM = "mb-047f3d88ef38"
EMPTY_ROOM = "observer-smoke-20260906-9bc137a4"
IDLE_CODE = "import time; time.sleep(600)"
EXEC_CODE = "import sys; sys.path.insert(0, '/state/observer.zip'); import live_smoke; raise SystemExit(live_smoke.main())"
EXPORT_CODE = "import sys; sys.path.insert(0, '/state/observer.zip'); import live_smoke; live_smoke.export_artifacts()"
IMPORT_CODE = """import os, sys
data = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
if len(data) > 4 * 1024 * 1024:
    raise SystemExit(1)
fd = os.open('/state/observer.zip', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o400)
with os.fdopen(fd, 'wb') as output:
    output.write(data)
"""


def container_command(image_id, name, mode):
    args = isolation.create_command(image_id, name)
    args[args.index("--network=none")] = "--network=" + ("none" if mode == "offline" else "bridge")
    args[args.index("--interactive")] = "--init"
    args[-3:] = ["-I", "-B", "-c", IDLE_CODE]
    return args


def inspect_checks(config, image_id, mode):
    # Keep the proven baseline inspector, with two explicit changes: a bounded
    # idle entrypoint allows copy/exec; live has a private bridge, not host mode.
    checks = isolation.configuration_checks(config, image_id)
    checks.pop("network_none")
    checks["expected_network"] = config["HostConfig"]["NetworkMode"] == ("none" if mode == "offline" else "bridge")
    checks["expected_entrypoint"] = (config["Config"].get("Entrypoint") == ["python3"]
                                     and config["Config"].get("Cmd") == ["-I", "-B", "-c", IDLE_CODE])
    checks["init_enabled"] = config["HostConfig"].get("Init") is True
    return checks


def source_files():
    files = []
    package = ROOT / "src" / "technocore_observer"
    for path in sorted(package.iterdir()):
        if path.suffix == ".py":
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("SOURCE_PATH_REJECTED")
            files.append((path, "technocore_observer/" + path.name))
    smoke = ROOT / "tests" / "live_smoke.py"
    if smoke.is_symlink() or not smoke.is_file():
        raise RuntimeError("SOURCE_PATH_REJECTED")
    files.append((smoke, "live_smoke.py"))
    return files


def make_archive(path):
    hashes = {}
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, name in source_files():
            if source.is_symlink() or not source.is_file():
                raise RuntimeError("SOURCE_PATH_REJECTED")
            data = source.read_bytes()
            ast.parse(data, filename=name)
            entry = zipfile.ZipInfo(name)
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
            hashes[name] = hashlib.sha256(data).hexdigest()
    path.chmod(0o644)  # Public source only; readable by the container's nonroot UID.
    return hashes


def save_artifacts(data, destination):
    if len(data) > 70 * 1024 * 1024:
        raise RuntimeError("ARTIFACT_TRANSFER_TOO_LARGE")
    allowed = {"report.json", "smoke-inbox.sqlite"} | {f"response-{i}.body" for i in range(1, 7)}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or not set(names) <= allowed or "report.json" not in names:
            raise RuntimeError("ARTIFACT_NAMES_REJECTED")
        if sum(entry.file_size for entry in entries) > 64 * 1024 * 1024:
            raise RuntimeError("ARTIFACT_TRANSFER_TOO_LARGE")
        if any(stat.S_ISLNK(entry.external_attr >> 16) or entry.is_dir() for entry in entries):
            raise RuntimeError("ARTIFACT_TYPE_REJECTED")
        destination.mkdir(mode=0o700)
        for entry in entries:
            # Only a fixed set of plain basenames is ever written in this repo.
            fd = os.open(destination / entry.filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output, archive.open(entry) as source:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)


def plan():
    requests = [
        {"path": "/config", "query": {}},
        {"path": "/r/" + ROOM, "query": {"format": "json", "limit": 1, "n": "unique"}},
        {"path": "/r/" + ROOM, "query": {"format": "json", "since": 0, "limit": 200, "n": "unique"}},
        {"path": "/r/" + ROOM, "query": {"format": "json", "since": 0, "limit": 201, "n": "unique"}},
        {"path": "/r/" + ROOM, "query": {"format": "json", "since": "anchor from GET 2", "limit": 200, "wait": 10, "n": "unique"}},
        {"path": "/r/" + EMPTY_ROOM, "query": {"format": "json", "limit": 1, "n": "unique"}},
    ]
    return {"image_id": BASE_IMAGE, "image_pull": False, "image_build": False,
            "source_delivery": "public source zip via docker exec stdin to existing /state tmpfs",
            "offline_create_command": container_command(BASE_IMAGE, "<offline-name>", "offline"),
            "live_create_command": container_command(BASE_IMAGE, "<live-name>", "live"),
            "source_command": DOCKER + ["exec", "--interactive", "--user=65532:65532", "<name>", "python3", "-I", "-B", "-c", IMPORT_CODE],
            "execute_command": DOCKER + ["exec", "--user=65532:65532", "<name>", "python3", "-I", "-B", "-c", EXEC_CODE, "<offline-or-live>"],
            "export_command": DOCKER + ["exec", "--user=65532:65532", "<name>", "python3", "-I", "-B", "-c", EXPORT_CODE],
            "origin": "https://technocore.chat", "method": "GET", "requests": requests,
            "max_requests": 6, "min_pause_between_requests_seconds": 2,
            "redirects": False, "proxy_discovery": False,
            "dns": "technocore.chat via Docker's configured DNS resolver",
            "network_enforcement": "fixed-origin application allowlist; bridge is not a domain firewall",
            "cleanup": "stop and remove only containers created by this invocation; retain repo artifacts"}


class Runner:
    def __init__(self):
        suffix = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self.directory = ROOT / "docs" / ("smoke-results-" + suffix)
        print(json.dumps({"stage": "artifacts", "create_path": str(self.directory)}), flush=True)
        self.directory.mkdir(mode=0o700)
        self.config = self.directory / "docker-client"
        self.config.mkdir(mode=0o700)
        self.archive = self.directory / "observer.zip"
        self.manifest = make_archive(self.archive)
        self.write_json("source-hashes.json", self.manifest)

    def write_json(self, name, value):
        fd = os.open(self.directory / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=True, allow_nan=False, indent=2)

    def docker(self, args, timeout=30, check=True, binary=False, input=None):
        command = ["docker", "--config=" + str(self.config), "--host=unix:///var/run/docker.sock"] + args
        return subprocess.run(command, capture_output=True, text=not binary, check=check, timeout=timeout, input=input,
                              env={"PATH": os.environ.get("PATH", os.defpath), "HOME": str(self.config)})

    def check_image(self):
        # Never resolve a registry reference; inspect the already-pulled immutable ID.
        result = self.docker(["image", "inspect", "--format", "{{.Id}}", BASE_IMAGE])
        if result.stdout.strip() != BASE_IMAGE:
            raise RuntimeError("LOCAL_IMAGE_ID_MISMATCH")

    def stage(self, mode):
        name = "observer-smoke-" + mode + "-" + uuid.uuid4().hex[:12]
        print(json.dumps({"stage": mode, "container": name}), flush=True)
        created = False
        started = False
        try:
            args = container_command(BASE_IMAGE, name, mode)[len(DOCKER):]
            self.docker(args)
            created = True
            config = json.loads(self.docker(["container", "inspect", name]).stdout)[0]
            checks = inspect_checks(config, BASE_IMAGE, mode)
            self.write_json(mode + "-configuration.json", {"container": name, "checks": checks})
            if not all(checks.values()):
                raise RuntimeError("CONTAINER_CONFIGURATION_REJECTED")
            self.docker(["start", name])
            started = True
            self.docker(["exec", "--interactive", "--user=65532:65532", name, "python3", "-I", "-B", "-c", IMPORT_CODE],
                        binary=True, input=self.archive.read_bytes())
            result = self.docker(["exec", "--user=65532:65532", name, "python3", "-I", "-B", "-c", EXEC_CODE, mode],
                                 timeout=240, check=False)
            # Transport a private binary archive over a captured pipe. No raw
            # message/evidence data is printed to the host terminal or its logs.
            exported = self.docker(["exec", "--user=65532:65532", name, "python3", "-I", "-B", "-c", EXPORT_CODE], binary=True)
            save_artifacts(exported.stdout, self.directory / mode)
            report = json.loads((self.directory / mode / "report.json").read_text())
            # Only fixed status metadata crosses the terminal boundary, not bodies.
            print(json.dumps({"stage": mode + "_finished", "status": report.get("status"),
                              "request_count": len(report.get("requests", [])),
                              "report": str(self.directory / mode / "report.json")}), flush=True)
            if result.returncode or report.get("status") != "PASS":
                raise RuntimeError("CONTAINER_DIAGNOSTIC_STOPPED")
            return report
        finally:
            # No broad cleanup, no image removal, and no forced deletion.
            if started:
                stopped = self.docker(["stop", "--time=5", name], check=False)
                if stopped.returncode:
                    print(json.dumps({"stage": "cleanup", "container": name, "status": "STOP_FAILED_RETAINED"}), flush=True)
                    created = False  # Do not attempt removal of a possibly running container.
            if created:
                removed = self.docker(["rm", name], check=False)
                if removed.returncode:
                    print(json.dumps({"stage": "cleanup", "container": name, "status": "REMOVE_FAILED_RETAINED"}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Run offline preflight, then at most six live GETs")
    args = parser.parse_args()
    # Validate all bundled local code even in plan mode; no Docker access occurs.
    for source, name in source_files():
        ast.parse(source.read_bytes(), filename=name)
    for code in (IDLE_CODE, EXEC_CODE, IMPORT_CODE, EXPORT_CODE):
        ast.parse(code)
    if not args.execute:
        print(json.dumps(plan(), indent=2))
        return 0
    try:
        runner = Runner()
        runner.write_json("execution-plan.json", plan())
        runner.check_image()
        runner.stage("offline")
        runner.stage("live")
        print(json.dumps({"status": "COMPLETE", "artifacts": str(runner.directory),
                          "scope": "bounded smoke only; no soak"}), flush=True)
        return 0
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, RuntimeError) as exc:
        error = {"status": "STOPPED", "error_class": type(exc).__name__, "automatic_retry": False}
        if type(exc) is RuntimeError:
            error["error_code"] = str(exc)
        print(json.dumps(error, ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
