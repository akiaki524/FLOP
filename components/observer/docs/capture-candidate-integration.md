# Minimal Capture Candidate integration

This repository carries the reviewed Capture Recovery Candidate from commit
`0ed90b21656c32d6dc37a97b6eb7600c95f88fb0` as a separate, opt-in Full
Capture subsystem. The adopted source files retain their Candidate bytes. The
[machine-readable provenance](../deploy/capture-candidate-integration.json)
lists the 24 historically reviewed files, the 19 newly adopted files, three
Observer dependencies already identical on main, two excluded notification
files, the carried regression tests, and their one test-only support file. It also records SHA256 values.

The `technocore_observer` core remains a read-oriented Observer. It does not
perform automatic Observer state resync or recovery. The independent
`technocore_full_capture` package includes bounded recovery for Capture
continuity gaps. That code does not authorize Live or Production execution,
state migration, external writes, or access to secrets. Those actions remain
subject to the repository's Human Gates.

The image source contract is `deploy/Containerfile.full-capture` plus its
Docker ignore file and the 17-entry `source-manifest.json`. Candidate runtime
and monitor evaluation are present for offline verification and future V2 use.
The existing `monitor.py` imports `packet.py`, `policy.py`, and `readonly.py`.
Importing those reviewed files does not adopt `packet.py`'s historical stage,
standing-package receipt, fixed-name stop, or lifecycle commands as current
Production authority.

The Candidate Monitor uses exactly nine `packet` attributes. V2 may reuse
`read_json`, `write_new`, `sync_dir`, and `require` as bounded file/validation
helpers. Its adapter **must replace** the other five:

| Attribute | Why V2 must replace it |
| --- | --- |
| `mounted_check` | Checks V1 fixed mounts and loop devices, not V2 mount identity. |
| `stop_roles` | Uses V1 role/fixed-name stop semantics, not V2 exact container IDs. |
| `receipt` | Treats the V1 standing receipt and bundle hash as authority. |
| `require_host` | Requires V1 root access on `node-01`. |
| `E` | Points to the V1 `/etc` state path. |

The Candidate Monitor still queues a notification event after a stop. V2 must
suppress that old side effect through its bounded adapter. The legacy
`packet.py` and `monitor.py` `__main__` entrypoints **must not be run directly
as Production operations** from this minimal integration. This tree is not a
complete standalone V1 Production packet: old stage, mount, probe, and Notifier
files are intentionally absent. V2 may use only the evaluation/helper surface
through its bounded adapter. This integration contains no V2 adapter or
deployment implementation. The old Pilot, Notifier, rollback helpers, Analyzer,
and historical execution evidence are outside this integration. No Production
readiness or GO decision follows from it.

The source Candidate is anchored by annotated tag `capture-candidate-0ed90b2`,
which peels to `0ed90b21656c32d6dc37a97b6eb7600c95f88fb0`. To recheck
one adopted file's source bytes, run
`git show capture-candidate-0ed90b2:<path> | sha256sum` and compare the digest
with its entry in the provenance record. The integration test checks the
adopted working-tree bytes against that record.
