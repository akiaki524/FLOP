# Controlled Collaboration Worker v0.1

Messages APIの最新状態は[初回Pilot release candidate](docs/messages-api-release-candidate-20260921.md)。
Human承認条件と既存FLOP資料／fixed promptを固定した条件付き候補であり、**Real GOではありません**。
最終差分のIndependent ReviewはPASS済みで、新規Findingはありません。現在の次のHuman GateはAgent API用Account / Console / Billing Boundaryの確定で、その後にConsole条件のHuman確認と最終policyに一致する新形式permitが必要です。
以下の過去Batchのclosed／未承認表記は当時の記録で、CLI RailのNO-GOは維持します。

FLOP / Technocore Agent Projectの独立したHuman-supervised Workerです。
Humanが選択した保存済み資料と依頼を固定し、根拠付きレビュー、返信Preview、
Human-operated Signerへの受渡し、承認済みの**模擬送信**までを一回実行型CLIでつなぎます。

**v0.1 / Batch B4も実利用は無効です。** テストProviderはTODOを機械的に検出します。
Human-operated LLM broker、利用承認、attempt ledger、独立した合成runner、broker側Evidenceを実装しています。
実Codex/Claude推論はDISABLEDです。B1はClaude用exec launcherをFake executableへ接続しました。
専用UIDの必要性は未確定です。認証・通信を許す実CLI境界は別の検証とHuman Gateが必要です。
合成runnerの成功は実Codex runtime・実LLM・実協働・Technocore送信の成功ではありません。

## Setup / Test / Smoke

Python 3.12以上、Linux x86-64、Landlock ABI 3以上、seccomp filterが必要です。
追加依存、install、root、Dockerは不要です。ローカルLinux filesystemで実行してください。
隔離不可の場合は起動を拒否します。Full Accessへ切り替える回避策はありません。

### GitHub-portable verification

monorepoルートから `cd components/collaboration/worker` で移動し、以降はこのComponentディレクトリで実行します。

```bash
mkdir -p .local
python3 -B src/ccw/isolation.py
python3 -B -m unittest discover -s tests -v
python3 -B tests/smoke.py
python3 -B tests/b1_smoke.py
python3 -B tests/secret_handoff_probe.py
python3 -B tests/secret_boundary_probe.py
python3 -B tests/secret_abc_probe.py
python3 -B tests/secret_consumer_window_probe.py
python3 -B tests/runtime_service_smoke.py
```

`secret_abc_probe.py` と `secret_consumer_window_probe.py` は、同梱の固定baseline fixtureと
現在の実装を比較するOffline probeです。過去SHAはfixtureの由来を示す識別子であり、
実行時にPrivate Git履歴や `git show` は使いません。fixtureを収録した配布snapshotでも実行できます。
実Credential・Provider・外部通信は使いません。Linuxの `/proc` 等の実行条件は各probeを確認してください。

### Host-specific verification

`tests/real_cli_probe.py` はGitHub-portable CIとは別のHost Verificationです。
通常CIの成功条件には含めず、Real CLI境界を変更・再評価する時だけ、対応Linux hostで明示的に実行します。

実行条件:

- 非特権user / network / mount namespaceとprivate propagationが利用できること
- `tests/b3_probe.py` が固定するreview済みCLI artifactを準備していること
- artifactのpath / SHA-256がreview済み値と一致すること

```bash
python3 -B tests/real_cli_probe.py
```

GitHub-hosted CIはこれらのHost条件やlocal artifactを判定・代替しません。
条件が不足する環境でCIを通すためにsudo / root / privileged Docker / Full Accessへ迂回せず、
unreviewedなinstalled CLIやnetwork downloadへfallbackしません。
将来、Host Verificationを継続的に自動化する必要が実際に生じた場合は、
通常CIへ条件分岐を積み増すのではなく、独立したverification workflow / supported hostを検討します。

2026-09-19のReal adapter Offline Batchは実装済みですが、**Real GateはNO-GO**です。
pin済みCLI 2.1.274がSSE overloaded error時にretry設定0でも3 requestsを送るFindingを
local fake endpointで再現しました。Human permitの発行・Real起動はコードで拒否します。
将来用のService／dummy Secret Handoff／CLI JSON mapping／Human PreviewとP3 Unicode修正、
20ケースのfake検証・残条件は[Real adapter checkpoint](docs/checkpoint-real-adapter-20260919.md)を参照してください。
このprobeは非特権user/net/mount namespace内のloopbackのみを使い、providerへ接続しません。
`PASS_OFFLINE_WITH_LIVE_BLOCKER`はReal利用許可を意味しません。

