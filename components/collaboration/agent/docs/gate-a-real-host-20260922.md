# Gate A — Real host activation rehearsal（実host実行待ち）

開始HEAD: `bb362c96168059a19a9399dad7003f447bf67bc1`、branch `main`、worktree clean。
この記録は **Gate A PASSではない**。実装とoffline検証まで完了したが、
`sudo -n true` は `sudo: a password is required` で終了した。
権限の迂回・sudo設定変更は行わず、rootの配備・実測は未実施。

## 開始・終了時の実host確認

- systemd: `running`。
- 設定: `DUMMY_OFFLINE`、既存Project DID固定。
- Worker / Approval / Signer / Transportは全てactive/running。
- 5ソケットは全てactive、サービスdrop-inなし。
- 4サービスのNoNewPrivileges=yes。SignerはPrivateNetwork=yes、AF_UNIXのみ。
- Signer StateDirectory: collab-signer:collab-signer、0700。
- setup directory: root:root、0700。deployment directory: root:root、0755。
- 終了時も4サービスは開始時と同じPID。host配備・設定・サービスの変更0。
- 最新の保存済み成功checkpoint:
  `.local/connection/host-checkpoint-1549b10ad031429fabac911a8ec5460c.json`。
  `passed:true`、DUMMY recovery、cross-UID拒否、IPC拒否、crash recoveryが記録されている。
  過去checkpointの内容確認であり、今回のReal credential実測ではない。
- 今回のread-only Evidence: `.local/gate-a/host-readonly-20260922.json`。

## 実装した実行経路

`deploy/connection/gate_a.py` は既存installerの配備先、
`recover_smoke.py` のunit rendering/preflight/atomic write/STOP/reset処理、
既存Real用config/drop-in templatesを再利用する。
新しいUID、deployment framework、signer protocol、secret managerは追加しない。

実行予定の一時変更:

| 対象 | Gate A中 | 復旧後 |
| --- | --- | --- |
| `/usr/local/lib/collab-connection/src/collaboration_agent/*` | 現checkoutのmjs/pin | 元の配備bytesへ復元、新規ファイルのみ削除 |
| `/etc/collab-connection/config.json` | REAL_ACCEPT_PREPARATION、既存DID、write=false | 元のDUMMY設定へ復元 |
| `/etc/systemd/system/collab-signer.service.d/gate-a.conf` | fake encrypted credential、計測用ExecStartPre | 今回作成したdrop-inと空directoryのみ削除 |
| `/etc/systemd/system/collab-transport.service.d/gate-a.conf` | 既存Real Transport template、StateDirectory | 同上 |
| `/usr/local/lib/collab-connection/gate_a_probe.py` | Signer UIDでの事前計測 | 削除 |
| `/var/lib/collab-connection-setup/gate-a-fake.cred` | root-only fake ciphertext | 削除 |
| `/var/lib/collab-connection-setup/gate-a/` | durable backup・transaction・sanitized Evidence | 保存 |
| `/var/lib/collab-transport` | systemdが既存Transport UID、0700で作成 | 空のinactive StateDirectoryとして保存 |

元のunit本体・Node binary・official runtime・deployment manifestは変更しない。
SIGKILL等で中断した場合に備え、変更前bytesとhashを先にroot-only領域へ保存する。
未知の上書きや非空Transport state、既存drop-in、既存Project credential、
boot-enabled unitは拒否する。既存のStateや歴史的Evidenceをリセットしない。

## Credentialと期待結果

固定の公開fake seed `bytes(range(32))` のみ使用する。入力seed指定は受け付けない。

```text
Python memory → anonymous stdin pipe → systemd-creds encrypt --with-key=host
→ gate-a-fake.cred (0600, root-only parent)
→ LoadCredentialEncrypted=project-seed:...
→ Signer credential directory
→ production Signer validation → REAL_CREDENTIAL_DID_MISMATCH → exit 70
```

plaintext persistent fileは作らない。`handoff_real.mjs` は先にProject DID一致を
要求する本番用経路なので、fakeを通すようには変更しない。Gate Aは固定fakeだけを
別のroot用rehearsal entrypointから暗号化する。productionのDID照合は無変更。
`ALLOW_TEST_DID`、production expected-DID override、alternate Identityは追加しない。

起動診断のclosed vocabularyへcredential未配送・不正サイズ・DID不一致等の
固定コードを追加した。例外本文を任意に記録せず、allowlist一致だけを採用する。
新しいinvocation IDとphase=CREDENTIALを照合し、古い診断でPASSしない。

`gate_a_probe.py` は同じSigner unitのExecStartPreで固定fake内容・32-byte・UID・
credential DAC・NoNewPrivileges・env/argvのfake不在を測定する。
root側はそのmount namespace内でWorker/Approval/Transport UIDによるopenを試し、
PermissionErrorを要求する。単なる/proc traversal拒否をcredential DACの証拠にしない。
その後に本来のSignerが独立してcredentialを読み、DIDをderiveして不一致で終了する。

**測定上の限界:** env/argvの直接取得はSignerのExecStartPre processを対象とする。
即時終了するSigner main processのenv/argvを直接捕捉する実装ではない。
mainのargvは既存unitで固定、seedはcredential file経由のみ。journalはSignerと
Transportについてraw/hex/base64形式のfake不在を調べる。未実行なので、いずれも
今回のhostで確認済みとは扱わない。

## Transport・STOP・recovery

TransportのsendAcceptは、payload validationより前にwrite OFF policyで拒否する。
固定の `{"operation":"sendAccept","value":{}}` を既存AF_UNIX socketへ送り、
汎用拒否・サービス稼働・attempt stateなしを確認する。URLやmessage入力は追加しない。
Transportを再起動して同じ拒否を確認する。

