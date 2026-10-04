# Batch 9: Bounded deterministic families

**実公開仕事のローカルCOMPLETEDは9 → 15（+6）、False Complete 0。**
対象は既存46件とBatch 8の71件、計117件。Batch 8のDevelopment Evidenceでは0 → 5、
旧46件では既存9件を維持して9 → 10となった。
完了した内容familyはvalidation / math / docsの3種類から、tablesを加えて**4種類**。
登録Solver familyの内訳では4 → 7。分類の粒度を変えて成功数を増やしたものではない。

開始状態は `main / 9f34db043bd8660d74a30ec11c2fd1df27a42ee4`、worktree clean。
今回の入力はすべて既に観測したFrozen Evidence。**これらの成功を未見generalizationとは主張しない**。
新規network取得、External Write、Signer、Wallet、Secret access、paid API、LLM/CCW、pushは0。
COMPLETEDは独立検証済みのローカル回答であり、投稿・payer合格・receiptではない。

## Evidenceによる優先順位

1. Batch 8の別sampleで、modular inverse問題の整数referenceとworker回答を比較するTaskが2件反復。
   self-containedなfull-specがあり、比較型を整数に限定できるため最初に実装した。
2. 同じ6列schemaのinline表が2件。各12行で、Task本文と表が同じ取得noteに含まれていた。
   前Batchまでのpreview不整合・切断した表とは異なり、Resolverによるbindingと完全性検査を通っている。
3. Batch 7と8に、無向・正重みグラフの最短経路問題が計2件。
   同じ入力構造で頂点・辺・重みが異なり、有界グラフのAlgorithmと独立検証を適用できるため同Sessionで追加した。

Task ID、数値、payer、seq、辺リスト、回答値をruntimeのlookupに使っていない。
自然言語の言換え全般には対応せず、既知の演算・質問文法とboard envelopeは閉じたまま、
文法内の入力値・行数・順序・グラフ構造を一般化した。Generic semantic engineや新しい実行Frameworkはない。

## Capabilityのbounded範囲

### validation.integer_reference@1

- 既知のmodular inverse posted-task構文と、reference / deliverable / doneの完全なenvelopeを要求。
- 整数はcanonical ASCII非負整数、最大20桁。係数・modulus・範囲・再掲operandの整合を確認。
- `1 <= a < p`、上限`p-1`、referenceが指定範囲内であることを確認。
  worker値が範囲外でもcanonical整数ならreferenceとの差異をFAILとして説明できる。
- 提示referenceとの整数比較を行い、PASS/FAILと1文を生成。別parserでdecision・説明内の値を照合してから完了。
- **reference自体の数学的正しさや、引用中のprime主張を検証する仕事ではない**。
  原Taskが求める「worker回答が提示referenceを与えているか」に限定する。
- 符号、少数、非canonical桁、追加説明、複数値、入れ子、別のposted-task型はUNKNOWN。

### tables.inline@1

- 1つの完全なinline full-spec。列は `seq | payer | amount | asset | proto | time` の6列固定。
- 1〜64行、seqは重複のないcanonical非負整数（最大20桁）。amountも同じ整数型。
- payerはASCII英数字1〜64文字。assetは大文字から始まる英数字1〜16文字、protoは小文字から始まる許可token1〜32文字。
  `did:key:`、空白入りpayer、Unicode payerはこのfamilyでは受け付けない。
- 時刻は正確な `HH:MM:SS`、00:00:00〜23:59:59。日付・timezone・時刻補完なし。
- 操作はpayerのASCII昇順→seq数値昇順、またはearliest/latest timeのseq（同時刻は低いseq）の2種類。
- body全体を消費できることを要求。途中行、重複seq、変更された列、追加指示、余剰文字は拒否。
- 候補生成のsort/min/maxとは別に、出力の集合・件数・隣接順序、秒数へ変換した全行との比較を検査する。
  独立評価器は別の空白/列parserとrank計算・時刻検査を使う。

