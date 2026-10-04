# Host OS Boundary: SIGSYS failure / probe recovery — 2026-09-22

## Scope and cause

開始HEAD: `905e61a`、worktree clean。Real Connection Offline Integrationのsetup失敗に限定した変更。
既存Historical Evidenceは修正しない。Real Secret access / External Write / push はすべて0。

Humanの実測: `DUMMY_OFFLINE`、`passed:false`、`failed_step:setup`。
Agentのread-only host確認: `collab-boundary-probe.service` は `Result=core-dump`、
`ExecMainCode=3`、`ExecMainStatus=31` (SIGSYS)。配置済みunit/probeは旧版と一致。
Human報告のx86_64 audit syscall 56はcloneであり、Pythonの`os.fork()`に対するseccomp禁止動作と整合する。
禁止失敗ではなく、probeのOSError捕捉とprocess終了型の拒否が一致していなかった。

hostで確認した既存資源:

| 資源 | 所有者 / mode |
| --- | --- |
| `/var/lib/collab-connection-setup` | root:root / 0700 |
| `/var/lib/collab-boundary-probe` | UID 995 / GID 987 / 0700 |
| `/usr/local/lib/collab-boundary-probe` | root:root / 0755 |
| 配置済みprobe.py / unit | root:root / 0644 |
| worker / approval / signer / transport | UID 999 / 997 / 995 / 994。専用groupは追加memberなし |
| unit | `ActiveState=failed`、期待FragmentPath、DropInPaths空 |

root保護のbefore.json・dummy.credの内容はAgent未読。sudo非対話認証はpassword required。
これらは下記Humanコマンドのpreflightで照合する。AgentはOS user・配置済みunit・host stateを変更していない。

## Fix

- probe専用unitを `deploy/connection/probe.service` に分離。
  `clone:EPERM clone3:EPERM fork:EPERM vfork:EPERM` に限定して、禁止時にEPERMを返す。
  `@debug @mount @reboot @swap execveat` のSIGSYS動作は維持する。
  `SystemCallErrorNumber=EPERM` の一括指定は使用しない。
- Signerその他の実運用service生成コード・runtime policyは変更しない。
- probeはEPERMのみをfork拒否PASSとし、errno、UID、NoNewPrivileges、INET拒否、
  dummy fingerprint、env/argvへのraw/hex/base64/base64url不在、実行IDを保存する。
- setupは全測定・cross UID・dummy fingerprintを検査し、不成立ならinstallへ進めない。
- oneshot終了後のsystemctl InvocationIDは空になりうるため、そこへの照合は使用しない。
  起動前の結果はroot保護領域へ一意名で保全移動し、起動後の新規regular fileだけを採用する。
- checkpointは古いsetup結果を流用せず、既存setupには明示的な限定recoveryを実行。
  checkpoint結果も一意名で保存し、旧 `host-checkpoint.json` を上書きしない。
  失敗時は静的failure codeを報告し、raw stderrや秘密値を出力しない。

