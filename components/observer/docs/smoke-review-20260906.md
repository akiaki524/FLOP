# Live smoke review — 2026-09-06

対象はObserver Specification v0.3 / Technocore **0.12.1**。
最新の修正後smoke `smoke-results-20260906-125741-7d70a083` もoffline/live PASSで、保存証跡の再検証に合格した。config/contentの誤警告は解消している。詳細は末尾の「修正後live smokeの確認」を参照。以下の最初の試験とDocker接続失敗は履歴として残す。

証跡は `docs/smoke-results-20260906-085129-5ac4541a/`。ユーザーが通常Ubuntu terminalで実行した結果はoffline PASS、live PASS、6 GET、COMPLETEだった。保存report、response hash、SQLiteを確認した結果、sequence/generation/gapやHTTP契約に停止を要する不一致は見つからなかった。ただし実装のconfig参照位置とcontent期待型に2点の誤りがあり、修正・offline再検証を完了した。

元のreport、response body、SQLite、source archiveは変更していない。以下の初回reviewにおける修正後評価は元のlive PASSと区別する。この初回reviewでは追加live requestは行っていない。

## 保存された実測結果

| 項目 | 観測・判定 |
| --- | --- |
| Container | offline/liveの設定検査・process検査は全項目PASS。非root、非privileged、capabilityなし、root read-only、secret/host HOME/control socketのmountなし |
| Offline Observer | anchor=100、poll=201、resolved=100、OPEN gap=1、DEGRADED。実Observerのtransactionを通過 |
| HTTP | 6回とも200 JSON。許可した2つの診断Room readと/configのみ |
| Version | 0.12.1でbaseline一致 |
| Main Room | mb-047f3d88ef38、generation=2、anchor=725 |
| limit=200 / 201 | 両方200件、seq=526..725、同一body hash。内部holeなし。clampと整合 |
| Empty long-poll | 約10.34秒、count=0、first_seq=null、last_seq=725、generation=2、wait_held=true |
| 保存cursor | poll=resolved=725、RUNNING、gap/message数0、failure/anomaly数0。anchor除外とempty pollのためmessage数0は正常 |
| SQLite | quick_check=ok、application_id=1413697346、user_version=1 |
| 診断用empty Room | count=0、first_seq=null、last_seq=0、generation=0、messages=[]、wait_heldなし |
| Cache | /configはHIT、Age=290、s-maxage=300。tail/historyはMISS、long-pollはno-store/BYPASS |

6 bodyのSHA-256はreportの記録と一致した。修正前のsourceと保存archive/hashの一致も確認した。SQLiteはread-onlyで検査した。raw message本文はterminalやreviewへ転記していない。

## 1. Configの参照位置を修正

元の診断はdeployment値をtop-levelから探したためnullを報告した。実際のresponseでは `settings` object内にある。Observerのcheck-configとsmoke診断で同じ正規化処理を使い、取得元・未取得field・型異常flagを返すよう修正した。

| 取得元 | 正規化した値 |
| --- | --- |
| settings.stillborn_seconds | stillborn_seconds=43200 |
| settings.max_waiters_total | max_waiters_total=64 |
| settings.max_waiters_per_ip | max_waiters_per_ip=4 |
| settings.static_cache_seconds | static_cache_seconds=300 |
| settings.rate_read | reads_per_minute_per_ip=600 |
| settings.rate_write | writes_per_minute_per_ip=300 |
| settings.ephemeral_ttl_seconds | ephemeral_ttl_seconds=900 |
| settings.max_wait | max_wait_seconds=10 |

`max_wait` はrequest waitの上限でありdefault時間ではない。`long_poll_seconds`、`retention_seconds`、`room_ring_bytes` はこのresponseから未取得で、仕様中の参考値を埋め込まない。retention未取得時のgap deadline hintはNULLとする。configはcacheされた観測値であり、瞬間的なdeployment状態の保証ではない。取得可能な値についてbaselineとの不一致や型異常flagはなかった。

