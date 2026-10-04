# Project DID Policy Signer｜Real Runtime Readiness Runbook

## Purpose

このRunbookは、Issue #6 / merged PR #15で実装したPolicy Signerを、
**Real Project DID seedを投入する前に実Hostで検証するためのHuman Gate手順**です。

Goalは24H AgentへSecretを渡さず、通常Actionごとのseed入力を不要にすることです。

このRunbook自体は以下を承認しません。

- Real Project DID seedの投入
- Production deployment / mutation
- Technocore External Write
- Real ACCEPT / DELIVERY / REVEAL
- Wallet / Claim / Faucet / Mainnet / value-bearing action

それぞれHuman Ownerの明示承認が必要です。

## Current validated baseline

Code baseline:

- PR #15 merge commit: `b839e365750b5c86e1d1833cf008574c257facc9`
- Signer reviewed head: `bfa7c2125a7bf891fc24591c51479d41398b4234`
- Signer CI at merge: 52 pass / 0 fail / 0 skip
- Independent Review: merge material blockerなし
- Targeted Re-review: Audit P2-1 Resolved / new P0-P3なし

Runtime toolchainのcurrent verified baselineは **Node.js 22.x** です。
Independent ReviewはNode v22.22.0で実施しました。

Node Permission Modelは、Node 22で`fsync` / `fdatasync`をpath grantに関係なく拒否するため、
Signer Runtimeでは使用しません。durable state / audit / snapshotのための`fsync`を優先し、
filesystem boundaryはsystemd sandbox + DACへ一本化します。

別majorへ変更する場合も、durability / Network / AF_UNIX / sandbox挙動をObserved Runtimeで再確認し、
同等性を推定しません。

---

## Gate A｜Agent principal boundary

Signer separationは、24H Agentがroot / sudo / Signer principalへ昇格できないことを前提にします。

Real Secret投入前に、Agentは専用OS userで動かしてください。

### Required

Agent実行userは：

- rootではない
- passwordless sudoを持たない
- sudo可能userではない
- polkit等でsystemd unit変更権限を持たない
- `flop-agent` group以外のSigner関連groupへ所属しない
- `flop-signer` userではない
- `flop-signer-acquisition` groupへ所属しない
- Signer config / state / audit / encrypted credentialへwriteできない
- encrypted credentialをreadできない
- Trusted Reader socketへ直接接続できない
- Agent-facing Signer socketだけを利用できる

### Human verification evidence

`<agent-user>`は実際に24H Agentを動かす専用userへ置き換えます。

Read-only確認例：

```bash
id <agent-user>
getent group flop-agent
getent group flop-signer-acquisition
getent passwd flop-signer
getent passwd flop-signer-reader
```

Human admin権限で確認する場合：

```bash
sudo -v
sudo -l -U <agent-user>
```

期待値：

- Agent userにsudo許可なし
- Signer関連group membershipなし
- root / systemd管理権限なし

さらに配置後は、Human adminが **内容を表示せずpermissionだけ** 確認します。

```bash
namei -l /var/lib/flop-policy-signer-secret/project-seed.cred
namei -l /var/lib/flop-policy-signer/state.json
namei -l /etc/flop-policy-signer/config.json
stat -c '%U %G %a %n' \
  /var/lib/flop-policy-signer-secret \
  /var/lib/flop-policy-signer \
  /var/lib/flop-policy-signer-input \
  /etc/flop-policy-signer/config.json
```

Secret fileやstate fileの本文を表示しません。

### STOP

以下のどれかならReal Secretを投入しません。

- Agent userがsudo可能
- Agent userがSigner / acquisition groupへ所属
- AgentからSecret directoryをread可能
- AgentからSigner state / configをwrite可能
- Reader userがSecret directoryをread可能
- UID/GID / socket ownerが意図と不一致

---

## Read-only preflight｜実Host mutation前

Gate Bへ進む前に、まず通常user権限でread-only preflightを実行します。

