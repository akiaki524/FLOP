# Soak helper offline verification — 2026-09-06

**offline検証・Docker内6秒検証はPASS。その後のlive soakはWARNで完了し、再実行しない。** 最新のtelemetry修正と30件のoffline regression結果を末尾に記録した。元のlive証跡やWARN判定は変更していない。

## 実装

- `tests/run_container_soak.py`: host側の単一request Gate、monotonic期限、resource記録、独立watchdogの起動、backup受信・整合検査、結果保存、限定したtest containerの停止・cleanup。
- `tests/soak_payload.py`: lockとStoreを保持するObserver process、1 RPCにつき最大1 GET、metadata計測、別processのread-only watchdog、SQLite backup APIと固定filenameのbinary転送。offline modeはネットワークclientを作らない。
- `tests/test_soak.py`: 新規25テスト。既存P0 testファイル、Observer本体、sequence/generation/gap、5xx/503/backoff処理は変更していない。

全GETはinit=1、config=1、poll<=360、watchdog<=12、合計<=374。failed requestも発行前に回数へ算入し、任意diagnostic/URLを追加する入口はない。init/configを含むnetwork phase全体を最大3600秒とし、終了処理でGETを発行しない。

## テスト結果

`python3 -B -m unittest discover -s tests -p test_soak.py -v` の24テストが成功。その後追加したDocker argv検査1テストも個別に成功し、**合計25テスト成功**。一括25件の再実行を主張するものではない。

| 検査 | 結果 |
| --- | --- |
| 全roleのbudget | 374件ちょうどを許容し、それ以上を拒否。role capと未知roleも検査 |
| Time/backoff | duration>3600/NaN等を拒否。待機中の期限切れ、600秒delay、同時/早期request拒否、initを期限に含めることを確認 |
| 仮想60分 | 仮想monotonic clockと実際のObserver process・別processのwatchdog・実SQLiteを使用。374以下、watchdog<=12、複数backup<=13、期限後の新規requestなし |
| Generation | NEEDS_RESYNC、cursor不変、Inbox非保存、後続GET拒否。watchdogのgeneration差でもGate停止 |
| Protocol | 同一cursorで3回異常→ERROR。以後GETしない |
| HTTP | fixtureの503と429でcursor不変・backoff、Retry-Afterの600秒clamp。その後の回復を確認 |
| Gap / wait | prefix gapはDEGRADEDで継続、resolved不変、wait_held=falseを測定しdelayを尊重 |
| Disk/boundary | fault injection後、Controllerは新規GETを停止。host空き不足ではworkerを起動しない |
| Memory | 閾値超過2サンプル、OOMカウンタ、計測欠落で停止する分岐をfixtureで確認 |
| Lock | 別processの2番目のwriterを拒否 |
| Backup | 実際のWAL接続からbackup APIを使用し、commit済みmessageとcursor、raw制御文字を保持。受信側のhash/schema/sequence/count検査を通過 |
| Artifact境界 | traversal名、不正hashを拒否。snapshotは0600。raw本文をmetadata logへ出さない |
| Process timeout | 実際のpipe/RPCで無応答を期限内に検出 |
| 終了 | cleanup失敗時のreportはSTOPPEDになり、PASSを残さない |
| CLI / Docker計画 | defaultは操作なし、explicit tail必須。offline preflight失敗でlive起動なし。create argvに固定image/非root/readonly/cap-drop/network/容量上限があり、secret/control socket/bind mountなし |

仮想時間の試験は速く終了するfixture検証であり、live load testでも、実時間60分のmemory leak試験でもない。resource閾値の一部はfault injectionで、実際のcontainer OOMやdisk fullを誘発していない。

## CLI全体のoffline実行

```sh
python3 -B tests/run_container_soak.py --offline --init-mode tail --duration-seconds 6
```

保存先: `docs/soak-results-offline-20260906-132533-af35b7d6/`。

| 項目 | 結果 |
| --- | --- |
| Status | OFFLINE_PASS |
| 外部request | **0** |
| 疑似GET | init=1、config=1、poll=1、合計3 |
| 時間 | network phase上限6秒。最終backupを含む記録は約6.09秒 |
| Cursor | anchor=100、poll=resolved=101、RUNNING |
| Failure/anomaly/gap | すべて0 |
| Backup | 最終snapshot回収成功、message=1、flagged message=0、gap=0 |
| Watchdog | この6秒実行では5分間隔に到達せず未実行。別process連携は仮想60分テストで確認 |

実行計画、container用source archive/hash、host helper hash、metrics JSONL、SQLite snapshotとmetadata、reportを保存した。offline fixtureの本文はSQLiteにだけ保持し、terminalにはstatus・件数・保存パスだけを表示した。

## Host検証終了時点の残る確認

このCodex sessionのDocker接続制約は未解消であり、今回はDocker接続を再試行していない。新helperのDocker起動・cgroup値・binary転送・停止を一通り実測するには、通常Ubuntu terminalでまず次を実行できる。これはnetwork=noneでliveへ進まないcommandである。

```sh
python3 -B tests/run_container_soak.py --container-offline --init-mode tail --duration-seconds 6
```

