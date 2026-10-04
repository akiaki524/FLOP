# Monorepo Migration

This is the migration-time decision record. Use [REPOSITORY_MAP.md](REPOSITORY_MAP.md) for current routing; historical repository and Issue identifiers below do not require continued legacy GitHub availability.

## Purpose

Consolidate the FLOP / Technocore Agent Project from multiple implementation repositories into one canonical monorepo while preserving legacy repositories as historical evidence.

## Migration branch

`chore/flop-monorepo-migration`

The migration starts from the former Project Control repository and places each source repository's latest `main` snapshot under `components/`.

## Source snapshots

| Legacy repository | Imported path | Imported main SHA |
|---|---|---|
| `technocore-observer` | `components/observer` | `adba10ebc24f901b368c80e28e297d67ebf0cda2` |
| `technocore-did` | `components/did` | `3f92fdedcdeaf1fe6429cda407aeeaa18be4da0d` |
| `collaboration-scout` | `components/collaboration/scout` | `24f382e3f0f5f0ef84dd329d6b8de1602614441a` |
| `collaboration-worker` | `components/collaboration/worker` | `6f346986f7835efa25dd7be6a6d855aa38ebb277` |
| `collaboration-agent` | `components/collaboration/agent` | `6248034095e7b17c04dcf6df7dda12c8cd81e316` |

Imported implementation files: **478**.

The former Project Control root remains the base for Project-wide documentation and is rewritten for monorepo operation.

## Git history policy

The new monorepo does not rewrite or splice the full independent Git histories of the five legacy repositories.

Instead:

- the imported `main` snapshot becomes the new canonical implementation baseline;
- legacy repositories preserve pre-migration commit / branch / PR / Issue history;
- source SHAs above provide exact provenance;
- legacy repositories should remain available at least through cutover validation.

## Open legacy PRs excluded from the snapshot

These were intentionally **not** silently folded into the monorepo migration:

- `collaboration-scout#20` — implementation + CI changes; open at migration time.
- `collaboration-worker#8` — documentation sync; open at migration time.

Each must later be explicitly:

1. merged before final legacy freeze and then ported;
2. ported as a fresh FLOP PR / commit; or
3. closed with an explicit disposition.

## Legacy Issues

Issue history stays in the legacy repositories as historical design / engineering evidence.

Current work should move to FLOP Issues after cutover. Important legacy Issue content may be summarized or linked rather than mechanically recreated.

## CI migration

Nested Component `.github/workflows/` files are retained as provenance but are not active GitHub Actions locations.

Root `.github/workflows/` contains monorepo-adapted workflows. A Component is not considered fully cut over until its root workflow has actually passed.

## Cutover checklist

- [ ] migration PR reviewed
- [ ] root monorepo CI passes or failures are explicitly dispositioned
- [ ] Scout #20 dispositioned / ported
- [ ] Worker #8 dispositioned / ported
- [ ] root Issue #1 updated to monorepo routing
- [ ] repository renamed to `FLOP` if desired
- [ ] local remotes / Codex Cloud references updated after rename
- [x] legacy repositories marked SUPERSEDED
- [ ] legacy repositories archived only after validation

Do not delete the legacy repositories as part of the initial migration.
