# 初回固定資料レビューの認証経路比較（2026-09-18）

基準: B4 `77cbd616e32ab176da49ad239a11e4f0d0ad50e2`、Linux/WSL、
Claude CLI `2.1.274 (Claude Code)`、SHA-256
`15e2d05148f801b5774032faad87e624ecd172e9903288bda448b892eb58fa07`。
これは選択のための比較であり、認証・通信・credential利用・Live Gate解除の承認ではない。

## 判断

**今回の一件は、既存の専用login設計を継続することを推奨する。**
ただし、credentialそのもののLeast Privilegeでは **setup-tokenが優位**。
両者を同じ権限のcredentialとみなした推薦ではない。

setup-tokenでも同じcallback listenerが必要であり、現状の主要なPermission blockerは消えない。
さらに1年tokenの安全な受け渡し、Evidenceへの混入防止、保管・失効手順が必要になる。
一件のためにこれらを追加するより、既存inventoryと専用auth homeを使う方が変更量が小さい。
通常loginの広いscopeとrefresh credentialを隔離CLIへ渡す残Riskは、Humanが別途判断する。
案(b)のTCP egress受容は、このcredential Riskや待受権限を承認したことにはならない。

**refresh credentialをRuntimeに渡さないことを最優先する場合はsetup-tokenを選ぶ理由がある。**
その場合は、秘密情報をEvidenceから分離する入口と失効方法を先に設計・reviewする。
通常loginがcredential面でも最小権限、またはsetup-tokenがすぐ利用可能、とは結論しない。

## 読み方と公式仕様

- **公式**: 2026-09-18に取得した公開docs。2.1.274専用の固定仕様ではない。
- **静的**: 上記hashのbinaryに埋め込まれた実装の読取り。実認証成功の証拠ではない。
- **実測**: 空の専用HOMEで、全socketを拒否し、version/helpだけを実行。
- **未確認**: OAuth server、実発行scope/期限、callback成功、TLS、Provider処理など。

公式はsetup-tokenを1年のsubscription tokenとし、通常loginと同じbrowser認証を経て
terminalへ表示し、自動保存しないと説明する。用途はモデル要求に限定され、Remote Controlや
claude.ai connector取得には使えない。一方、ローカルMCPの設定は引き続き有効。
[公式Authentication](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)

