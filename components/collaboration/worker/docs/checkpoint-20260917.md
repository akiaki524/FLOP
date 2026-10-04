# Controlled Collaboration Worker v0.1 — Batch B0 checkpoint

後続の現行判断は[Batch B1 checkpoint](checkpoint-b1-20260917.md)を参照。
以下はB0/Batch Aの履歴であり、当時の専用UID必須等の判断を現行の確認結果として扱わない。

基準commit: `227557b6479f04b300e19648d230704832f4ad8d`。検証日: 2026-09-17。
**実LLMを一度も呼んでいません。実Codex invocationはDISABLEDのままです。**

## B0の実装と判断

- Human操作の`llm-preview/approve/run-synthetic/retry/status/verify/export`を追加。
  Task/model/provider/attempt・時間・bytes・費用の上限/request digestを承認にbind。
- SQLiteの単回permit消費・attempt・budget予約をchild起動前に永続化。
  failed/startedは自動retry不可。再attemptはHumanの直前ID確認と残budget・有効期限が必要。
- 新規最小cwd、空の専用HOME/CODEX_HOME、read-only Landlock + seccompの別process合成runner。
  標準I/O以外のFD/環境を除去。exec/networkを拒否。timeout・出力bytes上限をbrokerで強制。
- brokerが実際に得たraw responseとprovenanceをHuman側に保持。
  WorkerのEvidenceコピーを改変しhashを再計算しても、Human DB照合で拒否。
  Human previewまでEvidenceを維持し、除去による格下げも拒否。
- JSON Schemaとmodel必須の候補Codex設定/argvを追加。不要なdestinationをpromptから除外。
  prlimit64をpid=0に限定し、Human stageのdirectory symlinkを拒否。

## B0検証

| 検証 | 結果 |
| --- | --- |
| `python3 -B src/ccw/isolation.py` | Landlock ABI 3 |
| `python3 -B -m unittest discover -s tests -v` | 47 tests PASS（既存26 + B0追加21、最終9.894秒） |
| runner attack probe | 43項目すべてPASS。別途sandbox unavailable時の入力前停止もPASS |
| `python3 -B tests/smoke.py` | 従来4送信ケース・Scout + B0承認/中断/再許可/合成runner/Worker/Human preview PASS |

最終Smokeの成果物: `.local/smoke-18xd2v5w/summary.json`。配下`batch-b0/human/llm.sqlite`に
中断/成功attemptとbroker Evidence、`batch-b0/runner/`に承認済み契約・schema・最小workspaceを保持します。
これらはgitignore対象です。`git diff --check`もPASSしました。

probeは**Repositoryの.local内に作った合成canaryのみ**を対象にしています。
Human DB/Signer file、他.local、通常homeの.codex/.ssh、無関係Repository、project config/hooks/plugins相当を
用意し、read/write拒否を確認しました。実home・実credential・auth.jsonは読取り・作成・コピーしていません。
errno=ENOENTは成功扱いせず、許可workspaceのread成功も正の対照として確認します。
FD（低位/高位）・symlink/hardlink・proc FD・TCP/Unix socket・exec/fork・signal・ptrace・process_vm・
他processのprlimit拒否、cwd/環境の固定を検査します。
別testでhome内config/hooks/pluginsの混入停止、timeout、過大出力、schema異常、承認改変・期限・
同時実行lock・request累積budget、強制終了後のattempt残存、Evidence改変/除去を検査しました。

## Independent Review findingsの扱い

| finding | B0で確認した範囲 | Real LLM開始前の残Gate |
| --- | --- | --- |
| P1-1 runner隔離 | Worker外の別process、最小cwd、入力前Landlock/seccomp、合成flowを実装・probe | 実Codex executable/runtimeを含むlauncherは未実装。専用OS UIDが必要 |
| P1-2 Human/Signer/credential境界 | 隔離済み同一UID runnerからcanary read/write、brokerへのprocess操作を拒否 | 未隔離同一UIDは保護外。実Codexのexec/auth/network追加後の境界は未証明。専用OS UIDが必要 |
| P1-3 LLM-use approval / ledger | 利用条件binding、起動前commit、単回permit、明示retryとbudgetを実装・検証 | 実providerの内部retryと課金上限強制はlive化前に別途検証 |
| P2-1 broker provenance | provider/model/合成CLI version/task/request/response/attempt等をbroker側に記録し改変検知 | 実CLI version・実provider/model応答の採取は未検証。synthetic Evidenceで代用しない |

**Batch Bの実呼出しblockerがすべて閉じたという判定ではありません。B0のoffline実装を完了し、
live化に必要なGateを具体化したcheckpointです。** 安全性を証明できない範囲を設定意図だけで閉じていません。

