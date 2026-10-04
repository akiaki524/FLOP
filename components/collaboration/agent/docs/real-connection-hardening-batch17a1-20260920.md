# Batch 17A.1 — Real Connection Preconditions Hardening（Offline）

この文書は、**今回Humanが共有したFindingを基にした対応記録**であり、Independent Review原文の保存ではない。
Human確認により原文はRepository内に存在せず、今回のPromptのP1-1〜P1-4、P2-1〜P2-5を作業基準とした。
P3はBlocking条件にしない。Real Secret接続・External Writeの許可や、Real運用可能という認定を意味しない。

## Checkpointと指示

- **Observed Fact:** 開始HEAD `f362ced5f5af2db146ec5cd1d91b351ff1bf3048`、開始worktree clean。Human報告と一致。
- **Observed Fact:** 開始regression 108 tests PASS / skip 0、21.305秒。別途、変更後に既存Signer 63項目もPASS。
- 実装checkpointは `cd0486f2f90e9cbacfa0ab6ea909b7255f46be06`（worktree clean）。その後、下記Independent
  Re-reviewの親directory fsync指摘を修正した。終了HEADは修正と本Reportを含む最終local commit。自己参照SHAを埋めず、commit後の
  `.local/batch17a1/final-state.json`と最終応答に確定SHA・worktree状態を記録する。
- 適用した指示はHuman提示の共通AGENTS.md、今回のBatch 17A.1と後続のReview/DIDに関する明示指示。
  プロジェクト配下の実装対象に追加AGENTS.mdは見つからなかった。外部保存checkoutの指示を実装対象へ転用しない。
- **指示競合記録:** 共通AGENTS.mdの自律修正1回制限、一般的な変更承認・作成数・commit前承認より、
  今回のOffline / Reversible実装・Test・Report・meaningful local commitの明示承認を優先した。
  調査・修正回数のみを理由に停止しない。権限・Signer action・Real接続の拡大は行わない。
- 新規配置は実装前に提示。変更対象はPilot module、Pilot test、今回のEvidence helper、Reportのみ。
  Solver / Classifier / acceptance boundary / fetch基盤 / pin / Historical Evidenceは変更しない。

確認したSourceは、[Batch 16](real-pilot-protocol-batch16-20260919.md)、[Batch 17A](typed-signer-batch17a-20260919.md)、
`git show f362ced`の追加内容、Signer/Supervisor/Gate/mock transport/journal/tests、保存済み公式実装。
公式checkoutは `5cc4ab93efbc8999a3a7e1471b639deca25998ea`、clean。runtimeの63ファイルは従来のhash pinで検証する。
新規dependency、install、Secret Store、Real credential、OS user、sudo、Production操作、push/PR/publicationはない。
追加network調査は公式の公開文書・Source・Issueのreadだけ。Technocore room取得やwriteは行っていない。

## Finding対応

| Finding（Reviewer Finding） | Offline対応（AI Engineering Decision / 検証） | Real前に残ること |
| --- | --- | --- |
| P1-1 sessionのみの1契約制限 | DIDに固定した永続ledger、admissionで枠消費、別journal/restartは照合専用。terminalでも枠を戻さない | Real DID anchor、ledger配置とOS保護、秘密custody/復旧のHuman承認 |
| P1-2 無関係recordで全契約停止 | party/contractとの関係を分類し、公式verify/handshake/foldへ当該契約だけ渡す。無関係recordはstate-neutral | Real観測adapterのlossless入力・完全性・時刻・generationの信頼根拠 |
| P1-3 nonce互換性 | ms×1000を除去。TEST allocatorと未決定Real policyを分離。下記Decision Packet | DID固有観測とReal方式のHuman決定。未決定のままRealへ進まない |
| P1-4 Workerが承認表示authority | 専用Gate経路からcanonical approvalを取得し、Approval componentでdigest再計算。Workerのpreviewを承認入力にしない | Human本人認証と表示端末/Approval processの独立保護 |
| P2-1 同一UID/process topology | WorkerをSignerの兄弟processへ移動。WorkerはSignerのparentでなく、Gate fdとspawn権限を持たない | 同一UIDへのOS攻撃耐性は未達。別UID等の設計はHuman Gate |
| P2-2 RPC timeout kill | timeoutでkillしない。out-of-band STOP、台帳のSEND_ATTEMPTED/AMBIGUOUSを保持 | Real transport timeoutとcustody lifetimeの運用設計 |
| P2-3 TEST操作混在 | launcherとSigner初期化の両方でREALを拒否。TEST controlsはmode guard付き。Workerからも不可 | Real有効化時はTEST controlsを提供しない別構成を再レビュー |
| P2-4 signature出力 | signatureのbase64url/base64/hexと構造fieldをguard。signed record/requestも拒否。stderr/stdoutを廃棄 | 将来Transportへのprivate handoffの別レビュー |
| P2-5 durability/readback | atomic replace + file fsync + directory fsync + checksum/schema/readback + DID lock。破損/欠損は停止 | power-loss実機検証、OS保護、backup/rollback防止の運用 |

