# Host checkpoint smoke failure repair — 2026-09-22

## Scope and observed cause

Baseline: `c608daf`. Initial worktree clean. DUMMY_OFFLINE only; no Real Secret access,
Technocore/tclk write, Real ACCEPT/Delivery/REVEAL, or push.

The installed `.mjs` files and Node binary matched the repository/source binary.
The installed signer unit had no drop-ins. Host read-only measurements confirmed
Signer UID 995, state mode 0700, root-owned deployment/config, `NoNewPrivileges=yes`
and `RestrictAddressFamilies=AF_UNIX`. Agent sudo access requires a password; no root
service update or root checkpoint was executed by the agent.

A temporary deployment of the baseline, with inherited Unix socket FDs, the same
Node permissions and a real AF_INET/AF_INET6 seccomp denial, reproduced the startup
failure. `PilotLedger` creates its empty ledger directory and calls `fsyncDirectory`
on its parent before creating an identity file or lock. The call to `fsyncSync`
raises `ERR_ACCESS_DENIED` with Node 22.22.0's Permission Model enabled.
Adding a subtree glob does not solve it: this API is unconditionally disabled.
The original top-level `await createConnectionRuntime()` ran before the fatal
handlers were registered; the uncaught startup exception exited with status 1,
while systemd discarded stdout/stderr as configured.

