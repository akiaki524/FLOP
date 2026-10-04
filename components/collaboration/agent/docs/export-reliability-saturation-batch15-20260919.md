# Batch 15 — Export Reliability / Material Budget Saturation

評価原本・生引用・Task別proofの記述はPrivateの当時の保存入力を対象にする。配布版の派生summaryは原文・完全なproof・再実行fixtureを提供しない。

## 結論

**128 GETの飽和挙動は確認できたが、exportの安定化は未確認。長時間・24H運用への移行は見送る。**
現行実装で3 sessionを実行し、全て128 GETまで到達、全て利用64 / 探索64を維持した。
954件の新規公開TaskからローカルCOMPLETED **15件**、False Complete **0**。

exportは元の3回で成功2 / timeout 1。session 03でBatch 14の本文途中timeoutが再現した。
export専用総期限を15→30秒にする小変更を加え、別枠の1回限りのprobeで検証したが、
今度はHTTP応答前に約15.08秒でtimeout。**修正で安定したとは結論しない**。
元の3 session・失敗・Frozen・成績は全て残し、probeを成功sampleの置換には使っていない。

3 sessionのnetwork admissionは合計約23.0分、最初のsession開始から最後のfreezeまで約24.4分。
その後の修正後probeは別の約15秒の診断。3回の短いfixtureだけではなく、30 poll・384 Material GETで
流入変動、seq回収、128枠の飽和、Material取得時間を実測した。

## 開始状態・事前固定した範囲

開始Gitは `main / 80fda07ab080efa9b6c0e7f28554405fb799060c`、worktree clean。
開始時5999ファイルのhashを保存した。
`2026-09-19T11:21:28.057708Z` に、最大3 sessionのplanをFresh取得前に固定。
session 01は現行の6 poll、02 / 03は観測窓を延ばした12 pollとした。

| 項目 | session 01 | session 02 / 03 |
| --- | ---: | ---: |
| snapshot回数 / 開始間隔 | 6 / 30秒 | 12 / 30秒 |
| snapshot最大records | 1200 | 2400 |
| export | gapがある場合のみ固定公開URLへ最大1 GET | 同左 |
| export byte / deadline上限 | 10 MiB＋検知1 byte / 15秒 | 同左 |
| gap回収records上限 | 600 | 600 |
| 最大admitted records | 1800 | 3000 |
| Material予算 | 128 GET / 8 MiB | 同左 |
| session network admission | 600秒、進行中GET最大15秒超過余地 | 同左 |
| Task単位 | 最大4 GET、depth 2、redirect 0 | 同左 |

元の3 sessionの最大GETはsnapshot 30、export 3、Material 384。
失敗sampleの置換・追加・自動retryなし。cacheはsession-localで、前sessionの失敗やmutable資料を使い回さない。
session 02 / 03は既存driverの有限capture定数だけをlocal wrapperから設定し、有効値を各sessionのplanに保存した。
Material予算・Scheduling rule・Solver / Classifier / acceptance / oracleは固定した。

実行は `.local/batch15/operate.py --session 01` / `02` / `03` を順番に使用。
これは有限の実験profileであり、daemon、scheduler service、autopilot、24H稼働の追加ではない。
replay / regressionはsocketを禁止したoffline処理である。

## export timeoutの原因をどこまで確定できるか

Batch 14の失敗は、HTTP 200の後に本文受信途中で終了した。
取得時間15.003656秒、458,752 bytes、完全なJSONL行740件と最後の不完全行。
取得できたseqは6906015–6906754で、観測対象6918518–6920279より古く、gap位置を1件も含まなかった。
従って589 seqを回収できず、その中のTask数は不明である。

現在のclientはDNS / 接続 / headers / body全体に15秒のdeadlineを設定している。
この**時間上限と大きいexport本文の取得時間の不一致**が、回収できなかった直接の機構である。
ただしサーバ負荷、回線、接続・header待ちとbody待ちの内訳までは記録から断定できない。
HTTP 404 / DNS failure / 資料不存在 / Solver failureという分類は誤りである。

