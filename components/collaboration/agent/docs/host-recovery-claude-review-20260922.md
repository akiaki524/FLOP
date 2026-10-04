# Claude Code Independent Review

Reviewed code: `c7a82acbcd3c4df74daf0310239559efb41388b7` against
`027c3d80ff611a12d62a89b4077fe8e5b5644341`.

Reviewer: Claude Code, claude-sonnet-5. Fixed committed snapshot, Read/Glob/Grep only;
no execution or editing tools, custom hooks/plugins/MCP disabled. The following
text is the reviewer’s result, not an implementer-authored review.

**Verdict: no blocking findings in the committed code. GO for P0 and for P1 as scoped, conditional on the fresh-distro clean retry passing end to end.** This is a read-only review with Read/Glob/Grep. I ran nothing, so behaviours below come from reading the source, not from observation.

## P0: reset-failed only on failed units — met
- `setup.py:51-64` runs `reset-failed` only when `LoadState=loaded` and `ActiveState=failed`.
- A `not-found`+`inactive` unit is skipped. Any other state raises `UNIT_STATE_UNSAFE`, which fails closed for `activating`, `deactivating`, and a `not-found` unit that is not inactive.
- It runs before the probe start (`setup.py:127`) and in recovery (`recover_smoke.py:524`), after stop, `daemon-reload`, and boundary re-measurement, and before any lock is archived or any socket started. Ingress therefore stays closed if the reset fails.
- `verify.py` has no `reset-failed` of its own. Its crash-recover path goes through `recovery.recover()` (`verify.py:69`), so it is covered. No blanket reset exists anywhere.

## P1: post-smoke DUMMY-only recovery — met
- **Preserved state:** ledger, custody, and quota files are never deleted, replaced, or rolled back. Only dead-owner locks are moved into evidence (`recover_smoke.py:527-530`).
- **Evidence:** `before.json`, `stopped.json`, `after.json`, and `failure.json` are kept, along with a backup of the diagnostic and of any replaced sources. The `after.json` file also records the state-hash listing.
- **Unknown states fail closed:**
  - Unexpected files in the state directory, or a custody directory without a ledger, are refused (`:201-224`).
  - A malformed lock, or a live or reused lock PID, is refused (`:229-247`, `:481-487`).
  - A source or manifest mismatch, an alternate or overridden unit, a wrong unit byte, or an untrusted path is refused.
  - The state is re-inspected after the stop, closing the gap between the first check and the stop.
- **Failure codes:** they are static. `failure.json` and stdout use the `[A-Z_]+` `RuntimeError` allowlist, otherwise `RECOVERY_OPERATION_FAILED` or `SMOKE_RECOVERY_FAILED`. The diagnostic is checked against fixed phase and code sets and a 256-byte limit.
- **Security boundary:** none is relaxed.
  - Recovery keeps the same unit text, `--permission` flags, and `NoNewPrivileges`.
  - Credentials stay encrypted and are never written in plaintext.
  - The signer only reaches reconciliation mode through its own validation.
  - Starting the passive SERVICES after `resume_status` validation (`:537`) only starts the same unit definitions that the permissions step already expects to be running.

## Observations, none blocking
1. **Failure masking.** The cleanup `stop` calls in the `except` block (`recover_smoke.py:539-540`) run through `setup.run`. If one fails, it raises `STOP_FAILED` and replaces the original code. `failure.json` then records the cleanup code, not the root cause. State is untouched, so this is a diagnostic-quality issue only.
2. **Degraded systemd.** `setup.py:90` uses `is-system-running --wait` with `check=True`. On a `degraded` manager it exits non-zero and setup fails with `MANAGER_QUERY_FAILED`. That is fail-closed. It may bite real WSL hosts that have an unrelated failed unit, so it is a usability risk, not a security one.
3. **Unchecked `after.json` write.** In the `try` block (`:531-541`), `resume_status` gates the start of SERVICES only in reconciliation mode. In pristine mode no post-start status check is done. This is acceptable because the smoke that follows exercises it.
4. **Rehearsal-only coverage.** `rehearsal.py:82-90` exercises `reset-failed` on a failed unit, a start-limit unit, and an absent unit. I did not verify that any test covers a PID-reuse live-lock refusal, or a crash between the `changed` writes and `daemon-reload`. I did not run the tests, so treat this as an unverified assumption.

