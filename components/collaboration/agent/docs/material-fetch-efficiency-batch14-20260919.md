# Batch 14 — Material Fetch Budget Efficiency / Scheduling Improve

評価原本・生引用・Task別proofの記述はPrivateの当時の保存入力を対象にする。配布版の派生summaryは原文・完全なproof・再実行fixtureを提供しない。

## 結論

128 GETを増やさず、Evidenceに基づく2枠のcost-aware取得順序を導入した。
重複削減を新規実装する余地はなく、Batch 13の重複Note GETは0だった。
実運用は101 Task / 77 GET / Material完全41 / Solver到達16 / COMPLETED 3 / False Complete 0。

Material完全/GETとSolver到達/GETは前回より高かったが、COMPLETED/GETは低下した。
export timeoutにより観測範囲が欠け、128 GETを使い切らないsessionだったため、
**liveでの飽和時改善や、予算打切り50→0の因果的改善は実証できていない**。
同じ既知資料の半分予算診断では、完全資料数と既存Solver資料カバーが各2件増えた。
今回確認したのは小さな配分改善と探索維持であり、総容量問題の解消ではない。

次は24H化ではなく、export timeoutと128枠飽和条件を対象とするbounded観測を推奨する。
この結果を見てpriorityを再調整したり、成功するまでsampleを追加したりはしない。

## 開始状態と変更範囲

開始Gitは `main / 880e39739937757ec0230921da9c47f745dc1bfd`、worktree clean。
既存実装の変更は `tests/readonly_intake.py` の取得順序・ledger・planのみ。
新規 `tests/test_fetch_scheduling.py` を追加した。
**src、Solver、Classifier、Resolver、acceptance、回答、独立oracleは無変更**。

Batch 8 / 10 Strict Fresh、Batch 11 diagnostic、Batch 12 / 13 Operationを変更せず、
今回の分析・診断・live結果は `.local/batch14/` と本Reportに別保存する。

## 1. Batch 12 / 13のGET消費分析

以下のGET帰属は、共有cacheを最初に取得したTaskへ新規通信費用を計上したもの。
同じresponseを後のTaskが再利用する場合、そのTaskの新規GETは0。
「そのGETだけで最終結果を生んだ」という因果主張や、集合間の加算可能性を意味しない。

| 指標 | Batch 12 | Batch 13 |
| --- | ---: | ---: |
| Task数 | 154 | 209 |
| Material新規GET | 115 | 128 |
| 0 / 1 / 2 GETのTask数 | 55 / 83 / 16 | 90 / 110 / 9 |
| full-spec GET | 99 | 119 |
| second-hop cited material GET | 16 | 9 |
| 一意URL / 一意Note URL | 107 / 102 | 128 / 123 |
| 重複GET | 8 | 0 |
| cache hitによる既存重複回避 | 1 | 3 |
| Material完全Taskへ帰属するGET | 60 | 61 |
| Solver到達Taskへ帰属するGET | 21 | 25 |
| COMPLETED / HUMAN_REVIEW / UNKNOWNへのGET帰属 | 7 / 27 / 81 | 7 / 43 / 78 |
| 予算打切りTask | 24 | 50 |

Batch 12の重複8 GETはpollを跨いだ4種の公式document URLで、**同一Noteの重複GETは0**。
Batch 13ではsession-local exact URL cacheが既に有効で、Noteを含む全128 GETが一意だった。
このためdedupe基盤の追加や、aliasと推測した別URLの統合は行わない。

Batch 13の全GETの119/128はfull specであり、second hop削減だけでは主要な不足を解消できない。
full spec取得後のpreview mismatch等は多数あるが、取得前に同一性不一致を確定できない。
UNKNOWN / HUMAN_REVIEWに終わったGETにも新PatternやFailureの観測価値があり、全て「無駄」とは呼ばない。

### budget exhaustionのroot cause

Batch 13の未試行50 Taskは、互いに異なる50個のallowlisted root Note URLを持ち、
いずれも既存128 GETのURL集合に含まれなかった。
従って、既知の依存資料GETと全pending rootを読むには**最低178 GET**が必要。
未取得full specの依存先・byte量は不明なので、178は下限であって推奨予算ではない。

