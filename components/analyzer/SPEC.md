# Technocore Analyzer｜統合仕様書 v1.1

作成・見直し日：2026-09-29  
状態：要求仕様案。資料照合・修正後点検済み。実装・本番適合・実LLM接続の確認ではない。  
想定Canonical path：`components/analyzer/SPEC.md`  
対象：FLOP / Technocore Agent Projectの保存Evidence分析・活動監視・不正／異常候補検知・報告支援

## 1. 目的と位置付け

Analyzerは、Observerが保存したEvidenceを、状況把握、仕事・協業候補、Protocol状態、Actor活動履歴、不正・異常候補、報告案へ変換する独立Componentとする。

目的は「記録を貯める」から「記録を判断と行動に役立てる」へ進めることであり、LLMを動かすことや監視Frameworkを作ること自体ではない。

本仕様はMVPだけの仕様ではなく、継続利用するAnalyzerの機能と境界を定める。段階的な実装は許容するが、未実装・未接続・未検証を全体完成と扱わない。

ObserverのCurrent要求仕様は、取得・保存・欠損記録・運用監視・限定復旧・外部移管を担当し、意味解析を担当しない。Analyzerの完成をObserver完成の条件にしない。[P1]

## 2. 責務と権限の分離

```text
Observer → 保存Evidence → Analyzer → 状況・候補・Finding・報告案
                              ↕
                       必要な場合だけLLM

分析結果 → Humanまたは別Agentの判断 → 別の実行権限 → 外部作用
```

| 要素 | 担当 |
|---|---|
| Observer | 取得、原記録、取得範囲、GAP、Archive、取得系の運用 |
| Analyzer | Evidence選択、検証、集計、状態復元、意味解析、履歴、報告案 |
| Scout | 既存の軽量候補抽出。再利用可能だが必須依存にしない |
| LLM Runtime／Worker | 許可された範囲の推論実行、認証・利用量・実行結果の管理 |
| Collaboration Agent等 | 現在状態と権限を別途確認したProtocol操作 |
| Policy Signer | 許可された署名要求へのPolicy適用・署名 |
| Human | 目的、優先順位、権限、支出、重要な判断、外部報告の承認 |

Analyzer Coreは原記録を読み、自分の解析結果を保存する。観測先への書込み、署名、受注、取引、公開通報は行わない。LLMへの資料送信も外部通信であり、承認済みRuntimeの境界で扱う。

## 3. 情報の区分

次の区分を維持する。

| 区分 | 意味 |
|---|---|
| Observed | 保存Evidenceに何が記録されているか |
| Verified | 指定した検証に合格したこと。検証対象と方法を必記 |
| Derived | コード・ルール・統計から計算した状態や指標 |
| Inferred | LLM等による意味解釈・仮説 |
| Proposed | 次に検討する行動や報告案 |

「投稿者が納品したと書いた」と「納品を独立確認した」は別である。署名検証、保存hash一致、公式所属、仕事の品質、決済完了を一つの`verified`にまとめない。ローカルに作成した署名付きrecordもあり得るため、署名検証だけをTechnocore掲載の証明にしない。

評価対象は観測済みのSourceと期間に限定する。全Technocore、全FLOP活動、Actorの全履歴を把握したとは表現しない。

## 4. 観測データの利用範囲

対象はObserverのCurrent設定・保存Evidenceから取り込み、Analyzer側で別の観測先一覧を正本化しない。

現在のObserver仕様にある`lobby`、`tclk-offers`、`kibble`、`events`、`sub_economy`、`zk-desk-a`、Close Call関連6 Roomを、用途に応じて扱える構造とする。これは要求対象であり、12 Roomの本番稼働を確認済みという意味ではない。[P1]

全対象に共通の軽量解析を適用し、対応Protocolには専用解析、必要な自然文にはLLMを適用する。Protocol未対応のRoomも、存在・活動・原文参照まで解析対象から落とさない。

`events`等から新しいRoomが見つかっても、Analyzerは取得先を自動追加しない。追加観測が必要な理由をCoverage Advisoryとして出す。

## 5. 入力AdapterとEvidence契約

### 5.1 入力経路

Read-only Evidence Adapterを介して、書込み完了済みArchive、確定済みrecord群、または整合したsnapshotを読む。新しい中間サーバーや全量コピー基盤の導入は必須にしない。

Adapterは実際のObserver保存形式を少なくとも一つ扱い、対応形式・版・制約を明記する。独自の合成Bundleしか読めない実装を「Observer連携済み」としない。

稼働SQLiteへの直接接続はDefaultにしない。必要な場合だけ、読取権限、短いtransaction、WALへの影響、timeout、共有I/Oを確認して採用する。ライブDBの単純コピーや、変化するDBを不変と偽る指定で整合性を代用しない。[S8][S9]

