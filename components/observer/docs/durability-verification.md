# Persistent Storage Durability — offline検証記録

2026-09-07更新 / Observer Specification v0.3 / Technocore 0.12.1。

**Local Persistent Storage Durability gateはCOMPLETE。** host process matrixと実Docker named-volume recreationの確認を完了した。Local結果は `LOCAL_DURABILITY_PASS_WITH_HARDENING_WARNINGS`、`production_hardening_gate=PENDING`。今回のreviewでは保存済みartifactとoffline testsだけを使用し、追加live通信・Docker操作・soak再実行・Observer本体変更は行っていない。

## 実Docker recreation reviewとLocal gate完了（2026-09-07）

対象: [report.json](durability-results-recreation-20260907-192144-4562d78c/report.json)。通常Ubuntu terminalで実行された12.294秒のoffline試験で、external_requests=0。15 source hashが現在のrepositoryと一致し、A/Bのimage内source hashも一致した。元artifactは変更していない。

| 段階 | poll_seq | resolved_seq | messages | OPEN gap | status |
| --- | ---: | ---: | ---: | --- | --- |
| A explicit init | 100 | 100 | 0 | なし | INITIALIZED |
| A contiguous保存 | 103 | 103 | 3 | なし | RUNNING |
| A prefix gap保存 | 201 | 103 | 5 | 104..199 | DEGRADED |
| A停止・削除後、同一volumeでB open | 201 | 103 | 5 | 104..199 | DEGRADED |
| B first_since=201から継続 | 203 | 103 | 7 | 104..199 | DEGRADED |

generation=2、epoch=1、anchor=100を全段階で保持。A終了前とB open後でstate全field、各message行hash・全体hash、gap全field/hash、event一覧が一致した。再開後も既存5行を保持し、新着fixture2行だけが増えた。BのDB接続前inventoryではWALが存在せず、欠落WAL単体を異常とせず安全にopenした。

構成検査はcleanupを含む5回すべて21/21 PASS、volume fingerprint照合は6回一致。A/Bのprocess probeはそれぞれcore11/11 PASSで、noexec/nosuid/nodevだけがfalse/WARN。未知label/mount・追加writable persistent path・bind・host namespace等の拒否条件は維持されている。確認済みoptional labelは `desktop.docker.io/wsl-distro` のみ。両containerはgraceful exit=0、error/cleanup errorなし。A削除前artifactは削除event直前までの記録と一致した。

SQLiteは各段階でapplication_id=1413697346、user_version=1、integrity_check/quick_check=ok、WAL/FULL/foreign_keys=ON。state directoryは0700、DB/lock/sidecarは0600、UID/GID=65532:65532を確認した。`sqlite3.Connection.backup` で回収した32,768 bytesのbackupはSHA-256 `92527ebf64ecd0e77dc791f13d8cea7b2565d4f6ab641cc8578160d02e53276a` と一致。完了済みbackupのWALは0 bytesで、immutable/read-only再検査でもSQLite整合・state・7 messages・OPEN gapがreportと一致した。live DBの単純cpは使用していない。

artifact記録上の残存resource（今回Dockerへ再照会・cleanupしていない）:

- B: `observer-durability-20260907-192144-4562d78c-2`
- 未起動のbase-export container: `observer-durability-base-20260907-192144-4562d78c`
- named volume: `observer-durability-20260907-192144-4562d78c`
- image: `observer-durability-local:20260907-192144-4562d78c`、ID `sha256:fed4ddbbf66813778ef4625aa72a65329cca48fad0456ef65a648598e4275b8b`
- A `observer-durability-20260907-192144-4562d78c-1` は証跡保存後に削除済み。

**Local gateの範囲:** host上の31ケースでprocess restart・COMMIT前rollback/COMMIT後保持・init crash・terminal/fail-closed・single-writerを検証し、Dockerではnamed volume上のA削除/B再作成・state保持・安全な継続を実測した。この組み合わせを今回のHuman decisionに基づくLocal完了条件とする。Docker内全crash matrix、VM/WSL再起動、電源断、VPS実環境を確認済みとはしない。

