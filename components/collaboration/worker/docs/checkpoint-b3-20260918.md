# CCW Batch B3 checkpoint — 2026-09-18

開始: `main` / `7b40044dbb8896607e3e4c8a60425aa86126f659` / tracked・untracked差分なし。
worktreeはこのRepositoryの1つのみ。引継ぎ基準commitとHEADが同一であることをローカルで確認した。
終了commitはこの文書を含むcommit。Review範囲は `7b40044..HEAD`。
既存変更・`.local/` Evidenceは保持。reset/checkout/削除・他Repository変更は行っていない。

入力のIndependent Review判定は **B2 Offline Implementation=ACCEPT / Real Runtime Boundary=NEEDS FIX /
初回Real=NO-GO**。Review原文 `ccw_b2_independent_review_20260918.txt` は、ignoredファイルを含む
Repository内検索でも見つからなかった。依頼文のFindingと現行code、保存binaryを基準にした。

**B3の既知mapper互換性修正とオフライン検証は完了。B3 Independent Reviewは未実施。
Real起動・loginは引き続きNO-GO。通信方式のHuman判断へ渡すcheckpointであり、
承認だけで即Real実行できるという判定ではない。** 案(a)の実装とBun/OAuth/TLSの確認は未完了。

## 変更とschema根拠

対象: Claude Code `2.1.274 (Claude Code)`。
SHA-256: `15e2d05148f801b5774032faad87e624ecd172e9903288bda448b892eb58fa07`。
既存B2 copy `.local/b2-probe-vg70xwcm/run/human/claude-runtime/claude` を静的に読んだ。
通常HOMEのcredential/configは読まない。埋込みJS/schemaは資料として読み、実行していない。
外部Web取得も行っていない。

`tests/b3_probe.py`はhash照合後に固定offsetのschema/生成codeを新しい`.local/b3-schema-*`へ保存する。
最終Evidence: `.local/b3-schema-vrbambnj/schema.json`。

| 根拠（binary内byte offset） | 確認した意味 / B3の扱い |
| --- | --- |
| 191810280付近 result schema | ttft_ms等の整数timing、wall time、queue/result index、fast mode、subagent statsを型検査。未知fieldは拒否 |
| 191899634 fast mode enum | off/on/cooldown。B3契約はoffのみ。disabled reasonは埋込みenumの10値に限定 |
| 191802515 subagent stats | spawned/requested/killed/refused等のnested shapeを検査。新規固定Taskでは全0と空by_typeだけ受理 |
| 191805313 accounting説明 | modelUsageはquery pipelineのmain loop・sidechain・compaction等の累積値。usageと合算しない |
| 191807151 result_index説明 | resultの出力順（0開始）。num_turnsやProvider request IDではない。今回queue=0/index=0のみ |
| 191719974 modelUsage schema | canonicalModelは価格lookup用ID、costBasisはlist/managed/unknown。要求modelと同じIDだと強制しない |
| 191097550付近 catalog | 補助accounting候補はclaude-haiku-4-5 / claude-haiku-4-5-20251001。利用可能性を証明しない |
| 193797459 StructuredOutput | read-only/internal Tool、構造化値を返しendsTurn=true。Bash/WebFetch/Agentと区別 |
| 204256800 / 204262580 | Toolのend-turn処理とmax-turn到達処理は別経路。単回CLI起動をProvider request1回と扱わない |
| 211593700付近 result producer | Tool返答、構造化出力retraction/retry、turn情報、timing付加を含む。Realでの分岐到達は未確認 |

Real target準備・読取りは上記version/hashへ固定した。準備時はversion/help診断の起動前にもdigestを検査する。
新しいinstalled CLIへ追従した場合は停止して再Review。
`claude-opus-5`、小数点を含む形式、旧世代の数字先行形式もsyntaxとして扱えるようにした。
実際のmodelはHuman未選択であり、構文合格はsubscriptionでの提供やbackend revision固定の保証ではない。
alias/latest/context suffix/引数注入は拒否する。