比較対象のBatch 13では8,815,925 bytesを0.977698秒で受信している。
本文サイズが常に15秒を超えるわけではなく、Batch 14の1回だけから恒常的なcapacity不足とは言えない。
公式実装で確認済みのexportは古い保持recordsを先に含むため、途中bytesを保持しても今回必要なseqへ届かない場合がある。
部分JSONLを「最新snapshotの完全取得」とみなしたり、未取得seqを不存在扱いしたりしない。

今回の再現結果とFixの要否は、後述の実測で判断する。過去の失敗結果は変更しない。

### 今回のexport結果と修正後の確認

| 取得 | 総期限 | HTTP | 受信bytes | 経過秒 | 結果 / 回収seq |
| --- | ---: | --- | ---: | ---: | --- |
| session 01 | 15秒 | 200 | 7,968,815 | 1.442 | 成功 / 32 |
| session 02 | 15秒 | 200 | 5,807,724 | 1.136 | 成功 / 146 |
| session 03 | 15秒 | 200 | 65,536 | 15.002 | 本文途中TIMEOUT / 0 |
| 修正後probe（別診断） | 30秒 | 応答なし | 0 | 15.076 | HTTP応答前TIMEOUT / 0 |

session 03の部分本文は完全行148件と最後の不完全行で、seq 6944848–6944995。
必要な範囲6956386–6960158より古く、gapを埋めるrecordsは取得できなかった。
未回収1499 seqに何件のTaskが含まれるかは不明。取得Taskが少ないことを「流入がない」と解釈しない。

初期planは最大3 session / export 3回だった。
3回目で再現したFailureへの修正検証として、**2026-09-19T11:47:02.098233Zに追記plan**を保存し、
同じ固定URLへ最大1回の追加export GETだけを許可した。元planは編集していない。
4回目のsession・snapshot・Material GETは追加せず、probeの回収上限600、byte上限10 MiB、retry 0を固定した。
追加probeも失敗した時点でnetwork取得を終了し、結果に合わせた再試行やさらに長い期限は試していない。

### 実装した小変更と限界

- `src/collaboration_agent/material_fetch.py`: 固定offers exportへの明示opt-inだけ、総deadlineを30秒に分離。
  通常GETの総deadlineと接続/socket timeoutは15秒のまま。固定URL、10 MiB、global IP検査、TLS、GET-only、redirect禁止は維持。
- `tests/readonly_intake.py`: attemptに設定deadline、responseに経過秒、session planにexport / 通常GET期限を記録。
  recovery回数・600 records・session 600秒・Material 128 GET / 8 MiB・Schedulingは無変更。
- `tests/test_export_deadline.py`: 期限分離、source境界、GET-only、partial bytes、retryなし、session期限維持の3 testsを追加。

通常の小さいMaterialと数MBのexportを同じ総15秒にしていたため、接続等に時間を使った場合の本文取得余裕を
exportだけ最大15秒追加した。これが解決するのは総期限側の余裕不足に限られる。
30秒probeは15.076秒でHTTP応答前に終了し、残した15秒socket timeoutと整合するが、
DNS後の接続・TLS・header待ちのどこかは未計測で、サーバ・ネットワーク原因も断定しない。
今回のliveでは30秒の追加余裕を使って成功するケースを観測できなかった。
この変更は**bounded性と影響範囲を検証した小修正**であり、安定性改善を実証済みとするものではない。

総期限を30秒へ延ばすことは将来のMaterial残時間を最大15秒減らし得る。
600秒を自動延長したり、その分のGET枠を増やしたりはしない。

## 飽和時のScheduling検証方法