**今回のOffline対処と、Real接続までの残件を区別する。** P1-3のHuman判断、P2-1のOS isolation、
Human認証・Real秘密復旧は未解決であり、解決済みとしてReal接続を認めない。
Independent Re-reviewで今回の修正がFindingを閉じるかを別途判定する。

## Persistent contract ledger

[pilot_ledger.mjs](../src/collaboration_agent/pilot_ledger.mjs)が信頼するSupervisor側で管理する。
固定pathは `.local/batch17a1/identity-<SHA256(DID)>.json`。journal pathやWorker requestから変更できない。
新しいjournalを選んでも同じDIDの台帳を再利用する。新規TEST sessionは新しいephemeral TEST DIDを生成するが、
これは独立した試験identityであり、RealのOne Identity policyで新DIDを生成する許可ではない。
Real modeは存在しない。今回のProject DID実値は未提示で、TEST DIDをその代用品として登録しない。

保存するのはversion/mode/DID/revision、quotaConsumed、OFFER/contract/payer/sourceDigest、
認証済みOFFER envelopeのdigest、public protocol state、action subject/subject digest/lifecycle/envelope digest。
subjectには正確な十進nonceとpayload digestを含む。signature、signed record、Seed、private key、
preimage、REVEAL本文は保存しない。envelope digestは署名を含む全送信envelopeのhashであり、relay可能な署名そのものではない。

admissionのACKを返す前にquotaを消費する。新ACCEPT、別contract、別journal、terminal後の次候補を拒否する。
ACCEPT前のcrashでもrestartは照合専用。未送信と推定して枠を返すAPIはない。
同じDIDのactive ledger ownerはexclusive lockにより1つだけ。Signerはfilesystem write権限を持たず、
Supervisorからledger/journalのdurable ACKを受けるまで次のeffectへ進まない。

台帳rootを作成/確認後、その親`.local`をfsyncしてdirectory entryを永続化してからlock/identityを作成する。
更新は新しい専用temp fileへ全bytesを書きfile fsync、同一directory内でrenameによるatomic replace、
directory fsync、readbackの順。schema/checksumは更新前後に確認し、active process中の台帳差替えも検知する。
journal新規作成時もdirectory fsync、各追記でfile fsyncする。ledgerがauthority、journalは補助auditである。
ledger更新後にjournal追記で失敗しても台帳を巻き戻さず、Signerを停止する。

checksumは事故による破損検知であり、同一UID攻撃者による正しいchecksum付き改竄やsnapshot rollbackに対する認証ではない。
台帳・lockを人手で削除したり別checkoutへidentityを移す運用を許可していない。
Supervisor全体がcrashしてlockが残る場合、勝手なstale-lock解放はせずfail-closed。
人手でowner停止を確認する復旧手順と、保護されたauthority配置がReal前に必要。

## Restart / crash / ambiguous

