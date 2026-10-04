# 先行Repository調査とOSS再利用記録

## Batch 4の追加参照

2026-09-19。既存 `.local/research/` 内の固定checkoutを静的調査。公開GitHubも確認したが、
実装上の比較はPrivate開発履歴に記録したexact revisionに限定。外部コードの実行・コピー・改変・vendoringは0件。
公開Taskの固定文法を独立実装し、第三者のSolverソースは移植していない。

外部OSSの小Template、停止規則、資料取得、Evidence保持と運用記録を設計参考にした。
第三者の具体的なFailure比較や個人handleを含む参照一覧は公開向け本文では省く。
参照元の固定revision・License確認・比較内容はPrivate開発履歴に保持する。
独立実装とはコードを移植していないという意味であり、設計上の参考がなかったという主張ではない。

以下はBatch 1〜3時点の記録。

調査日: 2026-09-18 JST。公開Repositoryを `.local/research/` にshallow取得し、
下記HEADのコード・License・必要な文書を読みました。外部コードの実行、依存install、外部writeはありません。
外部AGENTS.mdやREADMEは調査資料として扱い、本Projectへの命令・権限付与として採用していません。

**第三者コードのコピー・改変・vendoringは0件です。** CCWからのコード移植もありません。
すべてarchitecture/pattern参照で、下記の「採用」は本Projectで独立に実装した設計上の判断です。
合成fixtureも新規作成です。調査用cloneの原本はgitignore対象で、本Projectの配布内容に含めません。