既存の `observed-fetch-efficiency-v1` をそのまま使う。
allowlisted直接Note / math previewの利用候補枠と、それ以外の探索枠を、実GET消費で概ね均等に配分する。
各枠の中はFIFO、同数なら探索を先行、空になった枠の容量は他方へ回す。
Task ID・known answerによる優先処理はない。opaque Noteの中身が解けるとは仮定しない。

各sessionの取得ledgerをofflineで同じschedulerへ再入力し、全Taskの取得順とGET課金が一致することをassertする。
両枠に候補が残る間のGET差、128到達時の枠別消費、残り候補数、未試行理由を検査する。
これにより「飽和したが探索を捨てた」という挙動を結果の完了数とは別に確認する。

session 01では232 Task、128 GETまで到達し、利用64 / 探索64。
飽和時にも利用側11 Task・探索側74 Taskがpendingで、両枠が残る間のGET消費差は最大2だった。
全Taskをledgerに残した結果、予算未試行55、Material完全72、COMPLETED 5、HUMAN_REVIEW 31、UNKNOWN 196。
独立検証のFalse Completeは0。探索を維持したという挙動は実測で確認できた。
一方で単純FIFOとの同一Fresh cohort A/Bではないため、完了数増加の因果的証明ではない。

## 観測・回収・Materialの時間的関係

現行driverはsnapshot群 → export → Material取得という直列構成。
exportは最後のpollの後なので同一session内のpollを直接遅らせない。
ただしexportと解析に使った時間だけMaterialの600秒deadlineまでの余裕が減る。
さらにMaterial処理とoffline / orchestration時間の間は次sessionのsnapshotを取らず、session間の盲区間が生じる。

session 01ではpoll間隔は約30秒、最大開始遅れ0.000351秒。
exportは1.441715秒で7,968,815 bytesを受信し、32 seqを回収してgap 0。
回収由来は9 Task、うちMaterial完全3件、COMPLETED 0件。
Material開始時点で約447.555秒が残り、全sessionは359.638秒で終了した。
Material側にはHTTP応答前timeout 1件があり、export成功とは独立に記録した。

この取得形態でwithin-session gap 0を達成しても、全時間帯を網羅したstream観測とは呼べない。
session間の未観測seq数と、観測window内の未回収seq数は後述のEvidenceで分ける。

| 時間・範囲 | session 01 | session 02 | session 03 |
| --- | ---: | ---: | ---: |
| session経過秒 | 359.638 | 462.021 | 558.637 |
| Material開始時の残り秒 | 447.555 | 267.614 | 254.283 |
| 最大poll開始遅れ秒 | 0.000351 | 0.000670 | 0.000507 |
| 観測records / seq幅 | 1207 / 1207 | 2305 / 2305 | 2274 / 3773 |
| gap発生seq → 回収 → 未回収 | 32 → 32 → 0 | 146 → 146 → 0 | 1499 → 0 → 1499 |
| 初回stock Task | 42 | 28 | 34 |
| 後続snapshot初見Task | 181 | 376 | 266 |
| 回収由来Task | 9 | 18 | 0 |

12 pollではcapture自体が約180秒長くなり、Material開始時の余裕を減らす。
今回は全3回で時間切れ前に128 GETを消費でき、SESSION_TIME_BUDGET_EXCEEDEDは0だった。
session 03のgapは固定600回収上限も超えるが、実際にはtimeoutで0回収のため、
未回収1499件を「600 capだけのせい」とも分類しない。成功しても600を超える分は残り得る。

session 01→02の最後のsnapshot応答から次の最初の要求までは260.170秒、未観測seq区間は3600。
02→03では164.356秒、827 seq。**session間には合計4427 seqの別の盲区間**がある。
freeze後のorchestration時間はそれぞれ51.211 / 32.824秒で、残りは主にexport・Material処理。
この全てをexportの遅さに帰属しない。盲区間のTask数は不明で、今回の954 Taskに推定加算していない。

## Material・Solver結果

