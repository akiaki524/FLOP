# Project DID Policy Signer

Project DIDのSecret custodyと署名許可境界を、24H Agentの実行本体から分離するためのComponentです。

Private engineering archive controlling workstream: **FLOP Issue #6**.
A clean Public snapshot does not copy the historical Issue / PR objects; numbers below are retained as provenance identifiers.

## Goal

同じProject DIDを継続利用しながら、Humanが事前に許可した狭い範囲のPaper / no-value活動について、Human不在時でも必要な署名を安全に行える最小のPolicy Signerを作る。

Signer自体の完成を目的化せず、実際のAgent活動を成立させるためのInfrastructureとして扱います。

## Initial Boundary

- Project DID private key / seedをCollaboration Agentへ渡さない。
- 署名許可はdeterministic policyで判定し、LLMを使わない。
- generic `sign(arbitrary_bytes)` capabilityを作らない。
- 初期対象はTechnocore / tclkのPaper / no-valueのみ。
- Wallet / Claim / Faucet / Mainnet / value-bearing actionは対象外。
- ambiguous external resultでblind retryしない。
- capability expansionはHuman Gate。

## Current Development State

Issue #6のoffline Policy CoreとMinimal Real Runtime Boundaryはmainへmerge済みです。

- PR #12: synthetic / test-key Policy Signing Core
- PR #15: Secret custody / process / trusted-read / runtime boundary
- PR #15 merge commit: `b839e365750b5c86e1d1833cf008574c257facc9`
- PR #15 Independent Review + targeted re-review完了
- PR #35: Close Call owner registration capability
- PR #35 merge commit: `3246b7891d5d6d032a2ed900f77fa1ca30110eaa`
- PR #35 Signer CI: PASS
- PR #35 post-merge Claude Code Independent Review: **P0/P1なし / code必須修正なし**
- Current operational decision: Close Call owner-registration capabilityは**休眠扱い**

**Code merged != Real activated** です。

Real Project DID seed投入、Production deployment、External WriteはまだHuman Gateであり、
Observed Runtime EvidenceなしにReal-readyとは扱いません。

Current sourceには、用途を分離した3つのbounded policy profileがあります。

### tclk / GCD profile

```text
signed Paper OFFER
→ Final Revalidation
→ ACCEPT
→ optional INITIAL_HEARTBEAT
→ signed Paper LOCK + Paper note確認
→ GCD result DELIVERY
→ REVEAL
→ exact reconciliation / completion確認
```

Action:

- `ACCEPT`
- `INITIAL_HEARTBEAT`
- `DELIVERY_GCD`
- `REVEAL`

### Close Call owner-registration profile

Close Call / `close-1` のowner registration専用に、

- `CLOSE_CALL_OWNER_REGISTER`

だけを許可します。

このprofileはProject DID / contest / room / rules commit / package hash / lock時刻を固定し、
Signer自身がcanonical owner JSONを再構築します。trade署名Capabilityは含みません。

Current Project DIDのowner registrationはPrivate engineering archive Issue #27でsigned readback確認済みです。
そのため、このprofileは通常運用では**有効化しません**。

Close Call用policy / protected stateをHost上に用意するのは、
「mintされていない」ことを示す具体的Evidenceがあり、Humanが再登録を明示判断した場合だけです。
既存の登録がSigner外で行われたため、fresh `close1` exportだけでは過去の登録を確実に検出できません。

`SIGN_TEXT`等の任意署名APIはありません。

### Close Call Taker-Long profile

Human-supervised の単発署名入口は `src/close_call_one_shot.mjs` です。
既存24H Runtimeとは別に、匿名stdin/FD、Human-managed approval、1 process内の固定GETと
既存PolicySignerを使ってPOST-ready packageまで生成します。POSTは行いません。
利用境界・試行記録・Independent Review条件は
[`docs/close-call-one-shot.md`](docs/close-call-one-shot.md) を参照してください。

`close-call-taker-long/1` は `CLOSE_CALL_TAKER_LONG` だけを許可する別profileです。
owner-registration profileとの相互実行はできません。policyはProject DID、`close-1`、
rules commit、package manifest、`close1`、`d-close1-price`、Referee DID、Signer nonce ownership、
lock時刻、および2署名quotaを固定します。