```bash
cd <FLOP-repo>/components/signer
python3 deploy/runtime_preflight.py
```

将来24H Agentとして使う専用userがすでに存在する場合：

```bash
python3 deploy/runtime_preflight.py --agent-user <agent-user>
```

このcommandは：

- sudoを使わない
- file / unitを変更しない
- serviceをstart/stopしない
- Secret / state / config本文を読まない
- credential本文を読まない

sanitized JSON metadataだけをstdoutへ出します。

最低限、以下を確認します。

- PATH上source candidate Node major 22
- fixed Runtime Node `/usr/local/lib/flop-policy-signer/node` の有無 / owner / mode / version
- systemd running / degraded
- `systemd-creds` / `systemd-sysusers` / `systemd-tmpfiles` availability
- existing Signer users / groups / units
- target pathのexistence / owner / mode metadata
- `crashCapture`: WSL / boot ID / pattern分類 / pid / 実効safe / gate state存在（read-only）
- existing encrypted credentialの有無
- optional Agent userのUID / group membership

`readyForTestKeyRehearsalPreparation=false` の場合は、blockingObservationsを先に解消し、
Host mutationへ進みません。

crashCaptureがunsafeでもtest-key **準備**のread-only preflightは阻止しません。
armのroot承認と実効検証はseed生成前のrehearsal操作に残します。

このpreflightはsudo / polkit権限を判定しません。Agent privilegeのHuman確認はGate Aに残ります。

---

## Gate B｜Test-key Runtime Rehearsal

Real seedより先に、同じsystemd / UID / socket / filesystem構成を**test keyだけ**で実Host確認します。

### Node 22 Runtime provisioning

systemd unitはHuman userのNVM配下Nodeを直接使いません。

理由：

- Signer / Readerはdedicated system userで動く
- `ProtectHome=yes`
- Human home配下のmutable runtimeへProduction boundaryを依存させない

unitが使う固定Runtimeは：

```text
/usr/local/lib/flop-policy-signer/node
```

です。

read-only preflightの `binaries.node` は、Host上で利用可能な**source candidate**です。
`runtimeNode` はsystemdが実際に利用する固定Runtimeです。

`preparationActions` に

```text
PROVISION_RUNTIME_NODE_FROM_VERIFIED_NODE22
```

がある場合、**事前に手動installはしません**。
fresh-install判定と衝突するため、Node固定配置は`test_key_rehearsal.py prepare --node`自身に一本化します。

Humanはcopy source候補だけを確認します。

```bash
SOURCE_NODE="$(command -v node)"
"$SOURCE_NODE" --version
sha256sum "$SOURCE_NODE"
```

その後の`prepare --node "$SOURCE_NODE"`が：

- source SHA-256を採取
- root-owned fixed Runtimeへcopy
- installed SHA-256がsourceと一致することを確認
- installed fileがroot:root / 0755であることを確認
- **root-owned installed copyだけを実行してNode v22を確認**
- installしたcode / unit / sysusers / tmpfiles全体のSHA-256 manifestをprovenanceへ保存

します。

source NodeがHuman home/NVM配下でも、**copy sourceとしてのみ**使い、root processからsource Nodeそのものを実行しません。
unitのExecStartは固定Runtimeを参照します。

Real Project DID投入前には、Node SHA-256を公式配布元のSHASUMSとも照合する別Gateを残します。
別majorや別hashへ更新する場合はRuntime changeとして再検証します。

Real seedより先に、同じsystemd / UID / socket / filesystem構成を**test keyだけ**で実Host確認します。

Human Ownerがsystemd / root mutationを明示承認するまで、このGateは実行しません。

### Test-key rehearsal command

Human Gate通過後、reviewed checkoutから次の3操作だけを使います。

```bash
SOURCE_NODE="$(command -v node)"
"$SOURCE_NODE" --version

sudo -v
sudo -n python3 -I deploy/test_key_rehearsal.py prepare --node "$SOURCE_NODE"
sudo -n python3 -I deploy/test_key_rehearsal.py observe
sudo -n python3 -I deploy/test_key_rehearsal.py cleanup-test-key
```