2026-09-20の[最終Offline Investigation／Human Decision Packet](docs/claude-cli-final-offline-investigation-20260920.md)は
**Not Bounded / F-REAL-01 Open / Real Gate NO-GO**。公式設定の51ケースで79 provider POSTと51補助HEADを実測しました。
`max_attempts: 1`、`automatic_retry: false`、`fallback: false`はCCW自身の起動・再試行・切替の制約です。
CLI内部のprovider POSTを1回に制限する保証ではなく、`provider_requests_hard_cap: null`のままです。
CLI設定の追加hardeningは終了し、次は内部追加requestの明示Risk Acceptanceか別の公式interfaceの再評価をHumanが選びます。

2026-09-20の[Messages API / 公式SDK Offline評価](docs/messages-api-offline-assessment-20260920.md)は
**Suitable Candidate（採用・Real利用は未承認）**。retry=0に加えてredirect追従を無効化した候補43ケースで、
接続前失敗1ケースは0 POST、他42ケースは各1 POST、補助requestは0でした。
`max_retries=0`だけでは307/308で2 POSTになるため不十分です。既存ServiceへのOffline接続5ケースも検証しました。
API key・API課金の新しいHuman Gateが必要で、CLIのF-REAL-01 OpenとReal Gate NO-GOは維持します。
検証用SDKはignored `.local/` 内だけに置き、通常のCCW実行・testに依存を追加していません。

その後の[Messages API Rail最小Offline実装](docs/checkpoint-messages-api-20260920.md)では、
`offline-messages-api`をCLIとは別CapabilityとしてServiceへ接続しました。
非streaming専用、dummy API keyのみ、Real APIはdisabledです。32ケースで32 runtime起動／31 POST／補助request 0、
同じinstanceの再利用32回とGate拒否8ケースも検証しました。
API専用contractはCCW起動1回・POST最大1・retry/redirect/fallback無効と、別API課金のCost Gateを区別します。
SDKはoptionalな既存検証venvのみを使い、15依存のpin/hashを`src/ccw/messages_sdk.lock.json`に固定しました。
自動install/upgradeはありません。SDKがない環境でも通常のunit testは実行できます。
専用SDK環境がある場合の追加Offline検証: `python3 -B tests/messages_api_probe.py --rail`。
API採用・実credential・Real spendは未承認で、両Real Gateは引き続きNO-GOです。

[Real Gate Finalization（Offline）](docs/messages-api-real-gate-finalization-20260920.md)で、
MessagesのReal入口へHuman permit照合を接続し、Real候補profileをfake profileから分離しました。
候補はmodel `claude-sonnet-4-6`、wall 60秒 / HTTP 45秒、output 4096 tokens、raw 256KiB / Result 32KiB。
同じpermit経路をloopbackとdummy credentialで検証済みです。Real入口は別のrelease Gateで引き続き拒否します。
RECORD外fileの完全性はDependency検証の保証外です。追加検証は同じ`tests/messages_api_probe.py --rail`です。

[release / network Offline補完](docs/messages-api-release-network-offline-20260921.md)では、
Real候補のTCP DNSと証明書・hostname検証付きTLS経路を、固定endpointのまま隔離fake環境で検証します。
`python3 -B tests/messages_api_network_probe.py` はdummy credentialをReal用FD入口へ渡すテスト専用rehearsalです。
出荷コードのrelease Gateはclosedのままで、実providerへ接続しません。
TCP-onlyはprovider-only egressではなく、実環境のresolver/CAと課金・network条件はReal前のGateに残ります。

今回のcandidateでは同probeが実際のrelease判定を通し、合成attestation／dummy credentialと
隔離fake DNS/TLSで検証します。`python3 -B tests/messages_pilot_packet.py` は現在のcode identityと
policyを `.local/` へ再構成するだけで、permit・credential・通信を作成／利用しません。
再構成ごとにroot／nonceが変わるためpolicy digestも変わります。過去proposalのdigestは再利用しません。

Human terminal専用のReal起動入口は `python3 -B src/ccw/messages_real_human.py <reviewed-private-root>`。
既存のexact `request.json` とReal policy／permit／releaseを照合してから、端末の非echo入力で
API keyを一度だけ受け取ります。Activityとcredentialは別の匿名socketを使い、既存Serviceの
`--api-real-credential-fd`へ渡します。成功時はHuman Previewだけ、失敗時は固定分類だけを表示します。
この入口はAgent内から使わず、実Key・実通信を使う試行は本作業の範囲外です。
keyのPython process memory上の完全zeroizationは保証しません。失敗・結果不明でも再試行せず、
既存のHuman条件に従ってkeyをDisableまたはDeleteします。

