# Close Call Human-supervised one-shot signing

この追加経路は `src/close_call_one_shot.mjs` のみを入口とする、
first Taker-Long 用の Human-supervised 署名です。Real 利用はまだ許可・検証していません。
Claude Code Independent Review と具体的な Human の Real signing 承認を受ける前に
Real seed を入力しないでください。POST は別の Human Gate であり、この入口には実装しません。

## Contract and authority

Human は既存 `close-call-taker-long/1` policy と既存の
`CLOSE_CALL_TYPED_SIGN_REQUEST` / `CLOSE_CALL_TAKER_LONG` request 全体を確認し、
独立した approval JSON を Human 管理の場所に用意します。
request の `approved=true` 等は許可根拠にしません。形式は以下です。

```json
{
  "version": 1,
  "operationDir": "<absolute Human-owned attempt directory>",
  "requestSha256": "<SHA-256 of the exact request file bytes, including any final newline>",
  "policy": { "...": "<complete reviewed close-call-taker-long/1 policy fields>" }
}
```

`policy` は既存 validation が受理する完全な object で、expected Project DID、
Referee DID、rules commit、package hash、2署名quota、lease、freshness、lock 時刻を含みます。
request の nonce / final trade / signing bytes は既存 Signer が構築します。
完全な test-key object は `tests/close_call_one_shot.test.mjs` の fixture を参照してください。
Human は expected Project DID と Referee DID を request の自己申告から採用しないでください。

approval / request は起動 UID 所有の非 symlink regular file、group / other write 不可。
operation directory はその UID 所有の非 symlink directory、mode 0700、初回は空であることが必要です。
approval はこの directory の絶対パスと request の exact bytes を固定します。
これらと親 directory を Agent が作成・編集・削除・差し替えできない場所で Human が管理し、
Human が直接起動します。Agent-facing RPC、socket、approval 作成 API はありません。

同じ UID の悪意あるプロセスや root に対する隔離はこの入口では提供しません。
既存24H AgentのUIDでの無人実行には使いません。approval は Human-controlled launch の
local artifact であり、Human の存在を暗号学的に証明するものではありません。
policy / approval / operation directory をAgent管理下へ移す運用は、この範囲に含みません。

## Ingress and result

起動形式（Public なパスと FD 番号だけを argv に渡します）:

```text
node components/signer/src/close_call_one_shot.mjs \
  --approval <absolute approval.json> \
  --request <absolute request.json> \
  --operation-dir <same absolute attempt directory as approval> \
  [--input-fd <3..1024>]
```

省略時は stdin、指定時は指定 FD の匿名 pipe だけを受け付けます。
Human 承認済みの seed 提供機構から raw Ed25519 seed 32 bytes を渡し、EOF を閉じてください。
hex / base64、regular file、named FIFO、TTY は受理しません。
seed を argv / env / plaintext file に置く入力経路はありません。
既存 `assertHostCrashSafety()` を seed read 前に実行し、WSL の不安全な core capture 設定では停止します。

1 process 内で既存 PolicySigner を作り、入力 seed buffer と受信 chunk を zeroize します。
既存 Trusted Reader の固定 GET を `close1` → `d-close1-price` の順で各1回実行し、
直後に既存 `closeCallTakerLong()` で価格・maker署名・lease・nonceを再検証して署名します。
HTTP response の framing / generation / size / redirect、record署名も既存検証を再利用します。
Agent から snapshot / URL / nonce を注入する CLI option はありません。

成功時 stdout に返す JSON は以下の3フィールドだけです。

- `takerSignature`: Taker countersign
- `roomRecord`: 既存 POST-ready package（`room`, `nonce`, `sender`, `signature`, `line`）
- `price`: 署名直前に再計算した `sweep` / `referenceAgeSeconds` / `stale`。
  既存 profile どおり stale は拒否条件ではないため、Human は POST 判断前にこの値を確認してください。

`line` の final trade JSON に Taker signature が入り、room envelope signature と合わせて2署名です。
19桁 nonce は decimal string のまま保持します。HTTP wire encoding / dispatch はこの経路の対象外です。
失敗時は署名packageを返さず、固定診断 `CLOSE_CALL_ONESHOT_DENIED_NO_RETRY` と exit 70 を返します。

## One attempt, no recovery loop

seed read 前に `attempt.json` を exclusive create し、file / directory fsync します。
同じ directory の2重起動、成功後再実行、partial marker、失敗後再実行は拒否します。
自動retry / restart / marker削除 / state再初期化機能はありません。
別directoryを渡して同じapprovalを再利用することも拒否します。

directoryには試行記録、公開read snapshot、既存方式の encrypted policy stateだけを保存します。
seed / private key / encrypted credential は保存しません。credential handoff、systemd、
socket activation、新user、daemon、24H Runtime変更は不要です。

試行記録は成功・失敗・中断後も保持してください。directoryの削除・移動、approvalの再発行は
別のHuman判断であり、通常の再試行手順にしてはいけません。
最悪の失敗は署名済みpackageの出力直前のprocess停止です。署名生成の有無が曖昧でも再署名しません。
外部操作が行われていた場合は read-only reconciliation とHuman判断が必要です。
Git rollback は試行記録や外部結果を戻しません。

normal exit / exception / CLI の SIGINT・SIGTERM で所有するseed bufferをzeroizeします。
SIGKILL、native crash、host停止ではJavaScript cleanupを保証できません。
Ed25519 KeyObjectのnative memory消去は既存helperとprocess終了に依存します。
WSL crash-safety gate は必須ですが、これをメモリ消去の証明とは扱いません。

## Offline verification and review handoff

```bash
cd components/signer
node --test tests/close_call_one_shot.test.mjs
node --test tests/*.test.mjs
node --check src/close_call_one_shot.mjs
```

新規testはtest keyと実匿名pipe/FDを使い、positive、invalid typed request、approval mismatch、
DID mismatch、changed authenticated price、duplicate/concurrent/retry、partial attempt、
network failure、unsafe WSL crash capture、zeroization、file/named FIFO拒否を検証します。
Pythonの`os.pipe()`はテスト用で、実装に新しい依存はありません。
live GET、Real seed、POST、sudo/systemdはテストしません。

Claude Code は今回のdiffを先に読み、既存validation/readerとの接続、Human approvalのauthority、
匿名入力とWSL gateの順序、nonce/価格freshness、zeroization、再試行防止、外部write不在を
Independent Reviewしてください。Real seedやRuntime mutationは不要です。
レビュー結果が出るまでは実装・offline検証済みとしてのみ扱い、Real-readyとはしません。