`CLAUDE_CODE_OAUTH_TOKEN`はCLIの認証入力で、保存済みloginより優先する。
期限切れ時は新tokenへの入替えとプロセス再起動が必要。
[公式Environment variables](https://code.claude.com/docs/en/env-vars)

## 比較

本書の「専用 `/login`」は通常subscription OAuth経路を意味する。
CCWで実際に提案済みのcommandは **`auth login --claudeai`**。
対話型chatを立ち上げて `/login` を入力する方式ではない。

| 観点 | 専用login | setup-token |
| --- | --- | --- |
| 認証時callback | 共有OAuth flowがIPv4 `127.0.0.1` の動的portをlisten。手動code入力でも先に起動（静的） | 同じflow・同じ待受（静的）。callback回避策にはならない |
| 追加network権限 | 現行denyのbind/listen/accept系をlogin限定で別reviewする必要 | 同じ。生成場所を別hostへ移せば、そのhostの認証・credential移送という別Permissionが必要 |
| Runtime credential | 通常経路はaccess tokenとrefresh token、scope/期限等を保存しCLIが読める（静的）。一件に不要な権限も要求 | Runtime入力はaccess tokenのみ。環境変数分岐はrefreshToken=null。scope要求は`user:inference`のみ（静的） |
| 有効期間 | server返却期限＋refreshの仕組み。実期限未確認。短命・一回限りとは扱わない | 31536000秒（365日）を要求。helpに期間短縮optionなし。実発行期限は未確認 |
| 保存・露出 | 専用auth homeのcredential file。CLIとHumanの私有領域。Worker/Brokerへ内容は渡さない | terminalにtoken本体が出る。その後の安全な保存/受渡しは利用側の責任。環境変数は子CLIのmemory/environにも存在する |
| Tool/connector | credentialのscopeは広いが、CCWの空tools/strict MCP等で利用機能を絞る | cloud connector等のcredential権限が狭い。ただしローカルTool/MCPを無効化する仕組みではない。CCWの制限を維持する |
| logout/revoke | pinned logoutは保存refresh tokenのserver revokeを試み、その後local cleanup。失効失敗でもlocal logoutへ進む（静的） | env tokenだけでは同じlogout経路にrevoke用refresh tokenがない。process内unsetはparent shellやコピー、serverのaccess tokenを失効させない |
| auth inventory | 既存の`.credentials.json`/`.claude.json`0600、空`sessions/`0700契約を利用可能。認証後の実生成物は未確認 | `.credentials.json`にtokenを自動保存しない。設定/registry生成は別問題。保存先を増やすなら新契約が必要。空auth inventoryだけではtoken不在を証明できない |
| launcher変更量 | B4の計画・専用home・metadata inventoryを再利用。実行器、login限定待受、私有I/O、Recoveryはなお未完成 | 共通callback問題に加え、発行UI/token出力、秘密入力、Evidence非記録、破棄・失効確認を追加。単なるcommand差替えでは済まない |
| 今回の適合 | 一件への追加実装が少ない。credential権限は広い | credential権限は小さい。長期無人運用向けの利点より、一件の秘密管理追加負担が大きい |

どちらも、credential自体が資料digest・一回・特定modelに拘束されるわけではない。
single-use permitはCCWの起動制御であり、流出したtokenをProvider側で一回に制限しない。
任意IPv4/IPv6 TCP egress下でCLIが侵害された場合、両方とも持ち出し可能性が残る。
loginではrefreshを含む広いcredential、setup-tokenでは長期間使えるinference credentialが対象になる。
OSの同一UIDに対する完全な秘密保護は、どちらの保存方式についても主張しない。

## pinned binaryで確認した根拠

再現: `python3 -B tests/auth_route_probe.py`。
既存の固定binaryコピーだけを読む。認証を伴うcommandは実行せず、固定した`--help`だけを使う。
static excerptは`.local/`に置き、binaryや抜粋をGitへ追加しない。

| static.jsonのsection / byte offset | 読み取れること |
| --- | --- |
| `scope_constants` / 189597450 | `Sw=user:inference`、`dq=31536000`。通常scope集合はprofile/inference/sessions/MCP/file_uploadと条件付きpluginsを含む |
| `authorize_scopes` / 192247651 | URLのscopeはinferenceOnlyなら`[Sw]`、通常は`Bor()`。`Bor()`は`org:create_api_key`を含む集合のunion。これは**要求scope**であり、subscription側が全て発行する証明ではない |
| `callback_listener` / 207397050 | listenerをstartしてからmanual/自動redirect URLを作る。listen先`127.0.0.1`。manual code entryもlistenerを省略しない |
| `subscription_login` / 211073600 | `auth login --claudeai`が共有startOAuthFlowを呼び、保存関数へ渡す。refresh envによる別分岐はCCW環境には無い |
| `setup_flow_and_output` / 210807000 | setup-tokenも共有startOAuthFlow、inferenceOnly=true。成功時はaccessTokenを表示し、通常loginの保存関数分岐を通らない |
| `setup_lifetime` / 211016080 | 期間計算は固定365日。内部のexpiresInDaysという名前から短期発行optionの存在を推定しない |
| `credential_persistence` / 192306750 | 通常保存分岐にaccessToken/refreshToken/expiresAt/scopes等。実serverが返す内容は未確認 |
| `environment_token` / 192308805 | env access tokenの読取りはrefreshToken=null、expiresAt=null、既定local scopesはinference。local expiry=nullは無期限という意味ではない |
| `logout_cleanup` / 207183885、`refresh_revoke` / 192251801 | 保存refresh tokenがあればrevoke POSTを試行。失敗してもlocal cleanup。env tokenをprocess内でunsetするだけではserver失効にならない |

通常OAuth URLに`org:create_api_key`の文字列があることを、API key作成が承認済み・実行済み、
またはsubscription credentialで必ず可能という意味にはしない。実発行scopeとserver enforcementは未確認。
setup-tokenのUIは成功tokenを表示するが、OAuth交換の応答に何が含まれるかも未確認。
「発行serverがrefresh tokenを一切返さない」とまでは断定しない。

## 現在のCCWとの接続

以下は比較commit時点の状態。後続の[Secret Handoff prototype](checkpoint-secret-handoff-20260918.md)で
環境値のcontract/plan/Evidence収録を解消した。実token注入と実CLI検証は未実施のまま。

`claude_real.environment()`はcaller envを継承しない。tokenをshellでexportしてもCCWへ渡らない。
さらに`contract().environment_policy`と`login_plan().environment`は値をEvidenceへ含めるため、
**ここへtoken文字列を追加してはならない**。
setup-token採用時は、公開contractと秘密値注入を分離した入口を新設し、値がplan/ledger/
diagnostics/raw/logへ混入しないことを合成tokenで検証する必要がある。
FD渡し等の代案も、現行の継承FD閉鎖・sealed ELF FD3の契約を変更するため無償の差替えではない。
今回これらは実装せず、秘密値の読取りも行っていない。

どちらの推論でも既存の`--tools ""`、`--strict-mcp-config`＋空MCP設定、
`--setting-sources ""`、hooks無効、`--no-chrome`、slash commands無効、
no-session-persistenceを維持する。StructuredOutputの限定用途はB3/B4のまま。
認証方式変更だけを理由にshell/browser/MCP/connectorを追加しない。
callback listenerは認証時だけの問題で、推論profileのbind/listen/accept拒否は維持する。

## 次のHuman Gateと未確認事項

1. 経路を決める。推奨は専用login継続。ただし広いcredential/refreshをRuntimeへ渡す点を明示して判断する。
2. **login限定callback権限の設計review**。現行seccompはbind/listen/acceptを拒否する。
   binaryがloopbackを指定する事実だけでは、syscall許可後もloopback限定になるとは保証できない。
   素のseccompはsockaddrを検査できない。B4の別Human Gateを維持する。
3. 上記設計・private terminal I/O・timeout/一回起動/no-retry・停止回収・Recoveryを具体化した後、
   別途、指定account/subscriptionでの実認証・OAuth通信・credential保存を承認する。
   自動browser起動のためのexec許可は提案しない。Human browser操作も実認証承認の対象。
4. setup-tokenを選ぶ場合はさらにtoken表示/受渡し/保管の範囲、Evidenceからの分離、
   standalone access tokenのserver失効方法を確定する。shell履歴や一般の記録terminalへtokenを貼らない。
5. 認証後はprocess終了を確認し、専用auth homeのowner/mode/type/link数とpath差分を取得する。
   `.credentials.json`と`.claude.json`は0600、`sessions/`は空0700。
   未知生成物・残存PID/peer key・link・mode不一致は自動許可せず停止する。
   HOME/tmp/ログも生成物の安全性reviewが必要。token内容はEvidenceへ収録しない。
6. 初回Real承認はさらに別。Bun TCP DNS、TLS、OAuth/Provider相互運用、実効scope/期限、
   account/組織/課金設定、必要なlogout/revokeの対象と効果はoffline未確認。
   CLIのlogout成功表示も、既発行access tokenの即時失効を証明しない。

Recoveryは、失敗時に再送せず停止・回収し、私有auth領域とmetadata Evidenceを保持する。
server revokeの実通信は別途許可された操作だけで実施し、失敗時にlocal削除で成功扱いしない。
setup-tokenの確実な個別失効手順は今回の公式資料と静的読取りからは確定できず、Openのまま。
通常loginもserver失効の実効性は未確認だが、pinned logoutにはrefresh revokeの実装経路がある。
両方とも、実認証を試してこの不確実性を解消することは今回の許可に含まれない。

## Evidence / 変更範囲

実測Evidence: `.local/auth-route-8jxr355b/summary.json`、`static.json`、各commandの`stdout.txt`。
version、login help、setup-token help、logout helpは全てPASS、4個のauth homeはいずれも空のまま。
setup-token helpには`-h, --help`だけが表示され、期間短縮・no-browser optionはない。
最初のprobeはscratch親directory未作成でCLI起動前に失敗し、一度修正して再実行PASS。
失敗Evidenceは`.local/auth-route-6n_8v4rl/`に保持。

| 回帰検証 | 結果 / Evidence |
| --- | --- |
| `python3 -B -m unittest discover -s tests -v` | 80 tests PASS（B1/B2/B3/B4、両Gate・inventory・単回permit・raw保持を含む） |
| `python3 -B tests/smoke.py` | PASS、`.local/smoke-xsv6zz3v/summary.json` |
| `python3 -B tests/b1_smoke.py` | PASS、`.local/b1-smoke-cfxsnwse/` |
| `python3 -B tests/b2_smoke.py --saved-b3` | HELP_VERSION_ONLY成功、`.local/b2-probe-jb07ezwj/`。固定binary、空auth、attempts=0 |

変更は本書とhelp専用probeのみ。production code、権限、auth inventory contract、
login/Live Gate、single-use permit、no-retry、raw保持は変更しない。
既存B4のlogin実行案を置き換えず、本書を認証経路選択の補足Evidenceとする。
Rollbackはこの比較commitの2ファイルのみをrevertする。auth領域や既存ledgerは巻き戻さない。
