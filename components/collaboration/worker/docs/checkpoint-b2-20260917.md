# CCW Batch B2 checkpoint — 2026-09-17〜18

開始: `main` / `90fea33ed2fd2166d42d7eabc2ee5a4140f0c46c` / worktree clean。
終了commitはこのcheckpointを含むcommit。Review範囲は`90fea33..HEAD`。
既存`.local`、B1入力、旧Evidenceは保持。新規Evidenceだけを追加した。
B1 Review原文はRepository内に見つからず、依頼文のFindingと現行Codeを基準にした。
指定されたB0再ReviewとB1 handoff、README/design/B1 checkpointを確認した。

**B2オフライン実装のcheckpoint。Independent Reviewは未実施、Real利用はNO-GO / DISABLED。**
実装者の検証であり、Real認証・資料送出・推論の承認ではない。

## 到達点

- `claude_real.py`: native targetの準備/検証、Real契約、専用auth homeとmetadata inventory、正式result mapper、隔離parser。
- `claude_real_launcher.py`: sealed memfdのhash/execveat、別のnative syscall profile、TCP-only online mechanics、
  全socket拒否のoffline診断。Brokerとonline launcher入口の両方にB2の無条件停止Gate。
- `llm.py`/`human.py`: `claude-real`のpreview/approve、単回ledger/停止/capture/validation/Evidenceの共通経路。
  `llm-prepare-claude-real`はbinaryと空auth homeの準備だけ。`llm-run-claude-real`は承認があっても停止する。
- `model.py`/`worker.py`: Real schema/Evidence種別のhandoff準備。WorkerのOS隔離・Human側DBとの比較は維持。
- Fakeは従来profileのまま。通常のHuman/Signer/Worker workflowとFakeの実行Smokeが成功。

Real契約はexecutable、sealed-object digest、version/helpのdigest、provider/auth mode、model/effort、cwd方針、
schema、環境、runtime profile、auth inventory、egress残Riskを含む。共通approvalがtimeout/bytes/TTL/単回permitをbindする。
`real_authorized: false`と`egress.human_accepted: false`は解除しない。実行Gateを変えるには後続のreview済み変更が必要。
外部資料・LLM出力からbinary/command/endpointを組み立てるAPIは追加していない。

## Verification / 保存Evidence

| 検証 | 結果 / Evidence |
| --- | --- |
| `python3 -B src/ccw/isolation.py` | Landlock ABI 3 |
| `python3 -B -m unittest discover -s tests -v` | 66 tests PASS、17.482秒。既存58＋B2 8。`.local/b2-final-verification-20260918.txt` |
| 先行66件のログ | `.local/b2-verification-20260918.txt`（17.773秒、fcntl制限追加前） |
| 既存Smoke | `.local/smoke-x4mbxe87/summary.json`、通常4ケース＋Scout＋B0 PASS |
| B1 Smoke | `.local/b1-smoke-mc8_mow2/summary.json`、Fake approval→launcher→parser→Worker→Human PASS |
| native最終成功probe | `.local/b2-probe-vg70xwcm/summary.json`、同rootの`unapproved-real-preview.json` |
| native syscall記録 | 同rootの`run/human/claude-runtime/{version,help}/syscalls.json` |
| 空HOME認証status | 同summaryの`empty_auth_status.loggedIn: false`。raw auth JSON/account fieldは保存しない |
| online policyの最終合成probe | `.local/test-b2-40d4glbm/boundary.json`、fcntlの他process signal拒否を含む |
| 差分検査 | `git diff --check` PASS |

native probeは`python3 -B tests/b2_smoke.py --profile`。固定インストールから新規領域へcopyし、
version/helpと空HOMEの非推論status確認だけを実行。全socket禁止、入力資料なし、実credentialなし。
実測targetのpreviewには固定公開Taskと合成のmodel選択例を使用した。**利用modelをHumanが決定した証拠ではない。**
そのpreviewをapproveしていない。承認なしrunを拒否し、attemptが0件であることを確認した。

テストではnative CLIを推論に呼ばず、合成resultでmapperを検証する。未知field、不正型、model欠測/不一致、
Tool使用表示、引用/schemaの不正は拒否。usage欠測はnull/欠測field名となり、ゼロと解釈しない。
Real targetを合成metadataでpreview/approveした場合も、無条件Gateがlauncher/attempt作成前に拒否する。
binary/version/profile/model/effort/timeout/auth mode変更をbindし、直接APIから編集したpreviewのapproveも拒否する。
timeoutはnative用の同じconfineを使う合成processをkill/reapし、自動再起動しないことを確認した。
親kill/unknown/再permit拒否/停止marker/EOF後停止は共有経路のB1検証を維持する。

