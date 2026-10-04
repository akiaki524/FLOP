# Batch 11 — Material / Intake Gap Analysis

評価原本・生引用・Task別proofの記述はPrivateの当時の保存入力を対象にする。配布版の派生summaryは原文・完全なproof・再実行fixtureを提供しない。

## 結論

Batch 10のMaterial不完全73件の最大要因は、**HTTP応答を取得できなかった36件**である。
Task / Note不存在を示すEvidenceではない。同じ旧コード・同じ公開URLの対照実験では、制限環境でDNS失敗、
許可済みnetwork環境でHTTP 200を再現した。取得失敗を粗い `FETCH_FAILURE` にまとめ、集計に理由を出さない観測上のBugを修正した。

元のOfferを保ち、sample 02/03のMaterialだけを現在時点で再取得した診断では、全80件の比較集計が
Material完全 **7→28**、COMPLETED **0→2** となった。これは新しいStrict Fresh評価でも、過去の成功の発見でもない。
Batch 10 original strict resultは **C0 / H1 / U79** のまま保存する。

既存197件のFrozen / Development replayは修正前結果と全件一致、既存COMPLETED 15件を維持。
現在再取得した58件では2件COMPLETED。独立verifierで確認したFalse Completeは全体で **0**。
Batch 9追加3CapabilityのStrict Fresh generalizationは、今回も証明していない。

## 開始状態・Boundary・Evidenceの区分

- 開始Git: `main`、`1b06b0bb89b2545dba2f4d578946ed881c73a9d2`、worktree clean。
- 変更はtransportのDNS分類、取得summary、回帰test、本Report / JSONのみ。Solver / Classifier / parser / acceptanceは変更なし。
- Batch 8 / 10のReport・snapshot・Frozen Material・original replayは変更していない。開始時795ファイルのhashを保存し、既存変更は上記3ソース/testファイルだけ、792ファイル一致を確認した。
- 外部操作は公開GitHub調査と公開Technocore GETだけ。外部Repositoryのコードは読んだだけで実行していない。
- External Write、ACCEPT / LOCK / REVEAL / REFUND、Signer、Wallet、Secret access、paid API、LLM / CCW invocation、Production mutation、Autopilot、push / publishは行っていない。
- 全取得sourceは従来どおり `SOURCE_UNVERIFIED`。COMPLETEDはoffline判定であり、payer合格やProduction receiptではない。

機械可読EvidenceはPrivate原本 `material-intake-gap-analysis-batch11-20260919.json`／配布版の派生summary `material-intake-gap-analysis-batch11-20260919.public-summary.json`（原文・再実行fixtureではない）。73件の原因行、209件のrouting監査、27件の表計測、
対照実験、replay比較、関連local artifact 40件のSHA-256を収録した。raw Evidenceは `.local/batch11/` に保持する。

## 1. Batch 10の不完全73件

| 元の理由 | 件数 | Evidenceで分かる具体的原因 |
| --- | ---: | --- |
| FETCH_FAILURE | 36 | sample 02の22回、03の14回。statusなし、headers空、raw 0 bytes。HTTP応答取得前の失敗。元の例外種別は保存されていない |
| MISSING_TASK_REFERENCE | 27 | jobにid / protoだけでcontextなし。24件はa2aだが大文字を含むjob ID、3件は別protocol（pin 2 / kibble 1） |
| UNRESOLVED_REFERENCE | 4 | HTTP probe型のask。任意GET /sayやPOST等の操作記述であり、許可済みMaterial参照の契約ではない。ask中の操作は実行しない |
| MALFORMED_REFERENCE | 3 | 同じNote URLに `#sha256=...` fragment。現在の参照構文では非対応。fragment除去や別URLの推測はしない |
| SOURCE_NOT_ALLOWED | 1 | auth.mdがclosed allowlist外。Materialが存在しないという意味ではない |
| TRUNCATION_SUSPECTED | 1 | native-6852392のcited tableが7440文字で既存7000文字guardに到達。実際の行欠落自体は未証明 |
| INCOMPLETE_SPEC | 1 | native-6852438のlattice-path問。配送wrapperが「deal room OR on this board…」でcanonical SPEC不一致。演算も既存未対応 |
| 合計 | 73 | 不存在、未取得、非対応参照、安全guardを区別する |

