# Technocore Observer｜統合仕様書 v1.0

**作成日：2026-09-26**  
**状態：Current要求仕様。実装済み・本番適合済みを意味しない。**  
**Canonical path：`components/observer/SPEC.md`**  
**対象：FLOP / Technocore Agent Project の取得・保存・運用監視**

## 1. 目的・責務・判断基準

Technocore上の選定済みRoomを継続・並行観測し、後から検証・分析・再利用できる記録を残す。利用目的は、仕事やAgent活動の把握、イベントの検証、Project改善、不正・異常候補の検証・報告、将来のAnalyzer / Agentへの入力とする。

Observerの責務は、取得、取得状態の確認、永続保存、取得範囲と欠損の記録、限定的な自動復旧、運用監視、Discord通知、検証付きの外部保管への移管までとする。意味解析、仕事の受注、署名、取引、自動通報、信用評価の確定は責務に含めない。Analyzerの設計・MVP・実装をObserver完成の条件にしない。

本文はObserverのCurrent要求仕様であり、個々の実装方式・調整値と区別する。既存機能は流用し、未対応の要求だけを実装する。新しいGateやFrameworkを作ること、既存Gateを完走すること自体を目的にしない。重要な判断は、観測の継続、記録の再利用性、人間の作業負担、具体的な危険の抑制への寄与で行う。

**決定の由来：** 対象Room、並行観測、保存期間、外付けHDD等への移管後削除、再起動上限、Discord通知、Analyzerを主対象にしない方針はこの会話のHuman決定による。以下の保存区分、移管検証・削除条件、再起動の数え方、通知分離は、その意図を実装可能にする技術的な具体化である。公式SourceがProjectの観測先や保存日数を要求している、という意味ではない。

## 2. 観測対象と期間

### 2.1 対象Room

次の12 Roomを並行観測の対象とする。Roomの再選定は今回の作業では行わない。`lobby`を停止して他Roomへ乗り換える構成にはしない。

| 区分 | Room | 用途・位置付け | 初期の保存区分 |
|---|---|---|---|
| 常設 | `lobby` | 一般状況の把握。既存観測を継続 | 標準 |
| 常設・高優先 | `tclk-offers` | 仕事・契約候補の把握 | 重要 |
| 常設・高優先 | `kibble` | 仕事・成果・評価に関する活動の記録 | 重要 |
| 常設 | `events` | 新しい公開Roomの発見 | 標準 |
| 試行観測 | `sub_economy` | 選定済みTrial対象 | 標準 |
| 試行観測 | `zk-desk-a` | 選定済みTrial対象 | 標準 |
| Close Call | `close1` | イベント本体 | 重要 |
| Close Call | `d-close1-price` | イベント関連記録 | 重要 |
| Close Call | `d-close1-flow` | イベント関連記録 | 重要 |
| Close Call | `d-close1-positions` | イベント関連記録 | 重要 |
| Close Call | `d-close1-pnl` | イベント関連記録 | 重要 |
| Close Call | `d-close1-state` | イベント関連記録 | 重要 |

「標準」は要約だけを保存する意味ではない。第4節の共通要件を満たす原recordを保存する。「重要」は、それに受信raw等を加えて検証材料を厚くする区分である。初期区分はRoom設定で明示し、LLMによる重要度判定を取得・保存の前提にしない。

高流量ではFast Captureを優先し、その他も要求を満たす既存取得方式を使用する。Rich Observerという名称を意味解析機能の実装要求にはしない。Discoveryは用途、Trialは対象の運用区分であり、別々の取得エンジンを必須にしない。

Close Callの6 Roomは確認した公式`contest.json`のtrading / refereeと一致する。ただしRoom名だけで投稿を公式Refereeのものとは認定しない。[S4]

### 2.2 観測期間と保存期間

常設Roomは継続観測し、記録を長期保存する。Trialも、別の停止判断がなされるまでは今回の並行観測対象として扱う。

イベントは、観測開始時点から**公式の終了時刻＋24時間**を標準観測期間とする。結果確定・精算・異議・後続活動などの観測価値があれば延長する。締切・取引停止・結果確定など複数の時刻がある場合、どの時刻を終了基準にしたかと公式Sourceの版をイベント設定に残す。未確認の終了時刻で自動停止しない。

