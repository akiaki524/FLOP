# Host checkpoint recovery and disposable systemd rehearsal

Base: `027c3d80ff611a12d62a89b4077fe8e5b5644341` (clean at start).

## Cause and repair

The old setup, smoke updater and crash verifier unconditionally reset failed state
on units which systemd could already have garbage-collected. An unloaded unit can
make `systemctl reset-failed` exit 1. The old mock returned success regardless.

`setup.reset_failed` now queries LoadState/ActiveState separately for every unit.
Only loaded/failed is reset. Loaded/inactive, loaded/active and not-found/inactive
need no reset. Transitional, invalid and unknown states refuse recovery. A failed
query is not silently ignored; only an explicit not-found/inactive reply can
represent an absent unit. Every external host command through `setup.run` has a
fixed operation code; stdout/stderr from failed commands is never reflected.

`verify.py crash-recover` uses the same `recover_smoke.recover` transaction as
post-smoke interruptions. It verifies the dummy fingerprint before killing the
Signer. No `.mjs`, systemd service sandbox, credential lifetime or signer protocol
was changed.

## Recovery contract

Recovery validates accounts, original dummy fingerprint, exact DUMMY_OFFLINE
config, probe/unit bytes and overrides, source/runtime/node digests, and file
ownership/modes. It closes sockets before stopping services, rechecks stopped
state, and refuses any lock whose PID is still alive. Dead locks are moved to a
root-only evidence directory after boundary remeasurement and successful reset.
Ledger, quota, custody ciphertext, pointer and nonce witness are never reset,
deleted, rolled back or copied into a fresh identity. The original Signer validates
its durable state cryptographically when it starts. Invalid recovery closes ingress.

Evidence includes before/stopped/after file hashes, archived dead locks, and a
sanitized failure record for interrupted transactions. Interrupted forward source
updates still resume; installed `.mjs` digests are unchanged by this patch.

Known limits are deliberate:

- Unknown files, pending custody writes, incomplete custody and live/PID-reused
  locks fail closed and remain for diagnosis.
- A ledger created before admission is preserved in reconciliation mode; the
  host checkpoint reports `RECOVERY_UNADMITTED_PRESERVED`. It does not restore a
  signing session or erase identity state to get a green result.
- A partial install can resume once source, config, manifests and all units are
  written, even if daemon-reload/start never ran. Earlier unknown incomplete
  installation is refused.
- A clean retry means a **new disposable distro and new dummy identity**, never
  release of quota for the old identity.

## Repeatable rehearsal

Use a newly installed Ubuntu 24.04 WSL distro named exactly `collab-rehearsal`.
Do not export/clone the current host: that could copy unrelated secrets and the
host credential key. WSL creation/root testing/removal requires separate approval.
The user approved this bounded lifecycle for this task.

From a clean committed checkout, build the allowlisted payload:

```sh
python3 -B deploy/connection/rehearsal_bundle.py /absolute/path/to/node22 /tmp/collab-rehearsal.tar
```

It includes committed files, the pinned official runtime, public candidate JSON
and Node22. It excludes the rest of `.local`, home directories and credentials.
The tar contains `rehearsal-source.json` with commit and Node digest.

Create the new environment from Windows or the existing WSL interpreter:

```text
wsl.exe --install Ubuntu-24.04 --name collab-rehearsal --no-launch
```

Transfer the tar through stdin to
`wsl.exe -d collab-rehearsal -u root --exec tar -xf - -C /opt/collab-rehearsal`
after creating that directory. This avoids a shared home or exported host image.
Run, inside the dedicated distro:

```sh
cd /opt/collab-rehearsal
python3 -I deploy/connection/rehearsal.py /opt/collab-rehearsal/node22
```

The harness refuses root operations unless PID1 is systemd, the distro has the
exact disposable name, and no previous Collaboration installation exists. It
creates an isolated Human UID19000, then executes setup, interrupted install,
update/recovery, a real start-limit failure, unloaded reset failure, socket stop/GC,
daemon-reload, smoke, permissions, crash/recovery, and a second checkpoint.
Capture `.local/connection/rehearsal.json` and root
`/var/lib/collab-connection-setup/host-checkpoint-*.json` before destroying it.
Repeat from a newly created distro for a clean retry.

