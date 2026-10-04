# 運用手順と残りの移行条件

修正前のLocal Persistent Storage Durabilityは完了（Docker recreationはmount hardening WARN付き）。P1修正時点のoffline baselineは148 tests PASS / skip 0です。今回のNEW-1/NEW-2とcleanupの結果は[Pre-VPS final remediation artifact](pre-vps-final-remediation.json)を参照してください。ユーザー提示のOpus focused re-reviewで旧P1 5件はCLOSEDですが、今回の差分への独立reviewと[VPS移行前checklist](vps-migration-checklist.md)のproduction gate・Human Approvalは未達です。以下のdeployment command例は実行承認を置き換えません。

## 承認境界

リポジトリ内の可逆な実装・offlineテストは自律実行の対象です。秘密情報へのアクセス、リポジトリ外への書き込み、システム設定変更、外部依存のinstall、破壊的操作、外部への送信・公開、Technocoreへのwrite、network・権限範囲の拡大には明示承認が必要です。

過去の追加Autonomy policyで承認された隔離試験・live smokeは実施済みです。今回のfinal remediationでは外部通信、live GET、Docker、VPS、package installを行いません。以下のdeployment image・永続volumeの手順は別gateです。

## Secret isolation

`Containerfile` はhostのHOME、鍵、agent socket、Docker socketを一切mountしない実行のためのテンプレートです。イメージにコピーするのはObserverのPython sourceだけです。専用の非root UID 65532で動かし、state directoryを0700、DBを0600とします。

base imageのdigestはContainerfileに固定済みです。buildが未承認の外部registry通信を必要とする場合は停止し、その通信について承認を求めます。以下はdeployment用の手順例です。

```sh
docker build -f Containerfile -t technocore-observer:local .
docker run --rm --read-only --cap-drop=ALL --security-opt=no-new-privileges --pids-limit=32 --memory=256m --mount type=bind,source=/srv/technocore-data/observer-state/observer-id,target=/state technocore-observer:local run --room example --state-dir /state --check-config
```

Production stateは追加データdisk上のObserver専用directory `/srv/technocore-data/observer-state/<observer-id>` を唯一のpersistent writable bindとしてcontainer内 `/state` に配置します。上記の`observer-id`は実際の専用directory名に置き換えます。Dockerに空directoryを自動作成させず、事前にmount先が追加disk上にあること、directoryがUID/GID 65532・0700、DBが0600であることを確認します。既存stateの移動はObserverを停止し、WALを含むSQLiteの整合性と全stateの保全・移動後の読戻しを確認してから同じDBをbindします。再init、messages/gapsの破棄、稼働中DBの単純cpは行いません。実移行・host設定変更はHuman Gateです。

containerへこのstate以外のbind mount、credentials、環境変数経由のsecret、追加capabilityを与えないでください。受信上限は4 MiB、JSONのdepth/structureにもdecode前の予算があります。256 MiB RLIMIT_ASのoffline試験と、production containerの256 MiB cgroup実測を区別してください。後者は未完了です。

live前の隔離検証では、mount設定・UID・capabilityを確認し、実際の鍵を読みません。必要なら秘密情報でないcanaryファイルでhost HOMEが見えないことを検証します。専用Linux user案を採用する場合も、HOME変数だけでなく実際のfilesystem権限で隔離してください。その作成・設定変更には別途承認が必要です。

アプリケーションは固定のread requestだけを生成します。コンテナのnetwork自体をHTTP path単位で制限する仕組みや、任意のPythonコードを実行させた場合のsyscall sandboxは提供していません。外部contentに実行機能を与えるAPIは存在しません。smokeでの隔離検査はPASSしましたが、deployment imageと永続volumeにも同じ検査が必要です。[確認範囲](smoke-review-20260906.md)を参照してください。

## 初期化と停止

`init --mode tail` はnonempty Roomの最新seqをanchorにします。SQLiteは固定名 `state.sqlite.init` にDELETE journal・FULL synchronousで完成させ、close、fsync、rename、親directory fsyncの順で公開します。正常に完了するまで最終DBを使いません。

中断してstale initファイルが残った場合、再initは拒否します。自動削除・上書きはありません。Humanがファイルを保全・確認し、cleanupを承認してから操作してください。既存の正常DBに対するinitも拒否します。

