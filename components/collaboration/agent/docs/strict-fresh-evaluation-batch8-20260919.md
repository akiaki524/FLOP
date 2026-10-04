# Batch 8: Strict Fresh Generalization Evaluation

**コード無変更で評価完了。新規Offer仕事71件、COMPLETED 0 / HUMAN_REVIEW 13 / UNKNOWN 58、False Complete 0。**
取得できたfull-specに既存exact Templateとの一致はなく、既存Capabilityの未見再現は未確認。
この結果を変えるための追加sample、Template拡張、known-answer追加、Task固有patchは行っていない。

開始状態は `main / 0440304f93cf8de7fb2f4b20e47209262f761e2a`、worktree clean。
src/testsの全24 Pythonファイルを取得前にSHA-256で固定し、取得・replay・最終確認で一致した。
既存のcollector、Material Resolver、Classifier、Solver、独立評価器を変更せず使用した。
このcommitで追加するのは本報告とPrivate原本 `strict-fresh-evaluation-batch8-20260919.json`（公開版では除外、追加summaryなし）のみ。

## 内容確認前に固定したsampling rule

`2026-09-19T07:17:49.927708+00:00` に `.local/batch8/preregistration.json` を保存した。

- 同一公開URL `https://technocore.chat/r/tclk-offers?format=json&limit=200` の最新200 recordsを**3回だけ**取得。
- 次のsnapshot取得開始は前回取得完了から最低60秒後。ツール実行・承認待ちによる遅延は許容。
- 各sampleの全Offer jobを対象にする。容易さ、family、Solver結果で選別しない。
- Batch 2・7のsnapshotと、先行sampleに対してjob ID・job全体・Offer本文が重複するものを除外。
  job欠落は別記。full-specの同一性はOfferの同一性とは別に記録する。
- 取得失敗は保存し、sampleを置換・追加しない。sandbox DNSによる取得前失敗だけ同じGETの再実行を許す。
- Materialは既存予算を維持：sampleあたり32 fetch / 2 MiB、Taskあたり4 fetch、depth 2、redirect禁止。
  3 sample合計上限は事前に96 fetch / 6 MiBとした。既存資料cacheは再利用しない。
- `evaluate_fresh.py acquire` と `replay --baseline` をそのまま使う。
  評価器はBatch 2重複除外を内蔵するため、Batch 7とsample間の重複も別artifactで照合し、元の結果を変更しない。
- 成功・不成功にかかわらず3回で終了。コード変更・評価中の適応はしない。

preregistration SHA-256:
`845df35efc69a608abf0ca4823ac007a5c204411a14b972eaa781cfd0e58f80f`

## 取得結果と未見性

| sample | UTC取得完了 | seq範囲 | records | 新規Offer仕事 | 結果 |
| --- | --- | --- | ---: | ---: | --- |
| 01 | 07:19:24.495554 | 6826977–6827176 | 200 | 40 | freeze・replay完了 |
| 02 | 07:22:27.308604に失敗を記録 | 未取得 | — | — | HTTP 503、置換なし |
| 03 | 07:28:29.431407 | 6830733–6830932 | 200 | 31 | freeze・replay完了 |

sample 02の時刻は失敗を記録した時刻で、HTTP responseの正確な到着時刻ではない。
collectorはHTTP error時にraw bodyを保存しないため、503のbodyは保持していない。
sample 03もこの失敗記録から60秒以上空けた。実際の取得時刻間隔は一律60秒ではなく、sample 01と03は約9分離れている。

sample 01の最初のsandbox GETはDNS解決段階で失敗し、同じURLをsandbox外で取得した。
成功したsnapshotは2つ、HTTP 503は1つ。成功するまで取り直す処理はない。
取得済み400 recordsのsample間重複は0。新規Offer71件は旧snapshot・sample間とも仕事重複0、job欠落0。

ただし、**新Offerであることは本文や意味まで未見であることを保証しない**。

- `native-6830794` のattestation full-specは旧Task本文と同一。今回もUNKNOWN。
- この既知本文1件を除いた補助集計は **70件 / C0 / H13 / U57、Material完全21件**。
- onboardingの同一本文が新sample内でさらに5回反復していた。異なる仕事だが、独立した新Template観測には数えない。
- 資料が取得できない仕事について、full-specの未見性までは主張しない。
- collectorの歴史的な20/5分割metadataは今回の評価設計には使用しない。適応を行わず、すべてを評価する。

snapshot SHA-256:

- sample 01: `fd793a3a01e575f0a3796bf6c6e9745f334a518e8caa9900dcbaaa0ba3b656ac`
- sample 03: `b14a2fe31d7f192c0fd5869070d1930bf6b99e008458a9db961bc3dc485699bb`

## 評価結果

| Metric | sample 01 | sample 03 | 合計 |
| --- | ---: | ---: | ---: |
| 新規Offer仕事 | 40 | 31 | 71 |
| Material完全 / Classifier到達 | 12 | 10 | 22 (31.0%) |
| 登録Solver到達 | 3 | 4 | 7 (9.9%) |
| 既存exact Template一致 | 0 | 0 | 0 |
| COMPLETED | 0 | 0 | 0 |
| HUMAN_REVIEW | 7 | 6 | 13 |
| UNKNOWN | 33 | 25 | 58 |
| False Complete | 0 | 0 | 0 |

Material完全はResolverの `COMPLETE_WITHIN_SCOPE` であり、semanticな解決可能性や署名metadataの充足を保証しない。
既存の全文recognizerを取得full-specへ適用して一致数を調べた。完成資料22件では一致0。
残り49件は不完全な資料のため、最終的なTemplateの有無を確認できない。
「71件すべてで既存Templateが確実に存在しない」という結論ではない。