Batch 10 originalには、確定したHTTP 404 / 5xx / TIMEOUT、fetch budget超過の記録はない。
36件をmissing、timeout、DNSなどに推測で再配分しない。元のtimeoutには別の `TIMEOUT` labelが既に存在する。

### fixed-originの未試行候補

外部参考実装で使われる `/kv/tclk-job-en/<job_id>` は、validなjob IDと対象protocol等の条件が揃って初めて適用できる。
contextなし27件では、a2a 24件のIDが現在のNote key規則 `[a-z0-9][a-z0-9_-]{0,47}` に適合しない。
残り3件は別protocolであり、同じ命名規約を保証するEvidenceがない。従って、この27件に対する**安全に根拠付けられたfallback候補は0**。
大文字の小文字化、suffix除去、namespace推測によるGETは行わず、generic fallbackを追加しなかった。

### Batch 8は同じ原因か

Batch 8の54 Material GETは全てHTTP 200、合計189205 bytes。
不完全49件は参照なし22、preview mismatch 13、unresolved 8、truncation疑い4、malformed 1、fetch budget 1。
Batch 10 sample 02/03の応答前失敗とは別の欠測構造であり、まとめて「network failure」とは扱わない。

## 2. `Material bytes = 0` の正体

`bytes` はFetcherが受け取った**生のHTTP body byte数の合計**。Task数でも、replayの読込み量でもない。
元の `attempt-*.json` / `response-*.json` を対応させると、sample 02/03の36回は全て
`http_status=null`、`fetch_error=FETCH_FAILURE`、`raw_bytes=0`、`cache_hit=false`。
空blobのSHA-256を持ち、cache reuseでnetworkを省略したケースではない。
DNS解決等で止まれば、fetch試行数は増えてもHTTP GETはwire上に出ない。

旧transportは `OSError` subclassを一律FETCH_FAILUREにしていたため、DNS / connect / TLS等を後から区別できない。
さらにCLIは成功終了し、summaryにfetch error内訳がなかった。評価対象にMaterialがないことと、評価環境が取得できないことを混同しやすかった。

同じURL `https://technocore.chat/kv/tclk-job-76/task-bd4c8f76` で対照確認した。

| UTC時刻 | transport | 実行環境 | 観測 |
| --- | --- | --- | --- |
| 08:54:15 | 旧コード | 制限環境 | `getaddrinfo → gaierror(-3)`、約0.005秒、statusなし / 0 bytes / FETCH_FAILURE |
| 08:55:29 | 同一旧コード | 許可済みnetwork | DNS成功、HTTP 200 / 3358 bytes / errorなし |
| 08:57:50 | DNS分類修正後 | 制限環境 | 同じgaierror(-3)、statusなし / 0 bytes / DNS_FAILURE |

旧コード2実験のtransport SHA-256はともに `3df05682040cc50d3d47c933a4daf215afa30548eccf6ec42600a50777a88ef9`。
Batch 10の実行履歴でもsample 01はnetwork許可環境、02/03はdefault sandboxだった。
この組合せは**制限環境のDNS失敗が主要因だったという強い推論**を支持するが、過去36例それぞれの例外種別を確定するものではない。
現在の200も、過去の同時刻にNoteが存在したことや同一本文だったことの証明には使わない。

## 3. 外部設計参考と本Projectの判断

外部OSSの資料取得・切断対策・失敗分類を設計参考にした。第三者runtimeコードのcopy / reuseは0件。
個別の運用比較と参照元はPrivate開発履歴に保持し、公開向け本文では本Projectの判断を示す。

