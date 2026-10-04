# Persistent Storage Durability — offline検証計画

状態（2026-09-07）: **Local Persistent Storage Durability gateは完了。** host processの31ケースと実Docker named-volume recreationを組み合わせて確認し、Local結果は `LOCAL_DURABILITY_PASS_WITH_HARDENING_WARNINGS`。`production_hardening_gate=PENDING` を維持する。Observer Specification v0.3 / Technocore 0.12.1を基準とする。[実装・検証記録](durability-verification.md)、[final regression](final-regression-20260907.json)、[VPS移行前checklist](vps-migration-checklist.md)を参照。

## 目的と判定範囲

同じpersistent stateを再利用したprocess/container restart後に、SQLite state・messages・cursor・OPEN gapのaccountingが保持され、保存済みpoll_seqから安全に再開できることを確認する。成功したCOMMITの保持と、未COMMIT transactionのrollbackを区別する。

この計画は完全offlineとし、外部GET/POSTは0。FakeClientが小さい固定responseを返す。実Observer、structural validation、Store、StateLock、SQLite transactionを通し、取得・sequence・generation・gap・5xx処理は変更しない。稼働中のRoomや既存smoke/soakのDBを試験stateに流用しない。

Windows/WSL/Docker Desktop自体の停止、VHD損傷、物理電源断、filesystem/deviceのfsync違反を再現するものではない。これらを含む耐久性は、このprocess/container再起動試験のPASSから推定しない。

## 保存構成の候補と開始前確認

候補は**試験専用のDocker local named volumeを1個だけ `/state` にmount**する構成。host HOMEやrepo全体のbind mountは使わない。restartとcontainer再作成は同じvolumeで行い、volume名・driver・label・試験識別子を照合する。

| 項目 | 条件 |
| --- | --- |
| Runtime | UID/GID 65532:65532、non-privileged、cap-drop=ALL、no-new-privileges、read-only root、seccomp、memory=256 MiB、pids-limit=32、restart=no |
| Network | 全containerでnone。fixtureに実HTTP clientへのfallbackを設けない |
| State | `/state` とinbox directoryは0700・所有者65532、DB/lock/sidecarは適切なprivate permissions。SQLite WAL/FULL/foreign_keysを読戻す |
| Mount allowlist | 指定したlocal named volume → `/state` の1件だけ。別volume、bind、VolumesFrom、devices、control socket、host namespace、secretなし |
| Volume driver | local、外部storage/pluginなし。host pathを指すdriver optionsやNFS等を禁止し、inspectで確認 |
| Image | 既存の固定base digestから用意する専用offline試験imageをlocal IDで固定。source/fixture hashを記録し、codeはread-only image側に配置 |
| 初期所有権 | image内の `/state` を65532:65532・0700で準備し、空volumeへの初期配置で期待する所有権を得られるか実測する |
| 禁止する補修 | root container、sudo、host側chown/chmod、chmod 777、権限拡大で所有権不一致を回避しない |

既存Containerfileにはimage内 `/state` の所有権を設定する処理がある。ただし、named volume初期化後の所有権・modeまで確認済みとは扱わない。最初の非root probeで書き込み、stat、flock、SQLite作成を確認し、不一致ならinit前にfail-closedする。

**既存soakとの構成差分は `/state` のtmpfsからnamed volumeへの置換。** 承認済みのoffline recreation helperだけでfresh volumeを使用する。既存の`only_state_tmpfs`検査を汎用的に緩めず、durability用の検査でこの1 volumeだけを許可する。

### Local Docker Desktop durability validation

Human decisionにより、`run_container_recreation.py` は明示的な `local-docker-desktop` profileを使用する。Docker-managed named volumeを維持し、host bind mountは追加しない。`state_mount_noexec` / `state_mount_nosuid` / `state_mount_nodev` のfalseだけをhardening warningとし、観測値をそのままartifactに保存する。その他の11 process checksと21 container configuration checksはすべてhard-fail。欠落・未知・非booleanのprobe項目も拒否する。containerとhostの双方が同じpolicyで判定し、一致しなければfixture init前に停止する。

`/state`だけがpersistent writableであること、rootfs read-only、UID/GID 65532、cap-drop ALL、no-new-privileges、seccomp、network isolation、host HOME/keys/socket非公開、bindなし、未知label/mount拒否を維持する。汎用 `run_durability.py` とpayloadの既定profileは `strict` のままで、3属性も必須とする。