登録Solver到達7件はvalidation 4件と既存math.gcd_lcmへのdispatch 3件。
同じfamily名へのdispatchはexact Template一致ではなく、全件が非対応TemplateとしてUNKNOWNになった。

| 既存Capability | 今回の近接例 | exact match | 完了 | 未見再現 |
| --- | --- | ---: | ---: | --- |
| validation.ordered_seq_pair@1 | validation 4件。整数reference比較2、offer/lock数比較1、切断したfold質問の比較1 | 0 | 0 | 未確認 |
| math.normal_nim@1 | 別の数学3件：最短経路、modular exponentiation、格子経路数 | 0 | 0 | 未確認 |
| docs.quoted_limit@1 | 別の文書質問6件。message文字数/wait秒数質問なし | 0 | 0 | 未確認 |
| docs.agent_fields@1 | agent.jsonへのlicense質問1件。既知3質問とは異なる | 0 | 0 | 未確認 |

文書6件には最後のlicense質問も含むため、表の近接例は足し合わせない。
Material完全22件の内容分類は、別文書質問6、fold 6、別validation 4、別math 3、inline表2、既知attestation 1。

全件棄却なのでFalse Complete 0を維持したが、**正答能力の新しい肯定的Evidenceはない**。
exact matchの観測頻度は完成資料中0/22、全Offer中の確認済み一致は0/71。
単一boardの短い時間帯、欠測sample 1つ、資料不足49件があるため、母集団の一致率や能力不足を推定しない。

## Failure / Blocker

| Resolver上の理由 | 件数 | 判定 |
| --- | ---: | --- |
| MISSING_TASK_REFERENCE | 22 | UNKNOWN |
| PREVIEW_MISMATCH | 13 | HUMAN_REVIEW |
| UNRESOLVED_REFERENCE | 8 | UNKNOWN |
| TRUNCATION_SUSPECTED | 4 | UNKNOWN |
| MALFORMED_REFERENCE | 1 | UNKNOWN |
| FETCH_BUDGET_EXCEEDED | 1 | UNKNOWN |

UNRESOLVED_REFERENCEの8件はonboarding 6件と署名replay/POST probeの2件。
外部投稿・Signerが必要な仕事を実行可能にする変更や、許可外の取得はしていない。
sample 01は32 fetchの上限に達し、`native-6827161` の表集計previewを資料不足のまま残した。
予算をリセットしての追加取得や再評価はない。

実際のMaterial取得は54 fetch / 189205 bytes、同一sample内cache hit 2。
取得失敗、切断、preview不整合をSolverの失敗と混同せず記録した。

## Verificationと境界

- 既存 `replay --baseline` が各sampleの開始時実装lockを検証。24コードファイルのdigest不変。
- 71件を独立した2つの新stateで再生し、全Result一致。到達ケースではduplicate・引用schema・SQLite終端・auditも確認。
- 既存独立verifierを変更せず使用。COMPLETEDのない今回、正しい回答の新規verification proofは発生していない。
- 旧25件の全ResultはBatch 6と一致（C6/H1/U18）。Batch 7の全21件も追加後結果と一致（C3/H1/U17）。
  これら46件は回帰確認だけであり、今回のFresh完了数に加算しない。
- 76 tests PASS、skip 0。実行ログは `.local/batch8/tests.log`。
- replay中のsocket使用は禁止。External Write、tclk Action、Signer、Wallet、Secret access、paid API、LLM、pushは0。
- 全sourceはSOURCE_UNVERIFIED。作者の真正性・productionの合格・receiptを主張しない。

## Evidence / 再現

`.local/batch8/` にpreregistration、取得provenance、failure、frozen bundles、replay、audit/state、分析、回帰、testログを保存。
453 filesのhash一覧を `evidence-manifest.json` に保存した。
manifest SHA-256は `31c29385dad575cb7f55f0fa4918b71135aa67726b3ec06176dfcf1ab10ea8aa`。
本報告とPrivate原本 `strict-fresh-evaluation-batch8-20260919.json`（公開版では除外、追加summaryなし）以外に追跡ファイルの変更はない。

```bash
python3 -B tests/evaluate_fresh.py replay --baseline --root .local/batch8/sample-01 --output .local/batch8/replay-01-new
python3 -B tests/evaluate_fresh.py replay --baseline --root .local/batch8/sample-03 --output .local/batch8/replay-03-new
```

必要な `.local` 入力はgitには含めない。出力は未作成pathを指定する。
既存runnerのsummary内の `batch: 7` 等は評価器の歴史的metadataをそのまま残している。
Batch 8への帰属はpreregistration、source digest、取得日時、root path、本報告で管理し、runnerを書き換えていない。

## 次の判断

今回のstrict評価は終了し、**次の別Batchでは限定Capability開発へ戻る**ことを推奨する。
現在のTemplateを見つけるまでsamplingを追加するより、新しく反復した完全資料付きTemplateを扱う根拠が強い。

1. `native-6827024` / `native-6830841`：modular inverse問題に対する、整数referenceとworker回答のvalidation。
   別sampleで同じ構造が2回出現。まずexact grammarと比較対象の意味を固定する候補。
2. `native-6827049` / `native-6830821`：12行の完全なinline表とTask bindingがある。
   前者はpayerのASCII順・seq順sort、後者はearliest/latest timeのseq。表ごとに限定した解析・独立検証の候補。

これらのSolver、回答、known-answerは今回追加していない。
将来実装する場合、この71件は開発に使った資料として扱い、再現性の確認はさらに別の事前固定sampleで行う。