設定仕様はhostのsystemd 255のローカルmanual、および
[Ubuntu systemd.exec manual](https://manpages.ubuntu.com/manpages/noble/man5/systemd.exec.5.html) の
SystemCallFilterのsyscall別errno指定を参照した。

## Partial setupからの限定Recovery

`setup.py --recover-probe` は次を確認してから更新する。

1. root所有0700のsetup記録、before.jsonの「既存資源なし」記録と4ユーザー作成完了。
2. 4役の専用UID/GID、nologin、他ユーザーとのUID/GID共有なし、追加group membershipなし。
3. 既存暗号化dummyの所有者・mode。既存probe pathの所有者・mode・非symlink。
4. unitはinactive/failed、期待FragmentPath、drop-inなし。
5. unit/probeは固定SHA-256の既知旧版または今回版。未知の編集は拒否。
6. downstream deployment/config/signer state/socket directory/unit/installation markerが存在しない。

確認後、既存dummyをanonymous pipeで復号し、メモリ上でfingerprintを得る。再生成しない。
旧unit/probe/before/暗号化dummy/存在する旧結果をroot保護の
`/var/lib/collab-connection-setup/probe-recovery-*/` にhash manifest付きで保全・fsyncする。
probeと専用unitだけをatomic replaceし、daemon-reload → reset-failed → probe実行 → 結果検証へ進む。
途中の片方更新で中断しても既知旧版/今回版の混在を照合して再開できる。
setupディレクトリにflockを取り、同時recoveryを拒否する。
ユーザー再作成、dummy再生成、state/nonce/custodyの削除・rollbackはしない。

この経路は今回の**setup段階**のFailure用。install以降のpartial deploymentや未知の変更は保全して停止する。
万能な再インストールや自動rollbackではない。root管理者の並行した構成変更は行わず、checkpointは1回だけ実行する。

## Verification

| 検証 | 結果 / 限界 |
| --- | --- |
| 新規boundary/recovery targeted | 18/18 PASS |
| 実kernel seccomp・旧動作 | 子processがSIGSYS終了、結果ファイルなしを再現 |
| 実kernel seccomp・修正動作 | fork EPERM、probe正常終了、結果保存、dummy読取、env/argv不在、AF_INET拒否、NoNewPrivileges PASS |
| 上記の実施identity | 通常のローカルUID。専用service UID・systemd credential配送の再実測ではない |
| recovery fixture | backup/hash保全、混在版から再開、stale結果拒否、未知unit/probe、symlink、mode/owner、group/UID共有、wrong dummy、downstream存在、override拒否 PASS。systemdはmock |
| custody / nonce targeted | 12/12 PASS。SIGKILL後の同一preimage/reservation復旧を含む |
| Approval / policy / credential pin | 19/19 PASS |
| Signer integration | 91/91 PASS、public writes 0 |
| 既存regression | 108/108 PASS、48.704秒。Signer 91項目を内包し重複加算しない |
| 実Unix socket IPC | 4/4 PASS。通常host UIDで実施 |
| unit構文 | `systemd-analyze verify deploy/connection/probe.service` PASS |
| Historical Evidence | 11,946 files、変更0 |
| 実systemd専用UID / credential / cross UID / service crash-restart | **修正後はHuman実行待ち** |

Evidence: `.local/batch17a1/boundary-fix-{targeted,regression,signer,approval,historical}.json`。
最初のcustodyテスト実行はTAP出力をJSON専用evidence wrapperに渡して記録処理が失敗した。
直接実行で12/12 PASSを確認済みであり、これを製品test failureとは扱わない。
実kernelテストはlibseccomp経由の同等拒否規則を子processに設定する。
新unit全体をsystemd配下で実行した結果と混同しない。

## Humanの次の操作

```bash
sudo python3 -I $(git rev-parse --show-toplevel)/components/collaboration/agent/deploy/connection/host_checkpoint.py "$(command -v node)"
```

これはsetup recovery → dummy deployment → offline smoke → UID/credential/env/argv/journal/IPC検証 →
Signer SIGKILL/lock recovery/restart → reconciliation smokeを1回実行する。
結果はstdoutと `.local/connection/host-checkpoint-<id>.json` に出力する。
setup成功後はroot setup領域にも同名の結果を残す。
`passed:true` が得られるまでHost OS Boundary全体の修正後PASSを主張しない。
Real Secret / External Writeの有効化は含まない。

## Independent re-review

Primaryとは別AgentのGPT-5.6 Solが固定差分をread-only reviewした。
対象6ファイルのpatch SHA-256:
`8d46bc9eca7d85ea58939b6c8f7eb32a92a0aa057ec78f6237ee3161d788ba93`。
この節の追記前のartifactであり、レビュー後に実装は変更していない。

初回指摘はoneshot終了後InvocationID、役外UID重複、fresh unitのumask依存の3点。
すべて修正後に独立再確認され、**追加blockerなし**。
Reviewer自身も新規18件、unit構文、Python構文、staged diff check、artifact hashを確認した。
root/systemd経路のため影響は大きく、実ホスト未実行という検証限界は維持する。
このレビューは実ホストPASSやReal接続の承認を意味しない。
