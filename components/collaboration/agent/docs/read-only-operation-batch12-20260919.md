# Batch 12 — Bounded Read-only Operation / Real Distribution Observation

## 結論

固定した既存Capabilityで公開Taskを実際に取得・offline処理した。
4 snapshot、155 Offer、うち有効な新規Task **154件**。Material完全 **49件**、Solver到達 **21件**、
COMPLETED **7件**、HUMAN_REVIEW **27件**、UNKNOWN **120件**。
GCD/LCM 4件、Nim 2件、ordered-seq-pair reference検証1件が自然に再出現し、全7件を独立検証した。

最初の評価器はGCD/LCMのoracle欠落により4件をFalse Completeと警告した。
元の警告結果を保存したうえで、**評価器だけ**に独立oracleを補い、同じFrozen Materialを再検証した。
154件すべてのruntime回答・状態は変更なし、確認後のFalse Completeは **0**。警告が最初から0だったとは記録しない。

次は選択肢 **2. 特定FailureをImproveする** が妥当。
対象はSolverではなく、最新200 recordsの窓から漏れる観測範囲と、固定fetch予算によるMaterial未評価である。
今回の境界・予算は変更せず、次Batchでboundedな取得・予算配分を検討して再観測する。
5分程度の観測から24H Runtimeへ進む根拠はなく、Daemon / Scheduler / Frameworkは追加しなかった。

## 開始状態と固定したsession

開始Gitは `main / dfe82c2a16068520b372cc19b556d45c6f735d22`、worktree clean。
`2026-09-19T09:19:30.902235Z` に `.local/batch12/session-plan.json` を保存し、Task内容を見る前に以下を固定した。

- URL: `https://technocore.chat/r/tclk-offers?format=json&limit=200`。
- 4回のみ、開始間隔60秒以上、開始の全体期限30分。失敗回の追加・置換・自動retryなし。
- 各snapshot最大200 records / 2 MiB。Materialは既存の32 fetch / 2 MiB per poll、4 fetch per Task、depth 2、redirect 0。
- `collect_public.py` → `evaluate_fresh.py acquire` → Frozen `replay` を再利用。外部Taskにあるdelivery等の指示は実行しない。
- 取得は許可済みpublic network環境。offline replayではsocket使用を禁止。
- 既存の時系列順で取得し、内容・難易度・成功見込みによる優先選別なし。予算打切りTaskも分母に残す。
- runnerのBatch 2重複除外に加え、ReportでBatch 7 / 8 / 10とsession内のjob ID / job全体 / Offer本文重複を除外。最初の観測を採用。
- 初回windowは既存stock、その後の初見Taskは観測した流入として別集計。

準備時にsrc/testsの27ファイルをhash固定。4回のoriginal operation replay完了時まで全27ファイル一致。
その後変更したのは既存評価器2ファイルと新規test1ファイルのみ。**srcのSolver / Classifier / parser / acceptance / transportは無変更**。
Historical Evidence 1341ファイルの開始時hashを確認し、Batch 8 / 10 strict、Batch 11 diagnosticを含め全て不変だった。

## 1. 実際の流入と観測範囲

| poll | UTC開始 | 前回開始から秒 | seq window | Offer / 有効Task | Material完全 | C / H / U |
| --- | --- | ---: | --- | ---: | ---: | --- |
| 01 | 09:19:31.007 | — | 6886393–6886592 | 25 / 25 | 10 | 0 / 6 / 19 |
| 02 | 09:20:48.537 | 77.530 | 6886851–6887050 | 35 / 34 | 14 | 4 / 4 / 26 |
| 03 | 09:22:03.399 | 74.862 | 6887260–6887459 | 50 / 50 | 12 | 2 / 9 / 39 |
| 04 | 09:24:28.769 | 145.370 | 6888338–6888537 | 45 / 45 | 13 | 1 / 8 / 36 |
| 合計 | | | 800 distinct records | 155 / 154 | 49 | 7 / 27 / 120 |

poll 02の1 Offerはjob欠損/不正でありTaskには数えない。
過去snapshotとの重複、session内Task重複、record重複は全て0。
初回stock 25 Task、その後の新規観測129 Task。snapshot完了時刻間の301.004秒で観測新規Taskは約25.71件/分。
これは**見えたwindow内の観測値**であり、全公開Taskの到着率推定ではない。

