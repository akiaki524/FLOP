# Project GitHub Operations

This document is the canonical current Project-wide GitHub operating rule for the FLOP monorepo.

Private remains the ongoing canonical development repository; Public distributes
reviewed snapshots. This topology does not change the permissions or Human Gates
below.

The earlier operating model was developed in legacy `collaboration-scout` Issues #3–#19. Those identifiers record historical provenance; this document is the complete current Project-wide operating rule and does not require those Issues for routine work.

## 1. Source Roles

```text
FLOP repository root
= routing / map / shared rules / cross-component coordination

Component directory
= implementation / local docs / tests / additional boundaries

GitHub Issue / PR
= current engineering work / review / approval evidence

Runtime Evidence
= observed service / Production truth

Notion
= Project History / Decision Path

Project Memory
= durable policy / bootstrap pointers
```

Fresh Component / Runtime Evidence overrides stale Project pointers.

Configured / Intended != Observed Runtime.

## 2. Work Routing

### Single-component work

Keep the engineering scope inside the owning Component:

```text
Issue
→ branch / worktree
→ component implementation
→ component verification
→ meaningful commit
→ push
→ PR
→ CI / Review
→ blocker fix / targeted re-review
→ Human merge gate
→ merge
→ post-merge verification
→ operation gate when applicable
```

### Cross-component work

One Issue / branch / PR may cover multiple Components when the change is genuinely coupled.

The PR must identify every affected Component and run relevant verification for each one.

Do not create overlapping writers or opportunistically modify unrelated Components.

## 3. Issue Modes and Permission Boundary

Use a clear mode when it affects Agent behavior:

- `IMPLEMENT`
- `REVIEW-ONLY`
- `RESEARCH / READ-ONLY`
- `PROCESS / DOCUMENTATION`
- `INCIDENT / RECOVERY`

State important permissions explicitly when needed:

- Code Writes
- Runtime Writes
- Commit
- Push
- External Write
- Production Mutation
- Secret / Credential Access
- sudo / root

Unneeded important capabilities default to forbidden.

## 4. Branch / Worktree

Default new branch format:

```text
<type>/<issue-number>-<short-purpose>
```

Common prefixes:

- `feat/`
- `fix/`
- `ops/`
- `incident/`
- `docs/`
- `chore/`

Do not rename historical branches merely for style.

## 5. Commit / Recovery

Use meaningful commits as Engineering Recovery Points.

Avoid cosmetic commit spam and unrelated giant commits.

Do not rewrite useful evidence merely to make history prettier.

Git rollback does not restore external systems, Production state, databases, credentials, or other non-Git effects.

## 6. Pull Request / CI / Review

A PR should make clear:

- what changed and why;
- affected Component(s);
- Capability / Permission changes;
- important boundaries;
- verification actually run;
- review findings;
- verified / unverified;
- worst credible failure;
- rollback / recovery;
- merge gate.

Hard distinctions:

```text
mergeable != approved to merge
CI PASS != Review PASS
merge != deploy
Configured != Observed Runtime
```

Do not mark unrun checks as PASS.

### Findings

- P0 / P1 / P2: resolve before merge
- P3: fix or explicitly disposition with rationale

Important trust / permission / production / signer / value boundaries require Independent Review from a separate coding-agent family per Project policy.

### Pre-operation Exit Condition

For Pilot / Production / Real credential / Real network / other external operations:

- fix concrete blockers that prevent the current Goal;
- P0 / P1 / P2 remain mandatory blockers;
- P3 blocks only when it violates a defined blocker condition or material Human Gate; otherwise disposition as backlog / residual risk;
- "more hardening is possible" alone is not a reason to continue the review loop;
- after Independent Review shows no material blocker, proceed to merge / operation gate;
- prefer Observed Runtime Evidence over hypothetical edge cases after the defined gate is met;
- re-review should focus on blocker closure and regressions unless scope / trust / capability changed.

### Goal / Scope Decision｜目的と変更範囲の確認

Controlling IssueのGoal / Success Criteria / 許可範囲 / 停止条件を基準とし、同じ内容を毎回作り直さない。稼働が目的のTaskでは、PRやTest PASSは中間成果とし、合意した運用上の成功条件をRuntime Evidenceで確認する。

同じ設計上の原因や回避策の累積がGoalを妨げる根拠がある場合だけ、次の変更前に現方式の修正と簡素化・置換を短く比較する。障害件数だけで停止・再設計を決めず、修正・削除・再設計のいずれも既定の正解にしない。

必要な安全境界と既存のPermission / Human Gateを維持し、実装・検証・移行・復旧を含む総負担、人間の作業量、成功条件への到達見込みで選ぶ。重要な判断には、確認済みの根拠・重要な未確認事項・有力な代替案を選ばなかった理由を、既存Issue / PRまたは作業報告へ短く添える。

