# Public snapshot contents

This snapshot retains the source implementation, synthetic tests, workflow files,
licenses and required upstream notices. Eleven evaluation JSON originals are
excluded from this public file set; the Private originals are preserved.

Seven originals listed in PUBLIC_RELEASE_CHECKLIST.md have separately named
`*.public-summary.json` files with selected numeric metrics, statuses, digests
and structured provenance. Original JSON Pointer locations identify each selected
observation. These summaries omit quotations and response bodies and are not
replay fixtures, valid outcomes, or complete proofs. The seven summaries are retained.

The following four additional originals are excluded without new summaries.
All paths are under `components/collaboration/agent/docs/`:

- `fresh-evaluation-batch7-20260919.json`
- `strict-fresh-evaluation-batch8-20260919.json`
- `read-only-operation-batch12-20260919.json`
- `real-pilot-protocol-batch16-20260919.json`

Private empirical replay and historical verification require original records
and saved internal evidence. In particular, `components/collaboration/agent/tests/batch16_protocol.py` check/seal
commands require the excluded Batch 16 original and internal evidence.
Retained synthetic tests and offline smoke examples can run without those inputs.
Historical COMPLETED counts indicate local processing, not external acceptance
or production readiness. Public availability does not grant additional license
rights; retain all path-specific LICENSE and NOTICE terms.

The generic privacy checks do not establish absence of owner-specific identifiers.
The final release candidate must pass owner-personalized checks.
Publication requires explicit owner approval.
No Private Git history or local configuration is included.
