# Capture Upgrade Harness V2 — current offline contract

This document records the V2 implementation contract. **It does not establish current Production state or authorize Production operations.**
Current requirements: [SPEC.md](../SPEC.md). Current Private engineering: FLOP Issue #32; Private Issue #7 records the superseded V2 restart workstream. These Private Issue objects are not copied into a clean Public snapshot.
The detailed design checkpoint is `915b02c40dde26e42035929edc6789902af50ef7`
(`capture-upgrade-v2-design-20260923.md`, v0.2). This document reconciles it
with legacy Observer PR #5 and Issue #4 as historical decision records, rather than current controlling objects. The checkpoint's old D1 branch/base proposal,
its `require_host`/`E` delegation, `90-tc-v2` drop-in name, and unresolved
D2–D4 table are superseded here.

## Source and authority

V2 starts from current `main`, where the minimal Candidate integration is
merged. Source Candidate `0ed90b21656c32d6dc37a97b6eb7600c95f88fb0` is
anchored by annotated tag `capture-candidate-0ed90b2`; it need not be a `main`
ancestor. Candidate source and manifest bytes remain pinned by
`deploy/capture-candidate-integration.json`. The V2 release allowlist is
`deploy/production-capture-upgrade-v2/release-manifest.json` (22 existing
Candidate/common files plus 12 V2 Python files). Stage verifies a clean exact
release checkout, each source hash, then copies and rechecks those files in a
new root-owned Attempt directory. Activation rechecks staged bytes. A Production
release commit is selected by Human review; a CLI flag does not grant approval.

V1 Harness and historical evidence are frozen. V2 calls only Candidate
`packet.profile`, `packet.prod_mounts`, and `packet.check_container` as pure
container contract helpers. It never calls the V1 packet stage, lifecycle,
receipt, fixed-name stop, or legacy `__main__` paths. Notifier/Discord and 24h
observation orchestration are outside the deployment transaction.

## Attempt and evidence

Attempt ID: `YYYYMMDD-v2-NN`. New files live only under:

- `/opt/technocore-capture/upgrade-v2/<ATTEMPT_ID>/` — verified release copy;
- `/etc/technocore-capture/upgrade-v2/<ATTEMPT_ID>/` — immutable Attempt evidence;
- `/srv/technocore-capture/control/observation-v2-<ATTEMPT_ID>/` — Monitor evidence;
- three `99-zz-tc-v2-<ATTEMPT_ID>.conf` drop-ins, created only during Activation.

Evidence is O_EXCL and never overwritten: `attempt.json`, `staged.json`,
`activation-begin.json`, `activation-started.json`, and `terminal.json`.
An explicit rollback adds `rollback.json`. Stage failure records its partial
IDs in `terminal.json`. Interrupted/ambiguous Attempts require read-only
reconcile; the same Attempt is never auto-resumed, retried, or cleaned up.

## Flow

1. **PRE** reads host, stopped old Writers, running-container writable mounts,
   host-visible writable Capture data FDs (without reading process argv),
   mount identity, network ID, policy bytes,
   resource headroom, SHA256 of systemd effective commands, the explicit
   baseline drop-in path allowlist, and timer state. PRE rejects a baseline
   drop-in whose name would apply after `99-zz-tc-v2-<ATTEMPT_ID>.conf`.
   Raw old unit argv is not persisted. No mutation.
   The old `30-mount-identity.json` is treated as data, without V1 bundle-hash
   authority. All Production use, including PRE, requires the separate Human
   Gate even though PRE is read-only.
2. **STAGE** first repeats PRE. It copies only the allowlist; requires the pinned
   Python base image to be local; builds with `--pull=false --network=none`;
   creates and runs a no-network, no-mount image probe; then creates two
   attempt-labeled Candidate containers **stopped**. It rechecks network ID and
   validates regular, non-symlink `policy.json` bytes before create. STAGE does
   not issue mutating `systemctl` commands, start Writers, alter shared data, or
   write under the existing `/etc/technocore-capture` root outside its V2 Attempt namespace.
