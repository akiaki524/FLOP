# Batch 17A — Narrow typed signer / write path（Offline限定）

**Batch 17Aの実装・Offline検証は完了。108 Python testsと、そのうち新規統合test内の63項目がPASS。**
一度停止した`ERR_INVALID_FD_TYPE`はHumanの追加Offline修正許可後に解消した。
これはReal Pilotの実行許可やReal運用可能という判定ではない。
既存GCD Solverと独立検証の結果を、信頼するGateが1候補として固定し、
限定typed request → 個別承認 → TEST署名 → mock送信準備 → 結果照合へ接続した。
Real DIDの鍵・Seed・Wallet・Secret Storeにはアクセスせず、Technocore writeは0。
既存Project DIDを将来のIdentity Anchorとする方針は維持するが、この実装はTEST DIDしか生成しない。

## 1. Source、開始状態、変更範囲

開始実測は `main / e26f040980ae5bab94dea722e27ca5596ee13e9f`、worktree clean。
Humanのcheckpoint報告と一致。reset・既存変更破棄・履歴改変なし。
追加修正再開時も同HEADで、今回作成した8ファイルだけがuntrackedだった。
既存tracked fileは無変更。終了HEADは本Reportを含むlocal commitとなる。
自己参照を避け、確定SHAとworktree cleanの実測はcommit後の`.local/batch17a/final-state.json`と
Humanへの最終応答に記録する。pushなし。

今回原文を確認したものは、User提示の共通AGENTS.md、Batch 17A指示、README、
[Batch 16 Report](real-pilot-protocol-batch16-20260919.md)とPrivate JSON原本（公開版では除外）、Batch 16のPython/Node runner、
保存済み公式runtimeとSource、既存Solver・independent verification、GCD frozen bundle。
Project内に追加適用AGENTS.mdは見つからなかった。別のRoadmap・Phase-Gate Audit原文を
確認済みとはしない。Read-only Freeze、One Identity、未許可操作の境界はHuman指示を前提にする。

