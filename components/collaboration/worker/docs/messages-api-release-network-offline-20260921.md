# Messages API release / network path — Offline

Real releaseはclosedのまま。実credential、Anthropic通信、課金、Real permit発行、
Production/VPS変更、push/PR/publishは行っていない。

## Repositoryと調査結果

開始: `main / bcd511cebcd2f2395a62c3e0814fb23557a35364 / clean`。
作業場所: `<REPO_ROOT>/components/collaboration/worker`。
指定checkpoint `8a48c7093a9f5d516b467a60f8aa9fbdcd9098a9`からの差は、
AGENTS.mdの局所修正・再検証方針の更新だけ（1 commit）。その変更を保持した。
別worktree `collaboration-worker-simple`（`feat/b1-simple-brain`）は変更していない。

調査した現行経路:

1. `runtime_service.main --api-real-credential-fd`でActivity socketとprivate credential socketを分離。
2. policy照合 → Human permit照合 → release判定 → consumed永続化 → credential受領。
3. `execute_offline(..., real=True)`が再照合し、匿名socketのexchangeでlauncherを起動。
4. launcherがpermit/release/consumed/code/dependencyを照合し、TLS contextを作成。
   Landlock/seccomp適用 → ready → credential/request受領 → expiry確認。
5. `sdk_request`が固定Messages引数を生成し、single-use transportでmethod/URL/body/expiryを確認。
   pinned HTTP transport → `socket.create_connection`/`getaddrinfo` → TLS/SNI → POST。
6. bounded raw response → strict mapper/Secret scan/引用検査 → Serviceの再検査 → typed Result/Preview。

実装前からpermit、releaseの多段拒否、消費順序、固定endpoint、retry/redirect/proxy/fallback禁止、
POST 0〜1、Output Boundaryは接続済みだった。HTTPS用のdefault SSL contextも存在した。
一方、launcherのTCP-only seccompに対し、resolver設定fileのLandlock許可とTCP DNS指定がなかった。
従来candidate probeは数値loopbackのHTTPであり、このDNS/TLS経路を実行していなかった。

## 今回の変更と保証範囲

Real launcherだけが、環境を消去した後に `RES_OPTIONS=use-vc timeout:2 attempts:1` を固定する。
Landlock read追加は `/etc/resolv.conf`、`/etc/nsswitch.conf`、`/etc/hosts`、
`/etc/host.conf`、`/etc/gai.conf` の存在する個別fileとそのresolve先だけ。
`/etc`全体、UDP、UNIX socket、bind/listen/accept、exec/fork、filesystem writeは追加しない。
既存CA pathとpre-confinement default SSL contextを利用し、Real transport入口では
`CERT_REQUIRED`と`check_hostname=True`を要求する。

固定URLは `https://api.anthropic.com/v1/messages`、modelは `claude-sonnet-4-6`。
wall 60秒 / HTTP 45秒、max_tokens 4096、input 16KiB、raw 256KiB、Result 32KiBを維持。
Real Evidenceの`usage_source`をprovider accounting未取得という記述に修正した。
usage/costは引き続きnullであり、responseのcounterを確定請求と扱わない。

OSが保証するのはAPI子のsocket種別制限等であり、provider-only egressではない。
TCPはDNS resolverを含む任意の到達可能宛先へ接続できる。Landlockはここではfilesystem制限で、
宛先IP/domain/port allowlistやkernelによるPOST数制限を実装していない。
POST上限と固定URLはreview対象のpinned application codeの保証である。
DNSのA/AAAA問い合わせやIP接続試行はHTTP request数とは別である。

system resolver/CA/OS/interpreterと未隔離同UID supervisorは既存信頼基盤。
TCP DNS非対応resolverや追加NSS backendを必要とする環境への自動切替はない。
実環境のDNS到達性、実provider証明書、account/model/quota/billingはOffline成功では確認できない。
RECORD外file、検査後置換、全import closure、Python memory zeroization、未知のSecret符号化、
既存異常signalの残Riskの保証範囲は拡大していない。

## 検証

全unit suiteは既存129件＋TLS条件1件の**130件PASS / 47.303秒**。
`python3 -B src/ccw/isolation.py`はLandlock ABI 3を確認。
READMEの通常smoke、B1、Secret Handoff、Boundary、ABC、consumer window、Runtime ServiceはPASS。
既存CLI fake probeはexit 0 / `PASS_OFFLINE_WITH_LIVE_BLOCKER`で、既知のSSE error時3 POSTを再現。
CLI Railを再開したものではなく、既存NO-GOの回帰確認のみ。

最初のまとめ実行では開発sandboxが匿名socketやnamespace内socket操作をEPERMで拒否し、
Secret各probe、Runtime Service、CLI fake probeが失敗した。許可されたローカル実行で個別に再実行してPASS。
CCWのLandlock/seccompは解除していない。失敗Evidenceも保持した。

既存smokeの最終Evidence（すべてignored）:

