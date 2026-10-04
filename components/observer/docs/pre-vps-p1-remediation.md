# Pre-VPS P1: remediation and historical reproduction

## 現在の対応

ユーザー提示の公式v0.12.1 source確認（commit `2670ebb11ead1ab9b67307e518f8e8479fc9db47`）でP1-2を確定し、P1-1/3/4/5を実装した。[採用したread契約](technocore-v0121-read-contract.md)と[最終結果artifact](pre-vps-p1-final.json)を参照。Technocore GET/write、Docker socket access、VPS deploymentはいずれも0。

| Finding | 根本原因と修正 | 追加検証 |
| --- | --- | --- |
| P1-2 since | newest-Nの選択をforward paginationとみなしていた。gap eventにUNOBSERVED_NORMAL_READとphysical_loss=UNKNOWNを保存。TTLからのdeadline算出を停止 | 250件を保持したfixtureから最新200件を独立モデルで選択し、未取得50件が物理的には存在する例 |
| P1-1 forward jump | transaction内で欠落幅を検査せず巨大gapをcommitしていた。未取得10,000件超のprefixをFORWARD_JUMP_REQUIRES_RESYNCとして即ERROR停止。通常message/gap/cursorはrollback。旧seqもtransaction内で再検査 | 2^63-1攻撃、正常pollの抑止、reopen、閾値境界、巨大な絶対seqからの連続page、stale save拒否 |
| P1-3 resync | 単一epochの全行照合とOPEN gapによってresolvedが永久固定。明示resync-plan/resync、旧state snapshotと承認decisionの追記、epoch増分、epoch別整合性検証を追加。v1は明示migration | 旧messages/gaps/evidence/eventsの不変、同generation/新generation、seq重複、stale/変更/欠落approval拒否、CLI networkなし、COMMIT前後の実SIGKILL、実SQLITE_FULL、migration rollback |
| P1-4 storage | failureごとの無制限追記。16 KiB body prefix、取得body hashとscope/status/metadataを保存。global件数・bytes・DB/WAL/disk budgetで停止する保存方針 | 429/503/network failure、件数/bytes上限、metadata上限、旧証拠非削除、resyncで予算をresetできないこと、WAL/low disk時のGET抑止、実SQLITE_FULL rollback |
| P1-5 body | 32 MiB全体をparseしてから件数検査。4 MiB HTTP cap＋decode前のJSON depth/structure budgetに変更 | 実loopback body cap、最大4096文字textの200件、非BMPのescape拡大、密なarray/dict、deep nesting、上限超過、near-cap stringを256 MiB RLIMIT_ASで検証 |

`tests/test_p1_remediation.py`は25件の新規regression。`tests/p1_memory_probe.py`は9ケースの有界memory probe（1文字の非BMPでPython string全体が4-byte幅になる保存pathも含む）。既存`test_observer.py`ではschema version検査と、公式retention契約に反していたdeadline期待値を更新した。他の既存testの期待値を弱めていない。

最初の全regressionは144件成功（新規21件）。その後、transaction内旧seq拒否とWAL/resync容量不足の3件を追加して最終regressionを実行した。結果とsource hashesは上記artifactに固定する。過去のpre-fix再現artifactは上書きしていない。

起動時check-configの容量到達時GET抑止も追加した最終sourceで **148 tests / OK / skip 0 / 22.174秒**。9ケースのstandalone memory probeは256 MiB RLIMIT_ASで成功し、Linux peak RSSは93,843,456 bytes（約89.5 MiB）。両結果の実測範囲をartifactに明記した。既存isolation/durability helper 8ファイルは修正前artifactのSHA-256と一致し、strict/local profile境界を変更していない。

### Observer本体の変更