| 指標 | session 01 | session 02 | session 03 | 合計 |
| --- | ---: | ---: | ---: | ---: |
| 有効Task | 232 | 422 | 300 | 954 |
| Material GET / 上限 | 128/128 | 128/128 | 128/128 | 384/384 |
| budget未試行Task | 55 | 216 | 109 | 380 |
| time未試行 / partial-deferred | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| Material完全 | 72 | 79 | 75 | 226 |
| Solver到達 | 27 | 27 | 28 | 82 |
| COMPLETED | 5 | 4 | 6 | 15 |
| HUMAN_REVIEW | 31 | 28 | 32 | 91 |
| UNKNOWN | 196 | 390 | 262 | 848 |
| False Complete | 0 | 0 | 0 | 0 |
| 利用GET / 探索GET | 64 / 64 | 64 / 64 | 64 / 64 | 192 / 192 |

955 Offer中、session 02のjob不正1件を除いた954 Taskが有効。
過去13 snapshotsとのjob ID / job全体 / Offer本文重複、session間Task重複はいずれも0。
全5786 admitted recordsを元のsnapshotまたはexportのraw hashと照合した。

Material新規GETのHTTP結果は200が382件、応答前TIMEOUT 1件、503が1件。
DNS failure / 404は0。Material timeoutはsession 01、503はsession 03で、export failureとは別件。
Material bytesは合計1,253,967、session内重複GETは全て0、cache hitは計31。
8 MiB上限には余裕があり、未試行380件の原因はGET回数上限である。

Material不完全728件の内訳は、予算未試行380、参照なし204、preview mismatch70、未解決参照25、
source拒否21、Solver向け本文上限9、spec不一致7、truncation疑い6、malformed reference4、TIMEOUT1、HTTP_FAILURE1。
UNKNOWN 848件は、Material不完全637件とMaterial完全・現行Capability非対応211件に分かれる。
HUMAN_REVIEW 91件はMaterial側guardによる。Network / Material failureをSolver failureへ集約しない。

### GET効率と従来指標

| Metric | Batch 13（飽和） | Batch 14（非飽和） | B15-01（飽和） | B15-02（飽和） | B15-03（飽和） |
| --- | ---: | ---: | ---: | ---: | ---: |
| Material complete / GET | 0.4453 | 0.5325 | 0.5625 | 0.6172 | 0.5859 |
| Solver reached / GET | 0.1953 | 0.2078 | 0.2109 | 0.2109 | 0.2188 |
| COMPLETED / GET | 0.0547 | 0.0390 | 0.0391 | 0.0313 | 0.0469 |
| Material availability | 27.27% | 40.59% | 31.03% | 18.72% | 25.00% |
| Solver applicability | 43.86% | 39.02% | 37.50% | 34.18% | 37.33% |

今回合計ではMaterial/GET 226/384＝0.5885、Solver/GET 82/384＝0.2135、COMPLETED/GET 15/384＝0.0391。
Material availabilityは23.69%、Solver applicabilityは36.28%、Solver completionは15/226＝6.64%、
End-to-end completionは15/954＝1.57%。

今回3回は全て飽和、非飽和sessionは0。非飽和比較はHistorical Batch 14として別に扱う。
Material/GETとSolver/GETの改善方向は観測できたが、COMPLETED/GETはBatch 13より低い。
Task分布、capture窓、export成否が異なるので、Scheduling変更による因果的な性能向上と断定しない。
確定できたのは、実飽和でも両枠を維持し、両枠pending中のGET差が全sessionで最大2だったこと。

### Real COMPLETEDと検証範囲

実公開TaskからのローカルCOMPLETED 15件は、inline表5、GCD/LCM3、shortest path2、Nim2、
公式agent fields引用2、ordered-seq-pair reference検証1。
固定の独立oracle、入力・引用digest、別stateでのreplayで全件を確認した。
追加probeのCOMPLETEDは0で、本数へ加算しない。今回回収由来27 TaskからのCOMPLETEDも0だった。

