# Real Connection Offline Integration — 2026-09-22

## Checkpointと範囲

- 開始実測: `54eef13c19ff22abb86060c8e34fab74b1220260`、worktree clean。
- 一次資料: `real-connection-hardening-batch17a1-20260920.md`、既存Signer/Approval/ledger/protocol/tests、保存済み公式runtime。
- Project identity anchor: `<PROJECT_DID>`。新Real DIDを作成していない。
- 配置モードは **DUMMY_OFFLINEのみ**。既存Real鍵の探索・読取・import、Real signed frame、External Write、pushは実施していない。
- 完了判定は下記のObserved Runtimeとレビュー結果を参照する。設定を作成しただけでOS分離を達成したとは扱わない。

## 実装

既存 `pilot_signer.mjs` のtyped action、OFFER/contract検証、canonical approval、署名の出力guard、reconciliationを再利用する。
新しいSignerを別実装で作り直していない。既存TESTプロセス経路は維持し、`--offline-connection` にsystemd socket adapterを追加した。
旧TEST内部ledgerのmode表記は互換性のため維持するが、接続adapterと公開metadataはDUMMY_OFFLINEであり、ephemeral custodyとは異なる。

| 役割 | service / user | IPC能力 |
| --- | --- | --- |
| Worker | `collab-worker.service` / `collab-worker` | root所有Worker socketへのstatus / prepare / sign / prepareWriteのみ |
| Approval | `collab-approval.service` / `collab-approval` | Human UID専用socketを受付し、Signer専用Gateへ接続 |
| Signer | `collab-signer.service` / `collab-signer` | Worker/Gate別のsocket activation FD。dummy credential、暗号化custody、台帳の専用領域 |
| Transport | `collab-transport.service` / `collab-transport` | Signer group専用socket。署名検証とdigest ACKのみ。POST実装・destination引数・retryなし |

全socketの親はroot所有。Human socketは指定Human UIDの0600、Gateはroot:collab-approvalの0660、
Transport socketはroot:collab-signerの0660。peer UID APIの代わりにkernelのsocket DACを利用する。
Human sessionは信頼境界内であり、Human UID自体を侵害された場合の認証基盤は追加していない。

配置するコード/runtime/Node実行ファイルはroot所有へコピーし、公式runtimeの既存pinを照合する。
Workerからwrite可能なcheckoutをserviceが直接実行する構成にはしない。
NoNewPrivileges、ProtectHome、ProtectSystem、ProtectProc、core禁止、capability除去、Node child-process禁止を設定する。
Signer/Approval/WorkerはPrivateNetworkとAF_UNIX限定。Transportはnetwork familyを持つが、このcheckpointのコードは送信を行わない。
Nodeが必要とするthread生成まで一律に禁止しない。Signer起動時に実際のINET socket生成とchild process起動を試し、拒否を観測できなければ起動を拒否する。

## Human承認

`connection_terminal.mjs` はHuman UIDとTTYを確認し、専用Approval endpointからcanonical packetを取得する。
表示はAction、DID、room、OFFER、contract、Rail、public payload、payload digest、deadline、公開内容。
表示文字列はJSON escapeし、外部payload中のterminal control byteを実行させない。
`ACTION + approvalDigest先頭16桁`の完全一致入力を要求する。
Approval側がpacketを再取得・digestと明示入力phraseを再検証し、Signerがaction/subject/expiry/one-useを検証する。
Human UIDで動く任意のprocessと実際の人間の打鍵をOS sessionだけで区別することはできない。
同じHuman UIDのprocessはtrust boundary内であり、dummy smokeはこの経路をTEST自動操作する。実際のHuman操作を試験済みとは主張しない。
ACCEPT、Delivery、REVEALは別承認。REVEALはpreimage公開を明記し、秘密本文は表示しない。
REVEAL完全payloadの再計算は引き続きSigner境界内であり、外側で秘密本文を再構成したとは主張しない。

## Secretとpreimage

`setup.py` はdummy 32 bytesをメモリで生成し、anonymous pipeから `systemd-creds encrypt` へ渡す。
保存するのは暗号文だけ。serviceは `LoadCredentialEncrypted` のservice限定credentialを読む。
setupで生成したdummyのSHA-256 fingerprintをroot所有configへ固定し、runtimeで完全一致を要求する。
別の32-byte credentialへの誤置換を拒否する。fingerprintはdummyに対する照合値であり、秘密bytes自体ではない。
envにはcredential directoryのパスのみ、argvに秘密値はない。stdout/stderrはnull。Real鍵を使うfallbackはない。
host credential keyが既存かどうかを変更前記録へ保存する。新規生成された場合も共用systemd資源のため自動削除しない。