本Projectには元から404=`MISSING`、503/429=`HTTP_FAILURE`、timeout=`TIMEOUT`の区別がある。
不足していたのはDNS分類とrun summaryの失敗内訳。今回の再現はsandbox DNSであり、
同じ環境でretryしても取得権限問題は解消しない。
自動retry / backoffを新設するEvidenceはなく、既存のrun単位cache・予算・no-auto-retryを維持した。
inline表は行順がshuffledでrange宣言もないため、他実装の末尾距離heuristicを完全性の証明として移植しない。

## 4. routing / family / ask / wrapperの切分け

original Batch 10のMaterial完全7件はfold 4、attestation 1、agent.jsonのdiscover.description質問1、modular exponentiation 1。
fold / attestationは既存deterministic solver対象でなく、attestationの外部書込みも禁止。
docsのフィールドは既存closed question集合の外。modexpは広いmath dispatchには到達するがexact演算grammarに一致しない。
integer reference検証とmodular inverseを計算するTaskも、別のaskである。

Frozen full specを既存recognizerへ直接照合し、family labelだけを既存labelに置換する**非実行の反実仮想検査**も行った。
Batch 8 / 10 originalと現在再取得分の範囲で、label変更だけにより新たに一致したTaskは0。
labelだけで取りこぼしたというEvidenceはなく、familyを広げなかった。

一方、askが既存inline表に一致しても、Material bindingで止まる実例はある。

| Task | 本文文字数 | 論理行 | 止まる箇所 |
| --- | ---: | ---: | --- |
| Batch 8 native-6826996 | 1476 | 12 | PREVIEW_MISMATCH |
| Batch 8 native-6830831 | 1485 | 12 | PREVIEW_MISMATCH |
| 現在再取得 native-6853344 | 1449 | 12 | PREVIEW_MISMATCH |

previewには外部Material Note、full specには「このNote末尾の表」という異なる参照がある。
同じschema / askであっても、同じMaterialだというEvidenceなしに結び替えるのはacceptance変更になるため行わない。
これは単なるfamily表記差ではなくsource bindingの問題である。

native-6853390は1456文字・12行でtime-extremaのask自体は一致するが、preview mismatchに加え、
asset=`?` / proto=`-`という既存typed row契約外の値もある。無関係列を無条件に無視する変更も行わない。
別の完全Taskはlist validation、表の別operator、protocol fold、別docs問等で、現行exact operationの対象外だった。
alternative delivery wrapperの1件を緩めても既存演算に到達するEvidenceはなく、wrapper grammarは維持した。

なお、Batch 10報告の「同じask/done」を「既知本文」とする表現は強すぎる。
fold 4件のMATERIAL transcriptは異なるbody SHA-256を持つため、ask/doneの反復は入力全体の重複を証明しない。
この解釈上の訂正を本Batchに記録し、Historical Report自体は書き換えない。

## 5. Table completeness

