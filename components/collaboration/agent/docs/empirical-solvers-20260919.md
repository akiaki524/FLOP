# Batch 4: Empirical Solver / First Real Task Completion

評価原本・生引用・Task別proofの記述はPrivateの当時の保存入力を対象にする。配布版の派生summaryは原文・完全なproof・再実行fixtureを提供しない。

2026-09-19 JST。**実公開Taskの正しいローカル回答完了 0 → 2 / 25、False Complete 0。**
開始時は `main`、HEAD `3a609141d153c4158523c704b1df9a179aeb8c1e`、worktree cleanを実測した。
COMPLETEDはREADMEと同じくローカル回答の完了であり、accept、投稿、署名、payer合格、receiptではない。
Batch 2で固定した実OfferとBatch 3の実Materialを使用し、合成fixtureを実Taskの件数へ加算していない。

## Evidenceからの分類と優先順位

実25件のOffer、context、full-spec、引用Material、Batch 3判定を読み、次のTemplateに分類した。
参照がない13件はjob名から意味を推測せず、Template不明としている。
Material completeはResolverの構造上の完全性で、実行許可・意味の妥当性とは区別する。

| actual Template | 件数 | Material complete | deterministic / 機械検証条件 | Blocker / 実装候補・優先度 |
| --- | ---: | ---: | --- | --- |
| validation: earliest/latest seqのreference比較 | 2 | 2 | canonical整数2値、順序比較、PASS/FAILと説明の一致 | **優先1・今回実装**。外部依存なし、同じ全文Templateが反復 |
| docs: message最大文字数 | 2 | 2 | 固定質問2表現→保存READMEの一意な値・引用を照合 | 優先3。上限の文脈・GET URL byte制限との混同を拒否する契約が必要 |
| docs: wait最大秒数 | 1 | 1 | 固定質問→llms.txtのparameter上限引用 | 優先4。同様に一意なsource-bound抽出が必要 |
| math: normal-play Nim | 1 | 1 | 全文文法、合法な1山の減少、変更後XOR=0 / 元XOR=0ならnone | **次候補・優先2**。gcd/lcmに到達して文法不一致。専用Solverで閉じる |
| validation: HTTP statusと追加説明 | 1 | 1 | statusだけでなく追加の主張の妥当性が必要 | 未実装。単純substring一致でPASSにしない。引用URLはwriteなので実行しない |
| validation: 入れ子のvalidation | 1 | 1 | 一意な引用境界・外側referenceの確定が必要 | 引用の入れ子と途中で終わる内側deliverableが曖昧。UNKNOWN |
| protocol: tclk transcript fold | 1 | 1 | 固定版のreference fold / room / reject規則を再現 | 大きなprotocol実装と版拘束が必要。今回の0→1には不適 |
| attest: signed lineとseq | 1 | 1 | 実署名投稿とroom recordが必要 | Signer・外部writeが禁止。実装しない |
| tables: 同一senderのoffer/lock件数 | 1 | 0 | 完全な行、sender/type完全一致、独立recount | previewとfull-specが不一致。Resolver拒否を維持 |
| tables: 同一senderのlock件数 | 1 | 0 | 完全な行、sender/type完全一致、独立recount | Material切断。先行AgentにもJudge不一致の停止記録。拒否維持 |
| unresolved: context/reference欠落 | 13 | 0 | 本文・資料を提供して初めて判断可能 | 推測復元しない。Solver選定不能 |
| 合計 | 25 | 10 | | |

観測familyでまとめるとdocs 3、validation 4、math 1、protocol fold 1、attest 1、tables 2、不明13。
元のfamilyラベルapi/document/protocolにまたがる上限値質問は内容に基づいてdocsへ統合した。
同じseq比較の開発側1件だけでも0→1が可能。文書抽出より契約が狭く、Nimより同一Templateの観測頻度が高い。
引用された表そのものは不要：依頼は表の再計算ではなく、著者の提示referenceとdeliverableの比較である。

## 実装