`prepare`は：

- Real seedを受け取らない
- WSLではcrash gateをarmして実効no-coreを検証した後にtest seedをmemory上で生成
- test DIDをSigner codeで導出
- existing `credential_handoff.mjs` 経路でhost-bound encrypted credential化
- fixed Node 22 / code / units / sysusers / tmpfilesを配置
- unitを**enableしない**
- bootstrap → socket start → signer status → Reader live GET → restart recoveryを確認
- live test policy中にSigner `admit` の意図的な不正sourceによる `SOURCE_INVALID` を使い、Signer → Readerのlive refreshとSignerによるfresh snapshot読取を再起動前後に確認（work/署名は作らない）。`POLICY_EXPIRED`はPASSにしない
- Technocore External Writeを行わない

`cleanup-test-key`後に残したInfrastructureで次のtest-key rehearsalを行う場合は、
Current reviewed checkoutから `sudo -n python3 -I deploy/test_key_rehearsal.py prepare-existing`
を使います。`--node`は指定しません。停止済みunit、専用principalとmembership、
空のconfig/secret/input/state、root-owned Node 22とRuntime tree、unit配置と
drop-in不在を変更前に検査します。installed codeはCurrent HEADの祖先である
単一checkout（merge済みPR branch上のcommitを含む）のartifact群と完全一致しなければ
拒否します。旧artifactは照合後に上書きされるだけで実行しません。checkoutはclean必須です。
検査後に既知のcode/helper/unit/sysusers/tmpfilesのみCurrent Sourceへ更新し、
manifestとeffective unit（LimitCORE=0を含む）を確認してから既存のtest-key flowへ進みます。
更新途中に停止した場合はsanitized provenanceを残し、`prepare-existing`、
`resume-reviewed`、`cleanup-test-key`は再実行を拒否します。mixed artifactを手動照合したうえで
復旧方法を判断してください。Host操作はこのTaskでは行いません。

`observe`は現在のcrash safetyをread-only検証し、同じtest credential/stateでrestart recoveryとread boundaryを再確認します。
`resume-reviewed`は停止後、offline credential読取り前にWSL Gateをarm/recheckします。
cleanupのoffline decryptも先に実効safetyを要求します。再boot等でunsafeの場合は、
Human承認済みのarmを先に行い、stale stateがある場合は自動修復せずSTOPします。

`cleanup-test-key`はprovenance一致と `signaturesUsed=0 / work=null` を確認した場合だけ、
test credential / config / state / snapshotを削除します。Node / code / users / unit templateは
後続Gate用Infrastructureとして残します。
WSLではservice停止とtest credential/state削除の後にcrash gateをdisarmしoriginalへ戻します。
disarm失敗時はprovenanceを残してSTOPします。no-coreのまま中断することは安全側です。
次のcleanupは同一boot・同一実効状態なら再開し、不明/stale stateなら拒否します。
削除後・disarm前にWSLが再起動した場合、cleanupは `CRASH_GATE_STATE_MISSING` でSTOPします。
Human承認済みの `wsl_crash_gate.py arm`（再起動後の値をoriginalとして保存）を行ってから
cleanupを再実行すると、disarmでその値へ戻してprovenanceを削除します。

出力はsanitized JSONのみです。test seed本文は出力・保存しません。

### Runtime JIT / MDWE policy

Node 22 / V8は通常のJIT状態では`MemoryDenyWriteExecute=yes`と両立しないことをIndependent Reviewで確認しました。

初期Runtimeでは役割ごとに分けます。

- **Policy Signer**: `--jitless` + `MemoryDenyWriteExecute=yes`
- **Bootstrap**: `--jitless` + `MemoryDenyWriteExecute=yes`
- **Trusted Reader**: `--jitless`なし / `MemoryDenyWriteExecute`なし
  - ReaderのNode fetch/undiciがWebAssembly dependencyを持つため
  - ReaderはProject DID Secretを持たない