```text
wsl.exe --terminate collab-rehearsal
wsl.exe --unregister collab-rehearsal
```

This deletes only the named disposable distro. Observed disk budget: approximately
2–5GB plus a 129MB source payload. No existing distro is cloned or reset.

In this WSL build, starting the new distro removed the shared `WSLInterop` binfmt
registration and ordinary `.exe` execution then returned `Exec format error`.
No persistent host OS setting was rewritten. The existing interpreter worked with its
preserved-argv form:
`/init /mnt/c/Windows/System32/wsl.exe /mnt/c/Windows/System32/wsl.exe ...`.
Windows-side WSL commands are another option. After the test, the user separately
approved a narrowly scoped root restore of the missing transient binfmt
registration, using the exact value embedded in the installed `/init`. Normal
Windows executable invocation was then verified restored. Do not globally restart WSL while
unrelated workloads are running merely to repair this convenience.

The rehearsal uses the same WSL kernel and Ubuntu24.04/systemd boundary, but a
fresh OS patch level, new host credential key, synthetic sudo UID and no preexisting
host workload. It is not proof that the existing Human installation matches the
reviewed bytes; that remains the final checkpoint's purpose.

## Validation status

P0-1–P0-3 and P1-1–P1-4 are CLOSED for the documented DUMMY_OFFLINE scope.
Claude Code (claude-sonnet-5) found zero blockers in the core review, then confirmed
GO for the DUMMY_OFFLINE Human checkpoint after reading the final fresh-run
artifacts. Real Secret and External Write remain NO GO.

The final fresh-distro run used commit `04862d169a4d6a979cbc567178c5290d9e1356a6`,
Node22.22.0, WSL kernel6.6.87.2 and systemd255.4-1ubuntu8.17. The host and rehearsal
systemd versions are identical. All seven recorded steps and both checkpoints
passed. The dedicated distro was then removed and its absence verified; dummy
credential/custody contents were not exported. Only sanitized evidence remains.

| Verification | Result |
| --- | --- |
| Bug reproduction with GC model before repair | 3 errors / 10 tests, unconditional reset exit1 |
| Signer | 91/91 PASS (included in regression108) |
| Approval | 19/19 PASS |
| Custody/restart | 12/12 PASS |
| IPC | 4/4 PASS |
| Regression | 108/108 PASS |
| Boundary / host orchestration / recovery / systemd model | 19 / 7 / 16 / 5 PASS |
| Rootless four-role smoke/crash / startup / ledger | 1 / 2 / 4 PASS |
| Fresh filesystem install with injected pre-reload failure and recovery/update | PASS |
| Real failed/start-limit reset, unloaded reset, socket GC, daemon-reload | PASS |
| First real checkpoint | smoke13, recovery3, permissions, crash: PASS |
| Second real checkpoint | reconciliation, permissions, crash, recovery3: PASS |
| Historical Evidence | 11,946 checked, 0 changed |
| Real Secret access / protocol External Write / push | 0 / 0 / 0 |

The initial real run exposed a second-checkpoint transport activation omission,
which was fixed before the final run. Fresh WSL startup also exposed manager
initialization timing. The synthetic start-limit check initially assumed
`Result=start-limit-hit`; actual systemd255 retains `exit-code`, so the harness
now observes the exact start-limit journal event for its isolated false-service.
Raw journal messages are not persisted in evidence.

Remaining conservative refusals are described above. The review also notes that
a cleanup stop failure can mask an earlier operation code, and a degraded manager
is refused. Neither was classified as blocking.

See [machine-readable evidence](host-recovery-rehearsal-20260922.json) and
[Claude's independent review and final confirmation](host-recovery-claude-review-20260922.md).
No Human host checkpoint was rerun during this repair.
