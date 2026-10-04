# Gate B: Real Project DID identity-only host gate（Human実行待ち）

調査開始HEAD: `724cf9e52bebec27239e3ab02d379e13a7d917a8`、開始時worktree clean。
Gate Aの実host PASSはHumanから報告済み。今回CodexはReal Secret・host credential・
保管元にアクセスせず、host activationも行っていない。**Gate B PASSはまだ主張しない。**

## 既存経路と最小追加

`handoff_real.mjs`、`real_credential.mjs`、Real config/drop-in templates、SignerとTransportは無変更。
Gate A runnerは固定fakeと`REAL_CREDENTIAL_DID_MISMATCH`を成功条件にしており、Real起動へ転用できない。
`gate_b.py`は既存Gate Aのpreflight、atomic write、config生成、Transport拒否試験、
Gate A probeのACL判定を再利用する限定coordinator。
`gate_b_measure.py`はHumanが実行したrunner内でだけ、live Signerのmount namespaceで計測する。
新しいUID、ネットワーク権限、Secret Manager、DID override、write有効化経路は追加しない。

## 実行条件とhost変更

- 開始条件は全unit inactive、元のDUMMY配置・manifest・runtime pin整合、Gate A resultがPASS/rollback済み。
- 既存Real credential、Gate B transaction、Project DID ledger、Real custody、非空Transport stateを拒否する。
- 設定の`externalWriteEnabled`がtrueなら、起動前にSTOPと固定Real暗号文の除去を行いFAIL。
- root-only `/var/lib/collab-connection-setup/gate-b/`へ変更前bytesとtransactionを保存する。
- 配備済みmjs/pinをcheckoutの版へ一時更新し、configを固定DID・REAL_ACCEPT_PREPARATION・write=falseへ変更する。
- Signer/Transportに`gate-b.conf` drop-inを一時追加する。Signerは従来のPrivateNetwork=yes・AF_UNIXのみ。
- Humanのpipeを既存handoffへ直接渡す。Python coordinatorは入力seedを読まない。
- `/var/lib/collab-connection-setup/project-seed.cred`へ暗号文だけを排他的作成する。
- internal worker/gate/transport socketsとSigner/Transportを起動する。Human/Worker-control入口とWorker/Approvalサービスは起動しない。
- 成功・通常失敗とも、ingress → Transport → Signer → 他serviceの順に停止し、Real暗号文を除去し、元の配置/configへ復元する。
- 最終状態は`DUMMY_CONFIG_ALL_STOPPED`。boot enable、自動再開、push、外部送信はしない。

**stateは元に巻き戻らない。** 起動だけでもProject DID用ledgerが作成される。
既存ledger/nonce/custodyはbyte単位の不変を検査し、新しいReal ledgerも保存する。
ACCEPT/admitを呼ばないのでquotaConsumed=false、admission=null、actions=[]、Real custody未作成を要求する。
暗号文の除去はcryptographic revokeではなく、共有systemd host keyも変更しない。

## Secret入力のexact format

Ed25519 **生の32 bytesちょうど、その後EOF**。改行・BOM・JSON・hex/base64文字列・
64-byte secret key・mnemonic・PEMは受け付けない。Human側の保管形式が違う場合の変換は
Humanが自分の保管元に適合する方法を選ぶ。ここでは保管元や取得コマンドを指定・推測しない。

stdinはanonymous pipe限定。TTY、通常ファイル、named FIFOは拒否する。
内部handoffはFD入力も実装済みだが、今回runnerの公開入口はstdinだけ。
入力が32 bytesでも固定DIDに一致しなければ暗号文を作成せずFAIL。

固定DID:
`<PROJECT_DID>`

## Human terminalで実行

Secretをチャット、shell変数、argv、environment、history、plaintext fileへ置かない。
端末のtrace/入力記録やsudoのI/O入力loggingがある環境ではSecretを流さない。
`sudo -n`は認証promptによるstdin消費を防ぐが、sudo loggingを無効化するoptionではない。
このrunnerはHumanのSecret sourceそのものやterminal/sudo側の記録設定を変更・検証しない。

まずSecretなしでsudo認証を済ませ、pipeの右辺を定義する:

```bash
cd $(git rev-parse --show-toplevel)/components/collaboration/agent
sudo -v
set +x
set -o pipefail
gate_b_from_stdin() {
  sudo -n python3 -I $(git rev-parse --show-toplevel)/components/collaboration/agent/deploy/connection/gate_b.py run
}
```

Humanが選んだ「seedをargv/env/historyへ含めず、stdoutに生32 bytesだけを出す」既存の取得処理を、
この関数のstdinへanonymous pipeで接続する。以下の左辺は**置換必須の記号**であり、
実在コマンドや保管元の推奨ではない。Secretの値を書き込む場所ではない。

```text
HUMAN_SELECTED_RAW32_SOURCE | gate_b_from_stdin
```

EOFまでが入力。runnerのhandoff timeoutは120秒。`echo`、`tee`、file redirectへのfallbackは使わない。
Secret sourceをCodexが実行することはない。

右辺のargvはPython・runner path・`run`だけ。stdinを引数や変数へ展開せずNodeの既存handoffへ引き継ぐ。
handoff→systemd-credsもanonymous stdin pipe、子environmentはPATHだけ。
保存はsystemd host-key暗号文のみ。seedをstdout/stderrやEvidenceへ出力しない。
Humanが安全なsourceを接続する限り、historyへ残るのはsourceのコマンド名とpipeでありseed値ではない。
JS/Python heapやstream内部copyの完全zeroizationを保証するものではない。

## Expected result / Evidence

