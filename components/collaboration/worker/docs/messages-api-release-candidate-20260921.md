# Messages API 初回Pilot release candidate

**CANDIDATE ONLY / NOT REAL GO**。開始は `main`、
`0c1dd36f184c5d214d34c229212156ca317d6b0a`、worktree clean。
2026-09-22 JSTの最終Independent Reviewで `66fc9e8..3694f83` を確認し、
138 tests、README Smoke、API Rail、Offline Real Network PathはいずれもPASS。
Boundary regressionはなく、P0 / P1 / P2 / P3 Findingはなし。**Engineering complete**として扱う。
`3694f83` 以降のmain差分はRepository運用文書のみで、Messages API runtime sourceは変更されていない。
実API key、実Anthropic通信、Console funding、credit購入、課金、Real permit発行、
Actual Real attempt、Production/VPS変更は未実施。

## 固定した候補

`messages_pilot.py`のrequest／canonical payload／SDK HTTP bodyのSHA-256を
release predicateで要求する。別資料、別prompt、別profileを一般的に許可するreleaseではない。
`LIVE_BLOCKERS`の無条件拒否を、固定identityとHuman条件が一致する候補判定へ置き換えた。
候補判定だけでは実行できず、別途Human terminalでの最終attestationとpermitが必要。

| 項目 | 条件 |
| --- | --- |
| 運用 | local Human-supervised one-shotのみ |
| 資料 | 公開FLOP Yellow Paper v0.5.0 §6.2抜粋1件＋既存fixed prompt |
| model / endpoint | `claude-sonnet-4-6` / `https://api.anthropic.com/v1/messages` |
| 回数 | attempt 1、provider-facing POST 0〜1、補助HTTP request 0 |
| transport | retry / redirect / environment proxy / fallbackなし、single-use |
| token / bytes | max_tokens 4096、input 16,384 / raw 262,144 / Result 32,768 bytes |
| timeout | wall 60秒 / HTTP 45秒。ReviewRequestの既存timeout値もdigestで固定 |
| 結果 | Human Previewのみ、responseはuntrusted data、external actionなし |
| 失敗 | consumed後は自動retry・attempt返却なし。不明は未送信の証明ではない |
| key | single non-Default WorkspaceにscopeされたPersonal API key |
| Human条件 | key expiry 3時間、Workspace Spend Limit USD 1のConsole確認 |
| 終了 | 成功・失敗・不明を問わずHumanがkeyをDisableまたはDelete |
| network | provider-only egress保証なし。一般TCP残Risk受容はこのlocal 1 attemptだけ |
| 対象外 | VPS / 24H / Autopilotへ承認を拡張しない |

## コードによる強制とHuman attestation

コードはrequest／payload／wire body、model／endpoint／request shape、全policy、code manifest、
SDK依存pin、budget、TLS証明書／hostname、Landlock/seccomp、single-use transportを照合する。
Real permit v2はcanonical policy digest・期限・Human確認記録・実行network namespaceへbindする。
namespaceのbindingは、隔離probeで合成したpermitを通常hostへ持ち出して使うことも拒否する。
同UIDの信頼されたHuman/supervisorに対する偽造防止や本人認証ではない。

Human確認記録はpolicy digest、上表の条件、Consoleでのscope／expiry／USD 1確認、
実行後失効の約束、最終release差分のIndependent Review PASS、local環境と停止／Recovery準備、
**このexact one-shotを今許可する意思**を含む。欠落・追加・false・型違い・条件変更は拒否する。
issuerもrequestを読み、現在の全contractとreleaseを再検証する。
issuerの期限は従来どおり5分、照合時の上限は1時間。**keyの3時間expiryとは別物**。

コードはAnthropic Consoleに問い合わせない。Workspace scope、Personal key種別、3時間expiry、
USD 1設定、実行後のkey失効、local Human監督、reviewの実施を自動観測しない。
`human_conditions_verified_by_code=false`と`limit_verified_by_code=false`をpolicyへ明記した。
Humanによる虚偽のattestationをコードで見破る保証はない。post-run失効は将来の運用義務であり、
実行前に完了を検証したという意味ではない。token／byte制限をUSD 1の課金capとも表現しない。
実usage／確定課金は未取得でnullのまま。

Serviceの **policy → permit → release → consumed永続化 → credential → runtime** を維持。
launcherのpermit／release／consumed照合、ready後のSecret Handoff、transport直前expiry検査、
Output Boundary、credentialをActivityへ返さない設計を変更していない。
STOP、期限切れ、旧v1 Real permit、異なるnamespace／policy／code／入力／条件、
不足attestation、隔離不能、依存不一致ではfail closed。
TLS異常・拒否応答・Output Boundary拒否等のconsumed後失敗でも復活させない。

