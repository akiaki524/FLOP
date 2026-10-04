# Multi-Room Runtime (Issue #32)

This is an opt-in, separate runtime. It does not read, write, stop, or migrate the existing lobby Production state. `multi_room.py plan` is offline. The example config uses a `/tmp` root for **offline tests only**. Its limits are provisional caps for a 24-hour trial, not a measured 10-day allocation or preallocated disk.

## Shape

- Eleven new locally captured Rooms have separate Spool, Archive manifest/shards, producer lock, cursor, epoch, failures, and Archive worker. Capture and Archive are separate processes per Room. The twelfth configured Room, lobby, remains entirely external: initialization makes no lobby store and neither role starts for it. An operator can stop or replace a new Room's processes without stopping unrelated Rooms.
- One `ReservationBudget` directory charges all new Capture processes against an explicit shared allocation. The example leaves `external_rpm`, `external_waiters`, and `capture_rpm` unset. `plan` and offline initialization work, but Capture refuses to start until those values are set from a fresh inventory of every client on the same egress IP. The independent review found a Production *policy* reserving 480 requests/minute and three waiter slots across lobby capture, observer-live and observer-results-tail; this is not proof of their current activity or traffic.
- New-runtime ordinary polls use the documented `wait=0` read mode so quiet Rooms do not hold long-poll slots for ten seconds. A bounded HTTP slot protects simultaneous requests. With at least two allocated slots, one slot is exclusive to Recovery export/confirmation while normal Capture uses the rest. With only one slot, automatic Recovery is deferred and the gap is preserved instead of allowing an export to block every normal Capture. A slow or stalled request remains bounded by the 30-second client deadline.
- Each Room has `interval_seconds` and `max_rpm`. The example gives lobby, offers, and kibble faster policy and the quieter Rooms slower policy. Full pages rejoin the shared queue promptly. The example values are initial settings for a trial, not capacity proof.
- One `global_min_free_bytes` applies to the target filesystem across all new Rooms. Initialization and each service start check that budget, Spool and Archive paths are on the same filesystem. Spool transactions and Recovery staging, Archive shards and manifest work, and scheduler control transactions check the shared free-space floor before writing. The configured 10 GiB floor has a further 1 GiB write margin: eleven 64 MiB WAL gates, eleven 8 MiB Recovery staging bounds and eleven 4 MiB default Archive shards total about 836 MiB before smaller DB/control writes. Checks are admission guards, not a filesystem quota; other host writers can still consume space between checks. A capacity failure stops the affected service with state retained.
- Important Rooms use Spool schema v2. Each accepted poll's HTTP library decoded response body is stored in `response_raw` in the same SQLite transaction as its batch and cursor, with SHA-256, batch ID, Room via Spool identity, request cursor via `batches`, and an explicit `POLL_HTTP_BODY` label. For a recovered range, the triggering poll and confirmation poll bodies have distinct labels on the recovery batch. The batch `response_sha256` hashes the canonical normalized recovery NDJSON, not either HTTP body; each raw row has its own hash. The normalized messages remain in `entries`. Standard Rooms use existing schema v1. The existing lobby Spool is neither upgraded nor opened by this runtime.
- An Archive failure leaves Capture running while Spool capacity permits. Capture storage failure stops that Room. There is no automatic restart in this increment.

## Offline commands

From `components/observer`:

```sh
PYTHONPATH=src python3 -B -m technocore_full_capture.multi_room plan --config deploy/multi-room-example.json
python3 -B -m unittest discover -s tests -v
PYTHONPATH=src python3 -B -m technocore_observer --help
```

## SQLite image gate for the trial

The eleven new Room services have a separate image definition at `deploy/Containerfile.multi-room`; the existing lobby image is unchanged. It builds SQLite **3.53.4** from the upstream `sqlite-autoconf-3530400.tar.gz` archive after checking its published SHA3-256 (`454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338`). The runtime image loads the resulting `/opt/sqlite/lib` library through `LD_LIBRARY_PATH`. The final image build runs `sqlite_runtime --sqlite-provenance` as the unprivileged runtime user. Every image entrypoint invocation repeats that check before Multi-Room imports or opens a Room database. The check requires Python `sqlite3.sqlite_version=3.53.4`, SQLite's exact official `sqlite_source_id()`, and a single mapped `libsqlite3.so` under `/opt/sqlite/lib` in `/proc/self/maps`. The provenance command prints those observed values and the mapped library path. A mismatch exits 2 without touching Room state.

For a reviewable image artifact, build with `docker build -f deploy/Containerfile.multi-room -t observer-multi-room:sqlite-3.53.4 .` from this component and run its `--sqlite-provenance` command in a no-network, no-mount container. Record the immutable image ID and probe output in the new trial's operation record. Run an offline fixture compatibility check with that same image: open a copy of an existing Spool and Archive manifest, read messages/gaps/cursor/epoch/provenance, write and reopen a disposable copy, exercise WAL and checkpoint, and compare records before any Production start. The source archive download and compiler install occur only during this new image build; the image has no compiler. The local repository tests cannot establish the image's loaded library or Production database compatibility without building and probing that artifact.

