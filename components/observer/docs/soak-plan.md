# Local isolated soak plan

対象: Observer Specification v0.3 / Technocore **0.12.1**、Room `mb-047f3d88ef38`。

状態: **bounded live soak実施済み、判定WARNを維持。再実行しない。** `soak-results-live-20260906-133730-1b37726d` は300試行、通信失敗2回と503が1回、その後回復。gap/世代変更/保存破損なし。telemetryのみ修正しoffline regressionを完了した。[検証記録](soak-verification.md)を参照。以下の運転commandは既存手順の記録であり、今回の実行指示ではない。

## 実装したcommand

計画表示だけ（Docker接続・新規directory・networkなし）:

```sh
python3 -B tests/run_container_soak.py
```

host上のfixtureのみを使う短いoffline検証。通常UIDでも実ネットワークclientを使わない:

```sh
python3 -B tests/run_container_soak.py --offline --init-mode tail --duration-seconds 6
python3 -B -m unittest discover -s tests -p test_soak.py -v
```

新helperを含むcontainer preflightだけを実行するcommand（network=none。通常Ubuntu terminalでPASS確認済み）:

```sh
python3 -B tests/run_container_soak.py --container-offline --init-mode tail --duration-seconds 6
```

既存live実行command。最初にnetwork=noneの6秒preflightを実行し、PASSした場合だけ別のbridge containerを起動する。**今回の修正後に再実行しない。**

```sh
python3 -B tests/run_container_soak.py --execute --init-mode tail --duration-seconds 3600
```

`--init-mode tail`がない実行、3600秒を超える指定、不正なdurationは拒否する。initは毎回新規の試験stateへ明示的に行い、既存Inboxを再初期化しない。短縮したlive運転はPARTIALとし、初回60分のPASSにはしない。

## 目的と最初の運転範囲

初回は最大60分の通常取得を計画し、memory、DB/WAL/log増加、disk free、gap、HTTP failure、wait_held=false率、独立watchdogの結果を測る。message投稿、意図的な429、waiter枯渇、stress/load、export、自動recovery/resyncは行わない。新着がなくても観測として成立し、新着保存が未観測だったことを結果へ残す。

初回は検証済みの固定base image＋source zip＋64 MiB tmpfsを維持する。この運転で判定するのは、限られた時間・容量での取得処理と計測の安定性。tmpfs上のSQLiteを永続storageの実証には使わず、終了時と途中でSQLite backup APIによりrepoへ証跡を回収する。container/WSL停止やOOMで直近の未回収分を失う可能性を明記する。

永続volumeやhost bind mountを今回の計画へ追加しない。persistent storage上のdurabilityとdeployment imageの検証は、構成を別途具体化してから行うVPS前の残課題とする。この初回60分のPASSだけで§72の全gate達成とはしない。

## 維持する隔離条件

| 項目 | 計画値 |
| --- | --- |
| Base image | local ID sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea。pull/build/registry照会なし |
| User | UID/GID 65532:65532、HOME=/nonexistent、secretやhost環境変数の転送なし |
| 権限 | non-privileged、cap-drop=ALL、no-new-privileges、read-only root、seccomp、no devices |
| Mount | /state tmpfsのみ。64 MiB、0700、所有者65532、noexec/nosuid/nodev。host HOME、鍵、Windows drive、control socket、bind mountなし |
| Resources | memory=256 MiB、pids-limit=32、restart=no、initあり |
| Network | offline preflightはnone、liveはbridge。host network/namespaceなし、port公開なし |
| Source | public Python sourceのみstdin経由。source hash・archiveを保存し、実行版を固定 |
| Host側 | repo内の空のDocker client設定を使用。containerへDocker socketを渡さない |

smokeと同じconfiguration/process検査を毎回行い、1項目でも不合格ならliveを開始しない。実際の秘密鍵を読んで試さず、公開canaryとmount/namespace/権限検査を使う。bridge自体にdomain単位のegress firewallはないため、固定origin/pathのclient制限を維持する。

## Request上限と運転時間

外部HTTP originは `https://technocore.chat` のTLS検証付きTCP 443のみ。proxy自動探索・redirectは無効。DNSはDockerの既存resolverでこのhost名を解決する。response由来URL、外部registry、GitHub等には接続しない。

| 用途 | Request | 最大回数 |
| --- | --- | --- |
| Explicit init | GET /r/mb-047f3d88ef38?format=json&limit=1 | 1 |
| Deployment確認 | GET /config。実Observerのcheck_configでeventも保存 | 1 |
| Observer | GET /r/mb-047f3d88ef38、since=poll_seq、limit=200、wait=10、format=json、数値n | 360 |
| 独立watchdog | 同RoomのGET、format=json、limit=1、数値n。5分間隔 | 12 |
| 合計 | 失敗・retry・timeoutも回数に含める | **374以下** |

前回のsmoke用empty診断Room、limit=201、since=0履歴診断はsoakで使わない。global request counterと間隔制御は試験harnessに置き、Observer/watchdogを含む全GETを数える。request完了から次のrequest開始までは最低2秒空け、同時HTTP requestは1本までとする。retry時は本体のbackoff・Retry-After・wait_held=false delayと2秒制限の長い方を尊重する。待機後の追いつきburstを発生させない。

