# Collaboration Agent v0.1

独立した、単発実行型のOffline Collaboration Agentです。Humanが選んだTaskと保存資料を固定し、
既知のルールだけで処理して、根拠付きResultとHuman PreviewをSQLiteに残します。
対応不能は推測せず `UNKNOWN`、不正入力・運用上の停止・中断は `HUMAN_REVIEW` です。
`COMPLETED` はローカル処理の完了であり、Human承認・投稿・署名・相手方の合格ではありません。

Python 3.12以上、Linuxのローカルfilesystemで動作します。Python標準ライブラリだけを使用し、
install、APIキー、Dockerは不要です。Solver実行はOfflineです。
明示的な公開snapshot/Material取得コマンドだけが、限定した公開sourceへHTTPS GETします。

## 実行

monorepoルートから `cd components/collaboration/agent` で移動し、以降はこのComponentディレクトリで実行します。`init` は新規の専用ディレクトリだけを作成し、既存状態を上書きしません。

```bash
PYTHONPATH=src python3 -B -m collaboration_agent --state .local/manual init
PYTHONPATH=src python3 -B -m collaboration_agent --state .local/manual run examples/math.json --dry-run
PYTHONPATH=src python3 -B -m collaboration_agent --state .local/manual run examples/math.json
PYTHONPATH=src python3 -B -m collaboration_agent --state .local/manual preview example-math
PYTHONPATH=src python3 -B -m collaboration_agent --state .local/manual run examples/exact.json
PYTHONPATH=src python3 -B -m collaboration_agent --state .local/manual run examples/extract.json
PYTHONPATH=src python3 -B -m collaboration_agent --state .local/manual run examples/lines.json
PYTHONPATH=src python3 -B -m collaboration_agent --state .local/manual run examples/unknown.json
PYTHONPATH=src python3 -B -m collaboration_agent --state .local/manual audit
```

`run` は1ファイルにつき1Taskです。CLI出力はJSONで、端末制御文字を含むUnicodeをescapeします。
日本語も `\u...` で表示されます。`preview` は固定Task全文、引用、Result digest、未承認の表示を含みます。
`audit` / `status` は整合性検証後の件数・audit head・現在のpolicyを表示します。
Taskの3状態は終了コード0の正常な判定結果です。ファイル・DB・lock・容量等の運用エラーは終了コード2です。
終了コード0だけを `COMPLETED` と解釈しないでください。

## 現在のCapability

| Family | Params | 動作 |
| --- | --- | --- |
| `exact.match` | `candidate`, `reference`: source ID | 2つの保存本文を完全一致比較。大小文字・空白・改行の補正なし |
| `json.extract` | `source`, `pointer` | JSON Pointerで整数・文字列等を抽出。整数を丸めない。選択値に小数を含む場合は拒否 |
| `text.lines` | `source`, `first`, `last` | 1始まり、両端を含む行引用。引用内の改行はLF |
| `math.gcd_lcm` | `source` | `Compute gcd(12, 18) and lcm(12, 18).` 形式だけを処理 |
| `math.normal_nim` | `source`: full-spec ID | 既知のnormal-play Nim全文Template。1〜32山・各0〜20桁整数。合法な勝ち手または `none` を独立検証 |
| `docs.quoted_limit` | `spec`, `document`: source ID | 既知のmessage文字数/wait秒数質問のみ。確認済み文書digestに限定し、上限値を本文から引用・検証 |
| `docs.agent_fields` | `spec`, `document`: source ID | 確認済みagent.jsonへの3つの完全一致質問のみ。namespace note数・schema_version・ephemeral接頭辞を本文から引用・検証 |
| `public.validation` | なし | 公開validationのうち、earliest/latest seqの整数2値をreferenceと順序付き比較する全文Templateのみ |
| `validation.integer_reference` | `source`: full-spec ID | 既知のmodular inverse問題の整数referenceとworker値を比較。最大20桁、operand/range整合と回答の独立検査 |
| `tables.inline` | `source`: full-spec ID | 完全な6列inline表1〜64行。ASCII payer順/数値seq順、またはearliest/latest時刻のseq。重複・切断・未知操作を拒否 |
| `math.shortest_path` | `source`: full-spec ID | 2〜16頂点、正整数重みの無向単純グラフ。DijkstraとFloyd–Warshallで距離を独立検証 |

