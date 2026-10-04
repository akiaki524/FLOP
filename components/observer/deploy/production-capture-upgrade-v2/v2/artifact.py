"""Fixed reviewed-source allowlist and root-owned staged copy."""
from pathlib import Path
import os
import stat

from .common import CANDIDATE, V2Error, command, read_bytes, read_json, require, sha256, sync_dir, write_once

V2_FILES = (
    "__init__.py", "common.py", "artifact.py", "stop.py", "preflight.py",
    "stage.py", "activate.py", "monitor_runner.py", "writer_precheck.py",
    "reconcile.py", "cli.py", "__main__.py",
)
V2_PREFIX = "deploy/production-capture-upgrade-v2/v2/"
V2_IMAGE_FILES = (
    "deploy/production-capture-upgrade-v2/Containerfile.full-capture",
    "deploy/production-capture-upgrade-v2/Containerfile.full-capture.dockerignore",
)
MANIFEST_PATH = "deploy/production-capture-upgrade-v2/release-manifest.json"
PROVENANCE_PATH = "deploy/capture-candidate-integration.json"


def expected_paths(provenance):
    source = set(provenance["adopted_source_files"]) | set(provenance["existing_identical_dependency_files"])
    return source | {V2_PREFIX + name for name in V2_FILES} | set(V2_IMAGE_FILES)


def verify_source(source):
    """Verify every staged byte; no implicit files, symlinks, or late imports."""
    source = Path(source)
    provenance = read_json(source / PROVENANCE_PATH)
    require(provenance["source_candidate_commit"] == CANDIDATE, "CANDIDATE_COMMIT")
    require(len(provenance["adopted_source_files"]) == 19 and
            len(provenance["existing_identical_dependency_files"]) == 3, "CANDIDATE_SCOPE")
    manifest = read_json(source / MANIFEST_PATH)
    require(isinstance(manifest, dict) and set(manifest) == expected_paths(provenance),
            "RELEASE_ALLOWLIST")
    for path, expected in manifest.items():
        require(sha256(read_bytes(source / path)) == expected, "RELEASE_FILE_HASH")
    for group in ("adopted_source_files", "existing_identical_dependency_files"):
        for path, expected in provenance[group].items():
            require(manifest[path] == expected, "CANDIDATE_FILE_HASH")
    candidate_manifest = read_bytes(source / "deploy/production-capture-human/source-manifest.json")
    require(sha256(candidate_manifest) == provenance["source_manifest_sha256"],
            "CANDIDATE_MANIFEST_HASH")
    for path, expected in read_json(source / "deploy/production-capture-human/source-manifest.json").items():
        require(manifest[path] == expected, "CANDIDATE_MANIFEST_ENTRY")
    return manifest


def verify_checkout(source, release_commit, runner):
    require(len(release_commit) == 40 and all(c in "0123456789abcdef" for c in release_commit),
            "RELEASE_COMMIT")
    actual = command(runner, ["git", "-C", str(source), "rev-parse", "HEAD"], "RELEASE_GIT").strip()
    require(actual == release_commit, "RELEASE_COMMIT_CHANGED")
    clean = command(runner, ["git", "-C", str(source), "status", "--porcelain=v1"], "RELEASE_GIT")
    require(not clean.strip(), "RELEASE_CHECKOUT_DIRTY")


def copy_reviewed(source, release_dir, manifest):
    """Write into a new Attempt directory; preserve each verified byte exactly."""
    source, release_dir = Path(source), Path(release_dir)
    require(not release_dir.exists(), "RELEASE_ATTEMPT_EXISTS")
    release_dir.mkdir(mode=0o700)
    for path, digest in sorted(manifest.items()):
        destination = (release_dir / "v2" / path.removeprefix(V2_PREFIX)
                       if path.startswith(V2_PREFIX) else release_dir / "source" / path)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = read_bytes(source / path)
        require(sha256(data) == digest, "SOURCE_CHANGED_DURING_STAGE")
        write_once(destination, data, mode=0o600)
    write_once(release_dir / "release-manifest.json",
               read_bytes(source / MANIFEST_PATH), mode=0o600)
    sync_dir(release_dir)
    for path, digest in manifest.items():
        destination = (release_dir / "v2" / path.removeprefix(V2_PREFIX)
                       if path.startswith(V2_PREFIX) else release_dir / "source" / path)
        require(sha256(read_bytes(destination)) == digest, "STAGED_FILE_HASH")
    return release_dir


def verify_staged_copy(release_dir, expected_manifest_sha):
    """Recheck root-owned bytes before any shared switch or Writer start."""
    release_dir = Path(release_dir)
    data = read_bytes(release_dir / "release-manifest.json")
    require(sha256(data) == expected_manifest_sha, "STAGED_MANIFEST_CHANGED")
    manifest = read_json(release_dir / "release-manifest.json")
    require(isinstance(manifest, dict) and len(manifest) == 36, "STAGED_MANIFEST_SCOPE")
    for path, digest in manifest.items():
        require(isinstance(path, str) and (path.startswith(V2_PREFIX) or
                path.startswith("src/") or path.startswith("deploy/")) and
                ".." not in Path(path).parts and Path(path).is_absolute() is False,
                "STAGED_MANIFEST_PATH")
        target = (release_dir / "v2" / path.removeprefix(V2_PREFIX)
                  if path.startswith(V2_PREFIX) else release_dir / "source" / path)
        require(sha256(read_bytes(target)) == digest, "STAGED_FILE_CHANGED")
    return manifest
