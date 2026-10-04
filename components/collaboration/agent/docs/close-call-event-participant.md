# Close Call Event Participant｜LLMなし

Controlling Issue: FLOP #29  
Initial implementation: PR #30 / merge `776cb8940e45c3fed89f0cd4166e4223f95486e3`

## Implemented registration slice

Goalは、現在Liveの `Close Call / close-1` にProject DIDで参加できるLLMなしCollaboration Agentを作ること。

PR #30では、将来の自動売買Policyまで固定せず、まず次を実装する。

```text
official launch / referee verification
→ owner registration observation
→ exact owner registration payload
→ typed registration SignRequest
→ mint evidence observation
```

PR #30ではReal External Writeを行わない。

## Official baseline

Current Official source:

`flop-labs/technocore-close-call-challenge`

Frozen rules baseline:

- rules commit: `66c1da36538e4b1c685417d2f66922906b13fea0`
- contest: `close-1`
- trading room: `close1`
- lock sweep: `2556`
- package manifest SHA-256:
  `bae09812e25eb6f1369c611f24964f7ea0acafddfc45301a16f33f941296dafa`

Real use requires a Human-approved launch pin containing the signed seed envelope and referee DID. The adapter independently verifies the room signature, season, package hash and referee room set.

The launch seed mirrored in tests is only a cryptographic/parser test vector. It is not authority for Real activation.

## Registration read path

Owner-registration readiness deliberately reads only:

- `d-close1-flow`
- `close1`
- public room-owner notes for the five official referee rooms

It does **not** depend on:

- current price freshness
- positions / pnl / state
- community negotiation rooms
- trading strategy

Therefore a stale market reference or unavailable community room cannot block owner-registration planning.

## Registration states

The adapter reports:

- `READY_MINTED`
  - Project DID is explicitly present in an authenticated referee `mints` list.
- `REGISTRATION_OBSERVED`
  - the exact signed owner record is visible, but mint evidence is not.
- `UNKNOWN`
  - neither is currently observable.

There is intentionally no `UNREGISTERED` conclusion from recent-room absence.

Live public evidence shows mint lists may be omitted/truncated, and `close1` has rolling history. Therefore current absence cannot prove historical non-registration.

## Owner payload

Exact canonical payload:

```json
{"t":"owner","season":"close-1","key":"<Project DID>"}
```

The Agent emits one typed request:

`CLOSE_CALL_OWNER_REGISTER`

It contains the fixed DID, room, contest/rules/package binding, nonce ownership statement, exact UTF-8 preview and SHA-256.

The Agent does not receive a seed/private key and exposes no arbitrary-byte signing API.

Policy Signer support for `CLOSE_CALL_OWNER_REGISTER` is now merged via PR #35
(merge `3246b7891d5d6d032a2ed900f77fa1ca30110eaa`).
That capability remains bounded to owner registration and does not add Close Call trade signing.

## First Taker Long planning slice

The Agent now accepts one Human-selected local maker packet with exactly:

```json
{"terms":{"id":"…","maker":"did:key:…","px":"…","qty":"…","side":"sell","taker":"any","until":1236},"maker_sig":"…"}
```

`first-trade-plan` is read-only. It verifies the exact official terms shape and
the maker signature over `close-1|terms|<canonical terms>`, requires a different
maker DID with side `sell`, and accepts only `taker: "any"` or the Project DID.
It then verifies the approved launch pin and reads only authenticated referee
posts from `d-close1-price` to select the latest sweep.

The plan requires the trade price inside the published limits, an available
next sweep, and `until >= current sweep + 1`. It exposes the underlying reference
trade timestamp and exact age. An age over the official five-minute sweep cadence
is marked stale and warned about; it is not hidden, because the official rules
say the last reference stands when a fresh trade is unavailable. Missing,
signature-invalid, malformed, or conflicting latest price evidence stops the plan.