Claude Runtime Service v1のOffline Componentは、接続済みsocketを持つActivity側の
`RuntimeClient.review(ReviewRequest(...))`から、事前許可した保存資料1件だけを処理します。
Service／dummy runtimeは独立processで、Workerへcredential capabilityを追加しません。
request 65,536 bytes／result 32,768 bytes／runtime最大5秒／instanceあたり並行1・単回実行です。
credentialはService内のrandom dummyのみで、モデル相当の子プロセスには渡しません。
出力はbounded受信・Secret scan・厳格なschema/引用検証・canonical result budget検査後に返します。
実行attemptへ入った後の失敗は消費済みのままで、retry/fallbackはありません。
`ReviewResult.preview_text()`はuntrusted / offline / external action未実行を明示する表示専用ASCII JSONです。
失敗分類・検証・Real Gateの残条件は[Output Boundary checkpoint](docs/checkpoint-output-boundary-20260919.md)を参照してください。
Real adapterの将来用接続は上記checkpointを参照してください。実credential・Production権限構成は未検証です。Interface、信頼境界、Recoveryと検証結果は
[Runtime Service checkpoint](docs/checkpoint-runtime-service-20260919.md)を参照してください。

Secret Handoffのoffline prototypeは、Human側の固定参照解決と、独立した非LLM consumerへの
受渡し（resolver stdout/stderrとconsumer stdin/stdout/stderrは匿名socket、値はconsumer ready後のみ）を検証します。
実行ごとにmemory内で生成するdummyのみを使い、実1Passwordには接続しません。
consumerの全出力を破棄し、環境値をCCWのcontract・Evidence・login planへ収録しない回帰も検証します。
実CLI/dummy itemのHuman Gate、Windows/WSLの確認範囲、再利用境界は
[Secret Handoff checkpoint](docs/checkpoint-secret-handoff-20260918.md)と
[Review修正checkpoint](docs/checkpoint-secret-handoff-fixes-20260919.md)、最新の
[A/B/C checkpoint](docs/checkpoint-secret-abc-20260919.md)、
[consumer ready前window checkpoint](docs/checkpoint-secret-consumer-window-20260919.md)を参照してください。
実`op`はHumanがAgent外のterminalから起動する場合だけ受け付けます。
exchange実行中の終了signalのうちHUP/INT/QUIT/TERMだけが子孫cleanupへ進みます。SIGKILL・native crash、
既定動作が終了となる他のsignal（USR1/USR2/ALRM等）後のresolver/子孫残存は未解決で、自動再送はしません。ABC probeは旧実装とこの残Riskも再現し、
検証ハーネス自身が残存processを回収します。匿名socketの`shutdown`を拒否する実行sandboxでは
probe/handoffが停止します。制限を自動解除せず、明示承認されたローカル実行環境で検証してください。

既存Testは毎回 `.local/test-*` を作成して終了時に片付けます。B1 Testは `.local/test-b1-*` にEvidenceを残します。Smokeは `.local/smoke-*` に
成果物と `summary.json` を残し、既存実行を上書きしません。固定した合成資料と公開ダミー鍵だけを使い、
テストハーネスがHumanの操作を模擬します。任意の入力や実送信へ切り替えるオプションはありません。

Smokeの期待結果は、通常成功1件、送信前応答消失0件・unknown継続、送信後応答消失1件・Human照合後sent、
送信後プロセス強制終了1件・Human照合後sentです。全ケースで再送を拒否します。
別の合成Scout Evidenceも受付からPreviewまで確認します。
Batch B0 Smokeでは、承認なし拒否、attempt永続化後のプロセス強制終了、無許可retry拒否、
明示的なHuman再許可、合成runner成功、Worker取込、Humanによるbroker Evidence照合まで確認します。

## Humanが操作する手順

最初に一度 `mkdir -p .local` を実行します。`init` は未使用のディレクトリだけを作成します。
環境変数にSecretを渡さないでください。下記の `TASK_SHA256` と `PAYLOAD_SHA256` は
直前の出力にある64桁の値に手で置き換えます。Worker出力から自動承認へ配線しません。

```bash
mkdir -p .local
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual init
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual stage examples/task.json
PYTHONPATH=src python3 -B -m ccw.worker --root .local/manual intake TASK_SHA256
PYTHONPATH=src python3 -B -m ccw.worker --root .local/manual review TASK_SHA256
PYTHONPATH=src python3 -B -m ccw.worker --root .local/manual export TASK_SHA256
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual preview TASK_SHA256
```

Previewの `payload` で依頼・範囲・宛先・指摘・引用・修正案・未確認事項を確認します。
`exact_bytes_ascii` は送信するcanonical JSONそのもの、`payload_sha256` はそのSHA-256です。
`readable_body` は同じ内容を日本語のまま読みやすく表示します。端末制御や双方向文字はescapeします。
`body` 内もJSONなので、`\u...` は日本語のUnicode表現です。

確認後、Human自身が承認と模擬送信を別々に実行します。