## Canonical sourceと再現可能なrelease artifact

最新Decision Packetはメモリ上の再構成であり、保存済みproposalは存在確認できていない。
古い `.local/runtime-service-smoke-kuo5r3xs/human-preview.txt` はOffline fixture結果であり、
Realのcanonical source／prompt／payloadへ昇格させていない。

今回の再構成はtracked `tests/runtime_service_smoke.py:MATERIAL`の本文／locator、
`runtime_api.message_arguments()`のfixed instruction、REAL_PROFILEから行った。
この結果をHuman提示の最新Decision Packet digestと照合し、以下すべて一致した。
資料内容のアンカーは `§6.2 Agent autonomy — operating without per-action consensus`。
外部取得・資料の真正性検証は実施していない。

| 対象 | bytes | SHA-256 |
| --- | ---: | --- |
| source UTF-8 | 850 | `e6e1a0fe0c44a3235b473c1e5d9501151867b0d1a88f6900bf05602a0e7bca51` |
| ReviewRequest canonical | 1,073 | `6b70c47a0ba818b80471260efa9eee6931b59baad73be803549d87277775b35e` |
| task canonical | — | `60a64e97df7114e833cd4aeec4a321c4488733350ddb61224259d1865371207c` |
| fixed instruction UTF-8 | 559 | `2dbeeac5f4e9aacd5493493895eb8f2e7d3d9ecb54d685e93ecb2ec636347cf2` |
| Messages arguments canonical | 1,815 | `25f497ec65a1a77371aedf596b587ef62c27f7b0c8a749734f132e025a2b3b8c` |
| SDK HTTP body | 1,815 | `86f954f206096d56cdbab8f9d9b9c471ef91c7abad1ba28b1d9ec3a69c1febd7` |

user contentは1,674 bytes。canonical JSONとSDK wire bodyはkey順序が異なるためdigestが異なる。
意味的なrequest shape検査に加え、Real transport直前にHTTP body bytesのdigestも照合する。

再現コマンド: `python3 -B tests/messages_pilot_packet.py`。
既存のpin済みSDK環境を検証し、production SDK request関数と通信しないcapture transportで
HTTP bodyを再生成する。dummyのみ、外部通信0、permitなし。
新しいroot／nonceを使うためpolicy digestは実行ごとに変わる。

最終確認用artifact root:
`.local/messages-pilot-candidate-rt73mxwl/`

`source.txt`、`fixed-prompt.txt`、`request.json`、`payload.json`、`sdk-http-body.json`、
`user-content.txt`、`policy.json`、`summary.json`を保存。permit／consumedは存在しない。
生成物はignored `.local/` に保持し、commitしない。tracked generatorと入力から再現できる。

- このrootの最終policy SHA-256: `48a6f18be92b14cc96b2ea695591f9034297c866acc392890a35ade988d4fba6`
- 最終code identity（canonical code manifestのSHA-256）: `a3ffe4bda89cd2ec9f46ac3363fcdd28756252324eca8353b5e545e9d3f4e5f6`

manifestには新しい`messages_pilot.py`も含む。OS／Python interpreter／全import closureまでの
完全性証明ではない。commit IDとは別のidentityであり、docs／testsはmanifest外。
旧proposal／途中生成packet／旧code manifest digestは最終permitへ流用しない。
今後code、root、nonce、条件を変えた場合は最終policyを再計算し、Humanが改めて照合する。

## 検証

全検証はオフラインであり、実provider推論・課金・Console条件の確認を意味しない。
network／Rail／CLI probeの模擬通信先は隔離namespace内loopbackのみ。

- `python3 -B -m unittest discover -s tests -v`: **137 tests PASS / 49.169秒**。
  新規7 testsはattestationの欠落／余分なfield／false／型違い／USD 1変更／scope／expiry／
  review／GO／旧v1／namespace変更、input identity、policy／code／budgetの改変を拒否する。
  Serviceが不正attestationをcredential取得・consumed・runtimeより前に拒否することも確認。
  意味的には同じJSONでもSDK wire bytesが固定値と異なればtransport呼出前に拒否する。
- `python3 -B src/ccw/isolation.py`: **Landlock ABI 3、confinement available**。
- READMEの通常Smoke、B1、Secret Handoff、Boundary、ABC、consumer window、Runtime Service:
  **PASS**。CLI fake probeはexit 0、`PASS_OFFLINE_WITH_LIVE_BLOCKER`、既知SSE 3 POSTを維持。