要求modelがmodelUsageのkeyにあることは必須。追加keyは上記Haiku候補だけで、未知model/別Sonnet・Opusは拒否。
全entryを同じ厳密型検査にかけ、providerがあればfirstPartyのみ、webSearchRequestsは0のみ。
canonicalModelは価格用metadataとして別に保持する。reported_modelsは「usageに現れたkey」の一覧であって、
primaryの独立した確認ではない。reported_primary_model=null、model_roles_verified=falseを明示する。
Haikuが本当にauxiliaryだったかはresultだけでは立証できず、CCWのfallback許可にも転用しない。
--fallback-modelを付けず、automatic_model_fallback=falseを契約へ含める。
CLI内蔵fallback全体の封止を証明したという意味ではない。

usage/token/reasoning/cost欠測はnullまたは欠測entryのまま。costBasis欠測もlistへ補完しない。
reasoningは要求modelのoutput内の部分量であり加算しない。API換算額はsubscription実請求ではない。
利用枠残量・reset時刻・実請求・Provider request数・内部retryはunknown/null。

## StructuredOutput / parser / Failure Analysis

候補argvの --tools "" / --max-turns 1 は維持。Real用system promptだけに、外部Toolを使わず
StructuredOutputで回答することを明記した。B1 Fake契約は変更しない。
num_turnsは正整数（入力健全性上限1024）の観測値とし、1固定の誤拒否を除いた。
1024は実行許可数ではない。CLIに要求するturn上限は1のまま。num_turns=2 / stop_reason=tool_useでも
structured_outputのschema・引用が正しければ合成mapperは受理する。
StructuredOutput欠落、明示的error/max-turn/structured-output-retry超過は成功にしない。
実Providerが --max-turns 1 で構造化回答を完了するか、有効Tool一覧、内部request数は**Real未確認**。
合成テストと静的code読取りから推測してPASSにしない。

隔離parserの拒否を固定理由のJSON envelopeとしてBrokerへ返す。
未知fieldならその分類、error_max_turns等なら既知subtypeがEvidenceに残る。
未知key名、providerのerrors本文、tracebackは理由欄へコピーしない。詳細調査用rawはprivate DBに保持する。
exit 0/1の完了stdoutをmapperへ渡すplumbingにした。exit 1を成功とみなす変更ではない。
途中stdout/stderrの保存範囲は拡張していない。

post-auth inventoryはbeforeを保持し、afterが取れない場合もinvalidという固定状態を記録する。
追加・消失fileはreview_required、unsafe metadataはinvalid。いずれもunknownで止め、取得済みrawも保持する。
post-inventory失敗で例外だけが残りauth Evidenceがnullになるケースを改善した。
parser拒否の共有ledger保存はB1 Fake経路で検証した。Real Brokerの起動Gateをmockで解除するテストはない。
post-inventory関数は合成fileで検証し、Real統合動作は未確認と区別する。

## DNS / TCP offline probe

`tests/b3_network_probe.py`は非特権user/net/mount namespaceを作り、interfaceがloのみであることを確認する。
loをupにする以外にinterface/route/vethを追加しない。Fake DNSは127.0.0.1:53、
Fake vendorは127.0.0.2の一時TCP port、DNS名はvendor.ccw.test。
resolv.confは新namespace内だけのbind mountで、hostのfile内容は変更しない。
runtime profileを適用したchildがglibc getaddrinfoと固定Fakeデータの送受信を行う。
子のwall timeoutは親が8秒で強制。通常hostへfallbackする経路はない。

最終Evidence: `.local/b3-network-atxgqyy1/{stdout.json,observations.json,stderr.txt}`。

| 検査 | 結果 |
| --- | --- |
| ordinary resolver / TCP-only native profile | getaddrinfo失敗（EAI_NONAME=-2）、Fake DNS UDP受信0 |
| namespace内だけoptions use-vc | DNS A応答127.0.0.2取得、TCP DNS受信1、Fake vendor送受信成功 |
| DNSを使わず127.0.0.2へ直接TCP | ordinary/use-vcとも成功。現profileにvendor限定能力がないことを確認 |
| 192.0.2.1へのconnect | ENETUNREACH=101。隔離namespaceにrouteがないため。host/LAN制限の証明ではない |
| UDP socket | EPERM=1 |
| host resolver / 外部到達 | host変更なし、外部interface/routeなし、Provider/auth endpoint未使用 |
| Bun resolver / TLS / OAuth / login / inference | 未実施・未確認 |