### Filesystem isolation / durability policy

Node Permission Model（`--permission` / `--allow-fs-*`）は使いません。

Node 22ではopen済みfile descriptorに対する`fsync` / `fdatasync`等はPermission Model下で常に拒否され、
`--allow-fs-write`では許可できません。Signerのat-most-once / replay / audit durabilityを弱めないため、
`fsync`を削るのではなくNode Permission ModelをRuntimeから外します。

filesystem boundaryは：

- `ProtectSystem=strict`
- `ProtectHome=yes`
- Signer / Bootstrapの`StateDirectory=flop-policy-signer`
- Readerの`ReadWritePaths=/var/lib/flop-policy-signer-input`
- root / dedicated UID/GIDによるDAC
- `InaccessiblePaths`

へ一本化します。

`InaccessiblePaths`は少なくとも：

- Signer / Bootstrap: encrypted Secret directoryを直接不可視
- Reader: Secret directory / Signer config / Signer stateを不可視

にします。

credential生成より前に、Signerと同じ`--jitless + MDWE`条件を`systemd-run` sandbox probeで実測します。
probeは単なる`node -e`ではなく、temporary StateDirectory上で：

- file write
- file `fsync`
- atomic rename
- directory `fsync`
- delete後のdirectory `fsync`

まで実行します。

このdurability probeが通らなければtest credentialを作成しません。

さらにdaemon-reload後、各serviceについて少なくとも以下の**effective properties**を`systemctl show`で検証します。

- FragmentPath
- DropInPaths（空であること）
- User / Group
- PrivateNetwork
- MemoryDenyWriteExecute
- Signer / BootstrapのLimitCORE / LimitCORESoft（ともに0、pipe対策の代替ではない）
- ProtectSystem
- RestrictAddressFamilies
- InaccessiblePaths

設定fileの記述だけでPASSにしません。

### Partial failure / recovery

`prepare`が途中で停止した場合、同じHostで`prepare`を再実行しません。

Reviewed fixへ更新して既存test credential/stateを継続利用できる場合は、reviewed checkoutへ切り替えた後：

```bash
sudo -v
sudo -n python3 -I deploy/test_key_rehearsal.py resume-reviewed
```

を使います。

`resume-reviewed`は：

1. existing provenanceがtest-key-onlyであることを確認
2. old installed manifestがprovenanceと完全一致することを確認
3. services / socketsを停止
4. existing protected stateをofflineで開き、
   `signaturesUsed=0 / work=null / actions={}` を確認
5. current checkoutがcleanであることを確認
6. current reviewed artifactのtarget hash manifestをHost mutation前に計算
7. old manifest全一致状態からのみreviewed artifactへ更新
8. update途中でold/targetどちらにも一致しないmixed stateになった場合は自動継続せずSTOP
9. new manifest / effective unit propertiesを確認
10. socketsを再開し、同じtest DID/stateで`observe`を実行

します。

Real seedや新しいtest seedは生成しません。

`prepare`は最初のHost mutation前からphase付きsanitized provenanceを
`/var/lib/flop-policy-signer-rehearsal.json`へ保存します。

主なphase：

```text
STARTING
→ RUNTIME_INSTALLED
→ PRINCIPALS_READY
→ UNITS_VERIFIED
→ SANDBOX_PROBED
→ IDENTITY_READY
→ CONFIG_WRITTEN
→ CREDENTIAL_HANDOFF_STARTING
→ CREDENTIAL_WRITTEN
→ BOOTSTRAP_STARTING
→ BOOTSTRAPPED
→ SOCKETS_STARTING
→ SOCKETS_STARTED
→ OBSERVED
```

したがって途中失敗時も、test credentialをReal credentialと取り違えないためのprovenanceを残します。

まず：

```bash
sudo -n python3 -I deploy/test_key_rehearsal.py cleanup-test-key
```