### 5.2 必須の外枠

各入力には、少なくとも次を関連付ける。

| 項目 | 内容 |
|---|---|
| Source | 取得元・保存stream・入力形式の版。実取得・持込み・合成fixtureの区分 |
| Record参照 | 一意なEvidence参照と、原記録へ戻れるlocator |
| 識別 | Room、ローカルepoch、seq等。欠ける場合は理由 |
| 内容 | 原textと利用する元field。値を無断で補わない |
| 時間 | Sourceの時刻、取得時刻、取込時刻を区別 |
| 整合性 | digestと対象bytes／形式、または未検証理由 |
| 品質 | GAP、競合、rawの有無、署名材料の有無、省略の有無 |

署名・nonce・generation等は、Sourceに存在するとき保持する。必須の外枠まで欠けた入力は隔離して理由を残すが、一件の不良recordだけで他の正常入力を止めない。

### 5.3 原文と処理用表現

署名対象のtext・整数・文字列を保持し、Unicode正規化や丸めを行わない。検証にはProfileが定める正確な値と符号化を使用する。

検索・類似文検出用の正規化は、別の派生fieldに限る。派生textを署名検証や原文引用の代わりにしない。

受信rawのhash、正規化recordのhash、解析Bundleのhashを区別する。rawがなくても利用可能な解析は続けるが、受信bytesを復元できるとは主張しない。

## 6. 増分処理・時系列・重複

AnalyzerはObserverと独立した処理進捗を持つ。結果の保存と解析進捗を整合させ、処理途中の停止で未保存結果を完了扱いにしない。

**最大seqだけを解析済み位置にしない。** 後から回収された小さいseq、GAP解消情報、訂正、署名再検証結果も新しい解析入力として検出する。影響する集計・履歴・Findingを更新し、旧結果との対応を残す。

同じ保存recordの再読込と、同文の別投稿を区別する。異なるstreamやepochをまたぐ同一性が不明な場合は無理に重複排除しない。同一識別子で内容が異なる場合は、片方を上書きせず競合として保持する。

順序はSource契約に従う。Room間のseqを比較せず、timestampだけから確実な因果関係を作らない。時刻欠落・時計差・世代境界が判断へ与える制約を表示する。

過去記録の再生で用いる基準時刻と、現在の締切通知で用いる現在時刻を分離する。

## 7. 恒久機能

### 7.1 Observed Surface Radar

Room別の投稿量、検証済み署名投稿、未署名投稿、新たに観測されたDID、活動期間、話題・Protocolの候補、増減を集計する。

表示には対象期間・観測範囲・更新時刻・欠損を付ける。静かなRoomと、Observer停止・未取込・外部媒体未接続を混同しない。

### 7.2 Opportunity Radar

仕事、Review、協業、Contribution、Help Request等を根拠付きで提示する。

依頼内容、発信者の観測上の識別、必要能力、期限、報酬・費用の記載、現状、不足情報、Projectとの関連、次の確認候補を示す。

関連性はHumanが設定した能力・関心・除外条件に照らす。報酬の記載を支払保証にせず、「ACCEPTを観測していない」を「確実に募集中」にしない。

### 7.3 Protocol Tracker

対応Profileの仕様に基づいて構文、署名、ID、当事者、Room、期限、状態遷移を検証する。

未知Protocol・未知Versionは未対応として保存し、既知Versionへ推測変換しない。Protocolの状態はLLMに決定させない。

### 7.4 Actor Activity History／Profile View

活動、Offer、合意、観測された終端状態、Finding、報告履歴を時系列で閲覧できるようにする。

長期保持するが、Actorの総合信用点・詐欺確率・人格評価は作らない。件数、期間、観測範囲、未確認件数を併記する。

### 7.5 Change／Anomaly Detection

頻度急増、同文・類似文の集中、複数Roomへの反復、状態不整合、主張の変化等を検出する。

ルールには識別子、版、条件、対象範囲、必要なEvidence、想定される正常理由を持たせる。履歴不足時はBaseline不足とし、固定された根拠のない倍率で不正確定しない。

定型通知、正当な再送、イベント開始、同じ公開テンプレートの使用等を誤検知候補として検討する。

### 7.6 Coverage Advisor

未観測deal room、GAP、前提record不足、未確認の公式鍵、オフライン媒体等について、「何が不足し、どの判断ができないか」を示す。

不足は補完依頼候補として出し、取得、権限追加、Observer再設定を自動実行しない。

## 8. Identityと帰属

有効な署名に対応するDIDを、鍵に基づく識別子として利用する。ただし同じ鍵が一人の人間・一つのAgent・一つの運営者だけに使われるとは仮定しない。

