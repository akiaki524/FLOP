# Messages API Real Gate Finalization — Offline

開始: `main` / `caf9e5513f2265ebf4a2718b95ac20b1992d45a2` / clean。
今回の対象はpermit接続、Real候補profileとmapper、Dependency P3の記述整合のみ。
実provider、実key、API課金、Console、Production、外部write、pushは使用していない。
前回Independent Reviewの判定を変更したり、今回の差分が独立review済みと扱ったりしない。

## PermitとReal入口

`runtime_service.py --api-real-credential-fd`はCLI用`--credential-fd`と独立した入口。
`validate_policy → require_real_permit → require_real_release`をcredentialのrecvと
`consumed.json`作成より前に通す。keyが存在するだけでは進まない。
Real profileをOfflineでrehearsalする`--api-offline-credential-fd`も同じpermit検査を使う。
従来fake profileだけは従来どおりのOffline admissionを維持する。

permit形式は既存CLI authorizationと同じ
`version / contract_sha256 / expires_at / human_confirmed`。
private owned 0700 root内の0600 regular file、link数1を要求する。
canonical policy全体のdigestにより次をまとめてbindingする。

- task本文・locatorのdigest、request全体（request側timeoutを含む）のdigest
- root、attempt nonce、attempt最大1、CCW invocation最大1
- API rail、Offline/Realの別、endpoint、profile、model、SDKとCCW code identity
- wall / HTTP timeout、input bytes、output tokens、raw response / final Result bytes
- provider POST 0〜1、補助request 0、SDK/transport retry 0、redirect / automatic retry / fallback false
- stream false、toolsなし、credential / network / output / cost / stopの条件

期限はpermitそのものの検査対象で、発行関数は300秒、検査は現在時刻より後かつ3600秒以内。
permit自体のdigestも消費markerへ保存する。期限や確認flagの差し替えは消費後の照合で拒否。
Service admission、credential受領後のruntime起動直前、子のready直前に検査する。
子は受け取ったrequest digestも照合し、HTTP transportへ渡す直前にも期限を検査する。
有効期限は新規送信の開始期限であり、既に開始したPOSTの取消ではない。

既存のinstance lockと排他的な`save_new(consumed.json)`を再利用。
admission拒否ではcredential recv 0 / runtime 0 / POST 0で未消費。
admission後はcredential異常、期限切れ、起動失敗、接続失敗、response拒否も消費済み。
再起動・再利用・失敗後の自動retry / refundは不可。新root作成を再送回避に使わない。

Human発行関数`authorize`は既存`launch_guard`と完全contract確認を使う。
Human terminal用入口は`python3 -B -m ccw.runtime_api permit ROOT --confirm POLICY_SHA256`
（`PYTHONPATH=src`で実行）。Worker / Activityには公開しない。
今回はprobeが**Offline用の合成permitのみ**を作成し、Human発行関数やReal Gateを迂回しない。
同一UIDのtrusted Human / supervisorを信頼する既存モデルであり、permitは署名付き本人認証ではない。

**Real releaseはNO-GOのまま。** `LIVE_BLOCKERS`はService・launcher・SDK入口に残る。
有効permitでもReal issuer / Serviceは拒否する。環境値やCLI flagで解除しない。
今回のcontractでは`real_spending_authorized=false`を維持しており、Real実行承認を作成していない。
将来のrelease判断では承認済みcost/network条件をcontractへ反映し、code digestを固定し直す必要がある。
Offline permitはrail/endpoint/code digestが異なるReal contractには転用できない。

## Real候補profile

| 条件 | offline-fixture-v1 | real-candidate-v1 |
| --- | --- | --- |
| model | claude-sonnet-4-6 | claude-sonnet-4-6 |
| runtime wall timeout | 5秒 | 60秒 |
| HTTP timeout | 0.3秒 | 45秒 |
| request body budget（prompt含むcanonical / wire） | 16,384 bytes | 16,384 bytes |
| max_tokens | 128 | 4,096 |
| raw response budget | 65,536 bytes | 262,144 bytes |
| canonical final Result | 32,768 bytes | 32,768 bytes |
| requests | non-streaming POST最大1 | non-streaming POST最大1 |

候補は保存済みの短い公開技術資料1件のreview向け。HTTP待機45秒に対し親wall 60秒で
SDK初期化・検証の余裕を持つ。4096 tokensはJSON reviewと引用のための候補値であり、
全資料で成功する保証ではない。byte budgetはtoken budgetやドル建てspend capの代わりにならない。
requesterの`ReviewRequest.timeout_ms`は従来schemaのまま、trusted profileのwall timeoutを上書きしない。
modelは既存pin済み候補を継続し、現在のアカウントでの利用可否や将来のbackend不変性は未確認。

## Mapper compatibility

証拠は既存lockの公式SDK `anthropic==1.7.0`の以下のローカルtypes。
外部schema取得・provider接続・SDK更新はしていない。

- `.local/messages-sdk-20260920/venv/lib/python3.12/site-packages/anthropic/types/message.py`
- 同`usage.py`、`text_block.py`、`cache_creation.py`
- wheel provenance / hashは`src/ccw/messages_sdk.lock.json`と前回評価を参照。

SDKが返すobjectの検証済み扱いは継続して禁止。bounded raw bytesをCCW側で再decodeし、
duplicate key / invalid Unicode、Secretの既知表現、必須field、型、model、完了理由を検査する。
usageのinput/output counterだけをstrictな整数として採用し、output上限と照合。
cache_creation / cache_read、service_tier、inference_geo、output_tokens_detailsなどの付加metadata、
text citationsや将来の無関係metadataはSecret scanの対象とした上で結果へ渡さず無視する。
無視したmetadataのschema全体・課金値の正しさは保証しない。