- `python3 -B tests/messages_api_probe.py --rail`: **PASS_OFFLINE_API_RAIL**。
  48 request cases、runtime 48、POST 46、auxiliary 0、再利用拒否48、Gate拒否12、proxy 0。
  setup起動1を含む観測runtime総数49。Evidence: `.local/messages-api-vi7tx02x/summary.json`。
- `python3 -B tests/messages_api_network_probe.py`: **PASS_OFFLINE_REAL_NETWORK_PATH**。
  release predicateのpatchなし。既存Service main、launcher、SDK、Output Boundaryを通す。
  Evidence: `.local/messages-api-network-88nzv3x1/summary.json`。
- `python3 -B tests/messages_pilot_packet.py`: **CANDIDATE_ONLY_NOT_REAL_GO**。
  資料／canonical request／canonical payload／SDK HTTP bodyの全identityがHuman提示値と一致。
- `git diff --check`: **PASS**。

| networkケース | runtime | POST | 結果 |
| --- | ---: | ---: | --- |
| Console条件未確認 | 0 | 0 | DNSも0、未消費、拒否 |
| untrusted CA | 1 | 0 | TLS拒否、消費済み |
| hostname不一致 | 1 | 0 | TLS拒否、消費済み |
| 307 redirect | 1 | 1 | 追従なし、拒否、消費済み |
| credential入り応答 | 1 | 1 | Output Boundary拒否、消費済み |
| 正常TLS | 1 | 1 | 固定FLOP payloadからuntrusted Human Previewまで成功 |

消費済み5件はいずれも再利用拒否、追加POSTなし、Evidence不変。
受信した3件のSDK bodyはすべてDecision Packetのwire digestと一致、auxiliary 0。
合成permitを使うprobeはHuman issuerを呼ばない。namespace外への流用は拒否される。
SDK serialization probeのtransportはin-memory doubleで、networkもpermitも使わない。

最初のAPI unit実行は開発sandboxによる匿名socket送信EPERMで7件失敗。
ランタイムの承認経路でローカル再実行してPASS、最終全suiteも同環境でPASS。
CCWのLandlock/seccompを緩和していない。

既存SmokeのEvidence（ignored）:
`.local/smoke-4xnff3ha/summary.json`、`.local/b1-smoke-oppbcos1/`、
`.local/secret-handoff-5iwf188u/`、`.local/secret-boundary-i5ca90kr/`、
`.local/secret-abc-16nm1oqc/`、`.local/secret-consumer-window-6_g38xrt/`、
`.local/runtime-service-smoke-vsxoqgrw/`、`.local/real-cli-dwj46zye/summary.json`。

## FindingsとIndependent Review

2026-09-22 JSTの最終Independent Reviewは **PASS**。
対象は `66fc9e8..3694f83`、P0 / P1 / P2 / P3 Findingはなし。
138 tests、README Smoke、API Rail、Offline Real Network PathのPASSとBoundary regressionなしを確認し、
**Engineering complete / Final Human GOへ進める** と判断した。

既存CLI F-REAL-01（SSE error時に3 POST）はOpen、Claude Code CLI Real RailはNO-GOを維持する。
API RailのPASSによってCLI FindingをClosed扱いしない。
既存の一般TCP、同UID trusted supervisor、RECORD外file／検査後置換、未知Secret符号化、
異常signal時のcleanup等の残Riskも、本Reviewで解消したとは扱わない。

`3694f83` 以降のmain変更はRepository運用文書であり、review済みMessages API runtime source、
fixed request / payload / SDK HTTP body、permit / release / credential / Output Boundary contractを変更していない。
そのため、理由なくfixed Pilot Artifactを再生成しない。

## Real直前のHuman作業と最終Gate

1. **Agent API用Account / Console / Billing Boundaryを先に確定する。**
   既存Accountと分離するか、Organization / Billingをどう分けるかをHumanが決め、
   credit購入前にAnthropic公式Current条件とConsole表示を確認する。
2. Account / Organization確定後、必要最小限のAPI creditsだけを購入する。
   購入前に金額、財布の範囲、最大損失、自動追加課金の有無をHumanが確認する。
3. CCW Pilot専用のsingle non-Default Workspaceを作り、Spend LimitがUSD 1であることをConsoleで確認する。
   **USD 1を確認できなければReal不可**。勝手に上限を引き上げない。
