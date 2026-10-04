# Secret Handoff Finding A/B/C checkpoint（2026-09-19）

開始状態：`main`、HEAD `bd7138d77415bd32f045c11e7fb69b851ee6658e`、staged/unstaged/untracked変更なし。
Human未確認変更との衝突なし。この文書を含むlocal commitが今回の変更単位。push/tag/PR/releaseなし。
対象は既知のA/B/Cのみ。実Secret、実op/vault/login、credential、Provider通信、software installation、
root/別UID/host設定変更、Signer/Wallet/Technocore writeは行っていない。

## 結果と境界

| Finding | 状態 | 閉じた範囲 | 残Risk / Host依存 |
| --- | --- | --- | --- |
| A: resolver stdout pipe exposure | **Closed（指定のFD再open経路）** | resolver stdout/stderrを匿名AF_UNIX socketpairへ変更。同UID siblingによる`/proc/PID/fd/1,2`再openはENXIO。正常handoff維持 | resolver自体のmemory/ptrace保護とは別。exec後dumpableのまま。信頼されたresolver・Agent外起動・hostのptrace policyが引き続き必要 |
| B: launcher abnormal death | **Partially Closed** | SIGHUP/SIGINT/SIGQUIT/SIGTERMを停止要求として扱い、resolver/setsid+二重fork子孫をkill・reap後に失敗記録 | SIGKILLは実probeで残存を確認。native crash、os._exit、OOM kill等のcleanup不能ケースも保証しない。新supervisor等は導入しない |
| C: actual/public environment drift | **Closed** | fixture/real Claude builderと公開policyのkey集合を比較。opの実exchange引数と公開environment_keysも比較 | testを実行することが条件。将来の新adapterは同じ検証へ追加する。値を公開policyへ記録しない |

「A Closed」はSecret Security全体やresolver memoryの保護完了を意味しない。
旧[修正checkpoint](checkpoint-secret-handoff-fixes-20260919.md)の「非子孫はptrace_scope=1でresolverを読めない」
という記述は、pipe FD再openには当てはまらない。今回、同じhost設定のまま旧実装で取得を再現した。
同文書の「Linux子孫は消える」という記述も、launcherがcleanupを実行できる終了に限る。

## 実装

- `secret_handoff.exchange()`：resolverのstdoutとstderrをそれぞれ匿名socketへ接続。
  受信側write/送信側readをshutdownし、余分なendpointを閉じる。path/listen/network endpointは作らない。
  consumerは既存のpipeを使い、non-dumpable・Landlock/seccomp適用後のready byteまで値を書かない。
  Worker側のFD検証、socket/exec拒否、Tool一覧は変更しない。
- 同関数の実行中だけ4種類のsignal handlerを設定し、pumpが停止要求を検出する。
  handlerは非同期例外を投げないため、Popen途中でchild handleを失わず、cleanup中の連続signalでもkill/reapを継続する。
  cleanup中だけに到着したsignalも成功として返さない。元のhandlerとsubreaper状態は復元する。
  専用launcherのmain threadで使う既存のprocess-wide scopeであり、汎用並行process managerではない。
- 公開process policyへtransport、対象signal、uncatchable cleanup未保証を記録し、既存confirmation digestへbindする。
- `test_secret_handoff.py`：actual/public key整合性・drift mutation、spawn/cleanup中のsignal、handler復元を追加。
  public builderがprivate環境値builderを呼ばない既存testも維持する。

Human confirmation/source固定/single-use、resolve前attempt消費、retryなし/fallbackなし、親environment非継承、
値のDB/Evidence/Preview/export/Git非保存、consumer ready後の入力を維持。
Real Claude Gateはdisabledのまま。Secret capabilityをWorker/LLM/Agentへ追加していない。

## Failure → Fix → same-path Verification

実process比較：`tests/secret_abc_probe.py`。基準Git objectからhandoff moduleだけを読み込み、
同じdummy resolver・現行の変更していないconsumer・同じprobeで比較する。productionの旧経路/fallbackは追加していない。
基準moduleのsourceは`.local/`のみに保存。baselineのfile位置は現行module位置として評価し、
consumer pathを維持する。古いRepository全体の再checkoutを検証したとは扱わない。

