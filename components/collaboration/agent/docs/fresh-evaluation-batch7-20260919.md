# Batch 7: Fresh Public Task評価

Fresh 21 Taskの未変更Baselineは **COMPLETED 0 / HUMAN_REVIEW 1 / UNKNOWN 20**。
既存のvalidation・normal-play Nim・文書上限Templateに一致するTaskがなく、
**既存3 familyでのFresh完了は今回確認できなかった**。
その後、繰り返し出現したagent.json引用質問へ限定対応し、**COMPLETED 3 / HUMAN_REVIEW 1 / UNKNOWN 17**。
両段階ともFalse Complete 0。旧25件は6件完了を含む全25 ResultがBatch 6と完全一致した。

COMPLETEDは保存資料に対するローカル回答の完了であり、投稿・署名・相手方receiptではない。
全sourceはSOURCE_UNVERIFIED。署名による作者・資料の真正性を保証していない。

## 開始状態と取得順序

- `main / e891b6e4653739acd6e9e43d9a92ff5ef4b9407c`、worktree clean。
- 開始時src全体・既存collector・独立評価器のSHA-256と抽出方針を `.local/batch7/implementation-lock.json` に保存。
- 方針は最新200 recordsの全Offer。旧snapshotのjob ID、job全体、record本文との重複と、sample内重複を除外する。
  Solverの成功・容易さによる選別は行わない。本文のない仕事も残す。
- `2026-09-19T06:51:17.844654+00:00` に公開board snapshot取得完了。
  seq **6813852〜6814051**、200 records、125430 bytes。旧snapshotはseq6343686〜6343885。
- 22 Offer中、seq6813872はjob欠落のため非実行recordとして理由付きで除外。Fresh仕事21件。
  旧仕事との重複0、sample内重複0。別job IDの同一質問を意味的に新規とは保証しない。
- 既存Resolverの公開allowlist・32 fetch/2 MiB上限・Taskごと4 fetch・depth 2・redirect禁止を維持。
  新規Material GET **12回 / 47976 bytes**、同取得内cache hit 2。旧Material cacheは使わない。
- `2026-09-19T07:04:15.958402+00:00` に全Task・Materialのfreeze manifestを保存。
  取得前後で開始時実装digestが一致した。
- 未変更Baselineを `07:04:28.176237+00:00` に保存。
  `07:05:54.091458+00:00` のbaseline-lockで再固定した後に、初めてSolver/Classifierを変更した。

collectorが出す旧20/5分割候補は今回のHoldout設計には使っていない。
21件すべてをBaseline評価し、その後すべてを分析した。
追加後3件は**開発Dataset上の成功**であり、新Capabilityの未見Holdout成功とは主張しない。

## 観測したTemplateと優先順位

| 実Template | 件数 | Material完全 | 判断 |
| --- | ---: | ---: | --- |
| 参照のないOffer | 11 | 0 | MISSING_TASK_REFERENCE、UNKNOWN維持 |
| 表の偶数seq列挙 | 1 | 0 | preview/full-specの参照表現不一致、HUMAN_REVIEW維持 |
| 最短経路の長さ | 1 | 1 | 既存math非対応。単発観測のため今回は追加しない |
| 正の約数和σ(n) | 1 | 1 | 既存math非対応。単発観測のため今回は追加しない |
| agent.jsonの既知field引用 | 3 | 3 | 同一source/質問形式が反復。閉じた3質問へ対応 |
| 新規公開roomの発見URL | 1 | 1 | 単発の別質問。今回の閉じた3質問外としてUNKNOWN維持 |
| reference foldTranscriptによる最終状態 | 3 | 3 | 表示本文は取得済みだが認証metadataと参照revision不足。UNKNOWN維持 |

文書質問は広い意味では4件あるが、従来のmessage文字数/wait秒数とは質問契約が異なる。
validationとNimは今回0件。従来3 Templateの再現率の分母はいずれも0であり、
「既知Templateを誤って解けなかった」とも「Freshで一般化した」とも結論しない。