同じgeneration内でsnapshot間に258、209、878のseq位置が抜け、合計 **1345 records相当の未観測区間**がある。
最初から最後のseq幅2145に対し観測800、coverageは37.30%。未観測位置が何件のOffer / Taskだったかは不明。
初回より後だけではseq幅1945に対し600 recordsを捕捉した。

今回の手動bounded sessionは分析と取得を交互に行い、最後の間隔は約145秒になった。
これは固定60秒cadenceのcapacity testではない。ただし約75秒の間隔でもwindow gapを実測したため、
この取得形態を網羅的なstream観測と呼べないことは明確である。欠測分を推定補完せず、追加取得もしなかった。

## 2. Network / Materialを分けた観測

Resolver呼出し154 Task、うち実network fetchを伴ったTask 99件。
参照nodeは152件（URL検査拒否・budget拒否も含む）。実Material fetch attemptは **115回**、cache hitは別に1回。

| 指標 | 結果 |
| --- | ---: |
| snapshot GET | 4 / 4成功 |
| Material HTTP応答あり | 114 / 115 |
| HTTP 200 | 114 |
| DNS failure | 0 |
| timeout（HTTP応答なし） | 1 |
| その他transport failure | 0 |
| HTTP error / 404 | 0 / 0 |
| transport failure率 | 1 / 115 = 0.87% |
| Material取得bytes | 591322 |
| 全Material fetchの予算 | 最大128回に対し実115回 |

timeoutはpoll 02の `native-6886852`、参照 `/kv/tclk-job-en/task-ed27cf19-`。
`http_status=null`、raw 0 bytes、cache_hit=false、TIMEOUTで記録。Taskの演算能力不足ではなく、
**Material evaluation unavailable — transport** として扱う。再取得して成功へ置き換えていない。
404は観測0であり、「Note missing」を参照なし28件の意味に転用しない。

Material不完全105件の内訳は以下のとおり。

| 理由 | Task数 | 解釈 |
| --- | ---: | --- |
| MISSING_TASK_REFERENCE | 28 | TaskのMaterial参照なし。Note不存在の証明ではない |
| FETCH_BUDGET_EXCEEDED | 24 | poll 03 / 04で各12件。既存32 fetch上限による未評価 |
| PREVIEW_MISMATCH | 18 | preview / full-spec binding不一致 |
| UNRESOLVED_REFERENCE | 13 | 現行resolverが扱わない参照・操作記述 |
| SOURCE_NOT_ALLOWED | 9 | source allowlist外。勝手に取得しない |
| TRUNCATION_SUSPECTED | 5 | 既存長さ等のguard。全件の実欠落を断定しない |
| MALFORMED_REFERENCE | 3 | 現行参照構文に適合しない |
| TOO_LARGE_FOR_SOLVER | 3 | 受信成功でも既存Solver用本文上限を超過 |
| TIMEOUT | 1 | 応答前の取得失敗 |
| INCOMPLETE_SPEC | 1 | 既存SPEC grammar不一致 |

poll別のMaterial fetch / bytesは22 / 105551、29 / 111651、32 / 208607、32 / 165513。
2 MiB byte予算には余裕があったが、後半2 pollでfetch回数上限に到達した。
従って24件は「Materialが公開されていない」でも「Solverが解けない」でもなく、**今回の運用予算では未評価**。
今回のMaterial availabilityも、実世界の存在率ではなく「現行resolverと予算・guardの下で完全と判定できた率」である。

## 3. 分母を分けたMetrics

| Metric | 分子 / 分母 | 率 |
| --- | ---: | ---: |
| Material availability | 49 / 全Task 154 | 31.82% |
| Solver applicability | Solver到達21 / Material完全49 | 42.86% |
| Solver completion | COMPLETED 7 / Material完全49 | 14.29% |
| End-to-end completion | COMPLETED 7 / 全Task154 | 4.55% |
| 既存exact Template再出現 | 7 / Material完全49 | 14.29% |
| 同・全観測Task基準 | 7 / 全Task154 | 4.55% |

Classifier到達は49件。登録familyによる広いdispatchとexact Template一致は異なる。
Solver到達21件のうち、既存Templateに適合した7件は全てCOMPLETED、非対応Template 14件はUNKNOWN。
Material完全だが登録Solverなしは28件。