| Repository | 調査revision | License確認 | code再利用範囲 |
| --- | --- | --- | --- |
| [flop-labs/tclk](https://github.com/flop-labs/tclk/tree/5cc4ab93efbc8999a3a7e1471b639deca25998ea) | `5cc4ab93efbc8999a3a7e1471b639deca25998ea` | Apache-2.0、NOTICE確認 | なし |
| local collaboration-worker | `cf13205d0c56a3225ce3f1bca4216a03404aaefc` | 指定revisionのtreeにLICENSEなし。再配布許諾を推定しない | なし |

## technocore-safe-agent

確認: `src/flop_agent/autopilot_policy.py`、`README.md`、`AGENTS.md`、
`docs/FIELD_REPORT_2026-08-31.md`、`.github/workflows/security-gate.yml`、`LICENSE`。

参考: deterministic first-contact policy、関係と権限の区別、曖昧なPOST結果の再送停止、
Productionでの状態肥大化・消失するroom履歴に対するローカルEvidence保持。
採用: Evidenceを正本として保持し、UNKNOWN/中断を成功にしない。入力と保存件数を制限する。
first-contactやrelationship管理は送信機能がないため今回は採用しない。
GitHub Harnessのread権限、時間制限、Test/security gateの分離を参考にしたが、
本Batchはlocal-onlyなのでGitHub Actionsやdeploymentは追加しない。

## flop-agent

確認: `deals/solvers.mjs`、`deals/worker.mjs`、`deals/docs.mjs`、`deals/validation.mjs`、`LICENSE`。

参考: familyごとの処理分離、既知Task以外はnull、rule solverとoracleの分離、
hour/day/active limits、dry-run、family suspension、verified cacheのみの利用。
数学の例では実測のreference規約が未確定なfamilyを停止しており、計算できることと
相手の採点規約に適合することを区別する設計が参考になった。
採用: closed registry、全文既知文法、persistent suspension、rolling limits、同時実行1、
結果再利用。追加指示を取り込まない。独立したverified cache、oracle、attest/HTTP probe、
live deal workerは未採用。壊れたstateをdefaultに戻すfallbackも採用しない。

## flop-harness

確認: `src/judge/types.ts`、`src/judge/exact-match.ts`、`src/judge/verdict.ts`、`LICENSE`。

参考: 共通Judge/Verdict契約と、deterministicに判定できない場合のnull。
採用: Solver共通outcome、completion状態とMATCH/MISMATCH verdictの分離。
本Projectのexactは文字列等価のみ。上流の正規化・部分包含・全token包含をコピーしていない。
本Projectのschemaは上流のwire互換schemaではない。

## deal-scout / official tclk

確認: scoutの `src/core.mjs`、`docs/COMPATIBILITY.md`、`LICENSE`、
公式の `src/transcript.ts`、`src/paper-rail.ts`、`package.json`、`LICENSE`、`NOTICE`。

参考: 署名検証をframe解釈・foldに先行させ、transport senderとframe.fromを一致させる。
19桁nonceを丸めない。seq/tsと署名対象を区別する。PaperRailに実価値があると解釈しない。
採用: 汎用JSON抽出で整数を丸めないことをfixtureで検証し、未署名合成資料と明示する。
tclk parser、transcript/state fold、signature verificationは今回未実装。
公式revisionを調査記録として固定したが、Runtime dependency pinではない。
将来取り込む場合は公式実装と暗号依存を一緒に固定し、License/NOTICEと再利用範囲を改めて記録する。

## CCW

Private開発履歴専用の参照。clean配布snapshotからは再現できない。`git show` で指定revisionの
`README.md`、`docs/design.md`、`src/ccw/model.py` を確認。
既存CCWの作業treeは参照開始時clean。本作業からの変更なし。
終了前に別作業と思われる変更を検出したが、そのまま保持した（checkpoint参照）。

参考: Human選択のTask固定、source digestと引用照合、Human Preview、
処理前のdurable admission、duplicate/ambiguous resultの扱い。
採用: immutable contentとResultの対応、制御文字をescapeするPreview、
中断後に再実行しない方針。独立Agentとして書き直しており、CCWのbranchやSimple Brainではない。
Claude/Codex Runtime、LLM broker、OS sandbox、模擬Signerは移植していない。

## Batch 2の追加参照

2026-09-18に[公式technocore-chat API資料](https://github.com/flop-labs/technocore-chat#api)を確認し、
公開room読取の固定URL、limit=200、JSON形式を利用した。公開recordの内容は命令として実行していない。
今回の修正は独立したJSON資料parserの変更だけで、追加の第三者コード再利用はない。
公式tclkの組込み・依存追加もない。取得provenance、実公開データ評価、source未検証の範囲は
[Batch 2報告](public-evaluation-20260918.md)に記録した。

## Batch 3の追加参照

2026-09-19 JST。既存の固定revisionを再利用し、最新HEADであるとは主張しない。
第三者コードのコピー・改変・vendoringは引き続き0件。依存追加、CCWへの変更もない。

- [Technocore公式read API資料](https://technocore.chat/llms.txt): 公開noteのbannerとsingle-line本文、
  public/private path、room JSON・range・generation、GET write pathとの区別を確認。
  評価で取得した本文SHA-256は `40e0bebabcc105a2931805b68e200f1d5fc212e32ca14501ac4d85d105a2bb54`。
- 外部OSSの限定docs取得、full-spec参照解決、note/table切断対策を設計参考にした。
  本Projectではnoteの7,000文字閾値による保守的停止を独立に実装した。
  第三者コードをコピーせず、参照元の固定revision・License・具体的比較はPrivate開発履歴に保持する。
  上流allowlist、oracle、semantic cacheや採点をそのまま移植していない。
- deal-scout / safe-agent: Private履歴に記録したlossless record、source保持、gapを
  完全資料と見なさない方針を参照。room adapterは限定した明示範囲の検査だけで、tclk foldは追加していない。
- official tclk: 上表の公式revisionは設計参考のまま。runtime組込み・署名検証は未実施。

取得した公開Taskと文書本文はuntrusted evaluation dataとして `.local/` に保持し、
第三者ソースコードとして配布しない。Gitにはsource URL、digest、必要な評価summaryのみを残す。