`container` / refusal detailsはnullのみ、server tool usageはnullまたは全counterが整数0のみ。
contentはtext block 1個、stop_reasonはend_turnのみ。tool / thinking / refusal / 未完了は拒否。
text内部のCCW report schemaと引用照合、final Resultのcanonical budgetは従来どおり厳格。
Real用provider `anthropic-messages-real-v1`とruntime kind `real-messages-api`を追加し、
CLI結果やOffline結果との混同を拒否。表示はuntrusted / display-only / external actions false。
通常のcache metadata付きresponseをfake endpointから受理したが、実推論のJSON準拠は未確認。

## Dependency P3

実装は変更せず、[前回checkpoint](checkpoint-messages-api-20260920.md)とlauncher docstringを修正。
保証はlockの15依存のversion、固定RECORD digest、RECORD記載のhash付きfileのsize/hash/path、
検出distribution名の集合、.pth不在、isolated/no-site/no-bytecode bootstrap。
**RECORD外のsite-packages file、pipのfile内容、検査後の置換、全import closureは保証外。**
wheel hashはprovenance記録であり起動時に配布wheelを再検査しない。
OS / interpreter / trusted supervisorと未隔離同一UIDの変更権限は既存信頼基盤に残る。
同一UIDによるvenv改変攻撃を新たに防御したとは主張しない。

## Offline verification

- 最終差分の全unit suite: 129件PASS（API unit 10件を含む）。
- `python3 -B tests/messages_api_probe.py --rail`: PASS_OFFLINE_API_RAIL。
  既存32 + candidate 16 = 48 runtime起動 / 46 POST / 補助request 0。
  接続拒否2ケースはPOST 0、それ以外は各1。48件すべて同instance再利用を拒否、追加POST 0。
- candidateのcache metadata通常response / 通常responseを受理。
  malformed / raw oversized / tool / Secret（plain・escaped・base64）、全5 redirect、
  429、disconnectを拒否し、retry / redirect追従 / fallbackなし。
- Gate 12件（既存8 + permitなし・不一致・期限切れ・nonce変更）すべてruntime 0 / POST 0。
  credential異常のみ既存どおり消費済み、permit拒否4件は未消費。
- Real Serviceのpermit拒否をunit testで直接検査し、credential recv / exchangeとも0。
  正しいReal permitでもrelease拒否。消費後期限切れ、transport直前期限切れも追加POSTなし。
- すべてdummy credential。wire bodyへのkey混入なし、Evidenceへのkey/既知表現漏れなし、
  runtime workspaceへの永続化なし、環境proxyへのrequest 0。
- READMEのisolation、smoke、B1、Secret Handoff / boundary / ABC / consumer window、
  Runtime Service smokeがPASS。CLI fake probeもexit 0で既存F-REAL-01の3 POSTを再現。
  これはCLIの既知blocker維持の確認であり、MessagesのPOST上限と混同しない。
- 初回sandbox実行では既存匿名socket / namespace操作がEPERM。
  承認されたローカル実行環境で再実行しPASS。Landlock / seccompは解除していない。

今回のMessages evidence: `.local/messages-api-sun5si7p/summary.json`（ignored、commit対象外）。
既存smoke evidence: `.local/smoke-skytxxrb/summary.json`、`.local/b1-smoke-d34wboq0/`、
`.local/secret-handoff-chnk0d4o/`、`.local/secret-boundary-i0gwnf82/`、`.local/secret-abc-vzkgsilf/`、
`.local/secret-consumer-window-ogmd0r6v/`、`.local/runtime-service-smoke-x1k9y9db/`、`.local/real-cli-a925j1l8/`。

## Findings / residual risksと次のHuman判断

今回のOffline検証で新たなReal Gate blockerは検出していない。
POST最大1はreview済みpinned code内のtransport guardの保証で、改変SDKに対するkernelのHTTP上限ではない。
same-UID完全性、一般TCPとvendor-only egressの差、Secret scanの未知符号化・部分漏れ、
Pythonメモリzeroization不可、既存Handoffの異常signal残Riskは維持。
Realのmodel / 通常response / DNS / TLS / quota / billingは未検証。CLI Gateは引き続きNO-GO。

Humanが次にYES / NOを判断する対象は次の**単一attempt条件**。

1. Humanが選んだ保存済み公開技術資料1件を固定し、locator・本文・task/request digestを確認する。
   prompt含む16,384 bytes以内。超過なら事前停止し、自動切捨てや別modelへ切り替えない。
2. Messages API、claude-sonnet-4-6、wall 60秒 / HTTP 45秒、max_tokens 4096、raw 256KiB、Result 32KiB。
   `https://api.anthropic.com/v1/messages`へPOST 0〜1、attempt 1、retry / redirect / fallback / toolsなし。
3. 別API課金、利用可能model、現在価格をHumanが確認し、許容するドル建て上限とcredentialのscope / expiry、
   network capabilityを確定する。**金額は今回は未確定。resource budgetを課金上限と見なさない。**
4. release差分と実cost/network条件をreviewして固定した後、Human terminalで当該contractだけの
   有効期限300秒permitを発行する。実keyは専用private channelだけから、Worker / Activityには渡さない。
5. 結果はHuman Previewのみ。失敗・timeout・不明でも消費済みで停止、再送しない。
   STOPは開始前、進行中はservice/process group停止後にusage/課金をHumanが照合する。

YESは上の条件を固定する次のHuman Gateへ進む判断。今回のOffline結果だけでReal実行は許可されない。
条件3とrelease承認が揃うまでは実行NO-GO。今回の作業はReal APIを実行せず終了する。