### A

resolverがmemory内で生成した無権限dummyをstdout/stderrへ書き、ready markerを作る。
test用read gateでlauncherの読取りを待たせ、同UIDかつlauncherのsiblingである攻撃processが
`/proc/RESOLVER_PID/fd/1`と`fd/2`をO_RDONLY|O_NONBLOCKでopen/readする。
値はargv/environment/file/診断出力へ渡さず、取得結果はboolean/errnoだけを保存する。

| 観測 | 基準bd7138d | 修正後 |
| --- | --- | --- |
| stdout dummy取得 | true | false / ENXIO |
| stderr dummy取得 | true | false / ENXIO |
| consumer成功記録 | false（probeがpipeの値を奪う） | true / launcher exit 0 |
| 実行後resolver/子孫 | なし | なし |

ENXIOを直接assertし、単なるFD不存在やprobe失敗を防御成功に数えない。
kernel `6.6.87.2-microsoft-standard-WSL2`、UID 1000、Yama ptrace_scope=1で観測。
このpipe再openの修正はYama設定変更に依存しない。別kernel/Windows bridge/実opのsocket stdout互換性は未確認。
既存memory probeではlauncher/consumerはdenied、dumpable controlとresolver memoryはfound。
resolver memoryへの祖先アクセスは既知の残Riskで、HumanがAgent外のterminalから起動する条件を維持する。

### B

resolverとstdioを切り離したsetsid+二重fork子孫が存在するmarkerを待ってから、launcherに実signalを送る。
launcher終了後、harnessによる回収**前**に`/proc/PID`の存在を検査する。生存だけでなく未reapの残存も失敗対象。

| Signal | 基準 | 修正後 |
| --- | --- | --- |
| HUP / QUIT / TERM | resolver/子孫残存、failure記録なし、signal終了 | 両方なし、failure記録あり、exit 2 |
| INT | 両方なし、failure記録あり、exit 2 | 同じ（既存のcleanupを維持） |
| KILL | resolver/子孫残存、failure記録なし | **同じ：未解決** |

全ケースでattempt消費済み、成功記録なし。SIGKILLでfailure.jsonがないことを再利用許可にしない。
probe自身をsubreaperにして旧実装/SIGKILLの残存を最後にkill・reapし、harnessの子孫がないことをassertする。
productionにこのharness保護があると解釈してはならない。
追加unit testでも、Popenがchildを返す直前のSIGTERMとcleanup中のSIGHUPを実際に自processへ送り、
child終了とhandler復元を確認。cleanupだけにsignalが届く場合も失敗する。

native crash、SIGSTOP、uninterruptible kernel wait等は未probe。Python handlerが動けない間の即時停止や、
IPC先desktop/helperなどlauncher子孫でないprocessの終了は保証しない。
これ以上を閉じるsupervisor/daemon/container/権限拡大は今回導入しない。

### C

fixture `claude.environment(workspace)`、real `claude_real.environment(workspace, auth_home)`のkey集合を、
各`environment_policy()["keys"]`と独立に比較する。auth_homeは未使用のdummy pathで、credentialを読まない。
親environmentへ無害なcanary keyを追加してもactual側へ継承されないこと、公開keysの重複なしも検証。
`OnePasswordSource.resolve()`がexchangeへ渡す実envはstubで捕捉し、public環境keysと比較する。opは実行しない。

fixture/real × actual追加/actual削除/public追加/public削除の8 mutationすべてが、
正常時と同じ整合性assertionでAssertionErrorになることを検証した。
これは意図したnegative testであり、test suite自体はPASS。key集合だけを比較し、環境値をEvidenceへ追加しない。
既存のPreview/ledger/Evidence/export非漏出testと、public契約がprivate builderを呼ばないtestもPASS。

## Verification記録