- `protocol.py` / `http.py`: MAX_BODY=4 MiB、POLL_LIMIT=200、JSON depth=32/structure=32,768。固定origin・GET/path/query・TLS・proxy・redirect境界は同じ。
- `observer.py`: 異常なforward jumpを有限retryせず即ERRORへ。generation停止と他のprotocol anomaly 3回停止は維持。
- `storage.py`: schema v2は旧5 tableのschema/rowsを変更せず、epoch_history/evidence_metadataを追加。epoch別sequence accounting、manual resync、明示v1 migration、storage budgetを追加。
- `cli.py`: offlineのresync-plan/resync/migrate-plan/migrateを追加。planのhashは具体的な承認対象を結ぶもので、Human本人の認証や署名ではない。自動resyncはない。

### Storage policy

新規evidenceはbody prefix最大16 KiB、Content-Type/Retry-Afterは各512文字（固定の有限byte範囲）、anomaly名128文字。body_sha256は取得済みbody全体、metadataにreceived_bytesとhash_scopeを保存する。HTTP capで未取得部分がある場合はHTTP_PREFIX_ONLY。保存prefix短縮だけの場合はCOMPLETE_RECEIVED_BODY。Content-Type全文hashも保持する。network failureは取得bodyがないためeventのみ。

evidence body合計4 MiB、evidence 4,096行、events 8,192行、event JSON 4,096 ASCII bytes、DB 16 MiB（SQLite page ceiling）、transaction開始時WAL 8 MiB、空きdisk最低8 MiBを現在の保守的なlocal budgetとする。予算に達したら次のGET前にSTORAGE_BUDGET_EXHAUSTEDで非0終了。自動削除・rotation・間引き・予算resetなし。resyncも同じ予算を共有する。

WAL 8 MiBはtransaction開始時の停止閾値であり、そのtransactionが追加するbytesまで8 MiBに抑えるfilesystem quotaではない。通常autocheckpointは256 pages。長いreaderでcheckpointが進まない場合も次のtransactionで停止する。有限HTTP/page/row budgetで増加を抑え、実disk満杯ではrollbackして非0終了する。physical volume全体・SQLite sidecar・外部logに対するhard quotaと容量reserveはproduction gateで別途設計する。16 MiB DBは無期限のproduction保存容量ではない。

旧v1 evidenceはschemaもbytesも維持する。metadata行のないlegacy evidenceのhashは旧仕様どおり保存bytesに対するhash。過去の大型evidenceで既に新予算を超えているDBはmigration後もpollできない。証拠を削って起動するのではなく、Humanがbackup/保全と容量変更を判断する。容量停止時はERROR行すら書けない可能性があるため、supervisorは終了コードとbudgetエラーを監視する。

### 未解決P2/P3とreadiness

P2-3のbyte-exactでない点とP2-6のmachine-specific claimはREADME/operations/checklistで明確化した。列名変更や成功response全体の保存は行っていない。production entrypointの`-I`（P2-1）、watchdogのSQLite write permission依存（P2-2）はVPS前の確認項目として残す。その他P2-4/5/7、P3-1..4も未修正。

**P1のcode/offline remediationは完了。VPS migration readinessはNOT READY。** production imageの`-I`・isolation実測、256 MiB cgroup実測、実filesystemの容量/backup/recovery/監視、独立再review、Human Approvalは未完了。strict/local profileを変更せず、旧Docker artifactのPASSを今回のsourceへ転用しない。

## 以下は公式source提示前の調査・設計記録（現在の仕様ではない）

Status: **BLOCKED BEFORE IMPLEMENTATION — P1 5件とも未解決**。
この文書は修正完了報告ではない。ユーザー指定の「実装変更より先にsinceの実semanticsを確認」を適用し、Observer本体を変更していない。VPS deploymentも実施していない。

## 調査範囲と証跡

- [Opus監査](pre-vps-opus-audit.md)と、`src/technocore_observer/`、既存offline tests、smokeの保存済み6 responseを照合した。
- [再現artifact](pre-vps-p1-verification.json)にsource SHA-256、raw response hash照合、各再現結果を保存した。6 responseすべてreportのhashと一致した。
- [offline再現コード](../tests/pre_vps_audit_probe.py)を実行した。固定のfake reply、repo内一時SQLite、最大256 MiB address spaceの子processだけを使用する。reportは排他的作成で、既存reportを上書きしない。同じ保存先への再実行は拒否する。
- Technocore GET **0**、write **0**、Docker socket access **0**。secret探索、任意URL取得、OS設定変更なし。
- 数字はこのmachine・このsourceの観測。過去のDocker/source hash証跡を変更後のsourceの検証として転用しない。