を使います。cleanupはservices/socketsを先に停止し、必要ならencrypted test credentialをanonymous pipeで一時復号して**offline PolicySigner**でstateを確認します。live Signer serviceの正常動作には依存しません。

削除前Evidenceとして、存在する場合はauditのSHA-256 / byte数 / 行数をsanitized outputへ残します。

cleanupが一度`CLEANUP_STARTED`へ進み、offline protected-state checkを成功させた後に中断した場合は、
保存済み`cleanupProtectedStatus`を再利用します。credential / configが既に削除されていても、
stateだけ残った状態からcleanupを再開できます。

`CREDENTIAL_HANDOFF_STARTING`中にhash未記録credentialが残った場合は、
削除前に`systemd-creds decrypt → anonymous pipe → test_identity.mjs`でtest DID一致を確認します。

sysusersより前で失敗し、tmpfilesのowner principalがまだ存在しない場合は、
cleanupで`systemd-tmpfiles --create`を無理に実行しません。

#### Infrastructureまでfreshに戻したい場合

test-key cleanup後も、Node / code / users / groups / unitsは意図的に残ります。
次回は上記の`prepare-existing`で検証・更新して再rehearsalできます。

freshからやり直す必要がある場合は、以下を自動実行せずHumanがprovenanceとactual stateを照合してからremoveします。

対象Infrastructure：

```text
/usr/local/lib/flop-policy-signer/
/etc/flop-policy-signer/
/etc/sysusers.d/flop-policy-signer.conf
/etc/tmpfiles.d/flop-policy-signer.conf
/etc/systemd/system/flop-policy-signer*.service
/etc/systemd/system/flop-policy-signer*.socket
/var/lib/flop-policy-signer-secret/
/var/lib/flop-policy-signer-input/
flop-signer user
flop-signer-reader user
flop-agent group
flop-signer-acquisition group
```

未知のfile、異なるowner/hash、Real credential/stateがある場合は削除せずSTOPします。

### Source identity

`prepare`はroot mutation前にreviewed checkoutへ：

- `git --no-optional-locks -c safe.directory=<repo> -c core.fsmonitor=false -c core.hooksPath=/dev/null rev-parse HEAD`
- 同じread-only Git optionsで`status --porcelain`

を実行し、dirty treeを拒否します。

install後はruntime artifact hash manifestを保存し、`observe` / `cleanup-test-key`でも一致を再確認します。


確認対象：

1. `systemd-sysusers` / `tmpfiles` による専用user / directory境界
2. Signer socket: Agent → Signerだけ許可
3. Reader socket: Signer → Readerだけ許可
4. Signer: `PrivateNetwork=yes` / AF_UNIX only
5. Reader: Networkあり / Secretなし
6. Signer config: root-owned / non-writable / non-symlink
7. state / audit: Signer owner、Agent write不可
8. encrypted credential: root-only
9. Signer restart後に同一test DIDを復旧
10. quota / replay / action stateがrestartでresetしない
11. Live Technocore **read-only** Reader → snapshot → Signer decision path
12. External Writeは0のまま

### Evidence

Configured値だけでPASSにしません。

最低限、実Hostで：

- `id` / group
- file owner / mode
- socket owner / mode
- service status
- sandboxの実効状態
- restart前後のDID / policyDigest一致
- restart前後でSigner PIDが変わったこと
- restart前後のSigner network namespaceがHostと分離されていること
- restart前後のstate revision / quota / replay継続
- config / credential / state / socketのowner / group / mode
- ReaderがSigner RPC socketへ接続できないこと
- Reader live GET成功
- SignerからInternet不可
- ReaderからSecret不可
- External Writeはコード経路上0（`externalWritesByConstruction: 0`）

を観測します。

Configured / Intended != Observed Runtime.

---

## Gate C｜Human Secret Handoff

**このGateはReal Project DID seed利用の明示Human承認後だけ実行します。**

