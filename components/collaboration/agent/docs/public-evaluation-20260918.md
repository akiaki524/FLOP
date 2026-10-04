# Batch 2 — Real Public Task Offline Evaluation

## 結論

現行Agentは実公開recordのfield抽出・文字列照合・literal引用に使える。
一方、今回含まれた**公開仕事25件を完了できた件数は0**だった。
仕事本文／資料不足が18件、途中で切れたpreviewと未対応処理が7件あり、すべてUNKNOWNとした。
資料処理の成功を、payer jobの完了・tclk適合・相手の採点PASSとして数えない。

開発用226 casesでBaselineを測り、実際に16回出現したJSON解析の過剰拒否だけを改善した。
同じ固定Datasetの正しいcompletionは174→190。未使用40 recordsからのHoldout 67 casesでは58件。
False Completeはすべて0。ただし、1時点の小さい標本と限定された操作の観測結果であり、安全性の一般証明ではない。

## 開始状態・取得

- branch: `main`
- HEAD: `827857ac8b22e495ebf226cb041c3beb3683c76a`
- worktree: clean（開始時に再確認）
- CCW・Scout・Observerは変更も実行もしていない。
- 日時: `2026-09-18T14:37:13.285781+00:00`〜`2026-09-18T14:37:13.570785+00:00`
- Source: `GET https://technocore.chat/r/tclk-offers?format=json&limit=200`
- 取得: 200 records / 125,522 bytes / generation `1` / seq `6343686`〜`6343885`
- 元応答bytes: `.local/batch2/snapshot/raw.json`
- provenance: `.local/batch2/snapshot/provenance.json`
- raw SHA-256: `28dd9895d45986f5afed8ae0dff997940c6a6045f5942c375827aed72a3c6981`