失敗Evidenceも保持: `.local/b3-network-lzk4rlfc`（外側sandboxのsocket EPERM）、
`37a0nza3`（同prefix、stdinがpipeでなかった）、`hjfyqvuv`（同prefix、TCP検査失敗）、
`gcmuvfb_`（同prefix、詳細観測によりPython settimeoutでEPERMと特定）。
stdin修正1回後も失敗したためAGENTSの上限で停止し、Humanから追加offline診断・修正・再検証の許可を得た。
Python settimeoutはnative profileにないFIONBIOを使う。profileを広げず、probeの親timeoutで制限して成功。
外側sandboxの例外はこの固定offline probeと既存unit testに限定し、CCWのLandlock/seccompを外していない。

TCP DNS固定を本番採用したわけではない。Bunのresolverはglibcと同じと仮定できず、
このprobeのPASSだけでReal CLI通信可能と判断できない。UDPを全面許可する変更もない。

## 案(a)の実現性と残作業

**案(a)/(b)ともHuman未承認。案(a)は未実装。案(b)への自動fallbackなし。**
今回kernelのLandlockはABI 3でnetwork destination制限を持たない。現BPFはsocket domain/typeを検査し、
connectのsockaddrが指す宛先内容までは検査しない。TCP-onlyと名前解決設定だけでは案(a)にならない。
network namespace単独ならdefault-denyだがvendorへも到達できない。

sudo/host firewallなしの候補は、毎attemptで終了する限定relayを信頼側に置き、isolated namespaceから
そのrelayへだけ渡す設計。常駐daemonは不要にできるが、以下の新しいcode/Capabilityが必要:

- namespace外でsocketを作るtrusted connectorと、namespace内の固定proxy/pipeの橋渡し。
- Humanが固定したvendor host:portのみを許すCONNECT等のparser。任意proxy転送を許さない。
- 名前解決をconnector側へ寄せ、解決後IPのloopback/private/link-local/IPv4-mapped IPv6等を拒否する。
  redirect先・DNS変更にも同じ条件を適用する。DNS名の比較だけでは直接TCP/DNS rebindingを防げない。
- Bunがそのproxy方式を使うこと、runtimeがproxyを迂回してhost/LANへ到達できないこと、
  不要なnamespace内loopback portも拒否されることをFake環境で確認する。
- TLSを中間復号する設計は採らず、OAuth callback/refreshを含む必要endpoint集合を別途Reviewする。

現ABI/profileへ小さい宛先allowlistを足すだけでは足りず、新たな仲介境界の設計・検証になるためB3では実装しなかった。
seccomp user-notification等による宛先仲介も代案だが、TOCTOUやFD管理を含む新しい境界である。
host firewall、sudo、Docker権限、新daemon、host/WSL全体の変更を必要条件にはしていない。
次はHumanが案(a)の限定relay開発を選ぶか、初回のみ案(b)の任意TCP残Riskを受容するかを判断する。
案(b)を選んでもBun resolver/TLS/OAuth互換性は別途必要であり、現時点で起動を許可しない。

## 専用loginの準備手順（今回は実行しない）

login承認と固定Task推論承認は別。現launcherはauth loginを受け付けない。
将来のlogin専用launcherをReviewし、具体的なnetwork/DNS方式をHumanが承認してから進む。

1. 未使用run rootを選び、準備済みtargetのversion/hashを上記へ照合する。disk copyは0500、
   起動はsealed memfdの同一object照合。通常のPATHからclaudeを探して起動しない。
2. 専用配置は `ROOT/human/claude-runtime/auth`（CLAUDE_CONFIG_DIR）、
   `ROOT/human/claude-runtime/login-workspace/home`（HOME）、同workspaceのtmp（TMPDIR）を候補とする。
   各directoryはowned 0700、生成fileは0600相当、umask 077。新規空領域のみ、通常HOMEや
   既存Claude credentialからのread/copy、環境のtoken/API key流用をしない。
