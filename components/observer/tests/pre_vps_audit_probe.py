"""Bounded offline reproduction of the pre-fix P1 findings. No live transport.

Run from the repository: python3 -B tests/pre_vps_audit_probe.py
Writes only the named report and disposable repository-local SQLite databases.
This is a diagnostic of the current implementation, not a passing fix regression.
"""

import contextlib
import hashlib
import json
from pathlib import Path
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time

from test_observer import FakeClient, envelope, reply
from technocore_observer.observer import Observer
from technocore_observer.protocol import MAX_BODY, Reply, decode_reply
from technocore_observer.storage import StateLock, Store, initialize

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs/pre-vps-p1-verification.json"


@contextlib.contextmanager
def database():
    with tempfile.TemporaryDirectory(prefix=".p1-probe-", dir=ROOT / "tests") as name:
        directory = Path(name)
        with StateLock(directory):
            anchor = envelope([100])
            initialize(directory, "test-room", anchor, reply(anchor))
            with contextlib.closing(Store(directory, "test-room")) as store:
                yield directory, store


def cursors(store):
    state = store.state()
    return {key: state[key] for key in
            ("poll_seq", "resolved_seq", "status", "observer_epoch")}


def table_counts(store):
    return {name: store.conn.execute("SELECT count(*) FROM " + name).fetchone()[0]
            for name in ("messages", "gaps", "evidence", "events")}


def forward_jump():
    with database() as (_, store):
        observer = Observer(store, FakeClient(reply(envelope([2**63 - 1])),
                            reply(envelope([101])), reply(envelope([101])),
                            reply(envelope([101]))))
        result = {"before": cursors(store), "attack_seq": 2**63 - 1}
        result["accepted"] = observer.poll_once()[0]
        result["after_attack"] = cursors(store)
        result["gaps"] = [list(row) for row in store.conn.execute(
            "SELECT start_seq,end_seq FROM gaps")]
        result["legitimate_poll_continuation"] = [observer.poll_once()[0] for _ in range(3)]
        result["after_legitimate_polls"] = cursors(store)
        result["anomalies"] = [row[0] for row in store.conn.execute(
            "SELECT anomaly_type FROM evidence WHERE anomaly_type<>'INIT_ANCHOR'")]
        return result


def frozen_gap():
    with database() as (directory, store):
        observer = Observer(store, FakeClient(reply(envelope([200, 201])),
                            reply(envelope([202, 203])), reply(envelope([204, 205]))))
        snapshots = []
        for _ in range(3):
            observer.poll_once()
            snapshots.append(cursors(store))
        with contextlib.closing(Store(directory, "test-room", readonly=True)) as reader:
            reopened = cursors(reader)
        return {"snapshots": snapshots, "reopened": reopened,
                "gap_rows": [dict(row) for row in store.conn.execute("SELECT * FROM gaps")]}


def evidence_growth():
    with database() as (_, store):
        large_error = Reply(503, "text/html", b"x" * (1024 * 1024))
        rate_error = Reply(429, "text/html", b"x" * (1024 * 1024))
        observer = Observer(store, FakeClient(*([large_error, rate_error, TimeoutError()] * 8)))
        snapshots = []
        for number in range(24):
            observer.poll_once()
            if (number + 1) % 6 == 0:
                snapshots.append({"failures": number + 1, **cursors(store),
                    **table_counts(store), "evidence_body_bytes": store.conn.execute(
                        "SELECT sum(length(body_bytes)) FROM evidence").fetchone()[0]})
        return {"snapshots": snapshots, "requests": "24 fake replies; zero network requests",
                "note": "Network failures append events, even without evidence rows."}


def evidence_disk_full():
    with database() as (directory, store):
        before = {**cursors(store), **table_counts(store)}
        pages = store.conn.execute("PRAGMA page_count").fetchone()[0]
        store.conn.execute(f"PRAGMA max_page_count={pages}")
        code = None
        try:
            Observer(store, FakeClient(Reply(503, "text/html", b"x" * 1048576))).poll_once()
        except sqlite3.OperationalError as exc:
            code = exc.sqlite_errorname
        after = {**cursors(store), **table_counts(store)}
        with contextlib.closing(Store(directory, "test-room", readonly=True)) as reader:
            reopened = cursors(reader)
        return {"sqlite_error": code, "before": before, "after": after,
                "cursor_and_tables_unchanged": before == after, "reopened": reopened,
                "note": "Real SQLITE_FULL page quota, not host disk exhaustion or a mock."}


def memory_child():
    # Address-space limit protects the host; this is not a cgroup/container claim.
    ceiling = 256 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
    count = 400000
    record = b'{"seq":101,"ts":0,"from":"x","text":"x"}'
    body = (b'{"room":"test-room","count":400000,"first_seq":101,'
            b'"last_seq":101,"generation":7,"messages":[' +
            (record + b",") * (count - 1) + record + b"]}")
    result = {"body_bytes": len(body), "message_count": count,
              "max_body": MAX_BODY, "rlimit_as_bytes": ceiling}
    try:
        parsed = decode_reply(Reply(200, "application/json", body))
        result["outcome"] = "decoded_before_count_or_sequence_validation"
        result["decoded_count"] = len(parsed["messages"])
    except MemoryError:
        result["outcome"] = "MemoryError_before_protocol_validation"
    result["peak_rss_bytes_linux"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    print(json.dumps(result))


def artifacts():
    directory = ROOT / "docs/smoke-results-20260906-125741-7d70a083/live"
    report = json.loads((directory / "report.json").read_text())
    checked = []
    for request in report["requests"]:
        body = (directory / f"response-{request['number']}.body").read_bytes()
        checked.append({"number": request["number"], "query": request["query"],
                        "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                        "matches_report": hashlib.sha256(body).hexdigest() == request["body_sha256"]})
    config = json.loads((directory / "response-1.body").read_bytes())
    return {"source": str(directory.relative_to(ROOT)), "responses": checked,
            "history": report["limit_200"],
            "published_setting_names": sorted(config["settings"]),
            "since_semantics": "UNRESOLVED: oldest-first and newest-limit both fit",
            "documented_message_size_limit": "NOT PRESENT in saved config"}


def main():
    if sys.argv[1:] == ["--memory-child"]:
        memory_child()
        return
    child = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()),
                            "--memory-child"], cwd=ROOT, capture_output=True,
                           text=True, timeout=60, check=True)
    sources = sorted((ROOT / "src/technocore_observer").glob("*.py"))
    result = {"kind": "PRE_FIX_REPRODUCTION_NOT_FIX_VERIFICATION", "created_at": time.time(),
              "live_get_count": 0, "docker_socket_access": False,
              "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in sources},
              "P1-2": artifacts(), "P1-1": forward_jump(), "P1-3": frozen_gap(),
              "P1-4": evidence_growth(), "evidence_sqlite_full": evidence_disk_full(),
              "P1-5": json.loads(child.stdout)}
    with REPORT.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=True)
        handle.write("\n")
    print(json.dumps({"report": str(REPORT.relative_to(ROOT)),
                      "forward_jump": result["P1-1"]["after_attack"],
                      "gap_reopened": result["P1-3"]["reopened"],
                      "evidence_full_integrity": result["evidence_sqlite_full"]["cursor_and_tables_unchanged"],
                      "memory": result["P1-5"]}))


if __name__ == "__main__":
    main()