Resolverの既存preview照合と切断検査は緩めていない。引用対象は取得できたnote本文であり、
上流が完全な行単位で削除したような、元の宣言行数なしには検出できない欠落の不存在は保証しない。
これは既存の「author全体のsemantic/historical completenessを保証しない」範囲を維持する。

### math.shortest_path@1

- 頂点は0始まりの連続番号、2〜16頂点。1〜120本の無向辺。
- 重みは1〜10^9の整数。self-loop、同じ無向辺の重複、範囲外頂点、負・ゼロ・小数重みは拒否。
- source/targetは指定範囲内。Dijkstraで距離候補を生成し、Floyd–Warshallで独立検証する。
- 評価器は別parserとBellman–Fordで再検証。2実例とも答えは12だが、入力による計算であり定数ではない。
- 到達不能の場合は、数値以外の回答契約が定義されていないためUNKNOWN。
  到達可能なのに候補が欠落するなどの内部不一致はHUMAN_REVIEW。

## 実Task結果

| Task | 追加family | 回答 | 検証 |
| --- | --- | --- | --- |
| native-6827024 | integer_reference | PASS、reference `103452`との一致を説明 | 独立parser・値/decision/引用照合 |
| native-6830841 | integer_reference | PASS、reference `66328582`との一致を説明 | 同上 |
| native-6827049 | inline表・payer順 | 12個のseqをASCII payer順・数値seq順に列挙 | 全row照合、独立rank検査 |
| native-6830821 | inline表・time extrema | `6039687 5959178` | 全12行の秒数・tie規則検査 |
| native-6813896 | shortest_path | `12` | 7頂点12辺、独立距離配列 |
| native-6830783 | shortest_path | `12` | 6頂点8辺、独立距離配列 |

長いseq回答を含む正確な回答bytes、元Offerのdigest/record位置、bundle digest、独立proof、SQLite終端は
[機械可読Evidence](bounded-evaluation-batch9-20260919.json)の `new_cases` に保存。
実Taskの端から端まで、Offer→Material→分類→Solver→独立検証→COMPLETED→再open後previewを確認した。

| Dataset | 件数 | 開始時C/H/U | 今回C/H/U | False Complete |
| --- | ---: | --- | --- | ---: |
| Batch 2/3の旧25件 | 25 | 6 / 1 / 18 | 6 / 1 / 18 | 0 |
| Batch 7の旧21件 | 21 | 3 / 1 / 17 | 4 / 1 / 16 | 0 |
| Batch 8 sample 01 | 40 | 0 / 7 / 33 | 2 / 7 / 31 | 0 |
| Batch 8 sample 03 | 31 | 0 / 6 / 25 | 3 / 6 / 22 | 0 |
| 合計 | 117 | 9 / 15 / 93 | **15 / 15 / 87** | **0** |

Material完全/Classifier到達は41件のまま。登録Solver到達は20 → 22。
Batch 8だけではMaterial完全22件、Solver到達7 → 9、COMPLETED 0 → 5。
旧46件の既存Completion 9件はResultも完全一致。旧21件の最短経路1件のみ検証済みUNKNOWN→COMPLETEDへ変化した。
旧46件の残45 Result、Batch 8の残66 Result、計111 Resultは開始時と完全一致。
既知本文が別Offerで再出現したattestationもUNKNOWNのままで、Completionへ加算していない。

内容family別完了数はvalidation 2→4、math 1→3、docs 6→6、tables 0→2。
登録familyは既存4つに `validation.integer_reference`、`tables.inline`、`math.shortest_path` を追加して7つ。

## VerificationとFailure予防

- **86 tests PASS、skip 0**。
- 4頂点の全ての非空グラフ728通り（各辺なし/重み1/重み2）を単純経路列挙と比較。
  到達不能や別の最短距離も含み、実例の答え12に依存しない。