Secret sourceの保管形式・取得方法はこのRunbookで推測しません。

既存ProjectのReal ACCEPT handoffと同じBoundaryを使います。

### Real Host crash-capture Gate

WSLのpipe型 `kernel.core_pattern = |/wsl-capture-crash ...` は、crashしたprocessの
memoryをuserspace handlerへ渡します。pipeの場合 `RLIMIT_CORE` は無視されるため、
unitの `LimitCORE=0` **だけではSecret投入のauthorityになりません**。

今回のGateはWSL上で `core_pattern` を空文字、`core_uses_pid` を `0` にして、
Linuxのno-core条件を使用します。別のpipe handlerには差し替えません。
この変更はHost全体のcore生成を停止するため、Humanのsudo/root承認が必要です。

Real Secretの順序は必ず次です。

1. reviewed checkoutからread-only statusを確認。
2. Humanのsudo/root承認。Signer / Bootstrap / Signer socketを停止し、
   handoffやoffline decrypt等、unit外のSecret-bearing processも終了させる。
3. crash gate `arm` → readbackが `isWsl=true, armed=true, safe=true`。
4. Real credential handoff。**上流のSecret取得処理もarm確認より後に開始する**。
5. Bootstrap / Runtime activation → operation。
6. Signer socket / Signer / Bootstrap停止、handoff・Secret lifecycleの完了。
7. crash gate `disarm` →元のsysctlの完全一致readback →保存state削除。

```bash
python3 -I deploy/wsl_crash_gate.py status
# 以下はIndependent ReviewとHuman Host mutation承認の後だけ実行
sudo -v
sudo -n python3 -I deploy/wsl_crash_gate.py arm
python3 -I deploy/wsl_crash_gate.py status
# この後に別途承認済みのcredential handoff / activationを行う
# Secret-bearing processとSigner socketを停止してから:
sudo -n python3 -I deploy/wsl_crash_gate.py disarm
```

Gateはserviceを自動停止しません。Signer / Bootstrapがinactiveまたはfailedで、
MainPID / ControlPIDが0、pending Jobなし、Signer socketも停止済みでなければ拒否します。
adminはarm/disarmとactivation・handoff・sysctl変更を並行実行しないでください。
rootがoperation中にsysctlを変更することまで防ぐ機構ではありません。

`status` は通常userで実行可能で、sysctl / boot ID / gate stateの存在だけを読みます。
patternは `empty / pipe / file / unknown` の分類だけを出し、handler引数は出しません。
`armed` は**現在のno-core実効値**を表し、保存stateの存在・正当性を表しません。
stateはrestore専用で、安全判定のauthorityではありません。

状態遷移は次のとおりです。

| 状態 | 操作と条件 | 結果 |
| --- | --- | --- |
| stateなし | 同一boot確認、entry point停止確認後arm | `/run/flop-policy-signer-crash-gate.json` をroot:root / 0600でexclusive作成、original 2値とboot IDをfsync保存 |
| original保存済み | pid=0 → pattern空（newlineを書込）、各値と最終状態をreadback | no-core armed |
| stateあり・同一boot・実効値がno-coreと完全一致 | arm再実行 | originalを変更せず成功（最後の書込み後の中断も回復可能） |
| stateあり・同一boot・no-core完全一致・entry point停止済み | disarm | originalのpattern → pidの順に復元（空pattern + pid=1を経由しない）、readback後に自身のstateだけ削除 |
| stale/不正state、boot不一致、外部変更、readback不一致 | arm/disarm | STOP、state保存、推測復元なし |

2つのsysctlは一括atomicには変更できません。Secret-bearing processがない状態でのみ
順次変更し、書込み前に復元情報を永続化します。中断時にno-core完全一致なら再開可能です。
部分arm / 部分restore、restore完了後state削除前の中断で値がtarget以外なら、再試行も拒否します。
Humanがsanitized metadataとoriginalを調査するまでstateを消したり上書きしたりしません。
部分armの復旧はHumanがstateの `original` を確認し、rootでoriginal値を手動で戻した上で
stateを退避・削除してから新たにarmします（Secret-bearing processが停止中であることが前提）。