新規3件は同一の完全な資料から型と位置を明確に決められ、外部取得・意味推測・新Frameworkなしで検証できる。
反復するfoldは資料契約を満たさず、表はTask binding不整合が先にあるため、文書field引用を優先した。
単発の数学問題やURL質問へ広げるより、今回のBaseline結果を保存し、次の独立sampleで評価する価値が高いと判断した。

## 追加Capability

`docs.agent_fields@1`。新しい汎用JSON質問解釈器ではなく、取得済みの以下3質問のみ。

| Task | 質問 | JSON上の根拠 | 回答 | 独立検証の行 |
| --- | --- | --- | --- | ---: |
| native-6813918 | maximum number of notes allowed per namespace | `/limits/notes_per_namespace` | `"300000"` | 118 |
| native-6813949 | schema_version | `/schema_version` | `"0.1"` | 2 |
| native-6813959 | prefix for ephemeral rooms | `/conventions/room_classes` 内のephemeral記述のkey | `"e-"` | 87 |

引用元URLは `https://technocore.chat/.well-known/agent.json` に限定。
確認済み正規化本文digestは
`05ee7a33a4c9e30b6b8a936f5c727274634a30dbbc4732d4820733a653236e48`。
この版ではnotes全体の上限5242880とnamespace上限300000、製品version 0.13.0とschema_version 0.1、
unlisted `p-` とephemeral `e-` が別々に存在することを確認した。

Classifierはfull-specのfamily・質問・done・board末尾全体を照合して専用Solverへ渡す。
Solverも全文を再照合し、資料2件・異なるsource ID・指定URL・全文digestを確認する。
値は既存JSON parserで本文から抽出し、別のliteral行parserで回答を検査する。
評価器はruntimeの質問patternや抽出関数をimportせず、独立のJSON解析・意味対応・引用位置照合を行う。
誤った回答候補はHUMAN_REVIEW、未確認版・未知質問・追加資料はUNKNOWNへ閉じる。
回答値やTask IDによるruntime分岐はない。合成変更値のtestでも本文から抽出されることを確認した。

Frozen compilerの旧digest契約、duplicate、policy、UNKNOWN/COMPLETEDの意味は変更していない。
新stateは比較評価のためのみ。旧stateの同ID/旧family結果はtask_id_conflictで保護し、
新family policy欠落もfamily_not_configuredで止める。自動移行・既存UNKNOWNの上書きはしない。
第三者codeの新規コピーはない。

## 結果と検証

| Metric | Fresh Baseline | Fresh追加後 | 旧25件回帰 |
| --- | ---: | ---: | ---: |
| Task数 | 21 | 21 | 25 |
| Material完全 | 9 (42.9%) | 9 (42.9%) | 10 |
| Classifier到達 | 9 (42.9%) | 9 (42.9%) | 10 |
| 登録Solver到達 | 2 (9.5%) | 5 (23.8%) | 8 |
| COMPLETED | 0 | 3 | 6 |
| HUMAN_REVIEW | 1 | 1 | 1 |
| UNKNOWN | 20 | 17 | 18 |
| False Complete | 0 | 0 | 0 |

BaselineのSolver到達2件は既存math.gcd_lcmへのdispatch後に未知数学Templateとして停止したもの。
Freshの変更は上記3件だけで、他18件のResultはBaselineと完全一致。
旧25件は全ResultがBatch 6と完全一致し、不当なUNKNOWN/HUMAN_REVIEW→COMPLETEDはない。
合計では46件中9件完了だが、Fresh baseline 0と追加後3を混ぜて「既存Solverの再現」とは数えない。
Freshで完了したregistry familyはdocs.agent_fieldsの1種類。旧3 registry familyは旧資料でのみ回帰確認した。

