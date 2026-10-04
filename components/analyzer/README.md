# Technocore Analyzer

Observerが保存したEvidenceを **read-only** で読み、状況・機会・Protocol状態・Actor活動履歴・不正/異常候補・報告案へ変換するComponentです。要求仕様は [SPEC.md](SPEC.md)、実装との対応と未実装/未検証は [docs/implementation-status.md](docs/implementation-status.md) を参照してください。

**これは実装とOffline検証の段階です。実Observer保存物・Production・実LLM接続での確認は行っていません。**

- Python 3.11+、stdlibのみで動作。`cryptography`（optional）があるとEd25519署名とsecp256k1（tclk point lock）を検証します。無い場合、署名は `VERIFIER_UNAVAILABLE` のまま（`VALID` にならず、DIDへ帰属しない）で、導入後の次回runで再検証されます。
- 常駐processはありません。`run` は有限の1回実行です。定期起動は既存schedulerから呼ぶ想定で、Productionでの定期起動は別の運用承認です。
- 外部通信・送信・署名は行いません（LLM runtimeを明示有効化した場合の公式CLI起動を除く）。

## 使い方

```sh
cd components/analyzer
PYTHONPATH=src python3 -B -m technocore_analyzer --config analyzer.json run
PYTHONPATH=src python3 -B -m technocore_analyzer --config analyzer.json run --as-of 2026-10-04T09:00:00Z   # 履歴再生
PYTHONPATH=src python3 -B -m technocore_analyzer --config analyzer.json findings --unreviewed
PYTHONPATH=src python3 -B -m technocore_analyzer --config analyzer.json show F-xxxxxxxx
# Human専用（origin=HUMAN_CLIとして追記。LLM経路からは呼ばれない）
PYTHONPATH=src python3 -B -m technocore_analyzer --config analyzer.json review F-xxxx --revision <n> --decision CONFIRMED --reviewer <name>
PYTHONPATH=src python3 -B -m technocore_analyzer --config analyzer.json report-event F-xxxx --revision <n> --state SUBMITTED --actor <name> --channel <where> --candidate-sha256 <sha>
PYTHONPATH=src python3 -B -m technocore_analyzer --config analyzer.json resolve F-xxxx --revision <n> --resolution FIXED --actor <name> --source-ref <official reply>
PYTHONPATH=src python3 -B -m technocore_analyzer --config analyzer.json llm-preflight
PYTHONPATH=src python3 -B -m technocore_analyzer --config analyzer.json rebuild   # 派生cacheのみ削除（整合性factとsource identityは保持）
```

設定例は [examples/analyzer.example.json](examples/analyzer.example.json)。観測Room一覧はAnalyzer側で正本化せず、`observer_configs` に指定したObserverの設定ファイル（multi-room / production）から導出します。移管先媒体は `path_map` で読み替えます。Observer設定で表せない入力（lobbyの `state.sqlite` snapshot等）だけ `sources` に追加します。

## 入力

[docs/evidence-adapters.md](docs/evidence-adapters.md)。Defaultは `technocore_full_capture` のArchive（公開済みshard）。`manifest.sqlite` と稼働SQLiteの読取はopt-in。

## 出力

`<output_dir>/runs/<run_id>/`（commit後に `latest.json` が指す。`latest.json` は同じdirectoryの一時fileへ書いてfsyncしてからatomicに置換するので、公開に失敗しても直前のcommit済みrunを指したまま）:

| file | 内容 |
|---|---|
| `situation.json` / `situation.ja.md` | Situation Report（Canonical JSONと日本語表示） |
| `findings.json` | live: 全Findingの最新revision + 今回の変更（NEW / UPDATED / UNCHANGED / NO_LONGER_DETECTED）。replay: as-of時点で再構成したrun-local Finding（変更なし） |
| `opportunities.json` | Opportunity（tclk offer / 自由文候補）。semantic結果は `Inferred` として別field |
| `tclk.json` | Protocol Tracker（contract状態、record判定、Coverage、期限状態） |
| `actors.json` | DID別活動履歴、未署名nickname（別名扱い）、帰属しない主張 |
| `coverage.json` | Coverage Advisory（何が不足し何を判断できないか） |
| `report-candidates/` | 報告案（internal / public派生 / 日本語md）。**送信しない** |
| `semantic-bundles/` | LLMへ渡す（渡し得る）有限Bundleと加工記録 |
| `notifications.jsonl` | 承認済みNotifier向け最小event。Analyzerは配送しない |

