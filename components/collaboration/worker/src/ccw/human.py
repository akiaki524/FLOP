"""Human-operated local staging, preview, dummy signer and simulated transport.

This is a trusted CLI, not a worker tool. Never put it in a worker tool catalog.
Only mock-v1 is implemented; no URLs, credentials or real network calls exist.
"""

import argparse
from contextlib import contextmanager
import fcntl
import hmac
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from .llm import Broker
from .model import (Invalid, bundle, canonical, digest, identifier, layout, read,
                    require, sha, string, terminal_json, validate_handoff, write_new)

# Deliberately public fixture material. Not an identity key or production signature.
DUMMY_KEY = b"CCW-PUBLIC-DUMMY-KEY-NEVER-USE-IN-PRODUCTION-v1"


def initialize(root):
    root = Path(root).absolute()
    require(root == root.resolve() and not root.exists(), "init requires a new non-symlink directory")
    root.mkdir(mode=0o700)
    for name in ("worker", "human", "worker/inbox", "worker/outbox", "human/tasks"):
        (root / name).mkdir(mode=0o700)
    write_new(root / "human" / "dummy-key.json", {"kind": "PUBLIC_TEST_KEY_ONLY", "key": DUMMY_KEY.decode("ascii")})
    return {"root": str(root), "mode": "OFFLINE_MOCK_ONLY"}


