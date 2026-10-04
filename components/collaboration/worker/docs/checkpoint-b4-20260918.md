# CCW B4 — 案(b)での初回Real準備 checkpoint

開始commit: `aaef10f4d3475489d7361306ff88580dfeaa7e40`。開始時の作業treeはclean。
終了commitはこの文書を含むcommit。Review範囲は `aaef10f..HEAD`。
入力されたB3 Independent Review判定はOffline ACCEPT、既存境界PASS / readiness NEEDS FIX、
専用login・初回RealともNO-GO。B4のIndependent Reviewは未実施。

Humanは**初回固定資料レビュー一件について案(b)の任意IPv4/IPv6 TCP残Riskを受容する方針**を選択した。
これは実login、credential登録・利用、OAuth/Provider通信、推論、資料送出、Live Gate解除の許可ではない。
本checkpointはその区別を維持する。実loginと初回Realは引き続きNO-GO。

## 実装した範囲と停止位置

- `validate_auth_inventory`を追加。既知2fileの種類/modeと空sessions directoryを明示検査する。
  終了後の空sessions追加だけを許容し、未知生成物や残存PID/keyは停止する。
- Real専用environmentへ `RES_OPTIONS="use-vc timeout:2 attempts:1"` を固定。
  B1 environment、native seccomp/FS許可は変更しない。contractにもDNS方式と未検証範囲をbindする。
- `human llm-claude-login-plan`を追加。固定binary、argv、専用path、環境、inventory、未解決条件を表示。
  login用launcher入口は無条件停止。計画の300秒・単回起動は提案値であり、実行制御の完成を意味しない。
- 全socket禁止の診断に、レビュー引数＋help、login引数＋helpを追加。実login/推論には入らない。
- installed CLI更新時のため、`llm-prepare-claude-real --from-prepared-root ABSOLUTE_ROOT`を追加。
  指定した旧targetのversion/help/hashを検証してbinaryだけを新規rootへ準備する。
  元のauth、DB、permit、設定は読み取り/コピーしない。自動fallback・自動更新はない。

**追加の重要Permissionが必要な箇所は実装前で停止した。** 固定CLIのOAuthは手入力でもcallback listenerを
最初に起動し、現在拒否するbind/listen/accept系が必要。ユーザーのStop Conditionsに従い、この権限を
追加していない。login実行用のprivate端末bridge、単回起動記録、300秒timeoutも未実装。
既存の `login` 停止入口だけを外してもlogin可能にはならない。

## pinned binaryと静的根拠

Claude Code `2.1.274 (Claude Code)`、SHA-256
`15e2d05148f801b5774032faad87e624ecd172e9903288bda448b892eb58fa07`。
`.local/b2-probe-vg70xwcm/run/human/claude-runtime/claude`の保存済みbytesをhash照合して読んだ。
埋込みJS/schemaは静的資料として扱い、抽出codeを実行していない。

Evidence: `.local/b4-static-2juz2lth/snippets.json`（先行 `.local/b4-static-7m7gio0f` も保持）。

| byte offset | 確認した処理 / 意味 |
| --- | --- |
| 192082169 | sessionsを0700で作成、PID名JSONへpid/sessionId/cwd/時刻/version/kind等を登録、exit時に削除 |
| 192064428 | sessions内のPID＋socket hash名`.key`はpeerToken等を含む。mode0600。無害なcacheとは扱えない |
| 202516348 / 202518000 | `--tools ""`から空の選択を作り、builtInToolsDisabledと除外規則を設定 |
| 198113557 | builtInToolsDisabledの場合はpluginの動的Tool登録も拒否 |
| 189305775 / 196293244 / 204646700 / 204649873 | strict指定をNsrへ渡し、C0が状態を返す。cloud MCP取込のi3eはstrictならfalse。空のstrict MCP契約を維持 |
| 207397645 / 207401312 | OAuthが127.0.0.1の一時portへlistenし、その完了後に手入力/自動callbackのURLを作る |
| 204886542 / 211073600 | auth loginの`--claudeai`と`--console`は別経路。code#stateの手入力を受付 |

`auth login --no-browser`というオプションはこの版にはない。MCP loginの同名optionと混同しない。
browser自動起動は現profileで拒否される。拒否後のmanual入力が実際に完了することは未検証。

## sessions / auth contract

現在受理する終了後・起動前の状態:

