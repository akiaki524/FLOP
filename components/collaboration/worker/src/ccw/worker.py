"""One-shot, confined worker. No approval, key, network or shell API."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import re
from pathlib import Path
import resource
import sqlite3
import sys
import time

from . import isolation
from .model import (Invalid, bundle, canonical, digest, handoff, identifier,
                    layout, read, require, write_new)
from .providers import CodexCLIAdapter, FixtureProvider, ReplayRunner
from .llm import schema_check
from .claude import schema as claude_schema


@contextmanager
def locked(workspace):
    fd = os.open(workspace / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


class Store:
    def __init__(self, workspace):
        self.workspace = workspace
        self.db = sqlite3.connect(workspace / "tasks.sqlite", timeout=1)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA temp_store=MEMORY")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, frozen TEXT NOT NULL, state TEXT NOT NULL,
                report TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, task_id TEXT NOT NULL, state TEXT NOT NULL, at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS broker_results (task TEXT PRIMARY KEY, artifact TEXT NOT NULL);
        """)

    def event(self, task_id, state):
        self.db.execute("INSERT INTO events(task_id,state,at) VALUES(?,?,?)", (task_id, state, int(time.time())))

    def task(self, task_id):
        identifier(task_id)
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        require(row is not None, "unknown task")
        frozen = bundle(json.loads(row["frozen"]))
        require(digest(frozen) == task_id, "frozen task integrity failure")
        return row, frozen

    def intake(self, value):
        frozen = bundle(value)
        task_id = digest(frozen)
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO tasks VALUES(?,?, 'ready',NULL,NULL)",
                                     (task_id, canonical(frozen).decode("ascii")))
            if cursor.rowcount:
                self.event(task_id, "ready")
        row, _ = self.task(task_id)
        return {"task_id": task_id, "state": row["state"], "duplicate": not bool(cursor.rowcount)}

    def transition(self, task_id, state, allowed):
        row, _ = self.task(task_id)
        require(row["state"] in allowed, f"cannot {state} from {row['state']}")
        with self.db:
            self.db.execute("UPDATE tasks SET state=?,error=NULL WHERE id=?", (state, task_id))
            self.event(task_id, state)
        return {"task_id": task_id, "state": state}

    def review(self, task_id, provider="fixture", response=None):
        row, frozen = self.task(task_id)
        require(row["state"] == "ready", "review requires ready; explicit resume after interruption")
        self.transition(task_id, "reviewing", {"ready"})
        try:
            runner = ReplayRunner(response) if provider == "codex-replay" else None
            report = (FixtureProvider() if provider == "fixture" else CodexCLIAdapter(runner)).run(frozen)
            with self.db:
                self.db.execute("UPDATE tasks SET state='reviewed',report=?,error=NULL WHERE id=?",
                                (canonical(report).decode("ascii"), task_id))
                self.event(task_id, "reviewed")
        except Exception as exc:
            with self.db:
                self.db.execute("UPDATE tasks SET state='paused',error=? WHERE id=?", (type(exc).__name__, task_id))
                self.event(task_id, "paused")
            raise
        return {"task_id": task_id, "state": "reviewed", "report": report}

    def import_llm(self, task_id, attempt):
        require(re.fullmatch(r"[a-f0-9]{32}", attempt), "invalid attempt ID")
        row, frozen = self.task(task_id)
        require(row["state"] == "ready", "import requires ready")
        value = read(self.workspace / "inbox" / (attempt + ".llm.json"))
        evidence = value["evidence"]
        require(evidence["attempt_id"] == attempt and evidence["task_digest"] == task_id
                and (evidence["kind"], evidence["provider"]) in (
                    ("BROKER_SYNTHETIC_EVIDENCE", "synthetic"),
                    ("BROKER_CLAUDE_FIXTURE_EVIDENCE", "claude-fixture"),
                    ("BROKER_CLAUDE_REAL_EVIDENCE", "claude-real"))
                and digest(evidence) == value["evidence_digest"] and digest(value["report"]) == evidence["report_digest"],
                "invalid synthetic evidence handoff")
        if evidence["provider"] in ("claude-fixture", "claude-real"):
            from .model import review
            expected_schema = claude_schema()
            if evidence["provider"] == "claude-real":
                expected_schema["properties"]["provider"]["enum"] = ["claude-cli-real-v1"]
            schema_check(value["report"], expected_schema)
            review(value["report"], frozen)
        else:
            schema_check(value["report"])
            CodexCLIAdapter().parse(frozen, canonical(value["report"]))
        with self.db:
            self.db.execute("INSERT INTO broker_results VALUES(?,?)", (task_id, canonical(value).decode("ascii")))
            self.db.execute("UPDATE tasks SET state='reviewed',report=? WHERE id=? AND state='ready'",
                            (canonical(value["report"]).decode("ascii"), task_id))
            self.event(task_id, "broker-synthetic-imported; Human must verify provenance")
        return {"task_id": task_id, "state": "reviewed", "attempt_id": attempt, "real_llm": "DISABLED"}

    def export(self, task_id):
        row, frozen = self.task(task_id)
        require(row["state"] in {"reviewed", "exported"}, "review required")
        value = handoff(frozen, json.loads(row["report"]))
        broker = self.db.execute("SELECT artifact FROM broker_results WHERE task=?", (task_id,)).fetchone()
        if broker:
            value["llm"] = json.loads(broker[0])
        path = self.workspace / "outbox" / f"{task_id}.json"
        if path.exists():
            require(read(path) == value, "existing export differs; preserve for investigation")
        else:
            write_new(path, value)
        if row["state"] != "exported":
            self.transition(task_id, "exported", {"reviewed"})
        return {"task_id": task_id, "state": "exported", "payload_sha256": value["payload_sha256"], "handoff": str(path)}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", required=True)
    sub = result.add_subparsers(dest="command", required=True)
    imported = sub.add_parser("import-llm")
    imported.add_argument("task_id")
    imported.add_argument("attempt_id")
    intake = sub.add_parser("intake")
    intake.add_argument("task_id")
    for name in ("review", "export", "pause", "resume", "status", "adapter-request"):
        command = sub.add_parser(name)
        command.add_argument("task_id")
        if name == "review":
            command.add_argument("--provider", choices=("fixture", "codex", "codex-replay"), default="fixture")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        # Trusted bootstrap only. No bundle or provider is loaded before enter.
        root = layout(args.root)
        workspace = root / "worker"
        for name in ("inbox", "outbox"):
            require((workspace / name).resolve() == workspace / name, "symlink role path")
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16_000_000, 16_000_000))
        resource.setrlimit(resource.RLIMIT_AS, (512_000_000, 512_000_000))
        os.chdir(workspace)
        isolation.enter(workspace)
        identifier(args.task_id)
        with locked(workspace):
            require(args.command == "status" or not (workspace / "STOP").exists(), "Human stop is active")
            store = Store(workspace)
            try:
                if args.command == "intake":
                    value = read(workspace / "inbox" / f"{args.task_id}.json")
                    require(digest(value) == args.task_id, "inbox digest mismatch")
                    result = store.intake(value)
                elif args.command == "review":
                    response = read(workspace / "inbox" / f"{args.task_id}.replay.json") if args.provider == "codex-replay" else None
                    result = store.review(args.task_id, args.provider, response)
                elif args.command == "import-llm":
                    result = store.import_llm(args.task_id, args.attempt_id)
                elif args.command == "export":
                    result = store.export(args.task_id)
                elif args.command in ("pause", "resume"):
                    result = store.transition(args.task_id, "paused" if args.command == "pause" else "ready",
                                              {"ready", "reviewing"} if args.command == "pause" else {"paused", "reviewing"})
                elif args.command == "adapter-request":
                    _, frozen = store.task(args.task_id)
                    result = CodexCLIAdapter().request(frozen)
                else:
                    row, _ = store.task(args.task_id)
                    result = {"task_id": args.task_id, "state": row["state"], "error": row["error"]}
            finally:
                store.db.close()
        print(canonical(result).decode("ascii"))
        return 0
    except (Invalid, isolation.IsolationError, OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        # No raw untrusted text, paths or credential contents in errors.
        print(canonical({"error": type(exc).__name__, "message": str(exc) if isinstance(exc, (Invalid, isolation.IsolationError)) else "local operation failed; inspect state"}).decode("ascii"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