```bash
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual approve TASK_SHA256 --confirm-sha256 PAYLOAD_SHA256 --ttl 300
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual send-mock PAYLOAD_SHA256 --outcome success
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual status PAYLOAD_SHA256
```

承認は宛先、action、protocol、task ID、本文を含む最終バイト列に結び付きます。
有効期限は1〜3600秒、使用は1回です。Signerは承認時に保持したbytesをそのまま模擬送信します。
送信直前にも最新handoffを再検証します。Human CLI以外に承認発行機能はありません。
承認済みtaskには再承認を許可しません。TTL切れ・取消後の再承認もv0.1では未対応です。

### 資料の入力

Human指定資料は [examples/task.json](examples/task.json) と同じ形式にします。
`request`、`scope.include` と `scope.exclude`、Humanが選んだ `destination`、1〜10件の `sources` が必須です。
各sourceの `text` は保存済みUTF-8本文、`sha256` はそのUTF-8 bytesのSHA-256、`locator` は出典を示す文字列です。
URLを指定しても取得しません。ファイル名を本文から解釈して開くこともありません。
本文は1件30,000文字、入力JSONは512,000 bytes以下、固定bundleは256,000 bytes未満です。

Scoutからは、Humanが選んだ保存済み `source-ROOM.json` の `messages` 配列から1件を取り込みます。
`--index` は0始まりです。依頼・範囲・宛先はHuman作成の別JSONに置き、`sources` は空配列にします。
次は合成サンプルであり、既存Scout Repositoryへのアクセスは不要です。

```bash
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual stage-scout examples/scout-request.json examples/scout-evidence.json --index 0
```

取込元ファイル名、JSON Pointer、元JSONのcanonical digestと選択本文digestを保存します。
投稿者の真正性・Scoutの判定・資料の最新性を保証するものではありません。
元ファイルへの書込みや他メッセージの自動Task化はしません。
同じ固定bundleは同じtask IDになり、二重受付で状態や成果物をリセットしません。
依頼・資料・範囲・宛先を変えると別Taskです。

## 停止 / Recovery

Workerは常駐しません。処理中のCtrl-Cや終了はレビューの途中状態を残します。
Signer送信中に中断したら、成功とは扱わず必ずSigner状態を確認してください。

| 状況 | 操作と結果 |
| --- | --- |
| Taskがreadyで一時停止したい | `worker pause TASK_SHA256`。`resume`でreadyへ戻す |
| reviewingで中断した | 実行中プロセスが終了したことを確認後、`worker resume TASK_SHA256`、続けて`review`。他Workerがlock中なら拒否 |
| reviewed後にexportで中断 | 同じ`export`を実行。既存内容が一致すれば再利用し、差分があれば停止 |
| 未使用承認を取り消す | `human revoke PAYLOAD_SHA256` |
| 全体を止める | `human stop`。Signer自身のDBに停止を保持し、WorkerにSTOP markerを作成 |
| 全体を再開する | 原因確認後に`human start`。消費済み承認・unknownを再送可能にはしない |
| unknown / in_flight | `human reconcile-mock PAYLOAD_SHA256`。一致する模擬受領証があればsent、なければunknownのまま |
| 隔離拒否・整合性異常・DB破損 | 操作を止め、入力・状態・監査ログを保全。sandboxを弱めたりDBを編集して送信可能にしない |

表のコマンドには上記と同じ `PYTHONPATH=src python3 -B -m ccw.ROLE --root .local/manual` を付けます。
例えば応答消失は次で模擬できます。新しい未使用承認に対してのみ実行してください。

```bash
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual send-mock PAYLOAD_SHA256 --outcome lost-after
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual reconcile-mock PAYLOAD_SHA256
```

送信開始をDBに永続化してから模擬通信します。`lost-before`、`lost-after`、`crash-after` のいずれも
再送は不可です。`crash-after` は実際にプロセスを終了コード75で終了させるテスト専用指定です。
受領証がないことは未送信の証明ではありません。再送コマンド、タイムアウト後の自動解除、期限による再送はありません。
通常の `stop` は進行中操作のlock解放を待たず拒否されるため、緊急時は先にそのプロセスを中断して状態を確認します。

SQLite DBとjournal、Human snapshot、承認・receiptは一組として保持します。
`.pending-*` が残るファイル生成中断やhardlink検知時も自動削除・上書きせず調査します。
未送信と確定していないTaskを、新しいrootや新しいTaskに作り直して再送する運用は禁止です。
DB復元・移行・unknownから再送への解除はv0.1では実装していません。

## 成果物 / 境界