custody keyはdummy seedから用途を分離したHKDF-SHA256で導出する。
preimageはメモリ内で生成し、公式 `makeAccept` でcontractを確定後、DID/contract/revisionをAADに結合したAES-256-GCMで保存する。
公式contract IDはstatementとaccept nonceに依存するため、preimage生成前に最終contract IDを計算することはできない。
この順序上の制約があるが、**contractへのbinding、暗号化保存、file/dir fsync、再読込、hash一致確認のすべてがACCEPT準備より先**となる。

stateと別のhigh-water witnessをatomic replaceで更新し、readbackする。途中失敗はfail-closedであり、自動rollbackしない。
署名前もstate/witness/pointerを再読込する。missing/corrupt/wrong contract/wrong keyは拒否する。
再起動後は同じpreimage/commitmentを復旧するが、自動署名・自動送信を再開せずreconciliation-onlyとなる。
このcheckpointにrestart後のREVEAL再開操作はない。復旧可能なcustodyと、外部actionの再開許可は別である。
JavaScript heap上のstringを完全にzeroizeしたとは主張しない。

## Nonce

runtimeの固定DID台帳とcustody専用pathを単一authorityとする。別journalや別contractで枠を再利用しない。
room別にdecimal string/BigIntで `max(current ms, durable reserved + 1, exact observed + 1)` を計算する。
最大19桁を超えれば停止する。署名前にreservationをfsync/readbackし、未解決予約やAMBIGUOUSがあれば新規発行を拒否する。
SEND_ATTEMPTED後の結果不明は再利用・自動retryを許可しない。read-only reconciliationの結果でquotaを戻さない。

Project DIDの状態は引き続き **NO PRIOR NONCE OBSERVED / CURRENT LIVE STATE UNVERIFIED**。
今回Live GETは行っていない。保存済み23,809件の過去観測を現在nonce=0へ読み替えない。
dummy smokeのobservedNoneは独立したTEST identityに対するTEST venue条件であり、Project DIDのEvidenceではない。

## 検証とObserved Runtime

| 検証 | 実測結果 |
| --- | --- |
| 開始regression | 108/108 PASS、47.818秒 |
| Review修正後regression（Signer統合91項目を内包） | 108/108 PASS、49.573秒（修正前48.966秒もPASS） |
| 既存Signer targeted | 28/28 PASS |
| custody/nonce専用 | 12/12 PASS。SIGKILL、fsync故障、rollback/corruption、exact integerを含む |
| Approval terminal / service policy / credential pin | 19/19 PASS。TTYはtest stream、変更packet拒否はtrusted adapter契約のstubも使用 |
| 実Unix socket IPC | 4/4 PASS。ホスト通常UIDで実施、root不要 |
| Historical Evidence | 11,946 files、変更0 |
| 実systemd / cross UID / credential | **ホストcheckpoint未実行・未確認** |
| Independent Review | 別Agent（GPT-5.6 Sol）がcommit `2d87b93` をread-only review。2件を指摘し、修正patchを独立再確認して双方解消。Offline conditional GO / Real NO GO |

Python108件には既存Signer統合が内包されるため、内部91項目をPython件数に加算しない。
systemd 255、WSL2、ホストPID1=systemd、systemd runningはsandbox外で確認した。
sandbox内PID1はcodexであり、sandbox内の見かけをホストのsystemd非稼働と誤認しない。
`sudo -n true` は認証要求で失敗。Humanはホスト端末での限定セットアップ実行が可能と回答した。
OS user作成・service配置をAgent自身が実行済みとはまだ記載しない。

Evidence: `.local/batch17a1/connection-baseline.json`、`connection-regression.json`、`connection-review-fix-regression.json`、`connection-historical.json`。
ホスト実行時は `.local/connection/host-checkpoint.json` とroot保護の `/var/lib/collab-connection-setup/` にsanitized結果を記録する。
raw stderr、署名、preimage、private keyは保存しない。

## Failure → Cause → Fix → Verification