@contextmanager
def locked(root):
    fd = os.open(root / "human" / "human.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


class Human:
    def __init__(self, root):
        self.root = root
        self.private = root / "human"
        self.db = sqlite3.connect(self.private / "signer.sqlite", timeout=1)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS approvals (
                digest TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL, payload BLOB NOT NULL,
                expires INTEGER NOT NULL, state TEXT NOT NULL, signature TEXT, receipt TEXT);
            CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY, digest TEXT, event TEXT NOT NULL, at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), stopped INTEGER NOT NULL);
            INSERT OR IGNORE INTO config VALUES(1,0);
        """)

    def audit(self, key, event):
        self.db.execute("INSERT INTO audit(digest,event,at) VALUES(?,?,?)", (key, event, int(time.time())))

    def active(self):
        require(not self.db.execute("SELECT stopped FROM config WHERE id=1").fetchone()[0], "Human stop is active")

    def stage(self, value):
        self.active()
        for path in (self.private / "tasks", self.root / "worker" / "inbox"):
            require(path == path.resolve() and path.is_dir(), "symlink stage directory")
        value = bundle(value)
        key = digest(value)
        # Pin the Human input outside Worker reach before handing over a copy.
        for path in (self.private / "tasks" / f"{key}.json", self.root / "worker" / "inbox" / f"{key}.json"):
            if path.exists():
                require(read(path) == value, "existing snapshot differs")
            else:
                write_new(path, value)
        return {"task_id": key, "mode": "OFFLINE_MOCK_ONLY"}

    def preview(self, task_id):
        identifier(task_id)
        trusted = bundle(read(self.private / "tasks" / f"{task_id}.json"))
        require(digest(trusted) == task_id, "trusted snapshot integrity failure")
        value = read(self.root / "worker" / "outbox" / f"{task_id}.json")
        validate_handoff(value, trusted)
        provenance = None
        if (self.private / "llm.sqlite").exists():
            broker = Broker(self.root)
            try:
                known = broker.db.execute("SELECT id FROM llm_attempts WHERE task=? AND state='succeeded'", (task_id,)).fetchone()
                if known is not None:
                    artifact = broker.artifact(known["id"])
                    require(value.get("llm") == artifact and value["review"] == artifact["report"],
                            "handoff provenance differs from broker evidence")
                    provenance = artifact["evidence"]
                else:
                    require("llm" not in value, "no successful broker evidence")
            finally:
                broker.db.close()
        else:
            require("llm" not in value, "no broker evidence ledger")
        return {"task_id": task_id, "payload_sha256": value["payload_sha256"],
                "payload": value["payload"], "exact_bytes_ascii": canonical(value["payload"]).decode("ascii"),
                "readable_body": json.loads(value["payload"]["body"]),
                "broker_provenance": provenance,
                "mode": "OFFLINE_MOCK_ONLY"}

    def approve(self, task_id, confirmation, ttl=300):
        self.active()
        preview = self.preview(task_id)
        require(confirmation == preview["payload_sha256"], "approval must name exact preview digest")
        require(type(ttl) is int and 1 <= ttl <= 3600, "TTL must be 1..3600 seconds")
        with self.db:
            require(self.db.execute("SELECT 1 FROM approvals WHERE task_id=?", (task_id,)).fetchone() is None,
                    "task already has an approval history; no reapproval or replay in v0.1")
            self.db.execute("INSERT INTO approvals VALUES(?,?,?,?, 'approved',NULL,NULL)",
                            (confirmation, task_id, canonical(preview["payload"]), int(time.time()) + ttl))
            self.audit(confirmation, "human-approved-exact-bytes")
        return {"payload_sha256": confirmation, "state": "approved", "expires_in": ttl}

    def approval(self, key):
        identifier(key)
        row = self.db.execute("SELECT * FROM approvals WHERE digest=?", (key,)).fetchone()
        require(row is not None, "no Human approval")
        require(sha(row["payload"]) == key, "approved bytes integrity failure")
        return row

    def send(self, key, outcome):
        self.active()
        require(outcome in ("success", "rejected", "lost-before", "lost-after", "crash-after"), "invalid mock outcome")
        row = self.approval(key)
        require(row["state"] == "approved", "single-use approval consumed or stopped; never auto-resend")
        require(row["expires"] > int(time.time()), "approval expired")
        preview = self.preview(row["task_id"])
        require(preview["payload_sha256"] == key, "handoff changed after approval")
        key_record = read(self.private / "dummy-key.json")
        require(key_record == {"kind": "PUBLIC_TEST_KEY_ONLY", "key": DUMMY_KEY.decode("ascii")}, "only public dummy key accepted")
        signature = hmac.new(DUMMY_KEY, row["payload"], "sha256").hexdigest()
        # Durable uncertainty BEFORE any simulated effect. Crash here is terminal
        # until Human reconciles; no API transitions this row back to approved.
        with self.db:
            self.db.execute("UPDATE approvals SET state='in_flight',signature=? WHERE digest=?", (signature, key))
            self.audit(key, "attempt-started")
        try:
            receipt = MockTransport(self.private).send(key, row["payload"], signature, outcome)
        except Exception:
            with self.db:
                self.db.execute("UPDATE approvals SET state='unknown' WHERE digest=?", (key,))
                self.audit(key, "result-unknown-no-retry")
            return {"payload_sha256": key, "state": "unknown", "action": "STOP; Human reconcile required"}
        state = "sent" if receipt["accepted"] else "rejected"
        with self.db:
            self.db.execute("UPDATE approvals SET state=?,receipt=? WHERE digest=?", (state, canonical(receipt).decode("ascii"), key))
            self.audit(key, state)
        return {"payload_sha256": key, "state": state, "receipt": receipt, "mode": "OFFLINE_MOCK_ONLY"}

    def reconcile(self, key):
        row = self.approval(key)
        require(row["state"] in ("unknown", "in_flight"), "reconcile requires uncertain attempt")
        receipt = MockTransport(self.private).lookup(key)
        if receipt:
            require(receipt["payload_sha256"] == sha(row["payload"]), "receipt binding mismatch")
            state = "sent"
        else:
            # Absence is NOT proof of failure; retain the fence indefinitely.
            state = "unknown"
        with self.db:
            self.db.execute("UPDATE approvals SET state=?,receipt=? WHERE digest=?",
                            (state, canonical(receipt).decode("ascii") if receipt else None, key))
            self.audit(key, "human-reconcile-" + state)
        return {"payload_sha256": key, "state": state, "receipt": receipt, "resend_allowed": False}

    def revoke(self, key):
        row = self.approval(key)
        require(row["state"] == "approved", "only unused approval can be revoked")
        with self.db:
            self.db.execute("UPDATE approvals SET state='revoked' WHERE digest=?", (key,))
            self.audit(key, "human-revoked")
        return {"state": "revoked"}

    def stop(self, active):
        marker = self.root / "worker" / "STOP"
        # Fence LLM use first: interruption must not leave a stopped Signer with
        # an unfenced broker. Starting removes this fence only after DB commit.
        if active and not (self.private / "LLM_STOP").exists():
            write_new(self.private / "LLM_STOP", {"stopped": True})
        # Signer stop persists even if the Worker modifies its own marker.
        with self.db:
            self.db.execute("UPDATE config SET stopped=? WHERE id=1", (int(active),))
            self.audit(None, "human-stop" if active else "human-start")
        if active:
            if not marker.exists():
                write_new(marker, {"stopped": True})
        elif marker.exists():
            marker.unlink()
        if not active and (self.private / "LLM_STOP").exists():
            (self.private / "LLM_STOP").unlink()
        return {"stopped": active}


class MockTransport:
    """Local durable receipt ledger. NO Technocore wire compatibility claimed."""

    def __init__(self, private):
        self.path = private / "mock-remote.sqlite"

    def connect(self):
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, payload BLOB NOT NULL, signature TEXT NOT NULL)")
        return db

    def send(self, key, data, signature, outcome):
        require(sha(data) == key and hmac.compare_digest(signature, hmac.new(DUMMY_KEY, data, "sha256").hexdigest()), "mock signature rejected")
        message = json.loads(data)
        require(message["protocol"] == "ccw.mock.v1", "mock protocol only")
        if outcome == "lost-before":
            raise TimeoutError("no response")
        if outcome == "rejected":
            return {"accepted": False, "payload_sha256": key, "mock_receipt": "rejected:" + key}
        db = self.connect()
        try:
            with db:
                db.execute("INSERT INTO deliveries VALUES(?,?,?)", (key, data, signature))
        finally:
            db.close()
        if outcome == "crash-after":
            os._exit(75)  # Test-only simulated power loss after remote acceptance.
        if outcome == "lost-after":
            raise TimeoutError("accepted but response lost")
        return self.lookup(key)

    def lookup(self, key):
        db = self.connect()
        try:
            row = db.execute("SELECT payload,signature FROM deliveries WHERE id=?", (key,)).fetchone()
        finally:
            db.close()
        if row is None:
            return None
        require(sha(row[0]) == key and hmac.compare_digest(row[1], hmac.new(DUMMY_KEY, row[0], "sha256").hexdigest()), "invalid mock receipt")
        return {"accepted": True, "payload_sha256": key, "mock_receipt": "accepted:" + key}


def scout_bundle(request, evidence, index, filename):
    require(type(index) is int and 0 <= index < len(evidence["messages"]), "invalid Human-selected message index")
    selected = evidence["messages"][index]
    text = string(selected["text"], 30_000)
    request = dict(request)
    require(request.get("sources") == [], "Scout request must have empty sources")
    request["sources"] = [{"id": "s1", "origin": "scout", "locator": f"{filename}#/messages/{index};canonical-json-sha256={digest(evidence)}",
                           "text": text, "sha256": sha(text.encode("utf-8"))}]
    return bundle(request)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", required=True)
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("stop")
    sub.add_parser("start")
    sub.add_parser("llm-stop")
    prepare = sub.add_parser("llm-prepare-claude-real")
    prepare.add_argument("--from-prepared-root", type=Path)
    sub.add_parser("llm-claude-login-plan")
    for name in ("llm-preview", "llm-approve", "llm-run-synthetic", "llm-run-claude-fixture", "llm-run-claude-real", "llm-retry", "llm-status"):
        item = sub.add_parser(name)
        item.add_argument("task_id")
        if name in ("llm-preview", "llm-approve"):
            item.add_argument("--provider", required=True, choices=("synthetic", "openai", "claude-fixture", "claude-real"))
            item.add_argument("--model", required=True)
            item.add_argument("--effort")
            item.add_argument("--ttl", type=int, default=300)
            item.add_argument("--max-attempts", type=int, required=True)
            item.add_argument("--timeout", type=int, required=True)
            item.add_argument("--max-request-bytes", type=int, required=True)
            item.add_argument("--max-response-bytes", type=int, required=True)
            item.add_argument("--max-total-request-bytes", type=int, required=True)
            item.add_argument("--max-cost-microusd", type=int, required=True)
        if name in ("llm-approve", "llm-retry"):
            item.add_argument("--confirm-sha256", required=True)
        if name == "llm-retry":
            item.add_argument("--previous-attempt", required=True)
            item.add_argument("--confirm-stopped", action="store_true", required=True)
        if name == "llm-run-synthetic":
            item.add_argument("--outcome", default="success", choices=("success", "failure", "timeout", "crash-after-start", "invalid", "oversized"))
    for name in ("llm-verify", "llm-export"):
        sub.add_parser(name).add_argument("attempt_id")
    stage = sub.add_parser("stage")
    stage.add_argument("input")
    scout = sub.add_parser("stage-scout")
    scout.add_argument("request")
    scout.add_argument("evidence")
    scout.add_argument("--index", type=int, required=True)
    for name in ("preview", "approve"):
        item = sub.add_parser(name)
        item.add_argument("task_id")
        if name == "approve":
            item.add_argument("--confirm-sha256", required=True)
            item.add_argument("--ttl", type=int, default=300)
    for name in ("send-mock", "reconcile-mock", "revoke", "status"):
        item = sub.add_parser(name)
        item.add_argument("payload_sha256")
        if name == "send-mock":
            item.add_argument("--outcome", required=True, choices=("success", "rejected", "lost-before", "lost-after", "crash-after"))
    return result


def llm_command(broker, args):
    if args.command == "llm-claude-login-plan":
        from .claude_real import login_plan
        return login_plan(broker.root)
    if args.command == "llm-prepare-claude-real":
        from .claude_real import prepare_runtime
        return prepare_runtime(broker.root, from_prepared_root=args.from_prepared_root)
    if args.command in ("llm-preview", "llm-approve"):
        preview = broker.preview(args.task_id, args.provider, args.model, args.max_attempts,
                                 args.timeout, args.max_request_bytes, args.max_response_bytes,
                                 args.max_total_request_bytes, args.max_cost_microusd, args.ttl, args.effort)
        return preview if args.command == "llm-preview" else broker.approve(preview, args.confirm_sha256, args.ttl)
    if args.command == "llm-run-synthetic":
        return broker.run(args.task_id, args.outcome)
    if args.command in ("llm-run-claude-fixture", "llm-run-claude-real"):
        _, terms, _ = broker.approval(args.task_id)
        expected = "claude-real" if args.command.endswith("-real") else "claude-fixture"
        require(terms["provider"] == expected, "run command/provider mismatch")
        return broker.run_claude(args.task_id)
    if args.command == "llm-retry":
        return broker.retry(args.task_id, args.previous_attempt, args.confirm_sha256, args.confirm_stopped)
    if args.command == "llm-status":
        return broker.status(args.task_id)
    if args.command == "llm-verify":
        return broker.verify(args.attempt_id)
    return broker.export(args.attempt_id)


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "init":
            result = initialize(args.root)
        elif args.command == "llm-stop":
            root = layout(args.root)
            marker = root / "human" / "LLM_STOP"
            if not marker.exists():
                write_new(marker, {"stop": True})
            result = {"stop_requested": True, "provider_stopped": None,
                      "next": "llm-status TASK; started means uncertain; no retry"}
        elif args.command == "llm-status":
            # Read while the run holds human.lock. No Signer action, no permit.
            root = layout(args.root)
            broker = Broker(root)
            try:
                result = broker.status(args.task_id)
            finally:
                broker.db.close()
        elif args.command.startswith("llm-"):
            root = layout(args.root)
            with locked(root):
                broker = Broker(root)
                try:
                    result = llm_command(broker, args)
                finally:
                    broker.db.close()
        else:
            root = layout(args.root)
            with locked(root):
                human = Human(root)
                try:
                    if args.command == "stage":
                        result = human.stage(read(args.input))
                    elif args.command == "stage-scout":
                        result = human.stage(scout_bundle(read(args.request), read(args.evidence), args.index, Path(args.evidence).name))
                    elif args.command == "preview":
                        result = human.preview(args.task_id)
                    elif args.command == "approve":
                        result = human.approve(args.task_id, args.confirm_sha256, args.ttl)
                    elif args.command == "send-mock":
                        result = human.send(args.payload_sha256, args.outcome)
                    elif args.command == "reconcile-mock":
                        result = human.reconcile(args.payload_sha256)
                    elif args.command == "revoke":
                        result = human.revoke(args.payload_sha256)
                    elif args.command == "status":
                        row = human.approval(args.payload_sha256)
                        result = {"state": row["state"], "payload_sha256": row["digest"], "expires": row["expires"]}
                    else:
                        result = human.stop(args.command == "stop")
                finally:
                    human.db.close()
        print(terminal_json(result) if args.command == "preview" else canonical(result).decode("ascii"))
        return 0
    except (Invalid, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        print(canonical({"error": type(exc).__name__, "message": str(exc) if isinstance(exc, Invalid) else "local operation failed; inspect state"}).decode("ascii"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
