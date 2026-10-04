# First Real ACCEPT Preparation — 2026-09-22

開始HEAD: `62891e90ef226eb49d16911dafdfccc0b54d8832` / `main` / clean。
今回の成果物はOfflineのコード・test・配置レビュー用template・local commit。
**Real activation / Real write GOではない。次はClaude Code Independent Review。**
Real Secret access 0、External Write 0、Public GET 0、host systemd変更0、pushなし。

## 境界と追加経路

```text
Worker → typed candidate / prepare / sign / prepareWrite
                         ↓
Human terminal → Approval → Signer（AF_UNIXのみ、credential・custody・nonce・ledger）
                         ↓ signed record + exact approval + signed OFFER
Human sendAccept → Signer durable SEND_ATTEMPTED → dedicated Real Transport
                         ↓ fixed Paper ACCEPT GET（将来の明示有効化時だけ）
                  STOP / AMBIGUOUS → public read → reconciliation
```

- 既存typed Signer、canonicalApproval、Approval socket、ConnectionStore、AsyncPilotLedgerを再利用。
- `REAL_ACCEPT_PREPARATION`をroot所有configから明示選択。credentialは`project-seed`、固定Project DID以外は拒否。
- `externalWriteEnabled`は別の必須boolean。templateは`false`。credential投入や署名だけではwriteを有効にしない。
- `prepareWrite`はTransport policy検証だけ。Worker socketには`sendAccept`を公開しない。
- Human専用Approval→Gateからの`sendAccept`だけが送信を試みる。既存承認の有効期限内であることを再検証。
- `connection_terminal.mjs ACTION_ID`で従来同様に承認、別途`--send-accept ACTION_ID`でexact previewを再表示し、`SEND ACCEPT <digest-prefix>`を要求。
- dummy installerは従来どおり`DUMMY_OFFLINE`を配置する。templateを自動適用する処理はない。
- 既存`--offline-connection` entrypoint名とledger内部の`TEST_EPHEMERAL` v1タグは保存互換用。公開mode・admission・credential・Transport policyでReal準備を区別し、legacy mock操作をRealへ公開しない。

Human UIDは従来どおり信頼境界内。Human UIDの任意processと実際の打鍵を新しい認証機構で区別したとは主張しない。

## Secret handoff / DID

`deploy/connection/handoff_real.mjs`は将来の限定配送入口。今回production CLIやsystemd-credsは実行していない。

1. Humanが既存保管元を選ぶ。保管元の選択・探索・Real credential要求は今回行わない。
2. raw **32-byte Ed25519 seed**をanonymous pipeのstdin、または`--input-fd N`で渡す。hex/base64、argv/env、通常file、TTYへfallbackしない。
3. pin済みofficial `signerFromSeed`によるDIDが下記と完全一致した場合だけ次へ進む。
4. `/usr/bin/systemd-creds encrypt --with-key=host --name=project-seed - -`へanonymous pipe配送。
5. 固定先`/var/lib/collab-connection-setup/project-seed.cred`へ暗号文のみを排他的作成・fsync。既存ファイル上書き拒否。
6. 将来のSigner drop-inで`LoadCredentialEncrypted=project-seed:...`。Signerだけのcredential directoryから再度DIDを検証する。

固定Identity: `<PROJECT_DID>`。
CLIにexpected DID変更、secret引数、env、出力先変更の経路はない。stdout/stderrはsilent、失敗は非zero exit。
JS helperの期待test DID / adapter注入はOffline test用の信頼済み関数引数であり、production IPC/config/CLIのoverrideではない。
Signerの既存raw signing closureは境界内に留まり、Workerにはopaque action handleだけを返す。
JS heap・stream内部の全copyを完全zeroizeできるとは主張しない。永続保存は暗号文のみ。

## nonce / public observation

- decimal string最大19桁、比較・加算はBigInt。transport nonceとACCEPT frame内のhex nonceは別物。
- 既存式`max(time_ms, durable_reserved + 1, confirmed_public + 1)`を変更しない。
- durable reservation→Human approval→署名の順。未解決reservation、重複request、stale observationは拒否。
- `real_nonce.py:observe_project_nonce()`は既存`material_fetch.transport`に固定`https://technocore.chat/r/tclk-offers?format=json&limit=200`を1回だけ渡す。read transportは無変更。
- 上限131072 bytes、generation 1、seq 1から応答last_seqまで全件連続、count/metadata一致のみ採用。tail、gap、generation変化、部分応答はSTOP。
- Pythonのlossless整数parse→decimal string→Signer側`real_nonce.mjs`でBigInt比較。対象DIDの全recordはofficial署名検証を通す。
- packetは`source`（固定URL/status/raw digest/capture時刻）、`coverage`、全normalized records、observedNonce/observedNoneを含む。Human専用`nonceObservation` Gateへ渡す。raw HTTP取得の出所はtrusted read/Human側境界、署名・coverage・nonceの整合はSigner側で再検証する。
- 観測は30秒以内。nonce予約時にも時刻を再確認。範囲内にDIDが見つからないだけではabsenceを認定しない。

