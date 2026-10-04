# Component / Repository Map

This file is a routing table, not a status dashboard.

The Project is now organized as one monorepo. Legacy repository names below identify provenance; current work starts from the Component entries and current monorepo workstreams, without requiring a legacy GitHub repository to remain available.

A clean Public snapshot intentionally does not copy the Private engineering archive's Issue / PR / Actions history.
Issue / PR numbers below are provenance identifiers unless the corresponding Public object is explicitly recreated.

| Component path | Legacy repository (provenance) | Role | Lifecycle | Trust / Capability class | Canonical entry | Current pointer |
|---|---|---|---|---|---|---|
| repository root | `flop-agent-project-control` | Project routing / shared rules / coordination | ACTIVE | No Secret vault; Project-level control plane | root README + AGENTS; Private archive Issue #1 for maintainers | Repository docs / current public Issues |
| `components/observer` | `technocore-observer` | Technocore observation / capture | ACTIVE | Production runtime exists separately; GitHub != runtime truth | Component AGENTS + README + SPEC | Private engineering Issue #32; fresh Runtime Evidence for Production questions |
| `components/did` | `technocore-did` | Technocore DID tooling / local writer extension | ACTIVE | Signer-adjacent; upstream drift requires deliberate reconciliation | Component AGENTS + README | No automatic upstream reconciliation |
| `components/signer` | — | Project DID custody / deterministic signing policy boundary | ACTIVE | Secret / Signer / trust-boundary work is Human-gated for Real use | Component AGENTS + README | Private archive Issue #6; not copied into a clean Public snapshot |
| `components/analyzer` | — | Read-only analysis of stored Observer Evidence (situation / opportunity / protocol state / findings / report drafts) | ACTIVE | Read-only on Evidence; no external write / signer / wallet; Real LLM and Production run are Human-gated | Component AGENTS + README + SPEC | Component Issue / PR |
| `components/collaboration/scout` | `collaboration-scout` | Collaboration candidate discovery / Scout | ACTIVE | Read-oriented; no Project-wide authority | Component AGENTS + README | Feature work PAUSED; fresh Private FLOP Issue required to resume. Legacy #20 / #24 are historical implementation / review records |
| `components/collaboration/worker` | `collaboration-worker` | Human-supervised bounded collaboration work | ACTIVE | Provider / credential-adjacent work is Human-gated | Component AGENTS + README | Private engineering Issue #28; Issue #8 owns the Evidence-semantics blocker |
| `components/collaboration/agent` | `collaboration-agent` | Controlled agent-to-agent collaboration runtime | ACTIVE | DID / tclk / external-write boundary may apply | Component AGENTS + README | Private engineering Issue #29; completed Issue #9 records First Real Paper ACCEPT and selected signer-path reuse |

## Lifecycle Values

Use:

- `ACTIVE`
- `MAINTENANCE`
- `SUPERSEDED`
- `ARCHIVED`

Legacy repositories should not be archived until the monorepo cutover, open-PR disposition, and basic CI validation are complete.

## Adding a Component

Add a durable directory only when it has a distinct responsibility and local boundary.

Prefer:

```text
components/<component>/
  README.md
  AGENTS.md
  src/
  tests/
```

Do not create a new Repository merely because a new Agent exists. Split into a separate Repository later only when lifecycle, ownership, security boundary, or deployment boundary materially requires it.

## Legacy repository state

The five pre-monorepo implementation repositories are **SUPERSEDED** by the Private canonical monorepo (`akiaki524/FLOP-private`). `akiaki524/FLOP` is the separate distribution repository:

- `technocore-observer` → `components/observer/`
- `technocore-did` → `components/did/`
- `collaboration-scout` → `components/collaboration/scout/`
- `collaboration-worker` → `components/collaboration/worker/`
- `collaboration-agent` → `components/collaboration/agent/`

Repository retention, archive, and deletion are separate Human decisions. Selected pre-migration source / decision records are retained separately by the maintainer; they are historical evidence, not current engineering authority. Exact imported source SHAs are recorded in [MONOREPO_MIGRATION.md](MONOREPO_MIGRATION.md).

The former `flop-agent-project-control` name redirects to the canonical repository; it is not a separate legacy implementation repository or a cleanup target.
