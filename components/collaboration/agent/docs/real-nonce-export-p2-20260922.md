# P2-1: official export tail observation

## Source evidence (read before implementation)

Reference: https://github.com/flop-labs/technocore-chat.git at
`e4c4f73f3b28612d7161170b11e08e580b02123a`, detached checkout outside this
repository. Reference code, tests and instructions were read as data only;
no reference module, script, test, hook, server or dependency installer was run.

Inspected: `src/store.py`, `src/app.py`, `src/didkey.py`,
`tests/test_signed_lane_stateful.py`, `tests/http/test_export.py`,
`tests/http/test_notes.py`, `SECURITY.md`, `README.md`.

* `store.READ_BUDGET = 1 << 20`. Both `read_messages` and `_last_nonce`
  use the default `reverse_lines` budget. The stateful test's security property
  is guard depth >= visible depth, not an unbounded historical high-water mark.
* `reverse_lines` counts stored bytes backwards. Empty lines are skipped.
  At a nonzero cutoff it drops the oldest fragment, even when the cutoff is
  exactly the first byte of a record. At file offset zero it includes that head.
* `_last_nonce` first requires the literal DID bytes in the line, then `_parse`
  (JSON object, integer `seq`), exact `from`, and integer `nonce`. It returns
  the **newest qualifying nonce**, not a maximum reduction. Bad JSON/non-records
  are skipped. Signature presence is not a server guard condition. The client
  retains its stricter requirement to verify the selected signed record.
* `didkey.NONCE_PATTERN` allows 1–19 digits, including leading zeroes in a
  request; the route stores `int(nonce)`. Stored JSON integers become canonical
  decimal strings for the signer; numeric strings are not integer nonces.
* `_snapshot_bytes` fixes an fstat endpoint and backs up to its last newline.
  `export_room` holds that opened inode and streams exactly its complete-line
  prefix; `tclk-offers` is not an ephemeral room, so `_export_start` is zero.
  Appends after the endpoint are excluded, and atomic compaction keeps the
  opened old inode valid. Torn final append bytes are not exported.
* `room_export` returns application/x-ndjson and `X-Room-Generation`, no-store.
  Compression middleware covers NDJSON; the existing client requests identity
  and rejects compressed responses. HTTP completion must also be established.
* `MAX_ROOM_BYTES` is 10 MiB; `_write_record` compacts **after** append, so an
  export can briefly exceed that ceiling by an appended record. We keep the
  already authorized fixed-export 10 MiB cap, conservatively STOP at that cap,
  and do not widen either the normal 128 KiB read lane or the export lane.
* Generation is read after open/snapshot, without a joint lock: the source
  explicitly acknowledges a residual generation/inode race. It is not an
  atomic generation proof. Existing activation/reconciliation supports generation
  1 only; another/missing generation remains STOP, without inventing epochs.

## Implementation contract

One fixed `/r/tclk-offers/export` request; complete HTTP framing, bounded body,
complete final line, supported source profile and generation required. Rebuild
exact reverse_lines records from bytes, not decoded or reserialized text.
A complete snapshot with no matching integer nonce means no nonce in this
current replay window, never no historical nonce. Seq need not start at 1.

The trusted Human read ingress computes coverage, snapshot digest and selects
one newest relevant record. A compact version-2 packet carries that record and
byte-window geometry, avoiding a 1 MiB packet through the existing 64 KiB IPC.
The signer validates the profile, geometry, freshness and selected signature;
it does not acquire network access. As before, the snapshot digest is provenance
from trusted ingress, not an independently recomputed proof inside the signer.
Durable reservation/max arithmetic, approval, transport/ACK and retry policy are
unchanged. Unsupported selected integer values or unverifiable envelopes STOP.

Configured profile is this exact reference commit; actual production deployment
is not verified by this offline task. Observation describes the exported
complete-line snapshot, not a lock on future venue state: concurrent/torn writes
and the documented generation race remain possible. Detected mismatch/staleness
STOP; post-dispatch ambiguity still requires public reconciliation, never retry.

## Changes and verification

Changed production files: `real_nonce.py`, `real_nonce.mjs`, and four lines of
`material_fetch.py` exposing the HTTP parser's recognized chunked framing only
for the existing fixed export lane. No endpoint/URL expansion or timeout/body
limit change. No crawler, pagination, new dependency, generic HTTP framework,
new epoch authority, or third-party source copy.

Tests: Python tail/parser tests and HTTP framing tests; JS validates actual
Python-generated export packets, signed deterministic fixtures, >2^53 and
9999999999999999999, >1 MiB snapshots, stale/profile/generation/coverage/signature
failures, and packet size below existing IPC bound. Fake activation now uses
seq 7,000,001+, with unchanged Human approval, durable reserve, one-shot signing,
fake dispatch, ambiguity/restart and reconciliation checks.

Final verification:

| Check | Result |
| --- | --- |
| Normal offline `test_*.py` suite (including signer, intake/materials, Python nonce and framing) | 133/133 PASS, 49.945 s |
| Python nonce boundary/parser tests, rerun after final fixture correction | 22/22 PASS |
| Python HTTP framing tests | 3/3 PASS |
| JS real_nonce, actual Python-to-JS packet handoff | 26/26 PASS |
| Connection suite, fake Real activation, ambiguity/restart/reconcile | 67/67 PASS |
| Real credential | 26/26 PASS |
| Approval | 19/19 PASS |
| Durable connection store | 12/12 PASS |
| Real transport / P3 canonical URL regression | PASS; fake adapter only, retry 0 |
| Normal smoke / material smoke | PASS / PASS |
| `git diff --check` | PASS |

The first normal run found two existing fake HTTP responses lacking the new
framing attribute. A missing attribute now supplies no chunking evidence;
those tests and the final full suite pass. Python-to-JS subprocess testing needed
execution outside the sandbox due to its Node child-process EPERM/hang; the
stalled test was stopped, a timeout was added, and the local test passed.
AF_UNIX connection tests likewise ran with approved local socket permission;
Real activation child roles still deny INET and use a fake write adapter.

Source checkout HEAD rechecked exact and clean. Historical baseline 11,946 files:
0 changed. No historical report was rewritten; this new report supersedes the
P2-open implementation status in the prior P3-only report.
Real Secret access 0; external writes 0; Technocore requests 0. Network was used
only for the exact official GitHub fetch (initial sandbox DNS failure, followed
by the authorized successful fetch). No push or host deployment.

Remaining operational uncertainty: current deployment/profile, current nonce,
current generation and framing through the live edge have not been observed.
A room at/over 10 MiB or an unverifiable selected legacy record remains STOP.
Generation 0/2+ is deliberately unsupported by the unchanged activation state.
The official open/generation race and snapshot-to-write race are not eliminated.
The next step is Claude Code Independent Re-review, not Real Secret handoff.