[公式API資料](https://github.com/flop-labs/technocore-chat#api)でroom読取経路と200件上限を確認した。
GETにもwrite経路があるためmethodだけでは判定せず、collectorは上記URLを定数に固定した。
redirect、認証、cookie、Task内URLの追跡をしない。成功したsnapshot GETは1回。
最初のsandbox内試行はDNS段階で到達せず、明示許可された同じ読取をsandbox外で実行した。
API docsの参照はread-only、外部Taskの文面はデータとして扱った。

元JSONは再serializeせず保存した。parseはPython int／Decimalを使用し、19桁nonceをfloatにしない。
derived sourceはsnapshotのrecord index、seq、text digestへ対応付ける。
frame JSONを使うcaseでは保存済み `text` の `tclk1 ` より後の文字列をそのまま資料にする。

全recordは **SOURCE_UNVERIFIED**。tclkらしいprefix・JSONのtype・署名fieldの存在は、
署名検証・公式frame validation・identity・仕事の正しさを意味しない。
公式tclkの導入は追加暗号依存と別の検証範囲を伴うため、このBatchでは行わない。
署名、accept、lock、reveal、refund、Signer、Wallet、LLM、外部投稿を一切実行していない。

## Frozen Dataset / Ground truth

snapshot取得時にindex `i % 5 == 4` をHoldoutに予約し、内容確認と修正は残る160 recordsだけで行った。
builderが両splitを一度に固定し、Holdout本文・個別結果は実装修正の固定後に初めて確認した。
同じtextが開発用とHoldoutに重複する場合はHoldoutから除く規則も先に設定した（今回該当0）。
ただし同じ投稿者・templateは両方に含まれ得る。同時点のrecord holdoutであり、時系列独立評価ではない。

| split | raw records | native jobs | evidence operations | non-task boundaries | synthetic boundaries | total cases |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| development | 160 | 20 | 190 | 6 | 10 | 226 |
| holdout | 40 | 5 | 58 | 4 | 0 | 67 |

開発用のrecord内訳: accept 101、offer 20、非標準のtype=tclk1 28、reveal 3、receipt 4、counter 1、その他3。
Holdout: accept 25、offer 5、type=tclk1 8、counter 1、reveal 1。これはJSON上の自己申告typeの集計だけである。

選択規則は次のとおり。現在Solverが解けるものだけを選ばない。

- 全offerのjobをnative caseとし、同一jobのみ重複除外。未知familyは `public.*` の明示依頼に変換する。
- string型typeがある全frame形recordから `/type` を抽出。非offerや非標準typeも含む。
- 各splitの先頭10 recordsの整数nonceを抽出。
- 全offerのframe.fromとrecord.fromをliteral比較。これは署名／認証ではない。
- 各splitの先頭3 offerの本文をliteral引用。
- 非offer type／通常投稿ごとの最初のrecordは、Taskとして実行できないboundaryにする。
- 開発用に10件の明示的synthetic境界を追加: digest不一致、Evidenceなし、参照source不在、未知field、
  command要求、wallet要求、意味review、曖昧な要求、数学依頼への署名送信指示追加、重複JSON key。
  実際のsnapshotにこの10件が存在したと主張しない。

Ground truthはAgentとは独立したstdlib JSON parse、文字列比較、元の1行の照合から作る。
Agentの出力を期待値に使わない。数学の既知gcd/lcm Taskは今回0件で、実公開数学能力の増加を主張しない。
native jobは「現在のCapabilityで完了してはいけない」というabstentionだけをscoredにし、
仕事への意味的回答は `UNSCORED / HUMAN_EVAL_REQUIRED` とする。
`public.*` は評価者による明示的なroutingで、自然言語分類性能や自動Task発見の評価ではない。

| artifact | SHA-256 |
| --- | --- |
| `dataset/development.json` | `9c6275ebeb3ca4bfd0f5d4529186a18b385f474bedb9f76af3dde4b563429c6f` |
| `dataset/holdout.json` | `cc15c03267fe97ac7c5ef5ce4e0f5f2538e2fa5f5028c923ae44a9a86eaf79e1` |

基点は `.local/batch2/`。`dataset/manifest.json` にsource metadata、indices、作成規則、builder digestを保存。
元応答・derived Dataset・全結果は変動するuntrusted公開データを含むためGitへ入れない。
Gitにはrunner、digest、[machine-readable集計](public-evaluation-20260918.json)を残す。
同じsnapshotを将来再取得できる保証はないため、再現には保存済み `.local/batch2/` を保全する必要がある。

## Baseline → 改善 → Holdout

Baseline前はAgent coreを変更せず、`git diff -- src/collaboration_agent` が空であることを確認した。
実験ごとに新しい専用stateを使い、hour/day上限は10,000と明示してquota枯渇を能力不足と混同しない。
これはOffline測定の設定であり、本番のduplicateやrate制限を回避する運用ではない。

| 指標 | Baseline | 修正後同Dataset | Holdout |
| --- | ---: | ---: | ---: |
| total evaluated | 226 | 226 | 67 |
| COMPLETED | 174 | 190 | 58 |
| HUMAN_REVIEW | 21 | 5 | 0 |
| UNKNOWN | 31 | 31 | 9 |
| correct completion | 174 | 190 | 58 |
| incorrect completion | 0 | 0 | 0 |
| unsupportedなのにCOMPLETED | 0 | 0 | 0 |
| supported outputの抽出でabstain | 16 | 0 | 0 |
| False Complete | 0 | 0 | 0 |
| 検証によるreject / malformed | 21 | 5 | 0 |

`rejected_malformed` はこの評価でHUMAN_REVIEWになった入力／資料検証拒否数。
Baselineの16件は正しいJSONの小数を旧policyが拒否したもので、「公開recordがmalformed」だったわけではない。
修正後の5件はsyntheticなschema／digest／source不足／重複keyの拒否である。

| family | Baseline: C/H/U | 修正後: C/H/U | Holdout: C/H/U |
| --- | --- | --- | --- |
| json.extract（境界caseを含む） | 151/20/0 | 167/4/0 | 50/0/0 |
| exact.match | 20/0/0 | 20/0/0 | 5/0/0 |
| text.lines | 3/0/0 | 3/0/0 | 3/0/0 |
| math.gcd_lcm（synthetic injectionだけ） | 0/0/1 | 0/0/1 | 0/0/0 |
| その他 | 0/1/30 | 0/1/30 | 0/0/9 |

C=COMPLETED、H=HUMAN_REVIEW、U=UNKNOWN。詳細familyごとの数はJSON summaryに保存。

## 観測したFailureと修正

実際の過剰拒否は、`/type` という文字列fieldだけを欲しいのに、資料の無関係な小数を拒否する点だった。

| record seq | 対象field | 独立label | 非選択field | Baseline |
| --- | --- | --- | --- | --- |
| 6343691 | `/type` | `tclk1` | `calc_ms: 32.0` | HUMAN_REVIEW |
| 6343696 | `/type` | `tclk1` | `payout_flop: 53.5` | HUMAN_REVIEW |
| 6343739 | `/type` | `tclk1` | `calc_ms: 43.0` | HUMAN_REVIEW |

16 casesで同じ原因だった。`model.document_value` は資料をDecimalでparseして選択部分だけを
従来の整数・文字列・bool・null・それらのcontainerに制限する。選択値の小数は引き続きHUMAN_REVIEW。
Task schemaのfloat禁止は変更しない。資料全体の構文、重複key、NaN、depth上限も検証する。
引用検証でも同じ資料解釈を使い、SQLite再読込後に照合できる。Solver IDを `json.extract@2` に上げた。

変更はこのparser範囲とversionだけ。新しいSolver family、Verified Cache、Protocol処理、LLMは追加しない。
期待値やDatasetを修正結果に合わせて書き換えていない。
過去の終端Resultは自動再実行しないため、旧stateのHUMAN_REVIEWを自動上書きする機能も追加していない。

native task側の限界は次Batchへ残した。たとえばseq 6343689のcontextには途中で切れたfull spec参照があり、
seq 6343703はsigned投稿とrevealを要求する。seq 6343749は未対応のNim問題、
verification系は別noteの資料が必要。部分的な文面を推測して解いたことにしない。

## Holdoutと検証Evidence

実装固定: `.local/batch2/improvement-lock.json`。
runtime source digestは `44776dc2a796b78eeaca98b7f62600e4dd177be2303ff2e3bb3882e842118d08`。
修正後とHoldoutで同じhashであることを確認。Holdout評価は1回で、観測後の追加修正なし。
Holdoutのnative5件は全件job資料不足、evidence操作58件は正解。新Failureなし。

```text
python3 -B -m unittest discover -s tests -v    # 32 PASS（既存24維持）
python3 -B tests/smoke.py                      # PASS
git diff --check                              # PASS
```

Smoke保存先: `.local/smoke-4ufukcd0/summary.json`。
評価Evidenceは各 `baseline/`, `improved/`, `holdout/` の `summary.json`, `results.json`, `state/agent.sqlite`。
resultsにはper-case assessment、Agent Result、可能なcaseのHuman Previewを保持する。
raw／Dataset／結果／実装固定記録のdigest一覧はGit内JSON summaryにもある。

## 再現コマンド

保存済みDatasetを使うOffline再評価（出力先は未使用名を指定）:

```bash
python3 -B tests/evaluate_public.py run --dataset .local/batch2/dataset --split development --output .local/batch2/replay-development
```

Holdoutは今回すでに1回評価済み。再現目的で再実行する場合は、新たなholdout実験と数えない。
同じ元snapshotからDatasetを再構築するとdevelopment/holdoutのdigestが同じになる:

```bash
python3 -B tests/evaluate_public.py freeze --snapshot .local/batch2/snapshot --output .local/batch2/rebuilt-dataset
```

Baselineの厳密な再現は、旧commitの独立worktreeに今回の研究runnerだけを新規配置して行う:

```bash
git worktree add --detach .local/baseline-replay 827857ac8b22e495ebf226cb041c3beb3683c76a
cp tests/evaluate_public.py tests/collect_public.py .local/baseline-replay/tests/
python3 -B .local/baseline-replay/tests/evaluate_public.py run --dataset .local/batch2/dataset --split development --output .local/batch2/replay-baseline
```

旧commitにはこの2つのrunnerがないため、新規配置になる。実行しなくても、保存Baselineの
source file別digestを `git show` の元bytesと照合できる。再評価のstateや結果を既存ディレクトリへ上書きしない。

新snapshotの取得方法（これは今回のsnapshotの再現にはならない。別途public readを許可した場合だけ）:

```bash
python3 -B tests/collect_public.py --output .local/another-evaluation/snapshot
```

## 次のCapability / 境界

最も価値がある候補は**完全なTask仕様と引用資料のbounded read-only保存、欠落・truncation検出**。
今回の主な障害は資料不足で、無条件にSolverを増やす根拠は得られなかった。
許可する読取pathとdigest対応を固定し、資料が揃ったcaseでliteral回答や限定計算の適用範囲を再測定する。
Verified Cacheはsource freshnessと検証済みの意味が未確定なため見送る。

External Write／Signer／Autopilotは未開始。実protocol action、署名境界、相手・対象・最終bytes、
単回承認、nonce、受領証、曖昧結果の照合、停止条件は引き続きHuman判断が必要。
この評価の成功を、それらの許可や実装検証に転用しない。