live soakはまだ開始していない。実時間60分のresource成長、自然な新着/HTTP障害、Docker終了経路の実測、永続storageとdeployment構成、外部コード監査、Human Approval、VPS移行gateは引き続き未完了。[運転条件と上限](soak-plan.md)を維持する。

## Docker内offline検証の確認（2026-09-06 13:34 JST）

通常Ubuntu terminalで実行された `docs/soak-results-offline-20260906-133413-292491f0/` を確認した。

| 項目 | 結果 |
| --- | --- |
| Report | OFFLINE_PASS、疑似request_count=3、offline=true、warnings=[]、最終snapshot回収成功 |
| Network | fixtureのみ、external_requests=0という実行報告。設定検査とprocessのloopback_only検査もPASS |
| 隔離 | configuration.jsonとprocess検査の全項目true。非root、secret/control socket/bind mountなし、read-only root、権限・容量上限を維持 |
| Source | container用9ファイル、host helper 3ファイルのhashが現在のsourceと一致。archiveも一致 |
| SQLite | WAL/FULL/foreign_keys読戻し成功。回収snapshotのhash・schema・quick_check・sequence accounting・件数検査に合格 |
| State | anchor=100、poll=resolved=101、RUNNING。message=1、gap=0、flagged message=0 |
| Resources | 記録されたRSSは約29～30 MiB、container memoryは約45 MiB。tmpfs空きは約63.9 MiB、OOM=0、閾値超過なし |
| 時間/終了 | 6秒の発行期間＋最終backup等でreportの経過時間は約6.43秒。cleanup_errorなし。running_before_stop=trueはidle container停止前の記録で、停止失敗を意味しない |

configのdeployment値が全てnullなのは、offline fixtureがversionと空settingsだけを返すためであり、live configの回帰ではない。configのvalid_envelope=falseもRoom envelope検証の対象外を意味する。いずれも今回の警告ではない。

6秒試験のため、5分間隔のDocker内watchdogと途中backupはまだ発火していない。その連携はhost上の仮想60分テストで確認済みだが、Docker内の長時間実測とは区別する。今回の証跡にbounded live soakを妨げる不一致は見つからなかった。

次は既定の `--execute --init-mode tail --duration-seconds 3600` で、offline preflight再実行後に最大60分・全GET374以下のlive試験へ進める。既定の停止条件・network/secret境界は維持する。このreviewではlive soakを開始していない。

## Live soak後のtelemetry最小修正

`docs/soak-results-live-20260906-133730-1b37726d/` の解析では、300試行のうち通信失敗2回とHTTP 503が1回あり、WARNとなった。終了時はRUNNING、generation=2、poll=resolved=756、gap=0、保存message=4。watchdog 11回すべてOK、backup 12件の整合検査に合格した。例外型の欠落と時間・plan metadataの曖昧さに対応し、次だけを変更した。

| 修正 | 内容 |
| --- | --- |
| 通信例外 | harnessのMeasuredClientでerror_class/整数errno/内包例外reason_classを記録し、元の例外をそのまま再送出。str/reprや文字列reasonは記録しない |
| Phase時刻 | 新規request発行の閉鎖、最終backup完了、cleanup完了を分離。失敗したphaseの成功時刻はnull。Unix時刻とmonotonic経過時間を併記 |
| 総時間 | 従来elapsed_secondsは維持し、cleanupまで含むtotal_elapsed_secondsを追加 |
| Plan/runtime | planの固定soak_started=falseを削除。実行事実はreportのphase_timesで表す |
| Resource周期 | 60秒目安・処理完了後という実装に表記を合わせ、sample_interval_secondsと最大実測間隔を追加。schedulerは変更しない |

変更対象のPythonは `tests/soak_payload.py`、`tests/run_container_soak.py`、`tests/test_soak.py` のみ。src配下のacquisition/sequence/generation/gap/5xx処理、SQLite schema、既存P0 testファイルは変更していない。

実行したoffline regression:

```text
python3 -B -m unittest discover -s tests -p test_soak.py -v
30 tests: OK（skipなし、約8.66秒）
```

追加した5件と既存25件を一括実行した。TimeoutError/ConnectionResetError/URLErrorの安全なmetadata、元例外の同一性、例外文をformatしないこと、通信失敗→503→正常応答の既存retry/cursor挙動、plan固定flag削除、時刻のphase順序とwall clock変更に影響されない経過時間を確認した。既存のcleanup失敗・開始前failure試験にもphase時刻のassertを追加した。

全fixtureはoffline。Docker操作、soak CLIでの再運転、追加live通信、依存追加、system変更は行っていない。実行済みlive artifactsには新しいfieldを後付けしていないため、過去2件の通信失敗の例外型は引き続き確定できない。

当時の次の未達項目は [Persistent Storage Durability計画](persistent-storage-durability-plan.md) に整理した。その後、2026-09-07にhost process matrixと実Docker named-volume recreationでLocal durability gateを完了した。production hardeningはPENDINGで、残項目は [VPS移行前checklist](vps-migration-checklist.md) を参照。soakの元WARNと再実行しない方針は維持する。
