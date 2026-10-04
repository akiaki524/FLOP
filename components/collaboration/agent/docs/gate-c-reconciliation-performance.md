# Gate C reconciliation性能の局所修正

開始HEAD: `2a4e1535e47505c19444a39b1fc3c70c23d577a9`。
対象はtargeted review P2-1の `matchFirstAccept()` のみ。

従来はsnapshotの全recordをseq重複検査とexact envelope検索の両方で署名検証していた。
修正後は妥当な整数seqの重複だけをSetで検査し、room / sender / nonce / canonical line /
OFFERより後のseqという安価な条件で候補を絞ってから、候補全件を既存 `verifyRecord()` で検証する。
全体の走査はO(N)、snapshotの署名検証は2N回から候補数K回になる。

seq検査とprefilterは認証ではない。timestamp上下限、envelope digest、署名検証を通った
exact matchがちょうど1件のときだけ `RECONCILED_PRESENT` とする。
0件・複数件は既存の `AMBIGUOUS` / retry false。最初の候補で探索を打ち切らない。
署名不正のrecordを含む妥当なseqの重複も拒否する（依頼された署名非依存の重複検査）。
30秒freshness、capturedAt、C1 packet、Final Revalidation、admit、approval、Transport、
Secret Boundary、one-shot / retry条件は変更していない。

## Offline検証

新規 `tests/first_accept_reconciliation.mjs` は固定ダミーseedとpinned公式署名実装を使用する。
20,000件それぞれに異なるnonceで署名し、raw export換算5,169,254 bytesのfixtureを作成する。
測定範囲は `matchFirstAccept()` の開始から終了まで。fixture生成・取得時間は含まない。

- targetあり: 22.47ms、`RECONCILED_PRESENT`。30,000ms未満をassert。
- targetなし（20,000件）: 8.41ms、`AMBIGUOUS` / retry false。
- 注入runtimeの公式verifierをラップして実検証を維持し、snapshot候補の検証回数をpin。
  targetありは1回、なしは0回、exact候補2件は2回。固定OFFERの検証は別途1回。
- targetありfixtureではnonce observationにもtargetを設定し、既存の別runtime経由の
  observation検証1回も維持する。この1回はラップしたcall countの対象外。
  総署名検証回数は旧40,002回相当から3回（候補1 + OFFER 1 + observation 1）。
- 不正署名は、正しい形式で別メッセージを署名した値を使い、full cryptoで拒否。
- nonce一致/text違い、text一致/sender違い、nonce違い、room違い、seq <= OFFERを除外。
- 重複seq（正当署名・不正署名）、複数exact候補、timestamp/digest不一致を拒否。
- 正常候補と不正署名候補が混在しても、順序によらず認証済みexact matchだけで判定。
- freshnessは30,000msで受理、30,001msで拒否。

結果: 新規7 tests、既存packet 13 tests、Gate C fake E2E 6 tests、host 17 tests、
通常offline suite 142 testsが成功。`real_transport.mjs` はPASS、retryCount=0、
externalNetwork=false。`git diff --check` 成功。
fake E2Eは既知のAF_UNIX sandbox制限のため、承認機構経由でローカルfixtureのみ実行した。

再現:

```bash
node tests/first_accept_reconciliation.mjs
node tests/first_accept_packet.mjs
node tests/real_transport.mjs
python3 -B tests/connection_first_accept.py -q
python3 -B tests/gate_c_host.py -q
python3 -B -m unittest discover -s tests -q
git diff --check
```

Real Secret access=0、sudo=0、live GET=0、実External Write=0、pushなし。
実host/live C1/C2は未実施。性能値はこのローカルfixtureでの実測であり、live環境の保証ではない。
次はClaude targeted re-review。