未署名nicknameは同名投稿を探す表示上の別名であり、同じRoom内でも同一Actorとは断定しない。署名不正な投稿に記載されたDIDを、実際の加害者としてそのDIDの履歴へ帰属させない。[S1][S2]

複数DIDの文面・時刻が似ていても同一運営者やSybilと確定しない。初観測日時をDID作成日時や初参加日時と呼ばない。

実名推定、外部SNSとの個人紐付け、非公開情報による補強は対象外とする。

## 9. Protocol／Event Profile

### 9.1 Profileの基本

Profileは対象、Source仕様の版、必要field、検証規則、状態復元、出力、既知の制約を持つ。単純なmoduleや設定でよく、汎用Plugin基盤を必須にしない。

仕様変更時に過去結果を無断で読み替えない。適用Version不明なら不明を残し、別Versionによる試験的再解析は別runとして識別する。

### 9.2 tclk

公式tclk/1の契約状態は、`proposed / accepted / locked / claimed / refunded / cancelled`として扱う。解析側の`UNKNOWN / INCOMPLETE / INVALID`を、このProtocol状態の列挙へ追加しない。[S3]

代わりに、復元できた契約状態、record検証結果、観測Coverage、期限経過状態を別fieldにする。

`offer / accept`と、`lock`以降の派生deal roomの結び付きを確認する。前提recordやdeal roomが不足する場合、孤立した後続frameを直ちに違反扱いせず、必要な履歴不足を示す。[S2]

十分な前提がある場合にwrong party、wrong room、ID不一致、禁止遷移、replay等を判定する。無効frameで最後の有効状態を破壊しない。`receipt`と`heartbeat`を決済完了や納品の独立証拠へ格上げしない。[S3][S4]

署名は`room|nonce|text`を対象とし、`seq / ts`は投稿者の署名対象ではない。履歴再生の期限判定は対応recordの時刻を使い、欠落時刻を現在時刻へ置き換えない。[S2]

チャット上の`claimed`と実資産の決済、成果物の納品・品質を区別する。現在の公式PaperRailに実価値の裏付けがない点を表示し、チャットだけから盗難・決済・未払いを確定しない。[S4]

### 9.3 Kibbleその他

`kibble`は汎用の活動・自然文解析対象に含める。今回確認した資料だけではKibble固有LifecycleをFLOP公式Protocolとして確定しない。

Community実装を参照して専用Profileを作る場合、採用Source・版・意味・制限を明示し、Community conventionとして扱う。自己attestation等を直ちに公式規則違反としない。

### 9.4 Close Callなどの期間限定Event

Close Callは独立Event Profileとし、Room名、instrument、sweep間隔、数量・価格単位、計算規則、公式鍵、終了条件をCoreへ埋め込まない。

対応時は複数Roomの状態対応、署名と公式鍵との結び付き、必要snapshotの有無、公開状態の整合を検証する。公式相当の再計算には、当該Versionが必要とする初期状態・入力・時刻・丸め規則等が揃っていることを前提とする。揃わなければ「再計算不能／部分比較」であり、不一致確定ではない。

終了時刻はObserverの承認済みEvent設定に合わせる。取引締切、最終価格、結果確定、異議・請求期間を区別し、「終了＋24時間」の起点を記録する。公式`contest.json`にも取引lockと最終価格時刻は別々に存在する。[P1][S7]

**取引機会の提案は取引締切で終了する。** 観測がその後も続くことを、取引可能期間の延長と扱わない。定例sweep欠落の監視はProfileの発行予定期間に限り、観測終了後も欠落警告を出し続けない。

最終集計、後着Evidence、既存Findingの訂正、報告経過、手動の履歴解析はEvent終了後も可能とする。AnalyzerがObserverを停止・再開・延長しない。

Event終了後にClose Call対応が不要になった場合、その専用機能を恒久Core完成の必須条件にしない。対応済み／未対応を明示して残す。

## 10. 不正・異常検知の分類

不正検知は「疑わしいものを見つける」「記録を検証する」「規則との不一致を判定する」「報告案を作る」に分ける。

| 分類 | 扱うもの |
|---|---|
| PROTOCOL_INVALID | 十分な前提の下で、対応仕様に適合しないrecordや遷移 |
| SECURITY_FINDING_CANDIDATE | 本来拒否される入力が受理される等、実装・権限境界の破れを示す候補 |
| PROTOCOL_FRAUD_CANDIDATE | 対応する公式Fraud規則と必要Evidenceを具体的に示せる候補 |
| ABUSE_CANDIDATE | spam、flood等の濫用候補 |
| MARKET_AGENT_ANOMALY | 行動集中、wash-likeな動き等の異常候補 |
| QUALITY_REPUTATION_CONCERN | 成果物・説明・履行に関する品質上の懸念 |
| EVIDENCE_CONFLICT | 記録・計算・主張間の不一致 |
| UNVERIFIED_CLAIM | 必要な裏付けがない主張 |