helper単体の元reportには `full_durability_gate=PENDING` が残る。履歴を書き換えず、[統合review/final regression](final-regression-20260907.json) に `local_persistent_storage_durability_gate=COMPLETE` を記録した。`production_hardening_gate=PENDING`、VPS migration gateもPENDINGを維持する。

final regressionは **123 tests PASS / skip 0**（118 tests 18.920秒＋localhost HTTP 5 tests 0.833秒）。HTTP socketのsandbox制約のみ、同じcommandの承認済みsandbox外実行で解消した。Observer本体7ファイルは変更していない。[VPS移行前checklist](vps-migration-checklist.md)を作成した。以下は過去の実装・検証経緯であり、当時のPENDING/STOPPEDを保存する。

## 実装

- `tests/durability_payload.py`: offline専用RPC driver。explicit initと既存Storeのopenを分離し、実Observer/Store/StateLockを使う。固定fixture以外のHTTP fallbackはなく、socket作成・接続・名前解決とshell/subprocess起動もaudit hookで拒否する。productionには停止用CLIや自動bootstrapを追加していない。
- `tests/run_durability.py`: 同じpersistent directoryを使った別process再開、checkpoint barrier、実SIGKILL/SIGTERM、行hash比較、SQLite backup APIによるsnapshot回収。既定動作は計画表示のみ。`--execute-local`だけで今回の試験を再現できる。live modeはない。
- `tests/test_durability.py`: 実process matrixに加え、mount/volume/argvの拒否、所有者不一致、欠落DB/WAL、network禁止、backup方式、比較器のregression。Docker操作をmockしたtestはDocker永続性の実測には数えない。
- `tests/Containerfile.durability`: 既存base digestを指定した専用imageの定義。codeはimage側、stateだけnamed volume側に置く。`.dockerignore`にはこのhelperとContainerfileだけを追加許可した。

`src/technocore_observer/` の7ファイルは、既存live soakの `source-hashes.json` とSHA-256が全件一致する。取得・sequence/generation/gap・5xx/backoff、schema、既存P0 testファイルを変更していない。以前のsoakのWARNも変更していない。

## 実測結果

証跡: [report.json](durability-results-local-20260906-175824-5d4b3f34/report.json)、[source-hashes.json](durability-results-local-20260906-175824-5d4b3f34/source-hashes.json)。専用directoryは0700、report/backupは0600。意図的なpermission異常のfixtureだけは試験で設定した不正modeのまま保全する。

| 項目 | 結果 |
| --- | --- |
| helper status | `OFFLINE_PROCESS_PASS` |
| matrix | 31ケースPASS |
| 実行時間 | 7.182秒（helper、cleanup込み） |
| 外部通信 | 0。HTTP serverも使わない固定fixture |
| 強制停止 | SIGKILL 17回、SIGTERM 2回 |
| process再開 | 同じdirectoryの元DBを開き直す。backupからrestoreしない |
| runtime停止点 | HTTP受信後、message INSERT中、gap INSERT後、cursor UPDATE前、COMMIT前、COMMIT後。prefix gapあり/なしを実施 |
| COMMIT前 | 当該responseのmessage/gap/cursorがrollback。既存201から再取得 |
| COMMIT後 | 当該message/gap/cursorを保持。203または301から次のpollを開始 |
| Init停止点 | 6点。rename前はfinal DB不在・stale temp保持・再init拒否。rename後は完成stateのみ使用 |
| terminal state | NEEDS_RESYNC/ERRORを再開後も保持、writer open拒否、fixture poll 0回 |
| fail-closed | state不存在、room不一致、application_id/user_version不一致、物理破損、cursor accounting異常、directory/DB mode異常 |
| single writer | 別processの2番目だけflockで即拒否。元writerは201から再開 |
| SQLite | integrity_check/quick_check=`ok`、foreign_key_check空、application_id=`0x54434F42`、user_version=1、runtime WAL/FULL/foreign_keys=ON |
| WAL観測 | kill後、DB connectionを開く前に存在/size/modeを記録。runtime killでは非空WALを確認。WALの手作業削除なし |
| 内容 | raw/text/flags/hash/trust等をfixtureと照合し、既存全message行（ingested_atを含む）とgap行のhashを再開前後で比較 |
| backup | SQLite backup API、転送hash、回収後integrity確認PASS。稼働中DBの単純cpなし |