[SQLite WAL §11](https://sqlite.org/wal.html#walresetbug) identifies 3.51.3 and later as fixed. The [3.53.4 release log](https://sqlite.org/releaselog/3_53_4.html) carries the WAL-reset fix from 3.53.0 and publishes the expected source ID. The [official download page](https://sqlite.org/download.html) publishes the source archive hash. Do not substitute withdrawn 3.52.0 or accept a version string alone.

`initialize` creates a **fresh, empty** configured local root and 11 new Room stores. `run --room ROOM --role capture|archive` runs one service; `status --room ROOM` reads its Spool and Archive state. Capture makes fixed-origin Technocore GETs. Do not run Capture against Live until the Production Human Gate. The existing lobby remains an external producer. Its config is pinned to `capture_owner=external` and `max_db_bytes=null`; the new runtime rejects either lobby role even when requested explicitly. Any later producer transition requires a separately approved code/configuration change.

## Provisional 24-hour capacity

Human-shared manual node-01 probe ran 121.6 seconds / 21 cycles; it is not reproduced by this repository. It recorded close1 normalized 1238.7 KiB and raw 1240.8 KiB, tclk-offers 506.4 / 508.7 KiB, and kibble 476.9 / 478.8 KiB. Events had 2 messages, sub_economy 12, zk-desk-a 0, and each d-close1 Room 0 messages with about 2.1–2.2 KiB raw. The probe total allocated bytes were 5,791,744. The simple total extrapolation is about 3814 MiB/day, but the probe omitted Production Archive shards, long-running WAL growth, Recovery staging, control data and longer-term filesystem overhead. The lobby probe saw 3153 messages, 6 full pages and 6 gap events; lobby is external to this allocation and that sample can undercount it.

| New Room | Trial Spool DB ceiling | Reason |
| --- | ---: | --- |
| close1 | 4 GiB | About 1.7 GiB/day normalized + raw by short extrapolation, with more than 2× room for variation and SQLite overhead |
| tclk-offers, kibble | 2 GiB each | About 0.7 GiB/day each, with about 3× room for variation and overhead |
| events, sub_economy, zk-desk-a, each d-close1-* | 256 MiB each | Short probe quiet; floor allows metadata, empty-poll raw and bounded Recovery, but must be reassessed if traffic rises |
| lobby | none | Existing external producer; no new Spool or Archive allocation |

The 11 DB ceilings sum to 10 GiB but do not reserve 10 GiB of disk. They bound only SQLite DB files. Archive shards, raw inside important-Room Spool, WAL, Recovery staging, manifest and control consume additional space. The common 10 GiB free reserve plus 1 GiB write margin guards their shared filesystem; it is an initial operating threshold, not a proof of 24-hour fit. Before the trial, verify the actual target filesystem, free bytes, existing-client inventory and intended total demand. After 24 hours, measure each Room's DB, WAL, Archive, raw, recovery and control growth and filesystem free bytes. Update the per-Room `max_db_bytes` and `global_min_free_bytes` in configuration from that evidence; only then plan the approximately 10-day hot-storage target. A changed cap is applied on next service start through SQLite `max_page_count`; existing records are retained.

## Before the 24-hour Production trial

Human approval and an isolated runtime location are required. Confirm the current lobby is still advancing; inventory all clients on the same egress IP; set shared read and waiter reservations from that inventory; check the 24-hour trial caps against actual filesystem free capacity including Spool, Archive, raw, WAL, Recovery staging and control; provision process isolation and resource bounds; then start the other eleven Rooms without changing lobby. Observe actual per-Room cursor, response freshness, Archive lag, GAP and free capacity. None of these Production actions has been performed by this repository change.

## Historical V2 release recovery

The existing lobby artifact is independent of this source checkout. The fixed V2 release manifest intentionally rejects the changed Core in this branch, so a fresh V2 stage from current `main` fails closed. An offline readback of commit `cb1d62a229179f5ba222b14349f45ec3bb5a483b` verified all 36 manifest entries with the historical `verify_source` routine. If a new historical V2 attempt is actually needed, use a clean checkout of that reviewed commit, verify its manifest and exact release commit, and follow the existing V2 Human Gate and state-preserving recovery procedure in `capture-upgrade-v2.md`. Do not rebuild it from current Core or reuse an old Attempt. This documents a source recovery path; it is not a Production recovery exercise or approval.

For the SQLite update, lobby needs a new, separately reviewed V2 release/image and a new Attempt through its existing state-preserving gate. The new Multi-Room image is not a drop-in lobby image: its entrypoint and source are for the new Room CLI, while the V2 release manifest pins different bytes and builds offline from a locally available base. A minimal lobby plan is to add the same pinned SQLite library and source-ID probe to a new V2 image release, leave the existing lobby mounts/state untouched, verify disposable copies of its Spool and Archive manifest with both versions, then use V2 staging/activation and rollback checks under the Production Human Gate. Retain all old Attempt and Evidence files. This engineering task does not perform that transition.

## Remaining SPEC gaps

The important poll raw is retained in hot Spool but is not yet copied into Archive shards or an external transfer unit. Recovery export raw is not retained by this increment. The shared read ceiling relies on a fresh, accurate reserve for outside clients; it cannot coordinate with processes outside the new budget directory. Metrics history, bounded automatic restart, Discord notifier, verified external archive transfer and safe pruning are deferred. Actual 24-hour traffic, 10-day capacity and Production behavior remain unverified.