UNKNOWN 120件を原因別に分けると、Material不完全78件と、Material完全・非対応42件。
HUMAN_REVIEW 27件は今回すべてMaterial側のsource / preview guardであり、Solver内部例外ではない。
従って「UNKNOWN 120＝Solver failure 120」とは記録しない。

## 4. 既存Capabilityの自然な再出現

| Capability | eligible exact一致 | Solver到達 | C / H / U | 観測 |
| --- | ---: | ---: | --- | --- |
| math.gcd_lcm | 4 | 8 | 4 / 0 / 4 | 別の整数値4組で再利用。他のmath問4件は非対応 |
| math.normal_nim | 2 | 2 | 2 / 0 / 0 | heapsの異なる2 Taskで再利用 |
| validation.ordered_seq_pair | 1 | 11 | 1 / 0 / 10 | 提示reference比較1件。他のvalidationは非対応 |
| tables.inline | 0 | 0 | 0 / 0 / 0 | 一致本文2件はあるがMaterial bindingで停止 |
| validation.integer_reference | 0 | 0 | 0 / 0 / 0 | 今回の完全Taskでは未確認 |
| math.shortest_path | 0 | 0 | 0 / 0 / 0 | 今回の完全Taskでは未確認 |
| docs.agent_fields / docs.quoted_limit | 0 | 0 | 0 / 0 / 0 | 対応するexact問は未確認 |

exact.match / json.extract / text.linesの明示params選択型Capabilityも、native Offerからの自然dispatchは0。
CLI smokeで行ったpublic Materialのliteral inspectionは、native Taskの成功へ加算しない。

COMPLETEDのTask IDはGCD/LCM `6886932, 6887299, 6887317, 6888445`、Nim `6886997, 6887003`、
ordered pair `6887037`（全て `native-` prefix）。値・独立proof・source/result digestをJSONに記録した。
これはbounded operationでの再利用Evidenceであり、新しいStrict Fresh generalization評価の合格とは呼ばない。

### inline表の追加Evidence

`native-6887304` は本文1451文字 / 1451 bytes、`native-6888370` は1472文字 / 1472 bytes。
いずれも物理1行、論理12行、time-extremaの既存parserには一致するがPREVIEW_MISMATCH。
最終seqはそれぞれ5704507、6381533。行順・source hashはJSONに保持した。
宣言行数・seq範囲による全行存在の保証はなく、完全な行単位の切落しRiskは残る。
今回もpreview bindingやtable guardを緩めて成功させていない。

## 5. 実運用で見つかった小さな評価器Bug

元の `evaluate_fresh.py` はmathの独立検証をNim / Batch 9 bounded familiesに限っており、
以前から登録済みの `math.gcd_lcm@1` を検証する経路がなかった。
そのため正否未確認のCOMPLETEDが、fail-closedの集計でFalse Completeと警告された。

| 評価段階 | C / H / U | False Complete警告 | 説明 |
| --- | --- | ---: | --- |
| 固定コードによるoriginal operation | 7 / 27 / 120 | 4 | GCD/LCMにoracleなし。実誤答の証拠ではない |
| 評価器修正後・同じFrozen replay | 7 / 27 / 120 | 0 | GCD/LCM 4件の値と引用を独立検証 |

修正範囲は `tests/verify_bounded.py` のoracle、`tests/evaluate_fresh.py` の採点接続、
`tests/test_gcd_verifier.py` の回帰testだけ。
runtimeの `math.gcd` / `math.lcm` を呼ばず、独立parser、拡張EuclidによるBézout係数、整除条件、
LCMの積の関係、canonical回答、Frozen request由来askのcitationを検証する。
Task ID / known answer lookupは使わない。

oracle追加はSolverの受付拡大ではない。新しい実行Capabilityも追加していない。
修正前 `replay-01`〜`04` と修正後 `post-replay-01`〜`04` は別保存し、154件のresult objectが完全一致することをassertした。
original replay 02/03/04の非0終了とFalse Complete警告を隠さず残している。