**WSL restartはarmedの前提を無効化します。** `/run` stateはbootを跨ぐauthorityではなく、
WSLは元のcrash handlerを再設定し得ます。毎activation前に再arm/readbackしてください。
credential handoff、systemd credential loader（Bootstrap / Runtime共通）は各seed読取り前に
実際の `/proc/sys/kernel` 値を確認し、WSLで空pattern + pid0以外なら
`WSL_CRASH_CAPTURE_UNSAFE` で停止します。保存stateや過去のrehearsal成功では迂回できません。
ただしこのHostのsystemd v255では、`LoadCredentialEncrypted=` の復号は
ExecCondition / ExecStartPre / ExecStartより前にsystemd（sd-executor）が行い、
平文は `/run/credentials/<unit>` に置かれます。Nodeの `assertHostCrashSafety` はseedを
process memoryへ読む前に止めるものであり、systemdの復号自体は防ぎません。unsafe時に
Signer / Bootstrapを手動startしないでください。Signerは `Restart=on-failure` のため
unsafe状態で起動すると復号→拒否を5秒毎に繰り返すので、その場合は直ちにsocket / serviceを停止します。
非WSLでは従来の動作を維持します。

### Synthetic crash proof（Real seedなし）

Claude Code Independent ReviewとHuman Host mutation承認の後、credential未配置・Gate未arm・
services/socket停止状態で、reviewed checkoutから一度実行します。
既存test credentialがあるHostでは、承認済みtest cleanupを先に完了してください。

```bash
sudo -n python3 -I deploy/wsl_crash_proof.py
```

このcommandはoriginalを記録してarmし、新しいPython processでtest-only memoryを持つ
SIGABRTを発生させます（core/file size limitは0以外）。前後のno-core実効値、signal終了、
専用temporary working directoryにcoreが残らないこと、disarm後のoriginal完全一致を検証します。
失敗時は自動restoreを行わずSTOPします。非WSLではmutationなしでskipします。

handlerが呼ばれない根拠はLinux no-core条件とcrash前後のreadbackです。
Windows側captureの独立観測は自動化していません。**Humanは実行前後の
`%TEMP%\wsl-crashes` のmetadataを比較し、該当時間帯の新規dumpがないことを確認**してください。
dump本文を開いたりGitHubへuploadしたりしません。自動結果の
`windowsCaptureObserved=NOT_CHECKED` はWindows側を検査していないことを示します。

Hostの空pattern書込み、実crash、復元、systemd停止状態の実測はoffline testsとは別Evidenceです。
これらとClaude Code Independent Reviewが未完了の間はReal利用へ進みません。

### Input contract

`credential_handoff.mjs`へ渡す入力は：

- raw Ed25519 seed **32 bytes exactly**
- anonymous stdin pipe、またはanonymous `--input-fd N`
- EOFで終了

以下は禁止：

- seedをargvへ入れる
- seedをenvironment variableへ入れる
- seedをshell historyへ書く
- plaintext temporary fileへ書く
- named FIFOを使う
- ChatGPT / GitHub / Notion / Issue / PR / logへ貼る
- terminal recording / session loggingが有効な環境で流す

hex / base64を直接Signerへ渡しません。
保管元からraw32へ変換する責任はHuman-selected secret source側に置きます。

### Canonical handoff shape

まずsudo認証をSecret pipeと分離します。

```bash
sudo -v
set +x
set -o pipefail
```

Signer handoff wrapper：

```bash
signer_handoff_from_stdin() {
  sudo -n /usr/local/lib/flop-policy-signer/node --jitless \
    /usr/local/lib/flop-policy-signer/src/credential_handoff.mjs
}
```

Humanが選んだ、**argv / env / historyへseedを含めずstdoutへraw32だけを出す取得処理**を左辺へ接続します。