| パス（実行root配下） | 内容 / 所有する側 |
| --- | --- |
| `worker/inbox/ID.json` | Workerに渡す固定依頼と資料 |
| `worker/tasks.sqlite` | Task状態、固定bundle、review、状態イベント |
| `worker/outbox/ID.json` | 根拠付きreview、返信payload、最終digest |
| `human/tasks/ID.json` | Workerから変更不能なHuman選択時snapshot |
| `human/signer.sqlite` | 承認、TTL、送信状態、監査イベント、停止状態 |
| `human/dummy-key.json` | 公開テスト鍵のみ。実鍵は受け付けない |
| `human/mock-remote.sqlite` | 模擬受領側の永続ledger。実サービスではない |
| `human/llm.sqlite` | LLM利用条件・単回permit・attempt・raw response・broker Evidence・監査イベント |
| `human/LLM_STOP` | HumanのLLM停止marker。Worker/runnerから変更不可 |
| `runner/ATTEMPT_ID/` | attemptごとの新規最小workspace。合成runnerからはreadのみ |
| `runner/ATTEMPT_ID/codex-home/` | 空の専用CODEX_HOME。認証・user config・plugin等なし |
| `worker/inbox/ATTEMPT_ID.llm.json` | brokerからの合成結果・Evidenceのコピー。信頼根はHuman側DB |

Worker CLIは資料を読む前にLandlockで `worker/` のみにファイルアクセスを絞り、seccompで通信、exec、fork、
ptrace、process_vm等を拒否します。継承FDを閉じ、環境を空にし、標準出力・標準エラーは端末またはpipeのみ許可します。
そのためWorkerの出力をファイルに直接redirectする使い方は拒否されます。成果物は `outbox/` を使ってください。
Human側は別プロセスで動き、Workerの任意pathを受け取らず、固定形式のhandoffだけを検証します。
引用はsource digest、1始まりの行範囲、原文一致を検証します。指摘内容そのものの妥当性はHuman確認が必要です。

信頼するものはOS、Python、Repositoryのbootstrapコード、Human操作者です。
Human CLIにログイン本人確認機能はありません。未隔離の同一OSユーザーやrootから鍵を守る仕組みではありません。
Batch B0 runnerは同一UIDでも自身をLandlock/seccompで隔離してから入力を読みます。
一方、未隔離processやCodex本体全体の安全性を、Codexのlocal command sandboxだけで保証しません。
Human CLIをWorkerのToolとして公開すること、資料からPythonをimportすること、
Worker用rootに認証情報・承認・鍵・外部hardlinkを置くことは禁止です。
Landlockはmetadataの存在まで隠す機能ではなく、ここでは内容の読書きを隔離します。
詳細は [設計と脅威モデル](docs/design.md)、根拠は [参照記録](docs/references.md) を参照してください。

## Codex Adapter / 今後の明示的承認

```bash
PYTHONPATH=src python3 -B -m ccw.worker --root .local/manual adapter-request TASK_SHA256
```

この操作はlegacy Replay用metadataとpromptを表示するだけで、argvは空です。
model指定・利用条件を伴う新しい契約はHumanの`llm-preview`で作ります。
`review --provider codex` は拒否され、Taskをpausedにします。
`codex-replay` はHumanが `worker/inbox/ID.replay.json` に置いた合成CLI応答を検証するテスト用経路です。
形式は `returncode`、JSONL文字列の `events`、最終JSON文字列の `final`。
終了失敗・turn未完了・Tool実行イベント・引用不一致を拒否します。Replayも実LLM成功としては扱いません。

### Batch B0のHuman操作

`human stage`で固定した未レビューTaskを使い、次の例の`TASK_SHA256`、`APPROVAL_SHA256`、
`ATTEMPT_ID`を表示結果に手で置き換えます。CLI同士を自動承認に接続しないでください。
`offline-fixture-v1`は合成専用model名であり、OpenAIのmodel名ではありません。

```bash
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual llm-preview TASK_SHA256 --provider synthetic --model offline-fixture-v1 --max-attempts 2 --timeout 3 --max-request-bytes 256000 --max-response-bytes 128000 --max-total-request-bytes 512000 --max-cost-microusd 0
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual llm-approve TASK_SHA256 --provider synthetic --model offline-fixture-v1 --max-attempts 2 --timeout 3 --max-request-bytes 256000 --max-response-bytes 128000 --max-total-request-bytes 512000 --max-cost-microusd 0 --confirm-sha256 APPROVAL_SHA256 --ttl 300
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual llm-run-synthetic TASK_SHA256
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual llm-status TASK_SHA256
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual llm-verify ATTEMPT_ID
PYTHONPATH=src python3 -B -m ccw.worker --root .local/manual intake TASK_SHA256
PYTHONPATH=src python3 -B -m ccw.worker --root .local/manual import-llm TASK_SHA256 ATTEMPT_ID
PYTHONPATH=src python3 -B -m ccw.worker --root .local/manual export TASK_SHA256
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual preview TASK_SHA256
```