## 2. Content型の誤警告を修正

保存した200件はすべてtsがstring、nonceがintegerだった。以前の実装はtsをnumber、nonceをstringと仮定し、正常recordにもINVALID_TS_TYPE / NON_STRING_NONCEを付けていた。期待型を実測に合わせ、nonceの型異常はNON_INTEGER_NONCEとした。boolはintegerに含めない。

raw recordの保持方針は維持する。数値ts、string nonce、field欠落、unknown fieldなどはflagsを付けて保存し、ordering validationを変更しない。

保存済みresponseのSHA-256を確認し、実Observerを使って200件をrepo内の一時SQLiteへoffline replayした。synthetic anchor=525、generation=2からseq=526..725を保存し、raw record objectとdecoded textが元の200件と一致、全件flagsなし、poll=resolved=725、OPEN gap=0を確認した。これは通常取得・保存処理のoffline検証であり、元のlive DBを変更したものでも、liveで新着200件を受信した結果でもない。

修正後は追加compatibility試験7件、smoke helper試験11件、既存Observer試験23件の計41件が成功した。既存P0 testファイルとsequence/generation/gap、5xx/503/backoff処理は変更していない。

## 初回review時点の残る確認範囲と次段階

- limit=201の200件応答はcapと整合するが、保持件数が200件超であったことは独立には証明していない。
- wait_held=falseとliveでの新着保存は今回未観測。前者は既存offline試験、後者は保存responseのreplayで検証した。waiter枯渇、write、追加大量requestで状況を作らない。
- 診断Roomのempty応答は観測できたが、過去を含めた不存在やquiet/reapedの区別は保証できない。
- 隔離PASSの対象は固定base image＋source zip＋tmpfsのsmoke構成。bridgeはdomain単位のfirewallではなく、HTTP origin/pathはコードで制限する。deployment image・永続volume・長期容量・電源断耐久性は別の検証対象。
- 修正後のコードはoffline再検証済み。元のlive source archiveは修正前であり、修正後のlive実行済みとはしない。

次段階は隔離環境でのlocal soak準備。memory、DB/WAL/log増加、disk free、gap、HTTP failure、wait_held=false率、独立watchdogとのseq/generation比較を記録できる実行手順と終了条件を整える。今回soakは開始していない。このCodex sessionはDocker socketへ接続できないため、containerでの実行にはDocker Desktopへ接続できる通常Ubuntu terminalが必要になる。権限変更やsecret mountで回避しない。

soak、deployment構成の検証、Claude/Fableの独立コード監査、Human Approvalは未完了であり、VPS移行gateは未達。

## 修正後live smokeの実行試行（2026-09-06）

ユーザーの再実行指示により、`python3 -B tests/run_container_smoke.py --execute` を実行した。最初のDocker呼び出しである固定local imageのinspectでCalledProcessErrorとなり、STOPPEDで終了した。sandbox外で1回だけ再試行したが同じ結果だった。rawなsubprocess stderrはhelperが表示しないため、この試行の出力だけからDocker側の詳細原因は確定しない。以前確認した、このセッションからDocker socketへ接続できない制約は未解消として扱う。

| 試行 | 保存先 | 結果 |
| --- | --- | --- |
| 通常sandbox | docs/smoke-results-20260906-124839-9c3060c9/ | STOPPED / CalledProcessError |
| sandbox外の1回の再試行 | docs/smoke-results-20260906-124929-a76acc0d/ | STOPPED / CalledProcessError |

保存先には実行計画、source archive/hash、空の専用Docker client設定directoryがある。container作成前に停止したためoffline/live reportはなく、両試行ともTechnocore request数は0。修正後live PASSとは判定しない。これ以上の自動再試行、権限変更、追加network access、soak開始は行わない。