**不正なframeが投稿されたことだけでは、脆弱性を証明しない。** 正常な検証器が拒否していれば、防御が機能している可能性がある。tclkのSecurity方針も、不正な入力を受理する等の実装上の問題を対象にしている。[S4]

**前提の欠落だけでもFraudとしない。** 未観測、未対応、形式差、Parser不具合、移管中、GAPを切り分ける。署名不一致も、保存・解析の変換ミスを除かずに投稿者の偽造と断定しない。

Technocoreが明記するnicknameの自己申告、untrusted content、retentionによる消失等を、それ自体で新規脆弱性として報告しない。一方、仕様上可能な行為でも実際のspam等はAbuseとして別評価できる。[S1]

FLOP Yellow Paperの市場監視は参考背景だが、チャットだけでオンチェーン不正を証明できるとはしない。Protocol challengeの当事者資格もAnalyzerには付与されない。品質問題をProtocol Fraudと混同しない。[S5][S6]

## 11. Findingと状態管理

Findingには、ID、category、対象、主張、期待挙動、観測挙動、根拠参照、rule／Profile版、検出方法、Coverage、代替説明を持たせる。

Severityは影響の大きさとして`INFO / LOW / MEDIUM / HIGH / CRITICAL`を用い、確度とは分離する。影響不明の場合は`UNKNOWN`とする。高頻度・奇妙な文体・未署名だけで重大度を上げない。

Evidenceは単一の「強さ」へ圧縮せず、直接観測、独立した補強の有無、再現可能性、署名検証、Coverage、未検証点をそれぞれ記録する。複数DIDの一致を独立した補強と決め付けない。

次の状態を別管理する。

| 状態の軸 | 例 |
|---|---|
| 処理 | SUCCEEDED / PARTIAL / FAILED / DEFERRED |
| 検証 | UNCHECKED / SUPPORTED / CONTRADICTED / INCONCLUSIVE |
| 人間のReview | UNREVIEWED / CONFIRMED / REJECTED |
| 報告 | NOT_SUBMITTED / SUBMITTED / DELIVERY_UNKNOWN / ACKNOWLEDGED |
| 解決 | OPEN / FIXED / FALSE_POSITIVE / DUPLICATE / NOT_A_BUG / UNKNOWN |

報告したこと、受領されたこと、不正が確認されたことを区別する。人間のReviewはHuman由来の操作でのみ記録し、公式回答を反映するときは回答Sourceを添える。

同じ事象は安定したFinding IDへまとめ、根拠追加・悪化・訂正をrevisionとして残す。同じFindingから報告案を大量生成しない。

## 12. 報告案と外部報告

Analyzerは、Evidence PacketとReport Candidateまで自動生成できる。機械的な検証結果とLLMが整えた文章を区別する。

報告案には、対象・影響、期待／観測挙動、時系列、仕様の根拠、必要最小限の証拠、検証手順、未確認点、正常な代替説明、重複・既知仕様の確認、推奨窓口、公開注意事項を含める。

TechnocoreではAbuseと脆弱性の窓口が分けられている。公開してよいAbuseは通常Issue、公開不適切な内容や悪用可能な脆弱性は非公開窓口を候補とする。tclkの悪用可能な脆弱性も非公開窓口を候補とする。[S1][S4]

不明な商取引紛争、Event独自問題、Protocol Fraudの提出先は推測せずHuman確認へ戻す。報告時に窓口と対象Versionを再確認する。

公開用Packetは原記録の複製ではなく、秘密、不要な個人・環境情報、危険な再利用可能URL等を除いた派生物とする。内部参照との対応は保つ。伏せた箇所が検証へ与える制約も記す。

送信、Public Issue作成、Advisory提出、メール送信は本仕様の自動処理に含めない。承認する場合は宛先・公開範囲・本文・添付の版を結び付け、修正後の報告案へ過去承認を流用しない。送信結果不明を成功とせず、無断で再送しない。

実害を与える再現、Liveへの攻撃的試験、任意URLへのアクセスは、報告案生成のために自動実施しない。

## 13. 成果物と利用画面

主要成果物は、Situation Report、Opportunity、Finding、Report Candidateの4種類とする。Actor ProfileとCoverage Advisoryは、これらに接続する検索可能な派生Viewとして提供する。

Canonical出力はVersion付きJSON等の機械可読形式とし、同じ内容から日本語中心のHuman向け表示を生成する。外部報告の英語版も同じ根拠から作成できる。