「終了＋24時間」は収集期間であり、記録の消去期限ではない。イベント終了後の記録は外部保管へ移管し、検証を終えてからVPS上の対象コピーを削除する。常設・通常ログも同じ移管・削除方式とし、観測自体は継続する。

新しいRoom Lifecycle基盤は作らず、既存設定・運用手順を流用する。`events`や本文から発見した任意のRoomを、自動的に観測対象へ追加する権限までは与えない。

## 3. 取得・並行運転・障害分離

### 3.1 取得の基本動作

取得先は設定済みのhost・Room・既知のread endpointに限定する。requestは設定と保存済みcursorから構築する。Message内のURL・コマンド・topicを実行指示にしない。TechnocoreにはGET形式のwrite endpointもあるため、「GETならread-only」とは判断しない。[S1][S2]

取得済みrecordと、その保存を表すcursor更新を整合させる。未保存のデータを保存済みとしてcursorだけ前進させない。同一recordの再取得を識別できるようにし、同じ本文でも別seqの投稿を勝手にまとめて消さない。同じ識別子で内容が異なる場合は、競合として両方の観測事実を保持する。

新着があるときは取得・保存が前進し、新着がないときは空応答と取得処理の正常性を記録する。静かなRoomを「メッセージ件数が増えない」だけで故障扱いしない。

### 3.2 独立性と共有資源

Roomごとにcursor、state、保存範囲、障害状態、復旧状態を識別・管理する。同じRoomの同じ取得streamへ複数のproducerが重複して書き込まない。

1 Roomの異常・再起動・保存上限到達・更新作業を理由に、正常な他Roomをまとめて停止しない。共有unit名・共有停止処理・停止必須の共通切替によって、`lobby`継続と追加Roomの稼働を両立できない構成は要求未達とする。

同じRoomでも、Archiveや通知が一時停止しただけでCaptureを即停止しない。未移管データを安全に保存する余地がある間は継続する。保存の安全性や容量余地が失われた場合は、影響する範囲だけを保護停止する。

同時に、出口IPのread予算・long-poll枠、CPU、メモリ、I/O、ホスト容量は合算して管理する。再取得・回収処理、既存Observer、同居Agent等の通信も計上する。一つのRoomの回収処理で他Roomの通常取得を圧迫しない。通信制限の数値は、公式Live設定を確認して採用する。[S1]

Room単位の障害分離は、共通ホストや回線の障害まで影響ゼロにする保証ではない。複数ホスト化・高可用クラスタは本仕様の必須条件にしない。

## 4. 記録形式・検証可能性

### 4.1 全Roomに共通する保存要件

受信したrecordは、本文を要約・改変せず保存する。少なくとも、次の情報を関連付けて後から取り出せるようにする。

| 情報 | 保存する内容 |
|---|---|
| 識別 | 取得元host、Room、保存streamの識別子、ローカル観測epoch、seq、ローカル保存ID |
| 受信値 | `ts`、`from`、`text`、存在する`nonce / sig`、受信した追加field |
| 取得情報 | 取得時刻、観測したgeneration、requestのcursor、取得batchとの対応、取得経路 |
| 保存情報 | 保存形式の版、recordまたはbatchのSHA-256とhash対象の形式、保存先参照、Archiveとの対応 |
| 不完全性 | GAP・競合・境界未確定・解析不能・サイズ制限による省略と、その理由 |

hashの対象bytes・文字コード・再シリアライズ方法を保存形式で定義し、受信rawのhashと正規化recordのhashを区別する。

元recordにない署名・DID等を補わない。署名らしいfieldがあることと署名検証成功を区別する。本文のUnicode文字列を正規化せず、nonce等の整数を丸めない。未知fieldも安全な処理・容量上限内で保持し、保持できなかった場合は省略の存在を明示する。

generationは観測したサーバー情報として保存し、ローカルepochと同一視しない。普通の再起動で識別子やcursorを初期化しない。Room再作成・世代境界の扱いには旧記録と区別できるepochを使用し、不確かな境界を確定事実として結合しない。

### 4.2 標準保存と重要保存

**標準保存：** JSONを読み取って再シリアライズする方式を許容する。ただし、共通要件の値を保ち、署名対象の本文・nonce・Room等を再検証処理へ渡せること。通信上の空白・escape表記まで同一とは主張しない。