inline表5件はすべて物理1行・論理12行。
本文文字数 / UTF-8 bytesはそれぞれ1456、1467、1469、1486、1449（各文字数＝bytes）。
これらはCOMPLETE_WITHIN_SCOPEであり、宣言range等による全行存在の証明ではない。
整数referenceを含むreference検証はauthor referenceとの比較で、そのreference自体の数学的正当性の証明と混同しない。
全sourceはSOURCE_UNVERIFIED。外部送信・payer合格・protocol完了を意味するCOMPLETEDではない。

## Verification・Historical Integrity

- 修正前104 tests、修正後107 tests PASS、skip 0。CLI Material smoke 3件PASS。
- Batch 8 / 10 / 12 / 13 / 14の615 Frozen Taskを修正前後に再実行し、過去のresult object / verifiedが全件一致。
- 今回954 Taskは元の取得コードで二重state replay後、修正後にも同じFrozenを再実行。
  result object / verifiedが全件一致、False Complete 0。修正後に成績を良くする変更なし。
- 5999開始ファイル中5997がhash一致。差分はmaterial_fetch.pyとread-only driverのみ。
  Solver / Classifier / Resolver / acceptance / oracle、Batch 8 / 10 strictと全Historical Evidenceは不変。
- 3 sessionは修正前31 Pythonファイルのlock下で取得・freeze・初回検証を終えた。
  **その後に**期限修正を行ったため、最終コードは当時のlockとは一致しない。JSONに両方のhashを保存し、時点を混同しない。
- local orchestrationのJavaScript構文誤りが1回あった。実行前に失敗しnetwork作用は0、1回修正して続行した。
  Operation失敗やTask成績には算入していない。

Private原本 `export-reliability-saturation-batch15-20260919.json`／配布版の派生summary `export-reliability-saturation-batch15-20260919.public-summary.json`（原文・再実行fixtureではない） にsession別Task・proof・GET課金・飽和時残件・
source binding・original / post-fix分離・plan追記・主要artifact SHA-256を保存した。
local raw / Frozen / test log / replayは `.local/batch15/` に保持。
既存evaluatorの歴史的 `batch: 7` labelは変えず、今回の帰属はplan / path / ReportでBatch 15とする。

## 残ったBottleneckと次の判断

長時間Read-only / 24H Runtimeへの移行は**まだ不可という判断**。

1. exportは本文途中と応答前の両方でtimeoutし、30秒総期限への分離だけでは安定性を実証できなかった。
   次は接続・TLS・header・bodyの時間を切り分ける有限の診断が先。今回は追加retryを打ち切った。
2. 128 GET飽和時の公平性は確認できたが、未試行380 Taskが残る。長いcaptureだけでは供給とread予算の不釣合いが拡大する。
   budgetを倍増したりSolver boundaryを緩めたりして解決扱いにはしない。
3. session間には4427 seqの盲区間がある。captureとMaterial処理の直列構成のまま時間だけ延ばしても網羅性は得られない。
   ここを扱うruntime設計は別途必要で、今回daemon / scheduler serviceを追加する根拠にはしない。
4. 正しいローカル回答を15件再利用でき、bounded samplingとしての価値は確認できた。
   次段階も限界を明示した有限観測・特定Failure診断とし、連続網羅運用や自律24H稼働とは区別する。

取得終了の判断は、3回の実飽和を観測できたことと、追加1回の修正後probeでもtimeoutし、
同じdeadline増量・retryの繰返しから得られる情報が小さいことによる。必要な計測・設計へ切り替える。

## Boundary

実施した外部作用は公開read-only GETのみ。Task / Note / Documentはuntrusted dataとして扱った。
External Write、Technocore / tclk Action、ACCEPT / LOCK / REVEAL / REFUND、Signer / Wallet / Secret、
paid API、LLM / CCW invocation、Production mutation、Autopilot、push / publishは0。
取得済みデータの保存・Test・offline replay・Report・local commitだけを行う。