3. **ACTIVATE** requires its own Human approval and a fresh read-only baseline
   comparison. It records `activation-begin.json`, quiesces the Monitor timer,
   writes only its three drop-ins, reloads systemd, verifies effective commands
   and `Restart=no`, then starts Capture and Archive in that order by full ID.
   After each Writer start, it polls that exact ID for at most 15 seconds for
   `Running=true`, a positive PID, and no OOM indication. An exited/dead Writer,
   identity mismatch, inspect error, or timeout fails closed. Failed Activation
   evidence retains bounded, selected inspect State fields for diagnosis.
   It requires the Monitor service to be inactive both before and after timer
   quiesce. Effective drop-in paths must equal the PRE baseline allowlist plus
   the V2 switch as the final applied drop-in; a later/foreign override fails
   closed. Monitor `ExecStartPre`, `ExecStartPost`, `ExecStop`, and
   `ExecStopPost` are explicitly reset and must remain empty. Writer
   `ExecStartPost` and `ExecStopPost` are also reset and verified empty;
   Writer `ExecStartPre` is reset to V2 `writer_precheck.py`. The standing
   package is not executed. Candidate Docker `RestartPolicy=no` is verified.
   Monitor is the V2 runner with exact IDs bound in argv.
4. **SHORT SMOKE** checks both exact IDs running without OOM, fresh metrics
   after Activation, Capture messages and Archive `processed_through` progress, and no
   fatal Monitor findings. GAPs remain durable evidence/findings and do not by themselves
   fail Short Smoke or roll back otherwise healthy current observation. It requires at least 240 elapsed seconds
   and three Monitor samples, with a 600-second maximum. On failure before
   acceptance, quiesce V2 Writer units, exact-stop both IDs, then roll back
   only V2-owned switches when Writer units and jobs are settled.
5. **ROLLBACK** stops the Monitor timer, then stops both Writer units while
   the verified V2 drop-ins are active. It exact-stops the bound Docker IDs
   even if unit quiescence fails. It removes only switch bytes matching the
   Attempt hashes after both units are inactive with no pending Writer jobs,
   then reloads and verifies the baseline
   unit settings, and restores the prior timer state. It checks Writer units
   and jobs again before recording `STOPPED_BASELINE`. An unsettled job, active
   unit, changed switch, or failed stop yields `RECONCILIATION_REQUIRED`.
   A `failed` Writer unit with no pending job may be cleared with `reset-failed`
   only after verified unit stop and exact-ID container stop; both units must
   then be inactive before a switch is removed. The failed state is never
   treated as proof that a Docker Writer has stopped.
   It never starts old Writers.
6. **RECONCILE** reads Attempt evidence, exact IDs, switches, units, timer,
   observation and old Writer status. It issues no mutation command or repair.

Normal lifecycle uses systemd. Emergency Stop does not use systemd or fixed
container names. It inspects full Docker IDs and checks image, Attempt/role/
Candidate/release labels and no-restart policy before `docker stop --time 40`
by full ID. Only a complete Docker `No such object/container: <exact ID>` diagnostic
means absent; daemon errors and partial diagnostics remain unknown. Missing,
mismatched, and unknown targets are never replaced by a name lookup. A stale Attempt can address only its own bound IDs.

## Writer metrics history

Production Capture / Archive metrics history is stored per Room and role on
its existing control volume. The legacy `<room>.<role>.metrics.jsonl` is segment
zero; subsequent segments add `.000001`, `.000002`, etc. Before a record would
exceed the provisional 64 MiB `HISTORY_SEGMENT_BYTES` target, the Writer creates
the next segment exclusively. This is a changeable engineering parameter,
not a safety boundary or a total history cap. Records are kept whole; a record
larger than the segment target can occupy an empty segment on its own, subject
to the existing 4096-byte record limit. Segment boundaries do not stop Writers.