| Command | 結果 / Evidence（すべてoffline） |
| --- | --- |
| `python3 -B src/ccw/isolation.py` | Landlock ABI 3 |
| `python3 -B -m unittest discover -s tests -v` | **96 tests OK**、最終コード32.331秒 |
| `python3 -B tests/secret_abc_probe.py` | **PASS** `.local/secret-abc-h21khihn/summary.json`（最終コード） |
| `python3 -B tests/secret_handoff_probe.py` | **PASS** `.local/secret-handoff-6rjq7sh6/summary.json` |
| `python3 -B tests/secret_boundary_probe.py` | **PASS** `.local/secret-boundary-gj87i69f/summary.json` |
| `python3 -B tests/smoke.py` | **PASS** `.local/smoke-imbivapl/summary.json` |
| `python3 -B tests/b1_smoke.py` | **PASS** `.local/b1-smoke-b3wh9j5s/summary.json` |

ABC probe初回と、最終コード検証時の通常sandbox実行は同期待ちで停止した。
診断は匿名socket `shutdown`のEPERM。各回とも承認された通常ホスト権限での再実行でPASSし、
制限を外すcode変更、Full Access切替、sudo、host policy変更はしていない。
handoff/boundary probeも同じ理由で承認された実行環境を使用した。これをproduction側の権限要求とは扱わない。
途中のPASS Evidenceは`.local/secret-abc-hfzz7x7e/`にも残す。最終判定は上表の最終コードEvidenceを使う。
生成データはすべて`.local/`でGit対象外。commitには実装/test/docsだけを明示選択する。

## Real 1Password dummy Observationへの判断

**Offline Hardeningはこの限定範囲で終了できる。自動的に実op観測へ進める状態ではない。**
Independent ReviewでAの範囲とBの残Riskを確認し、HumanがSIGKILL等の残存リスクを受容するか判断する。
受容しない場合はBをOpen相当のlive gateとして停止し、別の設計判断へ戻す。今回そのarchitectureは作らない。

受容した場合でも、前checkpointの別Human Gateが必要：native Linux op/desktop認証経路の選択、
必要な導入許可、version/flag確認、dummy専用item作成許可、Agent外terminalからの一回実行。
実opのsocket出力互換性、空HOME/最小envでの連携、cache/background/内部retry、desktop認可の単位は未確認。
新しいattemptを自動生成して再送しない。Windows op.exe bridge、実credential、Claude注入は別Gateのまま。

## Independent Review / Recovery

差分は `git diff bd7138d..HEAD -- README.md docs/design.md docs/checkpoint-secret-abc-20260919.md src/ccw/secret_handoff.py tests/test_secret_handoff.py tests/secret_abc_probe.py`。
特にresolver outputのFD所有/close、signal設定からcleanup/復元までの範囲、baseline controlとSIGKILLの判定、
environment値が公開契約へ到達しないことを確認する。
abnormal終了後はconsumed attemptを保持し、resolver/子孫とresolver HOMEをHumanが確認する。自動再送なし。
rollbackは別途Human判断のlocal revertとし、consumed記録/Evidenceを巻き戻さない。

## 後続訂正（Independent Review後）

- 上記「A Closed」はresolver stdout/stderrの再open経路に限る。同Reviewで、consumerのexec〜non-dumpable化前に
  stdin/stdout pipeを同UID siblingが再openし、ready byte偽装または読取り競争でdummyを取得できることが実測された（P1-A′）。
  本文中の「consumerは既存のpipeを使い…ready byteまで値を書かない」は、偽装readyを考慮しておらず不正確だった。
  修正とsame-path検証は[consumer window checkpoint](checkpoint-secret-consumer-window-20260919.md)。
- Bのcleanup対象は、exchange実行中のHUP/INT/QUIT/TERMだけ。USR1/USR2/ALRM等の既定終了signalはSIGKILLと同じ扱い。
  exchange外の区間（resolve前・resolver終了後〜consumer起動前）は元のhandlerで、子processは存在しないが
  failure.jsonなしで終了しうる。いずれもconsumed記録は残り、再利用許可にはならない。