通常fixtureはanchor100→103→201→203→301。最終状態はmessages=9、poll_seq=301、resolved_seq=103、status=DEGRADED、OPEN gapは104..199と204..299。再度processを作り直しても全state/message/gap/eventの論理内容が一致した。

COMMIT前でも`start_poll()`が別transactionで更新した`last_poll_at`は変化し得る。これは許容し、cursor/message/gapの部分保存と混同しない。正常終了後の再開ではlivenessを含むstate全行の一致も確認する。

## Offline regression

以下を個別実行し、**合計69テストPASS**。全suite一括実行やDocker実測を主張するものではない。

```text
python3 -B -m unittest discover -s tests -p test_durability.py -v
13 tests / 7.614s / OK

python3 -B -m unittest discover -s tests -p test_observer.py -v
23 tests / 1.340s / OK

python3 -B -m unittest discover -s tests -p test_crash.py -v
3 tests / 0.342s / OK

python3 -B -m unittest discover -s tests -p test_soak.py -v
30 tests / 9.088s / OK
```

初回のdurability testはhelperの括弧欠落によるSyntaxErrorで読み込み時に停止した。1回の修正・再実行で上記13件が成功。その後helper CLIも独立して31ケース完走した。

```text
python3 -B tests/run_durability.py --execute-local
```

owner不一致の拒否はgeteuidをmockしたtestで確認し、host側のchownは行っていない。SQLite_FULLとwrite failureは既存Observer/soak regressionで確認し、今回のdurability matrixではdiskを埋める試験は行っていない。

## Docker段階と未達項目

Docker backendにも同じmatrixを実装した。同一containerのstop/start、checkpointでkill後のstart、A削除後にBを新規作成し元volumeを再利用する操作を含む。削除は自分で作成・識別照合したcontainerだけで、`-v`/prune/volume削除/backup restoreは使わない。失敗時は停止してcontainerとvolumeを保全する。成功時もvolumeは残す。

全containerはnetwork=none、UID/GID 65532、read-only root、cap-drop=ALL、no-new-privileges、memory256MiB/pids32。mountは指定local named volume→`/state`だけ。volume name/driver/scope/options/label/CreatedAt、image ID、起動時の実source hashを照合する。別volume、bind、追加mount、control socket、host namespaceは許可しない。

**当初のstrict policy（以下は経緯）:** 起動直後に`/proc/self/mountinfo`を検査し、noexec/nosuid/nodevが1つでも欠ければfixture init前に停止した。現在のLocal recreation profileは末尾のHuman decision反映を参照。root runtime、mount option変更、driver optionやhost path追加などによる自動回避は実装していない。

このセッションからDocker socketへ接続できないことは既に確認されているため、今回再試行や権限変更はしていない。専用imageのbuildも未実施。build時の`--network=none`はRUNの通信を制限するだけで、registryへの解決通信まで禁止する保証にはならない。未承認のregistry通信が必要なら、その操作を停止して確認する。

次段階の計画表示（Dockerへの接続・書き込みなし）:

```text
python3 -B tests/run_durability.py
```

既存baseからの専用image準備とmount条件を確認した後に使用する実行形式:

```text
python3 -B tests/run_durability.py --execute-docker --image-id <local-sha256-image-id>
```