最初の通常sandbox内テストではTCP socketの正対照だけが失敗した。CCW未適用でもAF_INET/INET6 socketが
EPERMだったため、Human承認の下で外側の開発sandbox制限を外して同じofflineテストを再実行しPASS。
TCPは作成して閉じるだけでconnect/sendしていない。CCW側のLandlock/seccompは適用したまま。
これはFull Accessへの切替、sudo、OS/WSL networking設定の変更ではない。

## 実binaryと同一object保証

- version stdout: `2.1.274 (Claude Code)`。
- 元path: `<HOME>/.local/share/claude/versions/2.1.274`。
- SHA-256: `15e2d05148f801b5774032faad87e624ecd172e9903288bda448b892eb58fa07`。
- copyは`ROOT/human/claude-runtime/claude`へ排他的に作成、mode 0500。既存targetは上書きしない。
- 起動時はそのcopyをopen→memfdへcopy→F_SEAL_WRITE/GROW/SHRINK/SEAL→同objectをhash→承認hashと照合→同FDでexecveat。
- version/helpもそのdigestを検査したsealed objectから得た。自動更新される元pathを起動時に探索し直さない。
- 0500のdisk copyは所有者に対してimmutableではない。path一致やinode番号だけをidentity証明にしない。
  変更されたdisk copyは起動時digest検査で拒否。seal後のsource差替えとmemfd write/truncate拒否を合成ELFで検証した。

実行objectのbytesを固定する保証であり、Providerの暗号学的証明、backend model revision不変性、
OS loader/library/kernel全体のhash closureではない。bootstrap/OS library/未隔離Humanは信頼する。
B1のFakeはhash検査後のpath execのままであり、そのTOCTOU保証を過大に表現しない。

## native profileの実測根拠と制約

| 分類 | 実装 / 実測 |
| --- | --- |
| thread | clone3→ENOSYS、cloneは`0x3d0f00`完全一致。helpでclone3のENOSYS後にclone 3回を観測 |
| event loop | eventfd2(290)、epoll_create1(291)、epoll_ctl(233)等。version/helpで実測 |
| FD | `fcntl(F_DUPFD_CLOEXEC, 1023)`を実測。nativeだけRLIMIT_NOFILE=4096。Fakeは128を維持 |
| signal | tgkillは自thread group、prlimitはselfのみ。fcntlはdup/get/set flagsに限定し、F_SETOWN/F_SETSIG/F_NOTIFY/O_ASYNCを拒否 |
| filesystem |専用home/tmp/authの通常read/write。OS libsと指定TLS/DNS file、当該processの`/proc/self/maps`だけ追加read |
| exec/process | sealed FD 3 + AT_EMPTY_PATH execveat。execve、fork/vfork、memfd_create、別FD execveat拒否 |
| network | online mechanicsはINET/INET6 TCP streamだけ。offline診断は全socket拒否 |

最初のnative probeはloaderへのexecute許可不足でEACCES。修正後はBun abort→timeout。
AGENTSの再試行上限で停止して報告し、Humanから追加offline診断・修正・再検証の許可を得た。
ptraceも外側sandboxが拒否したため別途承認の下で自身の診断子processだけをtraceした。
FD上限によるfcntl EINVALを解消した後もabortが残り、open先を調べて`/proc/self/maps`読取り不足を確認。
この単一fileを許可するとversion/helpが成功した。Bunのcrash-report URLへはアクセスしていない。
失敗Evidenceも`.local/b2-probe-ays2nt2t`、`vgr00odc`、`xg3devrx`、`qpgjllnq`、`76rwd7xk`、`519exuw_`に保存。
`prctl`のVMA命名、close_range、不要なproc/sys file等の拒否は、成功に不要だったため緩めていない。

syscall traceはmain threadのexecveat以降。thread内全syscallや実推論の実行面を網羅していない。
CPU=30秒、wall/stdio bytesは制限するがnativeのresident memory hard capは未実装。
静的seccompはexecveatのFD番号/flagsを検査し、FDが一度だけ使われることまでは強制しない。
当初FDはCLOEXEC、再memfd作成と子process生成は拒否するが、同じprocess内での自身/許可loaderの再execまで
完全禁止したという主張ではない。再execしてもLandlock/seccompは継承される。launcher回数と内部処理を区別する。

## auth home / parser / socket境界

auth homeは`ROOT/human/claude-runtime/auth`、所有UIDはtargetへ固定、mode 0700。
通常`~/.claude`のcopy/readはない。準備は空directoryだけ。実loginは後のHuman Gate。
launcherはrefresh用の通常read/writeを持つ。事前inventoryは既知の`.credentials.json`/`.claude.json`だけを許す。
symlink/hardlink・他UID・group/world access・特殊file・過大treeは拒否する。credential本文/hashをinventoryに含めない。
起動後は追加/消失/metadata変更を記録する。metadata inventoryはcredential内容の真正性を証明しない。
実loginが作る追加file/configは未検証なので、生成物がpolicy外なら停止して後続Reviewする。

