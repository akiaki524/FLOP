"""Offline parse/save cases under 256 MiB RLIMIT_AS; no Docker/cgroup claim."""
import contextlib
import gc
import json
from pathlib import Path
import resource
import tempfile

from test_observer import FakeClient, envelope, reply
from technocore_observer.observer import Observer
from technocore_observer.protocol import MAX_BODY, Reply, json_dump
from technocore_observer.storage import StateLock, Store, initialize


def main():
    ceiling = 256 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
    cases = []

    def check(name, raw, accepted, seq=100):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as root:
            with StateLock(root):
                initialize(root, "test-room", envelope([100]), reply(envelope([100])))
                with contextlib.closing(Store(root, "test-room")) as store:
                    Observer(store, FakeClient(Reply(200, "application/json", raw))).poll_once()
                    s = store.state()
                    passed = (s["poll_seq"] == seq and
                              s["consecutive_protocol_anomalies"] == (0 if accepted else 1))
                    cases.append({"name": name, "body_bytes": len(raw), "passed": passed,
                                  "poll_seq": s["poll_seq"]})
        gc.collect()

    # Every message is at the official 4096-character text maximum.
    obj = envelope(range(101, 301))
    for message in obj["messages"]:
        message["text"] = "a" * 4096
    check("200_max_ascii_texts", json_dump(obj).encode(), True, 300)
    # 63 UTF-8 records fit roughly 1 MiB; escaped non-BMP output expands 3x.
    obj = envelope(range(101, 164))
    for message in obj["messages"]:
        message["text"] = "\U0001f642" * 4096
    check("tail_window_nonbmp_escape_expansion", json_dump(obj).encode(), True, 163)
    del obj
    check("dense_array", b'{"x":[' + b"0," * 1000000 + b"0]}", False)
    check("dense_objects", b'{"x":[' + b'{"a":0},' * 100000 + b"{}]}", False)
    check("deep_nesting", b'{"x":' + b"[" * 1000 + b"0" + b"]" * 1000 + b"}", False)
    check("body_over_cap", b"x" * (MAX_BODY + 1), False)
    # Near-cap valid JSON reaches envelope validation, without pathological object count.
    check("near_cap_single_string", b'{"x":"' + b"x" * (MAX_BODY - 9) + b'"}', False)
    check("near_cap_escaped_unicode", b'{"x":"' + b"\\ud83d\\ude42" * ((MAX_BODY - 9) // 12) + b'"}', False)
    # A single non-BMP character widens the whole decoded Python string to
    # four-byte storage. Exercise the actual tolerant content/save path too.
    obj = envelope([101])
    obj["messages"][0]["text"] = "x" * (MAX_BODY - 512) + "\U0001f642"
    check("near_cap_wide_python_string_saved_untrusted", json_dump(obj).encode(), True, 101)
    del obj
    report = {"rlimit_as_bytes": ceiling,
              "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
              "scope": "Linux process RLIMIT_AS; not container/cgroup measurement", "cases": cases}
    print(json.dumps(report))
    return 0 if all(case["passed"] for case in cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