reportは `isolation_boundary`、`persistent_storage_durability`、`mount_hardening_noexec/nosuid/nodev` を分離する。A/B両方の構成検査・probe・driver readyとrecreation fixtureの成功を確認してからdurabilityをPASSとする。3属性不足を伴う成功は `LOCAL_DURABILITY_PASS_WITH_HARDENING_WARNINGS`、不足なしは `LOCAL_DURABILITY_PASS`。停止時の全体statusは `STOPPED`。helper単体のhistorical `full_durability_gate=PENDING` は元artifactに保持する。host process matrixと実Docker recreationを統合したreviewで `local_persistent_storage_durability_gate=COMPLETE` とした。`production_hardening_gate` は独立してPENDING。

### VPS production hardening — 別途security gate

Localのwarning許容はproduction security requirementの永久的な緩和ではない。VPSでnoexec/nosuid/nodevを実環境で再評価・実測する。将来の候補は `/srv/technocore-observer/state` のようなObserver専用host state pathだけの限定bind mount。ownership、directory 0700 / DB 0600、mount options、backup/recoveryとの整合性を別途設計する。今回はVPS用bind mountを実装しない。

`/home`、DID/Signer/seed/private key領域、`~/.local/share/flop`、Docker socket、host root filesystemはmountしない。host/Docker設定変更、新たなbind mount、外部通信/image pull、その他のsecurity boundary拡張は今回のLocal profileでは許可されない。

## 試験driverと再開契約

現soak Workerは新規試験用のexplicit initに特化しており、persistent resume試験へそのまま使わない。次の段階でoffline専用の小さいdriverを用意する。production ObserverやCLIへ自動bootstrap/resync機能を追加しない。

driverは初回の`init fixture`と、既存stateだけを開く`resume fixture`を分離する。resumeでは次を必須にする。

1. 同じvolume内のstate directoryに対してStateLockを取得する。
2. Storeで既存DBを開き、application_id、user_version、schema、quick_check、foreign keys、sequence accountingを検証する。
3. statusがINITIALIZED/RUNNING/DEGRADEDの場合だけ既存Observerでpollする。
4. fixture側で最初の`poll(since)`が保存済みpoll_seqと一致することをassertする。resume中のtail/init呼び出しはfixture側でも拒否する。
5. 同じroom・observer_epoch・server_generation・init_anchor_seqを保持し、再開だけを理由にepochを増加させない。

既存のcheckpoint callbackで停止点に到達したことをhostへ通知し、hostが対象process/containerだけを停止する。環境変数やmessage本文で任意command/停止点を指定できるproduction機能は追加しない。

## 基本fixtureと期待するstate

Room名は固定、generation=2、observer_epoch=1とする。textは小さい固定stringで、制御文字も含めてraw/text/hash保持を確認する。各scenarioは新規の試験専用stateから開始する。

| 段階 | 保存・応答 | 期待値 |
| --- | --- | --- |
| 初回explicit init | anchor=100 | poll=resolved=100、INITIALIZED、Inboxにanchorなし |
| Contiguous取得 | 101..103 | poll=resolved=103、RUNNING、messages=3 |
| Gap取得 | 200..201 | gap=104..199 OPEN、poll=201、resolved=103、DEGRADED、messages=5 |
| 再起動直後・poll前 | 既存stateを開く | 上記state/messages/gapがそのまま。init/tailなし |
| 再開後 | since=201に対して202..203 | poll=203、resolved=103、同じOPEN gap、messages=7 |
| さらにgap | since=203に対して300..301 | gap=204..299もOPEN、poll=301、resolved=103、messages=9 |

比較対象は行単位の論理内容とrecord hash。SQLite file全体のbyte一致は要求しない。WAL recovery/checkpointやbackupでfile配置が変わっても、論理stateとmessageの保持を検証できるようにする。保存済みmessageのingested_at/raw/text/flags/hashが再起動で書き換わらないことも確認する。

## 再起動・故障のmatrix

