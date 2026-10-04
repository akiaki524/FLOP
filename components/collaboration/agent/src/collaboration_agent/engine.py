"""Classification -> policy -> durable claim -> pure solver -> validated evidence."""

import json

from .model import (Invalid, check, decode, digest, encode, fingerprint, outcome,
                    sha, validate_outcome, validate_task)
from .solvers import classify
from .state import MAX_EVENTS, MAX_RUNS


def envelope(task, result, solver=None, dry_run=False):
    return {"version": 1, "task_id": task["task_id"], "task_digest": digest(task),
            "fingerprint": fingerprint(task), "solver": solver, "dry_run": dry_run,
            "outcome": result,
            "limitations": ["offline_only", "source_authenticity_not_verified",
                            "completion_is_not_human_approval", "no_external_write"]}


class Agent:
    def __init__(self, store):
        self.store = store

    def _audit(self, kind, value):
        with self.store.transaction():
            self.store.event(kind, value)

    def _finish(self, fp, result):
        with self.store.transaction():
            cursor = self.store.db.execute(
                "UPDATE runs SET result=?, result_digest=? WHERE fingerprint=? AND result IS NULL",
                (encode(result).decode(), digest(result), fp))
            check(cursor.rowcount == 1, "already_finished")
            self.store.event("finished", {"fingerprint": fp, "result_digest": digest(result)})

    def recover(self):
        # Holding the process lock proves that no conforming prior worker remains active.
        # An interrupted admission is terminal review, never an implicit retry.
        pending = list(self.store.db.execute("SELECT * FROM runs WHERE result IS NULL"))
        for row in pending:
            task = json.loads(row["task"])
            result = envelope(task, outcome("HUMAN_REVIEW", "interrupted_execution"), row["solver"])
            self._finish(row["fingerprint"], result)

    def process(self, raw, *, dry_run=False):
        self.recover()
        try:
            task = validate_task(decode(raw))
        except Invalid as exc:
            result = {"version": 1, "input_sha256": sha(raw), "dry_run": dry_run,
                      "outcome": outcome("HUMAN_REVIEW", str(exc))}
            self._audit("rejected_input", result)
            return {"result": result, "result_digest": digest(result), "duplicate": False, "dry_run": dry_run}

        fp = fingerprint(task)
        solver = classify(task)
        solver_id = f"{solver.name}@{solver.version}" if solver else None

        def stopped(reason):
            result = envelope(task, outcome("HUMAN_REVIEW", reason), solver_id, dry_run)
            self._audit("deferred", result)
            return {"result": result, "result_digest": digest(result), "duplicate": False, "dry_run": dry_run}

        bound = self.store.db.execute("SELECT fingerprint FROM identities WHERE task_id=?",
                                      (task["task_id"],)).fetchone()
        if bound and bound[0] != fp:
            return stopped("task_id_conflict")
        prior = self.store.db.execute("SELECT * FROM runs WHERE fingerprint=?", (fp,)).fetchone()
        if not dry_run and not bound:
            check(self.store.db.execute("SELECT count(*) FROM identities").fetchone()[0] < MAX_RUNS,
                  "identity_capacity_reached")
            with self.store.transaction():
                self.store.db.execute("INSERT INTO identities VALUES (?, ?)", (task["task_id"], fp))
                self.store.event("identity", {"task_id": task["task_id"], "fingerprint": fp})
        if prior:
            self._audit("duplicate", {"requested_task_id": task["task_id"], "fingerprint": fp,
                                      "result_digest": prior["result_digest"], "dry_run": dry_run})
            return {"result": json.loads(prior["result"]), "result_digest": prior["result_digest"],
                    "duplicate": True, "dry_run": dry_run}

        policy = self.store.policy()
        if solver:
            family = policy["families"].get(task["family"])
            if family is None:
                return stopped("family_not_configured")
            if not family["enabled"]:
                return stopped("family_disabled")
            if family["suspended"]:
                return stopped("family_suspended")
        now = self.store.now()
        last = self.store.db.execute("SELECT max(at) FROM events").fetchone()[0]
        if now < last:
            return stopped("clock_regression")
        if solver:
            for seconds, limit, reason in ((3600, policy["per_hour"], "hour_limit"),
                                           (86400, policy["per_day"], "day_limit")):
                used = self.store.db.execute("SELECT count(*) FROM runs WHERE charged=1 AND started>?",
                                             (now - seconds,)).fetchone()[0]
                if used >= limit:
                    return stopped(reason)
        check(self.store.db.execute("SELECT count(*) FROM runs").fetchone()[0] < MAX_RUNS,
              "run_capacity_reached")
        # Reserve enough audit space to finish an admitted attempt, including after a crash.
        check(self.store.db.execute("SELECT count(*) FROM events").fetchone()[0] <= MAX_EVENTS - 4,
              "audit_capacity_reached")
        if not dry_run:
            with self.store.transaction():
                self.store.db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                                      (fp, encode(task).decode(), digest(task), solver_id, now, int(solver is not None)))
                self.store.event("started", {"fingerprint": fp, "task_digest": digest(task),
                                             "solver": solver_id, "started": now,
                                             "charged": int(solver is not None)})
        try:
            result = solver.solve(task) if solver else outcome("UNKNOWN", "unsupported_family")
            validate_outcome(result, task)
        except Invalid as exc:
            result = outcome("HUMAN_REVIEW", str(exc))
        except LookupError:
            result = outcome("UNKNOWN", "pointer_not_found")
        except Exception:
            # Never reflect raw exception messages, source text or traceback into an answer.
            result = outcome("HUMAN_REVIEW", "solver_error")
        wrapped = envelope(task, result, solver_id, dry_run)
        if dry_run:
            self._audit("dry_run", {"task": task, "result": wrapped, "result_digest": digest(wrapped)})
        else:
            self._finish(fp, wrapped)
        return {"result": wrapped, "result_digest": digest(wrapped), "duplicate": False, "dry_run": dry_run}

    def preview(self, task_id):
        self.recover()
        row = self.store.db.execute(
            "SELECT r.* FROM runs r JOIN identities i ON r.fingerprint=i.fingerprint WHERE i.task_id=?",
            (task_id,)).fetchone()
        check(row is not None, "task_not_processed")
        task = json.loads(row["task"])
        result = json.loads(row["result"])
        validate_outcome(result["outcome"], task)
        return {"preview_only": True, "human_approval": "NOT_GRANTED",
                "requested_task_id": task_id, "task": task, "result": result,
                "result_digest": row["result_digest"],
                "external_worker": {"available": False,
                                    "suggested": result["outcome"]["status"] != "COMPLETED"}}