- 表の24順列、ASCII大小文字、同payerの数値seq順、earliest/latest双方のtie、1/2/16/64行を検査。
- 20桁整数、2^53を超えるseq、相違referenceのFAIL、範囲外worker値、引用sourceの改変を検査。
- 曖昧・不完全・重複・範囲外の入力を拒否。誤った候補を注入した場合、全3familyでHUMAN_REVIEWとなりCOMPLETEDしない。
- 117件を独立した2 stateで再生しResult一致・duplicate・auditを確認。
- 既存Smoke PASS（5 runs、`.local/smoke-8_18w2zz`）。Material Smoke PASS（3 cases、`.local/material-smoke-iey7br69`）。
  Smokeの資料行引用は実仕事の完了数に含めない。
- Frozen入力のcompiler digestは従来形式で検証し、実行時だけ新familyへ分類。
  旧stateの同ID結果はtask_id_conflictで保護し、新family policyのないstateはfamily_not_configuredで止める。
  旧結果の上書き、自動policy移行、quota回避は行わない。

このBatchの実Task実装・test・replayでは失敗による修正ループは発生していない。
具体的なIncorrect Completion pathとして、ASCIIとcase-insensitive順序の混同、seqの文字列順、
latest-time tieで高いseqを選ぶ誤り、重複seqによる行同定の曖昧化、不正な距離候補を検証対象にした。
実際に資料不足だった表を「使えることにする」変更は行っていない。

## 残したPatternと停止判断

Batch 8の資料不完全49件はすべて元の状態を維持。内訳は参照欠落22、preview不一致13、
未解決参照8、切断疑い4、不正参照1、元のfetch予算超過1。
完全資料22件のうち残17件は、fold 6、別の文書質問6、別validation 2、別math 2、attest 1。

- foldは認証metadataと参照revisionが不足し、公開された行だけから参照実装の結果を保証できない。
- onboarding、attest、HTTP probeは外部Action/Signerを要する。Boundaryを越えない。
- offers/locksの名付き2値validationはscalar整数型ではない。今回の1例だけを理由に任意のreference本文比較へ広げない。
- 切断されたfold質問のvalidation、入れ子、追加説明の意味検証は今回の比較型で扱わない。
- modular exponentiation、格子経路数、約数和などは今回個別に1例ずつ。反復が確認できた構造を優先した。
  modular exponentiationには「methodを1節で示す」と「residueのみ」の回答指示差も残る。
- 別の文書質問を任意の自然言語→JSON pointer変換に拡張しない。

安全に反復を根拠に実装できる高優先3familyを処理し終え、次は資料/意味/別schemaの契約が必要なため終了する。
第三者codeの新規コピーなし。アルゴリズムは標準的な整数比較・sort・最短経路を新規記述した。

## Evidenceと次のStrict評価

`.local/batch9/` に開始時lock、4 Datasetのreplay/SQLite/audit、比較、testログを保存。
84 filesの目録 `evidence-manifest.json` のSHA-256:
`b5dcbc65cb1d631be1c8c1d6e64a74444bd115952afb5638fd1afec2155d79df`。
Batch 8のStrict Baselineや元Frozen資料は変更していない。

```bash
python3 -B -m unittest discover -s tests -v
python3 -B tests/evaluate_fresh.py replay --root .local/batch8/sample-01 --output .local/batch9/replay-01-new
python3 -B tests/evaluate_fresh.py replay --root .local/batch8/sample-03 --output .local/batch9/replay-03-new
python3 -B tests/evaluate_fresh.py replay --root .local/batch7 --output .local/batch9/prior21-new
python3 -B tests/evaluate_empirical.py --output .local/batch9/prior25-new
```

出力先は未作成pathを指定。旧Strict実装lockとは異なるため、今回の適応後replayに `--baseline` は付けない。
runner内の歴史的Batch metadataはそのまま保存し、本報告でBatch 9のDevelopment評価として対応付ける。

次回は現在の実装を変更せず、別の取得範囲を内容を見る前に固定する。
新3familyごとに自然出現数、parse/Material到達数、独立検証済み完了数、拒否理由を報告する。
数値・reference一致/不一致、表の行数/順序/ASCII/tie、グラフの頂点/辺/重みが変わった実Taskで再現するかを確認し、
該当しない場合は未確認とする。今回の6件は以後もDevelopment Evidenceであり、未見評価へ再利用しない。
