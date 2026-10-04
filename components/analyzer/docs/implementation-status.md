# Implementation Status（SPEC v1.1 対応表）

記録日：2026-09-29。実装者：Claude Code（今回に限るHumanの明示指定によるPrimary Implementation）。Independent Review未実施。

区分：**実装**＝コードとOffline testあり ／ **部分**＝一部のみ ／ **未実装** ／ **未検証**＝実環境・実データ・実LLMで未確認。
Offline test（合成データ）はすべて `tests/` にあり、実Observer保存物・Production・実LLMでは確認していない。

## 全体

| 項目 | 状態 |
|---|---|
| 決定論的機能（Adapter、署名、tclk、Radar、Opportunity、Anomaly、Actor、Coverage、Finding、報告案、Close Call Profile） | 実装・Offline検証のみ |
| 実Observer保存物（VPS / 移管媒体）での読取 | **未検証**（Observerの実コードで生成したfixtureでのみ確認） |
| 実LLM（Claude Code CLI）接続 | **未検証・未実行**。Human Gate |
| Production定期起動、共有資源下での負荷 | **未検証**。Human Gate |

## 節ごとの対応

| SPEC | 状態 | 内容 / 制約 |
|---|---|---|
| §2 権限分離 | 実装 | Coreは読取と自DB書込のみ。送信・署名・Webhook・Observer操作なし |
| §3 情報区分 | 実装 | 出力に `information_class`（Observed/Derived/Inferred）、署名は検証方法付きの別status。`verified` の一括表示なし |
| §4 観測範囲 | 実装 | Room一覧はObserver設定（multi-room / production）から導出。未取込Roomは `ROOM_NOT_INGESTED`。`events` 由来Roomは `ROOM_DISCOVERED_NOT_OBSERVED`（`/r/<room>` 言及のheuristic）で、取得先は追加しない |
| §5.1 入力経路 | 部分 | Archive（Default）と明示宣言された `state.sqlite` snapshot。manifest読取・稼働SQLiteはopt-in。manifestはreceipt chainを再計算検証し、崩れたものは取り込まない。`/export` raw JSONL、Spool DBは未対応 |
| §5.2 外枠 | 実装 | Source/provenance/format版、locator、room/epoch/seq、原text、ts/取得時刻/取込run、digestとhash対象、品質flag。seq・hash等の外枠が壊れた単位はshard単位で隔離（Archiveはshard全体でhash検証するため、同一shard内の他recordも保留される）。内容fieldの欠落はquality flag |
| §5.3 原文 | 実装 | 原textで署名検証。類似検出は別の正規化値。Archive行hash / 正規化JSON hash / Bundle hashを区別。受信raw復元は主張しない |
| §6 増分・時系列 | 部分 | shard digestで進捗管理（最大seqを使わない）、新segment・LATE_OBSERVATION・署名再検証・record競合を検出。Room間順序は作らない。**GAP解消の再解析はFinding revisionで表すが、旧結果との対応は run/revision 単位まで** |
| §7.1 Radar | 部分 | Room別件数・署名内訳・初観測DID・期間・Protocol候補・前窓比。**静かなRoomとCapture停止の区別は未実装**（Archiveだけでは判別不能と明示） |
| §7.2 Opportunity | 部分 | tclk offer（構造化）と自由文keyword候補。報酬記載≠保証、ACCEPT未観測≠募集中を明記。関連性はHuman設定keywordのみ。必要能力の抽出はSemantic任意 |
| §7.3 Protocol Tracker | 実装 | tclk/1のみ（下記）。未知versionは `UNSUPPORTED_VERSION` |
| §7.4 Actor | 部分 | DID別履歴、未署名nicknameは別名、帰属しない主張を分離、スコアなし。**期間・契約等の検索UIは未実装**（JSON出力のみ） |
| §7.5 Change/Anomaly | 部分 | 頻度急増（Baseline不足時は判定しない）、同文集中、複数Room反復。ruleは識別子・版・条件・範囲・必要Evidence・正常理由を持つ。**類似文（非完全一致）、主張の変化検出は未実装** |
| §7.6 Coverage Advisor | 実装 | Source未接続、GAP（後着回収状況付き）、境界、segment不連続、未取込Room、deal room未観測、前提record不足、署名検証器なし、Baseline不足、公式鍵未確認 |
| §8 Identity | 実装 | VALID署名のみDIDへ帰属。不正署名に書かれたDIDへ帰属しない。Sybil断定・実名推定なし |
| §9.1 Profile | 実装 | tclk / close-call を版付きmoduleとして実装（汎用Plugin基盤なし） |
| §9.2 tclk | 実装 | 公式reference（commit `5cc4ab9`）のgolden vectorでoffer id・contract id・canonical lineが一致。状態enumは公式6値のみ、record判定・Coverage・期限は別field。署名は `room|nonce|text`、期限は各recordの `ts`（欠落時にnowへ置換しない）。**KV state note（`kv/tclk-*`）は読まない**、rail照会なし。**追記**：`type` が文字列でないframeは通常のframe拒否。前提transitionの「観測済み」判定は、そのframeが前提statusから `apply_frame` の全guard（party・contract・期限・rail・secret、ref付きreveal/refundは適用可能なlockのref）を満たす場合だけ数える（副作用なしの仮想評価）。lock等の必須の途中frameが観測されていない後続frame（lock無しのreveal/refund等）は `INSUFFICIENT_HISTORY` + Coverage `TCLK_TRANSITION_NOT_OBSERVED` とし、最後に確認できた状態を維持して違反とは断定しない。必須frameが後のseqで観測されている順序違反や、前提が揃った上のwrong party/secret/roomは従来どおり `PROTOCOL_INVALID`。処理順は同一room・同一server generation内ではserverの `seq` 順（stream＝Observer segment/epochやsnapshot epochはその局所区分にすぎず、重複recordのどのSourceのcopyを代表にしたかで順序が変わらない）。generationが異なるrecordは別blockのまま、blockの間は従来どおりstream順。`--as-of` 再生ではas-of以降のrecordを状態復元に使わない（cacheは保持、live runは全件対象） |
| §9.3 Kibble | 部分 | 汎用解析のみ。専用Profileなし |
| §9.4 Close Call | 部分 | 設定駆動（Room→post種別表・時刻・sweep間隔・公式鍵）。公式room表（`close-call-game.md` blob `4e3ed2e`）どおり price/seed/final は `d-close1-price`、flow/positions/pnl/state は同名room。wrong room・最小shape不一致の投稿はsweep観測に数えない。**公式鍵に一致した投稿だけ**を観測済みsweepとし、鍵未確認時はsweep監視を `INCONCLUSIVE_OFFICIAL_KEY_UNCONFIRMED` とCoverageに出す。sweep欠落監視は発行予定期間のみ、取引締切で機会提示終了、終了＋24hの起点記録、同一sweepの公式post矛盾。**公式相当の再計算は未実装（`RECALCULATION_NOT_SUPPORTED`）**、player tradeの両署名検証は未実装、公式鍵はlaunch recordがrepoに無いため未確認 |
| §10 分類 | 部分 | 8分類をenum化。決定論的に生成するのは PROTOCOL_INVALID / SECURITY_FINDING_CANDIDATE / ABUSE_CANDIDATE / EVIDENCE_CONFLICT / UNVERIFIED_CLAIM。**PROTOCOL_FRAUD_CANDIDATE / MARKET_AGENT_ANOMALY / QUALITY_REPUTATION_CONCERN の生成器は未実装**（公式Fraud規則を特定できていない。LLM結果はFindingへ昇格させていない） |
| §11 Finding | 実装 | 安定ID（同じruleと位置のEvidenceが複数ある場合は上書きせず1つのFindingへ全refをmerge：同一position・別contentのsecurity record、同一keyの複数record conflict、同一unitの複数回変化）、record conflictの報告案はexisting / observedの両sideをdigestで対応づけて表示（内容を保持していないsideはdigestのみ・`available: false`）、record以外の整合性Evidence（unit digest履歴・unit quarantine・Observer CONFLICT receipt）は報告案internal packetに種類別entry（digest変化の全履歴と順序／理由／receiptのroom・seq・detail、record Evidenceではないこと・unit bytesを保持しないことを明示）として載せ、public側は不透明ID・digest・理由code・room/seqのみ（source ID・unit名・receipt ref・detailは出さない）、source/unit/receiptのidentityはFindingの構造化field（details）から取り、refの文字列を区切り文字で再分解しない（source IDに`:`や`|`を含んでもよい）。報告案の`verification_steps` / `machine_verified_part`はEvidence種別（record / record conflict / unit digest履歴 / unit quarantine / Observer receipt）ごとに出し分け、非recordのEvidenceにrecord再読込・署名再検証の手順を出さない、append-only revision、Severity(影響)と確度の分離、Evidence facet、処理/検証/Review/報告/解決の別軸。Review・報告・解決はHuman CLIのみ。Review・報告event・Resolutionは `--revision`（current revisionのみ受付）に束縛し、revisionが進むと `STALE_RE_REVIEW_REQUIRED` / `PREVIOUS_REVISION_ONLY` / `REASSESSMENT_REQUIRED` と表示して旧判断を引き継がない。報告案のdigestは、run全体がSUCCEEDED/PARTIALでcommitされた時に同一transactionで記録し（失敗runは行を残さない）、報告eventはcommit済みrunのcandidate fileが記録digestと一致する場合だけ受け付ける。current revisionにcommit済みcandidateが無いFindingは次のrunで再生成する。DB commit後にrunが失敗した場合（例：`latest.json` 書込失敗）はrunをFAILEDにし、そのrunのcandidate行とoutput dirも取り除く。failed runで初めて記録されたFinding revisionは、次のSUCCEEDED/PARTIAL runで一度だけ `RECOVERED_FROM_FAILED_RUN` としてreview queue / 通知eventに出る（以降は通常の `UNCHANGED`）。`NOT_SUBMITTED` 以外の報告eventは、そのrevision向けに生成・記録したcandidate digestが必須。**追記**：`--as-of` replayはFinding lifecycleを一切読み書きしない（run-localな `REPLAY_DERIVED` 出力のみ、報告案・通知なし、failed run分のsurfacingも消費しない）。replayのEvidence integrity Findingは検出時刻 ≤ as-of のrecord conflict / shard変更のみで、時点不明のquarantine・Observer CONFLICTは除外して `REPLAY_INTEGRITY_UNPLACED` を出す。空・空白のみの `--reviewer` / `--actor` はCLIとStoreの両方で書込前に拒否。**追記**：run同士は `<state_db>.lock`（非blocking flock）で排他し、重なったrunは何も変更せず `ANALYZER_RUN_IN_PROGRESS` で拒否（interrupted回復はlock取得後のみ、real runtimeの予算確認も直列化）。replayは時刻を置けない保存済みCoverage itemを除外して `REPLAY_COVERAGE_UNPLACED` を出す。lockはstate DBが新規作成直後（WAL未checkpoint）の重なりでは `ANALYZER_STORE_FORMAT_MISMATCH` の拒否になり得る（拒否のみで破壊はしない） |
| §12 報告案 | 部分 | Evidence Packet（internal）と公開用派生（secret-shape除去・URL defang・locator除去・制約記載）、窓口候補（abuse=公開Issue / 脆弱性=非公開 / 不明=Human確認）。**LLMによる文案整形は未実装**、英語版はJSON fieldが英語であるのみ |
| §13 成果物 | 部分 | 4主要成果物 + Actor/Coverage view、共通envelope、日本語md、`latest` はcommit後のみ。**絞り込みUIは未実装** |
| §14 LLM | 部分 | 有限Bundle（目的・Evidence・context・schema・ID・上限・digest・加工記録）、keyword外の決定的sample、未解析量の記録。現runで選んだBundle（=task id）の結果だけを現runの出力へ付け、interests等が変われば旧結果は付けない（履歴には保持）。**過去LLM要約のContext利用は未実装** |
| §15 課金 | 部分 | Default `none`、自動fallbackなし、無限再試行なし。Real実行には (1) Human確認記録、(2) 公式の認証優先順位でsubscription loginより上位のcredential指標（`ANTHROPIC_API_KEY`・`ANTHROPIC_AUTH_TOKEN`・Bedrock/Vertex/Foundry・`ANTHROPIC_PROFILE`・federation変数・`CLAUDE_CONFIG_DIR` 等）が無いこと、(3) 毎回の直前に同じenvで `claude auth status --json` がexit 0かつHumanが固定した期待値と一致すること、が全て必要。`run()` 直接呼出しでも同じ検査を通る。**`auth status` のfield意味は公式に文書化されておらず、Extra usage・auto-reload・active profile fileはAnalyzerから確認不能（Human確認事項）** |
| §16 Runtime境界 | 部分・未検証 | 公式CLI reference（2026-09-29確認）に基づき `--safe-mode`（CLAUDE.md・skills・plugins・hooks・MCP・auto memory無効、認証は通常）、`--restricted`（user/project settings不読込、コマンド実行系Tool・WebFetch除去）、`--tools ""`、`--disallowedTools mcp__*`（`--tools` はMCPに効かない）、空MCP + `--strict-mcp-config`、`--permission-mode dontAsk` + `--permission-prompts none`、`--disable-slash-commands`、`--no-session-persistence`、空cwd、最小env、timeout、process group kill。**managed（policy）settings由来のhooks等は残る**。`--bare` はsubscription loginを読まないため不使用。flagの組合せは実CLIで未検証。確認記録（2026-09-29）：CLI 2.1.284 の `claude --help` に `--safe-mode` / `--restricted` / `--permission-prompts` が存在し、公式CLI reference（code.claude.com/docs/en/cli-reference）に `--restricted`（v2.1.248以降）と `--permission-prompts`（v2.1.259以降）の記載あり。全flagの組合せがAPIを呼ばずに引数解析を通る（`--permission-mode` 等に不正値を与えるとchoices違反で止まる）ことまでを確認しただけで、実行・認証は未確認。これらのflagは古いCLIでは存在しないため、実CLIのversionが2.1.259未満なら起動前にエラーになる（fail closed） |
| §17 入力安全 | 部分 | 観測textはデータ扱い、`allowed_rooms` 以外はBundle化しない、secret-shape除去、LLM出力のsecret-shape検出で隔離。検出器の完全性は保証しない。**実LLMでのPrompt injection試験は未実施** |
| §18 保存 | 部分 | 派生cache（`rebuild`で再構築）と保全履歴（revision・Review・報告・解決・LLM結果）を分離、再解析は新run。locatorは媒体非依存。`state_db` / `output_dir` はSource・Observer rootと重なるとconfig読込時に拒否。既存DBはSQLiteでopenする前にheaderでAnalyzer DBか確認（他DBのjournal modeを変えない）。state/outputは0600 / 0700で作成する。既存pathは「Analyzerのものと確認できてから」だけ矯正する：output dirは自前のmarker（`.technocore-analyzer-output`）がある／空／過去版と同じ配置（`latest.json` + `runs/<run id>`）の場合のみ採用してmarkerを書き、それ以外は何も変更せず拒否。state dirはAnalyzer DBのheader確認に通り他entryが無い、または空の場合のみchmod。他uid所有・symlink・複数hardlinkのfileは拒否（inode共有先のSourceへchmodが波及しない）。Source pathのmodeは変更しない。**cache容量管理・履歴の移管手順は未実装** |
| §19 継続運転 | 部分 | 有限run、`nice`、新規record上限、Source/Profile/LLM失敗の分離、未解析を「異常なし」にしない、変化のみ通知event。**CPU/メモリ/I/Oの厳密な上限、LLM待ち件数上限は未実装** |