別途、poll 01は取得完了前のoffline replay起動がmanifest未生成で1回失敗した。
取得はやり直さず、freeze完了確認後に1回だけ再実行して成功。local補助scriptへmanifest存在確認を加え、元の失敗logも保存した。
これはTask / network failureの件数には加算しない。

## 6. Verification・Historical Integrity

- 修正前88 tests、修正後91 tests PASS、skip 0。新規3 testsはゼロ・共有因数・大整数・誤答・引用改変・非対応入力を検証。
- 154 native Taskを独立した2 local stateでoffline実行。post-replayは全回PASS、duplicate / SQLite終端 / auditを確認。
- Batch 8 / 10 Frozen 151件を再実行し、Batch 11の対応replayとruntime result / verifiedが全件一致。
- Batch 8のC5はBatch 9以降の既存replay結果であり、original strict C0は変更していない。Batch 10 original C0も維持。
- CLI Material smoke 3件PASS。1 synthetic + 2 literal public Material inspectionsで、今回の154 Task / C7とは別。
- 過去Report / Evidence等1341ファイルがhash一致。srcは全ファイル無変更。
- `.local/batch12/` のplan・raw snapshot・Frozen bundle・失敗/成功log・original/post replayを保存。Report JSONには98 artifactのSHA-256と、修正後code hashを記録。

全sourceは従来どおり `SOURCE_UNVERIFIED`。False Complete 0はこの取得資料と独立oracleで確認した範囲の結果であり、
source真正性・生成元の全Material存在・外部payer合格の証明ではない。
integer reference / ordered pair検証はauthor referenceとの比較であり、そのreference自体の数学的正当性を証明するものでもない。

## 7. 反復Patternと次の判断

Material完全49件のsource familyはmath 10、validation 11、protocol 13、attest 6、extraction 4、
documentation 2、document / inference / review各1。
数字・DID・Note参照を診断用に正規化したask形状で、次の反復を確認した。正規化はReport集計だけで、routingには使っていない。

- **protocol transcript fold 10件**: 新family候補として蓄積。独立oracle・protocol意味論の別検討が必要。このBatchでは実装しない。
- **modular exponentiation＋一節の説明 2件**: 既存GCDへの広いdispatch後UNKNOWN。別operator候補であり、GCDの失敗ではない。
- **attestation 6件（2形状）**: 外部署名・書込みが必要なので、現BoundaryでのImprove対象外。
- **既存GCD 4件 / Nim 2件**: 既存Capabilityの再利用価値を実測した。new familyとして数えない。

反復Failureの優先順位は、fetch budget打切り24件、参照なし28件、preview mismatch18件。
timeoutは1件のみ、DNS / 5xx / 404は0。したがってgeneric retry基盤を最優先にするEvidenceはない。

次は **2. 観測範囲とfetch予算配分という特定FailureのImprove** を推奨する。
別のbounded session設計で、未処理件数・取り逃し・期限を明示しながら、許可済み総read / byte / time上限内で取得効率を検証する。
結果に応じて易しいTaskを優先したり、allowlistを広げたり、予算を無制限に増やしたりしない。
preview mismatchについても、参照同一性の証拠なしにguardを外さない。

継続観測には価値があるが、同じwindow / cadence / 予算のまま回数だけ増やすと欠測の偏りも続く。
よって1の単純継続より、対象を絞った2を先に行い、その後に再観測する。
3の新family開発は候補蓄積まで、4の24H Runtimeは未承認・Evidence不足として見送る。

## Boundaryと成果物

公開GETとlocal state / Evidence / Test / commit以外の動作は行っていない。
External Write、Technocore / tclk Action、ACCEPT / LOCK / REVEAL / REFUND、Signer、Wallet、Secret access、
paid API、LLM / CCW invocation、Production mutation、Autopilot、push / publishは0。
外部Task / Note / Documentはuntrusted dataとして扱い、本文の操作要求を実行していない。

Private原本 `read-only-operation-batch12-20260919.json`（公開版では除外、追加summaryなし） にpoll別metrics、Task別原因、全fetch結果、Capability再出現、
original警告とpost-verificationの分離、改善候補、hashを収録した。
既存collectorのholdout欄・summaryの歴史的 `batch: 7` は変更していない。本sessionの帰属と観測目的はBatch 12 plan / path / Reportで固定し、
holdout評価や過去Batchへの遡及成功とは扱わない。