| 相対path | 種類・mode | 内容の扱い |
| --- | --- | --- |
| `.credentials.json` | owned regular file、0600、nlink=1 | 本文・hashを取得しない。名前だけでsubscription認証済みとみなさない |
| `.claude.json` | owned regular file、0600、nlink=1 | CLI設定/状態。metadataのみ。設定内容の安全性は別確認 |
| `sessions` | owned directory、0700、空 | PID registry/peer鍵の親。内部fileは許可しない |

root自身は操作者UIDの0700。symlink、hardlink、特殊file、group/other権限、未知path、
既知fileをdirectoryに置換した状態も拒否する。実credentialを使った検査は行っていない。
変更検出は前後のmetadata snapshotで、常時監視やrace完全封止ではない。

post inventoryでは空sessionsの追加をobservedとして保持できる。既知fileの内容更新に相当するmetadata変更は
従来どおり記録し、credential/configの新規追加や消失、sessions directoryの置換はreview_required。
安全なinventory自体を取れなければinvalid。Brokerはobserved以外でunknownを維持し、取得済みrawを保持、
permitを戻さない。sessionsのPID JSON・keyが残った場合は本文を自動読取せずprivateに保全する。
stale PIDを自動削除したり名前patternだけで許可する経路はない。

help診断3種のauthは空。新規空HOMEのauth statusはloggedIn=falseで、0600の`.claude.json`だけを生成した。
今回のCLI診断ではsessions生成は観測していない。sessionsの用途・内容形は静的code、
空directory受理と残存物拒否は合成fixtureで検証した。実login後の生成物全体は未確認。

## DNS / Tool面の判定範囲

Real環境の固定RES_OPTIONSを、同じnative profile内でPython/glibc getaddrinfoへ渡した。
非特権user/net/mount namespaceにはloだけ、外部interface/routeなし。
Fake DNSは127.0.0.1:53、Fake endpointは127.0.0.2、名前はvendor.ccw.test。
resolv.confのmountは子namespace内だけで、host/WSLの設定は変更しない。

Evidence: `.local/b3-network-81qowd_5/{stdout.json,observations.json,stderr.txt}`。

| 検査 | 観測 |
| --- | --- |
| 通常設定 | DNS失敗 -2、UDP受信0 |
| 子resolv.confのuse-vc | Fake DNS/TCP成功 |
| 通常の子resolv.conf＋Real environment | Fake DNS/TCP成功。環境変数による経路を確認 |
| 直接loopback TCP | 全modeで成功。vendor限定でない残Riskを再確認 |
| UDP / callback bind / listen / accept | 全modeでEPERM |
| 192.0.2.1 | routeなしによるENETUNREACH。host/LANを遮断する証拠ではない |

**Bun/c-aresの実解決は未確認。** 固定binaryにRES_OPTIONS/use-vc文字列はあるが、文字列の存在は適用証明でない。
別のBun版やglibcの成功をClaudeのfetch成功とみなさない。実resolverのTCP応答可否、TLS証明書検証、
OAuth endpoint、redirect、refresh、Providerの動作は未確認。UDP許可、TLS検証無効化、proxyの自動追加はしない。

Tool制約は `--tools ""`、strictな空MCP、safe-mode、settings source空、hooks無効、
Chrome/skills無効、session persistence無効、固定system promptを維持した。
Read/Write/Bash/WebFetch/WebSearch/Agent等を必要Toolとして追加していない。
StructuredOutputだけが最終回答用の内部Toolとして必要。
CLIの固定引数＋help成功と静的な選択処理は確認したが、helpは推論sessionのTool一覧を出す操作ではない。
実際のsession初期化／Provider送出Tool一覧の確認はPartial。結果parserによる外部Tool活動拒否は既存回帰で確認。

## Verification / Failure Analysis

