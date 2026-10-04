# Gate C: First Paper ACCEPTのHuman orchestration

今回のfunctional fixの開始HEAD: `5732f5f0b844ddc2d81500f1838d5fbda79fb525`。
実装・検証はローカルfake/offlineのみ。Real Secret access=0、実External Write=0、
sudo=0、実host activation=0、pushなし。
Claude Independent ReviewはHumanへ引き継ぐ。[レビュー資料](gate-c-independent-review.md)を参照。
**実host C1/C2を実行した、または実行承認を得たという意味ではない。**

## C1とC2の分離

C1は固定 `https://technocore.chat/r/tclk-offers/export` をpublic GETする。
redirect・圧縮・不完全framing・未知generationを拒否し、最大10MiB未満のexportと
その全体の正規化recordを件数による打切りなしで扱う。署名、canonical frame、OFFERの意味はofficial pinned tclkで検証する。
nonceは既存serverの末尾1MiB windowと整数型判定を使い、19桁decimal文字列を丸めない。

候補はPaper/hash/rails=[paper]/paymentKeyなし、payerからのOFFERに限る。
malformed、expired、self-offer、unsupportedは除外する。入力順を保ち、順位付けしない。
Humanはexact offer IDを選ぶ。選択後のfresh snapshotからACCEPTを固定する。
protocol上のPAPER amountは仮想Paper条件であり、実資産railへの接続はない。
job本文は公開資料として表示するだけで、Solver・Delivery等を起動しない。

固定packetにはOFFER record、公開nonce observation、ACCEPT line、contract、nonce、
session/action ID、payload digest、approval digest、document digestを含む。
C1はProject seedを使わない。ACCEPTのhash commitmentを先に固定するためのランダムpreimageは
memory/anonymous pipeだけで既存systemd-credsへ渡し、root-only暗号文として保存する。
public packetへpreimageを含めない。C1終了時はサービス全停止・write OFFのまま。

C2は `Arm → Human Final Approval → Final Revalidation → admit → sign/send ×1` の順。
Armでhistorical packet検証、既存handoff、DID一致、Signer/Transport起動、固定packet限定の
write policy準備を終える。この時点ではadmission/custody/action/nonce reservationなし、quota未消費。
その後 `/dev/tty` で同じ `SEND ACCEPT <document.digest>` を承認する。

C1のobservation時刻はEvidenceであり、Human reviewの30秒deadlineではない。
Human承認後に固定exportを新しく取得し、Final Revalidationでのみ30秒freshnessを要求する。
profile/generation/coverage、同一OFFERの存在・有効期限、公式protocol foldでproposedであること、
frozen ACCEPTのapply可能性、Project DIDのvenue nonce条件を確認する。
nonce条件はC1との厳密一致を維持し、変化していればadmitせずRETURN_C1。
transport nonceのlate bindingやpacket再生成は行わない。

公開export全体をcoordinatorで検証した後、既存64KiB IPCにはselected OFFERと関連protocol recordsだけを渡す。
関連recordsの切捨てはせず、上限超過はadmit前に拒否する。Signerもこの証拠を再検証する。
新しいobservationはC1 packetと別に扱い、C1のsource digest、payload、frame内nonce、venue nonce、
session/action ID、contract、approval/document digestは書き換えない。

**唯一のCommit Pointはadmit**。Final Revalidationはcustodyを作る前に完了する。
admit内で既存custodyを初期化し、検証済みfinal nonce observationを保存する。
admit後は30秒のobservation ageやlocal session timerだけで中断せず、同じactionをprepare/approve/sign/sendへ進める。
approvalのschema/digestは不変。bound Gate Cのapproval expiryだけを
`min(OFFER.expiresMs - acceptMargin, OFFER.claimByMs - claimMargin)` へ固定し、
短いlocal execution timestampをHuman承認へ持ち込まない。既存DUMMY/legacyのTTLは変えない。
OFFER自体のprotocol期限、DID/payload/approval mismatch、durable整合性のfail-closedは維持する。