`PREPARED → SIGNED → WRITE_PREPARED → SEND_ATTEMPTED → OBSERVED / AMBIGUOUS → RECONCILED`
を台帳に保持する。signatureの生成と保存前後でcrashしても再署名・再送へ戻らない。
SEND_ATTEMPTEDはmock effect前に保存。署名結果や送信応答をWorkerへ渡さず、状態とopaque action IDのみ返す。

restartは `launchTestPilot(newJournal, { recoveryDid: previousTestDid })`。
同じ公開DIDを表示するが、署名鍵を再生成してそのDIDを名乗ることはしない。sign関数を持たない照合専用processとなる。
鍵とpreimageはprocess終了で失われ、秘密を保存したふりをしない。REVEAL継続は不可。
Realではpreimage continuityを解決せずにACCEPTしてはいけない。

`reconcileRestart`はGate専用。認証済みOFFERのcommitment、公式handshake/fold、party/room/nonce、
exact signed envelope digest、metadataの順序/鮮度を検証する。適合記録が1件だけあるactionをRECONCILEDにする。
記録欠損・別nonce・複数候補を「未送信」と扱わない。reconciliation後もquotaは消費済みで、署名を再開しない。
restart照合testのpublic transcriptは、独立したTEST venue fixtureの鍵でメモリ内に構築している。
live Signerから署名を取り出して試験を成立させたものではない。

RPC timeoutは処理結果不明の通知であってtransportの失敗判定ではない。
timeout時にSignerをkillせずSTOPを送り、secret custodyを保持する。進行中attemptは結果に応じてAMBIGUOUSへ移る。
新nonce、同一envelopeの自動再送、新contract、別journalへの迂回はない。
processの明示kill/crashでは秘密を失うが、永続stateから照合だけ可能。mock transportはSigner内のin-memory配列だけ。

## Transcript observation

公式 `verifyTranscriptRecord` / `findContractHandshake` / `foldTranscript`を再利用する。
offer room全体をfoldせず、当該OFFERとcontractのhandshakeを選ぶ。
当該partyを名乗るrecord、当該contractに結び付くrecordは公式署名検証を通す。
party由来のinvalid signature、sender/from mismatch、room/contract違反、当該契約の不正transitionは拒否する。
malformed frameでもfrom claimを確認し、party偽装を単なる無関係recordとして捨てない。

第三者の通常message、別OFFER、別contract、他Agentの正常frame、無関係malformed recordはstateを変えない。
正常なroom metadataがあれば、無関係recordでもcursorを進める。保持する契約historyには入れないので、
複数batchの無関係投稿で契約historyの上限を使い切らない。
署名対象外のseq/timestamp/generationは信頼する観測経路の責任であり、署名だけで真正性を保証しない。
1入力64 records/64 KiB等の資源上限、metadataのgap/競合拒否は残す。
room floodやpartyを名乗る偽造recordによる可用性低下を完全に防ぐ設計ではない。

## Process / Secret / signed material boundary

```text
Trusted Supervisor / Approval controller (TEST harness)
  ├─ Worker process: typed requests only; no Gate fd; no spawn/fs-write/runtime-read
  └─ Signer process: Node permission restrictions; ephemeral TEST key/preimage
       ├─ Approval policy: recompute canonical full payload digest
       └─ Mock Transport: in-memory append only; no network/export capability
Supervisor → dedicated Gate fd → Signer
Signer → public state/digests only → Supervisor ledger/journal
```

WorkerがSignerのparentだった構成を廃止。Workerは別processの限定relayであり、実Worker CLIへのReal統合は行っていない。
Supervisorは信頼するlauncherであってWorkerへ渡すmoduleではない。
Signerのfs-write/child/worker-thread/native-addon許可はfalseのまま。Workerにもspawnを許可しない。
Node Permission Modelを別UID等のOS isolationと同一視しない。同一UID攻撃、同一UIDからのsignal、
Supervisorへの侵害に耐えるReal secret boundaryは別途構築・レビューが必要。