| コマンド | 結果・Evidence |
| --- | --- |
| `python3 -B src/ccw/isolation.py` | Landlock ABI 3 |
| `python3 -B tests/test_claude_b3.py -v` | 14 tests PASS（B4追加4件を含む） |
| `python3 -B -m unittest discover -s tests -v` | 最終80 tests PASS、18.726秒。先行80件19.023秒もPASS |
| `python3 -B tests/smoke.py` | PASS、`.local/smoke-_lm18f4b/summary.json`、4ケース＋Scout＋B0 |
| `python3 -B tests/b1_smoke.py` | PASS、`.local/b1-smoke-k3opf92a/summary.json`、Fakeのみ |
| `python3 -B tests/b2_smoke.py` | installed binaryのhash不一致で起動前停止、`.local/b2-probe-kdidp4s0/summary.json` |
| `python3 -B tests/b2_smoke.py --saved-b3` | PASS、`.local/b2-probe-gf0t7aiw/summary.json`。固定版help/version/空auth、attempt=0 |
| `python3 -B tests/b3_probe.py` | 固定版schema静的確認、`.local/b3-schema-e55ycygm/schema.json` |
| `python3 -B tests/b3_network_probe.py` | 隔離FakeのみPASS、上記network Evidence |
| `python3 -B tests/b4_probe.py` | 上記静的根拠を保存 |
| `python3 -B tests/b4_probe.py --diagnostics` | PASS、`.local/b4-cli-yjfz4csl/summary.json`。全socket禁止、help/statusのみ |

network probeの初回は外側sandboxのsocket制限でEPERM（`.local/b3-network-twql28ja`）。
固定probeのsandbox外実行承認後、同じCCW制限・外部routeなしのnamespaceで一度再実行しPASS。
host root/sudo、host networking変更、Full Accessへの切替は使っていない。

B4診断の初回はschema依存importをLandlock適用後に行いEACCES（`.local/b4-cli-49va591h`）。
trustedな引数生成を制限適用前へ移す1回の修正・再実行でPASS。repository read許可は追加していない。
B2のhash不一致には未Review binaryを許す修正をせず、上記の明示的な旧target再準備を追加して一度再検証した。
途中失敗Evidenceは保持。既存ledger、single-use permit、raw保持、no-retry、Worker/Signer境界は維持した。

## Blocker一覧（実装者判断、独立Review前）

| Finding | 状態 | 次の確認 |
| --- | --- | --- |
| 空sessionsのauth contract不整合 | Closed（offline） | 実login/終了後の全生成物は再inventory |
| 未知auth生成物の扱い | Closed（fail closed） | login後の未知生成物は再Review。自動許可なし |
| TCP-only境界でDNS方式未配線 | Closed（glibc/Fakeの配線と検証） | Bun・実resolverを含むreadinessはPartial |
| 案(b)の方針選択 | Closed | 一件のlive許可とは別 |
| 固定資料用途のTool設定 | Partial | 静的codeとhelpは確認、実効session一覧は未確認 |
| login launcher / Evidence / Recovery | Partial | 表示計画・停止入口・手順を追加。実行制御は未実装 |
| login callback権限 | Open、重要Permissionの停止点 | 下記Gate Aを先に判断 |
| Bun / TLS / OAuth / refresh / Provider | Open | 許可された環境・段階だけで検証 |
| installed版の更新 | Closed（固定版準備の代替） | 今後の版変更は別Review。現installed版は不許可 |
| Live Gate / 単回permit / no-retry / raw | 維持・回帰PASS | 解除には別code Reviewと個別承認 |

## 具体的なHuman Gate

### Gate A — login専用の追加Capabilityを検討する許可

まずB4 diffとこのEvidenceをReviewする。案(b)を選び直す必要はない。
現行コードではcallbackのbind/listen/accept/accept4（必要syscallはFakeで絞る）が不足する。
この追加は**TCP egress方針の選択に含まれない待受Capability**である。

Humanが判断するのは「login専用profileへ待受権限を追加する設計・Fake検証に進んでよいか」。
CLIの実装は127.0.0.1へbindするが、現行のseccompはsockaddrの内容を検査できない。
単純にsyscallを許すだけなら悪意あるruntimeの任意interface/portへのbindまで防げず、
loopback限定を強制したと主張できない。この残Riskを含むlogin専用の追加を受容するか、
別の限定方法を要求するかを決める必要がある。今回はどちらも実装・許可していない。
inference profileのbind禁止は維持する。browser/shell exec、UNIX socket、通常HOME readは追加しない。

次の実装では、private Human端末↔bounded pipe、wall 300秒、起動1回の永続記録、kill/reap、
親終了時kill、前後metadata inventoryを用意する。失敗時も自動再試行しない。
OAuth URL/state/codeを通常のcapture EvidenceやCodex transcriptへ保存しない。
これをFake callbackと空HOMEでReviewしてから、Gate Bへ進む。

### Gate B — 専用loginそのものの個別承認（今は実行不可）

