# Signer Component Agent Rules

## Objective

このComponentはProject DIDのSecret custodyと署名Policy boundaryを担当します。

GoalはSignerの複雑化ではなく、同じProject DIDを維持したbounded 24H Agent activityを成立させることです。

Private engineering archive controlling workstream: FLOP Issue #6. In a clean Public snapshot where that Issue is unavailable, use this AGENTS.md, README, tests, and Current Source as the controlling local context.

## Autonomous Scope

以下は自律的に進めてよい。

- repository / Current Source調査
- architecture / implementation
- refactor
- offline tests
- synthetic / test key validation
- docs
- local / work-branch commit and normal work-branch push under Project policy

## Non-negotiable Boundaries

Humanの明示承認なしに以下を行わない。

- Real Project DID seed / private keyの取得・入力・利用
- Real Signer activation
- Technocore External Write
- Real ACCEPT / REVEAL
- Wallet / Claim / Faucet / Mainnet / value-bearing action
- Production deployment / mutation
- sudo / root / systemd mutation
- trust / permission / capability expansion

Secret本文をGitHub、Issue、PR、log、fixtureへ保存しない。

## Authorization Design

- 署名許可判断にLLMを使わない。
- Agentの `safe=true` / `paper=true` / `approved=true` 等の自己申告をauthorityにしない。
- generic `sign(arbitrary_bytes)` APIを作らない。
- Typed requestとtrusted source/stateからSigner自身がcanonical payloadを組み立てる方向を優先する。
- unknown / unsupported / unverifiableは推測で許可せずDENYまたはHUMAN_REVIEW。
- AgentがSigner policy、quota、replay state、audit stateを初期化・書換して制限回避できる構造にしない。

## Reuse / Simplicity

`components/collaboration/agent` の既存First Real ACCEPT、exact binding、Final Revalidation、nonce、at-most-once / reconciliation等をCurrent Sourceで確認し、必要なものだけ再利用する。

Historical Gate A/B/Cを理由なく再構築しない。

**Operational First. Sufficient, not Perfect.**

## Verification

Real Secretを使わず、少なくともREADMEに記載したpositive / negative / state-recovery条件をtest keyで確認する。Private engineering archive Issue #6が利用可能な場合はその条件とも整合させる。

正常系だけでDoneにしない。

重要なSigner / trust-boundary変更はReal利用前に別coding-agent familyによるIndependent Reviewを受ける。