既存baseline regression: `python3 -B -m unittest discover -s tests`。最初はsandboxのsocket禁止でHTTP setUpClassが失敗（118 tests、errors=1）。許可されたsandbox外の1回再実行では**123 tests / OK / skip 0 / 20.087秒**。Technocore通信はなくloopback HTTPだけ。P1修正後のfinal regressionではない。全treeがuntrackedのため、`git diff --check`だけでは今回の新規ファイルを検査した証拠にならない。

## P1-2: since semanticsを最初に確定する

保存済み2026-09-06のlive artifactでは、同じgeneration=2に対して次を観測した。

| request | 結果 |
| --- | --- |
| since=0, limit=200 | seq=550..749、200件、79,888 bytes |
| since=0, limit=201 | 同一body/hash |
| since=749, limit=200, wait=10 | empty |

先行smokeはseq=526..725を返したが、別時刻の観測であり、526..749が同時に保持されていた証明にはならない。

したがって「最古の保持messageから昇順に200件」と「sinceでfilterした最新200件」は両方とも観測に整合する。`since=0`のみのhistory probeなので、zeroに特別扱いがあるかどうかも分からない。保存済み`/config`は選択順、message byte limit、retention windowを公開していない。公式read仕様の本文またはupstream server実装はこのrepositoryに存在しない。

監査推奨の「保持250件以上でGETを1回」も、その保持状態の独立根拠が必要である。単に古いsinceを指定し200件が返ったことをもってPASSにしない。仮説Bの場合、sinceをさらに小さくしても同じ最新pageになる可能性があり、巻き戻しpollだけで回収できるとは断定しない。

### 最小live GET計画（未実行）

既存allowlistで実施できる候補は以下の**1 requestのみ**。通常clientのpath/query制約を変更しない。

```text
GET https://technocore.chat/r/mb-047f3d88ef38
query: since=525, limit=200, wait=10, format=json, n=<実行時の数値>
```

525は先行artifactの最初のseq=526の直前。zero特例を避ける診断値で、保持を保証する値ではない。TLS検証、proxy無効、redirect禁止、timeout=25秒、response読取上限1 MiB+1 byte、再試行なし。非200・generation不一致・構造異常・超過・通信失敗なら停止。HTTP error bodyも同じ上限とする。server側の操作はこの固定roomへのGETだけ。

保存項目はexact query、開始/終了時刻、status、Content-Type、Date/Age/cache headers、body prefixとhash、hash対象のbyte数・truncation、generation、count、first/last、判定根拠。raw本文をterminalに表示しない。実行時の保存パスは作成前に提示し、既存artifactを上書きしない。

**判定の限界:** 同じgenerationで526..725が返れば、以前観測した749より古いpageを選択した根拠になる。ただし現在の保持・個別削除の仕様を確定せずに、一般的なoldest-first契約の証明とはしない。550以降やemptyが返っても、retentionによる欠落と最新page選択を識別できない。結果が曖昧ならUNKNOWNのまま終了し、2回目を自動追加しない。1 GETで確定すると約束しない。

**実行環境の停止条件:** READMEとoperationsはlive CLIを専用userまたは隔離containerからだけ実行する契約。既存live helperはUID 65532、host HOME非公開、Docker設定inspectを要求する。今回のDocker socket禁止下ではそのhelperを使用できず、host直実行へ切り替えることは隔離条件の緩和になる。通常clientのallowlistにない`since`付き`limit=1`診断も無断で追加しない。

現状で必要なのは、**公式仕様のsince/limit選択順・zero特例・保持仕様と本文サイズ制限の該当部分**。それをユーザーから提供してもらえれば、通信や隔離条件を緩めずに実装判断を進められる。liveが依然必要なら、既存境界を満たす実行環境と識別条件が揃ってから上記計画を再評価する。