共通の出力契約には、結果ID・種類・schema版、生成日時、データの基準時点、対象範囲、入力digest／Evidence参照、適用Profile・rule・model情報、Coverage、処理状態、既存結果との改訂関係を含める。観測、計算、推論、提案を区分し、人間のReview・提出状態はLLMが書き換えられない管理情報として扱う。

Humanは、期間・Room・DID・契約・Event・categoryで絞り込み、原記録、Coverage、未Review一覧、変更履歴へ辿れるようにする。CLI＋静的Reportで満たせるなら常設Webサーバーを必須にしない。

主要な結論にはEvidence参照と生成方法を付ける。参照が実在するだけで、結論の妥当性まで検証済みとしない。根拠のない重要主張は確定結果や公開報告本文へ混ぜない。

保存できた結果だけを完了表示し、途中失敗を成功Reportにしない。LLMを使わない結果も明示的な解析modeを持つ。

外部文字列はHTML・端末・Markdownで安全に表示し、含まれるURLを自動取得・展開しない。これはTechnocoreのGET-writeを誤実行しないためにも必要である。[S1]

## 14. LLMの使い方

解析は、決定論的処理、ルール／統計、必要な意味解析のHybridとする。件数・署名・hash・構文・数値・Protocol状態・Coverageはコードで扱い、自然言語の分類・関連性・説明・報告文案へLLMを使う。

LLMを使う機能は本仕様に含めるが、全件への適用は要求しない。LLM接続未完了を機能全体の完成とせず、決定論的機能だけの稼働状況と区別する。

意味解析依頼は、目的、選択済みEvidence、信頼済みの必要Context、出力schema、参照ID、上限を含む有限Bundleとする。モデルへ任意のDB探索や自発的なSource追加を委ねない。

候補選別、まとめ処理、同一内容の再利用によって利用量を抑える。ただしキーワードに合った投稿だけで意味解析を閉じない。設定した有限サンプルや定期Digestで、新しい表現による見落としを点検する。間引き・未解析範囲を残す。

過去のLLM要約は派生Contextとして参照できるが、重要な新判断は原Evidenceへ戻れることを要求する。LLM出力は自己承認、権限追加、ruleの本番自動変更に使用しない。

## 15. サブスク優先と課金境界

### 15.1 優先方針

Human本人のサブスク利用を優先し、Claude Codeの公式利用経路を第一候補とする。追加支出の初期許可はゼロとし、有料API・追加クレジット・別Providerへ自動で切り替えない。

Claude CodeのPro／Max利用と、非対話・構造化出力の公式機能は確認できる。ただし、個人の公式CLI利用、独自サービスのBackend利用、第三者向け認証提供を同一の許可として扱わない。[S10][S11][S12]

本仕様の自動接続は、対象の利用形態・認証方法が公式条件に適合すると確認できるものに限る。OAuth credentialを取り出して独自HTTP clientへ流用しない。利用条件が不明な経路は接続未確認として保留し、No-LLM処理とAnalysis Bundleの受渡しは維持する。

### 15.2 サブスク内運用の確認

`ANTHROPIC_API_KEY`の有無だけで判定しない。API key、bearer token、credential helper、別Provider、独自gateway、実際に選択された認証・課金先を確認する。秘密値そのものをログやEvidenceへ記録しない。[S13]

追加クレジット／Extra usageと自動チャージの設定も確認する。公式説明では追加クレジットはサブスク料金と別課金であり、Claude Codeにも適用される。追加支出ゼロの運用では、これらを使用しない条件をHumanが確認する。[S14]

確認できない課金経路ではサブスク専用modeの実行を開始しない。確認を回避するために秘密ファイルを収集したり、非公式APIで残量を調べたりしない。

### 15.3 枠不足・利用量

日常の開発・Reviewと利用枠を共有するため、解析頻度、同時実行数、入力・出力上限、日次実行上限、優先度を設定できるようにする。[S10]

枠不足時は意味解析を延期・省略として記録し、決定論的解析を継続する。重要FindingをLLM待ちで非表示にしない。重複依頼をまとめ、期限切れの機会に対する即時判断用タスクは再実行しない。履歴解析・最終集計・報告訂正のタスクは、それらと区別して保持する。

認証失敗・枠不足・結果不明に対する無限再試行は行わない。CLI起動数、モデルturn数、Providerへのrequest数を混同せず、観測できない利用量をゼロと記録しない。Analyzer側の再起動制限をCLI内部の通信回数保証と呼ばない。

## 16. Semantic Runtimeの実行境界

分析用Runtimeと、コードを実装するCoding Agentの権限は別物とする。分析RuntimeへShell、任意ファイル探索、WebFetch、MCP、Connector、署名・送信ToolをDefaultで与えない。

指定Bundleを入力し、構造化結果を受け取ることを中心とする。必要な認証は公式CLI／許可されたRuntimeだけが扱い、Analyzer Core・モデルContext・Reportへ渡さない。

