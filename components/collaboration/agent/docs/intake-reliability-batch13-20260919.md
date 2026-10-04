# Batch 13 — Read-only Intake Reliability / Fetch Budget Improve

評価原本・生引用・Task別proofの記述はPrivateの当時の保存入力を対象にする。配布版の派生summaryは原文・完全なproof・再実行fixtureを提供しない。

## 結論

**観測範囲は改善した。Material予算の分断は解消したが、総予算不足は残った。**
Solver / Classifier / acceptance / answer / 独立oracleは変更していない。
有限の公開GET sessionを1回実施し、209 Task、Material完全57件、COMPLETED 7件、
HUMAN_REVIEW 43件、UNKNOWN 159件、独立検証後のFalse Complete **0**。

同一sessionの通常snapshotだけでは131 Taskだったところ、gap回収から78 Taskを追加観測できた。
回収Taskの33件でMaterialが完全、6件が既存SolverでCOMPLETED。
shortest path・inline表も再利用できた。これはIntake改善による観測・実行の直接Evidenceである。
ただし、別のMaterial配分をした場合との比較ではないため「回収がなければ成功が必ず6件減る」とは主張しない。

Material availabilityは49/154（31.82%）から57/209（27.27%）へ**低下**し、
予算打切りTaskは24件から50件へ増えた。取得母集団・時刻・snapshot予算が異なるため単純な因果比較ではないが、
総Material予算が拡大した観測範囲を処理し切れないことは今回も実測できた。
次は長時間化ではなく、未試行Taskの有限な繰越または観測量とMaterial予算の釣合いを対象にした追加Improveが妥当。

## 開始状態・変更範囲

開始Git: `main / 20815d5fecd98d115775dd8a28e3550e5819c344`、worktree clean。
開始時刻 `2026-09-19T09:45:15.199216Z`、src / tests / docs / historical local Evidenceの2428ファイルをhash保存。
既存ファイルの変更は `src/collaboration_agent/material_fetch.py` のみ。

- `material_fetch.py`: 明示opt-inしたIntake呼出しだけに、固定URL `https://technocore.chat/r/tclk-offers/export` を許可。
  上限10 MiB、GETのみ、既存のglobal IP検査・IP固定TLS・15秒deadline・redirect禁止を再利用。
  Materialの `allowed_url()` は無変更で、Task由来export参照は引き続き拒否する。
- `tests/readonly_intake.py`: finite sessionの取得・gap計測・最大600 records回収・FIFO Material予算共有・未試行ledger。
  既存Fetcher / Resolver / freeze / selection / offline replayを利用する。
- `tests/test_readonly_intake.py`: 取得・回収・予算・permission境界の9 regression tests。
- 本ReportとJSON Evidenceを追加。新しいDaemon / Scheduler / Framework / 永続cache / Solverは追加していない。

このrunnerは実験用の有限driverであり、常駐運用製品ではない。session rootに事前作成した
`implementation-lock.json` を要求し、開始時・freeze終了時に検査する。同じrootを再実行して結果を上書きできない。

## 1. 原因と設計根拠

### seq gap: `since` はforward paginationではない