**重要保存：** 標準保存に加え、取得に使用したHTTP応答本文のrawと、Room・generation・取得条件を対応付ける。適切な場面で取得した`/export`のraw JSONLも、その経路・取得時刻を区別して保持する。poll応答のrawと、サーバー保存JSONLのrawは別物として表記する。HTTPライブラリの展開後の応答本文を保存する場合は、その処理段階を明示する。可逆圧縮は許容し、圧縮ファイルと復元後の原bytesをそれぞれ照合できるようにする。[S1]

rawは既に取得した応答から保存することを基本とし、証拠強化のために全recordを別requestで取り直したり、通常運転で全量exportを反復したりする方式を必須にしない。上限超過・異常応答は、安全に残せた範囲と打ち切りを明記し、保存成功を偽らない。

重要／標準は原recordを捨てる区分ではなく、追加rawの保存強度の区分である。後から通常ログの価値が判明しても共通の検証材料は残るが、過去に保存しなかった通信rawは復元できない。

### 4.3 保証する範囲

署名再検証に必要な値を保つことと、Observerが全投稿の署名を取得時に検証し終えていることは別とする。未実施の検証は未検証として残す。署名検証・分析が遅れても取得を止めない。

Technocore署名は`room|nonce|text`を対象とし、`seq / ts`は投稿者の署名対象外である。保存hashは記録間の同一性・改変検出の材料であって、内容の真実性、悪意ある保存管理者からの保護、全履歴の完全性を単独で保証しない。[S1][S3]

Raw、検証結果、解釈・評価は分離する。生データ、運用状態、分析結果を一つの「verified」という表示にまとめない。

## 5. 保存先・容量・外部移管

### 5.1 保存先と約10日分の目標

VPSは稼働中の取得・保存と直近ログの参照を担い、外付けHDD等は長期保管先とする。通常ログ・イベントログとも、長期保存はVPSへの永久蓄積を意味しない。

全対象Roomで**約10日分のログを保存できる容量**を初期の設計目標とする。常設では、通常運用で直近約10日分をVPS側で参照できることを目安に、古い確定ログを外部へ移す。イベントは観測終了後に全期間を移管可能とし、終了済みデータをVPSに10日間残す義務は設けない。10日は無条件の保証でも、経過したら自動削除する期限でもない。

Room別の流量・bytes/messageだけでなく、採用した保存形式での実消費を測る。本文、追加raw、Spool、Archive、Manifest、WAL、metrics、Monitor履歴、通知待ち記録、移管用一時領域を合算し、ホスト全体の余裕を別に確保する。SpoolとArchiveに同じ10日分を二重保持することは必須にしない。

GiB値、圧縮・分割の具体値、配分は実測後に設定する。目標を満たせない場合は不足を明示し、効率化・移管方法・追加容量等の案を比較する。無断で対象Roomや保存精度を落として適合扱いにしない。費用・権限の追加はHuman判断とする。

### 5.2 移管・検証・削除の順序

長期保管は「コピーしたので削除」ではなく、次の順序で行う。

**書き込み完了範囲を確定 → 外部媒体へコピー → 移管先を読み戻して検証 → 移管記録を確定 → 許可されたVPSコピーだけ削除**

移管単位には、本文と追加raw、対応するRoom・epoch・seq範囲、既知GAP、必要なManifest・索引・形式説明を含める。元の稼働DBがなくても、その移管単位を読み出し、保存した検証材料へ到達できることを要求する。すべての内部DBをそのまま永久保持することは要求しない。

移管先での検証は、書き込み完了を確認したうえでファイルを開き直し、全移管ファイルのサイズ・SHA-256を元の確定済みmanifestと照合する。さらに、形式として読み出せること、対象範囲・既知GAP・必要な参照が揃うことを確認する。hash確認と形式確認は一回の読み取りにまとめてもよい。検証していない媒体を成功扱いにしない。

VPS側には、保管媒体の識別子、archive ID、対象範囲、hash、検証結果、移管・削除状態への参照を残す。詳細な過去manifestも必要に応じて分割移管できるが、外部保管先を探せなくする削除はしない。

### 5.3 削除できるもの・できないもの

削除対象は、検証済み移管に含まれ、稼働中のWriter・回収処理・必須consumerが不要になったと確認できる範囲のVPSコピーに限定する。稼働中のDB、WAL、未確定shard、再開cursor、未処理データ、稼働上必要な参照を一括削除しない。

