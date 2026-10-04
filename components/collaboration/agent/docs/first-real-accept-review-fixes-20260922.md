# First Real ACCEPT review follow-up — P3-1 fixed / P2-1 STOP

開始HEAD: `67b4cd9fdd9de5a5154b4cac57d2d57034aa10c8`、branch `main`、worktree clean。
Humanの追加指示に従い、P2-1を推測実装せず、P3-1だけを局所修正する。
Real Secret access 0、External Write 0、network request 0、host変更0、pushなし。

## P2-1: official source unavailable locally

Humanが指定したcurrent official sourceは
`flop-labs/technocore-chat@e4c4f73f3b28612d7161170b11e08e580b02123a`。
確認対象は `src/store.py`, `tests/test_signed_lane_stateful.py`, `SECURITY.md`, `README.md`。
このcommitのsourceは今回のローカル調査では確認できなかった。ネットワーク取得は行っていない。
repo内にある`5cc4ab93...`のpinは**tclk用**であり、Technocore serverのpinやserver実装を読んだ証拠にはならない。

保存済みofficial manualではnewest 1 MiBとbyte-exact exportを読めた。
`_last_nonce`と通常readが同じ`reverse_lines()`のdefault `READ_BUDGET = 1 MiB`を使う点はHuman提示情報であり、今回agentがserver sourceから独立確認した事実ではない。
byte offset、先頭partial line、malformed record、export snapshot末尾、generationの正確な実装を推測しない。

**P2-1は未解消。** 現在のnonce observationの `first_seq == 1` / `last_seq == len(rows) <= 200`条件、coverage packet、generation=1条件を変更していない。live roomのfunctional blockerは残る。
nonce store、read/export transport、Signer、Approval、credential、custody、IPCも変更していない。
newest-1-MiB coverageやgeneration inconsistencyを閉じたとは主張せず、新しいtail-window tests A〜Jも未実装。

P2再開には、指定commitの必要sourceをローカルに用意して内容を確認できることが必要。
その後、serverと一致するwindow/record境界を確認して実装・検証する。今回のnetwork禁止は維持する。

## P3-1: canonical signed-write URL

原因: `buildAcceptRequest`がroom/DID/signature/nonce/textすべてに`encodeURIComponent`を適用し、DIDのcolonを`%3A`にしていた。
修正後はvalidation済みraw segmentを使い、textだけをencodeする。

```text
https://technocore.chat/r/tclk-offers/say-signed/<PROJECT_DID>/<sig>/<nonce>/<encodeURIComponent(canonical-text)>
```

- roomは既存固定`tclk-offers`。DIDは既存のProject DID完全一致検証に加えてcanonical DID grammarを要求。
- signatureは86文字のunpadded base64url、末尾`A/Q/g/w`。nonceは最大19桁canonical decimal。segmentの`/`, `?`, `#`, padding等を拒否。
- textだけをencodeし、decode結果が署名対象の`record.line`と同一であることをテスト。
- 署名対象`<room>|<venue_nonce>|<canonical swept text>`、nonce値、署名bytesは変更しない。
- arbitrary URL/DID、Delivery、REVEAL、valueは引き続き拒否。
- credential available ≠ write enabled、Human Gate、ACCEPT/Paper-only、one-shot、proxy不使用、redirect禁止、retry 0を維持。
- HTTP 200もAMBIGUOUSのまま。STOP→public observation→exact envelope reconciliationの経路を変更しない。

変更ファイルは `src/collaboration_agent/real_transport.mjs`、`tests/real_transport.mjs`、この文書の3件だけ。
既存の歴史的文書は当時の記録として保持する。

## 検証

- `node tests/real_transport.mjs`: PASS。raw DID/no `%3A`、raw signature/nonce、text roundtrip、path injection負例、既存policy/one-shot/timeout/ambiguityを含む。adapterはfake、実network 0。
- `node tests/real_nonce.mjs`: 15/15 PASS（既存coverage policyの回帰。P2修正の証拠ではない）。
- `node tests/real_credential.mjs`: 26/26 PASS。
- `node tests/connection_approval.mjs`: 19/19 PASS。
- `node tests/connection_store.mjs`: 12/12 PASS。既存19桁・2^53超nonce、durable reservation/restartを含む。
- normal smoke / material smoke: PASS / PASS。
- 通常Offline suite: 114/114 PASS、49.830秒（既存Signer統合、Python nonce testsを含む）。
- connection / recovery / fake Real activation E2E: 67/67 PASS、9.337秒。
- Historical Evidence: 11,946ファイル照合、変更0。
- `git diff --check`: PASS。

次はClaude Code Independent Re-review。ただしP2未解消のためReal activationには進めない。