`src/collaboration_agent/empirical.py` に小さな `validation.ordered_seq_pair@1` を実装し、
既存 `public.validation` familyに登録。`materials.py`、Frozen入力形式、Classifier APIは変更していない。
保存full-spec 1件だけを使い、paramsは空、Task文・done条件・boardの既知末尾まで全文一致する場合のみ処理する。
非負整数はcanonical ASCII表記0〜20桁を2値、1個の空白で区切る。Python整数で丸めず順序付き比較する。
未知文法、改行、追加指示、別done条件、別Task、入れ子、余分なMaterialはUNKNOWN。
不正paramsやdigest不一致は既存契約に従いHUMAN_REVIEW。

値はMaterialから毎回取得する。実Task ID、実reference値、URL別の答え分岐はSolverに含まない。
Task本文の既知Templateを文法として持つことと、答えをhardcodeすることを区別する。
整数比較とcanonical文字列比較の整合、出力判定を検査し、source全体を引用する。
既存 `validate_outcome` が引用元digest・本文を再検証する。
`tests/evaluate_empirical.py` の独立delimiter parserはSolverのregex/定数/回答をimportせず、
公開referenceとdeliverableから正解を決め、decision・数値・順序・説明1文・verdictを照合する。
未知TaskのCOMPLETEDまたは検証不合格のCOMPLETEDをFalse Completeとして数える。

登録追加で既存stateのfamily policy lookupがKeyErrorになる経路を発見した。
`engine.py` / `state.py` に欠落policyの明示的な `family_not_configured` 停止だけを追加。
自動migration、既存UNKNOWNの再判定、quotaリセットはない。新stateは評価用であり運用回避ではない。

## 実Task回答

| Task / 元Offer seq | 分割 | reference = deliverable | Answer / 機械検証 |
| --- | --- | --- | --- |
| native-6343727 / 6343727 | development | `4576886 4978229` | `PASS: The earliest and latest seq values match the reference in order (4576886 4978229).` / PASS |
| native-6343730 / 6343730 | 旧holdout | `3682508 4093728` | `PASS: The earliest and latest seq values match the reference in order (3682508 4093728).` / PASS |

実referenceの表記にprivateという語があるが、値は既に取得済みの公開note本文に含まれる比較対象。
秘密領域、鍵、Signer、API、他Projectへアクセスしていない。
これらのPASSはvalidator Taskへの答えで、Production Judgeからの合格や外部の完了通知ではない。

## 全25件の到達状況

C=COMPLETED、H=HUMAN_REVIEW、U=UNKNOWN。登録Solver到達はTemplate適合とは区別する。

| Task末尾 | 分割 | Template | B3 → B4 | B4理由 |
| --- | --- | --- | --- | --- |
| 6343689 | dev | docs.message_limit | U → U | unsupported_family |
| 6343699 | dev | no_reference | U → U | MISSING_TASK_REFERENCE |
| 6343703 | dev | attest.signed_line | U → U | unsupported_family / 許可範囲外 |
| 6343712 | dev | no_reference | U → U | MISSING_TASK_REFERENCE |
| 6343713 | dev | validation.http_status_with_explanation | U → U | unsupported_validation_template |
| 6343716 | dev | no_reference | U → U | MISSING_TASK_REFERENCE |
| 6343727 | dev | validation.ordered_seq_pair | U → C | 独立検証PASS |
| 6343741 | dev | docs.wait_limit | U → U | unsupported_family |
| 6343746 | dev | docs.message_limit | U → U | unsupported_family |
| 6343749 | dev | math.normal_nim | U → U | unsupported_math_template |
| 6343753 | dev | protocol.transcript_fold | U → U | unsupported_family |
| 6343759 | dev | tables.offer_lock_counts | H → H | PREVIEW_MISMATCH |
| 6343763 | dev | no_reference | U → U | MISSING_TASK_REFERENCE |
| 6343779 | dev | no_reference | U → U | MISSING_TASK_REFERENCE |
| 6343793 | dev | validation.nested_ambiguous | U → U | unsupported_validation_template |
| 6343801 | dev | no_reference | U → U | MISSING_TASK_REFERENCE |
| 6343811 | dev | no_reference | U → U | MISSING_TASK_REFERENCE |
| 6343814 | dev | no_reference | U → U | MISSING_TASK_REFERENCE |
| 6343881 | dev | no_reference | U → U | MISSING_TASK_REFERENCE |
| 6343883 | dev | tables.lock_count | U → U | TRUNCATION_SUSPECTED |
| 6343690 | 旧holdout | no_reference | U → U | MISSING_TASK_REFERENCE |
| 6343695 | 旧holdout | no_reference | U → U | MISSING_TASK_REFERENCE |
| 6343730 | 旧holdout | validation.ordered_seq_pair | U → C | 独立検証PASS |
| 6343775 | 旧holdout | no_reference | U → U | MISSING_TASK_REFERENCE |
| 6343825 | 旧holdout | no_reference | U → U | MISSING_TASK_REFERENCE |