SQLiteのライブファイルを単純コピーして成功とせず、必要な場合はOnline Backup API等で整合したsnapshotを作る。WALを含むライブDBの分離・削除・renameを通常の移管手段にしない。稼働データの世代交代やpruningが必要なら、継続性と移管対応を保つ専用の保守処理として実装・検証する。[S5][S6][S7]

同じRoomのcursorやArchive参照が、移管済みの記録を「消失・破損」と誤認して起動不能にならないことを確認する。変更後に、取得継続・Archive追従・通常再起動が成立することを検証する。

これは「Failureを隠すための削除禁止」の例外ではなく、記録を別媒体に保存し続けるための正規移管である。旧Evidenceの内容は書き換えず、移管・削除の事実を追記する。

### 5.4 移管不能と長期保管の限界

外付けHDDが未接続、検証失敗、転送途中の場合は元データを削除しない。移管に人の操作が必要なら、必要時刻より前にDiscordで知らせる。移管頻度は、実消費、保持目標、転送所要量、空き容量の余裕から設定する。

容量の安全余地を失う場合は、無断削除ではなく影響範囲を保護停止する。未回収範囲は第6節に従って扱う。原記録の保全とホスト全体の保護を、見かけの無停止より優先する。

VPSコピー削除後に外部媒体が一つだけなら、その媒体の故障・紛失による消失には耐えられない。重要記録の別媒体への複製を推奨するが、複数媒体や専用バックアップ基盤の導入を今回の観測開始の必須条件にはしない。移管成功は、将来の媒体健全性の永久保証ではない。

## 6. 再起動・回収・GAP

予期しないprocess停止は自動復旧対象とする。短時間に繰り返す同一Roomの同一障害系列について、**初回停止後の自動復旧起動を最大3回**とする。最初の通常起動は3回に含めない。3回の復旧起動を試しても安定動作に戻らない場合は自動復旧を打ち切り、対象Roomと原因・試行回数・要対応をDiscordへ通知する。

短時間の判定窓、起動間隔、安定稼働と判断して回数を戻す条件は、既存機構と実測に基づく設定値とする。processを起動できた瞬間だけで回数をリセットしない。systemd・Docker・旧Supervisor等が互いの上限を回避して再起動し続けないよう、復旧の管理主体を一本化する。上限到達後は、明示的な再開操作まで無限retryへ戻らない。[S8][S9]

意図した手動停止・イベント終了による運用停止・保護停止を勝手に解除しない。容量不足、state破損、識別不整合など、再起動では直らないと確認できた状態に3回の無駄な再起動を適用しない。原因不明の停止は、保全された状態から上限内の復旧を試してよい。ホストの自動reboot、stateの無断再初期化、破壊的修復は行わない。

再起動後は保存済みcursorを使用し、公式read interfaceで取得可能な未取得ログを、時間・通信・容量の範囲を定めて回収する。公式read_messagesは、since条件を満たす最新N件を返すため、通常pollを無制限の過去ページ送りと仮定しない。[S12]回収が現在の取得を妨げるときは打ち切り、未取得範囲を残して新着観測へ戻る。[S1][S3]

GAPにはRoom、epoch、分かる範囲のseq始点・終点、検出時刻、回収結果・失敗理由を残す。GAP区間数と未取得seq数を区別する。範囲を確定できなければUNKNOWNとして残し、数字を作らない。後から一部回収できた場合は元のGAP記録を消さず、解消範囲を関連付ける。

**過去のGAPや新しい欠損の検出だけを理由に、健全な現在の取得を停止しない。** ただし、保存不整合・通信契約を扱えない状態等は別の障害として処理する。世代変化は記録を分離して継続することを目標とし、安全に境界を確定できない場合は対象Roomのみ保留・通知する。

## 7. Health・Discord通知

### 7.1 監視内容

Roomごとに、最終取得成功、空応答を含む応答の鮮度、新着時の保存前進、Archive追従、GAP、再起動、保存層ごとの容量を見られるようにする。processが生存していても取得workerが止まっている状態を区別する。metrics欠落を正常とみなさない。

Monitor・metrics・通知履歴にも分割・移管等の有界な運用を用意する。補助ログの固定上限到達やDiscord配送失敗だけで、正常なCaptureを一律停止する構成を避ける。実際のホスト容量危険や保存整合性の異常は第3・5節の保護対象とする。