Seed/private key/preimageに加え、signature/signed record/replay可能なsigned requestをprotected materialとする。
応答・auditに共通guardを適用し、署名値のbase64url/base64/hexと禁止fieldを検出する。
Signer/Workerのstdout/stderrは保存しない。例外は固定codeのみ。approval previewにも署名やpreimageを出さない。
mock transportはSigner内部だけで署名に触れる。将来のReal TransportはHuman承認後の専用private経路を別レビューする。
公開後の署名記録は第三者にrelayされ得るため、署名を単なる公開metadataとして扱わない。

Real Secret deliveryとしてenv/argv/Worker relay/Supervisor relay/普通のfile/log/tempを採用していない。
今回その入力interface自体を作っていない。実鍵の配送・custody・復旧には別のHuman許可が必要。
JS heapの完全zeroizationは保証しない。

## Trusted TEST approval path

[pilot_approval.mjs](../src/collaboration_agent/pilot_approval.mjs)を用い、
`pilot.approval.preview(actionId)`でSignerのcanonical subjectを専用Gate経路から取得する。
controllerはsubject digest、public payload digest、approval packet全体のdigestを再計算する。
Human向けに提示できるものはtype/DID/room/OFFER/contract/ref/public line/rail/deadlines/statement/digest。
`approval.approve({ actionId, approvalDigest, expiresAt })`はSignerから再取得したpacketと照合して承認する。
Workerが異なる説明を示してもcanonical payloadは変わらず、別digestの承認は拒否する。
承認後も署名前・effect前に同じsubject、期限、観測、状態を再検査する。

REVEALだけはpublicLine=null、`PUBLISH_PREIMAGE`と明示する。
完全なREVEAL payload digestを再計算するApproval policyはSignerの秘密境界内で動く。
外側controllerはredacted packet/subject digestを再計算するが、preimage未開示の完全payloadを独立に再構成はできない。
これは秘密をapproval previewへ出さないための設計上の区別であり、外側が完全bytesを検証したと主張しない。
Signer内部のApprovalコードとtrusted controllerはTCBに含む。Workerからの独立性を検証し、Signer侵害への耐性は主張しない。

今回はTEST/mock APIと検証まで。本人認証済みHuman操作、信頼できる実UI/端末、OS分離は未実装。
TESTの自動承認をHuman承認の代わりに使わない。REALはlauncher/Signer双方で起動時に拒否する。
advanceTestClock/mock/forgetMockRecords/artificial stateはTEST限定で、Workerからは呼べない。

## Nonce Decision Packet

### Factと未確認事項

**Human Decision:** Project DID実値は未提示/未確認。公開記録から推測・特定しない。
Primary確認対象は `tclk-offers`。deal roomはcontract確定後に公式 `dealRoom(contract)`で導出する。
DID別last observed nonceはHuman提供または別途確認待ちであり、0とも空とも扱わない。