承認はtask/provider/model/最大attempt数/timeout/request・response bytes/累積request bytes/費用上限と
request digestに結び付きます。request digestはprompt・schema・candidate argv・設定契約を含みます。
最初のpermitは1回限りです。max-attemptsを2にしても、2回目を自動で許可する意味にはなりません。
timeoutは各attemptの秒数、費用はmicro-USDでsyntheticは0固定です。
実費用・token上限の強制は未実装なので、openai用の条件をpreview/approveしても起動できません。
その承認を後からlive利用へ転用することもできません（request契約が変われば拒否）。

`started`で中断した場合は結果不明として保持します。失敗時もattemptと累積budgetを戻しません。
Humanが前process終了と原因を確認した後だけ、残予算・期限内で次を実行できます。

```bash
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual llm-retry TASK_SHA256 --previous-attempt ATTEMPT_ID --confirm-sha256 APPROVAL_SHA256 --confirm-stopped
```

`llm-retry`は1回分のpermit発行のみです。実行には別の`llm-run-synthetic`が必要です。
成功済みattemptのretry、期限切れ承認の再発行、Taskを作り直すbudgetリセットは許可しません。
`llm-export ATTEMPT_ID`は保存済み成功結果のコピー復旧用で、runnerを再実行しません。
Human previewはEvidenceの除去・改変・別reportへの差替えをHuman DBとの比較で拒否します。
通常の`human stop/start`はLLM brokerも停止/再開します。B1の実行中停止には下記の`llm-stop`を使います。

### Batch B1: Claude launcherのオフライン検証

`tests/b1_smoke.py`は固定した公開合成Taskだけを対象に、承認なし拒否 → 合成承認 →
起動前ledger → launcherでexec → Fake JSON応答 → Schema/引用/model照合 → Worker → Human Previewを通します。
`.local/b1-smoke-*/`にapproval-preview、status、Human Preview、DBと成果物を保持します。
自動承認はこの固定Test harness内の合成操作だけです。任意TaskやRealへの切替はありません。

Human CLIではB0のpreview/approveのproviderを`claude-fixture`、modelを`offline-sonnet-fixture-v1`、
effortを`--effort fixture-high`、max-attemptsを`1`にします。これらは実運用model/Effortではありません。
実行は`llm-run-claude-fixture TASK_SHA256`。後続の`llm-verify`、Worker `import-llm/export`、Human `preview`は共通です。
`--max-cost-microusd 0`は共通CLIとの互換用で、B1の金額上限強制を表しません。

承認format 2はledger固有ID、root、TTL、絶対期限をdigestへ含めます。
期限の起点は**そのTaskの最初のllm-preview時刻**です。preview/approveで同じTTLを指定します。
再previewしても期限は延長しません。期限切れchallengeの更新操作は未実装です。
旧formatの承認は保存結果の照合専用で、新規起動・retryには使えません。
旧DBを破棄しません。既存DBを新コードで開くとinstance/challenge用tableが追加されます。

```bash
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual llm-stop
PYTHONPATH=src python3 -B -m ccw.human --root .local/manual llm-status TASK_SHA256
```

`llm-stop`はHuman lockを待たずmarkerを保存し、B1の親processが監視してchildをkill/reapします。
要求の受付は停止完了の証明ではありません。statusで状態を確認します。
`not_started`はBrokerがlauncher開始通知を観測していない状態です。起動前準備だけでなくPopen失敗も含み、
通知直前の中断などを完全な未実行証明にはしません。`unknown`は開始観測後の異常、`rejected`は受信結果の検証拒否です。
親processを強制終了すると`started`が残り、結果不明として扱います。Provider停止・未消費を意味しません。
B1はどの状態でも再実行・retry不可です。停止解除の`human start`もpermitを復活させません。
保存済み成功結果の`llm-export`だけは再起動なしで復旧できます。コード/binary変更時は照合が止まるため、
元のcommit・DB・workspaceを保持し、DB編集・root作り直しで再送可能にしないでください。

旧B1の実binary無認証検証は`python3 -B tests/b1_smoke.py --probe-installed-cli`です。
空HOME、socket拒否、同じexec境界でversion/helpに限定します。今回versionは正常終了せずhelp未実施でした。
境界を緩めて再試行しません。実認証statusや推論コマンドを受け付けるオプションはありません。
詳細なEvidence・制約・Review項目は[Batch B1 checkpoint](docs/checkpoint-b1-20260917.md)を参照してください。

### Batch B2: Real Runtime Boundaryのオフライン実装