Monitorの通常詳細sampleはVPS上で**10日間保持**し、原則**7日ごとに外部backup**する。約3日の運用余裕を持たせる方針であり、10日経過だけでは削除しない。自動削除できるのは、対象rangeの外部backupについてreadback / hash / 範囲対応を検証でき、かつ10日を超えたVPSコピーだけとする。backup未実施・失敗・検証未完了・range不明・readback/hash確認不能の場合は保持し、Retention pending / backup requiredをdurable findingとして残す。この未確認だけで正常なMonitor / Captureを停止しない。

terminal failure、Capture / Archive stop、recovery、capacity incident、重大Monitor failure、checkpoint、Incident判断に必要なsummaryは通常sampleより長期保持するEvidenceとして分離し、上記10日retentionの対象にしない。Capture / Archive本体、GAP Evidence、Trial Historical Evidenceも対象外とする。外部backup / transfer / safe pruningの実装と運用は別workstreamであり、未実装の間は削除しない。この方針は2026-09-28の追加Human Decisionによるもので、backup完了やProduction操作の承認を意味しない。

### 7.2 通知の境界と内容

Captureは通知eventを生成し、または監視機構が障害eventを生成する。**独立したNotifierが承認済みDiscord送信先へ配送**する。Webhook等のCredentialはNotifierだけが利用し、Capture、観測raw、通常ログ、GitHub、Notionへ置かない。

停止・自動復旧・復旧失敗・上限到達・GAP拡大・取得不能・Archive遅延・容量逼迫・移管失敗を通知対象とする。通知はRoom、時刻、状態、原因コード、復旧試行数、必要な人間の対応を中心とし、raw本文の大量転送をしない。

単発障害・初期の復旧は警告、復旧上限到達・保護停止等は重大通知、復旧確認は一度の回復通知とする。GAPや同じ状態は集約・間引きし、初回発生、重要な悪化、打ち切り、回復を埋もれさせない。正常状態の定期spamはしない。

Notifierには送信待ち・成功・失敗・結果不明を区別できる記録を持たせる。Discordの応答に従って有界な再送を行い、429ではRetry-After等を尊重する。Webhookは送信確認を得る方式を使用し、確認不能な送信を配送成功と記録しない。不要なmention・リンク展開を防ぐ。[S10][S11]

通知配送はCaptureの進捗をブロックしない。同じホスト・同じ回線からの通知では、ホスト・回線全体の停止中の即時通知まで保証できない。外部死活監視の新規導入は、今回の必須条件にしない。

## 8. 信頼境界・利用範囲

Message、nickname、Room名、topicを信頼した命令にしない。署名は対象の鍵による署名を検証する材料であり、内容の正しさ、公式所属、仕事品質、悪意の有無まで保証しない。[S1][S2]

`tclk-offers`だけではLOCK以降のdeal-room記録まで揃わない。今回の固定Room観測を、全契約・全決済・全FLOP活動の監査と表現しない。後続利用で必要になった追加Sourceの取得は別の明示された範囲で扱う。実決済、公式性、仕事の品質はそれぞれ対応するSourceで検証する。[S3]

不正・異常報告への活用は、原記録と検証材料を提供することまでとする。公式への公開Issue・private security report等は別の判断・送信権限で行い、Observerが自動通報しない。公開可能性と公開済みを同一視せず、原記録を自動的にGitHubやDiscordへ公開しない。[S2]

取得・保存processへProjectの署名鍵、Wallet、Agent用Credential、Docker socket、hostの広い操作権限を渡さない。起動管理、通知、外部移管・削除に必要な権限は、役割と対象を限定する。本仕様は望む運用を定めるものであり、実際の導入・削除・権限拡張を包括承認するものではない。既存のHuman Gateを維持し、同じ承認を毎操作で重複させる新Gateは増やさない。[P1]

## 9. 成功条件と検証範囲