4. Workspace-scoped Personal API keyを作成し、3時間expiryを確認する。
   key値はChatGPT / Coding Agent / GitHub / Technocore / Chatへ貼らず、
   実行後にDisable/Deleteできる手順を準備する。
5. account/model/billingの利用条件と実行hostのTCP DNS/NSS/CA互換性を確認する。
   環境不適合時にproxy／fallback／UDP等で自動回避しない。
6. local Human-supervised 1 attemptに限る一般TCP残Risk、停止、失敗／不明時の照合、
   key失効をHumanが受け入れる。別root作成による不明attemptの再送は許可しない。
7. 最終artifactと現在のcode／dependencyを再照合し、Human自身が最終policyに対する
   明示的attestationを作成して短命の単回permitを発行する。その後に既存private credential
   handoffをHumanが操作する。**ここまではまだ未実施。**
8. 結果はuntrusted Human Previewとして扱い、external actionへ接続しない。
   成功・失敗・不明のどれでもkeyをDisable/Deleteし、自動retryしない。

以上の最終Gateが残る。Task完了はReal GOではない。

## P3 follow-up: Human spending authorization Evidence

開始HEAD: `66fc9e8da6fe78cfc8855d3268db0deec1eef971`（main、clean）。
Independent Reviewで指摘された、Real admission後も
`real_spending_authorized=false`を固定記録するP3だけを修正した。

既存のexact-policy permit／Human attestation／release検査を通過したAPI Real経路では
`real_spending_authorized=true`を記録する。これはadmission時点のHuman spending承認の
記録であり、Console設定をコードが確認したという意味でも、送信・利用・課金の証明でもない。
offline経路はfalse。消費後にtimeoutやOutput拒否となっても承認の記録はtrueを維持し、
`provider_usage`／`provider_charge`は引き続きnull（未確認）。課金済みの可能性は否定しない。
permit欠落・不一致・期限切れ・attestation拒否は既存Gateで拒否し、消費もcredential受領も
承認済みEvidenceの生成も行わない。承認経路・条件・消費順序には変更がない。

runtime sourceの変更に伴い、permitを発行せずofflineでartifactを再生成した。
上記初版のpolicy／code identityは履歴であり、更新後のpermitへ流用しない。
新artifact: `.local/messages-pilot-candidate-f8pvhvjl/`。

- code identity: `09a27b672075d87a274428d099950c460cb6d98e37bd7362642ca321ac573272`
- policy: `b8bbb7825d659a42bb68319f5d7e7ffde080a3bb79b201939e9682eda3c07d61`
- canonical payload（不変）: `25f497ec65a1a77371aedf596b587ef62c27f7b0c8a749734f132e025a2b3b8c`
- SDK HTTP body（不変）: `86f954f206096d56cdbab8f9d9b9c471ef91c7abad1ba28b1d9ec3a69c1febd7`

追加検証は合成permitとダミーcredentialのみ。Human issuer、実credential、外部通信、課金、
Console操作、Real permit発行、Real attemptは行っていない。
本P3差分のIndependent Reviewは2026-09-22 JSTにPASS済み。最終Human GOは別途必要。CLI Railの既存NO-GOも維持する。

P3差分の検証結果:

- `python3 -B -m unittest discover -s tests -v`: 138件PASS（53.795秒）。RC関連8件を含む。
- READMEのisolation確認、smoke、b1 smoke、Secret Handoff／Boundary／ABC／Consumer Window、
  Runtime Service smoke: 全PASS。CLI probe: `PASS_OFFLINE_WITH_LIVE_BLOCKER`。
- API Rail: `PASS_OFFLINE_API_RAIL`、48ケース、POST 46、auxiliary 0。
  offline Evidenceのauthorization=falseも全ケースで検証。
  Evidence: `.local/messages-api-rrgpkgt1/summary.json`。
- Real network-path offline: `PASS_OFFLINE_REAL_NETWORK_PATH`、runtime 5、POST 3。
  消費済みの成功／TLS失敗／redirect拒否／Output拒否でauthorization=true、usage／charge=null。
  Evidence: `.local/messages-api-network-fwtx9w7u/summary.json`。
- artifact再生成: `CANDIDATE_ONLY_NOT_REAL_GO`、外部通信0、Real permit発行なし。
- `git diff --check`: PASS。今回の検証で新規Findingなし。その後のIndependent ReviewでもP0 / P1 / P2 / P3 Findingなし。

Independent Review完了: `66fc9e8..3694f83`。P0 / P1 / P2 / P3 Findingなし、Engineering complete。