Review可能な具体例は `.local/b2-probe-gf0t7aiw/login-plan.json`。
同runは合成Smoke用途なので、本番のTaskや承認・ledgerとして流用しない。
Humanが選んだ未使用rootで `llm-claude-login-plan` を再生成し、以下を確認する。

1. 上記CLI version/hash、最終commit/code_manifest、login profile、argv、rootを固定する。
   argvは固定binaryに `--safe-mode --setting-sources "" --settings '{"disableAllHooks":true,"fastMode":false}'`
   と `auth login --claudeai`。`--console`、setup-token、既存token/env注入は使わない。
2. authは `ROOT/human/claude-runtime/auth`、HOME/tmpは同runtime内の新しい
   `login-workspace/home` / `login-workspace/tmp`。owned0700、umask077、authは空または空sessionsのみ。
   binary-only再準備は未知attempt再実行の口実にしない。
3. Gate AでReviewされたcallback方式、DNS、300秒・1起動・停止方法を確認。
   login中のOAuth/profile/token交換への実接続と専用credential書込みを**このloginに限り**承認する。
   未確認TLSやresolverが失敗したら止め、権限拡大・検証無効化・自動retryをしない。
4. Humanの私有browserでsubscription経路、予定account、Extra usage OFF、課金条件を確認する。
   account識別情報、URL/state/code/tokenをCodexへ貼らない。追加使用OFFはauth statusだけでは証明できない。
5. abort/失敗時にもprocess終了とProvider側失効手段をHumanが確認し、private生成物を保全する。
   login成功を推論承認に転用しない。Gate B終了時もReal推論は無効のまま。

### login後のinventory / 初回Real前

全子process終了後、auth・login HOME/tmpのpath/kind/UID/mode/nlink/size/時刻/inode/deviceを確認する。
credential本文やhashをログへ出さない。既知2file0600、空sessions0700以外、残存PID/key、
debug/cache/lock/backup等は自動許可しない。未知生成物はprivateに保全し、必要性と内容をHumanが別途Reviewする。
HOME/tmpの追加物もpath allowlistだけで非機密とは認めない。auth hardlinkの別名やraw/log/DBへの混入は残Risk。
metadata安全性とsubscription認証・Extra usage OFFは別に確認する。

初回RealにはBun/TCP DNS/TLS確認、login完了と生成物Review、実効Tool面、
StructuredOutputの互換性に残る不確実性、実model/effort/利用枠を確認する。
Live Gate解除のcode Reviewと、固定task/request digest・送出資料・1 launcher・timeout/bytes/TTLの
個別Human承認がさらに必要。max-turns=1はProvider request数1を保証しない。
利用後rejected、unknown、内部retry/fallback、usage欠測、費用上限非強制を明示し、再実行を自動許可しない。

## 案(b)の残Risk / Recovery / rollback

任意IPv4/IPv6 TCPによりProvider以外、loopback/LAN、他のTCPサービスへ到達し得る。
runtimeは専用credentialを読めるため、そのTCP経路への流出を外側境界で防ぐ保証はない。
DNSのTCP化はegress allowlistではない。credential hardlink、私有rawへの混入、loader経由再execの
既存残Riskも継続。CPU/wall timeoutやkillはProvider処理停止・消費ゼロの証明ではない。

loginでは中断後に再起動せず、process終了・auth/workspace生成物・Provider側状態をHumanが確認する。
local logoutやfile削除はserver側token/session失効の証明ではない。専用sessionの失効手順は
公式account画面とCLI仕様を、許可された段階で確認する。今は実logout・失効・削除をしていない。
推論の停止/照合は既存 `llm-stop` / `llm-status`。raw/attempt/permit/unknownを編集・巻戻ししない。

code rollbackはclean tree確認後、このB4 commitを `git revert B4_COMMIT` する。
reset、stash、既存Human作業の上書き、`.local/`一括削除はしない。新rootによる再送も禁止。
code manifest変更で旧Evidence照合が止まる場合は元commit/runtimeを隔離checkoutで保持して検証する。
DB・credential・Evidenceの巻戻しはcode revertに含めない。

実credentialアクセス/コピー/登録、実login、OAuth/Provider接続、推論、資料送出、API fallback、追加課金、
別account、sudo/host root、host-wide mutation、Signer/Wallet/Technocore write、push/PR/publishは実施していない。