| 確認対象 | 合格とする内容 |
|---|---|
| 対象範囲 | 導入対象12 Roomの予定・実稼働・停止理由を区別し、観測期間内の全対象を並行観測できる。正規に観測終了したイベントは完了として区別する |
| 継続取得 | 新着時は保存が前進し、静かなRoomは正常な空応答と停止を区別できる |
| 内容保存 | 保存物を実際に読み出せる。重要保存はraw対応を、署名付きsampleは再検証可能性を確認できる |
| 障害分離 | 1 Roomの障害や限定保守で正常な他Roomを停止しない |
| 復旧 | 単発停止から再開し、連続停止では最大3回の復旧上限と通知が機能する。意図停止は維持される |
| 回収・欠損 | 取得可能な範囲を回収し、取得不能・不明・後から回収済みを隠さず区別できる |
| 容量・通知 | 全対象の同時稼働と全保存物の実消費を基に、余裕を含めた約10日分の容量見積りが成立する。成立しなければ容量要件は未達とする。移管時期とDiscord通知の配送結果を確認できる |
| 外部移管 | 移管先でhash・範囲・形式・参照を検証でき、不成功時は削除しない。許可された削除後も観測・再起動が成立する |

検証は既存Testと今回の差分に応じた必要な試験を使用する。故障・削除試験は原則隔離した試験データで行い、本番の破壊的な再現を必須にしない。通知の実配送や本番の取得前進は、許可された実環境で別に確認する。

「約10日分」は実測増加量と容量から評価する目標であり、10日間待つことや10日分の巨大fixture生成を一律の合格条件にしない。ただし短い観測から長期無停止を保証せず、推定条件と未確認範囲を残す。

保存hash一致、Archive実ファイルのreadback、投稿署名の検証、全履歴の完全性は別の確認結果として報告する。PR merge、CI PASS、container Runningだけを本仕様への適合証明にしない。

## 10. 実装計画・設定値・変更管理

### 10.1 今後の実装順序

この仕様を基準に既存実装との差分を整理し、既存の取得・保存・復旧部品を流用する。まず並行稼働と保存契約の差分を埋め、その構成に限定再起動・Discord通知・補助ログの容量管理を接続する。併せて、通常ログとイベントログの検証付き移管・正規削除経路を実装する。

実測による容量配分と既存のReviewを経て、`lobby`を維持した段階導入で全対象へ展開する。段階導入は対象を減らす仕様変更ではなく、反映順序である。未導入・未検証を明記し、全対象の要件を満たすまで全体完成とは扱わない。既存の正常な観測を、文書整備や無関係な再試験のためだけに停止しない。

### 10.2 実装時に設定する項目

Room別の保存容量・追加raw区分・poll/回収予算、再起動判定窓・待機間隔・安定reset条件、Health閾値、通知集約間隔、確定ログの分割単位、移管先・実行頻度・所要余裕は設定として明示する。

ここでは秒数やGiB値を決め打ちしないが、運用開始時に未設定のままにはしない。既存値・公式の実設定・実測からEngineeringが選び、その根拠を短く残す。目的・保存範囲・費用・権限を変えない調整を毎回Humanへ差し戻さない。対象の削減、精度・保持方針の変更、支出、破壊的操作・権限拡張は既存の承認範囲に従う。

### 10.3 依存ソフトウェアと変更管理

実際に動くPython / SQLite / container等の版を確認し、該当する既知の重大なデータ破損不具合がある場合は修正版または確認済みの修正適用版を使用する。hostの版だけでcontainer内の版を推定しない。公式SQLiteが公表したWAL-reset不具合は、今回確認する具体的な対象である。[S6]

本仕様は固定不変にせず、Humanの目的変更、公式契約変更、実測Failure・利用結果に基づいて必要箇所を更新する。変更理由と影響要件を一度記録し、本文と実装・運用設定の不整合を残さない。現在のAGENTS等と衝突する自動復旧・移管・削除は、関連する契約を明示的に整合させてから導入する。

---

## 付録A｜今回のコード照合結果（要求仕様ではなく、2026-09-26時点の確認記録）

参照main：`9cce6a5149f7d74eebaa2696e0f5b4185971c57a`。PR #26は未merge、HEAD `dd8242aff18bfe62221d2aa7a927056dc8a5cbb2`。[P0][P2]

