# CCW Batch B1 checkpoint — 2026-09-17

開始時: `main` / `cf13205d0c56a3225ce3f1bca4216a03404aaefc` / worktree clean。
Batch A `227557b6479f04b300e19648d230704832f4ad8d` → B0 → このcheckpointを含むlocal commitがReview対象。
引継ぎ2ファイルを全文読取り、既存`.local/b1-input`とSmoke Evidenceを保持した。
Independent Reviewは未実施。本checkpointは実装者の報告でありReal利用の承認ではない。

## オフライン実装の到達点

固定Task/資料 → 合成LLM利用承認 → 既存Brokerの起動前attempt永続化 → B1 launcher →
別executableへのexec → Fake構造化応答 → Schema/原文引用/model照合 →
Broker provenance → 既存隔離Worker import/export → Human Previewが動作する。
Fakeは別の合成runnerでBrokerを迂回するものではなく、実コードのexec/stdio/validation/handoffを通る。
実binaryもこのlauncherからversion/helpだけを試す設計。B1のBrokerから実binaryへ切り替えるAPIはない。

| 区分 | 到達点 |
| --- | --- |
| 実装済み | Claude argv/stdin adapter、exec launcher、承認format 2、単回attempt、停止、結果/usage/provenance照合 |
| Fake検証済み | 正常成果物からHuman Preview、拒否/結果不明/改変/引用/model/usage欠測、認証fixture、exec後の境界 |
| 実binary確認済み | 配置path・ELF形式・全file digestのみ。version/help成功は未確認 |
| 実認証未検証 | 実credential・login・auth status・billing・利用枠・実model/Effort・実Tool面はいずれも未検証 |
| Real利用 | DISABLED / NO-GO。実推論/資料送出/追加消費/Technocore writeの承認はない |

`offline-sonnet-fixture-v1` / `fixture-high` / `fake-claude-b1-1`は合成値。
Sonnet/Opusの具体的な実ID・Effort・契約プランを決めたものではない。Fableは対象外。
認証statusはB1正規化fixture protocol。実CLIのJSON fieldを確認したと主張しない。
Brokerの固定auth fixtureと、Fake `auth status`取得probeは別であり、runtime起動回数へ追加auth起動を隠さない。

## Verification / Evidence

| 実行 | 結果 / 保存先 |
| --- | --- |
| `python3 -B src/ccw/isolation.py` | Landlock ABI 3 |
| `python3 -B -m unittest discover -s tests -v` | 最終58 tests PASS、15.568秒。既存47＋B1 11 |
| 最終Testログ | `.local/b1-verification-20260917.txt` |
| B1 exec後probe | `.local/test-b1-qngs4sr3/probe-results.json`、24項目PASS |
| `python3 -B tests/smoke.py` | 従来4ケース＋Scout＋B0 PASS。`.local/smoke-5tnmq3co/summary.json` |
| `python3 -B tests/b1_smoke.py` | PASS。`.local/b1-smoke-w60cwovn/summary.json` |
| B1 Human Preview / 承認 / 状態 | 同Smokeの`human-preview.json`、`approval-preview.json`、`status.json`と`run/human/llm.sqlite` |
| `python3 -B tests/b1_smoke.py --probe-installed-cli` | `--version`正常終了せず、help未実施。`.local/b1-cli-probe-swypq4my/summary.json` |
| `git diff --check` | PASS |

初回57 testsではB1に5 failure/4 error。Fake scriptを開く際のCPython FIOCLEXを新launcherが拒否していた。
local binaryのdisassemblyと隔離内の固定診断で原因を確認し、FDのclose-on-exec設定だけを許可した。
1回の修正・再実行で57 PASS。その後の差分確認でEOF後停止監視を補強し、親kill検証を追加して58 PASS。
Worker/B0のexec/socket禁止を緩めた修正ではない。最終Smokeはこの補強後のコードで実行した。