現時点の未達は、実Docker image/volumeのpreflight、container stop/start・削除/再作成後の元volume保持、共有volume上のlock、非root所有権/mount属性の実測。これらがPASSするまで、Persistent Storage Durability全体は未達とする。Windows/WSL/VM再起動、電源断、device/fsync故障も今回のPASS範囲外。

## Named volume recreation用の1コマンドhelper（追記）

`tests/run_container_recreation.py` と `tests/test_container_recreation.py` を追加した。上記の手動image準備手順に代えて、次の1コマンドでlocal image準備とA→Bのrecreation試験を行える。既定動作は計画表示で、`--execute`指定時だけDockerへ接続する。

```text
python3 -B ~/technocore-observer/tests/run_container_recreation.py --execute
```

Registryを利用するbuild/pullは実装していない。すでにlocalにある固定base IDをinspectし、**一度も起動しない**nonroot・network=none・read-onlyの準備containerをexportする。そのrootfs tarへreview対象のpublic sourceとUID/GID 65532・0700の `/state` を追加し、local fileからimage importする。hostへtarを展開せず、host HOME/config/keysを参照・mountしない。準備containerも含め、起動時のroot権限やpackage installは不要。import後のcodeはread-only image側に置き、起動時hashを再照合する。

fresh local named volumeを作成してAを起動し、anchor100→103→201まで保存する。A終了後、state/rows/gapsとcontainer/volume IDをfsync済みartifactへ記録してからAを削除する。同じvolumeのBでSQLite検証を行い、全stateとmessage/gap行hashの一致を確認する。Bの最初のpollはsince=201、保存後はpoll_seq=203、resolved_seq=103、messages=7、OPEN gap=104..199、DEGRADEDを期待する。SQLite backup APIでBのsnapshotを回収する。元volumeへbackup restoreは行わない。

`/state`以外のpersistent writable mountとrootfsのread-only属性をprocess内でも検査する。従来のnoexec/nosuid/nodev検査は維持した。named volumeで欠ける場合はfixture init前に停止し、自動でmountや権限を緩めない。

Docker操作は各30秒、RPC/barrierは10秒、試験は最大15分。rootfs archiveには512MiBの受入上限を設ける。network=none、外部GET/POST=0で、live modeはない。

証跡は `docs/durability-results-recreation-<日時>-<識別子>/` に保存する。`report.json`にはcontainer名/ID、volume名/driver/CreatedAt/label、image ID/tag、mount/隔離検査、再開前後のstate/hash/SQLite検証を記録する。A削除前に `before-remove-<A名>.json`、終了処理前に `before-cleanup-report.json` を保存する。証跡保存失敗時は削除しない。B・準備container・image・volumeは確認用に残す。volumeの自動削除はしない。

既存full matrix helperにも削除前のartifact保存を追加した。終了処理はstdin EOFに依存せず、offline driverへの明示的shutdown RPCにした。stateをまだ開いていないprocessも安全に終了できる。Observer本体7ファイルのSHA-256は以前のlive soak証跡と全件一致し、変更していない。

### shutdown修正後の再確認結果

usage windowリセット後、関連する5 suiteをすべて再実行し、**78テストPASS**。新規recreation testは実processのA→B相当の再開と、Docker操作順のmock regressionを含む。mockをDocker実測のPASSとは扱わない。

| suite | 件数 | 秒 | 結果 |
| --- | ---: | ---: | --- |
| test_container_recreation.py | 9 | 0.421 | PASS |
| test_durability.py | 13 | 7.910 | PASS |
| test_observer.py | 23 | 1.476 | PASS |
| test_crash.py | 3 | 0.464 | PASS |
| test_soak.py | 30 | 9.260 | PASS |

shutdown変更途中に、state操作前のSessionで `last_case` 未定義のAttributeErrorを検出した。初期値を与える1回の修正と再実行で解消し、stateを作らずshutdownするregressionも追加した。上表はその修正後の結果である。