原因は重複GETでもbytes上限でもなく、取得候補の一意URL数が128を上回ることと、
限られたGETを全Taskの単純FIFOへ配分していたことである。
今回は公開APIへの増量安全性・長期負荷のEvidenceを追加確認していないため、budget増量を正当化せず、
**総128 GET / 8 MiB / 600秒admissionを維持**した。

## 2. Evidenceに基づく取得順序の改善

取得前に見えるrequestの構造だけを使い、以下の2枠に分ける。
Task ID・job ID・known answer・数値・取得後の回答は判定に使わない。

- `efficient`: allowlist内の直接Note参照でpreviewがないもの、またはmath previewを伴うfull-spec Note参照。
- `explore`: その他すべて。未知family・invalid reference・参照なしも除外せず保持。

opaqueな直接Noteは中身もSolver適合性も不明であり、efficient枠を「解けるTask」とは呼ばない。
既存能力の利用機会とMaterial完全率を優先する枠である。
実際の過去消費をこのruleで再分類すると、次の差があった。

| 過去群 | Task | GET | Material完全 | Solver到達 | COMPLETED | 予算打切り |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Batch 12 efficient | 50 | 35 | 32 | 21 | 7 | 15 |
| Batch 12 explore | 104 | 80 | 17 | 0 | 0 | 9 |
| Batch 13 efficient | 77 | 53 | 46 | 24 | 6 | 25 |
| Batch 13 explore | 132 | 75 | 11 | 1 | 1 | 25 |

合計でefficientは78 Material完全 / 88 GET、exploreは28 / 155 GET。
特にinference / verificationのpreview群は68 GETから完全0件だったが、これらも探索対象として残した。
相関を将来の成功保証として使わず、full spec取得後の通常のbinding / grammar / acceptance検査は全部そのまま実施する。

### 配分方式

両枠に候補がある間は、**実際のGET消費が少ない枠**を次に処理する。同数ならexploreを先にする。
各枠の内部順序は元のseq順FIFO。
Taskを途中で分割しないため、通常の最大4 GET/Taskだけ枠間消費が一時的にずれる。
両枠が残る間の消費差は最大4。cache hitは0 GETとして扱う。
片方が空になったら他方が残枠を借りる。hardな64/64で未使用枠を残す設計ではない。

過去efficient消費は35/115、53/128で、半分より少なかった。
今回は利用側へ概ね半分を確保しつつ、探索にも概ね半分を残す小さな変更にした。
この比率を最適と主張せず、複数比率の結果を試して最良値を選ぶこともしていない。

取得を並べ替えても、元のTask選択・分母・Frozen manifest順・offline評価順は変えない。
取得順と枠・新規GET・未試行/途中打切りは別ledgerに保存する。
予算切れTaskもResolverを通し、未取得をmissing Noteと誤分類しない。
新規Daemon / scheduler service / 永続queue / generic cacheは追加していない。

## 3. 同じ既知資料でのoffline診断

Batch 13の元の予算打切り50件を除いた159 Taskを固定し、当時のresponse bytesだけを使用した。
不足bytesの推測・ネットワーク再取得は0。完全化したbundleのspec・資料digestは元のFrozenと一致させて検査した。
この既知資料subsetには選択バイアスがあり、209 Task全体やFreshでの改善率を推定するものではない。

| GET上限 | 方式 | 実GET | Material完全 | 過去Solver到達を資料面でカバー | 過去COMPLETEDを資料面でカバー |
| --- | --- | ---: | ---: | ---: | ---: |
| 64 | FIFO | 64 | 32 | 15 | 5 |
| 64 | balanced | 64 | 34 | 17 | 5 |
| 128 | FIFO | 128 | 57 | 25 | 7 |
| 128 | balanced | 128 | 57 | 25 | 7 |

64は固定した半分予算の診断で、実運用予算の変更ではない。
この比較で新しい正解数は主張しない。全資料を読み切れる既知subsetの128 GETでは差がなく、
順序の改善が総必要GET数を減らすわけではないことも示している。

## 4. bounded Read-only Operation