これは追加の承認段階や毎回の比較表・報告書を要求しない。必要な緊急停止・被害抑制を待たせず、許可済みの低リスク作業はAgentが継続する。権限・リスク受容・重要な方針変更は既存のHuman Gateへ戻す。

指針の効果は既存の作業結果で確認し、不要な差し戻しや検討負荷を増やす場合は短縮・修正する。指針の整備・評価そのものを稼働再開の前提にしない。

## 7. Monorepo CI

Canonical workflows live only under root `.github/workflows/`.

Component-local legacy `.github/workflows/` files are retained as migration provenance but GitHub does not execute workflows from nested Component directories.

Root workflows must set the correct Component working directory and preserve the former safety boundaries.
Do not silently claim a Component is covered by CI until its root workflow has run successfully in the monorepo.

## 8. Merge

Default preference for meaningful multi-commit work: merge commit, preserving useful recovery/evidence SHAs.

Before merge, fresh-check as applicable:

- exact PR head;
- intended files / Component scope;
- relevant tests;
- CI;
- unresolved findings;
- Human Gate;
- rollback / recovery.

After merge:

1. record merge SHA;
2. verify main;
3. verify post-merge CI when configured;
4. if routing materially changed, update Private development repository Issue #1 when available; in a clean Public snapshot, update repository docs / current Public Issue / PR.

Merge alone does not authorize Production rollout.

## 9. Incident / One-shot Operation

Record:

- Observed
- Suspected
- Unknown
- Impact
- immediate safety boundary
- recovery / exit condition

A one-shot diagnostic or repair needs a bounded execution contract when re-execution could be unsafe or misleading.

Use:

```text
Failure
→ Cause
→ Fix
→ Verification
→ Lesson
```

## 10. Human Gates

Project-wide Human understanding is required for important boundaries such as:

- Secret / Credential
- Signer / Wallet
- External Write / Publish
- Production Mutation
- Trust / Permission Boundary change
- major Network capability expansion
- root / sudo
- important Docker authority expansion
- Mainnet / value-bearing operation
- Spending Authority
- Autopilot / automatic recovery
- difficult-to-recover external effects

Concrete approval evidence belongs near the action in the relevant Issue / PR / operation record.

## 11. Information Boundary

Use four practical classes:

- `PUBLIC`
- `PRIVATE`
- `LOCAL-ONLY`
- `SECRET`

Hard rule: **Private GitHub != Secret Vault.**

For commits intended to survive into a Public repository, the Project owner
uses the GitHub no-reply commit address rather than a personal email address.
The repository Public Hygiene workflow rejects the known former personal
address and requires no-reply metadata for Project-owner commit identities.
It does not force external contributors to hide an email they intentionally
publish in their own Git metadata. This is separate from file-content secret
scanning.

Raw Runtime Evidence should not automatically enter GitHub.
Store only the smallest sanitized evidence needed for engineering reconstruction.

Changing Private → Public is a separate Human Publish Gate.

## 12. Project Current Handoff

Use the long-lived Project Current Handoff Issue #1 only when working in the Private development repository and that Issue is available.
In a clean Public snapshot, use repository docs and current Public Issue / PR for Project Current / routing.
Historical Issue numbers are provenance only and do not grant authority to same-numbered Public objects.

Keep it compact:

- Project NOW / priority
- active / paused workstream pointers
- current Project-level blockers
- material Project-level Human Gates
- routing to authoritative Component / Runtime sources

Do not copy detailed Runtime narratives into it.

## 13. Component Boundaries

Every durable Component should keep a local `AGENTS.md` when it has additional trust, capability, deploy, signer, credential, or runtime constraints.

Root rules are inherited; local rules may narrow them.

A new Agent does not automatically require a new Repository.

## 14. Repository / Component Lifecycle

Use:

- `ACTIVE`
- `MAINTENANCE`
- `SUPERSEDED`
- `ARCHIVED`

Repository lifecycle != Runtime lifecycle.

Splitting a Component back into its own Repository requires a concrete lifecycle / ownership / security / deployment reason.

## 15. Expansion Principle

Design for extension without prebuilding it.

When a new Agent / Component appears:

1. give it a durable responsibility;
2. decide whether it belongs inside an existing Component;
3. if distinct, add a Component directory and local boundary;
4. add routing only when useful;
5. split into a separate Repository only when a real boundary justifies it.

Do not add boards, bots, automatic maps, or generalized automation merely because the platform supports them.

## Historical Provenance

The earlier multi-repository operating model was developed and exercised in legacy `collaboration-scout` Issues #3–#19.

Those identifiers are Historical Design / Decision provenance, not current navigation or a promise of continued legacy GitHub availability. This document is the current monorepo operating surface.