未解決P0: 本作業で新規確認なし（独立Review前）。
未解決P1: P1-1/P1-2の実Codex用隔離。必要な専用UIDの作成・権限設定は今回行っていません。
未解決P2: 実binary/version、有効config/tool一覧、provider provenanceの実環境検証、実利用上限/内部retry制御。
P2-3一般Secret/URL scanner、実送信DID/nonce/idempotency/receiptは今回の対象外です。
P3: 新経路のconditional UPDATE・厳密probe・prlimit・stage symlink・prompt縮小・同一UID限界を改善。
既存Worker遷移/Signer送信のconditional UPDATE一般化は対象外のままです。

## 次のReviewとHuman承認

次のIndependent Review対象はB0差分全体、とくに`llm.py`のpermit/ledger順序・retry・Evidence、
`runner.py`と`isolation.py`の入力前制限、Human previewの改変/削除検出、probeの正負対照です。
その後、実Codex用launcherを別作業として設計・実装し、専用UID、CODEX_HOME/credential broker、
effective config、shell/web/MCP/plugins/hooks/Browser等のTool不在、endpoint制限と費用上限を検証します。

初回Real LLM前にHumanが承認する具体的内容:

1. 対象task digest、資料の外部送信許可、最終prompt/schema/config/argvを含むrequest digest。
2. providerと送信endpoint、固定model名、CLI binary digest/version、専用OS UIDとCODEX_HOME配置。
3. 初回最大1attempt、timeout、input/output bytesとtoken上限、総費用上限、TTL。
4. 認証の保管・受渡し方法（通常credentialからの流用を前提にしない）、禁止Tool一覧と隔離probe結果。
5. failed/interrupted時は自動再送しないこと、Human照合・再許可と残budget、保存するEvidenceと保存先。

本Batchのapprovalデータはsynthetic試験用です。実LLMの利用承認でも、実送信・Signer有効化の承認でもありません。
OpenAI公式仕様の現在値と限界は[参照記録](references.md)を参照してください。
push/publish、Production mutation、Scout/Observer変更、実Secret取扱い、sudo/UID作成、権限拡大は行っていません。

---

## 初期v0.1 checkpoint（基準commit時点の履歴）

検証日: 2026-09-17。実LLM利用・実送信・実協働の成功を意味しません。

## 検証結果

| 検証 | 結果 |
| --- | --- |
| `python3 -B src/ccw/isolation.py` | Landlock ABI 3検出 |
| `python3 -B -m unittest discover -s tests -v` | 26 tests、すべてPASS、最終実行4.970秒 |
| `python3 -B tests/smoke.py` | 4送信ケース + 合成Scout Evidence取込・Preview、PASS |
| `git diff --staged --check` | PASS（commit前確認） |
| Scout status | 開始時と同じclean、origin/mainより6 commits先 |

子プロセス内の17個のprobe結果により、ダミー鍵・合成認証・承認のread拒否、承認write拒否、
低位/高位継承FD閉鎖、symlink/hardlinkによる領域越境拒否、TCP/Unix socket拒否、
exec/fork/ptrace/process_vm拒否、環境クリア、Human Signer呼出し拒否とworker領域の通常I/Oを確認しました。
隔離不可時の停止、直接ファイルredirect拒否、同時実行lock、入力改変、引用改変、
承認後の本文・宛先改変、期限切れ・取消・二重承認、停止markerをWorkerが消した場合も検証しています。

Smokeの最終成果物は `.local/smoke-zujd98jf/summary.json` とその配下です。
このデータは合成ですが、承認/ledgerの運用領域をcommitしない方針に従いgitignore対象です。
再実行すると別の新規ディレクトリを作成します。

| 模擬シナリオ | 初期状態 | Human照合後 | delivery数 | 再送 |
| --- | --- | --- | --- | --- |
| success | sent | sent | 1 | 拒否 |
| lost-before | unknown | unknown | 0 | 拒否 |
| lost-after | unknown | sent | 1 | 拒否 |
| crash-after（exit 75） | in_flight | sent | 1 | 拒否 |

同じ合成依頼のtask IDは `4deb5f3f8201b8632962896d1e1a9c50939c2bf26432ad10e6a9559537c76a99`、
payload digestは `ab1860b5b6dd6ff1c98451c3805e8da17dc0dc7627768d9b3b2a4da9c27d4981`。
全ケースは別の新規rootで独立して検証しています。

## 未検証・未実装

- Codexの実推論・実認証・実CLI runner。Adapterは呼出し契約と合成CLI応答Replayまで。
- Technocoreの現行公式wire仕様、実署名、実nonce、実受領証、実送信、実相手との協働。
- 別OS/CPU、異なるfilesystem、OSユーザー分離、rootや侵害されたbootstrapからの保護。
- 実Scout本文を使った品質評価、レビュー内容の意味的妥当性、投稿者の本人性・資料の最新性。
- DBのmigration/復元、自動再送、unknownの解除。いずれも自動で実行しません。

実Secretへのアクセス、依存追加、Production/Observer変更、push/publish、権限拡大は行っていません。
公式Web資料の閲覧と `codex --version` / `codex exec --help` の参照のみ実施しました。
アプリケーション検証はオフラインです。

実LLM利用・実送信に必要な別途実装と具体的なHuman承認はREADME末尾に記載しています。
