# Batch 10: Strict Fresh Generalization Evaluation

**コード無変更でStrict評価を完了した。Fresh Public Task 80件、Material完全7件、COMPLETED 0 / HUMAN_REVIEW 1 / UNKNOWN 79、False Complete 0。**

Batch 9で追加した `validation.integer_reference@1`、`tables.inline@1`、`math.shortest_path@1` に一致する完全Taskは0件だった。
したがって3familyはいずれも「該当Taskがなく未確認」であり、「未見再現できた」「該当したが失敗した」のどちらでもない。
結果を受けたsample追加・置換、Solver / Classifier / parser / acceptance boundary変更、Task固有patchは行っていない。

開始状態は `main / 8e6f7369b8dbb28e0c1bc9f832e047b434f6a44f`、worktree clean。
取得前に現行実装と評価器15ファイルをSHA-256で固定し、3 sampleのbaseline replayと終了時に一致を確認した。
追跡ファイルの変更は本報告と[機械可読Evidence](strict-fresh-evaluation-batch10-20260919.json)だけである。

## 内容確認前に固定したsampling rule

`2026-09-19T08:13:42.278898Z` に `.local/batch10/preregistration.json` を保存した。

- 固定公開URL `https://technocore.chat/r/tclk-offers?format=json&limit=200` の最新200 recordsを3回だけGETする。
- successive requestの開始を60秒以上空ける。各sampleの全Offerを時系列順に対象とし、内容・難易度・family・結果では選別しない。
- Batch 2・7・8および先行Batch 10 sampleに対し、job ID・job全体・Offer本文の重複を除外する。欠損jobは別記する。
- 失敗した予定sampleはそのまま1枠として保存し、追加・置換しない。結果依存の追加samplingはしない。
- Material予算は成功sampleごとに32 fetch / 2 MiB、Taskあたり4 fetch、depth 2、redirect 0。過去cacheは再利用しない。
- 既存 `evaluate_fresh.py acquire` / `replay --baseline` と独立verifierを変更せず使用する。
- External Write、Signer、Wallet、Secret、paid API、LLM / CCW、Production mutation、push / publishは行わない。

3回ともsnapshot GETは成功した。開始時刻の間隔は約68.5秒と63.1秒で、固定した60秒以上を満たす。
取得後にsampleを追加・置換していない。

| sample | UTC開始 | seq範囲 | records | Offer | Fresh Task | snapshot SHA-256 |
| --- | --- | --- | ---: | ---: | ---: | --- |
| 01 | 08:14:41.470205 | 6852245–6852444 | 200 | 24 | 22 | `b6d0a5a75a9aeff43d17b910369a113cb1912b3cffe64a31a56f55426f5ac988` |
| 02 | 08:15:49.958516 | 6852700–6852899 | 200 | 36 | 36 | `9940ab1b4d0026c2c8916dcdec02cbb0aa85361c1c2cce6edf44c9b6c6b84663` |
| 03 | 08:16:53.102453 | 6853191–6853390 | 200 | 22 | 22 | `37c7a45cbc7ba7b5481ea4ef2572ca85a2ac16a161ba005423fb7ac3ab61b93b` |

sample 01の2 Offerはjob欠損/不正のため非Taskとして除外した。他の除外はない。

## Freshnessと既存Task重複

過去4 snapshot（Batch 2、7、Batch 8 sample 01/03）のOffer 118件と、Batch 10の先行sampleを機械比較した。
選択80件のjob ID、canonical job全体、Offer本文、record全体の重複はすべて0だった。

ただし、Fresh Offerは意味的に未見のTaskを保証しない。Material完全7件のask/done完全一致を過去評価資料と比較すると、5件は既知本文だった。

- fold本文4件はBatch 10内でも同一ask/doneで、過去sampleにも9件ある。
- attestation本文1件はBatch 8の1件と同一である。
- 残る2件はagent.jsonの別フィールド質問とmodular exponentiationで、いずれも既存exact Templateではない。

従って、job単位のFresh Taskは80件だが、内容まで確認できた未見specは2件である。資料不完全73件の本文未見性は主張しない。

## Main Metrics

| Metric | sample 01 | sample 02 | sample 03 | 合計 |
| --- | ---: | ---: | ---: | ---: |
| Fresh Task | 22 | 36 | 22 | **80** |
| Material完全 / Classifier到達 | 7 | 0 | 0 | **7** |
| existing family / exact Template match | 0 | 0 | 0 | **0** |
| 登録Solver到達 | 1 | 0 | 0 | **1** |
| COMPLETED | 0 | 0 | 0 | **0** |
| HUMAN_REVIEW | 1 | 0 | 0 | **1** |
| UNKNOWN | 21 | 36 | 22 | **79** |
| False Complete | 0 | 0 | 0 | **0** |

Solver到達1件はmodular exponentiationが広い `math.gcd_lcm` dispatchへ入った例である。
exact grammarに一致せずUNKNOWNとなり、既存Template matchには数えない。完成7件のruntime familyは
`public.protocol` 4、`public.extraction` 1、`public.attest` 1、`math.gcd_lcm` 1だった。

確認可能な完成資料での既存exact Template出現頻度は0/7、全Fresh Taskに対する**確認済み**出現頻度は0/80である。
ただし73件はMaterial不完全なので、全80件でTemplateが存在しないという推定ではない。

