# Initial Offline Implementation checkpoint

2026-09-18 JST。独立Repositoryのv0.1初回実装。

## 実装済み

- 明示family classifier、closed Solver Registry、4種類のdeterministic処理。
- UNKNOWN / HUMAN_REVIEW / COMPLETEDと判定verdictの区別。
- 固定Task、本文digest、引用検証、Result digest、未承認Human Preview。
- SQLite永続化、audit hash chainと対応検証、重複・ID再利用拒否。
- persistent enable/disableとsuspension、rolling hour/day limits、process lock。
- dry-run、処理中断のterminal Human Review、破損時fail-closed。
- 複数CLIプロセスを通るSmokeと、実際に途中終了させる耐中断Test。
- 操作・境界・先行調査revision／License／不採用理由の文書。

## 検証

```text
python3 -B -m unittest discover -s tests -v
python3 -B tests/smoke.py
git diff --check
```

結果: **24 tests PASS**、**CLI Smoke PASS**、`git diff --check` PASS。
SmokeはCOMPLETED 4件、UNKNOWN 1件を通常保存し、malformed入力のHUMAN_REVIEWも確認。
duplicate、dry-runの非admission、family suspension、5件の未承認Preview、再open時のaudit検証がPASS。
中断、cross-process lock、hour/day境界、時計巻戻り、DB/audit改変検知はTestで確認。

保存済みSmoke: `.local/smoke-mwcbfoos/summary.json`、同ディレクトリの
`preview-exact.json` / `preview-extract.json` / `preview-lines.json` / `preview-math.json` /
`preview-unknown.json` と `state/agent.sqlite`。`.local/` はcommit対象外。

参照先CCWは開始時cleanだったが、終了前確認で `src/ccw/claude.py`、`src/ccw/claude_real.py`、
`src/ccw/isolation.py`、`src/ccw/llm.py`、`tests/test_claude_b3.py` の変更を検出。
本作業によるCCWへの書込みはなく、これらの変更を操作していない。

## 停止地点

Offline coreのcheckpointで停止する。External Write、Signer、Wallet、Production変更、
LLM統合、Autopilot、常駐、push/publishは未実施。依存installなし。第三者コードの移植なし。

次の実運用ステップは、Humanが選択した公開Taskを保存して、既知familyへの対応率、UNKNOWN理由、
referenceとの一致を小規模に測定すること。実投稿の成功やtclk互換をこのSmokeから推定しない。
Humanは次Gate前に対象・相手・署名境界・最終bytes・単回承認・nonce・曖昧結果の照合・
停止条件・Evidence保持責任を決める。今回の実装完了をその承認として扱わない。