probeは合成canaryのread/write、TCP/IPv6/UNIX socket/socketpair、fork、親signal、ptrace/process_vm/pidfd/io_uring拒否、
環境の許可範囲、専用tmpのwrite正対照を確認した。通常credential、他repo、実UNIX socketへ接続していない。
既存B0の43 probeを実Claudeの証明に転用していない。カーネル攻撃・全syscall・全alias経路の完全監査ではない。

## 実CLI境界の確認範囲

インストール済み候補path: `<HOME>/.local/share/claude/versions/2.1.273`。
SHA-256: `6c752e2cc7c110c9df15f26d8d134d438c5ae95dbd610efc1a308bf7f9c5f6c1`。
`2.1.273`はファイル名由来で、version出力による確認ではない。
一時HOME/CLAUDE_CONFIG_DIR、env allowlist、Landlock、socket拒否の下で`--version`のみ試したが成功しなかった。
失敗の詳細原因は未確定で、raw stderrは保存しない設計だったため診断を完了していない。
通常HOME、認証、networkを開いて再試行していない。無断更新もない。

公式公開5資料との照合結果は[references](references.md)、境界とdigest範囲は[design](design.md)を参照。
safe-modeでは認証を維持するが管理policy由来の処理が残る。bareはサブスクloginを使わないため選ばない。
tools空/strict MCP/設定/独立HOMEは意図であり、実binaryの実効Tool一覧や管理policy不存在を証明しない。
offlineのsocket全拒否をそのまま実推論へ流用しない。実認証processに必要なcredential/refresh/通信を許す別profileが必要。

## B0 Findingsの扱い

| Finding | B1の判定 / 根拠 / 残Risk |
| --- | --- |
| P1-A real launcher | exec/stdio launcherを実装しFakeで検証。実binary互換性・認証経路はOpen |
| P1-B Codex設定 | Codex固有結果をClaudeの欠陥に転記しない。Claudeのsafe-mode/Tool/MCP/managed設定は公式資料で確認、実効性はOpen |
| P1-C egress/UNIX/interop/process | B1 offline exec後のsocket/外部ファイル/親操作拒否を確認。通信許可時のendpoint・UNIX・interop境界はOpen。UID必要性は未確定 |
| P1-D cost/internal retry | launcher一回、時間、入出力bytesは強制。金額/token/Provider request数/内部retryは強制していない。旧要求をClosedにしない |
| P2-A ledger/root再利用 | instance UUID/root結合。DB再作成・別rootの旧承認拒否を検証。DB全体rollbackや意図的改変は対象外 |
| P2-B timeout/kill | B1は開始後unknown、親kill後started保持、再permit拒否。B0合成のfailed/interruptedは互換のため維持 |
| TTL | format 2のTTLと絶対期限をdigestへbindし検証。起点は最初のpreview、challenge更新未実装 |
| stopのlock | B1 llm-stop/statusはrun lock中も使用可能。EOF後も監視。通常Signer stopの制約は変えない |
| 固定network/cost / provenance | B1はfixture明示、usage欠測null、API換算と実請求を分離。実通信・実費用の測定値はない |
| runner digest | B1は7 module＋binary＋fixture全file digest。OS/Python library closureは対象外、metadata秘匿も対象外 |

旧承認形式は履歴の検証用として保持し、run/retryは拒否する。新コードで既存Broker DBを開くと2 tableが追加される。
この作業では既存Smoke DBを開いて変更していない。検証はすべて新しい`.local` scratchを使った。
期限切れchallenge再発行、全DB復旧、Humanによる改変防止、未送信証明を持たない再送は実装していない。

## 要件変更案と初回Real利用へのGate