markerはEvidence。過去attemptのrollback完了、credential除去、DUMMY/write OFF・全停止、
実際のsafe-empty ledger、custodyなし、Transport state空をすべて検証できれば、新しいUUIDでC1/C2を行える。
既存directoryやmarkerを削除・再利用しない。rollbackが未完了なら先に復元する。
admit以降のADMITTED/PREPARED/SIGNING/SEND_ATTEMPTED/AMBIGUOUS、custody-only、receiptは
actual durable stateが再試行を禁止する。markerの有無でsafe-emptyへ戻さない。
state削除・quota返却・nonce rollbackで再実行可能にしない。

送信は既存の固定Paper ACCEPT Transportを一度だけ呼ぶ。応答成功だけでは成功判定せず、
必ずAMBIGUOUSかつlogical STOPにする。そのSignerを停止状態のまま残し、public GET一回で
exact signed envelopeを照合する。署名、payload、DID、room、nonce、envelope digest、
seq/timestampが一致した場合だけRECONCILED_PRESENTをdurableに記録する。
不在・取得失敗・照合失敗ならAMBIGUOUS/STOPを維持し、poll/retry/resendしない。
照合後もaction開始は禁止し、cleanupで実serviceを全停止する。

## Claude 3 Findingsのclosure

| Finding | 原因 | 今回の局所修正 |
| --- | --- | --- |
| P1-1 | C1時刻を起動・実行・Transportまで30秒deadlineとして使用 | C1をhistorical検証し、ArmとHuman approval後のfresh snapshotを別にFinal Revalidation |
| P1-2 | admit後にもobservation age／local approval TTLを再判定 | admit内のcustody作成前に最終検証。以降は同じ検証済みobservation/nonceで進み、actual protocol期限だけ維持 |
| P2-1 | marker存在だけで全将来attemptを拒否 | rollback完了＋現在のsafe-empty/credentialなし/write OFF/全停止を条件に新UUIDを許可。過去Evidence保持 |

新しいledger schema、ADMISSION_STARTED authority、transaction framework、retry engineは追加していない。
Arm中に作られる既存identity ledgerのrevisionは進み得るが、admission/custody/quota/sendは開始しない。
サービス準備失敗やfinal validation拒否ではcleanup後にactual stateを再確認する。
後続の正常起動が示すsafe-emptyだけが再試行根拠であり、例外名やmarkerだけから推定しない。

時間回帰はreal sleepで進めず、pure関数のnowMsとコピーしたtest fixtureのclock.jsonで注入する。
C1→61秒経過→fresh final→送信、admit→31,001ms経過→prepare/approve/sign/sendを確認。
別caseでadmit後の故意failure→write OFFの既存Real recovery起動→reconciliationOnly、
new admit拒否、custody file hash不変、Transport dispatch 0を確認する。
markerとrollbackを同じbytesで保持したままactual ledgerをempty/ADMITTEDに変えたfixtureでも、
前者だけsafe-emptyが通る。実データのledgerを書き換える操作ではない。

## 維持する境界

- SignerのAF_UNIX-only、PrivateNetwork、NoNewPrivilegesを拡大しない。
- Worker/Approval/TransportへProject seedやpreimage credentialの権限を追加しない。
- Human/Worker-control入口、Worker/Approvalサービスから自動実行を開始しない。
- C2の設定は固定document digestに限定し、C1とcleanup後はwrite OFF。
- Gate B後の検証済み空Project ledgerだけ再利用し、既存の義務・custody・receiptを拒否する。
- 異常時もledger/custody/nonce/Transport receipt/attempt evidenceは保存する。
- credential暗号文の除去はcryptographic revokeではない。

## Secret入力

Human-controlled anonymous stdin pipeに**Ed25519生32 bytesちょうど、その後EOF**。
改行、hex/base64文字列、JSON、mnemonic、PEM、64-byte secret keyは不可。
保管元や取得commandはHumanが決める。Codexは探さず、読まず、実行しない。
Python coordinatorはseed stdinを読まず、既存 `handoff_real.mjs` へFDを引き継ぐ。
固定Project DID一致後にsystemd host-key暗号文を作る。argv/env/historyへseedを展開しない。
TTY・通常ファイル・named FIFOへfallbackしない。`echo`、`tee`、plaintext一時fileを使わない。
JS/Python heapやstream内部copyの完全zeroizationを保証するものではない。

## 検証と限界