- sudo非対話実行: ホストが認証を要求。権限を迂回せず、Humanが実行する限定root入口を用意。実機検証は結果待ち。
- Unix socket test: sandboxでbindがEPERM。承認されたhost実行へ移し4/4 PASS。sandbox境界と製品不具合を区別した。
- storage統合確認: prepare後のstate破損を署名前に再確認する必要があったため、ACCEPT readinessでstate/witness/pointerを再読込するよう修正。専用test PASS。
- nonce観測更新: observedNoneが既存observed high-waterを消さないよう保持し、19桁上限も追加。専用test PASS。
- IPC実装確認: client half-close後も非同期ACKを返せるようserverのallowHalfOpenを指定。実socket test PASS。
- Independent Review: terminalだけでなくApproval serviceでもcanonical ACTION+digest phraseを必須化するよう指摘。service側validatorを追加し、missing/wrong actionの拒否を検証した。
- Independent Review: Project DID以外というだけでは別の実鍵の誤接続を拒否できないと指摘。setup生成dummyのfingerprintをroot configに固定し、署名鍵生成前に照合するよう修正。wrong/missing pinの拒否を検証した。

## ホスト実行・rollback・復旧

全操作は専用dummy資源に限定する。既存の同名user/group/unit/pathがあれば上書きせず停止する。

```bash
sudo python3 -I deploy/connection/host_checkpoint.py "$(command -v node)"
```

入口は、変更前記録→専用user→dummy credential→probe service→root-owned deployment→専用socket/service→
dummy承認/署名/private handoff→権限・env/argv/journal照合→SIGKILL→dead-owner確認→lockだけ回収→restart/reconciliationを実施する。
一部失敗後は自動再実行せず、記録を保全して原因を確認する。

安全な停止は専用socketを先に停止し、次に専用serviceを停止する。いずれもboot enableしない。
rollbackはservice停止後に今回配置したcode/unitを無効化し、daemon-reloadする。
**custody・nonce・identity ledger・quotaを過去snapshotへ戻さない。** state/witnessを整合した過去ペアへ同時rollbackする管理者操作は、この方式では検出できない。
user、共用systemd host credential key、用途不明資源を削除しない。完全撤去が必要なら変更前記録と作成物を照合した別作業にする。

crash復旧は、まずsocket/serviceを停止し、owner PIDが死亡していること、contract/nonce/attemptの状態を確認する。
TEST専用 `verify.py crash-recover` はdummy modeを確認し、dead ownerと一致する2つのlockだけを削除/fsyncする。
暗号文・台帳・quotaは維持する。次の起動はreconciliation-onlyであり、同じ予約の再送や新nonceの自動発行を行わない。
Actual power-loss・disk hardwareの耐久試験は未実施。fsync barrierとprocess crashの検証を、power-loss実測と同一視しない。

## 残るGateとレビュー対象

Real Secret接続前: 実機境界PASS、Independent Review、Humanによる既存Source of Truthと接続操作の承認が必要。
現在の配置はdummy credentialしか受け付けず、Project DIDの鍵が入力された場合も拒否する。
Real有効化を単純なenv切替で行わない。Human保有の既存保管元からanonymous pipe→systemd encrypted credentialへの限定配送を確認し、
期待DIDの厳密照合、復旧手順、root/credential trustを再確認する。env/argv/plain fileへのfallbackを認めない。

External Write前: 対象OFFER/contract/独立検証済みanswer、Current public nonce観測、fresh/lossless観測経路、
固定送信先への限定POST adapter、action別Human承認が必要。現Transportはoffline validationのみで、送信能力を有効化していない。
このcheckpointはReal Write GOを意味しない。

Independent Reviewで見る点: socket所有権とHuman UID、root-owned code配置、systemd credential配送、
暗号化stateとpublic ledger間のcrash窓、durability gate、nonce単一authority、replay/expiry、private handoffの出力抑止。
特にruntimeのDUMMY専用制約と、旧TESTのmock操作がsystemd endpointへ露出しないことを確認する。

## Independent Reviewと中間引継ぎ

Primaryと別AgentのGPT-5.6 Solが、初回commit `2d87b934ce612f7ea66fe6628a6b1f65ea4506d6` を読み取り専用でレビューした。
指摘2件を修正し、固定patch SHA-256 `b1043f4f85c06c8c34b72b7fea65cc626ed6323c8cee27bd3adbc6142ea84085` を別途再確認した。
Reviewerはpatch一致、reverse apply check、Approval/credential pin 19/19、構文検査を独立にPASSと確認し、双方の指摘を解消と判断した。
追加の具体的阻害要因は確認されなかった。これはPrimaryのself-reviewではない。

判定は **Offline conditional GO**。実systemd / cross-UID / credential配送 / service crash-restartがホスト上でPASSすることが条件である。
その結果はまだ未着であり、今回のDone条件をすべて満たしたとは主張しない。
終了HEADは最終応答と `.local/connection/` のlocal checkpointに記録する。