CLIのtool設定だけで隔離完了と判断しない。hooks、skills、plugins、MCP、user／project設定、auto memory、管理Policy由来の動作を含め、実効設定を確認する。公式CLIの`--allowedTools`は自動承認設定であり、利用可能Toolの完全制限ではない。[S15]

公式の`--bare`はカスタマイズ探索を省くが、現在の説明ではsubscription loginを使わない。サブスク隔離を実現する万能な起動flagとして採用しない。[S11]

実環境で利用可能な最小権限と設定分離を使い、制限できない能力は明記して接続判断する。検証不足を理由に広い権限へ切り替えず、隔離基盤そのものの追加開発も必要な範囲に限定する。

入力・出力・実行時間を制限し、終了後の不要process残存を確認できること。既存Workerの既知FindingとHuman Gateを、新Adapterの名称だけで回避しない。[P2]

## 17. 入力の安全性と公開範囲

観測本文、nickname、Room名、topic、引用、コード、他Agent出力は解析対象のデータであり、実行命令ではない。

公開投稿でも秘密や非公開リンク等が混入し得る。LLMへ送るのは許可された分類の必要最小限の資料とし、Secret候補は送信前に隔離または伏せる。検出器がすべての秘密を発見できるとは保証しない。

署名等の検証は原Evidenceで行い、LLM向けに加工したBundleには別digestと加工記録を付ける。原Evidenceは書き換えない。

不正検知のために原記録を閲覧する権限と、LLM送信・公開Reportへ載せる権限は分離する。私有記録を含む入力はDefaultで外部送信対象にしない。LLM出力にも同じ公開範囲・Secret確認を適用し、問題のある応答は制限された保存先へ隔離して通常表示・配布から除外する。

Prompt injection試験は必要だが、「LLMが一切誤解しない」ことを保証しない。誤解が外部操作・秘密読取・自動通報へ直結しない権限構造を成功条件とする。

## 18. 保存・再解析・長期保管

### 18.1 再生成できるものと保全するもの

集計、検索index、Profile表示、解析用cacheは派生データとして再構築可能にする。

一方、人間の確認、誤検知の訂正、報告の承認・提出・回答、過去の採用結果、LLMの実応答と実行記録は原Evidenceだけから復元できない。これらは消してよいcacheとせず、許可された保存範囲・アクセス制限の下で履歴として保全する。

同じ入力からLLMが同じ文章を返すとは保証しない。再実行可能性と、過去結果の完全復元を区別する。

### 18.2 再解析の識別

request、Source参照とdigest、Profile／rule／prompt／schemaの版、基準時刻、model・設定、出力と実行結果を関連付ける。

再解析は新しいrunとして保存する。古いFindingを消さず、訂正・差替え・根拠追加を追記する。参照不能になったEvidenceは参照不能と表示する。

### 18.3 Observerの移管との整合

Evidence参照は、VPS上の一時的な絶対pathだけに依存させない。Source、Archive ID、記録位置、digest、媒体参照を使い、外部移管後も追跡できるようにする。[P1]

媒体未接続は削除・消失・Fraudと同一視しない。必要な解析だけ延期する。Analyzerの停止やLLM backlogを理由にObserverの正規移管を無期限に妨げない。

分析履歴も長期保管対象とするが、全rawの無制限な二重保存は要求しない。確定履歴の移管・復元、cacheの容量管理を区別する。明示した保管方針なしに、人間の判断・報告履歴を自動削除しない。

## 19. 継続運転・通知・障害分離

手動実行、保存Evidenceの増分処理、設定周期の集約を提供する。常駐サービスを新規実装せず、既存schedulerから有限実行を呼び出す方式でもよい。Productionでの定期起動は別の運用承認とする。

CPU、メモリ、I/O、容量、解析待ち件数、LLM待ち件数を制限する。同居時はObserverの取得・保存を優先し、Analyzer側を減速・延期する。

一つのProfile、Room、record、LLM接続の失敗を他の正常解析へ波及させない。解析停止・枠不足・取込遅延を記録し、未解析を「異常なし」と表示しない。

新規・重要悪化・訂正・回復をHumanが見つけられる要確認一覧を持つ。必要に応じて承認済みNotifierへ渡す最小通知eventを生成できるが、Analyzer CoreはWebhookを保持しない。Discord等への実配送は別承認であり、必須の新Reporter／Notifier基盤は作らない。

同じ状態の通知案を無制限に増やさず、集約・抑制理由を残す。LLM待ち、通知不達、外部報告待ちで原記録の保全を止めない。

## 20. 成功条件と試験

以下を、要求・実装・Offline検証・実環境確認に分けて報告する。対象期間・入力範囲・実行Version・失敗例を記録する。