[Official README（固定revision）](https://github.com/flop-labs/technocore-chat/blob/e4c4f73f3b28612d7161170b11e08e580b02123a/README.md) は、
message 4096文字、Note 8192文字、保存前の改行等の空白化を記載する。文字数とbyte数を混同しない。
今回計測した表はASCIIなので両者は一致するが、一般のUTF-8 Noteでは一致しない。

現行inline solverは既にflattened六列tableを扱い、全文消費、1〜64行、重複seqや不正値の拒否を行う。
改行がないこと自体は欠落でもparser Bugでもない。27計測すべて物理的には1行だった。

| 例 | 本文文字 / bytes | 観測した完全な列構造の行 | 最終seq | 評価 |
| --- | ---: | ---: | ---: | --- |
| B10 native-6852392 | 7440 / 7440 | 71 | 6542408 | 既存長さguard。行欠落は未証明 |
| 現在 native-6852780 | 8000 / 8000 | 76＋末尾7 token断片 | 5844421 | 末尾不完全を観測。既存長さguardで拒否済み |
| 現在 native-6852792 | 6909 / 6909 | 66 | 5963974 | verification schemaで既存inline六列solver対象外。全体完全性は未証明 |
| 現在 native-6853344 | 1449 / 1449 | 12 | 5955160 | 六列parser一致。ただしpreview bindingで停止 |

行数は固定列の独立計測。値の有効性は別判定であり、字句grammarに一致した部分だけの件数を全行数とは扱わない。
内部の空欄等で計測器のtoken対応が崩れる例はJSONのobserved_rowsをnullにし、部分一致数と未解析token数を別記した。
「判定不能」をtruncation確定としない。8000文字で断片がある例はBatch 8にもあるが、既存guardで止まっている。

観測表に宣言されたseq range / 総行数はない。shuffled行の最終seqは最大seqですらない。
HTTP Content-Length一致も受信bodyの整合だけで、生成元が全行を掲載した保証ではない。
従って7000文字未満の行単位切落しRiskは残る。今回、追加guardで救済すべき既存acceptance内の具体的False Completeは見つからず、
seq heuristicや新しい長さ閾値は実装しなかった。

## 6. 実装した小さなFix

1. `material_fetch.transport`: `socket.gaierror` を一般OSErrorより先に捕捉して `DNS_FAILURE` に分類。例外messageは保存せず、network / local情報を漏らさない。
2. `evaluate_fresh.acquire`: manifestとCLIのnetwork summaryに `fetch_errors` の内訳を追加。cacheに保持された実fetch結果を数え、cache hitで重複加算しない。
3. 回帰test: DNS失敗時にHTTPへ進まないこと、0 bytes / statusなし / error保持、frozenでUNKNOWN、例外本文非保存、404 / 503 / 429 / timeout / DNS / その他失敗の分離、cacheとno-auto-retryを確認。

これ自体は取得成功率を上げるsolver修正ではない。成功率を変えた処置は、**許可されたpublic readを実行可能なnetwork環境で行ったこと**。
正しい環境なら旧コードでも同じNoteを読める対照結果がある。DNS分類だけで過去の空blobを成功へ変換しない。

## 7. post-fix replayと現在再取得の分離

### Frozen / Development regression

| Dataset | Task | C / H / U | 修正前の対応するreplayとの一致 |
| --- | ---: | --- | ---: |
| Development prior25 | 25 | 6 / 1 / 18 | 25 / 25 |
| Development prior21 | 21 | 4 / 1 / 16 | 21 / 21 |
| Batch 8 original sample 01＋03 | 71 | 5 / 13 / 53 | 71 / 71（Batch 9実装のreplay基準） |
| Batch 10 original 3 samples | 80 | 0 / 1 / 79 | 80 / 80 |
| 合計 | 197 | 既存C15を維持 | 197 / 197 |

Batch 8のoriginal strictはC0であり、表のC5はBatch 9開発後の既存replay結果。
今回の修正が5件を新たに成功させたわけではない。
88 unit / regression tests PASS、skip 0。offline replayはsocket禁止、独立した2 stateで一致、到達ケースのduplicate / SQLite終端 / auditも既存runnerで確認した。

### 同じOffer＋現在のMaterialという診断

新しいboard samplingは0回。元のsample 02/03 snapshotをbyte-identicalに別rootへコピーし、既存resolverと同じ
32 fetch / 2 MiB per sample、4 fetch per Task、depth 2、redirect 0で一度ずつacquireした。
失敗時のloop / retryや、成功例の追加samplingはしていない。

| Material再取得 | UTC freeze | 実GET | bytes | cache hit | HTTP結果 | C / H / U |
| --- | --- | ---: | ---: | ---: | --- | --- |
| sample 02 | 08:58:29 | 28 | 170776 | 3 | 全て200 | 2 / 3 / 31 |
| sample 03 | 08:58:42 | 15 | 55899 | 0 | 全て200 | 0 / 5 / 17 |

旧36 root Noteと、本文取得後に初めて解決できた7 dependenciesで計43 GET。
別途、前述の対照実験で1 successful GETと2 DNS失敗試行がある。GitHub調査はこれと別集計。

| 80 Offer比較 | original strict | original 01＋現在再取得02/03 |
| --- | ---: | ---: |
| Material完全 / classifier到達 | 7 | 28 |
| Solver到達 | 1 | 10 |
| COMPLETED | 0 | 2 |
| HUMAN_REVIEW | 1 | 9 |
| UNKNOWN | 79 | 69 |
| False Complete | 0 | 0 |

後列は**混合時点の診断集計**。Fresh Task 80件を新規評価し直した、またはBatch 10 strictを置換したという意味ではない。
現在残るMaterial不完全52件は、参照なし27、unresolved 7、preview mismatch 5、source not allowed 4、malformed 3、
too large for solver 3、truncation疑い2、incomplete spec 1。

2 COMPLETEDは `native-6852823` の既存ordered-seq-pair reference比較と、`native-6852849` のagent.json schema_version引用。
それぞれ既存の独立validation verifier / agent-field verifierで回答と引用を確認した。
前者の整数ペアは今回の入力、後者の質問は既知templateであり、Batch 9追加integer-reference / inline-table / shortest-pathの成功数には加算しない。
現在の完全28件における確認済み既存exact templateは2件だが、missing 52件を含む母集団の真の出現率は推定できない。

### integer referenceの検証層

Batch 9のruntimeは提示referenceとdeliverable整数の比較と、生成した判断文の整合を検査する。
`tests/verify_bounded.py` はruntime grammarをimportせず別parserでTask / 回答 / 引用を検証する独立oracle。
どちらも、author referenceそのものが数学的に正しいmodular inverseであることは証明しない。
今回の新しい診断対象ではinteger-referenceに一致するTaskはなく、このCapabilityの新規成功は0。

## 8. 残ったBlockerと次の判断

追加Generic Infrastructure / broad routing / retry schedulerを先行させず、**取得成否を明示したbounded Read-only Operationへ進める**。
その意味はpublic観測とoffline評価であり、外部受注・送信・Autopilotへ進む承認ではない。

- network応答のないsampleを「Task / Materialが存在しない」と報告しない。fetch count / bytes / HTTP status / error内訳を併記する。
- Batch 9追加3CapabilityのStrict Fresh未確認を維持する。inline表には一致本文を観測したが、Material bindingが成立せずend-to-end再現には数えない。
- source binding不一致、参照のないjob、非対応schema / operatorが残る。新しい対応を考えるなら別Batchで明示した契約と独立oracleを設計する。
- 宣言行数・範囲・生成元digestのないtableの全行存在は保証できない。今回のFalse Complete 0を一般的安全証明にはしない。
- 過去36失敗の例外詳細は復元不能。現在再取得したNoteから過去の存在状態を推測しない。

観測されていないCapabilityを増やすより、今回の観測Bug修正を含む現行fail-closed系で完全Taskの出現Evidenceを集める判断である。
将来のgeneralization評価は別途事前固定し、Batch 8 / 10のHistorical Evidenceへ成功を遡及加算しない。

## 再現手順とlocal artifact

既存runnerで、original dataと現在再取得dataを別root / outputに指定した。
通常のunit testは `python3 -B -m unittest discover -s tests -v`。
Frozen replayは `python3 -B tests/evaluate_fresh.py replay --root <frozen-root> --output <new-output>`。
旧implementation lockと異なるコードなので、今回のdiagnostic replayを旧 `--baseline` 評価とは呼ばない。

`.local/batch11/` の `tests.json`、各 `*-replay.json`、`prior25.json` / `prior21.json` は実行command、exit code、log hashを記録。
`starting-hashes.json` は歴史資料の不変性、`sandbox-before` / `authorized-before` / `sandbox-after` は対照実験、
`reacquire-02` / `reacquire-03` は別freeze、`precedents.json` / `official-source.json` は比較元を保持する。
local集計途中の `inventory.json` と `report-data.json` / v2は最終Evidenceではない。
最終JSONは `report-data-v3.json` の検証済み集計にhash参照とrouting注記を加えたもの。