## Limitations, not vulnerabilities
- `RECOVERY_UNADMITTED_PRESERVED` (`host_checkpoint.py:118-119`) preserves the state and fails the checkpoint. That is the correct fail-closed behaviour.
- The clean retry has to recreate the disposable distro, since there is no same-identity reset.

## Rehearsal sufficiency
The rehearsal covers setup, partial install, failed and start-limit reset, SIGKILL recovery, recovery smoke, and the second checkpoint. That is sufficient for this P1 once the fresh clean retry also passes.

I found no path where recovery deletes or resets quota, custody, or nonce state, or where a real credential or network write is reachable.

## Final evidence confirmation

Final rehearsal source: `04862d169a4d6a979cbc567178c5290d9e1356a6`.
The implementer independently checked `git diff c7a82ac HEAD -- deploy/connection src/collaboration_agent`: only the rehearsal harness changed.

**GO for the DUMMY_OFFLINE Human checkpoint only. Real Secret and External Write remain NO GO.**

**Scope:** I read only the prior review (`docs/host-recovery-claude-review-20260922.md`), `rehearsal-change.patch`, `deploy/connection/rehearsal.py`, `rehearsal-result.json` and `rehearsal-final-checkpoints.json`. I ran nothing. I did not diff the core files against c7a82ac. That they are unchanged rests on your statement and on the patch touching only `rehearsal.py`.

**Fresh clean retry condition: met.**
- `rehearsal-result.json` shows `passed: true` with all 7 steps: `fresh_setup`, `partial_install_before_reload`, `partial_install_recovered_and_update`, `real_failed_and_start_limit_reset`, `stopped_socket_gc_unloaded_reset_and_reload`, `smoke_crash_restart_recovery` and `second_run_recovery`.
- The result records `real_secret_access: 0`, `external_writes: 0`, systemd 255.4, and source head `04862d169a4d6a979cbc567178c5290d9e1356a6`.
- `rehearsal.py:57-61` refuses to run unless it is in a `collab-rehearsal` distro with systemd as PID 1 and no prior install state. That makes the run a fresh-distro run by construction, matching the "recreate, never reset" condition.
- The two host checkpoints (`rehearsal-final-checkpoints.json`) both pass:
  - **First checkpoint:** `state_mode` is `pristine`, smoke is 13/0, recovery is 3/0.
  - **Second checkpoint:** `state_mode` is `reconciliation` with `quota_consumed: true`, recovery is 3/0.
  - **Both checkpoints:** `external_writes` is 0, `real_secret_access` is false, `quota_reset` is false, `state_rollback` is false, and the pointer, custody and nonce are unchanged. Cross-UID read and IPC are denied, all four services have `no_new_privileges`, and the secret is absent from env, argv and journal.

**Final rehearsal change: no blocker.**
- The change is confined to the harness (`rehearsal.py:25-53`). It moves the failure block into `exercise_systemd` and replaces the `Result=start-limit-hit` poll with an exact journal-message match (`<unit>: Start request repeated too quickly.`).
- The journal query is limited to the synthetic `collab-rehearsal-failure.service` unit, `--since` the start time, `-n 100`. It contains no credentials, and only step names are persisted, never raw messages.
- It fails closed. A timeout raises `START_LIMIT_NOT_OBSERVED`, and any other exception collapses to `REHEARSAL_FAILED` in the `main` guard.
- The production `setup.reset_failed` is still what gets exercised on the failed unit, so the P0 gating logic (reset only when loaded and failed) is still tested. The absent-unit check (returncode 1, then a no-op reset) is unchanged.

**Non-blocking notes:**
1. The match depends on systemd's English message text. That is fine for the pinned systemd 255 in a disposable distro, but the harness would need updating if it is used on another systemd version.
2. `--since=@<int seconds>` has one-second granularity. Because the distro must be fresh, stale matches are not a practical concern.
3. The prior observations (cleanup `stop` masking the root cause, `is-system-running --wait` failing on a degraded manager, and no PID-reuse or crash-between-writes coverage) still apply. None of them changed and none blocks.