| 項目 | 確認した差分・留意点 |
|---|---|
| 並行稼働 | PR #26は1 Attempt・1 Roomと共有unitの契約。lobbyを動かしたままclose1のPRE/STAGEを通せないと明記されており、PR単体では本仕様を満たさない。[P2][P3] |
| 多Roomの設定 | 現行Production設定は1〜2 Room、highは最大1 Roomという制約を持つ。今回の対象構成と一致しない。[P4] |
| 自動再起動 | V2契約はsystemd / DockerのRestart=noを検証する。現行契約のまま本仕様の自動再起動を実現済みとは扱えない。[P3] |
| 通知 | V2 runnerは旧notification_eventを無効化する。別Notifierの接続・配送確認が必要。[P5] |
| 容量 | capacity_modelは1日を基準に計算する。約10日目標への適合は別確認。Monitor・metricsの固定履歴上限も継続運転の対象。[P4][P6] |
| 外部移管後の削除 | deletion_enabled=Falseが現行契約。SpoolとManifestの参照・整合検査を維持する必要があり、ファイルを移して消すだけの追加では不十分。[P4][P7] |
| 保存精度 | 既存保存物には再シリアライズされたJSONが含まれる。受信raw・署名再検証・実Archive読み戻しを一つの保証として扱わない。[P8][P9] |
| SQLite依存版 | 公式のWAL-reset不具合は実装時の確認対象。node-01／container内の実版・修正適用状態を今回は取得していないため、影響の有無は未確認。[S6] |
| 再利用できる土台 | Room-local Spool、単独producer lock、永続化、Archive・receipt構造は存在する。今回の確認だけから全面作り直しが必要とは判断しない。[P7][P9][P10] |

この照合でnode-01へ接続せず、Test、実移管、削除、Discord送信、Production変更は実施していない。Live `/config`と`/llms.txt`のWeb取得は失敗したため、現在の本番制限値は未確認。以前Humanが共有したlobbyの実測を、12 Roomの現在の適合証明へ拡張しない。

## 付録B｜作成後の再点検

これは作成者自身による仕様レビューであり、Claude Code等による独立監査や実機試験ではない。

| 点検項目 | 結果 |
|---|---|
| Human決定の保持 | 並行観測、lobby継続、通常／イベントの長期保管、移管検証後削除、最大3回、Discord、Analyzerを主対象にしない方針を反映 |
| 観測／保存／容量の混同 | 収集終了＋24時間、外部長期保存、約10日分の容量目標を分離。終了済みイベントを観測故障扱いせず、容量不足を確認しただけで合格にしない |
| 重要／通常の検証性 | 通常でも値・署名材料を保持し、重要では追加rawを保存。未保存rawの後日復元は保証しない |
| 削除と保全の矛盾 | 正規移管後の対象コピー削除と、Failureを隠す削除を分離。稼働state・必要な参照は維持 |
| 停止と再起動の競合 | 意図停止・保護停止・上限到達を維持。複数Supervisorによるretry上限回避を禁止 |
| 補助機能の連鎖停止 | 通知や補助ログだけの障害と、実保存・ホスト容量の危険を区別 |
| 過大な完全性保証 | GAP、不明な境界、共有host障害、単一媒体故障、未観測deal room、未確認Live値を明示 |
| 過剰な仕様拡張 | Analyzer設計、Room再選定、HA基盤、巨大fixture、追加承認Gateを必須化していない |
| 実装済みとの混同 | Current要求仕様と、付録Aの実装差分・未検証を分離 |

**再点検の結論：要求仕様として重大な内部矛盾は残っていないと判断する。実装・本番への適合は未完了であり、付録Aの差分と第9節の確認が必要。**

## 参照資料

Project方針の一次資料はこの会話のHuman決定。技術的な事実確認には以下を使用した。公開文書は参照時点の内容であり、将来の改訂とLive状態は区別する。

### 公式資料