**Current Official Source（今回public read）:** [Technocore manual](https://technocore.chat/)のSIGNING/NONCEは
1〜19桁、DID×roomの直前値より大きいnonceを規定する。ただし直前値検索は最新1 MiBのtailで、
署名recordがtail外になると同じsigned materialのreplayが通り得る。永続的なnonce registryではない。
この文書は公開仕様であり、Production server codeや配備状態を今回実機writeで検証したものではない。

[official tclk-mcp signing.ts](https://github.com/flop-labs/tclk/blob/main/mcp/src/signing.ts)は
`max(Date.now(), lastNonce+1)`のprocess内カウンタ。pin保存版も同じ。
[Issue #78](https://github.com/flop-labs/tclk/issues/78)はsafe integerを超えるnonceが通常JSON.parseで丸められ、
read/foldを阻害する問題を報告している。今回閲覧時のページ表示はOpen。Issueの他者観測値を自分の実測値へ置き換えない。
今回の運用pinは変更せず、mainや将来の修正を導入済みとみなさない。

**Observed Fact（保存済み公開EvidenceのOffline集計）:** Batch 16の`board.json` 23,809 records、
2026-09-19 11:00:37〜11:42:19 UTC。13桁19,542、16桁297、19桁3,953、3桁13、4桁4。
safe integer範囲外は3,953件。出典hashと集計は `.local/batch17a1/nonce-observation.json`。
Batch 16のlossless文字列をPython整数で集計し、DID attributionや最新room観測は行っていない。
16桁でも全てunsafeではなく、具体値と `2^53-1` の比較が必要。19桁をNumberへ変換してはいけない。

### 推奨とAlternatives（未承認案）

| 方式 | Pros | Cons / 既存DID・interoperabilityへの影響 |
| --- | --- | --- |
| **推奨案:** 単一nonce authority、room別のdurable reservationと確認済み公開値を照合。可能ならms相当のsafe integer帯 | official toolingとの通常互換性が最も高い。Identityを変更しない。restartで未確定値を再利用しない | DID別last observedをまず確認。同時のofficial tooling送信は禁止/統合が必要。large nonceや観測不足があれば自動開始しない |
| 大きい既存値を正確なdecimal/BigIntで継続 | DIDを維持し、既存のlarge nonce系列へ適合できる | #78のread-path制約とofficial allocatorの小さい値への復帰問題。送信/読取り双方を事前検証し、Humanが選択する必要 |
| 時間待ち/room tail状況を確認してofficial allocatorへ戻る | 条件が成立すれば同一DIDのままofficial toolingへ戻れる | tail外移動の時刻やglobal high-waterは保証できない。古い署名のreplay可能性が復活する。観測だけで安全なnonce resetを認定しない |
| DID変更でnonceを新しくする | nonce空間は別になる | One Identity / Identity Continuityに反し、今回の選択肢としては不採用。Humanの別方針変更なしに行わない |

推奨案の具体式・値・reservation policyは未承認で、Real allocatorとして実装していない。
TEST allocatorだけはms相当のdecimal文字列をroom別に増加しsafe integer範囲に制限する。
`mode=REAL`では `REAL_NONCE_POLICY_UNDECIDED`。ms×1000をReal defaultへ持ち込まない。

**Worst Case:** 既存のlarge nonceや別toolの同時writeでACCEPTが拒否される、署名read-pathの丸めで
観測不能になる、応答消失後に再送して二重公開する、tail外の署名が再relayされる。
room別の違いをDID全体の一律counterと誤認しない。最大桁の数字を選べば安全という根拠はない。
大きいnonceから永久に戻れないとも断定しない。復帰可否は同じDID×roomの観測範囲・tail・toolingに依存する。

**Humanが決める事項:** 既存public DID、`tclk-offers`のbounded read許可と観測資料、単一writer運用、
large nonceの場合の継続/保留/互換性確認、公式toolへ戻す条件、永続reservationの責任者。
ambiguous writeではnonceを再利用せず、新nonceでも再送せず、署名envelopeのdigestに基づくread-only照合だけ。
再送を検討する別Gateがあっても、現Signerにはその能力を追加しない。

## Tests / Evidence / Engineering上のFailure

実行結果は `.local/batch17a1/` に固定code/名前/件数として保存し、raw stderr、秘密、署名をEvidenceへ書かない。
開発中の失敗結果も保存し、成功件数へ加算しない。

- `baseline-regression.json`: 開始108 tests PASS / skip 0。
- `integration-01.json`: 修正後の既存63 Signer項目PASS。
- `targeted-final-03.json`: 新規27項目PASS。二重ACCEPT/別journal/quota、AMBIGUOUS restart、party/room filtering、
  deadline/DID/ref、guard、trusted digest、mode、crash/timeout、corrupt/partial/missing ledger、fsync/readback、exact reconciliation、
  STOP後のread-only観測とterminal stateの後退拒否。
- Independent Re-review後に親directory fsync失敗の1項目を追加し、`review-fix-targeted.json`で28/28 PASS。
- 最終Signer integration / full regression / Evidence checkは下記最終検証欄へ記録する。

crash testの初期実装で`process.exit()`が想定どおり即時のprocess exitとして観測されず、RPC timeoutとなった。
同時にclose処理のSIGNER_EXITEDが元の失敗を覆っていた。まずcloseの競合を修正し、固定codeだけの段階診断で
`CRASH_ATTEMPT_RPC_AMBIGUOUS_STOP`を特定した。crash simulationをself-SIGKILLへ変更し、durable SEND_ATTEMPTEDからの
restartを確認した。process.exitの内部停止原因は未確認であり、OS/kernelの原因を断定しない。
これはTEST fault injectionの修正で、通常のRPC timeoutによるkillを戻したものではない。
初期patch適用で同一fileのdelete/addを1 patchへまとめた形式が拒否されたため、逐次patchで適用した。
Routine Engineeringの追加試行は今回のHuman policyに従い継続した。

調査時の取り扱いも区別する。保存済み公開`board.json`の先頭を確認した際、既存公開recordのsignature 1件を
toolのread outputに含めた。新Signerが生成した署名ではなく、Real鍵アクセス・外部送信もないが、
protected materialを生で表示しない運用には不適合だった。今回のEvidence fileへraw recordをコピーせず、
以後のnonce調査は専用helperによる桁数/件数/hashの集計だけにした。会話上のtool outputは削除できない。
これはruntimeの出力guardによる漏洩とは別の調査上の取り扱い問題として残す。

再現（保存済み公式runtimeとBatch 15/17A候補資料があるこのworkspace、追加取得なし）:

```bash
node tests/pilot_signer.mjs targeted
node tests/pilot_signer.mjs
python3 -B -m unittest discover -s tests -v
```

Pythonの108件のうち1件がSigner統合を内包する。Node内部件数をPython件数へ加算しない。
pin欠損時にdependencyやSourceを自動取得しない。

## Stop / Recovery / Human Gate

STOPはGateの`stop`。新しい署名/effectを停止し、同processのread-only reconciliationは可能。
RPC timeoutはSTOPをlatchし、秘密保持のためprocessを残す。明示的な`close`は終了ACK→FD EOF→queue drain、
異常時の明示killは最後の手段。終了するとTEST key/preimageは失われる。
停止時はledger/journalを保全し、別journalや新DIDで再試行しない。

Recoveryは同じ公開DIDの台帳を読み、必要なpublic observationsを限定的に取得して照合する。
観測が不十分ならAMBIGUOUSのまま。欠損/破損ledgerは自動初期化しない。
Supervisor crashで残ったlockは自動削除しない。ownerが動作していないこと、state/未確定action、保存物の整合を
Humanが確認する復旧作業が必要。コードrollbackは通常のGit revertで可能だが、stateを巻き戻してquotaを再利用しない。
Real公開記録や契約をGit操作で取り消せるという意味ではない。

Real Secret接続前のMandatory残件:

1. 別ModelによるIndependent Re-reviewと、別途Humanの接続許可。
2. 既存public DIDの明示、固定policy anchor、台帳authorityの保護/backup/rollback防止。
3. WorkerとSigner/ApprovalのOS隔離、Human本人認証/信頼できる表示端末。
   別UID、制限IPC、debug/memory/core dump対策等の具体的な権限は別Gateへ提示し、今回は設定しない。
4. 禁止配送経路を使わないReal Secret custodyとpreimage continuity/recovery。
   普通のledger/journalへ秘密を保存して解決しない。
5. Nonce Decision PacketのDID固有情報とHuman選択。

External Write前には、上記に加え対象1件のOFFER/Task/独立検証済みanswer、PaperRail形式とclaim更新主体、
Humanのaction別承認、期限/観測予算、private signed-material handoffを持つ狭いTransport、
lossless/fresh/完全性の確認された観測経路をレビューする。
現実の公開DIDの評判、ACCEPTだけ残る、納品後に秘密喪失してREVEAL不能、相手不応答、
署名relay・誤観測・期限切れがWorst Case。no-valueでもこれらは消えない。

未許可actionは従来どおり。任意text/frame、generic GET signer/client、LOCK/refund/cancel/receipt、
無制限heartbeat、multi-contract、automatic ACCEPTを追加していない。
P3やread-only基盤の改善へ範囲を広げず、pushせず、Batch 17Bを開始しない。

## 最終検証・Independent Re-review引継ぎ

実装checkpoint `cd0486f`で以下を確認した（Independent Re-review後の最終結果は次節）。

| 検証 | 結果 | 保存先（`.local/batch17a1/`） |
| --- | --- | --- |
| 新規targeted | 27/27 PASS | `targeted-final-03.json` |
| Signer integration | 90/90 PASS（既存63 + 新規27） | `signer-integration-final-02.json` |
| 全regression | 108/108 PASS、skip 0、49.338秒 | `regression-final-02.json` |
| restart / crash / ambiguity smoke | 7/7 PASS | `smoke-restart.json` |
| RPC / transport timeout smoke | 2/2 PASS | `smoke-timeout.json` |
| Historical Evidence hash | 11,946 files、変更0 | `baseline.json` / `evidence-final.json` |
| diff review | `git diff --check` PASS。変更はPilot関連と今回Report/helperのみ | staged diff / local commit |

新規dependency/Real Secret access/External Write/Production Mutation/pushは0、Batch 17B未開始。
終了worktreeと確定SHAはcommit後の`final-state.json`に記録する。

レビュー対象は本Batchのdiffと上記Findingのみ。
優先順は `pilot_ledger.mjs` → `pilot_signer.mjs` → `pilot_client.mjs` / `pilot_worker.mjs` →
`pilot_approval.mjs` / `pilot_protocol.mjs` → `tests/pilot_signer.mjs`。
Real未実装事項をOffline test結果で充足扱いしないこと、REVEALの内側/外側digest検証の違い、
同一UIDとstale lockの限界、nonceのCurrent SourceとHistorical Observationの違いを確認してほしい。

## 別ModelによるIndependent Re-review結果

Humanの「完了後はPrimary Implementerと別ModelでRe-review」の指示に従い、implementation commit
`cd0486f`の後、**GPT-5.6 Sol / high**によるread-only reviewを実施した。
これは今回のBatch 17A.1に対する新しいRe-reviewであり、未保存のBatch 17A Review原文とは別である。
対象はbase `f362ced`からの実装差分と本Report。ネットワーク・Real Secret・外部write・file編集はReviewerへ許可していない。

**Reviewer Finding:** P1-1〜P1-4の具体的な回避経路は確認されず、Offline対応を確認。
ただしP2-5で、台帳rootのmkdir後に親`.local`をfsyncしていないため、初回作成直後のpower lossに対する
directory entryの永続化が不足している、という1件の具体的不備があった。
当初の判定はOffline CONDITIONAL GO / Real NO GO。Reviewer自身がtargeted 27/27 PASSとclean worktreeを確認した。

**AI Engineering Decision:** rootのmkdir/symlink確認後、lock作成前に親directory fsyncを必須化した。
故障注入testは実際に利用するNode fs primitiveをTEST processで差し替え、mkdir→parent fsyncの順序、
fsync失敗の伝播、lock/identity fileが未作成であることを確認する。Signerの権限・actionや依存は増やしていない。

**Independent再確認:** Reviewerがこの差分だけを再検証し、追加testを独立に1/1 PASSと確認。
指摘は解消、新たな問題なしとして、**Offline checkpoint GO / Real readiness NO GO**へ更新した。
P1-1/P1-2/P1-4はOffline範囲でCLOSED、P1-3はCLOSED-AS-DISABLED。
P2のOffline実装は確認済み。同一UID/OS保護、Human認証、Real秘密/preimage復旧、DID固有nonce等はReal Gate残件のまま。
このGOはReal Secret接続やExternal Writeの許可ではなく、Batch 17Bは開始しない。

**修正後の最終実測:** 新規targeted **28/28 PASS** (`review-fix-targeted.json`)、
Signer全体 **91/91 PASS** (`review-fix-integration.json`)、全regression **108/108 PASS / skip 0 / 50.319秒**
(`review-fix-regression.json`)。この最終suite内でもrestart/crash/AMBIGUOUS/timeoutを再実行した。
Historical Evidence **11,946 files変更0** (`evidence-review-fix.json`)。
最終commit前のdiff checkに問題なし。最終SHAとclean worktreeは`final-state.json`で実測する。
