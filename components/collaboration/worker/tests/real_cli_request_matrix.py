"""Final public-settings investigation, using the existing isolated fake endpoint.

Only synthetic data and a fresh dummy credential. No live permit, login, SDK
installation, binary patch, or production-policy change. HTTP records omit raw
headers/bodies/output. Every row starts one CLI and waits for completion.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from support import REPO
from real_cli_probe import namespace


PROFILES = {
    "current": {},
    "fallback-enabled": {"CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": None},
    "official-stop": {"FALLBACK_FOR_ALL_PRIMARY_MODELS": "1",
        "CLAUDE_CODE_RETRY_WATCHDOG": "0", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"},
}
CASES = ("success", "stream-error", "start-overload", "mid-overload", "mid-disconnect",
         "error-404", "error-429", "error-529", "error-500", "refusal",
         "stream-error-recover", "error-404-recover")
MATRIX = {profile + "--" + case: {"case": case, "profile": "text", "env": env}
          for profile, env in PROFILES.items() for case in CASES}
# Positive controls: a single additional HTTP retry, avoiding the default 10.
MATRIX.update({"http-retry-one--" + case: {"case": case, "profile": "text",
    "env": {"CLAUDE_CODE_MAX_RETRIES": "1"}}
    for case in ("error-429", "error-529", "error-500")})
# Current text profile has no --json-schema. Compare its no-op with the older
# structured profile; don't loosen safe-mode or tools just to make it succeed.
for profile in ("text", "structured"):
    for retries in ("0", "1", "2"):
        for case in ("success", "invalid-structured"):
            MATRIX[f"schema-{profile}-{retries}--{case}"] = {
                "case": case, "profile": profile,
                "env": {"MAX_STRUCTURED_OUTPUT_RETRIES": retries}}


def main():
    if sys.argv[1:2] == ["--namespace"]:
        namespace(Path(sys.argv[2]), sys.argv[3], sys.argv[4:], matrix=MATRIX)
        return 0
    cases = sys.argv[1:] or list(MATRIX)
    assert cases and len(cases) == len(set(cases)) and all(c in MATRIX for c in cases)
    area = Path(tempfile.mkdtemp(prefix="real-request-matrix-", dir=REPO / ".local"))
    print(area, flush=True)
    result = subprocess.run(["unshare", "--user", "--map-root-user", "--net", "--mount",
        "--propagation", "private", sys.executable, "-B", str(Path(__file__).resolve()),
        "--namespace", str(area), os.readlink("/proc/self/ns/net"), *cases],
        env={"PATH": "/usr/bin:/bin"}, timeout=60 * len(cases))
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