この時点では、Docker Desktopへ接続できる通常Ubuntu terminalで同じcommandを実行する必要があった。承認の追加ではなく実行環境上の制約による引き継ぎだった。その後の実行・証跡確認は次節に記録する。

soak開始前には、修正後live結果の確認に加えて、運転時間・正常poll頻度・停止条件の具体化、memory/DB/WAL/log/diskとwait_held=false率の記録、独立watchdogの実行方法、使用する保存領域と終了時の証跡回収を用意する必要がある。smokeの64 MiB tmpfsをそのまま長期運用のdurable storageとして扱わない。deployment imageや永続volumeを採用する場合は、その構成でも隔離・所有権・modeを検証する。

## 修正後live smokeの確認（2026-09-06 12:57 JST開始）

証跡: `docs/smoke-results-20260906-125741-7d70a083/`。通常Ubuntu terminalでoffline PASS、live PASS、6 GET、COMPLETEと報告された。保存reportでも両方PASS、offline外部request=0、live=6を確認した。

| 項目 | 修正後の確認結果 |
| --- | --- |
| Source | archiveの8ファイルすべてがsource-hashes.jsonおよび現在のsourceと一致 |
| Body | response 6件すべてのbyte数・SHA-256がreportと一致、truncated=false |
| 隔離 | offline/liveのcontainer設定検査・process検査がすべてtrue |
| Network計画 | 固定origin、GET、path/query、最大6件、pull/buildなし、proxy/redirect無効を確認。request間の休止は記録精度内で最低2秒 |
| Version/config | 0.12.1一致。settingsから8値を取得、config_validation_flags=[]。取得元と未取得fieldもbodyの再解析と一致 |
| Room/sequence | mb-047f3d88ef38、generation=2、anchor=749。limit=200/201はともにseq=550..749の200件、同一body hash、内部holeなし |
| Empty long-poll | 約10.286秒、count=0、first_seq=null、last_seq=749、wait_held=true、generation=2 |
| SQLite | read-onlyのschema・quick_check・foreign key・sequence accounting検査に合格。poll=resolved=749、RUNNING、message/gap=0、failure/anomaly=0 |
| Events/evidence | state transition 1件、init evidence 1件。generation変更・protocol anomaly・version drift eventなし |
| Content | 保存response内のcontent flagは0件。さらに200件を実Observerでoffline replayし、raw record object・decoded textの一致、flagsなし、poll=resolved=749、gap=0を確認 |
| Artifact権限 | 結果directoryとoffline/live directoryは0700、offline/liveの証跡ファイルは0600 |
| Cache/empty診断 | /configはHIT、Age=274、s-maxage=300。room tail/historyはMISS、long-pollはno-store/BYPASS。診断Roomはcount=0、first_seq=null、last_seq=0、generation=0、messages=[] |

再解析とoffline replayは追加network requestなしで行い、元の証跡を変更していない。replayのanchor=549はsyntheticな検証用で、live initのanchor=749とは区別する。live DBのmessage数0はempty pollとanchor非保存による正常な結果。今回のsmokeはconfigを診断clientで取得するため、DBへVERSION_CONFIRMED eventを記録するcheck-config commandのlive試験を兼ねてはいない。

**v0.3 / 0.12.1への不一致や、前回の2点に起因する警告は残っていない。** 次の制限は解消済みとしない。

- room_ring_bytes、retention_seconds、defaultのlong_poll_secondsは未取得。max_wait=10をdefault時間へ読み替えず、retention hintはNULLとする。
- limit=201の200件応答はclampと整合するが、保持件数が200件超であったことの独立証明はない。
- wait_held=falseと新着のlive Inbox保存は未観測。empty/reapedの完全な区別もできない。
- 同じsourceと隔離構成のsmoke合格であり、長時間運転・deployment image・永続volume・電源断耐久性の合格ではない。

soak計画作成へ進める。開始前の準備・上限・合否基準は [soak plan](soak-plan.md) にまとめた。soakは未開始。