**現在のProject DIDのlive nonceは未確認。** この実装は200件を越える現在roomの完全履歴を取得できない。保存済み過去boardは現在の証拠に代用しない。実観測が不十分なら開始を拒否し、追加の十分な観測経路は別レビュー・承認対象とする。巨大crawlerやnonce=0 fallbackは実装していない。
単一writer運用をHumanが確認する必要があり、他toolの同時writeをこのローカルauthorityでは封鎖できない。

## exact Stage 1 policy

Source of Truth: `tclk_pin.json`のcommit `5cc4ab93efbc8999a3a7e1471b639deca25998ea`、保存済みofficial runtimeと同commitのsource。
`frames` / `paper-rail` / `transcript` / `signing`および`examples/htlc-walkthrough.md`をOfflineで確認。

- 固定origin `https://technocore.chat:443`、room `tclk-offers`。
- endpointはGET `/r/tclk-offers/say-signed/<did>/<sig>/<transport-nonce>/<percent-encoded-canonical-line>`。callerはURLを渡さない。
- type `ACCEPT`のみ。signed OFFERをverifyし、canonical `asset: "PAPER"`, `rails: ["paper"]`, `lock: "hash"`, payer roleを要求。
- amountは元OFFERの正整数文字列にbindする。公式PaperRailはno-valueであり、amount=0という架空の形式は作らない。
- ACCEPT required fieldsは`type,from,ref,statement,contract,nonce`。official `makeAccept`で再構築しcanonical bytes一致を確認。paymentKeyは禁止。
- approvalはDID/type/room/OFFER/contract/ref/exact publicLine/nonce/payload digest/session/action/expiryをbind。元OFFERの期限・statementも照合。
- 既存GCD candidate、`TEST GCD bundle sha256=...`というsource bindingを維持。admissionは`EXPLICIT_PAPER_NO_VALUE`、heartbeatRequired=false。汎用contract admissionへ拡張しない。
- arbitrary room/URL/message、unsigned、Delivery、REVEAL、heartbeat、value rail、unknown type、missing/mismatched/expired approval、nonce mismatchは拒否。

## 1回制限 / network / ambiguity

Signerは送信前にcustody nonceをSEND_ATTEMPTEDへ永続化し、ledgerもSEND_ATTEMPTEDを永続化してからTransportへIPCする。
Transportは専用StateDirectoryの**固定one-shot receipt**を排他的作成・fsync/readbackしてからネットワークへ進む。別action/nonce/contractも2件目は拒否。receiptは送信capability消費記録であり、別nonce authorityではない。

直接Node HTTPS、`agent:false`、固定origin、環境proxy不使用、redirect followなし、retry loopなし。
総timeout 3000ms、response cap 16384 bytes、圧縮応答を成功とするdecodeはない。URL・署名・body・例外本文をlogやreceiptへ保存しない。

pin資料からsigned GETの確定ACK schemaは特定できないため、**200も含めすべて送信後はAMBIGUOUS/STOP**。
redirect/HTTP failure/timeout/reset/DNS・接続失敗も、dispatchが始まった後は不明として保持する。検証やdurable claim前の拒否はnetwork attempt 0。
HTTP failureを「未送信だった」と決め付けず、同signed actionも新nonceも自動再送しない。
Transport receiptがあるがSigner ledger更新が未完、またはnonce/ledger間のcrash窓がある場合もfail-closed。消費枠を戻さない。

## STOP / deactivate（将来のHuman承認操作。今回未実行）

