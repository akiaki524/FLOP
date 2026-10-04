# P1-1 / P3-1: Gate B後の空Real identity再利用

開始HEAD: `2f039f58f2702c3b740ed3f99bea1af457f12617`。開始時worktree clean。
Real Secret、sudo、実host Gate B、Technocore通信は使わない局所修正。

## 原因と修正

従来の`prior = ledger exists`は、ledgerを新規作成すべきかの判定と、
新規actionを禁止する復旧モードの判定を兼ねていた。
Gate BだけでもProject DIDのledgerが残るため、admissionのない空ledgerまで
reconciliation-onlyになり、admitもreconcileRestartもできなかった。

`prior`は保存したまま、`recoveryRequired = prior && !reusableIdentity`を分離する。
既存ledgerは`AsyncPilotLedger.open(create:false)`でlock・schema・checksum・DID検証を通して開く。
同じpath/identity/revisionを引き継ぎ、削除・reset・再createはしない。
Realの場合に限り、次の全条件を満たす既存identityを再利用する:

- 検証済みledgerのDIDが固定Project DIDと一致
- `quotaConsumed === false`、`admission === null`、`state === null`、`actions.length === 0`
- `real-accept-custody`が存在しない
- 当該Project identityの未確定/未知の`.json.*` artifactがない（取得済みlockは別）
- Transportの固定state directoryが厳密に空で、read-only IPCで確認できる

`persistPreimage`、nonce observation、send準備側の旧`!prior`条件も同じ
`!recoveryRequired`へ合わせる。外部writeは従来どおり別の明示booleanが必要で、
falseの場合の拒否を変更しない。
非空priorはreconciliation-only。破損、schema不整合、custodyだけの残存、
確認失敗などは起動拒否も含めfail-closedとし、新規actionへ戻さない。

## Ledger外のdurable state

`ConnectionStore.initialize()`はcustody root作成・親fsyncを先に行い、
lock、pointer、暗号化state、highwater witnessを保存する。
`admit`はこの初期化が終わってからledgerへADMITTEDをappendする。
したがって「custody作成→ADMITTED前crash」では、空directoryだけでも再利用を拒否する。
nonce reservation / SEND_ATTEMPTED / AMBIGUOUS、partial / pending fileも同じroot内に存在する。

Realのledger/custody存在判定は`lstat`のENOENTだけを不在とする。
dangling symlink、permission errorを不在と解釈しない。
Project identity固有のpending artifactはcreate前にも検査する。
別DIDのDUMMY ledgerは混同しない。

Transport receiptは別UIDの`/var/lib/collab-transport/first-real-accept.json`に保存される。
Signerへfilesystem権限を追加せず、既存Transport Unix socketに
`{"operation":"emptyState","value":{}}`というread-only照会だけを追加した。
Transportが固定directoryのtype/modeと内容を確認し、`{"empty":boolean}`だけを返す。
path入力、receipt本文出力、書込み、network dispatchはない。
receipt、partial file、未知entryのいずれも非空として扱う。確認失敗は起動拒否。
ledgerがない初回起動でも、孤立receiptのある状態は新規activationへ進めない。

正常な書込み順序はcustody SEND_ATTEMPTED→ledger SEND_ATTEMPTED→Transport receipt fsync→dispatch。
起動時はidentity lockを保持し、まだsend能力をIPCへ公開していない。
custodyの新規作成も既存rootを拒否するため、後から存在したcustodyを上書きしない。
任意のroot操作によるstateの同時改変や全snapshot巻き戻しまで検出する新機構は追加しない。

## IDENTITYと保存履歴

現行ledgerはappend-only event logではなく、checksum付きのpublic state snapshot。
IDENTITYとSTARTはfield内容を変えずrevisionを進める実装で、IDENTITY重複はschema上許される。
ただし再利用時に不要なIDENTITY announcementを再appendしないよう、
`connection.reusableIdentity`の場合だけSignerのIDENTITY emitを省く。
既存DIDを引き継ぎ、STARTによるrevision更新は通常どおり行う。
既存snapshotのrevision・action履歴を巻き戻したり削除したりしない。

## 互換性と範囲

DUMMYのprior判定・復旧モードは変更しない。Gate A/Bのrunner・handoff・unit templatesも無変更。
既存host recovery/checkpointの複数ledger拒否は維持する。
今回のGate B→次回Real startupはruntimeがDID固有ledgerを選ぶ経路であり、
DUMMY専用host updaterの拒否を緩める必要はない。
Gate B runnerの再実行は禁止のまま。旧DUMMY recovery toolをReal stateへ適用しない。

## 検証

新しい`tests/connection_empty_identity.py`はtest identity・一時配置・INET拒否・write=falseで、
Gate B相当のidentity-only起動→停止/credential cleanup→同identity再配送→
通常起動→admit/prepareを確認する。実systemd credential配送の検証ではない。
既存のfake送信回帰は外部adapterをfixtureへ置換したoffline検証であり、実hostのwrite enableではない。

確認済みの回帰結果:

| 検証 | 結果 |
| --- | --- |
| Gate B cleanup→次回admit/prepare・negative restart | 9/9 PASS |
| 通常offline suite (`test_*.py`) | 133/133 PASS |
| 既存connection / DUMMY / crash / host checkpoint模型 / Gate A | 67/67 PASS |
| 既存fake Real activation / AMBIGUOUS restart / no resend | 3/3 PASS |
| Gate A transaction / ACL | 12/12 PASS |
| Gate B transaction / measurement | 15/15 PASS |
| ConnectionStore / nonce crash・rollback safety | 12/12 PASS |
| Real nonce | 26/26 PASS |
| Real credential | 26/26 PASS |
| Real Transport fake adapter | PASS、externalNetwork=false |
| README Smoke / Material Smoke | PASS (5 runs / 1 case) |
| Node構文 / git diff --check | PASS |

Node→Python子process検証はsandbox内でEPERMとなったため、許可された非rootの
ローカル検証へ切り替えてPASSを確認した。sudoは使っていない。
実host Gate BとReal Secretの動作は未検証。
次はClaude Code Independent Re-review。Real activation/ACCEPTへのGOを意味しない。

新規9件には、distinct DUMMY ledger共存、空再利用のrevision +1、
ADMITTED/PREPARED/SEND_ATTEMPTEDの復旧専用維持、SEND_ATTEMPTEDでcustody bytes不変・
再送なし、空custody/dangling custody、ledger破損、pending artifact、
既存空ledgerとledgerなし両方の孤立Transport receipt拒否を含む。
Real Secret access=0、実host activation=0、sudo=0、External Write=0、pushなし。
