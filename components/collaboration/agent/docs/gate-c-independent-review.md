# Gate C Independent Review handoff

この変更はHumanによるFirst Real Paper ACCEPT 1件だけのためのC1/C2 orchestration。
今回のbaseは `5732f5f0b844ddc2d81500f1838d5fbda79fb525`。
Humanから報告されたClaudeのP1-1/P1-2/P2-1を局所修正する。
**修正後のClaude Re-reviewは未実施**。外部モデルへコードを送信せず、この資料を残す。
実Secret・実host activation・Technocore writeは検証に使っていない。
実host実行のGOを示す資料ではない。

## Reviewerへの依頼

実装者の説明を正しさの証拠とせず、sourceとtestを独立にレビューする。
各findingにseverity、file/function、具体的な到達条件、影響、最小修正案を付ける。
未確認事項をPASSにしない。Secretを探す・読む、sudo、host activation、
Technocore request、外部送信、push、コード変更はレビューに不要。

要求する境界:

- C1は固定public export GETのみ。generation=1、HTTP完全性、nonce profileを検査する。
- official pinned tclkで署名・canonical OFFER・Paper/hash/no-value条件・期限を検証する。
- malformed、expired、self-offer、unsupportedを除外。入力順を保ち、Humanが明示選択する。
- ACCEPT payload、contract、nonce、digestを固定したpacketを保存し、C1は必ず停止する。
- C2はArm→Human Final Approval→fresh Final Revalidation→admitの順。C1時刻を実行deadlineにしない。
- frozen nonce/payload/contract/digestは不変。nonce/offer状態変化はadmit前にRETURN_C1。
- admit後にlocal freshness/session timerだけで中断しない。protocolの実期限は維持する。
- Real seedは既存Gate B anonymous stdin handoffのみ。固定Project DID一致が必要。
- Armは固定packet限定write policy準備まで行うがadmit/sendしない。Human approval後にPaper ACCEPTだけ1回。
- pre-admit clean failureはrollback＋actual safe-emptyを検証後に新UUIDでやり直せる。marker単独で禁止しない。
- post-admit retry、second ACCEPT、Delivery、REVEAL、value、Autopilotを開始しない。
- send後はAMBIGUOUSかつlogical STOP。公開exact signed record一致のみRECONCILED_PRESENT。
- cleanupはingress→Transport→Signer停止、Project credential暗号文除去、write OFF設定復元。
- ledger、custody、nonce、receipt、quota、attempt markerを削除・rollbackしない。
- Signerネットワーク、Worker/Approval/Transportのcredentialアクセスを広げない。

## Source map

| ファイル | 確認点 |
| --- | --- |
| `src/collaboration_agent/first_accept_read.py`, `real_nonce.py` | 固定GET、framing、lossless nonce、bounded export |
| `src/collaboration_agent/first_accept_packet.mjs` | 候補policy、Human selection、packet固定・再検証、exact照合 |
| `deploy/connection/gate_c.py` | host preflight、immutable artifact、terminal承認、handoff、transaction/cleanup |
| `deploy/connection/gate_c_client.mjs` | 固定socket呼出順、admit後の失敗分類、send一度、reconcileのみ |
| `src/collaboration_agent/connection_runtime.mjs` | Project DID、preimage credential、frozen nonce、safe-empty/recovery境界 |
| `src/collaboration_agent/pilot_signer.mjs` | exact subject/action、承認、send後STOP、reconciliation後もSTOP |
| `src/collaboration_agent/connection_services.mjs` | Transportでの二重packet binding、write OFFの先行拒否 |
| `src/collaboration_agent/real_transport.mjs` | 既存fixed writer、exclusive receipt、retryなし（無変更） |
| `src/collaboration_agent/connection_store.mjs`, `pilot_ledger.mjs` | 既存durable quota/custody/nonce safety（無変更） |
| `deploy/connection/handoff_real.mjs`, `gate_b.py`, `gate_a.py`, `recover_smoke.py` | 既存handoff/trust/rollbackの再利用（無変更） |

今回のtest変更: `first_accept_packet.mjs`, `connection_first_accept.py`, `gate_c_host.py`。
reader、credential handoff、nonce store、ledger、Transport writerは今回無変更。

## 特に確認するFailure window

1. C1から61秒以上のHuman delay、Arm、final approval後のfresh snapshot。packetを変更しないこと。
2. custody作成後、ADMITTED前のcrash。C1に戻す/義務を消す操作がないこと。
3. SEND_ATTEMPTED/receipt保存後に応答が失われる。再送・別packet実行へ進まないこと。
4. public read失敗、exact record不在・改変、世代変更でも成功と扱わないこと。
5. cleanup途中crash、異なるlive file内容、残存drop-in/credential、非空Transport state。
6. Gate B後の空Project ledgerと既存DUMMY ledgerが共存する正規経路。
7. preimageのplaintextがdisk/argv/env/evidenceへ出ないこと。
8. admit後に注入clockを31秒以上進めてもlocal freshnessだけで中断しないこと。
9. marker-only/Arm failureはactual safe-empty＋rollbackで新attemptを許し、post-admitは拒否すること。
10. bound approval expiryは既承認OFFER期限由来であり、schema/digest bindingを弱めないこと。

## 限界

HTTP generation/framingは応答から検査する。nonce PROFILEは既存server sourceの静的互換pinであり、
現在のremote deploymentのコード同一性をattestする仕組みではない。
root checkoutとroot-only artifact、Human-controlled public-read coordinatorを信頼する。
root権限での同時state改変や全snapshot rollbackに対する新しい防御は追加しない。

offline E2Eはtest identity、temporary files、INET拒否、fake Transportを使用する。
host runnerはsystemd/credential encryptionをmockした契約テストであり、
実systemd配送・実ACL・実host rollback・現在のTechnocore挙動を実測したものではない。
検証結果とHuman手順は [Gate C手順](gate-c-first-accept.md) を参照。

## ローカルreview bundle

commit後に `.local/gate-c-functional-fix-review/` へ以下を生成する（Git管理外）。

- `review.txt`: この依頼、対象commit情報、対象source/test全文
- `change.patch`: 開始HEADから対象commitへの差分
- `manifest.json`: base/target HEADと各artifactのSHA-256

Humanが内容と送信先を確認してClaudeへ渡す。Codexから送信しない。
レビューは対象commitに固定し、結果をこの作業へ返す。
大きなsource bundleを渡せない場合は、同じcheckoutと `change.patch`、
本書のsource mapを使ってHumanがClaudeの読取範囲を指定する。