## P1-1: unbounded forward cursor jump

**Reproduce:** `poll_seq=100`から`seq=9223372036854775807`の1件が通常pollとしてcommitされた。gapは101..9223372036854775806、resolved_seq=100。その後の正当な101を3回拒否しERRORになった。監査の再現と一致。

**Root cause:** protocolはSQLite表現範囲と内部連続性を検証するが、保存cursorに対するprefix差分を制限しない。`Store.save`は差分全部をOPEN gapとして記録する。

**Minimal design（未実装）:** since契約の確認後、通常pollで無承認に許容する最大prefix欠落をObserver側の運用予算として明示する。server seqの最大値とは別物にする。許容外は専用anomaly/evidenceを保存して停止し、messages/gaps/poll_seq/resolved_seqは変更しない。検査をtransaction内の最新stateにも適用する。巨大なseqでも現在cursorに連続している場合はSQLite範囲内なら受理できる設計にする。閾値100,000をserver仕様から導出した値として扱わない。

**Tests → implementation → regression（未着手）:** 2^63-1の攻撃、閾値境界、繰返し攻撃、直後の正当page、restart、巨大な絶対seqからの連続page、evidence保存失敗を検証する。既存の小さなprefix gapとSIGKILL transaction試験も維持する。

## P1-3: manual resync / epoch

**Reproduce:** 200..201、202..203、204..205を順に保存してもresolved_seq=100。DBを開き直しても同じ。schemaはgap statusをOPENだけに固定し、全message/gapを単一epoch/generationに限定する。

**Minimal design（未実装）:** 明示的なresync commandを追加する。Humanがroom、旧epoch、旧generation、旧poll/resolved、対象gap、意図的にスキップする範囲、新anchorとその証拠hashをreviewする。実行時に承認対象のstate/hashが一致することをflock＋transaction下で再検査し、stale approvalを拒否する。

旧epochのstate snapshotと承認理由を追記し、observer_epochを1増やして新epochのanchorから追跡する。旧messages/gaps/evidenceを削除・上書きしない。旧gapはOPENの証拠として保持し、別のresync記録に「未回収のまま旧epochを終了」と明示する。lossをCLOSEDや回収済みと偽装しない。resolved_seqは**epoch内**の連続性であり、旧epochとの間の欠落が埋まった意味にはしない。Analyzerもepoch境界を消して連結できない契約にする。

schema versionを更新し、明示migration時だけ旧v1を検証・保全して移行する。各epoch別のsequence accountingとidentity検証を維持する。起動時の自動resync/migration、DB破棄、直接SQLの手修正を回復手段にしない。

**Tests → implementation → regression（未着手）:** 承認なし・stale approval拒否、同generation gapとgeneration変更のresync、epoch増分、新epochでresolved進行、旧行の完全一致、reopen、transaction途中失敗/SIGKILL/SQLITE_FULL、v1 migrationの失敗時保存を検証する。

## P1-4: evidence / events storage

**Reproduce:** 503(1 MiB)、429(1 MiB)、network failureを8組、計24回のfake pollで、evidence bodyは16,777,391 bytes、eventsは24行まで増えた。network failureもevents経由で増加する。有限サンプルの増加と、停止・容量制約がないcodeを合わせて無制限増加を確認した。

**既存integrity確認:** evidence INSERT中の実SQLITE_FULLでcursor・messages・gaps・events・evidenceは元の値へrollback。DB reopenの検証も成功した。host diskを埋めず、SQLite page quotaを用いた。`last_poll_at`は独立の試行記録なので比較対象のcursorとは別。

**Minimal design（未実装）:** 例えばbody prefix=16 KiB、metadataもfield単位でbyte上限、evidence/event総件数と総保存byte数を明示的な予算にする。本文を切っても取得済みbytes全体のSHA-256、取得byte数、保存byte数、HTTP status、anomaly、request_since、epoch、時刻、Retry-After診断は残す。HTTP読取上限超過のhashはprefix hashと明示し、未取得の全文hashを主張しない。