`run` はflockをprocess lifetime中保持します。SIGINT/SIGTERMは処理中のrequest/transactionが戻ったところで終了します。socket timeoutは25秒です。長いRetry-After待機は停止signalで中断できます。

`NEEDS_RESYNC` と `ERROR` は再起動しても解除しません。手動resyncは以下の明示commandだけで行います。SQLiteの値を書き換えて自動復帰させる運用はサポートしません。

## 明示migrationとmanual resync（offline）

Observerを停止し、同じ専用UID・state権限で操作します。v1 DBではまずSQLite backup APIによる保全をHumanが確認し、`migrate-plan --room example --state-dir /state`のstate/counts/hashをreviewします。そのtokenを`migrate --room example --state-dir /state --approval TOKEN_FROM_REVIEW`へ渡したときだけ、既存5 tableのrowsを変えずv2の2 tableをtransactionで追加します。自動migrationはありません。中断時はv1または完成したv2のどちらかです。

gapまたは異常停止のreviewでは、旧epoch/generation/poll_seq/resolved_seq、gap範囲、evidence、以後のAnalyzer処理への影響を確認します。新anchorはHumanが明示的に選択します。現在poll_seqで新epochを始める場合も、未取得範囲が回収できた意味にはしません。generation変更時は新generation/anchorの根拠を別途確認してください。このcommand自身はlive tailを取得せず、入力をHUMAN_ASSERTED_NOT_LIVE_VERIFIEDとして記録します。

```sh
python3 -B -m technocore_observer resync-plan --room example --state-dir /state --anchor-seq 201 --generation 7 --reason 'Human reviewed unobserved range; begin new epoch'
python3 -B -m technocore_observer resync --room example --state-dir /state --anchor-seq 201 --generation 7 --reason 'Human reviewed unobserved range; begin new epoch' --approval TOKEN_FROM_REVIEW
```

数値は例です。Humanがplanの旧state・新anchor・reasonを確認してから同一内容とtokenを指定します。planを自動的にresyncへ渡すsupervisorは禁止です。stateやdecisionが変わればtokenは無効になります。tokenは承認対象のhashであり、署名・本人認証ではありません。

resyncはflockと1 transactionで旧state/decisionをepoch_historyへ追記し、epochを1増やします。旧messages/gaps/evidence/eventsを削除・上書きせず、旧gapはOPENのままです。heartbeatは現epochのopen_gap_countとhistorical_open_gap_countを区別します。Analyzerはepoch/generationを指定し、epoch境界の欠落を連続取得とみなさないでください。容量予算は全epoch共通で、resyncでは解除されません。

NEW-2: 1 epochでHuman reviewなしに受理できる量はOPEN gap **16件以下**かつ累積未観測 **50,000 seq以下**です。等号は許可し、初めて超えるgap-bearing pollはmessages/gap/cursorを保存せず、`EPOCH_GAP_BUDGET_REQUIRES_REVIEW`のevidence/eventとterminal ERRORを保存します。protocol anomaly数には加算しません。既存evidence/gapは保持され、再起動はHuman reviewでブロックされます。上限上のcontiguous/empty pollは許可します。承認したmanual resyncでのみepochが増え、現epochのgap集計が新しくなります。

16件は小規模Observerで人が範囲を確認できる件数を重視したpolicyです。limit=200のgap-bearing poll 16回は最大3,200 messagesです。50,000 seqは許可最大prefix 10,000の5回分までとし、大量の未観測seqを抱えたまま進め続けることを防ぎます。これらはworkloadの実績に応じHumanが再評価する値です。大きなrecords、過去epoch、gap以外のtrafficによる容量消費は別途監視が必要で、絶対的なDB空き容量保証ではありません。newest-Nにより正当なhigh-volume roomでもgapは生じ得ます。攻撃・物理喪失の判定、TTL、自動回収・自動resyncには使いません。

DB障害では非0終了し、transaction内のcursor先行を防ぎます。ERROR state自体が永続化できない場合もあるため、supervisorは終了コードを監視してください。

## 監視とwatchdog

`run` はpoll試行ごとにJSON heartbeatを出力します。`heartbeat` はnetwork requestなしでDBのsnapshotを出力します。`watchdog` は別processとして最新1件を読み、取得後のSQLite snapshotとseq・generationを比較します。Observer本体のlockを取得せず、tableは更新しません。ただし同一UIDとSQLite WAL sidecarへのwrite permissionに依存します。read-only mountや別UIDで動くと仮定しないでください（VPS前の再確認対象）。