公式pinは [flop-labs/tclk](https://github.com/flop-labs/tclk/tree/5cc4ab93efbc8999a3a7e1471b639deca25998ea)
の `5cc4ab93efbc8999a3a7e1471b639deca25998ea`、`@flop-labs/tclk 0.1.0`。
Batch 16取得済みのclean checkoutと型消去済みruntimeを再利用し、Source/生成物hashと
既存暗号依存tarballのlockfile由来SHA-512を照合した。取得日時はBatch 16の
2026-09-19 13:24 UTC台。今回の照合日時は機械可読Reportに記録する。
`@noble/curves`、`@noble/hashes`、`@scure/base`各2.4.0は既承認分のみ。
新規依存、install script、live example、MCP serverの実行はない。
実行時は[tclk_pin.json](../src/collaboration_agent/tclk_pin.json)の63ファイルを照合し、欠損・改変時は起動しない。
GitHub入口は今回も閲覧したが、最新mainのSHAやProduction配備との一致を新たに認定していない。
Technocore仕様はBatch 16保存版を参照し、今回Technocoreへの追加readも行っていない。

使用する公式APIは `makeOffer` / `makeAccept` / `makeHeartbeat`、`encodeFrame` / `decodeFrame`、
`generateHashLock`、`dealRoom`、`verifyTranscriptRecord`、`foldTranscript`、`openContract` / `applyFrame`、
`lockTerms`、`paperNote`、`decodePaperRecord`、`PaperRail` / `MemoryNoteStore`、
同pinのsigning helper `signerFromSeed` / `canonicalMessage` / `sweep`。
独自のframe canonicalization、ID、署名challenge、signature verifier、Protocol foldは作っていない。
承認subjectのJSON digestと操作状態管理はローカル権限制御であり、tclk互換実装ではない。

追加は3つのJS module、pin manifest、Python/Node tests、本ReportとJSON。
Solver、Classifier、acceptance boundary、既存Worker CLI、Batch 16までのEvidenceは変更しない。
READMEの「Signerなし」は既存Worker CLIについて引き続き成立するが、今回追加した隔離TEST moduleを
含むRepository全体の説明としては過去の記述になった。本Reportで差分を明示する。

## 2. 何の署名を許す設計か

| typed action | 必須条件・公開予定の内容 | 上限 |
| --- | --- | --- |
| ACCEPT | 認証済みpayer OFFER、固定Task digest、GCD独立検証済み回答、paper/hash/PAPER、期限余裕、個別Gate承認。TEST DID・hash statementを含む公式frame | 1回 |
| INITIAL_HEARTBEAT | ACCEPT観測済み、Taskが初回heartbeatを要求、同一full contract・導出room、個別承認 | 要求があるTaskで1回 |
| DELIVERY_GCD | LOCKとPaperRail記録を別々に検証、必要heartbeat観測済み、固定回答と完全一致、個別承認。本文は `gcd=<g> lcm=<l>` の1行のみ | 1回 |
| REVEAL | 上記に加えDelivery観測済み、LOCK.ref=full contract、claim期限余裕、個別承認。Signer内で生成・保持した32-byte preimageを公式frameへ含める | 1回 |

LOCK、refund、cancel、receipt、OFFER、継続heartbeat、任意text/frame署名、URL送信は許可しない。
Deliveryとheartbeatを含めた根拠は、Batch 16で確認した
`.local/batch15/session-01/frozen/native-6948666/bundle.json`の全文Task規約とGCD実納品形式。
heartbeatはtclk一般の必須仕様ではなく、このTaskの初回room作成要求である。
未確認Taskへこの許可を一般化しない。Nim/shortest pathのSigner actionは追加していない。

1件は「Gateがadmitした1候補・1 full contract」で数える。ACCEPTは最大1回、LOCK待ち中も枠を消費する。
未LOCK・timeout・不明結果でも次候補に自動で進まない。準備後の承認失効も再準備・nonce更新で回避しない。
この制限はTEST session内のもの。別journalで独立TESTを起動するテストハーネスを、
Real Pilotの全process・全再起動を通じた永続quotaとみなしてはいけない。

時間設定はAIによるOffline検証用提案であり、Real値は未承認。
観測鮮度30秒、ACCEPT期限余裕60秒、claimByまで120秒、claimByとrefundAfter間60秒、
session最大15分、承認最大120秒、観測最大64 records。
署名・送信準備・attemptのたびに承認と状態・期限を再検証する。

## 3. Worker / Gate / Signer / Secretの境界

`pilot_client.mjs`は信頼するTEST Supervisor。Workerには`worker.request`のfacadeだけを渡す。
Gateへのfd 4とWorker用IPCを分け、Worker経路の`approve`を拒否する。
Gateはadmission、各actionのsubject digestと有効期限に対する承認、認証観測の入力を担当する。
Signerは候補選択や回答評価を行わず、固定された許可条件との一致だけを検査する。
Task本文・Room message・URLが「許可する」と主張してもGateの権限にならない。

秘密鍵は別Node子processの内部でランダム生成するephemeral Ed25519 TEST keyのみ。
Seed・鍵path・Real mode・credentialの入力interfaceはない。親環境を引き継がず、
Workerへの返り値は公開preview（REVEALはredact）、digest、opaque payload handle、状態だけ。
署名済みREVEAL本文もWorkerへ返さない。mock transportは子process内部にあり、外へのpayload export APIはない。

子processはNode Permission Modelで読取りをmoduleとpin済みruntimeに限定する。
filesystem書込み、child process、worker thread、native addon等の許可を与えず、SIGUSR1によるinspector起動と
文字列からのcode生成も無効化。journal書込みはSupervisorが新規専用fileへ行い、`fsync`完了ACK後にSignerが進む。
Seed/preimageの既知表現が応答・journalへ含まれれば拒否する。例外は固定codeだけを返し、入力・stackをechoしない。
テストもraw IPC、署名済みREVEAL、鍵、preimageをEvidenceやsnapshotへ保存しない。

ただし、これは信頼するSupervisorとcapability facadeの境界であり、敵対的な同一UID processからの
メモリ読取り・debugger攻撃を防ぐOS隔離ではない。Node自身もPermission Modelをtrusted code向けの
保護と説明している（[Node公式説明](https://nodejs.org/docs/latest-v22.x/api/permissions.html)）。
WorkerへlauncherやGate objectを渡す統合は誤り。Human本人認証・承認UI・秘密保管庫は未実装。
JS文字列の完全zeroizationも保証しない。終了時はprocessを破棄し、鍵を永続credentialにしない。

## 4. Write準備、結果不明、停止とRecovery

操作状態は `PREPARED → SIGNED → WRITE_PREPARED → SEND_ATTEMPTED → OBSERVED`。
応答消失なら`AMBIGUOUS`となり、read-only照合で確認できたときだけ`RECONCILED`になる。
session/action ID、typed action、DID、room、full offer/contract/ref、Task digest、
正確な十進nonce、公式challengeのdigestを承認subjectへ固定する。
秘密を含む署名payload本体はSigner内に残し、承認後にWorkerが差し替えられない。

送信試行のjournalをdurableに保存してからmock appendする。attemptは1回のみ。
room・sender・nonce・signature・exact lineが一致する記録を見つけ、公式検証と契約foldを通す。
見つからない場合は送信されていない証明とせず、AMBIGUOUSのままHuman停止。
新nonce、新ACCEPT、別候補、同じ署名payloadの自動再送はいずれもない。
記録欠損・epoch変更・競合・署名不良は補完せず停止する。

観測には信頼するGateが与えるTESTの時刻・順序・generationを使う。
seqとvenue timestampは署名対象外で、署名検証だけでその真正性を保証しない。
現在のadapterは1契約の連続したTEST記録用で、他者ACCEPTを含むpublic board全体のimporterではない。
板全体をfoldして先着優先と解釈する実装もない。

Public HTTP送信実装はない。defaultのeffectはdisabledで、Gateが明示したときだけin-memory mock appendが可能。
GETの`say`等も含めURL・HTTP methodを受け取る経路がない。
さらにJSのfetch/socket/http/https/tls/dgram入口を停止するが、これをOSレベルnetwork隔離とは呼ばない。
今回のwrite 0の根拠は、実行コードがmockのみで公開送信adapterを持たず、既存Solverもsocket禁止下で動かしたこと。
packet captureで全host通信を監査したという主張ではない。

STOPは信頼するGateの`stop`で新しい署名・effectを拒否する。必要ならSupervisorの`close`、応答不能なら`kill`で
子processを終了する。closeは明示的な制御messageのdurable保存完了ACKを待ち、親側control FDを閉じる。
子側はEOFを受けて処理queueを完了し終了する。5秒以内に終了しなければSIGKILLする。
最終統合試験では全fixtureが強制終了なしで通常closeした（明示killを検証するcaseは別）。
STOP後のread-only reconciliationは可能だが、process終了後の自動再開は実装しない。
既存journalへの起動は`wx`で拒否し、新しい鍵を生成して継続したことにしない。
再起動でpreimageを失う設計なので、Real接続前にcustody・復旧とnonce continuityの設計が必須。

RollbackはまずこのTEST子processを停止し、journalとEvidenceを保持する。
既存Workerは今回のmoduleをimportしないため、その運用を戻す設定操作はない。
コードを戻す必要があれば、Humanレビュー後に本local commitを通常のrevert対象とする。
今回revert/reset/削除は実行せず、外部契約を取り消せるという意味でもない。

## 5. 検証と限界

開始時既存regressionは **107 tests PASS / skip 0**。
最終regressionは **108/108 PASS / skip 0、21.620秒**。既存107件と新規統合1件の合計である。
新規統合内部の **63/63 PASS**は別の粒度であり、Python件数へ足して171 testsとは報告しない。
新規統合だけの試験もPASS（15.322秒）。起動・通常close、期限余裕の再検査、型の厳密性、
重複観測の冪等性と競合拒否まで最終codeで実行した。
成功logは`.local/batch17a/regression-resumed.log`、各63項目の結果は機械可読Reportに含めた。
前回の失敗Evidenceと停止時Reportは変更せず、`.local/batch17a/stopped-report.md/json`にも保存した。
旧`regression-final.log`は停止前の失敗試行のfile名であり、今回の最終成功logではない。
同じ保存GCD資料から既存Solverと独立oracleを再実行し、fixtureのadmissionと照合する。
TEST PayerとTEST Signerは別鍵。公式PaperRailのhypothetical LOCKを使い、4 typed actionsを通す。
`claimed`は公式ProtocolのTEST終端であり、Real Counterparty acceptanceや品質合格ではない。
PaperRailのLOCK整合、署名有効、fold終端、相手の評価を別判定にする。receipt署名権限はない。

negative testsは未許可action、DID/from/room/offer/contract/party/ref不一致、承認digest差替え、
承認期限切れ、観測stale、期限切れOFFER、署名不良・不正frame、精度欠損nonce、非paper/no-value未確認、
2件目candidate、重複ACCEPT/REVEAL、LOCKなし、Paper Noteなし・非対応形式、heartbeat不足、
Delivery未観測、入力secret/command注入、disabled network、応答消失前後、保持喪失、STOP、
journal再利用拒否、同時重複要求の直列化を含む。完全一致の19桁nonceは文字列のまま検証する。

開発中の問題も記録する。初回統合testはPermission Model下の`fsync`拒否で停止し、
Signerの書込み権限を広げずSupervisor側durable journalへ修正した。
その後mock claimedまで到達したが終了EOF待ちが完了せず、試験を中断して明示closeと5秒上限を追加した。
この段階の58項目はPASS。通常終了でも強制終了待ちが発生する問題を避けるため、
fd 4を非同期Socketで読む変更を加えた。最初の拡張試験では59項目まで進んだが、
Python wrapper経由の再実行では起動失敗した。この時点で共通AGENTS.md §10に従い停止した。
Humanは同じOffline実装内のRoutine Debug継続を明示許可した。診断では、fd 4は公式import前後とも
有効なsocketである一方、既存socketの属性照会経路が`EPERM`となり、Node Socket wrapperが
`Unsupported fd type: UNKNOWN`を返すことを4試行で確認した。鍵生成なしの診断で、pinや鍵の問題ではない。
具体的なkernel filterの内部実装までは調べていない。

修正は既存FDのfilesystem readへ戻し、上記ACK→親FD close→EOF→queue drainの終了順を設けたもの。
socket種別の推測を不要にし、読取りthreadが終了を妨げる問題も解消した。子の権限・依存・署名actionは増やしていない。
診断は`.local/batch17a/fd-probe.mjs`と`fd-diagnosis.json`で追跡可能。
期限fixtureの旧refも最終63項目で再検証済み。診断用import path誤りを含む過去失敗は成功件数に含めない。

再現コマンド（このworkspaceの保存済みBatch 15/16/17A資料とNode v22.22.0が前提）：

```bash
python3 -B -m unittest discover -s tests -v
node tests/pilot_signer.mjs
```

新しいcloneで欠けているruntime・Evidenceを自動取得しない。pin検証失敗時は停止する。
テスト生成物は`.local/batch17a/`だけで、秘密を除いたsummary・journal・Solver検証結果を保存する。
既存11,495ファイルのhash保持と最終test内訳は[機械可読Report](typed-signer-batch17a-20260919.json)に記録する。

## 6. Independent Reviewと次のHuman Gate

レビュー順序は[protocol/pin guard](../src/collaboration_agent/pilot_protocol.mjs)、
[Signer](../src/collaboration_agent/pilot_signer.mjs)、[Supervisor/facade](../src/collaboration_agent/pilot_client.mjs)、
[negative tests](../tests/pilot_signer.mjs)。別人によるIndependent Reviewは未実施。
特に、Gate能力の渡し先、署名前/attempt前の再検証、REVEAL非開示、durable SEND_ATTEMPTEDと再送禁止、
署名とPaperRail検証の違い、終了時の曖昧さを確認してほしい。

**Humanが決定済み:** 今回はOfflineだけ、既存Project DIDを将来のAnchorとする、Real Secret・Real Write禁止、
local commit可、1件Pilot・個別承認が基本、Batch 17Bを自動開始しない。

**今回のAI提案:** GCDの確認済みformatを最初の1件に選び、4 actionsを各1回・個別承認とする。
ACCEPT前に解答を独立検証し、LOCKとrail記録の両方を確認してからDeliveryし、観測後にREVEALする。
上記の時間余裕を下回れば停止する。未LOCK・相手無応答・不明結果でも次候補へ進まない。
事前委任・自動ACCEPTを必要とするEvidenceは今回もない。

**Humanが今後決める事項:** このaction範囲・対象Task・1候補1契約の数え方、heartbeatを含む各承認主体、
時間上限、公開するDID/answer/hash/preimageの範囲、監視中止時の責任、鍵管理とRecovery方針。
SignerにReal鍵を渡すことは今回の設計承認に含まれない。Worst Caseは、将来のReal送信で
ACCEPTだけ残る、納品済みでもREVEALできない、秘密公開後に相手が無応答、結果不明で停止すること。
no-valueでも公開DIDの履歴と評価は残り得る。成功率・リスク確率の数値は作らない。

Batch 17B前のMandatory残件は以下に限定する。

1. Independent Reviewと、別途のHuman許可。TEST GateをHuman本人認証と誤認しないこと。
2. 既存DIDを維持するReal鍵境界・秘密保管/復旧・永続nonce allocator・全sessionでの1契約上限の設計。
   今回のTEST key生成をReal DIDへ置換するだけでは成立しない。Secretへのアクセスは別Gateまで禁止。
3. Real OFFER/job/full-specとverified answerのadmission binding。現在はTEST専用contextとasset=PAPERに限定し、
   実記録のFLOP-on-paper等を自動受理しない。public boardから対象契約を抽出する観測adapterも必要。
4. 狭い送信adapterと永続照合資料のレビュー。秘密をroutine logへ出さず、失われた応答とprocess crashのRecoveryを確定する。
5. Batch 16で見つかった実Paper Noteの非対応JSON、およびclaim後のNote更新主体を解決する。
   正しいLOCKだけでrail整合・納品合格を認定しない。

他Solverのtyped Delivery、汎用transport、heartbeat定期実行、receipt署名、統計精密化、Read-only改修は後回し。
未解決事項を理由にReal鍵へ接続したり、Batch 17Bを開始したりしない。