既存TransportのAF_INET/AF_INET6許可は維持する。今回追加のnetwork hardeningはない。
External Write 0の根拠はwrite=falseの固定config、validation/send前のpolicy拒否、
起動時に送信処理がない既存コード、空のattempt stateであり、packet captureや
kernelのegress禁止を実測したと主張しない。

Signerの実際の失敗状態をresetし、同じcredentialで再起動して再度DID_MISMATCHを要求。
Stateのhashを照合し、quota・custody・nonceのrollbackや自動送信がないことを確認する。
終了・通常例外時ともsocket→serviceの順でSTOPし、元のDUMMY設定・sourceを復元。
**成功時の最終状態は `DUMMY_CONFIG_ALL_STOPPED`**。DUMMYを自動で再開しない。
旧startup diagnosticは復元し、今回のdiagnostic/probeはroot-only Evidenceへ保存する。

## 検証結果

| 検証 | 結果 |
| --- | --- |
| 通常offline suite `test_*.py` | 133/133 PASS |
| connection suite `connection_*.py` | 70/70 PASS（新規Gate A 3件を含む） |
| Gate A transaction/rollback tests | 6/6 PASS |
| Real credential suite | 26/26 PASS |
| Real Transport suite | PASS |
| Gate A固定fake seedとproduction DID照合 | REAL_CREDENTIAL_DID_MISMATCH |
| Rendered base units + Gate A drop-insのsystemd-analyze verify | exit 0 |
| Python compile / git diff --check | PASS |
| 実hostのroot Gate A | **未実施: sudo認証が必要** |

最初のsandbox内connection実行ではAF_UNIX bindがEPERMとなり12 errors。
許可されたsandbox外のローカル一時領域で再実行し、70件PASS。
この再実行も実hostのsystemd・credential・別UID実測を代替しない。
rollbackテストはsystemd/account/pathをmockし、部分配備、未知の変更の保存、
冪等復旧、通常例外時のfinally cleanupを確認した。

## Humanによる実行と結果の返却

今回host変更の許可は既に受領済み。追加の作業範囲承認ではなく、現在のagentに
利用できないsudo認証が実行上の障害である。パスワードをチャットに渡さない。
このcheckoutからHuman端末で次を実行し、sanitized JSONをHuman/ChatGPTへ戻す。

```bash
sudo python3 -I $(git rev-parse --show-toplevel)/components/collaboration/agent/deploy/connection/gate_a.py run
```

中断またはrollback failure時:

```bash
sudo python3 -I $(git rev-parse --show-toplevel)/components/collaboration/agent/deploy/connection/gate_a.py rollback
```

Evidenceは `/var/lib/collab-connection-setup/gate-a/result.json` と同directoryの
`signer-1.json`, `signer-2.json`, `rollback.json` 等に保存される。
再実行は既存transactionを検出して拒否する。Evidenceを消して無条件に繰り返さない。
この未実測版で途中失敗したら、出力と保存済みsanitized Evidenceから局所修正する。

実hostでのcredential配送、UID/DAC、Signer mainの即時終了、journal、Transportの
socket activation、再起動・cleanupは未確認。Gate AはOPENのまま。
Real Secret access 0、External/Technocore write 0、ACCEPT/Delivery/REVEAL/value 0。
新たな重大Permission Boundary変更・Security Findingは確認されておらず、
Independent Reviewは起動していない。次はHuman/ChatGPTによる結果確認。

## 2026-09-22 追記: credential_other_permissions_absent=false の原因と修正

初回Gate A実行でprobeの `credential_other_permissions_absent` だけがfalseとなり、
ExecStartPreが終了したため、後続のroot nsenter/runuserによる別UID読取り試験は実行されなかった。

- 判定式は `stat.S_IMODE(st_mode) & 0o077 == 0` の**mode bitのみ**。
- systemd 255はnon-root serviceのcredentialをroot所有0400で作り、service UIDへ
  named-user ACL `user:<uid>:r--` を付与する（tmpfs、本kernelは `CONFIG_TMPFS_POSIX_ACL=y`）。
  ACLがあるとstatのgroup bitはACL maskを表すため、`group::---`/`other::---` でも0440に見える。
- rootlessのtmpfs再現で0400 + named-user ACL → stat 0440、probe判定falseを確認。
- 既存DUMMY checkpoint（`verify.py`）はmode bitではなく `runuser ... test -r` の実アクセスで測定していた。
- 実fileのuid/gid/mode/ACLはprobeがbooleanしか保存していないため直接は未観測。
  よって「probeの誤判定」が最有力の原因だが、実hostでの確定は次回実行の記録による。

修正（host/unit/permission変更なし）:

- probeはPOSIX ACLを読み、owner（rootまたはSigner）とSigner UIDのnamed-user read以外に
  アクセスが無いことを判定する。ACLが無い場合は従来どおりmode bit判定。
- file・credential directory・`/run/credentials` のuid/gid/mode/ACLを
  `credential_metadata` として保存する（内容は保存しない）。
- 別UID読取り拒否の実測（nsenter + runuser）は変更せず、引き続き必須。
- `rerun` は、rollback済み（`rollback.json` passed、`result.json` あり、fake/drop-in/probe無し）の
  transactionを `gate-a-attempt-N` へrenameして保存してから実行する。Evidenceは削除・編集しない。

```bash
sudo python3 -I $(git rev-parse --show-toplevel)/components/collaboration/agent/deploy/connection/gate_a.py rerun
```