watchdogの結果は次のとおりです。

| result | 意味 | 終了コード |
| --- | --- | --- |
| OK | 独立サンプルで先行messageを検出しなかった | 0 |
| BEHIND | 新着があるがlast valid responseからの時間が閾値未満 | 0 |
| LAGGING | 新着がありlast valid responseが閾値以上古い、または未取得 | 1 |
| GENERATION_CHANGE | 保存generationと不一致 | 1 |
| OBSERVER_STOPPED | stateがERROR/NEEDS_RESYNC | 1 |
| EMPTY_OR_REAPED | latest messageが得られず判別不能 | 0 |

閾値は `--stall-seconds`（default 30）です。watchdogの定期起動と通知は外部supervisorの責任であり、このrepositoryからシステム設定や通知先へ書き込みません。quiet Roomでは最新seq比較だけで停止を検知できないため、heartbeatの時刻とprocess終了も監視してください。quiet/reapedの完全な区別はできません。`/rooms` は使いません。

`check-config` または `run --check-config` で明示的に `/config` を確認します。versionが0.12.1以外ならVERSION_CHANGED eventとJSON警告を出力します。read仕様は自動変更しません。`retention_seconds`は公開された場合に診断値として保持しますが、gap deadlineには使いません。通常roomはbyte compaction/idle reapで保持を失い得て、messageの7日TTLではありません。新規gapのdeadline hintは常にNULLです。

liveのdeployment値は `/config` の `settings` objectから読みます。`rate_read` / `rate_write` はIPごとの毎分read/write上限へ対応づけ、`max_wait` は `max_wait_seconds` として扱います。これはwait parameterの上限であり、default long-poll時間とは区別します。取得元、未取得field、不正な設定型を結果に含めます。2026-09-06のresponseでは `retention_seconds`、`room_ring_bytes`、defaultの `long_poll_seconds` は未取得でした。仕様の参考値で補完せず、gap deadline hintもNULLとします。

## 保存と容量

parse後のnormalized JSON recordを既存列名 `raw_record_json` に保持します。byte-exact rawではなく、空白・escape・数値表記は変わり得ます。成功responseのwire bytesは保存せず、Signerの署名用原文には使えません。`text_value` は正常なUnicode stringのdecoded本文を保存します。`ts_value` と `from_value` はJSON表現で、field不存在はSQL NULLです。異常なtext型や未対Unicode surrogateはnormalized JSON側に保持し、後者はINVALID_UNICODE_TEXTを付けます。text hashは通常UTF-8、未対surrogateではsurrogatepass encodingです。

liveで確認したcontent型はts/text/from/sigがstring、nonceがintegerです。nonceのboolはintegerとして扱いません。数値tsやstring nonceなどの型変化はvalidation_flagsを付け、raw recordを保持します。nonce/sigのfield不存在はflagにしません。JSONの重複key・非有限数・不正UTF-8は曖昧な復元を避けprotocol anomalyにします。

v2のevidence本文は最大16 KiB prefixですが、SHA-256は取得済みbody全体を対象にします。evidence_metadataにreceived_bytes、HTTP_PREFIX_ONLY/COMPLETE_RECEIVED_BODY、epoch、Retry-After等を保存します。4 MiB超過で取得できなかった部分のhashを主張しません。metadataのないv1 evidenceは旧仕様の保存bytes hashのままです。raw bodyをstdout/stderrへ出しません。

eventsとevidenceは総予算内で追記し、到達時は次のGETより前にSTORAGE_BUDGET_EXHAUSTEDで非0終了します。自動削除・rotation・backupはありません。Production defaultはevidence本文合計128 MiB/65,536行、events 262,144行/各4,096 bytes、SQLite DB 10 GiB、transaction開始時WAL 128 MiB、disk空き最低10 GiBです。DBの10 GiBは`PRAGMA max_page_count`にも設定するhard ceilingであり、filesystem上の予約容量ではありません。disk空き10 GiBも予約領域ではなく停止判定の下限です。WAL閾値はin-flight writeやsidecarを含むhard filesystem quotaではありません。[元のlocal容量方針](pre-vps-p1-remediation.md)はhistoricalです。容量停止後はHumanが保全と容量変更を判断し、resyncやrestartで予算をresetしないでください。disk free、DB/WAL・外部logを監視し、実diskの空き・backup容量を確認します。live DBをcpでbackupせずSQLite backup APIまたはVACUUM INTOを使います。