旧レビューの「Provider request数1を必ず照合」「費用/token/内部retryを必ず強制」は、
サブスクCLIから取得・強制可能とは確認できていない。launcher一回とProvider request一回を混同しない。
変更案は、強制できるlauncher回数・wall時間・bytesを承認し、内部処理は不明として明示し、
公式画面で追加使用OFFと実行前後の枠をHumanが照合すること。停止後の継続消費の可能性は残る。
この変更案にはIndependent ReviewとHuman判断が必要で、本BatchのFake成功を受諾や利用承認にしない。

1. 隔離した実binaryのversion/helpを成立させる。固定version/digest、必要なOS library・memory・thread動作を確認する。
   現在wall/CPU/outputは制限するが実CLIのresident-memory上限は強制していない。失敗原因を特定せず境界を広げない。
2. 専用の公式認証領域と必要なrefresh write/通信を設計・検証する。通常HOMEコピーやtoken抽出で代用しない。
   実auth status・loginが必要になる前に、その操作と送出範囲を説明して別のHuman承認を受ける。
3. Claude実効設定、管理policy、Tool/MCP/hooks/plugins/Browser/自動model切替/有料速度設定の制御を確認する。
   socketを許すprofileではendpoint制限とUNIX/interop/他process遮断を再probeする。UIDの選択はそこで判断する。
4. auth-statusとresult metadataの実versionでの形式を確認しmapper/profileを更新する。
   subscription認証・請求経路・追加使用OFFを別々に確認し、Sonnet/Opusの具体的model/Effortを選ぶ。
   制御できないretry/token/costを受け入れるか判断する。API/追加credits/別account/別modelへ自動fallbackしない。
5. 下記B1差分のIndependent Reviewを行い、オフライン受入れとReal境界の判定を分ける。
6. その後にHumanが一件のtask/request digest、送る資料/依頼、binary/契約/認証/model/Effort、
   TTL/期限、launcher一回、時間/bytes、未知の内部処理、停止/照合方法を具体的に承認する。
   実行前後の公式Settings > Usage（必要なら公式`/usage`）をHumanが確認し、時刻・欠測・他用途の消費も区別する。
   本実装は枠の非公開API、token抽出、追加hooksや監視基盤を導入していない。

## Independent Reviewで重点確認してほしい点

- `llm.py`: format 2/legacyの区別、起動前commit、単回permit、unknown/rejected/not_started、stop/EOF/親kill、再実行拒否。
- `claude_launcher.py`: exec継承のLandlock、library許可範囲、clone/tgkill/ioctlの引数制限、stdio以外のFD除去。
- `claude.py`: fixtureのみの実行fence、認証protocolが合成であること、allowlist環境、モデル/引用/usage検証。
- `worker.py`/Human Preview: 従来隔離を維持し、provenance改変・除去をHuman DBとの照合で拒否すること。
- Fake検証、実binary失敗、実auth未確認を混同していないこと。特に上の旧要求変更案とReal残Gate。

## 停止 / Recovery / 戻し方

Human CLIの`llm-stop`を実行し、`llm-status TASK`で確認する（詳細なprefixはREADME）。
ローカルkill/reapとProvider側停止/未消費確認は別。started/unknownは未送信に戻さない。
停止解除やTTL経過でも消費したpermitは復活しない。B1には再実行permit発行機能がない。
成功結果は元commit/runtimeで`llm-verify`後に`llm-export`で再handoffでき、推論を再起動しない。
DB、journal、snapshot、workspace、raw response、出力を一組として保全する。
コードを戻す場合も状態DBを巻き戻さず、元commitを別の承認済みcheckoutで確認する。
この変更の無効化にはB1コマンドを使わず停止markerを保持すればよく、OS設定変更の撤回は不要。

実credential読取り/コピー/抽出、実auth status/login、実Runtime推論、資料送出、追加消費、
Signer/Wallet/tclk、Technocore write、Scout/Observer変更、UID/sudo/Docker権限拡大、push/publishは行っていない。
公式公開Docs閲覧と、上記の隔離された無認証binary probeだけを実施した。