`claude-fixture`は従来のFake専用profile、`claude-real`はnative専用profileを使います。
Real契約は固定binary/digest/version、first-party subscription OAuth、完全model ID、effort、
schema、cwd、環境、auth inventory、runtime/egress方針をrequest digestへ含めます。
timeout・bytes・期限・単回permitは共通の承認条件に結合されます。
`claude-real`のpreview/approveが成功しても**Real利用の承認ではありません**。
Brokerとlauncherの両方にB2の無条件停止Gateがあり、環境変数やCLIオプションでは解除できません。

無認証・全socket禁止の実binary probe:

```bash
python3 -B tests/b2_smoke.py
python3 -B tests/b2_smoke.py --profile
```

新しい`.local/b2-probe-*`にのみ保存します。`--profile`は自身の診断用子processをptraceし、
syscall番号・errno・clone flags・空認証診断のopen先pathを保存します。
実行対象はversion/helpと、別の空HOMEでの未ログインstatus確認だけです。
help/versionのstdoutと空認証診断のstderrだけを保存します。auth statusは未ログインの判定だけを残し、
raw auth JSONやaccount fieldを保存しません。通常のrunner stderr本文も保存しません。
外側の開発sandboxがptraceやTCP socket作成を禁止する場合は、その制限とCCWの制限を区別します。
必要な確認なしにFull Accessやホスト設定の変更へ迂回しません。

Human専用`llm-prepare-claude-real`は、固定されたローカルnativeインストールから
`ROOT/human/claude-runtime/claude`へ排他的にcopyし、sealed memfdの同一objectをhash/execします。
version/helpも同じdigestのsealed objectで検査し、成功時だけ`target.json`を保存します。
既存runtimeは上書きしません。自動更新先を起動時に探索し直しません。
`0500`のdisk copy単独を変更不能とは呼びません。実行時のkernel sealsでbytesを固定し、
承認digestとの照合後に同じFDでexecveatするのが保証の中心です。

専用auth/config homeは`ROOT/human/claude-runtime/auth`（操作者UID、0700）です。
準備では空directoryを作るだけです。通常`~/.claude`の読取り/コピーやloginはありません。
runtimeだけが専用auth homeの読書きを許可されます。Workerと別processのresult parserには許可しません。
起動前inventoryと終了後差分はpath・所有者・mode・size・時刻・inodeのみで、credential本文やhashは取りません。
inventoryは内容の真正性証明ではありません。公式loginと生成物の確認は別Human Gateです。

Online profile案はAF_INET/AF_INET6のTCP streamのみ、UNIX/NETLINK/UDP/socketpairは拒否します。
endpoint限定proxyは実装していません。任意TCP（loopback/LANを含む）に到達し得る案(b)を契約に明示し、
`human_accepted: false`のまま保持します。DNSを含む実通信互換性は未検証です。
netnsだけではProviderへ到達できず、案(a)には別途proxyと限定された到達経路が必要です。
本番runtimeにnetnsや新しい通信権限は追加していません。B3のFake通信probeだけが
非特権user/network/mount namespaceを使います。sudo、新規OSユーザー、host/WSL networking変更はありません。

Real mapperは公式JSON resultの`structured_output`をschema/引用検証へ接続します。
requested modelが`modelUsage`のkeyに存在することを要求し、aliasを自動解決しません。
B3では固定CLI 2.1.274の埋込みschemaに基づくoptional fieldと、明示したHaiku補助usage keyを検査します。
`canonicalModel`は価格計算用IDとして別に記録します。複数usage keyはfallback許可ではなく、
resultだけではprimary/auxiliaryの実際の役割を証明できません。他の未承認model keyは拒否します。
session IDはProvider request IDではありません。usage欠測はnull、未知fieldや不正値・model不一致は拒否します。
API換算推定額をsubscription billingと扱いません。`max_cost_microusd: 0`は金額強制ではなく、
`money_cap_enforced: false`を併記します。token/内部retry/Provider request数の完全強制は要求しません。
**将来、1回分の利用枠を消費した後にstrict mapperでrejectedとなる可能性があります。再実行は許可されません。**

実測結果、残Gate、Review対象は[Batch B2 checkpoint](docs/checkpoint-b2-20260917.md)を参照してください。

### Batch B3: 初回Real前の互換性修正

[B3 checkpoint](docs/checkpoint-b3-20260918.md)はB3時点の記録です。
現行判断は[B4 checkpoint・専用login Gate](docs/checkpoint-b4-20260918.md)を参照してください。
mapperの未知field拒否、単回permit、生応答保持、no-retryは維持しています。
隔離parserの拒否理由を固定した安全な文言でEvidenceへ返し、auth inventory異常時も事前inventoryを保持します。
StructuredOutputは構造化回答用の内部Toolです。Bash/WebFetch/Agentを有効化しません。
`num_turns`は観測値であり、`--max-turns 1`、CLI起動1回、Provider request数とは区別します。