## Capability別判定

| 既存登録Capability | confirmed Task | Solver到達 | COMPLETED | 判定 |
| --- | ---: | ---: | ---: | --- |
| `validation.ordered_seq_pair@1` | 0 | 0 | 0 | 該当Taskがなく未確認 |
| `math.normal_nim@1` | 0 | 0 | 0 | 該当Taskがなく未確認 |
| `docs.quoted_limit@1` | 0 | 0 | 0 | 該当Taskがなく未確認 |
| `docs.agent_fields@1` | 0 | 0 | 0 | 該当Taskがなく未確認 |
| `validation.integer_reference@1` | 0 | 0 | 0 | 該当Taskがなく未確認 |
| `tables.inline@1` | 0 | 0 | 0 | 該当Taskがなく未確認 |
| `math.shortest_path@1` | 0 | 0 | 0 | 該当Taskがなく未確認 |

対象3familyでは、異なる整数、表の行数/順序、グラフ構造を持つTask自体が確認できなかった。
従ってBatch 9の6 Development成功を未見成功へ昇格しない。該当した失敗も0件である。

inline表Taskは確認済み0件のため、追加Evidenceの本文長・行数は該当なしである。
`inline_table_length_evidence` は空配列として保存した。資料不完全Taskを推測でinline表に分類していない。

### integer referenceの検証層

今回 `validation.integer_reference@1` は0件なので、runtime検証も独立oracle検証も実Taskに対して0回である。
Batch 9のruntimeは入力envelope・整数/range/operand整合と、worker値が提示reference整数と一致するかを検査する。
独立oracleは別parserで同じTaskと生成回答を再解析し、decision・値・引用を照合する。
これは二つの独立な実装層だが、どちらもauthorのreferenceが数学的に正しいmodular inverseかを証明するものではない。
今回この区別を「独立oracleでreference自体を数学検証した」と表現しない。

## Material Failureと評価限界

| Resolver理由 | 件数 |
| --- | ---: |
| `FETCH_FAILURE` | 36 |
| `MISSING_TASK_REFERENCE` | 27 |
| `UNRESOLVED_REFERENCE` | 4 |
| `MALFORMED_REFERENCE` | 3 |
| `SOURCE_NOT_ALLOWED` | 1 |
| `TRUNCATION_SUSPECTED` | 1 |
| `INCOMPLETE_SPEC` | 1 |

sample 02/03のMaterial取得は計36回のfetch試行を記録したが取得bytesは0で、完成資料は0件だった。
固定ruleを守り、再取得、別sampleへの置換、予算リセットは行っていない。この欠測により肯定的な未見再現性Evidenceは弱いが、
80 Fresh Taskを固定ruleどおりfail-closedに評価し、欠測を成功扱いしなかった点でStrict評価は成立する。

## VerificationとBoundary

- 3 sampleをそれぞれ独立した2 stateでoffline replayし、全Result一致、到達ケースのduplicate、SQLite終端、auditを確認した。
- baseline replayが取得前implementation lockを検証し、終了時にも対象15ファイルのSHA-256一致を確認した。src/testsの変更は0。
- 86 tests PASS、skip 0。Batch 9の既存6実Taskと独立verifierの回帰もPASSしたが、Fresh完了数には加算しない。
- replay中のsocket使用は禁止。External Write、Signer、Wallet、Secret、paid API、LLM / CCW、Production mutation、push / publishは0。
- 全sourceは `SOURCE_UNVERIFIED`。COMPLETEDがないため、新しいanswer proof、外部receipt、payer合格はない。

## 判断

False Completeは0。Batch 9追加3familyは未見再現も失敗も観測されず、generalization Evidenceは**未確認のまま**である。
一方、確認済み出現頻度0/7で、新規Capability開発を正当化する反復した完全Taskも得られなかった。

このEvidenceからは、結果のないsampleを追ってClassifier/Solverを変更するより、**現在のfail-closed実装のままread-only運用へ進む**のが妥当である。
運用表示では対象3familyを「Batch 9 Development検証済み / Strict Fresh未確認」と明示する。
将来別Batchを行う場合も、新しい事前固定sampleとして扱い、本Batchへsampleを追加して成功を作らない。

## Evidence

`.local/batch10/` にpreregistration、implementation lock、3 snapshot、Frozen Material、二重state replay、audit、重複分析を保存した。

- preregistration SHA-256: `8d112f6960f5a8a707b64a77393a19649cf7009276d5693e6eb0ffb296b55dfd`
- implementation lock SHA-256: `f5f0cbb5a2d56e3fab88ef7e57424ed76dd1b1c5733cda1b1a1e4ac4f1a2d770`
- analysis SHA-256: `17485c3098147d22e0db200c46c299197c86c67cdcd666b136ee0b8a95e2fdf1`
- evidence manifest: 380 files, SHA-256 `fc4ad470fd0e208b22231716f46b1b3cc49c99ccca10c8723281c7064c113382`

既存runnerのsummary内に残る歴史的 `batch: 7` は評価器を変更しなかったためである。Batch 10への帰属はpath、preregistration、snapshot digest、本報告で固定した。