Batch 12は各回最新200 recordsだけを読み、取得間隔が約78 / 75 / 145秒まで開いた。
その間に200件を超えるrecordsが流入すると、窓の間に未観測seqが残る。
公式実装の `read_messages` は、`since` より新しいrecordsを**末尾からlimit件**選ぶ。
`since` を付けて同じtailを繰り返しても、飛ばした古いrecordsには戻れない。
[公式store実装（固定revision）](https://github.com/flop-labs/technocore-chat/blob/e4c4f73f3b28612d7161170b11e08e580b02123a/src/store.py)

公開room exportは保持中のJSONL snapshotと `X-Room-Generation` を返す。
そこで、固定offers roomのexportを最大1回だけ読み、既に観測したfirst〜last seqの**gap内だけ**を回収する方式にした。
上限・generation・重複record一致を確認し、保持期間外やpartial応答は「不存在」の証拠にしない。
[公式export実装](https://github.com/flop-labs/technocore-chat/blob/e4c4f73f3b28612d7161170b11e08e580b02123a/src/app.py)、
[公式manual](https://github.com/flop-labs/technocore-chat/blob/e4c4f73f3b28612d7161170b11e08e580b02123a/src/manual.md)

`openapi.json` のfirst_seq gap説明だけからring evictionと断定しない。
limitによる窓欠測でもgapは生じ、今回は残った8 seqもexportに実在した。
公式コードはAPI意味論の確認だけに用い、コピー・実行・architecture移植はしていない。

### fetch exhaustion: per-poll分断と総量不足を分離

Batch 12のMaterial GETはpoll順に22 / 29 / 32 / 32回。
全体では最大128回のうち115回しか使っていないが、後半pollでは32回上限に達して24 Taskを打ち切った。
前半の未使用13枠を後半へ回せず、exact URL cacheもpollを跨いで再利用できなかった。
byte数は各pollの2 MiB上限より小さく、原因はbytesではなくfetch回数だった。

今回もMaterial総量は**128 GET / 8 MiB**に固定。
4個の既存32-fetch / 2-MiBブロックをsession全体で順に使い、exact URL cacheをsession内だけ共有する。
cacheは最大128 entries、失敗結果も再試行せず保持、別sessionへ持ち越さない。
per Task 4 fetch・depth 2・既存source policyは変更しない。
Taskはseq順FIFOで、family・難易度・答えによる優先選別は行わない。

## 2. 事前固定したbounded Operation

`2026-09-19T09:51:42.480453Z` にsession planを保存し、その後に最初の公開Task GETを開始した。
実装30 Pythonファイルのhashは全session中・終了後とも一致。

| 項目 | 固定した上限・方針 |
| --- | --- |
| snapshot | 6回、30秒間隔、各200 records / 2 MiB |
| URL | 固定offers room、2回目以降は検証済みnumeric `since` のみ |
| recovery | gapがある場合のみ固定exportを1回、10 MiB、最大600 gap records |
| admitted records | snapshot＋回収で最大1800 |
| Material | 最大128 GET / 8 MiB、FIFO、session内exact URL reuse |
| 時間 | network admission 600秒、進行中の1 GETは最大15秒の超過余地 |
| retry / replacement | なし。結果を見た追加poll・上限変更なし |
| epoch / conflict | generation変更・record不一致でfail-closed、混在しない |
| partial export | 完全なJSONL行だけを検査。欠測区間は未解決として残す |

transportは超過検知用にcap+1 byteを読む。exportは最大10 MiB+1 sentinelで、今回の実受信量は上限内。
Materialは各readのsentinelを含めて総8 MiBを超えないよう残予算を渡す。

実行command: `python3 -B tests/readonly_intake.py --root .local/batch13/live`。
事前lock作成と検証commandは `.local/batch13/checks.py`、Task別集計は `analyze.py` に保存。
公開GETに必要な許可済みnetwork環境で実行し、offline replayではsocket使用を禁止した。
session全体は220.450秒で終了。24H稼働・autopilot・自動再開は実施していない。

## 3. 取得結果・観測可能範囲

| page | UTC GET開始 | seq窓 | 有効Task | Material完全 | C / H / U |
| --- | --- | --- | ---: | ---: | --- |
| 1 | 09:51:42.481 | 6901020–6901219 | 14 | 3 | 0 / 3 / 11 |
| 2 | 09:52:12.481 | 6901419–6901618 | 13 | 6 | 0 / 4 / 9 |
| 3 | 09:52:42.481 | 6901774–6901973 | 16 | 3 | 0 / 9 / 7 |
| 4 | 09:53:12.481 | 6902037–6902236 | 32 | 11 | 1 / 10 / 21 |
| 5 | 09:53:42.481 | 6902405–6902604 | 14 | 1 | 0 / 0 / 14 |
| 6 | 09:54:12.481 | 6902628–6902827 | 42 | 0 | 0 / 0 / 42 |
| gap回収 | 09:54:13頃 | 上記窓間のみ | 78 | 33 | 6 / 17 / 55 |
| 合計 | | 1800 records | 209 | 57 | 7 / 43 / 159 |

表の結果は全件freeze後、merged seq順でMaterial取得・offline評価したもの。
各page取得直後にSolverを動かしたという意味ではない。

211 Offerのうちjob欠損/不正1件、session内重複1件を除外した。
Batch 2 / 7 / 8 / 10 / 12の11 historical snapshotsとのjob ID / job全体 / Offer本文重複は0。
1800 recordsすべてをpageのraw hash＋index、またはexportのraw hash＋seqへ照合した。

snapshot間gapは199 / 155 / 63 / 168 / 23、合計608 seq。
600 seqを回収し、残った区間は **6902620–6902627の8 seq**。
この8件は同じexport rawに存在するが、固定600件上限でadmitしなかった。
「APIから失われた」「Task資料がない」とは分類しない。また、Task内容を分類して分母に足していない。

first〜last seq幅1808に対して、snapshotのみ1200/1808＝66.37%、回収後1800/1808＝**99.56%**。
この範囲外、初回snapshot以前・最終snapshot以後のcoverageは保証しない。
exportは保持中の古いrecordsも含む8,815,925 bytesを受信したが、許可したgap外のrecordsはTask化しない。
rawは監査用Evidenceとして残し、任意history探索や繰返しcrawlには使わない。

初回stockは14 Task、以後の初見観測は195 Task。
snapshot完了時刻差150.374秒に対して約77.81 Task/分を観測した。
これは今回の範囲内の観測量であり、全公開Taskの普遍的な流入率やBatch 12からの市場増加率ではない。

## 4. Network・Material・Solverを分けた結果

snapshot GET 6/6成功、export GET 1/1成功、Material GET 128/128がHTTP 200。
Materialは422,371 bytes、cache hit 3、実GETを伴ったTaskは119件。
sessionのHTTP本文受信総量は9,899,467 bytes（snapshot 661,171＋export 8,815,925＋Material 422,371）。
DNS failure / timeout / その他transport failure / HTTP error / 明示404はいずれも0。
Material transport failure率は0/128。これはこの短いsessionに限った結果である。

| Material不完全の理由 | Task数 | 解釈 |
| --- | ---: | --- |
| FETCH_BUDGET_EXCEEDED | 50 | 総128 fetch到達、全50件未試行。Note不存在でもSolver failureでもない |
| MISSING_TASK_REFERENCE | 40 | 参照なし。HTTP 404とは異なる |
| PREVIEW_MISMATCH | 34 | 取得済みfull specとのbinding guard |
| UNRESOLVED_REFERENCE | 10 | 現行resolverで未解決 |
| SOURCE_NOT_ALLOWED | 9 | source policyで拒否、勝手に外へ取りに行かない |
| TRUNCATION_SUSPECTED | 5 | 現行guard、真の切落しを断定しない |
| INCOMPLETE_SPEC | 3 | 現行spec grammar不一致 |
| TOO_LARGE_FOR_SOLVER | 1 | 取得成功とSolver向け上限を区別 |
| 合計 | 152 | 完全57＋不完全152＝209 |

ledgerはMATERIAL_COMPLETE 57、MATERIAL_UNAVAILABLE 102、DEFERRED_UNATTEMPTED 50。
DEFERRED_PARTIALとSESSION_TIME_BUDGET_EXCEEDEDは今回は0。
独立した単体testでは、途中の依存Materialで予算が尽きる場合・初回未試行・DNS failure・503・404を区別した。

未試行Taskのnetwork bytes 0は「read admissionなし」を意味し、空のNote取得や404ではない。
cache利用の場合も新規network bytesは0になり得るため、cache hit / HTTP応答 / error / raw digestを別々に保持する。

Material完全57件のうちSolver到達25件。既存exact対応7件がCOMPLETED、到達した非対応問18件はUNKNOWN、
登録Solverへ到達しない完全Taskは32件。完全TaskのUNKNOWNは計50件。
不完全TaskはUNKNOWN 109件・HUMAN_REVIEW 43件。
**UNKNOWN 159件を一括してCapability failureとは扱わない。**

## 5. Batch 12との比較

| 指標 | Batch 12 | Batch 13 |
| --- | ---: | ---: |
| snapshot / export GET | 4 / 0 | 6 / 1 |
| 観測records | 800 | 1800 |
| 有効Task | 154 | 209 |
| snapshot間gap → 回収後残り | 1345 → 1345 | 608 → 8 |
| 観測seq幅内coverage | 37.30% | 99.56% |
| Material GET / 同じ総回数上限 | 115 / 128 | 128 / 128 |
| Material complete | 49 | 57 |
| Material availability | 49/154＝31.82% | 57/209＝27.27% |
| Material予算打切りTask | 24 | 50 |
| Solver applicability | 21/49＝42.86% | 25/57＝43.86% |
| Solver completion | 7/49＝14.29% | 7/57＝12.28% |
| End-to-end completion | 7/154＝4.55% | 7/209＝3.35% |
| COMPLETED / HUMAN_REVIEW / UNKNOWN | 7 / 27 / 120 | 7 / 43 / 159 |
| False Complete（独立確認後） | 0 | 0 |

Material総read / byte上限は同じだが、snapshot / exportの許容通信量と観測期間・Task分布は異なる。
同予算の完全なA/B実験ではない。Material完全数は8件増加した一方、availability率・budget打切り件数は改善していない。

### 同じBatch 12 workloadでの予算配分diagnostic

別のoffline診断で、元の154 Taskを順番不変でSessionFetcherへ流した。
既存response bytesがあるURLはそれを使用し、当時未取得の21 URLには
`COUNTERFACTUAL_BYTES_UNOBSERVED` を付け、資料の内容・成否・404を捏造しなかった。
ネットワークは一切使っていない。

このread eligibilityモデルでは、旧24件の予算打切りが3件となり、21件で新しくread枠を確保できた。
これは未使用枠共有とexact URL reuseの有効性を示す補助診断であって、**21 Taskの資料完全化・成功の証明ではない**。
未観測bodyに依存参照が含まれる可能性やbyte消費量は不明であり、実際の結果が24→3になる保証もない。
元のBatch 12結果は一切変更していない。ネットワーク失敗を今回の実測件数へ加算してもいない。

## 6. 既存Capability・独立検証

| Capability | COMPLETED | 今回の確認 |
| --- | ---: | --- |
| math.gcd_lcm | 4 | 異なる整数組、独立Bézout証明・積関係・引用検証 |
| tables.inline | 1 | payer順sort、独立column parser・rank検証 |
| math.shortest_path | 1 | 7頂点・9辺、0→6、長さ20。独立Bellman–Fordで照合 |
| docs.agent_fields | 1 | 固定digestの公式JSONからschema_version引用を照合 |
| integer reference / ordered pair / Nim / その他 | 0 | 今回のeligible exact再現なし。未確認を成功へ数えない |

COMPLETED IDは `native-6901221, 6901226, 6901239, 6901263, 6901266, 6902156, 6902361`
（全IDに `native-` prefix）。うちdocs 1件を除く6件がgap回収由来。
Task別結果・source digest・回答・独立proofをJSONに保存した。

inline表 `native-6901239` のfull-spec本文は**1483文字 / 1483 UTF-8 bytes / 物理1行 / 論理12行**。
最終取得行のseqは5179136。payer sortは表内seq順とは別で、元の行順と回答順をEvidenceに残した。
宣言された行数・seq範囲はなく、丸ごと1行が落ちるtruncationを排除する証明にはならない。
今回も既存のCOMPLETE_WITHIN_SCOPEと独立oracleの範囲でFalse Complete 0とし、source完全性の保証と混同しない。

integer referenceのruntimeはauthor reference比較であり、そのreference自体の逆元計算の正当性を証明するものではない。
独立oracleも別実装で比較意味論・回答・引用を検査する。今回は該当COMPLETEDなし。
外部payer acceptance・署名・送信完了は評価していない。全sourceは従来どおりSOURCE_UNVERIFIED。

## 7. Verification・Evidence Integrity

- 最終100 tests PASS、skip 0。新規9 testsでURL / cursor / generation / conflict / cap / partial JSONL /
  session budget / per-task budget / cache / deadline / 未試行と404等の区別を検査。
- Batch 8 Frozen 71件、Batch 10 Frozen 80件、Batch 12 Frozen 154件、合計305件をoffline再実行。
  対応するBatch 11 / Batch 12 post-verificationのresult object・verifiedが全件完全一致。False Complete 0。
- Batch 8 replayのC5は既存Capability追加後の過去replayとの一致。original Strict C0を変更していない。
  Batch 10 original Strict C0、Batch 11診断、Batch 12 original oracle警告と修正後検証も維持。
- 今回209 Taskを2つの独立local stateでoffline replayし、同一結果・duplicate・SQLite終端・auditを検査。
- CLI Material smoke 3件PASS、False Complete 0。synthetic / literal inspectionをnative Task成功数へ加算しない。
- 開始2428ファイルのうち2427ファイルがhash一致。唯一の差分は許可したmaterial_fetch.py。
  Solver / Classifier / Resolver parser / acceptance / evaluator / oracleとhistorical Evidenceは不変。
- live取得前の30 Pythonファイルlockは取得後・検証後も一致。評価結果を見たruntime修正なし。

Frozen replayは既存 `tests/evaluate_fresh.py replay --root ... --output ...` を使用。
歴史的summary内の `batch: 7` labelは変更せず、本sessionのplan / path / ReportでBatch 13として区別する。
`.local/batch13/` にplan・raw・attempt・response・Frozen・ledger・replay・test log・regression・counterfactualを保持。
Private原本 `intake-reliability-batch13-20260919.json`／配布版の派生summary `intake-reliability-batch13-20260919.public-summary.json`（原文・再実行fixtureではない） にTask別原因、回答proof、provenance、主要artifact SHA-256を収録。

## 8. 残ったFailureと次の判断

優先するのは追加Solverでも24H Runtimeでもなく、**観測量とMaterial read admissionの不釣合い**への追加Improve。

- 128 GETの全枠を使っても50 Taskが未試行。FIFOなので後方Taskが偏って未評価になる。
  有限な繰越worklist、またはTask受入量・read予算の釣合いを、明示した容量・期限・read上限の下で別途検討する。
  このBatch中には結果を見て追加fetch・queue・daemonを実装しない。
- gap recoveryは有効だが8 seqを上限で残した。export受信量8.8 MBに対しadmit600 recordsであり、通信費用もある。
  長時間化するとring retentionや総予算で回収できない区間が生じ得る。無制限history scanで解決しない。
- 参照なし40、preview mismatch34等も残る。存在・同一性のEvidenceなしにallowlistやbinding guardを緩めない。
- DNS / timeout / 5xxは今回は0。generic retry基盤を拡張する新Evidenceはない。

Read-only継続観測には価値があるが、現状のまま長時間化しても未試行Taskを積み上げる。
次は対象を絞ったImproveとbounded再観測を推奨する。24H常駐化・permission拡大は行っていない。

公開GET・local Evidence / Test / commit以外の作用は0。
External Write、tclk Action、ACCEPT / LOCK / REVEAL / REFUND、Signer / Wallet / Secret、paid API、
LLM / CCW invocation、Production mutation、Autopilot、push / publishは行っていない。
外部本文はuntrusted dataとして解析し、そこに書かれた操作要求は実行しなかった。