1. 稼働中ならHuman Gateの`stop`を要求し、Worker/Human/write ingressを閉じる。root operatorは既存専用socketを先に停止する。
2. 停止対象socketは`collab-worker-control.socket`, `collab-human.socket`, `collab-worker.socket`, `collab-gate.socket`, `collab-transport.socket`。socket activationで再起動させない。
3. Transport serviceを停止、次にSigner serviceを停止。Worker/Approval serviceも停止する。停止確認前にcredentialを消さない。
4. root configを`externalWriteEnabled:false`へ戻す。boot enableや自動restartはしない。
5. 将来のdeactivation承認の下で、固定暗号文`/var/lib/collab-connection-setup/project-seed.cred`だけをhostから除去し、Signer用LoadCredentialEncrypted設定を無効化する。共有systemd host keyや他credentialは削除しない。
6. custody/nonce/ledger/approval/action/quota/Transport receiptは保全する。秘密除去が目的でも状態を過去snapshotへ戻さない。

in-flight requestの取り消しや既に公開されたrecordの撤回は保証しない。local credential削除はleaked keyのcryptographic revokeではない。既存DIDのrotationや新DID作成、過去署名の消去は行わない。

## recovery

- Real custodyは`/var/lib/collab-signer/real-accept-custody`。dummy custodyと区別し、identity ledgerは既存DID固定namespaceを共有する。
- ledger内のapproval binding/expiryも保存。既存v1 ledger（approval field無し）は読めるが、再起動後に署名/送信を復活させない。
- STOPしたままledger、nonce ciphertext/high-water witness/pointer、quota、approval/action、Transport receiptを記録・照合する。
- crash後は全socket/service停止とlock owner PID死亡を確認。必要なlock回収は、Signer ledgerのProject DID該当lockとReal custody lockの**記録したdead owner一致**だけに限定し、親directoryをfsyncする。Live ownerやowner不明なら停止する。
- dummy専用`recover_smoke.py` / `verify.py crash-recover`はRealへ使わない。Real hostへのlock回収・再起動は別のHuman承認操作。今回のrootless E2Eだけがdummy keyでそのinvariantを試験した。
- root configをwrite disabledに保ち、同credential・同保存状態で再起動するとreconciliation-only。新規admit/prepare/sign/sendを拒否する。credentialだけを投入して起動した後のrestartも保守的にreconciliation-onlyとなる。
- 公開recordのexact envelope digest（署名含む）が一致し、既存fold/連続record検証を通った場合だけ`reconcileRestart`または稼働中`observe`でRECONCILED_PRESENTへ進める。absenceやtimeoutで予約・quotaを解放しない。
- store更新後ledger更新前にcrashしてもnonce側RECONCILED_PRESENTは再度consumeせず、ledger照合をやり直せる。再送能力は付与しない。
- stateとwitness両方を管理者が整合した過去snapshotへ巻き戻す操作の検出、実power loss耐久性、Real host配置は未検証。

## Offline検証と次のGate

`connection_real_activation.py`は一時checkoutにfake deterministic seedのDIDを固定し、全roleをseccompでINET拒否、Transportをfake adapterへ置換する。
production config/IPCにtest DID overrideはない。4-role実Unix IPC、Human approval、19桁exact nonce、durable custody、署名、exact GET request生成、one-shot、AMBIGUOUS restart、公開照合、秘密がargv/env/service output/Worker responseへ出ないことを検証する。
この試験は本物のHuman打鍵、Real Secret、host systemd UID分離、実Technocore ACKの試験ではない。

検証実測:

| 検証 | 結果 |
| --- | --- |
| 通常Offline suite（既存Signer統合を含む） | 114/114 PASS、48.823秒 |
| connection / host model / IPC / recovery / Real E2E | 67/67 PASS、最終9.533秒 |
| credential | 26/26 PASS |
| Signer側nonce packet | 15/15 PASS |
| 既存Approval | 19/19 PASS |
| 既存custody/nonce store | 12/12 PASS |
| dedicated Transport policy / HTTPS fake / durable one-shot | PASS、retry count 0 |
| normal smoke / material smoke | PASS / PASS |
| Historical Evidence | 11,946ファイル照合、変更0 |

件数は重複加算しない。新規Python nonce 6件は114件内、Real E2E 3件は67件内。
初回E2EはsandboxのUnix bind EPERMで未実行。許可されたrootlessローカルIPC実行へ移行後、test期待件数の`>20`を`>=20`へ修正してPASS。実サービス不具合やReal operationは発生していない。
終了commit・worktree状態は最終報告を参照。歴史的Evidenceは変更していない。
次に必要なのはClaude Code Independent Review。その後もroot-owned配置/templateの採用、credential保管元とhandoff、十分なfresh public nonce観測、対象Paper OFFER、単一writer、write有効化とexact actionのHuman承認はそれぞれ未実施。