数学の4つの数値は0〜100桁の非負整数で、gcdとlcmの引数は一致必須です。
前後への指示追加、別書式、他の計算問題は `UNKNOWN` です。
完全一致で不一致なら、判定処理は `COMPLETED`、`verdict` は `MISMATCH` になります。
JSONのpathがなければ `UNKNOWN`。抽出されたJSON nullは `COMPLETED` として区別します。
Batch 2の `json.extract@2` は、資料内の選択対象外の小数をDecimalで正確に保持して読めます。
選択値に小数を含む場合、Task入力自体のfloat、重複JSON key、NaNは引き続き拒否します。

`examples/extract.json` はTechnocore公開room exportに似せた**未署名の合成fixture**です。
19桁nonceの保持を確認できますが、署名検証・live互換・tclk対応を示すものではありません。
全fixtureは新規作成した合成データで、実Secret・実Signer・実資産は使いません。

## Task / Evidence入力

各exampleを雛形にHumanがTaskを作成します。現時点で自然言語からの自動Task作成はしません。

- 最上位は `version: 1`, `task_id`, `family`, `params`, `evidence` のみ。
- `evidence` は1〜8件。各sourceは `id`, `locator`, `text`, `sha256` のみ。
- `sha256` は `text` のUTF-8 bytesのSHA-256。改行もhashに含みます。
- `locator` は保存元の説明文字列。URLやpathを書いてもAgentは開きません。
- 入力およびcanonical JSONは256,000 bytes以下、source本文は各32,000文字以下、JSON深さは32以下。
- 未知field、重複source ID、重複JSON key、NaN、float、digest不一致、symlink、非通常ファイルを拒否します。

source digestは保存資料との対応を示します。出典の真正性・公式性・新しさを証明しません。
任意のsourceを「verified」「official」と自己申告して信頼度を上げるフィールドはありません。
実Secretや個人情報を入力しないでください。この版は自動DLPや秘匿ストレージを提供しません。

## Policy / 復旧

初期値はrolling 1時間60件・24時間500件、同時Solver実行1件です。
別設定で始める場合は `init --per-hour 10 --per-day 50` を指定します。0は実行停止です。
制限は**このstate内**で共有します。複数stateを横断する制限はありません。

```bash
PYTHONPATH=src python3 -B -m collaboration_agent --state .local/manual family math.gcd_lcm suspend --reason contract-needs-review
PYTHONPATH=src python3 -B -m collaboration_agent --state .local/manual family math.gcd_lcm resume --reason contract-reviewed
PYTHONPATH=src python3 -B -m collaboration_agent --state .local/manual family text.lines disable --reason operator-choice
PYTHONPATH=src python3 -B -m collaboration_agent --state .local/manual family text.lines enable --reason operator-choice
```

`enable` はsuspensionを解除しません。`resume` はdisabledを解除しません。
制限やfamily停止による見送りは未実行なので、解除後に同じTaskを処理できます。
通常実行ではTask IDと内容を固定し、同じIDを別内容に転用すると `HUMAN_REVIEW` です。
Task IDだけを変えた同一内容も重複として既存Resultを返し、Solver・quotaを再消費しません。
重複Resultには最初のTask IDが残り、別名の `preview` からも参照できます。

dry-runはpolicyを適用して計算結果を表示し、ローカルauditにTaskとResultを保存します。
新規Taskの通常admission・ID固定・quota消費はしません。dry-run自体の繰返し計算は可能です。
保存済みResultがある場合はそれを返し、外側の `dry_run` が今回の指定、Result内部は元のprovenanceを示します。
dry-runのみのTaskは `preview TASK_ID` の対象外で、run出力とDBのdry-run eventが確認資料です。