## §20 成功条件（Offline）

| 確認対象 | Offline | 実環境 |
|---|---|---|
| 実データ連携 | Observer実コードで生成したArchive / state schemaを読み、locatorで戻れる | 未検証 |
| 原記録保全 | run前後でArchiveのbytes・mtime・modeが不変 | 未検証 |
| 増分処理 | 再読込で重複しない、新segment・後着seqを反映、中断runはFAILED | 未検証 |
| 世代・順序 | segment/epoch別stream、Room間seq比較なし | 実世代境界は未検証 |
| 署名 | VALID / INVALID / MATERIAL_ABSENT / UNSIGNED / MALFORMED / UNSUPPORTED / VERIFIER_UNAVAILABLE | 実署名recordで未検証 |
| tclk | 公式golden vector一致、正常遷移、拒否、履歴不足、replay、wrong room、from偽装 | 実transcriptで未検証 |
| 不正分類 | 拒否frame=PROTOCOL_INVALID(INFO)、履歴不足=Coverage、mb-未署名=SECURITY候補 | — |
| Opportunity / Actor | 根拠・不足・未確証を明記、スコアなし | — |
| Finding / Report | 重複抑制、revision、Review・報告・解決の別履歴 | 外部報告は未実施 |
| 表示・送信 | HTML/URL/制御文字の無害化、公開前加工の記録 | — |
| LLM | fixtureで正常・schema不一致・架空参照・timeout・枠不足・結果不明を識別。fake CLIでtimeout kill | **実LLM未実施** |
| 課金 | preflightがAPI key環境・確認記録欠落で拒否 | **実経路未確認** |
| LLMなし | 決定論的結果とFindingが出力され、意味解析未実施が表示される | — |
| Event終了 | 取引締切後の機会停止、sweep監視の期間限定、終了後も履歴解析可 | 実Eventで未検証 |
| 外部移管 | locatorは相対、`path_map`、未接続は `UNAVAILABLE` | 実媒体で未検証 |
| 負荷・障害 | Source/Profile/LLM単位の障害分離 | **共有資源下で未検証** |

## 実環境で残るHuman Gate

- 実Observer保存物への接続方法（Archive pathの読取権限、manifest読取の可否、lobby `state.sqlite` のsnapshot取得手順）
- Claude Code実接続：本人アカウントの認証経路、Extra usage / auto-reload無効、apiKeyHelper等の有無、実効設定（hooks・memory・managed policy）、自動起動が公式利用条件に適合するか
- Close Call公式鍵・Event設定の確定（Observer承認済み設定との一致）
- Productionでの定期起動、同居時の資源予算、保管先
- 報告案の外部送信、Notifier実配送