### 容量の観測・予測可能な停止（NEW-1）

heartbeatの`storage_budget.metrics`は`evidence_rows`、`evidence_bytes`、`event_rows`、`db_bytes`、`wal_bytes`それぞれの`used` / `limit` / `remaining` / `utilization`を返します。remainingは0以上、utilizationは0〜1にclampしません。DB bytesは同じSQLite snapshotのpage_count × page_size（WAL内のDB pagesを含む論理割当量）、WAL bytesはfilesystemの採時値です。同時のfileサイズとの完全一致は保証しません。`db_reusable_bytes`はfreelist pagesの量で、retention後はファイルが縮まなくても再利用できます。disk free / 最低空き / headroomと、次のevidence prefixに必要な16 KiBも公開します。raw content、message body、HTTP header、secretはこの集計に含めません。

いずれかの使用率が**80%以上**で`storage_budget.state=WARNING`になります。残り20%をoperatorの保全・review余地とするlocal警告で、最悪responseの保存保証や残り運転時間の保証ではありません。警告は読取り結果だけでeventを増やさず、正常なcursor transactionを妨げません。evidence/event件数の上限到達、最大prefixの残量不足、DBの割当上限（freelist再利用分を考慮）、WAL上限超過、disk最低空き未満は`EXHAUSTED`です。DB/WAL/diskの事前検査を通ってもin-flight transactionが`SQLITE_FULL`となり得ます。

永続`status`を必ずERRORへ変更できるreserveは実装しません。FULL時にはfailure evidenceも書けない場合があります。観測可能なbudget exhaustionではheartbeatに`effective_status=ERROR` / `human_review_required=true`を併記し、永続`status`との違いを明示します。実行中の容量停止はexit **1**、stderr JSONは`error=STORAGE_BUDGET_EXHAUSTED`、SQLite障害は`error=DATABASE_FAILURE`と`sqlite_code=SQLITE_FULL`等、いずれも`human_review_required=true`です。supervisorは非0終了・stderr・事前heartbeatを合わせて監視し、自動restart loopを止めます。DBを読めない障害や、単一transactionのallocation失敗後のheartbeatが必ずEXHAUSTEDになるとは保証しません。

**heartbeatはpredictive capacity indicator、実SQLITE_FULLはauthoritative write failureです。** capacity stateは観測時点のutilization / warning indicatorであり、次の任意サイズtransactionが必ず成功する保証ではありません。SQLite 3.45.1の実fixtureでは、page_size=4,096、割当48 pages（196,608 bytes）を上限とし、freelistに16,384 bytesがある状態でWARNINGでした。約1 MiBのrecord保存は再利用可能量を超え、SQLITE_FULLでtransaction全体がrollbackしました。rollback後にも空きpageは残るためWARNINGが続きます。これをEXHAUSTEDへ合わせるためのcapacity計算変更は行いません。

operator/supervisorは直前または停止後のheartbeatがWARNING/OK、永続statusがINITIALIZED/RUNNING/DEGRADEDでも、write failureによるexit 1を**停止・Human review必須**として優先します。heartbeat commandのexit 0はsnapshotの読取り成功を意味し、workerの稼働・健康を示しません。単独の`effective_status`や`human_review_required=false`から既知のSQLITE_FULLを解除せず、process終了理由をreview完了まで保持してください。自動restartはせず、以下の保全・plan・承認・整理・integrity確認を経てHumanが再開を判断します。DBを必ずERRORへ書き換えられるという保証もありません。

### サポートする容量停止後の復旧（offline / Human approval必須）