## Batch 3 → Batch 4

| 指標 | Batch 3 | Batch 4 |
| --- | ---: | ---: |
| 実Task | 25 | 25 |
| Material complete | 10 | 10 |
| Classifier到達 | 10 | 10 |
| 登録Solver到達 | 1 | 5 |
| COMPLETED | 0 | 2 |
| HUMAN_REVIEW | 1 | 1 |
| UNKNOWN | 24 | 22 |
| False Complete | 0 | 0 |

validation 4件が新Solverに到達し、exact Template 2件だけ完了。元のNim 1件は既存SolverでUNKNOWN。
developmentはC1/H1/U18、旧holdoutはC1/H0/U4。参照解決件数・Material完全性はBatch 3から不変。
False Complete=0はこの25件と検証契約での実測。未知の全入力やProduction Judgeとの一致は保証しない。
両方の成功がPASSであり、実TaskのFAIL例は未評価。順序逆転・欠落・値違い・20桁精度は合成testで確認。

## Verification / Evidence

- 55 unit/regression tests PASS。17種の文法逸脱、独立verifierへの誤回答、policy欠落、重複等を検査。
- 全25件で元raw snapshot→record seq/text hash→Dataset→Frozen bundle/raw Material→Task→Result引用を確認。
- Frozen bundleは書換えなし。独立した新state同士で全25件の結果一致。到達10件の重複再実行で同一Result。
- SQLite audit検証、各到達TaskのHuman Preview保存、socket禁止下のoffline replay。
- 既存Smokeと開発/旧holdout Material Smoke PASS（pathはEvidence目録参照）。
- Batch 3評価器は「nativeはすべてunsupported」という旧境界をそのまま残した。新完了の採点にはBatch 4評価器を使用。
- 20/5分割は保持。ただし実分類時に旧holdoutの本文も見たため、**未見Holdoutではない**。
  回答の手入力やholdout値埋込みはない。今後の一般化評価には新しい未見データが必要。

評価本体 `.local/batch4/evaluation/`、再現可能な集計と各25件のsource/digest/判定は
Private原本 `empirical-evaluation-20260919.json`／配布版の派生summary `empirical-evaluation-20260919.public-summary.json`（原文・再実行fixtureではない）。
全artifact目録は `.local/batch4/evidence-manifest.json`。元データは既存 `.local/batch2/` と `.local/batch3/`。
目録SHA-256: `fcab1eb38184ac37c1f454dd28d7d36e4af6fb43e69b42fe36a9076121a7676c`（162 files）。
公開集計SHA-256: `e0c3e36ac212842df0c90c1ee0a90853297e2a931502982272283f119e38f691`。
通常Git cloneだけでは過去の可変公開bytesは復元できない。欠落時は停止し、現在のWeb内容で置換しない。

```bash
python3 -B -m unittest discover -s tests -v
python3 -B tests/evaluate_empirical.py --output .local/batch4/replay-new
python3 -B tests/smoke.py
python3 -B tests/material_smoke.py --public .local/batch3/development
python3 -B tests/material_smoke.py --public .local/batch3/holdout
```

## 先行Agent比較 / 再利用