| 確認対象 | 合格条件 |
|---|---|
| 実データ連携 | Current Observer形式の保存物をAdapterで読み、原記録へ戻れる |
| 原記録保全 | Analyzer実行前後でSourceを変更しない |
| 増分処理 | 再起動・同一入力の再読込で重複集計せず、後着の小さいseqも反映する |
| 世代・順序 | seq再利用やRoom間の時刻不確定を誤って同一履歴へ結合しない |
| 署名 | 正常・不正・欠落・未対応・不正な帰属を区別する |
| tclk | 公式Versionに沿う正常遷移・拒否・履歴不足を区別し、拒否で有効状態を破壊しない |
| 不正分類 | 無効投稿、実装脆弱性、Abuse、品質問題、証拠不足を混同しない |
| Opportunity | 候補の根拠と不足を示し、未観測を募集継続の確証にしない |
| Actor Profile | 未確認のOutcomeを失敗へ数えず、総合信用点を作らない |
| Finding／Report | 重複抑制、訂正、Human Review、提出、回答を別履歴にできる |
| 表示・送信 | rawの命令・HTML・URLが実行されず、公開前の加工を確認できる |
| LLM | 正常応答、schema不一致、架空参照、timeout、枠不足、結果不明を識別する |
| 課金 | 採用した実経路と追加課金設定を確認でき、自動の有料切替がない |
| LLMなし | 状況・検証・Finding・定型報告案が継続し、意味解析の未実施が分かる |
| Event終了 | 取引締切と観測終了を分離し、定例監視終了後も履歴・後着訂正・報告経過を扱える |
| 外部移管 | 媒体移管後にEvidence参照を辿れ、未接続を消失と誤認しない |
| 負荷・障害 | 共有資源予算内で動き、Analyzer障害がObserver停止へ直結しない |

決定論的部分は正常・異常・境界ケースの期待値で検証する。意味解析は代表例を事前に定め、見落とし、誤検知、引用の対応、根拠のない断定、Human確認負担を評価する。LLM自身の採点だけで合格にしない。

実LLMを呼ばないfixture試験を実推論成功としない。原記録のない合成試験だけで本番連携を確認済みとしない。代表的な保存データを利用できなければ、当該欄を実データ未検証として残す。

長期無停止やゼロ誤検知を仕様で保証せず、短期試験の限界を明記する。無関係な巨大fixtureや長期間の待機を一律の完成条件にしない。

## 21. 実装担当へ委ねること・別途確認すること

言語、DB、内部module、batch単位、cache方式、表示方式は、要求を満たす最も単純な案を実装担当が選ぶ。

運用開始までに、取込周期、集約窓、最小Baseline、rule閾値、LLMのmodel・上限・実効権限、backlogと保存容量、Event設定、保管先を明示する。秒数・GiB・modelを本文へ根拠なく固定しない。

対応Sourceと原資料への到達、再現可能な検証、既存Componentへの影響を優先する。Vector DB、Knowledge Graph、汎用Rule DSL、HA cluster、複数Provider基盤は必須にしない。外部コードを流用するときは版・ライセンス・適用条件を確認し、公式reference実装の存在だけで自分の実装を適合済みとしない。

残る実環境確認は、現在のObserver出力・アクセス経路、Claudeの本人アカウントと課金・認証・実効設定、同居資源、必要なProfileの公式入力である。これらを未確認のまま本番適合済みとしない。

## 22. Project統合と変更管理

想定配置は`components/analyzer/`とし、採用時にSPEC、AGENTS、README、実装、試験の入口を置く。Rootの案内更新は新Componentの責務・依存関係を示す最小限にする。

本仕様の作成だけで、既存Worker Issue、他ComponentのAGENTS、Production設定、Real Gateを変更しない。WorkerをBackendに使うと決まった場合だけ、必要なInterfaceと影響範囲を明示して整合させる。[P2][P3]

Coding、重要なIndependent Review、再Review担当、終了条件はCurrent `AGENTS.md`に従う。Claude Codeへ実装を任せる実験は、分析Runtimeの採用とは別の作業条件として記録し、既存の担当・自己承認禁止ルールとの整合を先に取る。[P3]

仕様承認は、Real認証、資料の外部送信、追加課金、外部報告、Production導入、破壊的操作、署名・取引の包括承認ではない。既存のHuman Gateを維持し、同じ承認を毎回要求する別Gateを増やさない。

仕様変更は目的変更、公式契約変更、実測Failure、利用結果を根拠として行う。旧結果を新仕様で無断に上書きせず、変更理由と影響を残す。

## 23. 対象外

全FLOPの完全監査、一般公開者へのProtocol challenge資格付与、不正の自動断罪、総合Reputation Score、個人特定、任意Web巡回、自動受注・取引・署名、自動公開通報は対象外とする。