Agentのtyped requestをそのまま信頼せず、SELL termsとmaker署名を再検証し、Trusted Readerから
current priceと`close1` nonce stateを再取得します。bound price recordがCurrent latestと異なる場合は
`CLOSE_CALL_REPLAN_REQUIRED`で停止します。公式current referenceが古い場合は拒否条件を追加せず、
再計算したage / staleを返却値に明示します。

署名前にrequest digest、Signer採番nonce、2署名分quotaを`RESERVED`としてdurable保存し、
taker countersign、exact final trade JSON、room envelope署名を生成した後に`ATTEMPTED`へ更新します。
成功後も同じoperationは再署名できません。このcapabilityは署名packageを返すだけでPOSTしません。

### Read boundary

Signer自身はHTTP clientを持ちません。

`src/technocore_read.mjs` は、Signerに注入したread-only acquisitionが取得したTechnocore `/r/<room>/export` のraw JSONL bodyと`X-Room-Generation`相当のgenerationを受け取り、以下を検証・正規化します。

`PolicySigner`はコンストラクタで信頼済み`acquisition`を受け取り、署名判断のたびに`export(room)`を呼びます。Agentの`admit`/`issue`/`reconcile`要求からsnapshot、generation、取得時刻、Paper noteを受け付けません。取得口は`{room, generation, body: Buffer, capturedAtMs}`を返し、Signerは各判断時にraw bodyから署名を再検証します。`generation`と`capturedAtMs`はHTTP取得側の値なので、Real利用にはAgentが取得口を偽造・差替できないprocess/IPC境界が必要です。この初期sliceはその隔離を実装していません。

- roomごとのgeneration binding。offers roomはadmit時に観測値をwork stateへpinし、deal roomは未作成のgeneration 0 / 空bodyを許容して、作成後の最初のnon-zero値をpinします。pinは暗号化された同一work stateに保存され、再起動後も維持します。
- byte-complete JSONL
- signed recordのEd25519再検証
- 19桁nonceのlossless保持
- current Technocore replay window相当のnewest 1 MiB nonce observation
- source-order / duplicate sequence rejection

HTTP acquisitionそのものはこの初期sliceには含めません。

Paper noteはTechnocoreの公開CAS noteで、署名されたrecordとは異なります。Signerは注入された`acquisition.paperNote(contract)`から値を取得します。Agentの任意文字列をRealの完了根拠として採用してはいけません。

### One work identity

初期work typeは、signed Paper OFFERの`job.context`に`TEST GCD bundle sha256=<source digest>`を含むものだけです。source bytesはASCIIの`gcd_lcm <positive integer> <positive integer>\n`で、各整数は最大9桁です。SignerがSHA-256とGCD/LCM回答を計算します。Agent指定の回答や`safe`/`paper`/`approved`等の宣言は署名許可に使いません。

1 work、heartbeatありなら4署名、なしなら3署名をpolicyで固定します。受入時とACCEPT直前は`now + acceptMarginMs + minCompletionWindowMs + claimMarginMs < min(claimByMs, policy.expiresAtMs)`を要求します。synthetic GCD pathの`minCompletionWindowMs`初期fixture値は60秒で、ACCEPT後のheartbeat（有効時）・LOCK確認・DELIVERY・REVEAL・completion確認に割り当てる最小の予約時間です。後続署名もclaim期限より前に余裕を要求します。`COMPLETED`はREVEALの署名済み公開recordがexactに1件観測され、対応するPaper claimed noteを確認した**local synthetic**状態です。Realで相手方の作業受領を意味するものではありません。

### Protocol compatibility

初期実装はCurrent Official tclk commitをpinします。

```text
5cc4ab93efbc8999a3a7e1471b639deca25998ea
```

Official signing contractとtclk golden vectorをoffline testで照合します。

### Persistent state

Policy / DID / replay / quota / action stateとREVEAL用preimageは、test seedから導出したkeyによるAES-256-GCM envelopeで保存します。