1. Observerと自動restartを停止し、同じ専用UID・state directoryで作業します。各maintenance commandは既存flockを取得するため、workerが稼働中なら拒否します。restartまで停止を維持してください。
2. 下記`retention-backup`を明示実行します。SQLite backup APIでstate directory内の新規0600ファイルへ保全し、schema/sequence/FKと`integrity_check=ok`、全tableの内容hash一致、file/directory fsyncを確認します。backupファイル名は`review-`＋英小文字/数字/ハイフン＋`.sqlite`のみで、上書き・symlink・外部path・sidecar付きbackupは拒否します。backup失敗時は部分ファイルも自動削除しません。
3. retention対象期限をHumanがUnix秒で指定して`retention-plan`を実行します。planには削除対象evidence/eventsの件数、ID最小/最大、時刻最小/最大、関連metadata件数、保護table件数、snapshot hashを表示します。ID範囲内全件の削除という意味ではなく、期限より厳密に古いものだけです。INIT_ANCHORと残すeventが参照するevidenceは対象外です。bodyは表示しません。planはDBを更新しません。
4. Humanがbackup・保持方針・削除件数/範囲を確認し、そのplanのtokenを同一引数の`retention-apply`へ明示入力します。tokenは承認対象hashで、認証や署名ではありません。plan→applyの自動連結は禁止です。backupが現在の全table内容と一致しない、token/cutoffが変わった、backupが破損した場合はfail-closedです。
5. applyは1 transactionで古いevents→対象evidence_metadata→対象evidenceの順に整理し、COMMIT前にschema/sequence/FKとintegrityを検証します。途中障害ではrollbackします。messages（inbox）/ gaps / state（cursor・epoch）/ epoch_historyにはDELETE/UPDATEしません。backupも保存したままです。
6. `heartbeat`でbudgetとstatusを再確認します。整理はvacuumせず、freelistを再利用します。ERROR/NEEDS_RESYNCを解除しません。容量に余裕が戻り、必要なHuman reviewが終わってからrestartします。terminal gap ERRORなら別の既存resync-plan/resyncを承認し、旧gap/historyを保存したまま新epochにします。

既存のObserver起動環境で使う例です。`CUTOFF_UNIX_SECONDS`とtokenはHumanが選ぶ値に置き換えます。今回これらを本番stateへ実行したという意味ではありません。

```sh
python3 -B -m technocore_observer retention-backup --room example --state-dir /state --backup-name review-20260907.sqlite
python3 -B -m technocore_observer retention-plan --room example --state-dir /state --backup-name review-20260907.sqlite --before-unix CUTOFF_UNIX_SECONDS
python3 -B -m technocore_observer retention-apply --room example --state-dir /state --backup-name review-20260907.sqlite --before-unix CUTOFF_UNIX_SECONDS --approval TOKEN_FROM_REVIEW
python3 -B -m technocore_observer heartbeat --room example --state-dir /state
```

backupにはDB全体分＋最低10 GiBの空き、applyには最低10 GiBの空きが必要です。空き不足、保持対象messages/history等だけで上限となった場合、整理対象なし、整理しても残量不足、DB破損の場合は停止を維持し、保全領域・容量変更・restoreを別途Humanが設計/承認します。通常復旧に直接SQLは要求しません。commandで容量変更・保護table削除を代行しません。backupの退避・保存期限・production restore scheduleはVPS gateで決定し、backupの自動削除・外部exportはありません。

起動時はquick_checkとschema検査に加え、messages/gapsを走査してcursorとの整合性を確認します。走査時間はDB件数とともに増えるため、長期soakで起動・watchdog実行時間も測ってください。wall clockの変更は時刻ベースの警告に影響しますが、seq transactionの整合性には使っていません。

## live smoke・soak・移行gate

承認済みの隔離環境からのみ、小数のrequestで実施してください。stress test、意図的な429、write requestは対象外です。

1. `/config` のversionとdeployment値を記録する。
2. normal Roomでgeneration、limit=200、cache headers、empty long-poll、wait_heldを確認する。
3. 独立した診断用readでlimit=201のclampと、valid nonexistent Roomの全envelope fieldを確認する。通常Observer clientはlimit=201を生成できないため、この診断は別途レビューする。
4. 隔離環境で連続運転し、memory、DB/WAL/log growth、disk free、gap count、HTTP failures、wait_held=falseの割合を記録する。期間と合格閾値は実施前にHumanと決める。
5. Claude/Fableのコード監査、指摘への対応、Human Approvalを経てVPS移行する。

live結果が提示された仕様と異なる場合、read契約の変更を推測して追従せず、Evidenceをもとに仕様を見直してください。