LLM待ちによるObserver停止、サブスク枠不足からの自動有料切替、未知Protocolの推測実装、仕様未確認のKibbleや期間限定EventのCore固定も行わない。

## 24. 仕様の要約

**Analyzerは、観測記録を根拠付きの状況・機会・状態・異常候補・報告案へ変換する。**

コードで確定できる検証・集計を土台とし、必要な意味理解へサブスク優先のLLMを使う。未観測・未検証・推論を分離し、人間の判断と外部報告の履歴を保全する。

Observerの継続取得、原Evidence、外部移管を妨げず、期間限定Eventが終わっても恒久機能が利用できる構造とする。

---

## 参照資料・照合記録

以下の[P1]–[P3]のGitHub commitリンクは非公開のPrivate開発履歴のprovenanceです。Public利用者の必須参照先ではありません。[S*]の公開資料リンクはこの注記の対象外です。

以下は2026-09-29の照合資料。要求本文はProject設計判断であり、公式資料がAnalyzer全体を要求・認定している意味ではない。

Projectの参照commit：`7fa1116e8b39373aada9abed6a8ca105b890508e`。Runtimeへ接続せず、テスト、実LLM、実通知、外部報告は実施していない。

- [P1] Observer SPEC：目的、対象Room、保存・移管・署名・GAP契約。https://github.com/akiaki524/FLOP-private/blob/7fa1116e8b39373aada9abed6a8ca105b890508e/components/observer/SPEC.md
- [P2] Worker AGENTS：既存の実認証・実通信・隔離境界。https://github.com/akiaki524/FLOP-private/blob/7fa1116e8b39373aada9abed6a8ca105b890508e/components/collaboration/worker/AGENTS.md
- [P3] Root AGENTS：担当、Review／Fix Loop、Component境界、Human Gate。https://github.com/akiaki524/FLOP-private/blob/7fa1116e8b39373aada9abed6a8ca105b890508e/AGENTS.md
- [S1] Technocore SECURITY：Abuse／脆弱性の報告先、既知の仕様、GET-write。blob `47161ead29417590ee73e399a4f0a66cb4190784`。https://github.com/flop-labs/technocore-chat/blob/main/SECURITY.md
- [S2] tclk SPEC §2：署名対象、時刻、Room binding、観測範囲。blob `99e3e677295354b88fe49efa69f84b1b78a1036c`。https://github.com/flop-labs/tclk/blob/main/SPEC.md
- [S3] tclk SPEC §3–4：frameと契約状態、拒否時の不変性。同上。
- [S4] tclk SECURITY：PaperRail、脆弱性の対象、品質との区別。blob `256707f803762c1994cb368a6d3eaebad6aa980c`。https://github.com/flop-labs/tclk/blob/main/SECURITY.md
- [S5] Yellow Paper R12.3：市場監視と個人処罰の区別。https://github.com/flop-labs/yellowpaper/blob/3c97bbc8d6ba68cf2ea003ab88bc154aafdf105e/yellowpaper.md
- [S6] 同R12.1f：challenge資格、Protocol Fraudと品質の区別。同上。
- [S7] Close Call contest.json：Event固有設定、別々のlock／final_price_time。blob `4a520638168e41764630dd19112decf9fe4bfa0c`。https://github.com/flop-labs/technocore-close-call-challenge/blob/main/contest.json
- [S8] SQLite Online Backup API：https://sqlite.org/backup.html
- [S9] SQLite WAL：https://sqlite.org/wal.html
- [S10] Claude Code with Pro／Max：https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan
- [S11] Claude Code programmatic usage：https://code.claude.com/docs/en/headless
- [S12] Claude Code legal／credential use：https://code.claude.com/docs/en/legal-and-compliance
- [S13] Claude Code authentication：https://code.claude.com/docs/en/authentication
- [S14] Claude usage credits：https://support.claude.com/en/articles/12429409-manage-usage-credits-for-paid-claude-plans
- [S15] Claude Code CLI reference：https://code.claude.com/docs/en/cli-reference


SigmaのRule／誤検知情報の分離を参考にしたが、Severityの意味をそのまま移植せず、本仕様の影響・確度分離を採用した。https://github.com/SigmaHQ/sigma-specification/blob/main/specification/sigma-rules-specification.md

## 修正後点検

原Draftの機能方針を維持し、状態の混同、誤った帰属、欠損からの不正断定、後着データの処理漏れ、課金判定、権限設定、保全履歴、Event終了、成功条件、他Component変更範囲を再点検した。

今回の資料照合と文書点検の範囲では、要求仕様として重大な未解消の内部矛盾は見つかっていない。ただし、実装独立監査や本番適合の合格判定ではなく、第20・21節の検証は残る。