確認済み:

| 検証 | 結果 |
| --- | --- |
| 通常offline suite | 140 PASS |
| 既存Recovery / Gate A / Gate B / fake Real / empty identity | 106 PASS |
| C1 packet / Final Revalidation / immutable binding / reconciliation | 11 PASS |
| rootless C1→C2 fake E2E（注入clock／post-admit recovery含む） | 6 PASS |
| host runner / rollback / checkpoint / pre/post-commit境界 | 17 PASS |
| Connection approval digest | 19 PASS |
| ConnectionStore / durable nonce | 12 PASS |
| Real credential / Real nonce | 各26 PASS |
| fake Transport | PASS、retryCount=0、externalNetwork=false |
| README Smoke / Material Smoke | PASS、5 runs / 1 case |

fake E2EはseedなしC1、explicit selection、packet/preimage binding、1 fake dispatch、
AMBIGUOUS/STOP、exact reconciliation後もSTOP、second execute拒否、
wrong approval/tamper/stale final observationによるdispatch 0、公開record改変によるAMBIGUOUS維持、
Delivery/REVEAL拒否、durable quota/nonce/receiptを確認する。
clientを経由しない別のvalid packetもSigner admitとTransportでそれぞれ拒否し、state/dispatch不変を確認する。

各roleはテストprocess内でINETを拒否し、Transportはfake adapterへ差し替える。
これは実hostでのcredential配送・ACL・systemd rollbackの実測ではない。
public readerはfixture検証であり、今回live GETは実行していない。
PROFILEは静的server source互換pin。応答から検査するgeneration/framingと異なり、
remote deploymentの実装同一性をattestするものではない。

## Record-count blocker局所修正（base `4e18af4`）

Python readerとNode snapshot検証の4096件capを撤去した。raw exportは引き続き10MiB未満、
complete framingと既存profile/generation検証を必須とする。Signer向け関連recordの64件上限と
IPC byte上限は変更していない。malformed行・署名・protocol検証の扱いも変更していない。

offline fixtureの4096／4097／10000件で取得→C1 candidates→freeze→最終再検証が成功し、
選択OFFERだけがadmission snapshotへ渡ることを確認した。10MiBちょうど／超過、末尾framing、
malformed、profile/generation、関連record上限の回帰も確認した。
通常suite 141件成功後に末尾framingテストを追加し、reader 9件を再実行して成功。
Node packet 13件、Gate C fake E2E 6件、host 17件、Smoke 5 runs／Material Smoke 1 caseが成功。
fake E2EはsandboxのAF_UNIX bind拒否後、承認機構を通じて同じローカルテストを再実行した。
Real Secret／sudo／live C1／実External Write／pushは実行していない。

## Human手順（独立レビュー後、別途実行判断）

実hostの前提はGate B PASS後のDUMMY配置・全停止、検証済み空Project ledger、
既存DUMMY state不変、Real custodyなし、Transport state空。
過去attemptがあっても、rollback完了と現在のsafe-emptyを確認できればHumanが新しいC1を実行できる。
automatic retryは行わず、過去Evidenceを保存する。

C1はSecretなしで実行する。terminalに候補が表示され、exact offer IDを入力する。

```bash
cd $(git rev-parse --show-toplevel)/components/collaboration/agent
sudo -v
sudo -n python3 -I deploy/connection/gate_c.py c1
```

成功出力は `passed:true`, `stage:C1_STOPPED`, `externalWrites:0` と
`attemptId`、exact `document`、`nextConfirmation`。
保存先は `/var/lib/collab-connection-setup/gate-c/<attemptId>/`。
公開packetは `packet.json`、private preimageの暗号文は `accept-preimage.cred`。
ここで必ず終了し、C2を自動起動しない。

C2では `<C1のattemptId>` を公開UUIDに置換する。Secret値は置換箇所へ書かない。

```bash
set +x
set -o pipefail
gate_c_from_stdin() {
  sudo -n python3 -I $(git rev-parse --show-toplevel)/components/collaboration/agent/deploy/connection/gate_c.py c2 '<C1のattemptId>'
}
```

Humanが選んだ「argv/env/historyへseedを含めず、stdoutへraw32だけを出す」取得処理を接続する。
次は実在commandではなく、左辺をHumanが決めるための記法である。