- atomic replace
- file fsync
- directory fsync
- policy digest binding
- DID binding
- corruption / tamper fail-closed
- 明示的な`initialize: true`がない状態ファイル欠落は`STATE_MISSING`で停止

を行います。

ただし、**process停止中に過去の正しい暗号化snapshotへ丸ごとrollbackされた場合の検出は、このfile単体では保証しません。** Real pilot前の未解決事項です。

`initialize: true`は新しいsynthetic stateの初回作成だけに使います。既存state紛失後にこれを再指定するとquota/replayを再初期化できるため、Real利用前に初期化権限とstateの外部anchor、単一writerの運用境界が必要です。

### Ambiguous result

署名済みEnvelopeは、`ATTEMPTED` stateをdurableに保存した後でのみcallerへ返します。

結果が不明な場合は同じActionを再発行せず、公開snapshot上のexact envelopeをread-only reconciliationします。

Policy expiryは新規署名を停止しますが、既存Attemptのreconciliation / completion確認までは妨げません。

## Minimal Real Runtime Boundary（PR #15 / merged）

Real Project DIDを預ける前の最小Runtime Boundaryはmainへmerge済みです。

現在実装済みのもの：

- merge後Reviewで見つかったdeadline TOCTOUを修正し、外部read後・署名直前にPolicy / claim windowを再確認
- PolicySigner / Ed25519 helperはKeyObject生成後の一時seed / PKCS8 bufferを明示zero化し、長期保持は署名KeyObject + 導出済みstate keyに限定
- Humanのper-action seed入力を前提にせず、初回だけ匿名pipeから32-byte seedを受けてhost-bound `systemd-creds` encrypted credentialを作るone-time handoff
- Runtimeはsystemd `LoadCredentialEncrypted=project-seed:...` からcredentialを取得し、Agent requestからseed / credential pathを受け付けない
- Runtime serviceは既存stateが必須で `initialize:false` 固定。初期state作成は別oneshot bootstrapへ分離
- AF_UNIX / systemd socket activationのTyped RPCのみ。公開methodは `status / admit / issue / reconcile / observeCompletion / closeCallOwnerRegister / closeCallTakerLong` で、generic signing APIはない
- Signerは `PrivateNetwork=yes`。Networkを持つのはSecretを持たない別userのTrusted Readerだけ
- Trusted Readerの外向き先はCurrent pinの公式経路だけ：
  - `https://technocore.chat/r/<derived-room>/export`
  - `https://technocore.chat/r/close1/export`（Close Call owner-registration / nonce確認）
  - `https://technocore.chat/r/d-close1-price/export`（Close Call current price確認）
  - `https://technocore.chat/kv/<paperNote(contract).ns>/<paperNote(contract).key>`
- Reader RPCは `refreshOffers / refreshCloseCallRegistration / refreshCloseCallPrice / refreshDeal(contract) / refreshPaper(contract)` のみ。URLや任意room、POST/write operationを受け付けない
- Reader socketはAgent-facingではなくSigner内部専用。Signer Runtimeが各操作の直前に必要なReader refreshを呼び、成功後だけPolicy Coreへ進む
- ReaderはTechnocore note readの`!! UNTRUSTED CONTENT` bannerを除去し、PaperRailのsingle-line valueだけをsnapshot化する
- Readerがatomicに保存した公開snapshotをshared read-only acquisition group経由でSignerが読む。Agentはsnapshot本文・capturedAt・generationをSigner RPCへ注入できない
- Runtime request auditはmethod / action / outcome / revisionだけをfsync付きJSONLへ記録し、request本文・seed・署名Envelopeは記録しない。Audit開始記録が書けなければ署名処理へ進まない
- deployment templateではSigner / Readerを別userとし、Secret directory / public acquisition directory / socket ACLを分離

### Secret custody flow

```text
Human（初回のみ）
  ↓ anonymous pipe / 32-byte seed
credential_handoff.mjs
  ↓ DID照合
systemd-creds encrypt --with-key=host
  ↓
/var/lib/flop-policy-signer-secret/project-seed.cred
  ↓ LoadCredentialEncrypted
Policy Signer service
  ↓ service初期化時だけcredential bytesを読む
Ed25519 KeyObject + derived state key
```