通常pollのlimit/wait/seq処理と本体の5xx/503処理は変更しない。2秒の試験間隔を追加するため、この運転は無制限CLIの最大取得性能を測るものではない。quiet Roomではlong-poll時間＋休止となり、360回未満が正常。busy Roomで回数上限へ先に達した場合は延長せず、60分未達の部分結果とする。

実装では上限を保守的にし、**init・configを含むnetwork phase全体**をmonotonic clockで60分以内とする。期限後は新規requestを発行せず、処理中のrequest/transactionの終了と証跡回収へ移る。単一RPC/watchdogの待機上限は60秒。snapshot RPCは30秒、snapshotの転送は30秒、回収確認は10秒、SQLite backup処理自身にも20秒の期限を設けた。Docker準備は各commandを30秒以下、終了時のinspect/stop/removeは各10秒以下に制限する。終了処理はGETを一切行わない。SIGINT/SIGTERMでも発行を止め、backupと終了処理を試みる。

## 開始前の未達項目

| 項目 | 現在の状態・必要な作業 |
| --- | --- |
| 修正後live smoke | 完了。offline/live PASS、6 GET、source/body整合、config/content警告なし |
| 実行harness | 実装・offline PASS。default計画のみ、explicit tail必須。Observer processがlockとStoreを保持し、hostからの有界RPCを処理 |
| 時間・回数・間隔制御 | 実装・offline PASS。hostの単一Gateがinit/config/poll/watchdogを発行前に数える。未許可のdiagnostic commandはGET前に拒否 |
| 計測 | 実装・offline PASS。HTTP metadata、heartbeat、wait_held、RSS、cgroup memory/OOM、DB/WAL/SHM、host/tmpfs容量を記録。Docker内6秒の計測も確認済み |
| 独立watchdog | 実装・offline PASS。別process・別read connectionで実行し、同じGateの回数・間隔制限を使用 |
| Backup/回収 | 実装・offline PASS。SQLite backup API、途中/最終snapshot、容量制限、hash・SQLite整合・件数確認、fsync後の回収確認 |
| 容量・停止検査 | 閾値と停止経路をoffline/fault fixtureで確認済み。実際のcontainer FULL/OOMを作る試験は未実施 |
| Container preflight | Docker内6秒offline PASS。その後のlive soakでwatchdog 11回・backup 12件も確認済み。今回のtelemetry差分はoffline regressionのみ |
| 実行環境 | このCodex sessionのDocker接続は未解消。実行時はDocker Desktopへ接続できる通常Ubuntu terminalを使う |

helper実装、offline検証、Docker内実測、初回live soakは実施済み。liveのWARNはそのまま残す。その後、2026-09-07に [Local Persistent Storage Durability](persistent-storage-durability-plan.md) も完了した。次は [VPS移行前checklist](vps-migration-checklist.md) のproduction実測・監査・承認であり、追加live soakは行わない。

## 計測と証跡

新規結果directoryを実行前に表示し、`docs/soak-results-<mode>-<日時>-<識別子>/`（0700）へ保存する。証跡は0600、raw本文を含まない公開source archiveだけは親0700の内側で0644を許容する。plan、source hash、host helper hash、隔離検査、開始・終了理由、request累計を残す。Docker使用時は終了時のOOMKilled等も記録する。原本smokeは変更しない。

| 頻度 | 記録内容 |
| --- | --- |
| Pollごと | 安全なrequest metadata、HTTP status/elapsed/body byte数、validation結果、wait_held true/false/missing、backoff、heartbeat |
| 60秒目安・実行中の処理完了後 | Observer RSS、container memory使用量/上限/OOM、DB/WAL/SHM byte数、log byte数、tmpfs free、host artifact filesystem free、process状態。各sampleの実測間隔とreportの最大間隔を記録 |
| 5分ごと | 独立watchdog結果、SQLite backup APIのsnapshot回収。poll中の長いread transactionを避ける |
| 開始/終了 | schema/application_id/user_version/quick_check、runtime WAL/FULL/foreign_keys読戻し、stateとmessage/gapのsequence accounting、最終backup hash |

結果には`report.json`、`metrics.jsonl`、`snapshot-NN.sqlite`とそのhash/state/count metadataを含める。SQLite snapshotはhostへ回収して整合性・hash・件数を再確認し、file/directoryのfsyncが成功してからcontainer内の一時snapshotを削除する。元の稼働DBは削除・copyしない。hostのoffline fixtureでは`request_count`は疑似GET呼び出し数で、外部通信は0である。

telemetry修正後は、通信例外に`error_class`、整数の`errno`、URLError内包例外の`reason_class`を記録する。raw例外文・URL・reasonの文字列は記録せず、同じ例外を再送出してObserverの既存処理に渡す。次の正常応答へ古いerror metadataを持ち越さない。