中断後は次のCLI起動時に未完了admissionを `HUMAN_REVIEW / interrupted_execution` に確定します。
自動retry・quota返却はありません。familyのresumeでも中断Taskは再実行しません。
既知Solverの失敗やUNKNOWNへの到達も、開始済みならquotaを消費します。
未知familyはSolverを呼ばず、quotaを消費せず終端UNKNOWNとして保存します。
時計が過去のaudit時刻より戻れば新規実行を停止します。

DB破損・hash不一致・状態欠落は空の状態へ作り直さず停止します。
stateディレクトリを退避して調査し、DBを編集したり新stateへ作り替えて制限を回避しないでください。
実行中はlockを保持するため、policy変更も同時操作を拒否します。

## Test / Smoke

```bash
python3 -B -m unittest discover -s tests -v
python3 -B tests/smoke.py
python3 -B tests/material_smoke.py
```

Testは一時ファイルを `.local/test-*` に作成し終了時に片付けます。
Smokeは新しい `.local/smoke-*` にSQLite、5件のPreview、`summary.json` を残します。
既存CCWや調査用RepositoryはTest・Runtimeから参照しません。

## 境界と次のGate

Solver実行経路にはネットワーク取得がありません。別のMaterial Resolverが限定GETを行い、
Taskと資料をfreezeした後に既存Agentへ渡します。Signer、Wallet、tclk parser/state machine、LLM Runtime、
自動返信、daemon、cron、Autopilot、外部投稿・承認機能がありません。
将来のExternal WorkerはPreviewの未対応表示だけで、呼出しは行いません。
独立したsemantic Verified Answer Cacheは未実装です。同一TaskのResult再利用だけを行います。
CCWのOS sandboxは移植していません。信頼するローカルPythonコードに対するOS隔離は主張しません。

Batch 2/3で実公開TaskのOffline評価と限定資料取得を確認しました。
Batch 6で公開validation 2件、normal-play Nim 1件、文書上限質問3件、計6件・3 familyのローカル完了に到達しました。
次は未見の公開Taskによる3 familyの再評価、または完全な資料とTask bindingが揃う新しい表Taskが候補です。
External Write前には対象・最終bytes・期限・単回承認・署名鍵境界・nonce・受領証・曖昧結果照合を、
Autopilot前には対象family・相手との関係・停止基準・rate・監査保持・監視責任をHumanが判断します。

詳細: [設計](docs/design.md) / [先行調査と再利用](docs/references.md) / [初回checkpoint](docs/checkpoint-20260918.md)

## Batch 2: 実公開データ評価

2026-09-18に `tclk-offers` を固定URLへの読取GET 1回で200 records取得しました。
資料処理と実際の仕事を分けた評価結果は以下です。

| 評価 | cases | 正しいCOMPLETED | HUMAN_REVIEW | UNKNOWN | False Complete |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline | 226 | 174 | 21 | 31 | 0 |
| 同Dataset修正後 | 226 | 190 | 5 | 31 | 0 |
| 未使用recordのHoldout | 67 | 58 | 0 | 9 | 0 |

COMPLETEDは公開recordに対して評価者が指定したfield抽出・比較・引用です。
**公開仕事そのもの25件の完了は0件**で、本文・資料不足18件、不完全なpreviewと未対応処理7件をUNKNOWNとしました。
署名／tclk検証は行っておらず、全recordはSOURCE_UNVERIFIEDです。
16件の失敗を根拠にJSON抽出だけを改善し、新family・LLM・cache・送信機能は追加していません。

Batch 2の研究用 `tests/collect_public.py` は固定URLからの公開読取を行います。
Agentからは呼びません。通常Test／Smoke／Batch 2評価runnerはOfflineです。
Frozen Datasetと元bytesは `.local/batch2/`、再現手順・digest・限界は
[Batch 2評価報告](docs/public-evaluation-20260918.md)、集計は
[machine-readable summary](docs/public-evaluation-20260918.json) を参照してください。

## Batch 3: Task Material Resolver

public note / full-spec参照 / 公式文書をallowlist・IP検査・15秒timeout・128 KiB本文上限付きで取得し、
元bytes・provenance・Task bindingをdigest付きでfreezeします。run上限32 fetch / 2 MiB、
Task上限4 fetch、参照depth 2、redirect 0。認証、proxy、GET write path、未知hostは拒否します。
`material_cli resolve` は取得あり、`material_cli run` は固定資料だけで再生します。

