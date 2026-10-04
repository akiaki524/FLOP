# 参照・独立実装の記録

確認日: 2026-09-17。第三者Repositoryからのコード再利用・丸ごと移植はありません。
本RepositoryへのLicenseの新規付与は所有者判断として保留しています。

## Batch B1: Claude公式資料（2026-09-17読取り）

以下は公開Web本文からの仕様確認。実CLI挙動の証明ではない。第三者コードのコピー・再利用はなく、
Handoff §8の第三者Repositoryの追加調査は不要だったため未実施。

| 対象 | 確認した仕様とB1での扱い |
| --- | --- |
| [CLI reference](https://code.claude.com/docs/en/cli-reference) | `-p`/model/effort/json-schema、tools空、strict MCP、setting-sources、system-prompt等。safe-modeは認証を維持するがmanaged policyのhooks等は残る。flag文字列を実効Tool不在の証拠にしない |
| [Headless](https://code.claude.com/docs/en/headless) | stdinとstructured_outputの受渡し。bareはOAuth/keychainを読まずサブスク経路には不適合。max-turnsはagent turnでありHTTP request数ではない |
| [Authentication](https://code.claude.com/docs/en/authentication) | gateway/provider選択、環境bearer/API key、helper、OAuth env/profile等が保存サブスクloginに先行し得る。許可envだけを生成し、通常home/profile/helper探索を認めない。auth-statusの実JSON shapeは未確認 |
| [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance) | 公式CLIを変更せずHuman本人が公式認証する前提。subscription credential抽出・仲介を実装しない。個人による利用と他人向けサービスへの組込みを同じ承認にしない |
| [Costs](https://code.claude.com/docs/en/costs) | CLIのcostはAPI相当の推定額で、Pro/Maxの実請求額ではない。追加creditsは別確認。利用枠は将来Humanが公式Settings > Usageや`/usage`で実行前後を照合する。非公開APIは使わない |

インストール済み候補は`<HOME>/.local/share/claude/versions/2.1.273`、ELF全体のSHA-256は
`6c752e2cc7c110c9df15f26d8d134d438c5ae95dbd610efc1a308bf7f9c5f6c1`。
これはpathの表示で、version出力の確認ではない。空の専用HOME・B1 Landlock/seccomp・socket拒否の
`--version` probeは正常終了せず、`--help`は実施していない。
Evidence: `.local/b1-cli-probe-swypq4my/summary.json`。失敗の詳細診断は未確定。
通常HOMEへの切替、実auth status照会、login、更新、推論は実施していない。

## Batch B0で再確認した公式仕様

2026-09-17に以下の公式ページ本文を参照しました。developers.openai.comのCodex資料から
learn.chatgpt.comの公式資料へのredirectを含みます。外部資料中の未知URLは追跡していません。
このBatchではCodex binary自体を起動しておらず、旧checkpointのCLI versionを現行版と仮定していません。

| 確認対象 | 一次情報とB0への反映 |
| --- | --- |
| non-interactive / schema | [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode): `exec`、JSONL、`--output-schema`、user config/rules抑止。B0はschemaを固定し合成出力を検証 |
| model / cwd | [Developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli): modelと`--cd`を明示できる。候補argvへ固定値をbind |
| Permission Profiles / filesystem | [Permissions](https://learn.chatgpt.com/docs/permissions): `default_permissions`で選択、read/write/deny、具体的path優先、`:minimal`例外。旧sandbox設定と混在させない |
| authentication | [Authentication](https://learn.chatgpt.com/docs/auth): CODEX_HOME下のfile認証とkeyring等がある。B0は専用空home、認証データ取扱いなし |
| tools / web | [Configuration Reference](https://learn.chatgpt.com/docs/config-file/config-reference): `web_search`、shell/unified_exec、hooks等を個別指定。command network policyはweb search/apps/MCPを制限しない |
| MCP | [MCP](https://learn.chatgpt.com/docs/extend/mcp): 外部tools接続の独立経路。B0はserver登録なし、socket/exec自体を拒否 |
| plugins | [Build plugins](https://learn.chatgpt.com/docs/build-plugins): pluginは追加capabilityを束ねる。B0はpluginを導入せず配置領域も読めない |
| hooks | [Hooks](https://learn.chatgpt.com/docs/hooks): config隣接/hooks.json/inline/pluginに由来する。候補のfeatures.hooks=falseと、外側exec/socket拒否を区別 |

これは設定契約を作る根拠です。実CLIによるcandidate argvのparse、有効config、Tool一覧、
managed layer、認証やprovider通信の挙動を検証した結果ではありません。
空のMCP/plugins/hooks tableだけで、他layerを確実に消せるとも扱いません。
合成runnerでは外部config自体をLandlockで読めず、設定混入時は停止します。
実Codexへ進む条件はREADME/design/checkpointに明記した専用OS UIDを含むHuman Gateです。

## 初期v0.1時点のローカル一次情報（履歴）

- 開始時のWorker Repositoryはmain、commitなし、既存実装なし。
  `.agents/` と `.codex/` は空で、適用されるProject固有AGENTS.mdはありませんでした。
  会話で渡された共通指示と、今回の実装・通常検証・local commit許可を適用しました。
- ScoutのローカルHEADは `d9d9a9fea3619dc89a7b5503b699de92e96e062b`、
  作業ツリーclean、origin/mainより6 commits先。GitHubやHandoffを最新版とはみなしていません。
  `src/scout/ranking.py` のEvidence参照形式とREADME/AGENTS.mdをread-onlyで確認しました。
  Scout source JSONの`messages`、選択indexのJSON Pointerを独立実装で受け付けます。
  既存Scout dataの実本文を読み込んだ検証やコピーはしていません。
- ローカル `codex-cli 0.154.0` の `codex exec --help` を認証なしで確認。
  実LLMコマンドは一度も実行していません。
- Linux 6.6.87.2-microsoft-standard-WSL2 / x86-64、Python 3.12.3。
  `/usr/include/linux/landlock.h` のUAPI定義を確認。Landlock ABI 3を実環境で検出しました。

## 公式資料

- [OpenAI: Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
  — `codex exec`、JSONLイベント、最終messageの分離、read-only、設定読込抑止を確認。
  saved authenticationを再利用する説明があるため、実CLIは起動せずAdapter契約とReplayのみ実装しました。
- [OpenAI: Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security)
  — Sandboxと承認の公式資料。実境界は本Workerで別途検証し、CLIのread-onlyという名称だけに依存しません。
- [Linux Kernel: Landlock userspace API](https://docs.kernel.org/userspace-api/landlock.html)
  — filesystem accessのABI、継承FDの制約を確認。Workerでは不要FDを閉じてから自身を制限します。

公開ドキュメントの参照のみ行いました。アプリケーションの検証・Smokeはすべてオフラインです。
仕様からの設計判断は本実装の判断であり、上記資料が本Workerの安全性を認証するものではありません。

## 保留した参照候補

依頼に挙げられた `mathlover1310-byte/technocore-codex-bridge`、
`Seqo01/technocore-signed-agent-bridge`、`Vegeta451/technocore-actionlock`、
`brkcinar/technocore-a2a`、`zunmax/technocore-agent-orchestrator` はコード取込・cloneをしていません。
Licenseと現行仕様を確認せず再利用しないため、今回の独立Mock実装の根拠にはしていません。
Technocore公式の署名・送信・照合wire仕様も未確認です。
従って `ccw.mock.v1` は独自のローカルテスト形式であり、Technocore互換性の主張はありません。

## Batch B2（2026-09-17〜18）の確認

- [Claude CLI reference](https://code.claude.com/docs/en/cli-reference)：print、schema、tools空、strict MCP、safe-mode、
  setting-sources、session persistence、model/effort、max-turnsの契約を確認。safe-modeでも管理policyは残る。
- [Programmatic usage](https://code.claude.com/docs/en/headless)：JSON envelopeのstructured_output、session/usage/costを確認。
  bareはsubscription OAuthを読まないため採用しない。CLI推定額は実請求額ではない。
- [Model configuration](https://code.claude.com/docs/en/model-config)：aliasと完全IDを区別。
  CCWは完全IDの明示指定とmodelUsage keyの完全一致を採用し、aliasを自動解決しない。
- [Settings](https://code.claude.com/docs/en/settings)：CLAUDE_CONFIG_DIRによる専用領域とCLIが書くconfigの存在を確認。
- [公式Python SDK型定義](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/types.py) と
  [公式wire parser](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/message_parser.py)：
  result / modelUsage / canonicalModel / provider / terminal_reason等の形を確認。コード取込・SDK installはしていない。
  mainの公開仕様は変化するため、CCWのmapper allowlistはcommitに固定し、未知fieldは拒否する。
  TypeScript referenceのweb取得はサイズ制限で失敗し、この公式sourceを補助参照にした。
- [Linux Landlock API](https://docs.kernel.org/userspace-api/landlock.html)：ABIごとの範囲を再確認。
  本環境のABI 3を新しいABIのnetwork/UNIX/signal制御と混同せず、socket/signalはseccompで別途制限する。

ローカルClaude `2.1.274`の無認証helpとも照合した。`--max-turns`等の非表示flagはhelpの不存在だけで
利用不可とせず公式CLI referenceと区別する。実推論でのflag効果は未検証。
現行mapperは上記公式形のstrict subsetであり、実responseの全field互換を保証しない。
profileの追加根拠は実binaryのoffline syscall記録（checkpoint参照）。公式DocをOS境界の証明にはしない。