PASS後に承認済みのsandbox外local Docker接続確認を再実行したが、`permission denied while trying to connect to the docker API` が継続した。そのため実image準備、named volume作成、container起動はこのセッションでは未実施。通常Ubuntu terminalへ上記1コマンドを引き継ぐ。Docker socketのpermission/ownerやsystem設定は変更していない。追加live通信も行っていない。

このhelperが成功した場合の判定名は `OFFLINE_RECREATION_PASS`。今回指定されたA→Bの永続性のみを示し、未実施のDocker crash/full matrixや全体durability gateを一括PASSにはしない。

## Configuration拒否の診断修正

20260906-223132-4d8372a3の実測は、container A作成後・起動前の `CONTAINER_CONFIGURATION_REJECTED` で停止した。個別checksを保存する前に例外を出していたため、元artifactから不合格条件は特定できなかった。

`tests/durability_diagnostics.py` を追加し、`run_durability.py` の通常検査とcleanup検査で、例外の前に `configuration-<連番>.json` を0600で保存・fsyncするよう修正した。container ID、stage、全checks、failed_checks、安全な観測値、現在の期待値を記録する。診断書込みが失敗した場合も、起動や削除へ進めない。

保存する文字列は既知のDocker enum・この試験で設定した固定値・Docker IDに限定する。container環境変数は既知の変数名と期待HOMEとの一致だけ、任意label・driver options・bind元path等は存在/件数等の情報に限定し、未知の値はredactする。host環境変数やinspect全量、subprocessのraw stdout/stderrはartifactやterminalへ保存・出力しない。既存checks・期待条件・mount条件は変更していない。

次のoffline regressionは全件PASS:

| suite | 件数 | 秒 |
| --- | ---: | ---: |
| test_durability_diagnostics.py | 7 | 0.016 |
| test_container_recreation.py | 9 | 0.365 |
| test_durability.py | 13 | 7.300 |

計29テスト。secretを模した文字列の非保存、拒否前の保存、cleanup拒否の記録、診断書込み失敗時の停止を検証した。CAP_ALLやVolumeOptionsのfixtureは診断用の仮想入力であり、今回の実Dockerで観測した値ではない。Observer本体7ファイルは元実測manifestと全件SHA-256一致する。

既存Aだけへのread-only inspectを試みたが、sandbox内・承認済みsandbox外の両方で `DOCKER_PERMISSION_DENIED` だった。最新記録は [inspect report](durability-inspect-20260906-224024-ac306199/report.json)。取得失敗のため、failed check/observed value/原因分類は引き続き未確定。取得していない値を推測して期待値を緩める修正は行っていない。

通常Ubuntu terminalでの診断用1コマンド:

```text
python3 -B ~/technocore-observer/tests/durability_diagnostics.py --execute
```

対象は固定で `observer-durability-20260906-223132-4d8372a3-1` のみ。Docker操作はcontainer inspect 1回だけ。image/volume/containerの作成・起動・停止・削除、recreation再実行、設定変更、live通信は行わない。`docs/durability-inspect-<識別子>/report.json` のexpected/observed/failed_checksを取得した後に原因と最小修正を判断する。

## extra_label_keysの最小追加

通常Ubuntu terminalで取得された [224311のreport](durability-inspect-20260906-224311-9cf753a1/report.json) は、purpose値一致、labels総数2、追加1、他20checks PASSを示した。ただし追加label名・値は未記録なので、由来や無害性はまだ判断できない。

追加承認の範囲でdiagnosticだけを拡張し、expected以外のkey名を `extra_label_keys` に保存するようにした。値・label mapは保存しない。expectedの設定値そのものもdiagnostic出力から外し、purposeのpresence/value一致結果だけを保持する。実際のlabel期待条件、Observer本体、network/mount/権限条件は変更していない。