- `.local/smoke-mlzosmt2/summary.json`
- `.local/b1-smoke-734t36xu/`
- `.local/secret-handoff-goomihla/`
- `.local/secret-boundary-y97n_0fm/`
- `.local/secret-abc-qxsbs7wk/`
- `.local/secret-consumer-window-5hnae8pq/`
- `.local/runtime-service-smoke-gzyygcwe/`
- `.local/real-cli-o9g8oyye/summary.json`
- 初回sandbox内実行ログ: `.local/release-network-regression-6h0ond_1/`

`python3 -B tests/messages_api_probe.py --rail`: **PASS_OFFLINE_API_RAIL**。
48 request cases / runtime 48 / POST 46 / auxiliary 0。接続拒否2件のみPOST 0。
48件の再利用拒否、Gate拒否12件、proxy request 0を維持。
P3の集計修正: setup isolation subprocessの実Popen後observationも収録し、
`setup_runtime_starts=1`、`runtime_process_starts_observed=49`とした。
従来の48はrequest matrix内の数で、全runtime processの数ではなかった。
Service/Activity/harness process全体の起動数とは区別する。
Evidence: `.local/messages-api-wx98mlbh/summary.json`。

`python3 -B tests/messages_api_network_probe.py`: **PASS_OFFLINE_REAL_NETWORK_PATH**。
実Serviceの`main --api-real-credential-fd`、実launcher/SDKをsubprocessで実行。
user/net/mount namespaceのloopbackだけにfake TCP DNSとTLS serverを置き、
固定hostname・port 443・pathを維持した。namespace内だけでresolv.confをbind mountし、
fixture側にはuse-vcを設定せず、production launcherのRES_OPTIONSを検証した。
新しい依存installはなく、既存opensslでローカルテスト用CA/証明書を生成した。

| ケース | runtime起動 | POST | 結果 |
| --- | ---: | ---: | --- |
| 出荷コードのrelease closed | 0 | 0 | DNSも0、未消費、拒否 |
| 信頼されないCA | 1 | 0 | TLS後のHTTP送信なし、消費済み |
| hostname不一致 | 1 | 0 | TLS後のHTTP送信なし、消費済み |
| 307 redirect | 1 | 1 | 追従なし、拒否、消費済み |
| credentialを含む応答 | 1 | 1 | Output Boundary拒否、消費済み |
| 正常TLS応答 | 1 | 1 | Real RuntimeClientからuntrusted Previewまで成功 |
| **計** | **5** | **3** | **全件auxiliary 0** |

接続した5ケースでTCP DNSのA/AAAA、SNI `api.anthropic.com`を観測。
HTTPを受けた3ケースはTLSv1.3 / TLS_AES_256_GCM_SHA384。
消費済み5件はすべて再利用を拒否し追加POSTなし、Evidence不変。
Evidence: `.local/messages-api-network-ptgunj24/summary.json`。
新規network probeの初回sandbox実行ではloopbackの特権port bindが拒否され、
正規のランタイム承認経路でローカル再実行した。host側の権限・設定は変更していない。

このrehearsalは**テストprocess内だけ**でrelease判定を差し替え、test CAを信頼させる。
Service/launcherの各bootstrapでprivate loopback namespaceを検査してからpatchする。
TLS verificationは無効化していない。endpointやSDK transportをfakeへ差し替えていない。
permit fileはテスト専用合成fixtureで、Humanの`authorize`は呼ばず、Real利用許可を発行しない。
launcher commandの差替えは同じlauncher.mainを呼ぶテストwrapperのためだけであり、
admission/consumed/credential/exchange/expiry/Output Boundaryは実コードを通す。
出荷コード自体のreleaseは常時closedで、環境変数やCLIの解除flagを追加していない。
Realの成功を確認したのはプロトコル経路だけで、実provider推論・課金の成功ではない。

## Findingsと残るGate

- P0/P1: 現時点で新規検出なし。
- P2: Real候補の通常DNSが現行sandboxで成立する設計になっていなかった。
  TCP-onlyを維持してresolver入力とTCP DNS指定を接続した。実環境互換性は別途確認が必要。
- P3（修正済み）: Real Evidenceのsynthetic表記と、setup runtime 1件を含まない集計表記を修正。

Real実行前に、承認されたcost/network条件をcontractへ反映するrelease差分の作成・review、
実行環境のTCP DNS/NSS/CA互換性確認、credential供給の運用と停止・失効・不明attempt照合の準備が残る。
releaseを開く実装はこの作業には含めず、既存`LIVE_BLOCKERS`を維持する。

Human判断は、Messages API採用、資料の外部送信、別API課金と金額上限、account/model、
credential scope/expiry/revoke、一般TCPの残Riskまたは別egress制御の要求、release差分の承認、
その後の単回permitである。過去のAI提案やCLI向け方針をMessages APIの承認へ転用しない。
今回のOffline Engineering依頼をReal実行承認として扱わない。