旧証拠の削除・上書き禁止と有限容量を両立する最小策は、**予算到達前に停止し、明示的な保全・容量判断を要求する保存方針**。metadataを無期限に追記しつつ全storageをboundedと呼ぶ設計は採らない。eventsも同じ予算で扱い、停止記録用reserveを確保する。空きdisk下限とDB/WALの増加も監視する。停止記録さえ書けない場合は非0終了し、cursorを更新しない。messagesを含めDB全体にもpage上限が必要か検討し、WALとreaderによるcheckpoint阻害を総容量から除外しない。

**Tests → implementation → regression（未着手）:** 異なる大量429/503、network/protocol failure、巨大metadata、prefixと取得hashの相違、総budget境界、restartでbudget保持、保存停止後のnetwork抑止、実SQLITE_FULLの再検証。resyncでbudgetをresetして無制限に増やせないことも検証する。

## P1-5: HTTP cap and Python expansion

**Reproduce:** body=16,400,094 bytes、40万messagesのJSONが件数検証前にdecodeされた。Linux peak RSS=141,676,544 bytes（約135 MiB）。子processのRLIMIT_AS=256 MiB内で今回のdecodeは成功した。これはcgroup memory=256 MiBの実測ではなく、監査のOOM断定を再現したものでもない。tracemallocのpeakにbody/stringを重複加算するような外挿は行わない。

**Root cause:** poll limit=200の検査がJSON全体のdecode後。body capだけでは配列・dict・小さい値のobject増加を制御しきれない。正常の実測最大79,888 bytesと32 MiB上限には約420倍の差がある。

**Minimal design（未実装）:** 公式のmessage byte/character上限、escaping、metadata余白、configured poll limit=200を確認してresponse予算を計算する。実測だけから「1 MiBですべての正当responseを保証」と断定しない。候補1 MiBはObserver側の保守的な受入れ予算であり、documented最大pageが超えるなら拒否とmanual対応を明記するか設計を見直す。bodyだけでなくJSON nesting/token予算をparse前に検査し、Python object expansionをboundedにする。

**Tests → implementation → regression（未着手）:** 上限直下/超過の実loopback response、密なarray/dict・深いnest・Unicode expansion・200件の最大合法messageを256 MiB address spaceの子processで試験し、fail-closedとcursor不変を確認する。許可されたcontainer環境でのmemory=256 MiB/cgroup実測は別gateとして残す。Docker socket禁止を破って実測しない。

## P2/P3とVPS readiness

P2/P3も今回は未修正。特に以下をVPS前の再確認対象として残す。

- P2-1: production entrypointの`-I`、実deployment imageのconfiguration/process probe。fixture imageのPASSを転用しない。
- P2-2: watchdog/heartbeatは同一UIDとSQLite WAL sidecarへのwrite accessに依存する。tableを更新しないこととfilesystem read-onlyで動くことを区別する。
- P2-3: `raw_record_json`はparse後の再serialize。byte-exact rawではなく、数値・空白・escapeの表現を保存しない。Signerの署名用原文には使えない。
- P2-4: Store単体のwriter lock契約。P2-5: open/watchdogの全件走査。P2-6: live corpusの少なさ、artifact依存testと未commit tree。P2-7: state-dirの明示。
- P3-1: opener handler縮小。P3-2: host HOME probeのmachine固定。P3-3: path/fdのTOCTOU。P3-4: EMPTY_OR_REAPEDの監視exit code。

過去のsoakは保存4 messages・gap 0・64 MiB tmpfsの観測であり、長期容量やcatch-upを保証しない。元の123 tests/skip 0も保存artifactが存在するmachineでの結果である。

**VPS migration readiness: NOT READY。** P1の再現と修正設計の準備まで。since確認、P1 implementation/negative regression、変更後の全regression、production security/storage gate、Human Approvalが未完了。Observer/Signer分離、key禁止、GET allowlist、Docker socket禁止、strict/local profileの境界を変更していない。