実行前のplanから固定値`soak_started=false`を削除した。実行事実はreportの`phase_times`で、controller開始、request window開始、最後のRPC発行/終了、新規発行閉鎖、最終backup成功、cleanup成功/試行終了を区別する。各値はhostのUnix時刻とcontroller開始からのmonotonic経過時間。失敗して完了しなかったphaseはnullとする。`last_request_rpc_finished`はRPCの終了であり、通信失敗時のserver到達やHTTP完了を証明するものではない。

互換性のため`elapsed_seconds`は従来どおりrequest window開始からbackup処理後・cleanup前までを表す。`total_elapsed_seconds`はcontroller開始からcleanup試行終了後まで（report自体の書き込み時間は除外）。60分の新規request期限、backoff、samplingの実行順序は変えていない。

wait_held=false率は「構造検証に成功し、wait_heldがboolだったpoll」を分母とし、true/false/missing/全valid poll件数も併記する。HTTP 200回数とvalid response回数は別に集計する。quiet Roomでlast_message_saved_atがNULLでも単独ではstallと判定しない。

watchdogはObserverと別process・別read connectionで動かす。共通のrequest間隔制御で待たされた時間と実際のHTTP/SQLite時間を区別し、計測harnessが作る待機をserver障害と誤認しない。Observerが停止・長時間request中でもhost側の経過時間監視を止めない。

raw messageはSQLiteへ保持し、terminal/logへ出さない。正常HTTP bodyを毎回別ファイルに複製せず、anomaly evidenceは本体DBへ保存する。live DBをcpしない。途中backupは固定名1個へ更新せず別名で回収し、hashを記録する。snapshot最大12回＋最終1回、repo側artifact総量256 MiB、metadata log合計8 MiBを上限として自動削除・無制限蓄積を避ける。

## 警告・停止・合否

次の数値は初回運転の保守的な試験閾値であり、Technocoreのdeployment値や一般的な性能保証ではない。

| 条件 | 扱い |
| --- | --- |
| ERROR / NEEDS_RESYNC、隔離違反、許可外request | 停止・証跡保全。自動復帰せず不合格 |
| Prefix gap | DEGRADEDとgapを保存し、規定どおり新着監視を継続。試験終了時は要確認とし、gapなしPASSにしない |
| Protocol anomaly | 本体の連続3回でERRORという規則を維持。1回でも発生したら結果に残し、無警告PASSとはしない |
| 429、5xx、timeout/reset | 本体のtransient failureとbackoffを維持。発生数・回復を記録。429を作りに行かない |
| VERSION_CHANGED | event/警告を保全し、この0.12.1対象の試験は中断。自動仕様追従しない |
| last_valid_responseから120秒超 | host監視で要確認を記録。正当なbackoff待機と比較する |
| last_valid_responseから10分超、または単一requestが60秒超 | 試験を中断して部分結果を保全。本体の永続statusを書き換えて停止理由を偽装しない |
| RSSまたはcontainer memoryが192 MiB以上を2サンプル連続、あるいはOOM | 中断。OOM時は回収済み証跡の範囲を明示 |
| tmpfs free <48 MiB、host free <512 MiB、artifact/log上限到達 | 次のrequestを開始せず回収・終了。容量不足はPASSにしない |
| RSS後半15分の中央値が最初の安定15分より32 MiB超増加 | 成長警告。message量・DB/cache増加と併記してreviewする |
| request/time上限 | 新規requestを止める。60分未達なら部分結果。自動延長しない |

旧baselineのbody capは32 MiBでした。current implementationは`MAX_BODY=4 MiB`で、`json.loads`前にJSON depth 32 / structure 32,768のpre-budgetを適用します。保存済みsoak artifactの旧baseline値は書き換えません。48 MiBの空き監視は途中snapshotやDB拡大用の余裕であり、単一responseのdecode/saveに十分なmemory・diskを保証しません。64 MiB tmpfs/256 MiB memoryのproduction cgroup実測は別gateです。上限を黙って広げず、保存済み証跡で原因を確認し、今回soakは再実行しません。

停止時は新規requestの発行を止め、transactionを完了またはrollbackさせ、最終snapshotとmetadataを回収する。応答しない場合も停止待機は有界にし、強制終了が必要だった事実を記録する。回収・検証に成功したtest containerだけを停止・削除し、回収失敗時は取得済みartifactと識別情報を保持する。無関係なcontainer/image/DBには触れない。

初回の無警告PASSは、60分完走、上限・隔離違反なし、DB/cursor整合、failure/anomaly/gap/content flagsなし、容量・memory閾値内、計測と最終backup回収成功を条件とする。自然発生した一時的5xxから正しく回復しても、その結果はWARNとしてreviewする。これは5xxをfatalに変更するものではない。ERROR/NEEDS_RESYNC、watchdogのgeneration差、disk failure、boundary違反では直ちに新規GETを停止する。回数上限による早期終了・失敗・cleanup失敗はSTOPPED、短縮live完走はPARTIAL、fixture正常完走はOFFLINE_PASSとする。

全体PASSでも、新着保存・wait_held=falseが未観測なら明記する。quiet Roomの結果だけでactive Roomの保存性能を保証しない。Claude/Fable監査、persistent storage/deployment検証、Human Approval、VPS移行は別gateとして残す。
