# Claude Code CLI 最終Offline Investigation / Human Decision Packet

判定: **B. Not Bounded。F-REAL-01はOpen / Real Gate blocker、Real Claude GateはNO-GO。**
現在のpinと初回利用候補profileでは、調査した関連公式設定を組み合わせても、
1 CLI invocationあたりprovider POSTを1回に制限できない。
pre-content SSE overloadは3 POST、streaming HTTP 404は2 POSTのまま残る。
「Not Bounded」は要求された上限1を保証できないという意味であり、無限retryを実証した意味ではない。
観測した最大4 POSTを一般的な上限としても使わない。CLIの追加hardeningは終了する。

開始: `main` / `40e4ff81398926f7c5b2442c6f2b26dbe85215d7` / clean。
Real provider通信、実credential、login、Production変更、External Write、push / PR / publishはない。
公開公式Docsのみread-onlyで参照した。CLIの通信先は隔離namespaceのfake endpointだけ。
Independent ReviewのReal Adapter Boundary判定とlone surrogate P3 Closedを変更する新事実はない。
本調査自体の新たなIndependent Reviewは未実施。

## Pin / 実行条件

- binary: `.local/b2-probe-vg70xwcm/run/human/claude-runtime/claude`
- version実測: `2.1.274 (Claude Code)`
- SHA-256: `15e2d05148f801b5774032faad87e624ecd172e9903288bda448b892eb58fa07`
- model: `claude-sonnet-4-6`、effort: `high`。全79 POSTで同じmodelを観測。
- 各起動でsealed memfdのhash検査後にexecveat。binary変更・更新・内部API利用なし。
- `--print --safe-mode --max-turns 1 --tools ""`、空MCP、設定source空、hooks無効、
  session保存なし等の既存境界を維持。主matrixは既存strict JSON text profile。
- credentialは毎回生成するdummyのみ。通常HOME、既存credential、外部Repositoryは読まない。
- 既存user/net/mount namespace、loopbackのみ、host routeなし、Landlock ABI 3 / seccompを維持。
  CLI側の本番profileやSecret / Output Boundaryは変更しない。
- matrixのwall timeoutは40秒。全行がCLIの完了まで観測でき、timeout killによって1 POSTを作った行はない。

## Count semantics / harness

`tests/real_cli_request_matrix.py`が既存`tests/real_cli_probe.py`のnamespace、dummy handoff、
native launcher、fake SSE生成、出力検査を再利用する。設定差し替えはtest harness内のみ。

| 指標 | 定義 |
| --- | --- |
| CLI invocation | exchangeが起動したnative CLIの回数。全行1、CCW再起動なし |
| provider POST | fake endpointが受信した`POST /v1/messages`。stream有無を別途記録 |
| auxiliary request | 上記以外にfake endpointが受信したHTTP request。今回すべて`HEAD /api/hello` |
| 全受信HTTP | provider POST + auxiliary request。matrixでは130 |

HTTP method dispatch前にmethod、path、順序を記録するため、HEAD / GET / その他methodも落とさない。
POSTにはmodel、stream、fixture、応答status、body key名、dummy認証一致等のallowlist情報だけを追加する。
header本文、credential、prompt、raw CLI stdout/stderrを保存しない。
補助HEADにはfakeが404を返した。旧harnessはHEAD handlerがなく501を返しており、`calls`に記録していなかった。
旧報告のrequest数を「全HTTP数」と読むのは不正確であり、今回POSTと補助requestに分離して訂正する。

全行のIPv4 TCP SYN宛先はfake endpointだけ。GET / PUT等の受信は0。
これは受信HTTPの全method記録であり、失敗した全connect syscall、IPv6、全DNS試行の完全記録ではない。
実provider DNS / TLS / OAuth / 課金 / server内部処理は未検証。

## 公式設定の選定

公式Docs参照日: 2026-09-20 JST。更新されるDocsの説明と、固定pin上の実測を区別する。
第三者mirror、binary内の非公開switch、binary patchは候補根拠に使っていない。

| 設定 | 比較した値 / 採用理由 |
| --- | --- |
| `CLAUDE_CODE_MAX_RETRIES` | 0を基本に1をHTTP error対照とする |
| `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK` | 1と未設定を比較 |
| `MAX_STRUCTURED_OUTPUT_RETRIES` | 0 / 1 / 2、textと旧`--json-schema` profileで比較 |
| `FALLBACK_FOR_ALL_PRIMARY_MODELS` | 1を追加。fallback model未設定のoverload停止候補 |
| `CLAUDE_CODE_RETRY_WATCHDOG` | 未設定と明示0。無期限retryを有効にしない |
| `CLAUDE_CODE_DISABLE_TERMINAL_TITLE` | 1を追加し補助model request抑止条件も明示 |