実装とruleをlive取得前に固定した。Batch 13と同じ6 snapshot・30秒間隔・200 records/回、
gap時だけ固定公開exportを最大1回、10 MiB＋超過検知1 byte、admit最大600 gap records。
Materialは128 GET / 8 MiB、通常のper Task 4 GET / depth 2 / no redirect、600秒read admission。
結果を見た増量・追加sample・retry・置換は行わない。

実行: `python3 -B tests/readonly_intake.py --root .local/batch14/live`。
snapshot GET 6回は成功した。exportはHTTP 200の後、本文受信中にTIMEOUTし、458,752 bytesを保存した。
これはMaterial Noteの不存在でもSolver failureでもない。完全なJSONL行のみ既存の検査で扱い、未観測seqを成功扱いしない。

公開データはSOURCE_UNVERIFIED。外部のTask / Note指示は実行せず、操作要求もデータとしてだけ扱う。
External Write、tclk Action、Signer / Wallet / Secret、paid API、LLM / CCW invocation、Autopilot、
Production mutation、push / publishは0。

## 5. 実運用結果とBatch 13比較

plan固定は `2026-09-19T10:27:55.850344Z`、freeze完了まで321.048秒。
6 snapshotで1173 records、101 Offer / 有効Taskを観測し、除外・session内重複は0。
Batch 2 / 7 / 8 / 10 / 12 / 13の12 historical snapshotsとのjob ID / job全体 / Offer本文重複も0。
各recordを元のpage raw hash＋indexへ照合した。

gapは275 / 96 / 10 / 208 seq、計589。
exportは途中までの458,752 bytesに今回のgap recordsを含まず、回収0・未回収589となった。
受信できなかった残りのexportに当該recordsがあるかは不明で、不存在とは断定しない。
1762 seq幅内のcoverageは1173/1762＝66.57%。初回以前・最終snapshot以後のcoverageは対象外。

| 指標 | Batch 13 | Batch 14 |
| --- | ---: | ---: |
| 観測Task | 209 | 101 |
| seq gap（回収前 → 後） | 608 → 8 | 589 → 589 |
| Material完全 | 57 | 41 |
| 新規Material GET / 上限 | 128 / 128 | 77 / 128 |
| full spec / second hop GET | 119 / 9 | 70 / 7 |
| 一意Note fetch | 123 | 72 |
| 重複GET | 0 | 0 |
| 既存cacheによる重複回避 | 3 | 6 |
| budget-untried / partial-deferred | 50 / 0 | 0 / 0 |
| Solver到達 | 25 | 16 |
| COMPLETED / HUMAN_REVIEW / UNKNOWN | 7 / 43 / 159 | 3 / 21 / 77 |
| False Complete | 0 | 0 |

0 GETのTaskは31件、1 GETは63件、2 GETは7件。
31件の0 GETはすべてMISSING_TASK_REFERENCEであり、今回は予算打切りではない。
Material GET 77/77はHTTP 200、311,384 bytes。DNS / timeout / その他transport failure / HTTP error / 404は0。
**exportのtimeout 1件とMaterial GETのtimeout 0件を別集計**する。

| GET効率・従来指標 | Batch 13 | Batch 14 |
| --- | ---: | ---: |
| Material complete / GET | 57/128＝0.4453 | 41/77＝0.5325 |
| Solver reached / GET | 25/128＝0.1953 | 16/77＝0.2078 |
| COMPLETED / GET | 7/128＝0.0547 | 3/77＝0.0390 |
| Material availability | 57/209＝27.27% | 41/101＝40.59% |
| Solver applicability | 25/57＝43.86% | 16/41＝39.02% |
| Solver completion | 7/57＝12.28% | 3/41＝7.32% |
| End-to-end completion | 7/209＝3.35% | 3/101＝2.97% |

Task分布・観測範囲が異なり、GET回数だけならFIFOにも全候補を読む余裕がある。
別の取得順で同じnetwork結果・時間内完了になることまで保証するものではない。
したがってMaterial率上昇や打切り減少を、schedulingの効果として主張しない。
またcache hit 6件は従来の仕組みによるもので、新しいdedupe改善量は0。
Offer snapshotのpreviewをfull specの代用にする、別URLを同一Noteと推測して統合する、といった変更もしていない。

### 枠の利用と残ったMaterial Failure