keyはstring型・1〜256文字・ASCIIの英数字と`_.:/-`（先頭は英数字）を検査し、最大64件を記録する。上限超過や不正型・制御文字等は元keyを出さず、`label_key_diagnostic_flags` と `extra_label_keys_complete=false` を記録する。これは診断出力の制限であり、labelの許可条件を緩めるものではない。key名もuntrusted dataのままで、URLやinstructionとして解釈しない。

offline testsは、diagnostic 13件、recreation 9件、durability 13件の**計35件PASS**。追加0件・1件・複数、extra/expected value非保存、実artifactへの非漏洩、keyの型・長さ・文字・件数上限を確認した。通常およびcleanup拒否の記録も維持する。

同一containerへのread-only inspectをsandbox内・sandbox外で各1回試みたが、いずれも `DOCKER_PERMISSION_DENIED`。最新の取得失敗記録は [225025のreport](durability-inspect-20260906-225025-500cf55e/report.json)。containerを起動・再作成しておらず、設定変更や外部通信はない。key名を未取得のため、Docker Desktop/image/Engine由来か、security boundaryへの影響、allowlist対象にできるかは未判定とし、未確認labelを一般許可する変更は行わない。

実行コマンドは前項の `tests/durability_diagnostics.py --execute` と同じ。今回から正常取得時にはartifactだけでなくterminalのJSONにも `extra_label_keys` を出すため、追加key名を直接確認できる。recreation testは実行しない。

## 確認済みWSL metadata labelの限定許可

ユーザーが通常Ubuntu terminalで取得した追加keyは `desktop.docker.io/wsl-distro` だった。Docker公式Docsおよび公式for-win issueのinspect例での確認情報と、この1 keyだけを許可する明示方針を受け、container label検査を最小修正した。新しい外部通信による調査は行っていない。

必須 `technocore-observer.durability` の存在・設定identityとの完全一致は維持する。optional keyのallowlistは **`desktop.docker.io/wsl-distro` の1個だけ**。そのvalueの内容・有無からsecurity判断を行わず、値はdiagnosticにも保存しない。未知key、同じnamespaceの別key、似たprefix/suffixのkeyは拒否する。volume側のlabel条件は従来のままである。

全label mapの完全一致が不要なのは、この確認済みmetadataの存在が、helperの所有識別や操作権限を決める条件ではないため。helperはpurpose/identityとresource識別を維持し、network、rootfs、user、capabilities、mount、entrypoint等は従来の実値検査で判定する。追加metadataを根拠にそれらの検査を省略したり、optional valueをcommand/設定として解釈したりしない。今回の停止は、この確認済みmetadataまで拒否したhelper期待値の過剰制約として扱い、Observer durability bugとは扱わない。

既存reportとの互換性のためcheck名 `exact_labels` は維持するが、その内容は「必須label完全一致＋列挙したoptional keyだけ」である。diagnosticには `optional_metadata_keys`、`optional_values_used_for_security=false`、`unknown_extra_keys_allowed=false` を記録し、全map完全一致と誤認しないようにした。観測値にも許可済みkey名と残りの未知key数を分けて示す。

関連offline regressionは**39テストPASS**:

| suite | 件数 | 秒 |
| --- | ---: | ---: |
| test_durability_diagnostics.py | 17 | 0.040 |
| test_container_recreation.py | 9 | 0.360 |
| test_durability.py | 13 | 7.721 |

optional keyなし/あり、optional value変更、必須purpose欠落/値違い、未知追加key、近似key、値のartifact非漏洩、volume条件維持を確認した。他20のcontainer checksはコードを変更せず、optional valueの変更前後で20項目の結果が一致することも確認した。Observer本体7ファイルは元実測manifestとのSHA-256一致を再確認した。

named-volume recreation helperの既定計画表示も確認した。起動時のnoexec/nosuid/nodev等の既存mount gate、read-only root、非root・network=none、削除前の証跡保存は維持する。今回の修正後に実Docker recreationやlive通信は実施していない。既に記録されたこのsessionのDocker socket接続拒否があるため、通常Ubuntu terminalでの実測用コマンドは次の1つ:

```text
python3 -B ~/technocore-observer/tests/run_container_recreation.py --execute
```

この実測の結果を確認するまではrecreation PASSや全体durability gate PASSに更新しない。

## Local mount policy — Human decision反映

`durability-results-recreation-20260906-231130-1030b653` の保存済みprobeは14項目中11項目PASS、`state_mount_noexec/nosuid/nodev` の3項目falseだった。SQLite init前の停止であり、Observer durability bugの証拠ではない。元のSTOPPED artifactは変更していない。

今回の承認を `local-docker-desktop` profileとしてhelperに限定して実装した。payloadとhostの両方で3属性だけをwarning扱いにし、falseの観測値、warning対象、hard-fail項目を保存する。汎用durability helperとpayloadのdefaultはstrictのまま。専用imageのentrypointにprofileを固定し、inspectでもその完全一致を検査する。profile不一致やprobe欠落は停止する。

その他11のprocess checksと21のcontainer configuration checksを維持した。unknown label、unknown/additional mount、bind、unexpected persistent writable pathを許可する変更はない。Docker-managed named volume、nonroot、network=none、read-only root、cap-drop ALL、no-new-privileges、seccomp、host HOME/key/control socket非公開を維持する。Observer本体7ファイルは元実測manifestとSHA-256一致。

reportは次を独立表示する。

| Field | 判定 |
| --- | --- |
| isolation_boundary | A/Bの構成・process probe・readyが揃えばPASS。違反はFAIL、未完了はPENDING |
| persistent_storage_durability | 上記PASSかつrecreation/state比較/resume/SQLite backup fixture成功でPASS。それ以前はPENDING |
| mount_hardening_noexec/nosuid/nodev | 観測trueはPASS、falseはWARN、未観測はPENDING。A/BのどちらかがfalseならWARNを保持 |
| status | LOCAL_DURABILITY_PASS_WITH_HARDENING_WARNINGS、LOCAL_DURABILITY_PASS、またはSTOPPED |
| production_hardening_gate / full_durability_gate | PENDING。Local recreation成功だけでは更新しない |

cleanupで構成拒否があってもdiagnosticとhardening観測を保持し、全体statusはSTOPPEDにする。A削除前の証跡保存、B/image/volumeの保持、SQLite backup APIによる回収は変更していない。

offline regression: **44 tests PASS / 7.606秒**（recreation 14、durability 13、diagnostics 17）。31ケースの実process matrix、再作成相当のprocess再開、COMMIT前後停止、SQLite state/cursor/gap/permissions/backup検証に加え、3属性だけの許容、他11項目それぞれの拒否、欠落/未知/型不正、host側profile照合、label/mount拒否、reportのPASS/WARN/PENDING分離を検証した。

[機械可読offline結果](durability-local-mount-policy-regression.json)にsource hashと保存済みprobeのpolicy replayを記録した。元probeはLocal policyでaccepted、strict policyでは拒否される。これはpolicyのoffline確認であり、named-volume durability実測結果ではない。

VPS productionでは別のsecurity gateを設け、noexec/nosuid/nodev、専用state path、ownership、0700/0600、backup/recovery整合性を実環境で再検証する。[更新計画](persistent-storage-durability-plan.md)のとおり、今回はVPS bind mountを実装していない。Local warningをproduction-readyと扱わない。

Docker APIへのread-only接続確認はsandbox内・承認済みsandbox外の双方でpermission deniedだった。今回container/image/volumeの作成・再実測やlive通信は行っていない。通常Ubuntu terminalでの実測用1コマンド:

```text
python3 -B ~/technocore-observer/tests/run_container_recreation.py --execute
```

固定local imageからexport/importし、fresh named volumeでA→削除→Bを検証する。pull・外部通信なし、最大900秒、全container network=none。実測後に保存されたreportを確認するまで、Local named-volume recreationはPENDING。