成功はexit 0かつsanitized JSONの次の条件を**全て**満たすこと:

```text
passed: true
signer.derived_did: <PROJECT_DID>
signer.boundary.mode: REAL_ACCEPT_PREPARATION
signer.boundary.externalWriteEnabled: false
signer.service.ActiveState: active       # 計測時。終了時は停止
signer.measurement.credential_32_bytes: true
signer.measurement.credential_isolated: true
signer.measurement.no_new_privileges: true
signer.measurement.seed_absent_env_argv: true
signer.measurement.seed_absent_journal: true
signer.cross_uid_read_denied: worker/approval/transport 全てtrue
transport / transport_after_signer: send_refused=true, state_empty=true
real_ledger: quota_consumed=false, admission=null, actions=[], custody_created=false
rollback: DUMMY_CONFIG_ALL_STOPPED
real_credential_removed: true
external_writes: 0
```

credential/親directoryのUID/GID/mode/ACLを記録する。別UID試験は同mount namespaceで
Worker/Approval/Transportへ権限を落としてopenし、PermissionErrorを要求する（内容を読まない）。
DIDはcredentialからderive・固定DID検証を済ませたproduction Signerの`boundaryStatus`で確認する。

Human-run計測子processだけが配送済みcredentialをmemoryで読み、Signer/Transportのlive env/argvと
両者のcurrent invocation journalを照合する。seedやseed hashを返さず、boolean/権限metadataだけを返す。
journalはraw/hex/base64/base64urlとJSON decodeしたMESSAGEを検査する。
それ以外のprocess・過去journal・外部terminal logや任意の変形表現の不存在は証明しない。
packet captureやkernel-level Transport egress禁止の実測でもない。

Evidence:

```bash
sudo cat /var/lib/collab-connection-setup/gate-b/result.json
sudo cat /var/lib/collab-connection-setup/gate-b/rollback.json
```

この2つのsanitized JSONとpipe全体のexit codeを返す。credential、backup、journal全文は返さない。
Human結果のレビューまでReal ACCEPT等には進まない。

## External Write OFFの確認

runnerはReal configの**boolean false**をhandoff前後・起動前・実測前後で再検査し、
SignerのboundaryStatusもfalseを要求する。Transportには固定の空payloadによる拒否確認だけを行い、
実際のACCEPT envelopeを作らない。既存Transportはwrite policyをpayload検証・state作成・送信より先に判定する。
Signer起動の前後で`CONNECTION_REQUEST_DENIED`、Transport active、attempt directory空を要求する。

終了後の元DUMMY configには`externalWriteEnabled` fieldが存在しない場合がある。
これはReal動作中のfalse検査を省略する意味ではない。最終状態はDUMMYかつ全停止。

## STOP / rollback

Ctrl-C等の通常signal/例外ではrunnerがcleanupする。SIGKILL・電源断やcleanup失敗後は、
同じcheckoutでHumanが次を実行する:

```bash
sudo python3 -I $(git rev-parse --show-toplevel)/components/collaboration/agent/deploy/connection/gate_b.py rollback
```

runnerがまだ動作中ならtransaction lockにより二重操作を拒否する。
先に元のrunnerをCtrl-Cで停止し、cleanup終了を確認する。
rollbackはingress → Transport → Signer停止、Real暗号文除去、その後に変更前bytesを復元する。
未知のfile変更を上書きしない。復元競合時でも停止とReal暗号文除去を先に試み、FAILを返す。
STOP失敗なら残るserviceがある可能性があり、credential file除去だけでmemory内seedは失効しない。
`FAILED_MANUAL_RECOVERY_REQUIRED`をPASSとして扱わずEvidenceを返す。
nonce/ledger/custodyの削除、巻き戻し、lockの手動解除は行わない。

## 再実行・次Gateの制約

このGateは一度だけ。既存transaction/Project ledgerがあれば`run`を拒否し、rerun optionはない。
P1-1修正後は、検証済みの空Project ledger・Real custodyなし・Transport state空・
不確定なidentity artifactなしの場合に限り、同じledgerを再利用して次回admit可能となる。
それ以外はreconciliation-onlyまたは起動拒否。詳細は
[空identityの局所修正](gate-b-empty-identity-fix-20260923.md)を参照。
通常はDUMMYとRealの2 ledgerが残り、DUMMY専用`recover_smoke.py`やGate A preflightは
`RECOVERY_STATE_UNSAFE`で拒否する。**Gate B後に旧Gate A rollback/recover_smokeを使わない。**
Real ACCEPTに進む前にはIndependent Re-reviewとGate Cのhost起動手順・承認が別途必要。
これを回避するためのledger削除、DID bypass、state resetは今回追加していない。

## ローカル検証

Real Secret不要、host変更なしのoffline checks:

```bash
python3 -B tests/gate_b_host.py -v
python3 -B tests/gate_a_host.py -v
node tests/real_credential.mjs
python3 -B tests/connection_gate_a.py -v
python3 -B tests/smoke.py
python3 -B tests/material_smoke.py
git diff --check
```

host systemd/credential実測はHuman実行待ち。offline testは実host PASSの代わりにはならない。

今回の結果: Gate B offline 15/15 PASS、Real credential 26/26 PASS、
固定production DIDの拒否/Transport write OFF 3/3 PASS、Gate A transaction/ACL 11 PASS・1 skip
（sandbox内tmpfs ACL未対応）。READMEのSmokeは5 runs PASS、Material Smokeは1 case PASS。
Python構文とgit diff --checkもPASS。実host systemd/Real seedによる成功経路は未検証。
CodexによるReal Secret access=0、host activation=0、External Write=0。