3. 固定argv候補は `claude auth login --claudeai`。このsyntaxと--consoleのAPI課金区別は
   binary offset 204886544付近のcommand登録を静的確認した。今回は起動していない。
   環境はCCWの最小envを基礎に固定し、通常PATH/proxy/keyring/WSL_INTEROPを継承しない。
4. login processは専用authのread/writeとVendorへの認証通信を持つ。OAuth browser callback用の
   bind/listen/acceptやbrowser起動は現在のinference profileにないため、必要な場合はlogin専用の
   最小範囲をFake callbackで先にReviewする。任意shell/browser execを一括許可しない。
   Humanが私有browser/terminalで公式画面を確認する方法も、URL/stateの扱いを含めて決める。
5. Humanがsubscription経路・対象account・Extra usage OFFを公式画面で確認する。
   auth statusだけで追加使用OFFや実請求0を証明しない。URLのstate/code、token、auth JSON、
   account情報をCodex transcriptや共有Evidenceへ貼らない。token確認・本文cat/hashはしない。
6. 前後inventoryはmetadataだけ。既知候補はauth内の`.credentials.json`/`.claude.json`。
   新しいdebug/config/cache/lock等が生成されたら、推論を始めずprivateに保全し、必要性・権限・
   設定解釈をHumanが再Reviewする。自動削除、自動allowlist拡張、通常HOMEへの切替は禁止。
   HOME/tmpを含むlogin workspace全体も確認する。metadataが安全でも内容の安全性は未証明。
7. 失効・Recoveryの確認項目は、専用sessionのlogout手順、公式account側でのsession/token失効方法、
   refresh tokenの扱い、callback process停止、workspace/log/rawの保存・削除範囲。
   local logout/file削除をserver側失効と同一視しない。これらの実動作は別承認後に確認する。

credential hardlink Findingは維持。runtimeはauthとworkspaceへ通常file書込みができ、credentialの別名が
home/tmpへ残る可能性がある。inventoryは起動前後の観測であり、瞬間的なlinkや本文流出の完全防止ではない。
実login後のrunner workspace / DB / parser input / log / raw responseは、Human確認前に公開可能Evidenceと扱わない。
合成hardlinkをmetadata inventoryが拒否するテストのみで、実credentialによる再現はしていない。

## Verification

| 検証 | 結果 / 保存先 |
| --- | --- |
| python3 -B src/ccw/isolation.py | Landlock ABI 3 |
| python3 -B tests/test_claude_b3.py -v | 9 tests PASS、1.229秒（初回追加test検証） |
| python3 -B -m unittest discover -s tests -v | 最終76 tests PASS、18.559秒（B3 10件＋B0/B1/B2回帰）。先行75件も19.506秒でPASS |
| python3 -B tests/smoke.py | PASS、`.local/smoke-5bw8cyrh/summary.json`、4送信ケース＋Scout＋B0 |
| python3 -B tests/b1_smoke.py | PASS、`.local/b1-smoke-giqvb15i/summary.json`、Fakeのみ |
| python3 -B tests/b2_smoke.py | 最終HELP_VERSION_ONLY成功、`.local/b2-probe-ot6rfbi9/summary.json`。先行`.local/b2-probe-krv_frkw`も保持 |
| B2空auth確認 | 新しい空HOME・全socket禁止でloggedIn=falseのみ確認。通常account未参照、raw auth JSON未保存 |
| B3静的schema probe | hash一致、上記9区画を保存。Real responseではない |
| B3network/DNS probe | PASS、上記隔離Fake endpointのみ。Bun/TLS/OAuth未確認 |

新規testはoptional field/不正型、複数modelUsage/canonical差異/auxiliary、usage欠測、StructuredOutput/
複数turn、error/max-turn系、未知field、隔離parser拒否理由、raw保存/no-retry、auth異常、model syntax、
Brokerと直接launcher両方のLive Gate、未Review binaryの診断起動前拒否を検査した。
未知fieldや追加modelは黙って採用しない。duration系の整数型も埋込みschemaに合わせて厳密化した。
Real契約を合成metadataでapproveするtestでも real_authorized=false / human_accepted=false、attempt=0。
本物のCLI診断はversion/helpと新規空auth statusのみ。Real推論・実credential取得・Provider送信は0。