| ケース | 操作・確認 |
| --- | --- |
| Process正常終了 | process AがCOMMIT後にclose。別process Bが同じstateを開いてresume |
| Process SIGTERM | idle時とfixture受信待ち時に停止。受信中transactionの終了/rollbackを確認して別processでresume |
| Process SIGKILL | 既存checkpointのhttp_received、message_inserted、gap_inserted、before_cursor_update、before_commit、after_commitで停止 |
| Container停止/起動 | 同一containerをstop/start。stateを再initせず、同じvolumeからresume |
| Container強制停止 | 対象containerだけを停止点でkill。再起動時にWAL recoveryを含めて検証 |
| Container再作成 | Aを停止・削除し、同じimage IDと同じnamed volumeでBを作成。snapshotからrestoreせず元volumeそのものを再利用 |
| 二重起動 | Aがlock保持中にBが同じvolumeでwriter起動を試み、Bだけ即fail。Aのstateと取得を壊さない |
| Terminal state | generation変更でNEEDS_RESYNCにしたfixture、およびprotocol異常3回でERRORにしたfixtureを再起動。stateを保持し、poll/tailを行わず拒否 |
| State fail-closed | DB不存在、別volume、room不一致、application_id/user_version異常、破損、owner/mode不一致を各々別のfixture stateで確認。自動tail-startしない |
| WAL正常運用 | SIGKILL後の未checkpoint WALを保持してresume。正常close/checkpoint後にWALが存在しないcaseも許容する。WALを手作業で削除しない |
| Init中断 | 既存の6 init checkpointで停止。rename前なら不完全final DBを使わず、stale tempを残して再init拒否。rename後なら完成stateを検証 |

COMMIT前のkillではそのresponseのmessage/gap/cursorがすべてrollbackされ、次回は同じsinceで再取得できること。after_commitのkillではすべて保持され、次回sinceが進んでいること。prefix gapの有無を両方含める。OPEN gapを跨いでresolved_seqを進める結果は即不合格。

container削除時はvolumeを残し、`-v`、prune、volume再作成で結果を隠さない。意図的な破損・権限異常は独立した試験stateだけに作る。正常stateや過去のsmoke/soak証跡を変更しない。

## 段階・上限・証跡

最初にrepo内のprivate directoryを使い、通常processでdriverのfixture、barrier、resume、比較器をoffline検証する。これはhost filesystemでのprocess試験であり、Docker volumeの証明とはしない。その後、named volumeの隔離・所有権preflight、container matrixの順に進める。

外部通信0、並列負荷なし。基本fixtureは9 messagesで、fault fixtureも20 messages以下・本文1 KiB以下とする。停止点の通知は10秒、Docker操作は各30秒、suiteは最大15分で打ち切る。timeoutを成功として扱わず、無制限retryしない。disk容量の実消費を増やすFULL/load試験は今回行わない。

結果はrepo内の新規 `docs/durability-results-<識別子>/`（0700）へ保存する。source/image/volume識別、検査済みmountとUID/mode、各停止点の通知、process終了code/signal、再起動前後のstate、messages/gapsの件数・hash、最初のsince、各不変条件の合否を記録する。raw本文はterminalへ出さずprivate SQLiteで保持する。

証跡snapshotはSQLite backup APIまたはVACUUM INTOで作成し、0600で回収してhash・quick_check・foreign keys・accountingを検証する。**backupからのrestoreを、元volumeが保持されたことの証拠として使わない。** live DBをcpしない。停止直後の未checkpoint stateを観測する前に、証跡取得のためのconnectionがrecovery/checkpointを起こさないよう、fileの存在/size等と初回openの順序を記録する。

失敗時はそのcaseを停止してvolumeと証跡を保全し、成功として先へ進めない。cleanupは対象識別を照合した試験container/imageだけとし、volumeの削除は別途扱う。無関係なcontainer、他project、Docker Desktop設定に触れない。

## 合格条件

- 成功したCOMMITのmessages/cursor/gapsがprocess・container restartおよびcontainer再作成後も保持される。
- COMMIT前の停止では部分保存やcursor先行がなく、保存済みsinceから安全に再取得できる。
- OPEN gap、resolved_seq、generation/epoch/anchor、raw recordとcontent metadataが再起動で変質しない。
- ERROR/NEEDS_RESYNCや不正stateは再起動後もfail-closed。単一writer lockが共有volume上でも機能する。
- 新規取得前の検証、SQLite設定、volume同一性・権限・隔離検査、証跡回収がすべて成功する。

全caseを満たした場合に、この限定されたPersistent Storage Durability検証をPASSとする。現時点では**offline process部分はPASS、Docker部分は未実施、全体gateは未達**。以前のtmpfs soakはWARNのまま保持し、本計画で置き換えたりPASSへ変更したりしない。