全出力に schema版、生成時刻、as-of / データ基準時刻、対象範囲、入力digest、適用Profile/rule/model、処理状態、前回runを付けます。

## 解析の分担

| 決定論的（コード） | Semantic（任意・LLM） |
|---|---|
| 件数、署名、hash、構文、tclk状態遷移、期限、Coverage、重複/頻度rule、Event sweep | 自由文の分類・関連性説明、keyword外表現の定期sample点検 |

LLM runtimeは Default `none`。`fixture` はOffline評価用で実推論ではありません。`claude-code-cli` は、Humanの課金/認証確認記録があり、上位credentialの環境指標が無く、直前の `claude auth status --json` がHumanの固定した期待値と一致した場合だけ起動します（`run()` を直接呼んでも同じ検査）。`--safe-mode --restricted`、Tool/MCP無効、prompt拒否、空cwd、timeout、process group killで実行します。確認できない場合は実行しません（未実行・未検証。制約は [docs/implementation-status.md](docs/implementation-status.md)）。

`state_db` / `output_dir` はObserver Source / rootと重ねられません（config読込時に拒否）。stateとoutputは0600 / 0700で作成します。Review・報告event・Resolutionは finding revision（報告eventは加えてcommit済みrunの報告案digest）に束縛され、Findingが改訂されると再Review / 再評価が必要になります。`--reviewer` / `--actor` が空・空白のみの場合は書込前に拒否します。

### `--as-of` 履歴再生（replay）とlive runの境界

- live run（`--as-of` なし）だけがFinding lifecycle（revision追加、`NO_LONGER_DETECTED`、failed run分の `RECOVERED_FROM_FAILED_RUN`、報告案digest、通知event）を読み書きします。
- replayはEvidence cacheを共有しますが、Findingはその場で再構成したrun-localな派生結果（`lifecycle: REPLAY_DERIVED`、revision・Review/報告/解決の束縛なし）として `findings.json` に出すだけで、永続Findingを作成・改訂しません。報告案・通知eventも作りません。全出力の `run_mode` が `replay` / `current` を示し、`revision_of` も同じmodeの前回runだけを指します。
- 1つのstate DBにつき同時に走れるrunは1つです（`<state_db>.lock` のflock。live/replay問わず、実行中に別runを起動すると `ANALYZER_RUN_IN_PROGRESS` で拒否し、何も変更しません。`rebuild` も同じlockを取り、run中は拒否します）。RUNNINGのまま残った行は、lockを誰も持たない時（前のprocessが死んだ時）だけ次のrunがinterruptedとして `FAILED` にします。
- 保存済みCoverage item（manifest GAP/boundary/late observation、pending checkpoint）は時刻を持たないため、replayでは当時存在したと推定せず除外し、Coverage `REPLAY_COVERAGE_UNPLACED` に種別ごとの件数を示します（live runは従来どおり）。
- Message系Evidenceはas-of以前の `ts` のものだけを使います（時刻を置けないrecordは除外し `REPLAY_TIME_UNPLACED`）。Evidence integrity factは、Analyzer自身の検出時刻を持つrecord conflict / 公開済みshard変更だけを「検出時刻 ≤ as-of」で採用します。検出時刻を置けないquarantined unit・Observer `CONFLICT` receiptは当時存在したと推定せず除外し、Coverage `REPLAY_INTEGRITY_UNPLACED` に件数を示します。

## Offline検証

```sh
python3 -B -m unittest discover -s tests -v
```

testはObserverの `src/`（Spool / ArchiveWorker / storage schema）で実形式のfixtureを作ります。tclkはflop-labs/tclk referenceのgolden vector（offer id / contract id / canonical line）と照合します。