Risk output uses `Decimal` and shows price, quantity, notional, base 1% fee, and
base required funds. It explicitly warns that actual clawback can exceed the base
1% depending on the sweep close. Mint and available funds remain `UNKNOWN`; a
plan does not claim that settlement will succeed.

The one future Signer request is `CLOSE_CALL_TAKER_LONG`. It binds the Project
DID, frozen contest/rules/package/room, exact canonical terms and digest, maker
DID/signature, authenticated sweep/reference/limits/age, Long direction, SELL
maker side, and Signer-owned room nonce. Its atomic operation is:

```text
taker countersign
→ construct exact final trade JSON
→ allocate close1 nonce and sign the close1 room envelope
```

The Agent neither accepts arbitrary signing bytes nor signs or posts anything.
No Signer implementation is included in this slice.

## Official trade primitives

The module retains only pure official-protocol helpers for later work:

- exact Decimal parsing
- canonical terms serialization
- maker signature preimage
- taker signature preimage
- final official trade JSON shape
- official ±5% / signed-limit checks

These helpers and the planning slice do not discover counterparties or choose
when, why, or how much to trade. The maker packet remains Human-selected.

## Deferred

Not part of this PR's active execution path:

- `close1-offers` or any other community negotiation convention
- offer ranking
- automatic maker/taker selection
- momentum or other trading strategy
- quantity / notional risk policy
- trade-id generation policy
- trade execution/reconciliation CLI
- Close Call-specific persistent audit/state

These should be selected only after registration/mint evidence and current event observations justify the next slice.

## CLI

```bash
PYTHONPATH=src python3 -B -m collaboration_agent.close_call_cli \
  --launch-pin /path/to/human-approved-close1-launch-pin.json \
  status

PYTHONPATH=src python3 -B -m collaboration_agent.close_call_cli \
  --launch-pin /path/to/human-approved-close1-launch-pin.json \
  register-plan

PYTHONPATH=src python3 -B -m collaboration_agent.close_call_cli \
  --launch-pin /path/to/human-approved-close1-launch-pin.json \
  first-trade-plan --maker-packet /path/to/human-selected-maker-packet.json
```

No CLI command signs or posts. `status` and `register-plan` retain their existing
registration behavior; `first-trade-plan` does not propose owner registration.

If current evidence is `READY_MINTED`, no registration action is proposed.

If it is `REGISTRATION_OBSERVED`, the CLI stops at Human review rather than proposing a duplicate registration.

If it is `UNKNOWN`, the CLI can show the exact registration signing plan, but explicitly states that current public history cannot prove the DID has never registered.

## Current event state / next blocker

Project Event Issue #27 records that the current Project DID owner registration is already **DONE**
with signed readback from `close1`.

Current unresolved evidence is the per-owner 10,000 POLF mint/readiness state; it is not directly confirmed.
Current public flow/history may not provide a practical per-owner proof once the relevant records are omitted or aged out.

```text
owner registration                 DONE
signed readback                    confirmed
Signer owner-registration support  merged / dormant
→ inspect mint/readiness as far as Current Runtime evidence allows
→ if direct proof remains unavailable, preserve UNKNOWN and design the minimum first-trade slice around that uncertainty
```

Do not treat the merged registration capability as authorization to register the same DID again.

PR #35のpost-merge Independent Reviewでは、owner-registration capability自体はboundedで妥当、
code必須修正なしと確認されています。ただし既存registrationはSigner外で完了しており、
fresh `close1` historyだけでは過去登録を確実に検出できません。

そのため、Close Call owner-registration capabilityは**休眠扱い**とします。
mintされていないことを示す具体的Evidenceが出て、Humanが再登録を明示判断した場合だけ、
Signer側でClose Call policy/stateを用意して再利用します。

Do not wait indefinitely for evidence the public event surface may no longer expose.
The first-trade plan preserves mint/readiness as `UNKNOWN` and hands off only a
typed, fully bound future Signer request. Remaining Real blockers are the matching
Signer capability, Human approval, one Real post, and next-sweep settled/void
reconciliation.