runtimeの合成auth read/write正対照、Human/Signer/LLM DB/通常HOME/他repoのcanary read拒否、
隔離parserからauthを含む全canary拒否を確認。Workerには既存のworker-only Landlock/seccompを維持する。
AF_INET/INET6 stream作成は成功し、UNIX/NETLINK/UDP/raw/socketpairはEPERM。
pathname/user D-Bus/WSL interopは無害な代替pathを使い、socket作成段階のEPERMを確認した。
実WSL socket/実D-Busへ接続していない。Windows側interop悪用もしていない。
ptrace/process_vm/pidfd、親signal、fork/exec、io_uring等も拒否する。

## Findingの判定（実装者判定）

| B1 Real blocker | 判定 | 残り |
| --- | --- | --- |
| target/contract | Closed（offline実装） | Real authorizationはfalse、後続Gateが必要 |
| native起動/profile | Partial | version/help/empty authは成功。実通信/推論の全syscallは未確認 |
| UNIX/interop/egress | Partial | socket domain/他process境界はprobe済み。vendor endpoint制限なし、通信互換性は未検証 |
| 専用auth home | Closed（準備と合成検証） | 実login/refresh/実生成物の確認はOpen |
| Real output mapper | Closed（公式形へのoffline mapper） | 実response未検証。strict拒否の可能性あり |
| binary identity/TOCTOU | Closed（main ELF object） | OS library/kernel/bootstrapは信頼基盤 |

P3: clone3 fallback誤記、dedicated UID必須の断定、cost 0の誤解、B1 identityの過大解釈、not_startedの意味を修正。
approve APIは現行preview再生成との一致を検査する。challengeの起点/更新方式は再設計していない。
Humanによる意図的DB改変、全Evidenceの暗号証明、汎用recovery、全P3 hardeningへ拡張していない。

## egressと次のHuman Gate

案(a)のnetns＋vendor限定proxyは未実装。netnsだけでは外へ到達できず、proxy/到達経路の追加が必要。
このBatchではhost権限・新UID・ネットワーク設定を変更せず、巨大proxy基盤は作らなかった。
案(b)のTCP-only boundaryはコード/合成probeまで実装したが、**残RiskへのHuman承認は未取得**。
任意TCP宛先、loopback/LANのサービス、専用credentialにアクセスできるruntimeからの流出可能性が残る。
UDPを閉じるため、DNS over TCPを含むresolver互換性、TLSと認証refreshの通信は未確認であり、
現時点でonline動作可能/vendor-onlyと表現しない。HTTP redirect/proxy環境を許可した証拠もない。

実login前には、専用homeの場所/所有者/権限、runtimeがcredentialを読書きすること、
通常HOMEを流用しないこと、login時のネットワークと公式画面の契約/Extra usage OFFの照合を説明し、別承認を得る。
それを初回推論の承認へ転用しない。初回推論前には公開Task/request digest、送出範囲、
完全model ID/effort、binary/version/profile、通信案、単回launcher/wall/bytes/期限、停止方法をHumanが確認する。
金額/token/Provider内部request/retryを完全強制する要件には戻さない。観測不能な値はnull。
API換算額はsubscription billingではない。利用枠/実請求/Extra usageはHumanが公式画面で照合する。
strict mapperにより消費後rejectedとなり得ること、timeout後のProvider処理停止を証明できないことも承認対象。

## Independent Review重点 / Recovery

1. 同一memfdのseal/hash/exec、元インストールからの自動追従がないこと、execveat残余面。
2. native BPF引数filter、clone3 fallback、fcntl経由signal、FD整理、Landlockのloader/maps/library範囲。
3. auth inventoryの限界とparser隔離、実authを使わず検証したこと。
4. request/approval binding、無条件live Gate、単回ledger、after-inventory異常時のunknown保持。
5. mapperのversion互換性・完全model比較・欠測/未知field・API換算表示と、実Tool面未検証の扱い。
6. TCP-onlyをendpoint限定と誤認させないこと。案(b)の受容または案(a)への変更はHuman判断。

`human llm-stop`で停止marker、`llm-status TASK`で状態確認。local kill/reapとProvider停止は別。
started/unknown/rejected/not_startedはいずれも再permitを自動発行しない。startやTTL経過でも復活しない。
not_startedは開始通知未観測であり、あらゆる中断で未実行を証明する状態ではない。
DB/journal/snapshot/workspace/raw response/Evidenceを保全。DB rollback、新rootによる再送、既存runtime上書きをしない。
成功結果の再exportは元commit/runtimeで照合して行う。コード変更で古いEvidenceが検証停止する制約は継続する。

実credentialの読取り/コピー/抽出、実login、通常accountのauth status、実Runtime推論、Provider資料送信、
追加課金、Technocore/Signer/Wallet/tclk、Scout/Observer変更、sudo/UID/Docker権限拡大、host設定変更、push/publishは行っていない。
公開公式Doc閲覧、空認証CLI診断、合成テストのみ。local commitで停止する。
