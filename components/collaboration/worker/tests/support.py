"""Synthetic data only; never reads user credentials or live Scout data."""

from pathlib import Path
import os
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from ccw.model import sha


def sample(text="# Transport notes\nTODO: document timeout recovery.\n"):
    return {"version": 1, "request": "保存資料の未完了事項を根拠付きでレビューしてください。",
            "scope": {"include": ["資料内のTODOと復旧手順"], "exclude": ["URL取得、Code実行、実送信"]},
            "destination": {"room": "mock/flop_labs", "recipient": "human-selected-demo"},
            "sources": [{"id": "s1", "origin": "human", "locator": "examples/material.txt",
                         "text": text, "sha256": sha(text.encode("utf-8"))}]}


def cli(role, root, *args, expected=0):
    result = subprocess.run([sys.executable, "-B", "-m", "ccw." + role, "--root", str(root), *args],
                            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO / "src"), "LANG": "C.UTF-8"},
                            cwd=REPO, capture_output=True, text=True, timeout=10)
    if result.returncode != expected:
        raise AssertionError(f"{role} {args}: {result.returncode}; stdout={result.stdout}; stderr={result.stderr}")
    return result
