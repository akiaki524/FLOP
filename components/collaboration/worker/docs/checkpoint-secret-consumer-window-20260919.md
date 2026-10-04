# Secret Handoff P1-A′ consumer ready前window checkpoint（2026-09-19）

開始状態：`main`、HEAD `85d8ce4227d65ae92338d91eb52ae13243dc2127`、staged/unstaged/untracked変更なし。
対象はIndependent ReviewのP1-A′のみ。BのSIGKILL残Risk、Secret Manager再設計、CのCodex経路は対象外。
実Secret、実op/vault/login、credential、Provider通信、software installation、root/別UID/host設定変更、
Signer/Wallet/Technocore writeは行っていない。push/tag/PR/releaseなし。

## 結果

| Finding | 状態 | 閉じた範囲 | 残Risk / Host依存 |
| --- | --- | --- | --- |
| P1-A′: consumer ready前のstdio pipe再open | **Closed（同UID siblingの`/proc/PID/fd`再open経路）** | consumer stdin/stdout/stderrを匿名AF_UNIX socketpairへ変更。ready偽装・stdin競争・echo読取りの前提となる再openはENXIO。正常handoff維持 | `pidfd_getfd`/ptraceはPTRACE_MODE_ATTACHで、Yama `ptrace_scope=1`では祖先（Human terminal側）とrootに限られる。ready前consumerのmemory/FDを祖先が取得することはresolverと同じHost policy依存 |

これで「consumer ready後のみSecretを渡す」は、同UIDの非祖先processに対してコードで成立する。
ready byteはlauncherが作ったsocketにしか書けず、値はlauncherとconsumerだけが持つsocketにしか流れない。

## Failure

consumerはexec直後、自身で`PR_SET_DUMPABLE=0`を呼ぶまでdumpable（exec時にkernelが戻す）。
Yama `ptrace_scope=1`はPTRACE_MODE_READの`/proc/PID/fd`を制限しないため、同UID siblingが
その間に`/proc/PID/fd/0`（stdin pipe）と`fd/1`（stdout pipe）を再openでき、再openしたFDは
consumerが後からnon-dumpableになっても使える。launcherのready判定はstdout pipeの先頭byteが`R`であることだけなので、

- **ready偽装**：`fd/1`をO_WRONLYで開き`R`を書くと、launcherは隔離前のconsumerへ値を書き、攻撃者が`fd/0`再openから読む。
- **stdin競争**：偽装しなくても、`fd/0`再openとconsumerの`read`が同じpipeを取り合う。

どちらの場合もconsumerは値を受け取れず失敗記録になる。つまり検知はされるが、取得は防げなかった。

## Fix

- `secret_handoff.exchange()`：`await_ready`（Secretを入力するconsumer）ではstdin/stdout/stderrの3本すべてを
  一方向の匿名socketpairにする（launcher側・child側それぞれ逆方向をshutdown）。launcher側は素のFD objectで持ち、
  stdinのcloseで唯一のlauncher参照が閉じてconsumerがEOFを受ける。resolver経路（stdout/stderr socket、空stdin pipe）は不変。
- `isolation.enter(secret_peer=PID)`：旧`stdin_pipe=True`を置換する固定consumer専用opt-in。fd 0/1/2がすべて
  無名AF_UNIX stream socketで、`SO_PEERCRED`が同UIDかつ指定PID（launcher）であることを要求し、pipe/tty/他socketは拒否。
  既定（Worker/既存runner）はstdout/stderrのpipe/tty必須とstdinの/dev/null化を従来どおり維持し、socketを許さない。
  Landlock/seccomp（socket作成・exec拒否）、close_range、no_new_privsは変更なし。
- `secret_consumer.py`：`secret_peer=parent`を渡す。parent PID確認、parent-death signal、non-dumpable化、
  rlimit、隔離後のready byteの順序は不変。
- 公開`consumer.secret_input`を`stdin-socketpair-to-process-local-environment`、`process.consumer_stdio_transport`を追加。
  既存confirmation digestへbindされる。

retry、fallback、Secret loggingの追加なし。consumer出力は従来どおり両stream破棄。

## same-path Verification