Restart selects the highest numbered segment, including an empty file left by
interrupted creation. A torn final line is retained: append adds a separating
newline when it fits, or leaves the old segment unchanged and writes the new
record in the next segment. Files are never truncated, overwritten, renamed,
or deleted. Successful appends fsync the file and control directory under the
existing per-role service lock.

Initialize no longer reserves a fixed total history allocation. Retained history
can grow across segments; existing bounded control-volume, free-byte and inode
checks still apply, and real filesystem/write/fsync errors remain fatal.
Read-only `control_dir` filesystem samples are included in both roles' latest metrics
and sanitized history: `control_volume_bytes` (total), `control_free_bytes`
(available bytes), and `control_free_inodes` (available inodes). These describe
the shared control filesystem, not a per-role allocation or estimated runway.
The existing 16 MiB free-byte and 1024 free-inode protective stop is unchanged. This
implements no external backup or pruning. Deployment and actual Production
behavior require the existing Human Gate; these are repository implementation
semantics, not Runtime evidence.

## Monitor seam

Current `monitor.py` has evolved for Issue #32; historical Candidate hashes
remain unchanged in provenance artifacts. V2 imports and calls `tick()`;
it never runs Candidate `monitor.py` as `__main__`. The closed packet seam exposes
exactly nine attributes:

| Reuse within a bounded helper | V2 replacement |
| --- | --- |
| `read_json`, `write_new`, `sync_dir`, `require` | `mounted_check` (V2 mount/data check), `stop_roles` (exact bound IDs), `receipt` (only V2 `activation-started.json`), `require_host` (V2 root/host gate), `E` (V2 Attempt state path) |

Any extra Candidate `packet` access fails closed. The runner redirects
observation evidence into its Attempt directory and sets gap baseline from
STAGE. It disables `notification_event`, so no old outbox event is queued.
SIGTERM and other Monitor exceptions attempt bounded exact stop from argv-bound
IDs even if mutable state cannot be read. ACTIVATE handles SIGTERM and SIGHUP
as failures: it quiesces verified V2 Writer units, performs exact-ID stop, and
rolls back owned switches only after Writer jobs have settled.
A stop already in progress is allowed to complete within the systemd grace;
there is no blind retry.

Normal Monitor samples have a 10-day VPS retention policy with external backup
normally every 7 days, leaving about 3 days of operational margin. Deletion is
permitted only for the exact range older than 10 days whose external backup has
been verified by readback/hash and range coverage. Age alone never authorizes
deletion. This fix implements no backup receipt consumer, transfer scheduler,
external storage integration, or pruning. `history_retention` in latest state
records 864000 retention seconds, 604800 backup-interval seconds, and explicitly
false verified-backup/deletion flags. Attempt activation time is a conservative
age reference, not a verified sample range. At 7 days it records
`MONITOR_HISTORY_BACKUP_REQUIRED`; after 10 days it records
`MONITOR_HISTORY_RETENTION_BACKUP_REQUIRED`. These findings stay durable in
latest/checkpoint and do not stop healthy writers.

`history-current.jsonl` and `history-previous.jsonl` are each bounded to 16 MiB.
On further rotation the old previous is preserved as a uniquely named
`history-sealed-<time_ns>.jsonl`, using no-clobber hard links with directory fsync
before removing an old name. No historical bytes are overwritten or deleted;
sampling continues beyond two generations and beyond 10 days while backup is
unverified. Consequently each newly produced generation is bounded, but total
unbacked history grows; the 10-day policy is not a total storage cap. Actual
capacity danger remains fatal. Oversized individual samples and local history
access errors (`EACCES`/`EPERM`) become durable findings while latest/checkpoint
continue. Latest/checkpoint failures, invalid file types, capacity exhaustion,
I/O errors, read-only filesystems, and unknown errors remain fatal.