- 76 tests PASS、skip 0。改変文書/質問、別URL、追加source、不正候補、不正引用、旧state保護を含む。
- Fresh21件・旧25件それぞれ独立した2 stateで再生し、Result一致・audit・duplicateを確認。
- 完了3件は元Offer→bundle digest→分類→回答→独立proof→SQLite終端→再open後previewまで確認。
- `tests/smoke.py` PASS、5 runs、`.local/smoke-7a_1m3c1`。
- `tests/material_smoke.py` PASS、1 case、`.local/material-smoke-od5p4_7a`。
- `tests/material_smoke.py --public .local/batch7/frozen` PASS、3 cases、`.local/material-smoke-bq3h1as8`。
  Material Smokeの行引用は仕事完了数に加算しない。
- replayではsocket使用を禁止。外部write・Signer・Wallet・Secret access・paid API・LLM・pushは0。

## 発見したFailureと制約

1. sandbox内DNS制限で最初のsnapshot取得が失敗。許可済み公開GETをsandbox外で再実行して取得した。
2. job欠落Offerで最初のselectionが停止した。network取得前の停止で、仕事を捏造せず除外理由を保存するよう修正。
3. 11/21件はTask参照がない。報酬やjob名から入力を推測していない。
4. native-6813880のpreviewは `/kv/tclk-mat-en/minf-3d85dac0-`、取得full-specは
   `the table at the end of this note` を指す。既存PREVIEW_MISMATCHを緩めて表集計へ渡していない。
5. fold 3件の行schemaは `room | time | sender DID | frame line`。
   参照foldが要するrecord署名などを欠き、参照revisionも固定されていない。
   本文中frameのnonce相当値を、欠落したtransport認証metadataの代用にしない。
6. 文書は取得時点の引用。現在のdeployment値や今後の版を保証しない。新しい全文digestは再確認が必要。
7. 単一200-record時間窓のsampleで、旧3 Templateが出現しなかった。新Capabilityも未見評価は未実施。

## Evidenceと再現

Raw、取得provenance、実装lock、21 bundles、Baseline、追加後、旧25回帰、SQLite/auditは `.local/batch7/`。
159 filesのhash一覧を `evidence-manifest.json` に保存した（manifest自身は一覧から除く）。
集計・21件のbinding/proofの原本 `fresh-evaluation-batch7-20260919.json` はPrivateに保持し、公開版には収録しません。追加summaryは作成していません。

| Artifact | SHA-256 |
| --- | --- |
| snapshot/raw.json | `e1d44c1cf41e35355fdfb91bb17a2c2678c424adc2a17f46791ab83a5f285dc9` |
| implementation-lock.json | `31982acbb51bf541033d54045ef39b35e5ef6840851606a927aa71a39cafac52` |
| frozen/manifest.json | `a96b96384d13d083ba55e42791a165001d4ef40917a324a817cd511bdac37eb0` |
| baseline/summary.json | `5901576ea2172637fb01690c181ccba8c1d0e67a0e63c7dc14767b3651e7c96f` |
| evidence-manifest.json | `82d2828e98d7cfb924a962a0c933659dd87af5b4db626b4e87eeeefa893139cf` |

```bash
python3 -B tests/evaluate_fresh.py replay --output .local/batch7/replay-new
python3 -B tests/evaluate_empirical.py --output .local/batch7/regression-new
python3 -B -m unittest discover -s tests -v
python3 -B tests/smoke.py
python3 -B tests/material_smoke.py --public .local/batch7/frozen
```

出力先は未作成pathを指定。`.local` の入力はgitには含めない。入力がない場合、replayは成功扱いせず失敗する。
Baseline再現には開始revisionのsrcと既存評価器、および保存した `baseline-evaluator.py` を使う。
`--baseline` はimplementation-lockとの一致を必須にし、変更後実装をBaselineとして再実行できない。
取得済みファイルを上書きする再取得や、追加後ResultでBaselineファイルを置換する操作は行っていない。

次の最重要Milestoneは、**現在の実装をさらに変更せず、事前に固定した別の時刻範囲・取得件数で、
旧3 Templateと今回の文書field Templateの未見Taskを評価すること**。
選別は取得時間/件数で決め、成功するまでsampleを追加しない。該当なしは該当なしと報告する。
表Solverは、完全な表と矛盾しないTask bindingを備えた実例が先に必要である。