通常の署名ActionごとにHumanがseedを入力する経路は、このRuntime設計にはありません。

### Runtime split

```text
24H Agent
  ↓ AF_UNIX
Policy Signer（Secretあり / Networkなし）
  ├─ typed request validation
  ├─ AF_UNIX → Trusted Reader（Secretなし / fixed GET only）
  │               ↓
  │       fresh public snapshots
  │               ↓ read-only
  └─ Policy / state / signing
                  ↓
             signed envelope
```

### Real activation前に残るGate

Minimal Real Runtime Boundary（PR #15）のIndependent Reviewは完了しています。
Close Call capability（PR #35）もpost-merge Claude Code Independent Review済みで、
code必須修正なしと確認されています。

ただしowner registrationは既に完了しているため、Close Call registration profileは休眠扱いです。
現在のBlockerは主にObserved RuntimeとHuman operationです。

- 24H Agentを専用non-sudo UIDで動かし、Signer / rootへ昇格できないことのObserved Evidence
- systemd unit / socket / tmpfiles / UID/GID / filesystem boundaryの実Host Evidence
- Human seed handoff runbookに沿ったcredential custody
- service restart後もper-action seed入力なしで同一DID / protected stateを復旧できるEvidence
- Signer-controlled Reader refresh → trusted snapshot → signing decisionのLive Technocore read-only E2E Evidence
- Real External Writeはさらに別Human Gate

初回single-work Pilotでは、valid old encrypted-state rollbackの外部anchorとone-work後の自動rotationはLaterとします。
root / complete-host compromise resistanceは初期Threat Model外です。

詳細手順は `docs/real-runtime-readiness.md` をCanonical runtime Gateとして参照してください。

## Verification

```bash
cd components/signer
node --test tests/*.test.mjs
node --check src/technocore_signing.mjs
node --check src/tclk_v1.mjs
node --check src/technocore_read.mjs
node --check src/policy_signer.mjs
node --check src/runtime_service.mjs
node --check src/trusted_reader.mjs
node --check src/credential_handoff.mjs
```

現在のtestsは、少なくとも以下を確認します。

- Official Ed25519 / DID互換
- Official tclk Offer / ACCEPT golden vector
- Paper GCD workのend-to-end offline completion
- 19-digit nonce lossless handling
- value-bearing拒否
- tampered / incomplete export拒否
- competing ACCEPTのFinal Revalidation拒否
- ambiguous outcomeのblind retry禁止
- restart後のreplay保持
- policy tamper / state corruption / DID mismatchのfail-closed
- generic signing operation拒否
- Close Call typed registration requestのexact binding
- Close Call owner JSONのSigner-side再構築
- visible duplicate owner registration拒否
- Close Call lock時刻 / single-signature quota
- Close Call Taker-Long policyのowner-registrationとのcapability分離
- SELL terms / maker署名 / Agent request bindingのSigner-side再検証
- current Referee price一致、limits、next sweep、lockのFinal Revalidation
- Signer-owned nonce、2署名quota、durable `RESERVED` / `ATTEMPTED` replay denial
- stale official current referenceの非拒否と明示

## Not Yet Verified

- Real Project DID credential handoffの実Host実行
- dedicated Agent / Signer / Reader UIDとsocket / filesystem isolationのObserved Runtime
- Real credentialでのrestart recovery
- Live Technocore Reader → snapshot → Signer decisionのread-only E2E
- Real External Write
- Production deployment
- valid old encrypted-state rollback detection

これらはoffline testやsystemd templateの存在から推定しません。

Real Runtimeの次手順は `docs/real-runtime-readiness.md` に従います。

## Start Here

1. root `README.md`
2. root `AGENTS.md` / `docs/OPERATIONS.md`
3. this `AGENTS.md`
4. this `README.md` / runtime docs
5. reuse調査が必要な場合だけ `components/collaboration/agent`
6. Private engineering archiveを扱うmaintainerだけ historical Issue #1 / #6

Configured / Intended != Observed Runtime.