25 public Taskは「参照欠落13、直接note参照5、preview + full spec 7」に分類されました。
旧資料不足18件中5件を解消し、全体10件がAgent/classifier、1件が登録Solverへ到達しました。
**元Taskの完了0、HUMAN_REVIEW 1、UNKNOWN 24、False Complete 0**です。
HTTP 200でもpreview不整合1件・引用表の切断疑い1件を止めています。
別枠のHuman選択による公開資料行引用3件は正解でした。元Task完了へは加算しません。

既存32件を含む49 tests、既存Smoke、Material smoke、25件のFrozen再生一致を確認しました。
署名・tclk検証はなく、全資料は `SOURCE_UNVERIFIED`。
実行例、取得policy、完全性の保証範囲、Holdout、Evidence digestは
[Batch 3報告](docs/material-resolver-20260919.md)、集計は
[machine-readable summary](docs/material-evaluation-20260919.json) を参照してください。

評価原本に含まれる生引用・Task別proofと、以下のFrozen Dataset再生はPrivateの保存入力を対象にします。
配布版の `*.public-summary.json` は原文を省いた派生集計であり、これを入力に再生することはできません。
Public checkoutには同じPrivate入力・履歴があると仮定しないでください。

## Batch 4: Empirical Solver / 実公開Taskの回答完了

同じ25件のFrozen Datasetで **正しいCOMPLETED 0 → 2、False Complete 0**。
`native-6343727` と `native-6343730` は、提示されたreferenceとworkerのearliest/latest seqを
比較してPASSと説明1文を返す仕事です。元の表の計算、投稿、署名、相手方receiptは含みません。
全体はCOMPLETED 2 / HUMAN_REVIEW 1 / UNKNOWN 22。Material/classifier到達10、Solver到達5。

新Solver `validation.ordered_seq_pair@1` は `public.validation` に登録しています。
原文のTask・done条件・board末尾を全文照合し、整数2値以外、入れ子、追加指示はUNKNOWNです。
referenceは公開noteに提示された比較基準で、実Secretの取得はありません。
合成fixtureの値や実Task IDによる回答分岐はなく、値は毎回保存Materialから読みます。

```bash
python3 -B -m unittest discover -s tests -v
python3 -B tests/evaluate_empirical.py --output .local/batch4/replay-new
```

出力先は未作成pathを指定。Batch 2 snapshot/datasetとBatch 3 bundlesが必要です。
runnerは元Offerまでhash照合し、独立の回答検証、全25件のfresh-state再生一致、重複、auditを確認します。
従来の20/5分割は維持しましたが、今回分類時に両方を読んだため未見Holdoutの主張はしません。
55 testsと既存/Material SmokeがPASS。取得・外部write・LLM呼出しはありません。

古いstateには新familyのpolicyがありません。その場合 `HUMAN_REVIEW / family_not_configured` で停止し、
自動有効化やpolicy移行はしません。既存stateのUNKNOWN/重複結果も書き換えません。
上記評価runnerの新stateは比較実験用で、運用quotaや既存結果を回避する仕組みではありません。
Batch 3 runnerの「全native completionを境界違反と数える」旧評価契約は保存しているため、
新Capabilityの評価にはBatch 4 runnerを使用してください。

分類、優先順位、Failure、再利用記録、再現結果は
[Batch 4報告](docs/empirical-solvers-20260919.md) / Private原本 `docs/empirical-evaluation-20260919.json`／配布版の派生summary `docs/empirical-evaluation-20260919.public-summary.json`（原文・再実行fixtureではない）。

## Batch 5: normal-play Nim / 2つ目の実Task family

**実公開COMPLETED 3件 / 2 family / False Complete 0**。
`native-6343749` の `[20, 33, 16, 20]` に `heap 2 to 16` と回答。
合法な1山の減少、変更後 `[20, 16, 16, 20]` のNim和0を独立検証し、SQLiteのCOMPLETEDまで確認しました。
元のvalidation 2件を含む他24件はBatch 4と結果が完全一致。全体はC3 / H1 / U21です。