- [S1] FLOP Labs, technocore-chat README。取得・export・署名対象・信頼境界・共有通信予算。参照blob `4ea46ad9861a2b953903e8ecc3c4eba8f42b8dca`。https://github.com/flop-labs/technocore-chat/blob/main/README.md
- [S2] FLOP Labs, technocore-chat SECURITY。GET write、untrusted入力、abuse／脆弱性の報告経路。参照blob `47161ead29417590ee73e399a4f0a66cb4190784`。https://github.com/flop-labs/technocore-chat/blob/main/SECURITY.md
- [S3] FLOP Labs, tclk SPEC §2。exact text、署名対象、deal room、保存と再生の境界。参照blob `99e3e677295354b88fe49efa69f84b1b78a1036c`。https://github.com/flop-labs/tclk/blob/main/SPEC.md
- [S4] FLOP Labs, Close Call contest.json。Room一覧。参照blob `4a520638168e41764630dd19112decf9fe4bfa0c`。この確認だけで公式launchの真正性を認定しない。https://github.com/flop-labs/technocore-close-call-challenge/blob/main/contest.json
- [S5] SQLite Online Backup API。https://sqlite.org/backup.html
- [S6] SQLite Write-Ahead Logging、特に§4・§6・§11。§11はWAL-reset不具合と修正版・修正backportを説明する。https://sqlite.org/wal.html
- [S7] SQLite How To Corrupt An SQLite Database File、特に§1.2・§2.5。https://sqlite.org/howtocorrupt.html
- [S8] Ubuntu配布systemd 255系のsystemd.unit manual、StartLimitIntervalSec / StartLimitBurst。https://manpages.ubuntu.com/manpages/noble/man5/systemd.unit.5.html
- [S9] Ubuntu配布systemd 255系のsystemd.service manual、Restart / 意図停止。https://manpages.ubuntu.com/manpages/noble/man5/systemd.service.5.html
- [S10] Discord公式Webhook Resource、wait / allowed_mentions。https://docs.discord.com/developers/resources/webhook
- [S11] Discord公式Rate Limits、Retry-After / retry_after。https://docs.discord.com/developers/topics/rate-limits
- [S12] FLOP Labs, technocore-chat src/store.py、read_messagesの最新N件選択契約。参照commit `0e47f770b13cc27e1e2e199d4cdf70a4778c97cc`。https://github.com/flop-labs/technocore-chat/blob/0e47f770b13cc27e1e2e199d4cdf70a4778c97cc/src/store.py

### Private開発履歴の照合先（非公開provenance）

履歴commitはPrivate側に保持します。Public利用にPrivateへのアクセスは不要です。[P0]はcanonical Privateの現在mainへの可変pointerで、固定された履歴provenanceではありません。Privateアクセスが必要です。

- [P0] Private-onlyの現在main参照（可変、Public利用者には解決不要）。https://api.github.com/repos/akiaki524/FLOP-private/git/ref/heads/main
- [P1] Project運用・権限。https://github.com/akiaki524/FLOP-private/blob/9cce6a5149f7d74eebaa2696e0f5b4185971c57a/docs/OPERATIONS.md
- [P2] Private archive PR #26（historical provenance）。
- [P3] V2契約。https://github.com/akiaki524/FLOP-private/blob/dd8242aff18bfe62221d2aa7a927056dc8a5cbb2/components/observer/docs/capture-upgrade-v2.md
- [P4] Production設定・容量・履歴。https://github.com/akiaki524/FLOP-private/blob/9cce6a5149f7d74eebaa2696e0f5b4185971c57a/components/observer/src/technocore_full_capture/production.py
- [P5] V2 Monitor runner。https://github.com/akiaki524/FLOP-private/blob/dd8242aff18bfe62221d2aa7a927056dc8a5cbb2/components/observer/deploy/production-capture-upgrade-v2/v2/monitor_runner.py
- [P6] Candidate Monitor。https://github.com/akiaki524/FLOP-private/blob/9cce6a5149f7d74eebaa2696e0f5b4185971c57a/components/observer/deploy/production-capture-human/monitor.py
- [P7] Spool consumer・Manifest。https://github.com/akiaki524/FLOP-private/blob/9cce6a5149f7d74eebaa2696e0f5b4185971c57a/components/observer/src/technocore_full_capture/spool_archive.py
- [P8] Observer README。https://github.com/akiaki524/FLOP-private/blob/9cce6a5149f7d74eebaa2696e0f5b4185971c57a/components/observer/README.md
- [P9] Room-local Spool。https://github.com/akiaki524/FLOP-private/blob/9cce6a5149f7d74eebaa2696e0f5b4185971c57a/components/observer/src/technocore_full_capture/spool.py
- [P10] Archive。https://github.com/akiaki524/FLOP-private/blob/9cce6a5149f7d74eebaa2696e0f5b4185971c57a/components/observer/src/technocore_full_capture/archive.py
- [P11] Observer AGENTS。https://github.com/akiaki524/FLOP-private/blob/9cce6a5149f7d74eebaa2696e0f5b4185971c57a/components/observer/AGENTS.md