上記は[公式Environment variables](https://code.claude.com/docs/en/env-vars)で公開されている。
`--max-turns 1`はagentic turn制限で、POST capとは記載されていない。
`--fallback-model`は追加model呼出しを有効にするため設定しない。
[公式CLI reference](https://code.claude.com/docs/en/cli-reference)

公式error referenceはnon-streaming fallback抑止の404例外を明記する。
今回の2 POST（streaming→non-streaming）はその説明と一致する。
[公式Error reference](https://code.claude.com/docs/en/errors)

`switchModelsOnFlag`はFable / Opus 5等のcontent-based model切替に関する設定であり、
今回の固定Sonnet 4.6への同model再送を止める候補ではないため総当たりしない。
refusal fixtureはSonnet 4.6の通常`stop_reason=refusal`で、他modelのcategory-based fallbackを実証したものではない。
[公式Model configuration](https://code.claude.com/docs/en/model-config)

## Matrix結果

各セルは**1 CLI invocationで受信したprovider POST数**。全セルにHEADが別途1回ある。

- C / current: retries=0、non-streaming fallback抑止=1、structured retries=0。
- F / fallback-enabled: Cからnon-streaming fallback抑止だけを未設定にする。
- O / official-stop: Cに`FALLBACK_FOR_ALL_PRIMARY_MODELS=1`、watchdog=0、title抑止=1を追加。

| fake応答経路 | C | F | O |
| --- | ---: | ---: | ---: |
| 正常JSON text成功 | 1 | 1 | 1 |
| pre-content SSE overload（message_start前） | **3** | **4** | **3** |
| message_start直後のoverload（contentなし） | **3** | **4** | **3** |
| content delta後のSSE overload | 1 | **2** | 1 |
| content delta後のTCP切断 / mid-stream failure | 1 | **2** | 1 |
| HTTP 404 | **2** | **2** | **2** |
| HTTP 429 | 1 | 1 | 1 |
| HTTP 529 | 1 | 1 | 1 |
| HTTP 500 | 1 | 1 | 1 |
| refusal | 1 | 1 | 1 |
| 最初だけpre-content overload、以後成功 | **2** | **2** | **2** |
| 最初だけHTTP 404、以後成功 | **2** | **2** | **2** |
| 合計（各profile 12 invocation） | 19 | 23 | 19 |

stream error fixtureはstreaming requestにだけerrorを返し、non-streaming fallbackには正常JSONを返す。
そのためFのoverload 4 POST / mid-stream 2 POSTは最終mappingがacceptedになる。
HTTP error fixtureはstream有無によらず同じerror。recover行だけ2回目以降を成功にする。
mid-stream fixtureは最初の20文字をdeltaで出した後、block completion前にerrorまたは切断する。
遅い切断・全block完了後などあらゆるstream位置の上限を主張しない。

| 追加比較 | invocation | POST | HEAD | 結果 |
| --- | ---: | ---: | ---: | --- |
| `MAX_RETRIES=1`、HTTP 429 / 529 / 500 | 各1、計3 | 各2、計6 | 各1、計3 | 全件CCW拒否。設定0では各1 POST |
| text profile、structured retry 0 / 1 / 2、正常/不正JSON schema | 計6 | 6 | 6 | 正常だけ受理、不正はCCW拒否。修復POSTなし |
| 旧structured profile、structured retry 0 / 1 / 2、正常/不正tool input | 計6 | 6 | 6 | 全件拒否、structured_outputなし |

旧structured profileでは0 / 1が`error_max_structured_output_retries`、2が`error_max_turns`。
max-turns=1と既存tool制約を維持した比較であり、structured retry設定単独の一般的な上限証明ではない。
text profileには`--json-schema`がないので、この変数を変えてもprovider retry制御にはならない。
正常なschema応答まで拒否する設定を「正常経路も満たす解決」としない。

| 実行群 | CLI invocation | provider POST | auxiliary HEAD | その他method |
| --- | ---: | ---: | ---: | ---: |
| 最終matrix | **51** | **79** | **51** | 0 |
| 既存20ケースの回帰 | 20 | 22 | 20 | 0 |
| fake endpoint群の合計 | **71** | **101** | **71** | 0 |
| version / help / empty-auth診断（全socket禁止） | 4 | 0 | 0 | 0 |

従って今回のpin実行は合計75 invocation（fake 71 + 非推論診断4）。
119 testsや他Smokeの合成fake executable起動はpin CLI invocationに含めない。
sandboxによる最初の2回の拒否はCLI exec前であり、この回数に含めない。

## 抑止できたもの / 残ったもの

- HTTP 429 / 529 / 500の通常retryはMAX_RETRIES=0で1 POST。1にすると2 POSTになる対照あり。
- non-streaming fallback抑止は、今回のmid-stream failureで2→1 POST、early overloadで4→3 POST。
- early overloadのstreaming再送2回は残る。message_startを受け取ってもcontent前なら同じ結果。
- streaming 404は抑止設定にかかわらずstream=true→falseの2 POST。同じmodelへのtransport fallback。
- FALLBACK_FOR_ALL_PRIMARY_MODELSを含むO profileでも上記は残る。
- refusalは今回1 POST、CCWで拒否。正常成功は1 POST、受理。
- HEAD /api/helloはすべてのprofileで1回。provider POSTとは区別し、課金有無は推定しない。

recoverケースでは`num_turns=1`、`is_error=false`、`subtype=success`で、CCW mappingもaccepted。
正常1 POSTとoverload retry後成功2 POSTは同じenvelope key集合になった。
404後成功はstreaming timingの3 keyが欠測したが、retry回数を示すfieldにはならない。
durationやusageからの推測は確実なretry検知ではない。post-hoc拒否も送出済みPOSTを取り消せない。
CCWは各行1 process / 1 exchangeであり、追加POSTはそのCLI内部で起きている。

## Contract semantics / 最小修正

| 現在のfield | 保証する範囲 |
| --- | --- |
| `max_attempts: 1` | CCW側の単回実行。provider POST数の保証ではない |
| `automatic_retry: false` | CCWがexchange / runtimeを自動で再実行しない |
| `fallback: false` | CCWが別provider / model / APIへ切り替えない |
| `api_retries: 0` | CLIへの設定要求。wire上の追加POSTが0という保証ではない |
| `structured_retries: 0` | structured-output設定要求。transport retryとは別 |
| `provider_requests_hard_cap: null` | provider request数のhard capは未保証 |

誤解防止の説明修正は必要。今回READMEとこのpacketに明記し、runtime contractのbytesやversionは変更しない。
将来HumanがCLI継続を選ぶなら、contractに例えば`retry_policy_scope: ccw_invocation`と
`cli_internal_provider_requests: not_bounded_by_ccw`を明示する最小schema改訂を検討する。
既存permitへの暗黙適用はせず、reviewと新contractを必要とする。今回は提案に留める。
`max_attempts=1`をprovider POST capと読み替えてblockerを消してはならない。

## Findings / Gate

| Finding | Status / 追加証拠 |
| --- | --- |
| F-REAL-01 | **Open / Real Gate blocker**。公開設定を追加してもearly SSE=3、404=2 |
| F-REAL-01追加証拠 | retry後成功はCCW accepted。外側の単回実行・num_turnsでは識別できない |
| OBS-HTTP-01（新規計測Finding） | 旧harnessのHEAD記録漏れ。全method記録へ修正し、HEAD /api/helloを分離集計 |
| P3 lone surrogate | Closed維持。既存unit / fake endpoint回帰PASS |

Real Claude Gate: **NO-GO**。新しいSecurity Architecture、egress mediator、TLS MITM、
token extraction、内部API依存、binary patch、追加provider権限は導入しない。

## Tests / 再現 / Evidence

```text
python3 -B tests/real_cli_request_matrix.py
python3 -B -m unittest discover -s tests -v
python3 -B tests/real_cli_probe.py
python3 -B tests/b4_probe.py --diagnostics
```

119 tests PASS（50.457秒）。READMEのisolation check、smoke、b1_smoke、secret_handoff、
secret_boundary、secret_abc、secret_consumer_window、runtime_service_smoke、real_cli_probeはすべてPASS。
P3・拒否後の消費維持・Real gate拒否・Secret/Output Boundaryのregressionを維持。
probeはNOT_BOUNDEDを正しい調査結果として正常終了する。テストPASSはReal GOを意味しない。

最初のmatrixと既存real_cli_probeは実行sandboxにより`socket.if_nameindex`がEPERMで停止。
それぞれ必要なlocal実行承認経路で1回再実行し完了。namespace / Landlock / seccompを弱めていない。
自律的なコード修正→失敗再実行のループはない。

| Evidence | 保存先（すべてignored `.local/`） |
| --- | --- |
| 51行・全受信HTTP順序・設定・mapping・通信/漏洩検査 | `.local/real-request-matrix-h1h6_acd/summary.json` |
| 既存20行の回帰 | `.local/real-cli-hqtnhqvl/summary.json` |
| pin version / help / 空auth | `.local/b4-cli-fj4z2ujl/summary.json` |
| 通常Smoke | `.local/smoke-7dp93vhi/summary.json` |
| B1 Smoke | `.local/b1-smoke-kwu5wwb8/` |
| Secret probes | `.local/secret-handoff-0grd9l0g/`, `.local/secret-boundary-v_emrmil/`, `.local/secret-abc-cjc4zslq/`, `.local/secret-consumer-window-7kpeysuu/` |
| Runtime Smoke | `.local/runtime-service-smoke-u5qmk8ki/` |

SHA-256:

```text
matrix summary: 4679b64d764369504027a26a4782c858478e58f3c9a5deb3141aef63ad5aa4d0
20-case summary: 73d3a4ba00563033a29bb24d3799567e474bb273e331b3af1053e18e588424bc
diagnostics summary: 841b7cb5dd9e508b992941a44a7aecb64fa3e3b9e7d84f07f5f3dba135578364
real_cli_probe.py: 8a995e6b7c263a4d41eaca13ad58ea0af1abee2bed0b676c3f61d8c232f25c76
real_cli_request_matrix.py: 34237555065b6ef0fcc55cacf4468392af6beabb34f087c135b40b90d93ae82b
```

Git変更対象はREADME、既存probe、新matrix、新報告書の4ファイルのみ。
`src/`と既存Gate、他Repository、Productionは未変更。生成Evidenceはcommit対象外。
まとまりのdiff確認後にこの4ファイルをlocal commitする。push / PR / publishはしない。

## Human Decision Packet

**判断すること: provider POST=1を必須条件として維持するか、CLI内部の追加requestを明示受容するか。**
今回どちらも選択・承認済みとは扱わず、Real利用は停止したまま終了する。

| 比較軸 | 1. Claude Code CLI継続 + Risk Acceptance | 2. 別の公式Anthropic interfaceを再評価 |
| --- | --- | --- |
| request制御 | 1 invocation内のprovider request数はCCWからboundedできないと明示受容する。実測1〜4をhard capにしない | 第一候補はMessages API + 公式Client SDK。明示的な1 createとretry=0を候補に、同matrixで再実証する |
| credential | 現構想はHuman-held setup-token→private socket→CLI OAuth env。dummy以外は未検証 | Console API key等の公式認証方式を別途選定。CLI subscription tokenの流用可否は仮定せず、抽出しない |
| subscription / API課金 | 公式Docs上setup-tokenはsubscription向け。実account、追加使用OFF、初回請求挙動は未確認 | API / Consoleはsubscriptionとは別。API課金・予算・利用可能modelを確認する必要がある |
| Architecture | native launcher / JSON mapperを維持しやすい。Human契約はrequest単位保証を削除し、riskとunknown countを明示 | trusted serviceのtransport / launcher / mapper / contract / dependency pinの再評価が必要。Workerへcredential/tool権限を増やす理由にはならない |
| 継続して残るRisk | 成功でも内部retry不明、資料の複数送出・利用枠消費。既存TCP / provenance / 認証等のGateも別途残る | SDK自体のretry、redirect、stream recovery、tool runner等を実測しない限りPOST=1は未証明。現構成との隔離互換性も未確認 |
| 次の承認対象 | 明示的なRisk Acceptanceの文面と変更後contract、残Gateのreview。F-REAL-01を技術的Closedにせずrisk処理を別記 | まずoffline feasibility評価の範囲・credential方針・課金前提。SDK install / 実credential / Real通信は別途判断 |

公式Python Client SDKは`max_retries=0`を公開し、通常は一部errorを自動retryする。
このため比較候補になるが、本Repositoryで導入・実測したわけではなく、優位性やGate通過を確定しない。
[公式Python SDK](https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python)

setup-tokenとsubscriptionの対応は[Claude Code Authentication](https://code.claude.com/docs/en/authentication)、
API / Consoleとsubscriptionの分離は[公式課金説明](https://support.claude.com/en/articles/9876003-i-have-a-paid-claude-subscription-pro-max-team-or-enterprise-plans-why-do-i-have-to-pay-separately-to-use-the-claude-api-and-console)を参照。
実accountの契約や利用条件はこのDocs確認で検証済みにはならない。

Agent SDKはClaude Codeのagent loopを使うため、包装を替えるだけでrequest数の問題が解決するとは扱わない。
[公式Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
別の公式`ant` CLIも存在するが今回は未検証で、代替の安全性を宣言しない。
[公式interface一覧](https://platform.claude.com/docs/en/cli-sdks-libraries/overview)

推奨: **POST=1が必須なら選択肢2のoffline再評価**。subscription利用を優先して選択肢1を選ぶ場合は、
追加requestのRiskをHumanが明示的に受容し、他の残Gateも確認する。選択だけでReal GOにはしない。