Primary implementation references:
[Node 22.22.0 fsyncSync](https://github.com/nodejs/node/blob/v22.22.0/lib/fs.js#L1234),
[owned FileHandle.sync](https://github.com/nodejs/node/blob/v22.22.0/lib/internal/fs/promises.js#L788).
The owned FileHandle API was measured successfully for both file and directory
barriers with the original scoped path permissions. No internal binding, permission
toggle, native addon, child process or broader path grant is used.

This identifies the failure in a matching local reproduction. The original host
journal did not contain the exception, so it is not claimed to have been recovered
from that journal. The final host run remains the confirmation of the installed fix.

The first complete four-service test exposed a second, previously unreachable smoke
failure: `INVALID_CONTEXT` at admission. The official offer/accept context contained
one optional `undefined` value (zero non-plain objects), which the store deliberately
rejects. The Signer adapter now takes the actual JSON snapshot before passing it to
the unchanged validator. Secret-field rejection is retained. A fixture-only snapshot
first demonstrated that this resolves admission and permits the complete dummy smoke
and crash/recovery flow; the final test uses the production fix without that workaround.

## Fix and boundaries

- The service uses `AsyncPilotLedger`; the legacy TEST supervisor retains its
  synchronous API. Both share ledger validation and state-transition rules.
- Custody writes, nonce reservations and lock barriers use awaited owned
  `FileHandle.sync()` operations. File sync, atomic replace, directory sync and
  readback remain required. Custody mutations are serialized; failed persistence
  remains fail-closed. There is no rollback or automatic action retry.
- Signer awaits ledger ACK, custody initialization/readiness and nonce reservation
  before returning success or signing. Graceful shutdown closes ingress and waits
  for in-flight requests before closing custody and ledger.
- Startup handlers are registered before initialization. Diagnostics contain only
  a closed set of phase/code values and an invocation identifier, saved mode0600
  within the existing signer state grant. Exception text, paths, stack, secret,
  signature and preimage are never logged. stdout/stderr stay disabled.
- `host_checkpoint.py` routes the existing installation to `recover_smoke.py`.
  Recovery verifies the baseline/current code, manifest, pinned runtime, Node,
  exact units, absence of overrides, accounts/groups, dummy fingerprint and probe.
  Only empty signer state or the observed empty mode0700 ledger directory is
  accepted. Initialized ledger/custody, locks and unknown contents are preserved
  and refused. Known diagnostics and original changed files are archived.
- Recovery closes sockets before services, checks state again and atomically
  updates only reviewed source/manifest. Interrupted forward updates are supported.
  It preserves encrypted dummy credentials and identity, reruns the OS probe,
  then clears the dedicated StartLimit failures and opens sockets. A failed probe
  leaves ingress closed. It does not delete the partial installation.
- Crash verification kills the dummy Signer, stops activation before dead-owner
  lock inspection, removes only the two matching dead-owner locks, and restarts
  through an IPC request. It verifies recovered status and unchanged custody/nonce
  storage. Live NoNewPrivileges and active socket checks are included.

Service unit bytes and Node grants are unchanged. UID separation, AF_UNIX restriction,
child/worker/addon denial, credential pin, state ACLs, ProtectSystem and output
boundaries remain in force. No dependency was added. Test fixtures substitute paths
only in temporary copies; production has no path override or test-mode bypass.

## Verification and host gate

| Validation | Result |
| --- | --- |
| Baseline restricted startup | Reproduced `ERR_ACCESS_DENIED` at `fsyncSync` |
| Current restricted standalone / FileHandle barriers | 2/2 PASS |
| Four-role real Unix IPC, dummy smoke, SIGKILL, recovery | 1/1 PASS; initial smoke 13 checks, recovery smoke 3 checks |
| Reversed Gate/Worker inherited FD order | PASS in four-role test |
| Dummy absent from env/argv; NoNewPrivs; silent service output | PASS in temporary four-role test |
| Signer integration | 91/91 PASS |
| Approval | 19/19 PASS |
| Custody / nonce / restart, including concurrent reservation | 12/12 PASS on host, unprivileged temporary data |
| Existing IPC | 4/4 PASS on host, unprivileged temporary sockets |
| Async ledger permissions / barrier faults / concurrency | 4/4 PASS |
| Existing OS probe / setup recovery | 18/18 PASS; systemd operations mocked, seccomp real |
| Smoke-update preservation / refusals / interrupted update | 10/10 PASS; host operations mocked, staging SIGKILL real |
| Checkpoint routing / typed step outcomes / diagnostics / crash orchestration | 6/6 PASS; host operations mocked |
| Regression (includes Signer integration) | 108/108 PASS, 47.802 seconds |

The Python regression's 108 tests include the Signer integration; the inner 91
checks are not added to that test count. New standalone test filenames stay outside
the default `test_*.py` discovery and are explicitly run. The sandbox's Node test
runner collapsed the custody suite to a file-level result; the 12-case result was
confirmed separately on the host. Evidence summaries are under `.local/batch17a1/`
with the `smoke-repair-` prefix.

Real systemd activation, cross-UID credential/IPC DAC and root crash recovery remain
the Human host checkpoint gate. Temporary process tests do not claim these properties.
Actual power loss and hardware durability are not measured by process crash tests.

## Independent review

A separate `codex exec` process, GPT-5.6 Sol / Medium, reviewed the implementation
read-only without the implementation conversation. The first review of `bcfbabc`
reported one P1: a SIGKILL before atomic replace could leave a staging file inside
the strict deployment tree, preventing the next recovery preflight.

The update now stages within the existing root-only setup area, outside the deployed
tree, and verifies the same-filesystem requirement before stopping services. Both
rename directories are synced. A real child SIGKILL test leaves the staging file,
then successfully reruns recovery while preserving that file and all prior state.
The reviewer's runtime tests were limited by its read-only environment: syntax and
two non-writing tests passed, while temporary-directory tests could not start.
Those failures were not presented as product failures or as passing tests.

The independent re-review of `11437b0` confirmed P1 resolved and identified P2:
exit-zero JSON objects did not require positive semantic outcomes. The checkpoint
now checks each step's required fields and exact value types, including every live
UID/NoNewPrivileges/secret-absence result, IPC denials, all smoke checks, and crash
invariants. Empty objects, explicit failure, missing fields, nested failure and
boolean/integer substitution fail closed. Human need not inspect substeps manually
to compensate for this issue.

Final independent review target:
`c81ca1bb97417ec4e4b6e39d945051b991f3de1d`. The separate GPT-5.6 Sol / Medium
review confirmed P2 resolved, producer/schema compatibility, correct step routing,
and no additional blocking finding. Together with the preceding P1 re-review,
the result is **Offline conditional GO**, with only the actual Human host checkpoint
remaining. No extra manual substep inspection is required to compensate for the
reported issues. This is not a Real-use approval or an assertion that host root
measurements have already passed.

Review records: `.local/connection/independent-review-bcfbabc.txt`,
`independent-rereview-11437b0.txt`, and `independent-final-c81ca1b.txt` in the same
directory. The final commit after this reviewed code adds reporting only.
Historical Evidence: 11,946 original files checked, zero changed.

Human command from the same reviewed checkout and original Human account:

```bash
sudo python3 -I $(git rev-parse --show-toplevel)/components/collaboration/agent/deploy/connection/host_checkpoint.py "$(command -v node)"
```

The command preserves old evidence and writes a unique sanitized checkpoint result.
If an unexpected initialized state is present, it stops without resetting it.
Success is only `passed: true` after setup recovery, smoke, live permissions,
SIGKILL recovery and recovery smoke. This is not permission for Real use.