```text
HUMAN_SELECTED_RAW32_SOURCE | gate_c_from_stdin
```

seedはstdin pipeを流れ、terminalの承認は別の `/dev/tty` で入力する。
表示packetを確認し、`SEND ACCEPT <document.digest>` を完全一致で入力する。
`sudo -n`は認証promptによるseed stdin消費を防ぐ。sudo/terminalのI/O logging自体は無効化しないため、
入力記録のある環境へseedを流さない。Secret sourceの保管形式や変換方法はこの手順で推測しない。
Arm後にHuman承認し、その後のFinal Revalidationが不成立ならcleanup後RETURN_C1となる。
C1の経過時間だけでは拒否しない。OFFER期限やfinal observation期限は検査する。
失敗後はrollbackとactual durable stateを確認する。safe-emptyなら新UUID、義務があればreconciliation-only。
同じattempt directoryの再使用、marker削除によるretry、自動再送は行わない。

成功出力の必要条件:

```text
passed: true
stage: C2_STOPPED
status: RECONCILED_PRESENT
retry: false
packetDigest: C1 document.digest と一致
rollback: DUMMY_CONFIG_ALL_STOPPED
projectSeedRemoved: true
```

HTTP応答だけで `passed:true` にしない。公開exact record確認を意味する結果だけが成功。
送信可能性が不明な失敗はExternal Write 0と断定しない。
`result.json`, `rollback.json`, `c2-attempt.json`はsanitizedなEvidenceで、seedを含まない。
credential file、backup一式、journal全文をチャットへ渡さない。

## STOP / rollback

通常例外・SIGINT/SIGTERM等ではfinally cleanupを試みる。
SIGKILL・電源断・cleanup失敗後は、同じcheckoutとC1のUUIDでHumanが実行する。

```bash
sudo python3 -I $(git rev-parse --show-toplevel)/components/collaboration/agent/deploy/connection/gate_c.py rollback '<C1のattemptId>'
```

同じsetup directory lockにより、実行中runnerとの並行変更を拒否する。
ingress → Transport → Signerの停止を先に試し、Project seed暗号文を除去し、
変更前のDUMMY config/source/drop-inへ復元する。未知のfile変更を上書きしない。
復元できなければ `FAILED_MANUAL_RECOVERY_REQUIRED` とし、成功を主張しない。

`c2-attempt.json`、packet、暗号化accept-preimage、ledger、custody、nonce、receiptは保存する。
preimage暗号文はdurable custodyの元資料であり、cleanupで消すProject seed暗号文とは区別する。
全service停止によりsystemdのruntime credential配送を終了する。
ファイル除去だけでmemory内のseedや既存署名を失効させるものではない。

write OFF確認は、rollback成功に加えてDUMMY configと全unit停止を確認する。
元のDUMMY schemaは `externalWriteEnabled` fieldを持たず、Real write capabilityは無効。
C2中だけのReal configは `externalWriteEnabled:true` と固定 `firstAcceptDigest` の組を要求する。
Signer/Transportがこの固定packet以外を拒否することはoffline E2Eで確認している。

## Offline再現command

以下はローカルfixtureのみ。systemdやReal credentialの実操作は含まない。
root権限では実行しない。

```bash
python3 -B -m unittest discover -s tests -q
python3 -B tests/connection_first_accept.py -v
python3 -B tests/gate_c_host.py -v
node tests/first_accept_packet.mjs
PYTHONPATH=tests:src python3 -B -m unittest connection_empty_identity connection_real_activation connection_offline connection_startup connection_ledger connection_smoke_recovery connection_gate_a connection_host connection_boundary connection_ipc connection_systemd gate_a_host gate_b_host -q
node tests/connection_approval.mjs
node tests/connection_store.mjs
node tests/real_credential.mjs
node tests/real_nonce.mjs
node tests/real_transport.mjs
python3 -B tests/smoke.py
python3 -B tests/material_smoke.py
git diff --check
```

sandboxがAF_UNIXやテストの子process生成を拒否する環境では、許可されたローカル検証環境が必要。
そのためにsudoを使ったり、実host serviceの制限を緩めたりしない。