Terminal failure, Capture/Archive protective stop, and major Monitor/capacity
failure summaries are retained independently as first `monitor-terminal.json`
Evidence and current `monitor-terminal-latest.json` status under the Attempt
state directory. The first receipt is never overwritten. The one-time checkpoint
and latched latest findings also stay outside normal-sample retention. Recovery
Evidence, Capture/Archive data, GAP records, Trial/Historical V2 Evidence, legacy
`history.jsonl`, and historical release hashes are not deletion targets. Future
verified transfer must establish each normal-sample range from the stored sample
timestamps and hashes; the filename or policy age reference is not backup proof.

A fatal result always exits 2. Stop outcomes, `stop_confirmed`, `retry_required`,
and `timer_quiesced` are distinct. Durable first/current stop-result Evidence
is saved before quiescing the fixed `technocore-capture-monitor.timer`, and the
observed timer result is saved afterward. No timer stop is attempted without
confirmed exact pair stop and durable Evidence. A failed timer stop is not
reported as success. Stop failure/unknown/mismatch keeps scheduling available
and retries only argv-bound IDs on the next invocation, with fresh identity
checks and no name/role search. Already-stopped exact writers require no repeated
stop or Monitor evaluation. A saved terminal receipt never alone suppresses stop:
subsequent invocations recheck exact identity/state and retain FAIL. Manual stop
reason stays unknown and is never an automatic recovery request. Without a fatal
terminal receipt, an exact pair observed stopped emits structured stdout Evidence
with `verdict=UNKNOWN`, `writers_state=STOPPED`, and
`stop_reason=UNKNOWN_NOT_RECOVERY` (exit 2). It creates no terminal receipt and
issues no writer or timer stop/start/restart. Scheduling remains available for
read-only identity/state observation; a legitimate restart of the same exact IDs
returns to normal evaluation, including its existing fatal findings latch.
Existing fatal terminal receipts still take precedence over this observation path.
No reset-failed,
restart, automatic receipt clearing, or recovery is added.

Actual systemd behavior and external backup remain unverified offline. Timer
stop authority in Production still requires its Human Gate; implementation and
tests do not authorize rollout. Historical release allowlists still reject
evolved source; this patch does not authorize or prepare rollout.

## D2–D4 engineering dispositions

- **D2:** 240 seconds, three Monitor samples, 600-second maximum. Candidate
  evaluation suppresses Capture failures for its first 180 seconds, so smoke
  extends beyond that grace and also requires post-Activation progress.
- **D3:** two serial stops have a conservative worst-case budget of
  `2 × (30s inspect + 55s stop command + 30s re-inspect) = 230s`.
  Monitor drop-in sets `KillMode=process`, `TimeoutStartSec=300s`, and
  `TimeoutStopSec=300s`; effective properties are checked before start. This
  gives the Python SIGTERM handler time to stop exact IDs without systemd
  killing its Docker child simultaneously. Each Docker stop uses `--time 40`.
  Actual systemd behavior and timing remain a Production gate observation.
- **D4:** PRE and STAGE validate the existing `policy.json` with Candidate's
  pure `validate_config` on host-side bytes, require the current time inside
  `start_at` / `end_at`, and retain SHA256 in evidence. The start precheck
  repeats the window and hash comparison immediately before each Writer starts.
  No image execution or external request is needed to decide schema validity.
  The actual Production file has not been examined in this offline task.

## Review and verification

The V2 implementation is in `deploy/production-capture-upgrade-v2/v2/`.
`tests/test_capture_v2_monitor_evaluation.py` locks the Phase 1 F3 evaluation
subset; `tests/test_capture_upgrade_v2.py` exercises authority boundaries
through fakes. No Docker, systemd, Technocore Live, Production SSH, external
provider, or secret was used for this offline verification. Independent Review
and fresh Production evidence are required before any rollout gate.