Nim Solverは既知のfull-spec全文とdone条件に限定。misère、複数山操作、曖昧な数値、追加指示は拒否します。
XORで生成した回答を別の合法手・ビット偶奇検査に通し、不合格ならHUMAN_REVIEW。
評価器はさらに独立parserで89合法手を列挙し、この実Taskの勝ち手を確認しました。

```bash
python3 -B tests/evaluate_empirical.py --output .local/batch5/replay-new
```

最新runnerはBatch 5の評価契約です。出力先は未作成pathを指定してください。
Batch 3のFrozen compiler digestは元の形式で検証した後、実行時だけ厳密一致するNimを専用familyへ割り当てます。
元bundleや過去stateは書き換えません。旧stateで同じIDが旧分類に固定済みなら `task_id_conflict`、
新family policyがなければ `family_not_configured` で停止します。新stateは比較実験のために使用します。
62 tests、25件replay、既存/Material Smoke PASS。未見Holdoutの主張は引き続きありません。

[Batch 5 checkpoint](docs/empirical-solvers-20260919.md#batch-5-checkpoint-normal-play-nim) /
Private原本 `docs/empirical-evaluation-batch5-20260919.json`／配布版の派生summary `docs/empirical-evaluation-batch5-20260919.public-summary.json`（原文・再実行fixtureではない）。

## Batch 6: 文書上限の引用 / 3つ目の実Task family

**実公開COMPLETED 6件 / 3 family / False Complete 0**。
message文字数上限2件に加え、同じBatchでwait秒数上限1件も対応しました。
元のfamilyラベルapi/document/protocolを、既知の質問全文で `docs.quoted_limit@1` へ分類します。
回答はそれぞれ `"Messages ≤ 4096 chars"` と `"10"`。保存文書の値を引用する仕事の完了です。

質問・done・末尾は全文照合し、文書URLと確認済み全文SHA-256を固定します。
messageとnoteの文字数、HTTP body/URLのbyte制限を区別。waitはWAITING節とclamp記述の上限を照合します。
文書の変更・切断・追加の主張は、digestを再計算して渡してもUNKNOWNです。新しい文書版には再確認が必要です。
値自体はhardcodeせず本文から抽出し、別parserによる検査とliteral行引用の検証に通します。
現在のdeploymentや過去の稼働値を保証するものではありません。

```bash
python3 -B tests/evaluate_empirical.py --output .local/batch6/replay-new
```

最新runnerはBatch 6契約。既存3件を含む他22件はBatch 5とResultが完全一致。
全25件はC6/H1/U18、Material/classifier到達10、Solver到達8です。
69 tests（skipなし）、25件の独立state再生、既存/Material Smoke PASS。
実文書のintegration testはFrozen入力がなければskipしますが、Empirical replayは入力欠落時に失敗します。

残19件は資料不足、曖昧なvalidation、認証metadata不足のfold、外部署名投稿で停止条件に達しています。
詳細な停止根拠と再現Evidence:
[Batch 6 checkpoint](docs/empirical-solvers-20260919.md#batch-6-checkpoint-source-bound-document-limits) /
Private原本 `docs/empirical-evaluation-batch6-20260919.json`／配布版の派生summary `docs/empirical-evaluation-batch6-20260919.public-summary.json`（原文・再実行fixtureではない）。

## Batch 7: Fresh Public Task評価

新規200 records中22 Offerを取得し、job欠落1件を除く21 Taskと参照資料を、Solver変更前にfreezeしました。
旧25 TaskとのOffer重複は0件です。未変更Baselineは **C0/H1/U20、False Complete 0**。
既存3 Templateの該当Taskがなく、既存CapabilityのFresh再現は未確認です。

同じ取得済みagent.jsonへの3つの質問を根拠に `docs.agent_fields@1` を追加し、
Freshは **C3/H1/U17、False Complete 0**。回答は `"300000"`、`"0.1"`、`"e-"` です。
質問全文・文書URL・確認済みdigestを固定し、JSONからの抽出、独立の行照合、評価器による引用検証を通します。
未確認文書版はUNKNOWN。既存stateのUNKNOWNを上書きせず、評価専用の新stateで比較しています。

これは追加Capabilityの開発に使ったDataset上の結果です。未見Holdout成功とは主張しません。
旧25件は全ResultがBatch 6と一致し、6件完了を維持。76 tests、既存/Fresh Material SmokeもPASS。

```bash
python3 -B tests/evaluate_fresh.py replay --output .local/batch7/replay-new
python3 -B tests/evaluate_empirical.py --output .local/batch7/regression-new
```

出力先は未作成pathを指定。Fresh snapshot・freeze・旧snapshotは `.local/batch7/` と `.local/batch2/` に必要です。
`evaluate_fresh.py acquire` だけは明示的な公開GETを行い、実装lockと照合して取得・freezeします。
`--baseline` は開始時実装digestに一致する場合のみ使用でき、変更後の結果をBaselineと名付けられません。

[Batch 7報告](docs/fresh-evaluation-batch7-20260919.md) /
Private原本 `fresh-evaluation-batch7-20260919.json`（公開版では除外、追加summaryなし）。

## Batch 8 / 9: Strict観測とbounded family追加

Batch 8はコード無変更で新規Offer71件を評価し、exact Template一致0、C0/H13/U58、False Complete 0でした。
[Strict評価報告](docs/strict-fresh-evaluation-batch8-20260919.md)の取得範囲・失敗sample・Baselineは保存しています。

Batch 9では反復した入力構造を根拠に、整数reference比較、6列inline表2操作、正重みグラフ最短経路を追加。
**実公開COMPLETED 9→15（+6）、内容family 3→4、False Complete 0**。
Batch 8で5件、旧Batch 7で1件を新たに完了し、既存9 Completionを含む他111 Resultは不変です。
登録Solver familyは4→7。入力値・行数・順序・辺構造を一般化し、Task IDや回答値で分岐しません。

これは観測済み117件に対するDevelopment評価で、未見generalizationではありません。
86 tests、全117件の二重state replay、既存/Material Smoke PASS。
資料・binding不足は維持し、旧stateやFrozen入力は上書きしません。

[Batch 9のbounded範囲・checkpoint](docs/bounded-evaluation-batch9-20260919.md) /
[117件の集計と新6件の独立proof](docs/bounded-evaluation-batch9-20260919.json)。

## Gate B: Real Project DID接続のみ（Human実行待ち）

Gate A後のReal credential handoff・DID一致・credential隔離・Transport write OFF確認は
[Gate B手順・Evidence・STOP/rollback](docs/gate-b-real-host-20260922.md)を参照。
匿名pipeの生32-byte seedはHuman terminalだけが渡し、CodexはSecretへアクセスしない。
`gate_b.py`は既存handoff/Signer/Transportを再利用し、成功時も全停止・暗号文除去・DUMMY設定復元まで行う。
新規Real ledgerは保存される。検証済みの空identityだけは次回起動で再利用し、admit可能とする。
既存の義務・custody・不確定stateがあれば復旧専用または起動拒否。Gate B再実行や旧DUMMY復旧ツールの使用はしない。
Real ACCEPT・External Writeは今回の範囲外。host実測はまだ未実施。

Gate B後の空identity再利用と回帰検証: [P1-1局所修正](docs/gate-b-empty-identity-fix-20260923.md)。

## Gate C: First Paper ACCEPTのHuman orchestration（実行・独立レビュー待ち）

[Gate C手順・offline検証・STOP](docs/gate-c-first-accept.md)を参照。
C1は固定public GETからHuman選択したOFFERのexact ACCEPT packetを固定して停止する。
C2は別commandでArmとHuman final approval後に最新の公開情報を再検証し、
そのpacketのadmitをCommit Pointとして一度だけ送信する。C1の30秒deadlineは引き継がない。
送信後はAMBIGUOUS/STOPとし、公開exact recordを照合する。Delivery・REVEAL・valueへは進まない。
今回の検証はfake/offlineのみ。Real Secret access=0、実External Write=0。
ClaudeレビューはHumanが実施するため、[独立レビュー資料](docs/gate-c-independent-review.md)を用意した。