## B2 Findingの現在状態（実装者判断）

| Finding | 状態 | 残る条件 |
| --- | --- | --- |
| pinned Real output optional field不一致 | Closed（確認済みsubsetのoffline修正） | Real response互換性全体はPartial、未知fieldは拒否 |
| singleton usage / canonicalModel混同 | Closed（accounting分離） | primary/auxiliaryの実役割とCLI内部fallbackは未確認 |
| StructuredOutput / num_turns=1固定 | Partial | 誤拒否修正、静的確認・合成test済み。実Provider完了可否は未確認 |
| glibc DNS / UDP拒否 | Partial | ordinary失敗とuse-vc成功を再現。Bun、採用通信方式は未決定 |
| vendor-only egress案(a) | Open | 上記限定relay等の後続設計・実装・検証。案(b)も未承認 |
| model名validation | Closed（syntax） | Humanの実model選択・提供確認は未実施 |
| parser拒否理由 | Closed（offline） | 安全な分類とrawを保持。Real統合は未実施 |
| auth inventory異常Evidence | Closed（offline関数） | Real login生成物/refreshと統合挙動は未確認 |
| Secret-bearing runner / credential hardlink | Open / 方針明記 | 実credential probeなし。公開前のHuman確認が必要 |
| P2-1 loader経由再exec | Partial / 保証範囲訂正 | 制限は継承。再exec完全封止を初回mandatory blockerへ昇格しない |
| binary identity表現 / 古いB1共有エラー | Closed（説明・文言） | main ELFの同一object保証。loader/library全体の固定保証ではない |
| 専用login手順 | Partial | 手順とGateを具体化。login launcher/認証通信の実検証は次段階 |

追加・変更Capability: parserの受理する既知metadata・accounting形、内部StructuredOutputのprompt説明、
offline testの隔離loopback通信。**本番runtimeのsyscall/FS/network許可は拡張していない。**
Worker/Human/SignerのTool境界、単回ledger、停止、no-retryは維持。
RSS上限、別UID、再exec完全封止、全P3は今回の必須範囲へ追加していない。

## 次のHuman Gate / rollback

順序は、B3 Independent Review → egress(a)/(b)とDNS方式のHuman判断 → 選択方式の残実装と
Bun/Fake環境検証 → 専用loginの個別承認・生成物再Review → 固定Task一件の推論承認。
本番通信/TLS/OAuthの未確認点は、承認された段階でだけ確認する。
推論承認にはtask/request digest、資料送出範囲、要求model/effort、binary/profile、auth inventory、
1 launcher、wall/bytes/TTL、停止方法、課金上限非強制、内部retry/fallback不確実性を含める。
消費後rejected、timeout後Provider処理継続、任意TCPを選ぶ場合のloopback/LAN/credential流出も判断対象。

B3のBrokerとonline launcherには無条件Gateが残る。Humanの承認値をtrueにする操作は行っていない。
Gateを将来解除できるcode変更のReviewと、特定TaskをHumanが承認する操作は別であり、
現在のoffline承認を流用しない。ここでは解除switchも新設していない。

停止はhuman llm-stop、状態確認はllm-status。local kill/reapをProvider停止の証明にしない。
コードrollbackは作業差分がないことを確認し、このB3 commitだけをlocal git revertする方針。
実行はHuman判断時に行い、reset --hardや既存worktreeの上書きはしない。
DB/permit/attempt/raw/auth/Evidenceは巻き戻さない。新しいrootを作って再送するRecoveryも行わない。
旧Evidenceの検証は元commit/runtimeを保持した隔離checkoutで行う。code manifest変更により旧Evidence照合が
止まり得る制約は継続する。`.local/`を一括削除しない。

push/tag/PR/release、実login・credential読取り/コピー、通常account参照、Providerへの接続・認証疎通・推論・
資料送出・追加課金、API fallback、別account、Technocore write、Signer/Wallet、sudo/Docker拡張、
host/WSL全体のnetwork変更は実施していない。