[references.mdのBatch 4記録](references.md#batch-4の追加参照)に固定revision・License・対象pathを記録。
外部OSSの小Template、unknown時の停止、実運用の資料不足と成果の区別を設計参考にした。
本実装は狭いTask契約に限定した独立実装で、第三者コードのコピーは0件。
参照元の固定revision・License・比較記録はPrivate開発履歴に保持する。
公開向けの説明では個別の参考人物や第三者の失敗比較を省き、本Projectの検証範囲を示す。
本作業のMetricは許可範囲のローカル回答完了であり、外部のterminal receiptを得たとは主張しない。
protocolのHTTP probeにはGET writeも含まれるため実行しない。
「generic infrastructure過多から明示的に戦略Resetした」という因果全体は確認した固定revisionの資料では
裏付けられず、事実として追加しない。今回の狭いSolver選択は本ProjectのEvidenceに基づく判断。

## 発見したFailure / 未解決と次

入れ子validationではreference/deliverable markerが複数あり、部分regexだと内側referenceと外側回答を
混ぜる危険がある。全文Templateに限定して拒否した。HTTP validationは追加説明の真偽を無視しない。
preview不整合と切断は修復したふりをせず保持。参照欠落13件は解決しない。
調査用表示scriptでNoneのMaterial textをsliceして1回失敗し、None対応へ修正して再実行成功。
実装後のunit/replay/Smoke失敗はなし。新familyの旧policy欠落はtest前のコード確認で発見しfail closedに修正。

次に価値が高いのは `native-6343749` のnormal-play Nim：本文だけで完結し、合法手とXOR条件を
独立検証できる。message-limit引用2件はその次。一般的なsemantic engineや新Harnessへ広げない。
外部write、tclk action、Secret access、LLM、paid API、daemon、pushは0件。

## Batch 5 checkpoint: normal-play Nim

2026-09-19 JST。開始は `<REPO_ROOT>/components/collaboration/agent`、`main`、
`ef167ffdbeba40a154de1024f656470595ce0828`、worktree cleanを実測。
**Real Public Task COMPLETED 3件、2 family、False Complete 0**に到達。
対象は取得済み `native-6343749`。外部取得・通信なしで処理できるため、Batch 4の優先順位を維持した。

### 実装と完了前検証

`empirical.py` に `math.normal_nim@1` を追加。既存のfull-spec末尾を共有し、本文・difficulty・done条件も
全文一致するnormal-play Nimだけを受け付ける。1〜32山、各山canonical ASCII非負整数0〜20桁。
勝ち手はXORで計算し、条件を満たす最初の1-based heapを減らす。元XOR=0なら `none`。
全山0もnormal playの終端負け局面なので `none`。空の山リストは未知入力として拒否する。

生成後は独立した `verify_nim_move` に回答文字列を渡す。山indexの範囲、非負target、厳密な減少、
1山だけの変更を検査する。XOR生成関数を呼び直さず、各ビット列の偶奇から変更後が負け局面か検査する。
検証不合格は `nim_verification_failed / HUMAN_REVIEW` で、回答を残さずCOMPLETEDにしない。
全文の引用・digestは既存 `validate_outcome` で検証。Agent/state semanticsは変更していない。

Batch 3は全数学Taskを `math.gcd_lcm` としてcompileし、そのdigestをfreezeしている。
このbindingを破壊しないよう `compiled_task` の既定動作は従来のまま、`for_execution=True` の場合だけ
全文が一致するNimを `math.normal_nim` に振り分ける。`load_frozen` は従来digestとraw bytesを先に照合する。
`run_frozen` と評価器は検証後の実行用Taskを利用。Humanが明示したselectionは上書きしない。
registry、family policy、duplicate control、auditは既存経路を通る。

### Real Offer → Material → Answer → local state

| 項目 | Evidence |
| --- | --- |
| 元公開Offer | seq `6343749`、snapshot record index `63`、`native-6343749` |
| 保存済みfull-spec | `/kv/tclk-job-en/math-cf9f2a18-`、取得日時 `2026-09-18T15:37:09.234131+00:00` |
| Material raw SHA-256 | `cfab420b7185f38d07aafd0f40a1d9fbdd64961f86f4e3d3bae0914674d09f64` |
| Frozen bundle SHA-256 | `7538cec4e35fc98401dd570a534e8ac920b0fd1910f67187ea04c9b48ae4e046` |
| 旧compiled Task digest | `c0b6ed1f9bbded94ee553ba4d0a2aa2ca15bdf2c5036ef884f7bac0b1cf2c1f2` |
| 実行用Task digest | `c6889ab130311ab3bf6af6f28d848eba2ecde915f9b5f258813dfbc82c3f0341` |
| classification / solver | `math.normal_nim` / `math.normal_nim@1` |
| 入力 | `[20, 33, 16, 20]`、normal play、1山を減らし最後の手で勝つ |
| Answer | `heap 2 to 16` |
| 独立検証 | 2番目だけ33→16、変更後 `[20, 16, 16, 20]`、Nim和49→0 |
| 合法手の独立列挙 | 全89手を評価し、勝ち手は `heap 2 to 16` の1手 |
| local state | `identity` → `started` → `finished(COMPLETED)` → `duplicate`、SQLite audit検証PASS |

実行Evidenceは `.local/batch5/evaluation/native-6343749-preview.json`、`results.json`、
`development-state/agent.sqlite`。実測audit seqはidentity=41、started=42、finished=43、duplicate=45。
既存stateのUNKNOWNを書き換えたのではなく、新しい比較実験stateでadmissionから完了まで処理した。

評価器の独立parserはSolverのregex/定数/生成関数を使わず、Task全文の条件から山を取得する。
二進文字列の各列の偶奇と合法な後続盤面の列挙で回答を照合し、検証proofを各Task行へ保存する。
元Offer→Dataset→Frozen→実行Task→Result→保存stateまでdigestと内容を照合した。

### Batch 4 → Batch 5

| 指標 | Batch 4 | Batch 5 |
| --- | ---: | ---: |
| 実Task | 25 | 25 |
| Material complete / Classifier到達 | 10 / 10 | 10 / 10 |
| 登録Solver到達 | 5 | 5 |
| COMPLETED | 2 | 3 |
| 完了family数 | 1 | 2 |
| HUMAN_REVIEW | 1 | 1 |
| UNKNOWN | 22 | 21 |
| False Complete | 0 | 0 |

NimだけUNKNOWN→COMPLETED。validation 2件を含む他24件はBatch 4のResultと完全一致。
Solver到達数が増えないのはNimが以前もgcd/lcm Solverへ到達してUNKNOWNとなっていたため。
新familyの完了を入力資料の抽出や合成fixtureの成功と混同していない。

### Verification / Failure / 制約

- **62 tests PASS**。780盤面（1〜4山、各0〜4）でXORを使わない再帰ゲーム木と勝敗・回答後の負けを照合。
- all-zero、単独山、複数勝ち手、64-bitを超える値、最大20桁・32山を確認。
- misère、0-based指定、複数山操作、負数/小数/全角/leading zero、余分な指示、未知done等を拒否。
- 不正/非勝利の生成回答をfault injectionし、COMPLETED前にHUMAN_REVIEWで停止することを確認。
- Frozen hash維持、専用Classifier/registry、再起動後のduplicate/Preview/audit、family停止を確認。
- 既存IDの旧分類結果がある場合は `task_id_conflict`、旧policyに新familyがなければ `family_not_configured`。
  既存UNKNOWNの置換、policy自動有効化、quota回避のためのstate再作成は行わない。
- 25件を2つの独立stateで再生して全件一致。到達10件の重複再生と保存終端Resultの一致もPASS。
- 既存Smoke、開発/旧holdoutのMaterial Smoke PASS。unit/replay/Smokeの失敗は0件。
- 実Nimは1件のみで、実Taskの負け局面は未観測。追加の実Task取得でcoverageを水増ししない。
- 未見Holdoutとは主張しない。sourceは引き続きSOURCE_UNVERIFIED。payer合格や外部receiptは未取得。

主要な互換性上の制約は旧数学分類の固定digestで、保存物を変更せず実行時の限定分類で対応した。
次の最も価値が高いMilestoneは、取得済みREADMEのmessage最大文字数2件をsource-bound引用で検証し、
**COMPLETED 5件 / 3 family**へ進むこと。本文・質問・引用条件を狭く固定し、汎用semantic engineへ広げない。

再現（元 `.local/batch2` / `.local/batch3` が必要、出力は未作成path）:

```bash
python3 -B -m unittest discover -s tests -v
python3 -B tests/evaluate_empirical.py --output .local/batch5/replay-new
python3 -B tests/smoke.py
python3 -B tests/material_smoke.py --public .local/batch3/development
python3 -B tests/material_smoke.py --public .local/batch3/holdout
```

最新runnerはBatch 5契約。Batch 4の公開集計ファイルは変更せず比較基準として保持。
Batch 5の機械可読Evidenceは Private原本 `empirical-evaluation-batch5-20260919.json`／配布版の派生summary `empirical-evaluation-batch5-20260919.public-summary.json`（原文・再実行fixtureではない）。
163 artifactの目録は `.local/batch5/evidence-manifest.json`。
目録SHA-256: `d950bcfad70ab6d0d8a715e76488f5151578f4995db6059db2500c8d44d017c2`。
公開集計SHA-256: `a72847024b68199360dff5152b13ecd75ab0bcffb5099b809493c4c0797978e9`。
依存追加・第三者コードコピー・外部取得・外部write・Signer・Wallet・Secretアクセス・LLM・pushは0件。

## Batch 6 checkpoint: source-bound document limits

2026-09-19 JST。開始は `collaboration-agent`、`main`、
`2a075157c395ec5fd3fa73f78a63264a631f72cc`、worktree cleanを実測した。
**Real Public Task COMPLETED 6件 / 3 family / False Complete 0**。
message上限2件で5件に到達したところを上限にせず、残るwait上限1件まで同じBatchで対応した。

### 実Taskと独立検証

| Task | 元family / 質問 | Answer | 保存文書の根拠 |
| --- | --- | --- | --- |
| native-6343689 | api / maximum character limit for messages in the chat | `"Messages ≤ 4096 chars"` | README API節58行目 |
| native-6343746 | document / maximum size in characters for a message body | `"Messages ≤ 4096 chars"` | 同じREADMEの58行目 |
| native-6343741 | protocol / maximum duration in seconds for wait | `"10"` | llms.txt WAITING節46行目とclamp記述59行目 |

同じREADMEを2つの実Offerが異なる質問表現で参照していた。問われているのは資料中の値/句の引用であり、
現在のサーバへのprobeや上限挙動の実証ではない。文字数・note上限・HTTP bodyのbytes・GET URLのbytesを
混同せず、対象をmessageまたはwaitに限定した。
READMEの「POST raises the size ceiling」は隣接するGET URL byte制約の説明と合わせて確認し、
message文字数上限の引用をPOST payload sizeの値へ置き換えていない。

確認済みnormalized document SHA-256:

- README: `4fd57fb18f5f9a4c16771238f5e3d2c77b10c3592c8315da23101f30719e02b3`
- llms.txt: `40e0bebabcc105a2931805b68e200f1d5fc212e32ca14501ac4d85d105a2bb54`

SHA-256は出所の認証ではなく、意味と引用箇所を確認した保存文書の版を固定するために使う。
値やTask IDによるanswer lookupはない。既知の質問3表現、source URL、文書版を契約として固定し、
値は実行のたびに文書から抽出する。Task ID・full-spec note keyには依存しない。
合成testでは別の値73とtest専用digestを使用し、回答が本文に従うことも確認した。
そのtest専用digestをproductionで受け付ける経路はない。

### 実装 / local transition

`docs.quoted_limit@1` を既存registryへ追加。message文字数とwait秒数は同一の文書引用familyで数え、
元のapi/document/protocolラベルを別々の成功familyとして水増ししていない。
`empirical.py` の小さな既知Template処理と、既存実行時compilerへの分岐追加だけで対応した。
Generic semantic engine、Fact Engine、新しいHarness、外部fetchは追加していない。

messageは一意な `Messages ≤ N chars` をAPI節から抽出し、別のdelimiter処理でNames/notes境界と照合。
waitはWAITING行の上限を抽出し、別のclamp記述と一致するか確認する。
候補回答が誤っていれば `*_verification_failed / HUMAN_REVIEW`。引用は既存行selectorで再検証する。
評価器はSolverの定数/regex/抽出関数をimportせず、質問・source digest・値・単位・引用符・行・citationを照合。
文書URL違い、未知版、切断、矛盾追記、質問/単位変更、未知done、余分なMaterialはCOMPLETEDにしない。

3件とも新しい比較用stateで `identity → started → finished(COMPLETED) → duplicate` を確認。
audit seqは6343689が2/3/4/6、6343741が29/30/31/33、6343746が35/36/37/39。
SQLiteの終端Resultと返却Result、独立stateの再生結果が一致した。
既存stateやFrozenを上書きせず、旧compiler digestは既定経路で先に検証する。
旧IDの別分類は `task_id_conflict`、旧policyの新family欠落は `family_not_configured` で停止。

### Batch 5 → Batch 6

| 指標 | Batch 5 | Batch 6 |
| --- | ---: | ---: |
| 実Task | 25 | 25 |
| Material complete / Classifier到達 | 10 / 10 | 10 / 10 |
| Solver到達 | 5 | 8 |
| COMPLETED | 3 | 6 |
| 完了family数 | 2 | 3 |
| HUMAN_REVIEW | 1 | 1 |
| UNKNOWN | 21 | 18 |
| False Complete | 0 | 0 |

完了familyは `public.validation`、`math.normal_nim`、`docs.quoted_limit`。
既存3件を含む対象外22件はBatch 5 Resultと完全一致。元25件、全Frozen bundle、元Offer bindingは不変。
69 tests PASS、skipなし。引用の取り違え、単位/資料変更、抽出結果へのfault injection、
新family停止、旧state保護、再起動後の重複/Previewを確認した。
全25件を2つの独立stateで再生し、socket禁止下で一致。既存Smoke、開発/旧holdout Material SmokeもPASS。
unit/replay/Smokeの失敗は0件。未見Holdoutの主張はしない。
通常cloneでFrozen文書がない場合、一部integration unitはskipするが、Empirical replayは欠落で失敗する。

### 6件後の残19件を調査した結果と停止根拠

| 残Template | 件数 | 現在の実Evidence / 追加実装しない理由 |
| --- | ---: | --- |
| context/reference欠落 | 13 | 本文自体を特定できない。job名から推測しない |
| offer/lock集計 | 1 | Offer previewとfull-specが不一致。資料の意味を同じと見なして解除しない |
| lock集計 | 1 | 保存表が切断疑い。全行がなく正確なcountを出せない |
| attest | 1 | 実署名投稿とseqが必要。External Write / Signer境界 |
| HTTP status validation | 1 | referenceはstatus 403のみ、workerは応答本文も主張。実応答bytesを取得しておらず「nothing invented」を検証不能。URLはwrite pathなのでprobeしない |
| 入れ子validation | 1 | 複数のreference/deliverable markerと閉じ方が曖昧な引用。外側だけを推測で採用しない |
| protocol transcript fold | 1 | 表はroom/time/sender/frameだけ。参照されたfoldTranscriptは認証済みrecordを要求するが、署名・transport nonce・seqが表にない。認証済みと仮定した別foldへ置換しない |

最後のfoldについては、保存済み公式checkout `flop-labs/tclk` の
`5cc4ab93efbc8999a3a7e1471b639deca25998ea`、`src/transcript.ts` を静的確認した。
`TranscriptRecord` はsignature/nonce等を持ち、`foldTranscript` は最初に `verifyTranscriptRecord` を呼ぶ。
資料中のframe nonceはtransport署名nonceの代用ではない。Taskはreference revisionも指定していない。
署名不足を無視してaccepted/locked等を答えるのは、Taskが指定した処理の検証にならない。
コード再利用・実行・依存追加はなし。[固定版source](https://github.com/flop-labs/tclk/blob/5cc4ab93efbc8999a3a7e1471b639deca25998ea/src/transcript.ts)。

現在の25件には、これ以上資料と意味が揃った安全な有力Templateが残っていないため、6件でこのSessionを閉じる。
次の有力Milestoneは**未見の公開Taskで3 familyの再現性を測ること**。
新Capability候補は、完全な表と一致するTask bindingが得られた場合の明示的な表集計。
可変noteを再取得して旧Offerの不整合を解消したことにせず、新取得と旧Frozenを区別する必要がある。

最新runnerの再現:

```bash
python3 -B -m unittest discover -s tests -v
python3 -B tests/evaluate_empirical.py --output .local/batch6/replay-new
python3 -B tests/smoke.py
python3 -B tests/material_smoke.py --public .local/batch3/development
python3 -B tests/material_smoke.py --public .local/batch3/holdout
```

元 `.local/batch2` と `.local/batch3` が必須。出力先は未作成pathを指定。
Batch 5の公開集計は変更せず保存。今回の集計・25件の検証proofは
Private原本 `empirical-evaluation-batch6-20260919.json`／配布版の派生summary `empirical-evaluation-batch6-20260919.public-summary.json`（原文・再実行fixtureではない）。
164 artifactの目録は `.local/batch6/evidence-manifest.json`。
目録SHA-256: `010661d8518a3377981bd36f996b155acc05d0f5e32a22c81f744de72f509ea0`。
公開集計SHA-256: `6238cd513f0a0f62bfec6f1cbd5f6d81448be4c4df19cdc4c012bc2333c2229f`。
Local COMPLETEDは投稿・payer合格・receiptではない。sourceはSOURCE_UNVERIFIED。
外部通信、External Write、Signer/Wallet/Secret、paid API、LLM/CCW、Production mutation、push/publishは0件。
# Batch 7 checkpoint: Fresh evaluation and source-bound agent fields

開始 `main/e891b6e` clean。新規200 records / 22 Offerを取得し、job欠落1件を除く21件を
開始時実装digest固定→Task/Material freeze→未変更Baselineの順に評価した。
旧25件の再利用・成功ケースによる選別はない。

Baselineは **C0/H1/U20、False Complete 0**。旧3 Templateの該当例がなくFresh再現は未確認。
同一の完全なagent.json資料を持つ3件を根拠に、`docs.agent_fields@1` を追加。
native-6813918 / 6813949 / 6813959が、それぞれ `"300000"` / `"0.1"` / `"e-"` と回答し、
独立JSON/引用検証とSQLite終端確認を通過。追加後 **C3/H1/U17、False Complete 0**。
これは開発Dataset上の成功であり、新Capabilityの未見Holdout成功ではない。

旧25件はBatch 6と全Result一致、C6/H1/U18を維持。Freshの他18件もBaselineと全Result一致。
76 tests（skipなし）、Fresh/既存の二重state replay、既存Smoke、Fresh Material Smoke PASS。
参照欠落11、表preview不整合1、認証metadata不足のfold 3、単発未対応3件を残した。
未知質問/文書版は拒否し、state semantics・permission boundaryは変更していない。

詳細・digest・取得順序・再現手順は [Batch 7報告](fresh-evaluation-batch7-20260919.md) /
Private原本 `fresh-evaluation-batch7-20260919.json`（公開版では除外、追加summaryなし）。次は固定した別sampleによる既存Templateの未見評価。

## Batch 9 checkpoint: typed references, inline tables and shortest paths

開始 `main/9f34db0` clean。Batch 8のStrict資料をDevelopment Evidenceとして使い、
整数reference比較2件、完全なinline表2件、Batch 7/8の最短経路2件を実装・独立検証した。
**実公開COMPLETED 9→15、False Complete 0**。内容familyはtablesを加えて3→4、登録familyは4→7。
新familyは `validation.integer_reference@1`、`tables.inline@1`、`math.shortest_path@1`。
数値・行数・順序・グラフをboundedに解析し、Task ID・known answer lookupは使わない。

Batch 8はC5/H13/U53。旧46件はC10/H2/U34となり、既存9 CompletionのResultは全て維持した。
旧46件の最短経路1件を含む6件のみ検証済みUNKNOWN→COMPLETED。他111 Resultは完全一致。
86 tests（skipなし）、全117件の二重state replay、既存/Material Smoke PASS。
新規network、外部write、Signer、LLM、pushなし。旧Strict Baseline・Frozen input・state semanticsを維持。

残る反復foldはmetadata不足、外部Action系はBoundary外。別schema/単発問題を推測で汎用化せず停止した。
今回の成功はDevelopment上の結果で、次の別sampleによるStrict再現確認が必要。
[Batch 9報告](bounded-evaluation-batch9-20260919.md) /
[機械可読Evidence](bounded-evaluation-batch9-20260919.json)。