追加オフライン検証:

```bash
python3 -B tests/test_claude_b3.py -v
python3 -B tests/b3_probe.py
python3 -B tests/b3_network_probe.py
```

schema probeはB2で保存した固定binaryを静的に読むだけです。network probeは独立namespace内の
Fake DNS/TCPだけを使い、namespaceが作れなければ停止します。通常hostへfallbackしません。
glibcの通常DNSは現profileで失敗、専用namespace内の`use-vc`は成功しました。
これはBun/TLS/OAuthの確認ではありません。B4ではruntimeの最小環境に
`RES_OPTIONS="use-vc timeout:2 attempts:1"`を固定し、同じprofile内のglibcからFake TCP DNSへの
名前解決を確認しました。host resolverは変更せず、UDP権限も追加していません。
案(a)は未実装。Humanは初回一件に限り案(b)の残Riskを受容する方針を選択しました。
`human_accepted: false`は個別live許可が未発行という意味で維持し、方針決定と区別します。
両方のLive Gateは無条件停止のままです。

実login後のauth、runner workspace、DB、log、raw responseは**Secretを含み得る私有データ**です。
inventory合格だけでは公開可になりません。credential hardlinkによるworkspace内別名の残存もあり得るため、
Humanが確認するまで公開Evidenceへコピーしないでください。削除・失効・再送も自動化しません。

### Batch B4: login前のreview checkpoint

auth契約は0600の`.credentials.json` / `.claude.json`と**空の0700 `sessions/`**だけを認めます。
`sessions/`にはPID登録やpeer鍵も作られるため、内部fileを一括許可しません。
正常終了後の空directory追加だけを通常変化とし、残存PID/key・未知生成物・link・mode異常は停止します。
credential本文のread/hashは行いません。

```bash
python3 -B tests/b4_probe.py
python3 -B tests/b4_probe.py --diagnostics
python3 -B tests/b2_smoke.py --saved-b3
```

`--diagnostics`は固定済みbinaryのversion、固定レビュー引数＋help、login引数＋help、空auth statusのみ。
全socketを拒否し、login・推論はしません。`--saved-b3`は既存B3 Evidenceの固定runtimeからbinaryだけを
新しいscratchへ準備し、元のauthを読取り/コピーしません。installed版が更新された場合も自動追従しません。

Human CLIの`llm-prepare-claude-real --from-prepared-root ABSOLUTE_PREPARED_ROOT`も、指定した旧targetの
version/help/hashを再確認し、binaryだけを新規rootへ準備します。既存runtimeは上書きしません。
`llm-claude-login-plan`は同じ`--root`指定で、固定argv・専用path・最小環境・inventory・未解決事項を表示します。
この計画にlogin許可はなく、launcherの`login`入口も無条件停止します。

**専用loginはまだNO-GOです。** 固定CLIは手入力でも先に127.0.0.1 callback listenerを作り、
現profileのbind/listen/accept拒否に当たります。loginだけに必要な追加権限と実装のReviewが次のHuman Gateです。
その後に別途、実login／credential登録／OAuth通信の承認が必要です。Bun resolver、TLS、OAuth、
実ProviderでのTool一覧・StructuredOutput完了は未確認です。詳細な承認項目・RecoveryはB4 checkpointにあります。

### 初回Real LLM前の停止Gate

実LLM・実送信へ進むには、承認だけでなく別の実装と境界検証が必要です。

1. **実LLM**: Human/Signerからの分離を別作業で検証し、固定CLI binary/version、model、
   実効config/tool一覧、専用認証領域、送信先、請求経路と追加使用の抑止を確認する。
   B0の合成runnerはexecもsocketも禁止しており、実Codexをそのまま動かすlauncherではない。
   UID分離の必要性は未確定で、UIDだけで通信・interopは解決しない。上記をIndependent Review後、対象task/request digestと
   資料の外部送信、provider/model/Effort、最大1attempt、timeout、bytes、期限をHumanが承認する。
   Claudeで強制できないtoken/費用/内部retryは残Riskを明示し、旧要求の変更案をReviewする。
   B1の具体的な順序と未確認事項は[checkpointのReal Gate](docs/checkpoint-b1-20260917.md)を参照する。
2. **実送信**: Technocore公式wire仕様・署名方式・nonce・受領証/照合仕様を確認して専用Transportを実装する。
   Human管理Signerの鍵境界を検証し、環境・宛先room/recipient・最終送信bytes/digest・期限を指定した
   **その1回の送信承認**を受ける。unknown時は停止を維持する。

これらの承認は本実装依頼には含まれません。公開・push・Production変更も行っていません。
完全A2A互換、MCP Gateway、Swarm、長期Memory、Wallet、daemon / cronは対象外です。