| 枠 | Task | GET | cache hit | Material完全 | Solver到達 | C / H / U |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| efficient | 34 | 35 | 1 | 31 | 16 | 3 / 0 / 31 |
| explore | 67 | 42 | 5 | 10 | 0 | 0 / 21 / 46 |

exploreにも42/77 GETを配分し、未知Taskを全廃していない。
利用枠が空になった後は探索へ残容量を回すため、最終比率を厳密な1:1に揃える無駄GETはしない。
新規GETの結果帰属はCOMPLETED 3 / HUMAN_REVIEW 21 / UNKNOWN 53。
完全資料Taskへの帰属は46 GET、Solver到達Taskへの帰属は16 GET。

Material不完全60件は参照なし31、preview mismatch13、source拒否8、未解決参照5、
truncation疑い1、Solver向け本文上限超過1、spec不一致1。
UNKNOWN 77件は、Material不完全39件と、完全だが現行Capability非対応38件に分かれる。
HUMAN_REVIEW 21件はMaterial側のguard。Network / Material failureをSolver failureと一括しない。

COMPLETEDはnormal Nim 2件とordered-seq-pair reference validation 1件。
既存の独立oracleと引用検証で3件とも確認し、False Complete 0。
reference検証はauthor referenceとの一致比較であり、そのreference自体の正当性の証明ではない。
inline表・shortest path・integer referenceの今回のCOMPLETEDは0で、未確認を失敗や成功に置換しない。
source真正性・全行存在・外部payer acceptance・送信完了を保証するものでもない。

## 6. Verification・Integrity

- 104 tests PASS、skip 0。追加4 testsは取得前rule、ID/known answer非依存、探索維持、GET費用公平性、
  FIFO、cache hitの0費用、余剰枠融通、128 GET上限、全未試行Taskのledger対象維持を確認。
- Batch 8 71件、Batch 10 80件、Batch 12 154件、Batch 13 209件、計514 Frozen Taskをoffline replay。
  過去対応replayとresult object / verifiedが全件一致、False Complete 0。
- live 101 Taskを2つのlocal stateでoffline replayし、結果一致・duplicate・SQLite終端・auditを確認。
- CLI Material smoke 3件PASS。synthetic / literal inspectionをnative COMPLETEDへ加算していない。
- 開始時4005ファイル中4004がhash一致。既存差分は取得driverだけで、全srcとHistorical Evidenceは不変。
- live直前31 Pythonファイルlockは取得後・検証後も一致。live結果を見た実装修正なし。

全raw・Frozen・attempt/response・取得順ledger・test log・regression・offline diagnosticは `.local/batch14/` に保存。
Private原本 `material-fetch-efficiency-batch14-20260919.json`／配布版の派生summary `material-fetch-efficiency-batch14-20260919.public-summary.json`（原文・再実行fixtureではない） にTask別GET帰属・結果・proof・主要artifact hashを収録した。
既存replay summaryの歴史的 `batch: 7` labelは変更せず、本plan / path / ReportでBatch 14を区別する。

## 7. 次の判断

長時間Read-only / 24H Runtimeへの移行はまだ推奨しない。

1. 同一Noteの重複や明確に不要な追加GETという削減余地は、現在のEvidenceではほぼ尽きている。
   これ以上はunknown資料を読まないというcoverageとのtrade-offになる。guardを緩めて効率を作らない。
2. cost-awareな2枠配分は小さな改善として維持するが、実運用での128枠飽和条件は今回は未確認。
   boundedな追加観測で確認し、比率や予算を今回の3成功に合わせて調整しない。
3. export timeoutでgap589が残り、Intakeの通信時間制約が再び観測範囲を制限した。
   単発から恒常障害と断定せず、まず再現頻度・取得bytes・deadlineを観測する。
4. 128 GETを超える需要がBatch 13で確認できても、それだけでbudget増量・常駐化の安全性は示せない。
   必要なら将来、明示したread rate・byte・時間・探索coverageとの釣合いを別の有限実験で検討する。

本Batchでは追加のGeneric Infrastructure、retry基盤、Solver、24H daemonを作らず、
分析で裏付けられた取得順序の変更と、その限定付きの検証・実観測で完了する。