次はSecret取得commandを推測しないための記法です。

```text
HUMAN_SELECTED_RAW32_SOURCE | signer_handoff_from_stdin
```

`sudo -n`を使う理由は、sudo password promptがseed stdinを消費する事故を避けるためです。

### Handoffが成功する条件

Code側は：

1. inputがraw 32 bytes
2. configの`expectedDid`と導出DIDが完全一致
3. `systemd-creds encrypt --with-key=host --name=project-seed - -`成功
4. 固定先へencrypted credentialをexclusive create
5. file fsync + directory fsync
6. 既存credentialがある場合は上書き拒否

を要求します。

固定先：

```text
/var/lib/flop-policy-signer-secret/project-seed.cred
```

### Success evidence

成功後もSecretをdecrypt / catしません。

Human adminはmetadataのみ確認します。

```bash
stat -c '%U %G %a %s %n' \
  /var/lib/flop-policy-signer-secret/project-seed.cred
```

期待：

- owner: root
- group: root
- mode: 600
- size > 0

Handoff successそのものがconfig `expectedDid`とのDID一致を意味します。

### Failure

失敗時にblind retryしません。

特に：

- `WSL_CRASH_CAPTURE_UNSAFE`
- `CREDENTIAL_ALREADY_EXISTS`
- `CREDENTIAL_DID_MISMATCH`
- `CREDENTIAL_DIRECTORY_UNSAFE`
- `CREDENTIAL_ENCRYPT_FAILED`
- `CREDENTIAL_STORE_FAILED`

では原因とactual filesystem stateを先に確認します。

既存encrypted credentialを自動削除・上書きしません。

---

## Gate D｜Real credential restart rehearsal

Real credentialを作成しても、すぐ24H Agentを動かしません。

External Writeを行わず、Human-supervisedで：

1. Signer起動
2. DID一致確認
3. state open成功
4. Signer停止
5. Signer再起動
6. per-action seed再入力なしで同一DID復旧
7. state revision / quota / replay継続
8. Reader live read-only acquisition
9. Agent UIDからSecret / state / Reader socketへ到達不可

を確認します。

このGateでTechnocore writeは行いません。

---

## Gate E｜Limited Real Pilot decision

Gate A-DのEvidenceをHuman Ownerが確認してから、初めて限定Real Pilotを判断します。

初期PilotはIssue #6どおり：

- current Project DID
- one known GCD work
- Paper / no-value only
- known Technocore / tclk only
- maxWorkItems=1
- bounded 24H policy
- generic signなし
- unknown operation DENY
- ambiguous resultはno blind retry / read-only reconciliation

のままです。

Real External Writeの有効化は別Human Gateです。

---

## Explicit Later

初回single-work Pilotの前提条件にはしません。

- HSM / TPM signing
- multisig
- remote KMS
- HA Signer
- generic multi-work framework
- valid old encrypted state rollbackの外部anchor
- one-work後の自動policy rotation

ただし2本目のworkへ進む前には、state / policy rotation手順を別途定義します。

---

## Evidence handling

保存してよいもの：

- commit SHA
- unit / UID / GID metadata
- permission metadata
- sanitized status
- DID
- state revision / quota count
- public Technocore read evidence
- test result / exit code

保存しないもの：

- seed
- decrypted credential
- raw credential bytes
- Secret source commandがSecret値を含む場合のcommand history
- process memory dump
- unrestricted journal dump
- credential directory contents

SecretはChatGPT / GitHub / Notionへ置きません。

## STOP conditions

次のいずれかでReal Pilotへ進みません。

- Agent privilege boundary未確認
- Secret / state / config ACL不一致
- DID mismatch
- state corruption / policy mismatch
- restartでstate reset
- ReaderからSecretへ到達
- SignerからInternetへ到達
- Live read-only E2E未確認
- unexplained audit / replay / quota inconsistency
- Independent Review前提から外れるruntime変更