`tests/secret_consumer_window_probe.py`。Reviewの攻撃を再現可能なprobeにしたもの。
launcherのsibling（祖先ではない同UID process）が、consumer workspace pathをcmdlineで見つけ次第、
`/proc/PID/fd/0,1,2`をO_RDONLYで再openし、forgeでは`fd/1`をO_WRONLYで開いて`R`を書く。
その後5秒間、stdin（とstdout/stderrのecho）から値を探す。stdout再openはready交換後にだけdrainし、probe自身がready byteを奪わない。
基準`85d8ce4`の`isolation.py`/`secret_consumer.py`/`secret_handoff.py`を`.local/`へ取り出し、同じprobe・同じfixtureで比較する。
値はlauncher memory内で生成し、保存するのはboolean/errno/公開metadataだけ。

| 観測（trial数） | 基準85d8ce4 | 修正後 |
| --- | --- | --- |
| forge：fd 0/1/2再open | 5/5成功 | 5/5すべてENXIO |
| forge：`R`偽装 | 5/5成功 | 0/5（書込用再openもENXIO） |
| forge：dummy取得 | **5/5** | **0/5** |
| race：fd 0/1/2再open | 10/10成功 | 10/10すべてENXIO |
| race：dummy取得 | **10/10** | **0/10** |
| 正常handoff（成功記録・launcher exit 0） | 0/15 | 15/15 |
| attempt消費 | 15/15 | 15/15 |

修正後は、EACCES（window後に来た）ではなくENXIOであることを全trialでassertする。ENXIOは攻撃者がconsumerの
dumpable windowで`/proc`アクセスに成功し、対象がsocketのため再openできなかったことを示す。
Evidence：`.local/secret-consumer-window-u369ls5h/summary.json`（PASS）。同ディレクトリにdummy形式の文字列がないことを確認した。
kernel `6.6.87.2-microsoft-standard-WSL2`、UID 1000、Yama `ptrace_scope=1`。

## 関連Verification

| Command | 結果（すべてoffline） |
| --- | --- |
| `python3 -B src/ccw/isolation.py` | Landlock ABI 3 |
| `python3 -B -m unittest discover -s tests -v` | **96 tests OK**（32.6秒） |
| `python3 -B tests/secret_consumer_window_probe.py` | **PASS** `.local/secret-consumer-window-u369ls5h/` |
| `python3 -B tests/secret_abc_probe.py` | **PASS** `.local/secret-abc-0wdjzxwc/`（A/B不変） |
| `python3 -B tests/secret_handoff_probe.py` | **PASS** `.local/secret-handoff-dml9kq47/` |
| `python3 -B tests/secret_boundary_probe.py` | **PASS** `.local/secret-boundary-hk8_83jx/`（consumer memory/fd denied、exit 0） |
| `python3 -B tests/smoke.py` / `tests/b1_smoke.py` | **PASS** `.local/smoke-eppdzgvx/`、`.local/b1-smoke-606b3qp1/` |

Testの更新：consumer直接起動testとboundary probeのconsumer caseをsocketpairへ変更した（consumerはpipeを拒否するため）。
`test_consumer_receives_value_only_after_ready_byte`へ、pipe stdioではready byteを出さずexit 2になるassertを追加した。
初回のboundary probeは、旧pipe起動のconsumer caseがこの拒否でBrokenPipeになり停止した（仕様どおり）。
このsessionの今回の実行はsandbox内でPASSし、承認外の権限は使っていない。

## 残Risk / 未確認

- 祖先processやrootによるready前consumerへのptrace/`pidfd_getfd`はHost policy依存（resolverと同じ）。Agent外Human terminal起動条件を維持。
- 実op経路は不変。実opのsocket stdout互換性は引き続き未確認。
- Bの残Risk（SIGKILL/native crash、HUP/INT/QUIT/TERM以外の既定終了signal、exchange外区間でのfailure記録なし終了）は
  [A/B/C checkpoint](checkpoint-secret-abc-20260919.md)の後続訂正のとおりで、今回変更しない。
- CのCodex経路（文字列policyと`runner.py`の実env）は対象外のまま。
- 前[修正checkpoint](checkpoint-secret-handoff-fixes-20260919.md)の「consumer：ready byte前は値を受け取らない」は、
  本修正以前は偽装readyにより成立していなかった。

## Independent Review

差分：`git diff 85d8ce4..HEAD`。特に`isolation.enter()`の既定経路不変、socketpairの方向・close・EOF、
`SO_PEERCRED`検査、probeのENXIO判定と基準比較を確認する。
