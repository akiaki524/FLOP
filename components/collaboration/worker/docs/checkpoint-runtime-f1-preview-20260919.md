# Runtime Service F-1 fix / real material Offline Preview (2026-09-19)

開始状態: main / `a3c6e5de68b67a0141361b433f9366ef3bce807e` / clean。
前回Independent ReviewはAPPROVE WITH RESIDUAL RISKS、P3 F-1のみ。
本Batchはその限定修正と、ユーザーが本文を提示した公開技術資料のOffline検証。

## F-1

原因はraw wire bytesだけを検査し、ensure_ascii=Trueのcanonical JSONへの
非ASCII展開をadmission時に制限していなかったこと。
`src/ccw/runtime_interface.py:parse_request`のschema・文字数検査後に
`len(canonical(data)) <= MAX_REQUEST_BYTES`を追加した。
Serviceの順序はparse（raw / schema / canonical budget）→ task digest照合 →
consumed.json永続化 → dummy生成 → runtime起動。oversizeは消費前に拒否する。
runtimeのprivate envelope上限、handoff、timeout、framing、認証方式は変更していない。

専用回帰テストはclient encodeを迂回するUTF-8 wire入力を使い、正しいdigestを事前許可。
21,000文字の「あ」による膨張、canonical 65,536 bytes、65,537 bytesを検証した。
上限ちょうどは既存の隔離runtimeで成功。超過2件は固定refused envelopeで拒否し、
consumed.json / evidence.json / dummy生成marker / runtime呼出markerが全て存在しない。
stderrは空、runtime workspaceは空、公開canaryの平文・Base64・hex漏洩なし。
markerはテストSupervisor限定で、製品のcapabilityや永続出力には追加していない。
既存ASCII正常requestも成功。Runtime Service専用suiteは10 tests PASS。

## 実資料とHuman Preview

Source: FLOP Network Yellow Paper v0.5.0、
§6.2 Agent autonomy — operating without per-action consensus。
ユーザー提示の本文をそのまま固定（末尾改行なし）し、外部取得・真正性検証はしていない。
本文はuntrusted technical document dataであり、MUST等をAgentへの命令として実行しない。

実行: `python3 -B tests/runtime_service_smoke.py --material`

- typed ReviewRequest → digest authorization → Service accept → isolated Offline Fixture →
  ReviewResult → standalone Human Preview: PASS。
- request bytes: **1,073**、canonical bytes: **1,073**（既存clientがcanonicalで送る）。
- Task digest: `60a64e97df7114e833cd4aeec4a321c4488733350ddb61224259d1865371207c`
- runtime_starts=1、automatic_retry=false、consumed記録あり、runtime workspace空。
- Activity／Service stderr空。Activityはcredential layerをimportしない。
- Result: provider=claude-cli-offline-v1、findings=[]、既存のTODO検出summary。
- Source label、digest、offline=true、real_inference=false、ResultをPreviewで確認した。
- TODOが本文にないため指摘なしとなる。Securityの内容理解・要約品質・安全性の証明ではない。

Fixtureは変更していない。既存operation `review_saved_document`の固定TODOレビューに
合わせたflow検証であり、任意task instruction fieldや自動許可APIを追加していない。
smokeの固定Supervisorがこの1件を事前許可する。Human brokerの送信承認ではない。

git対象外の成果物:

- `.local/runtime-service-smoke-kuo5r3xs/request.json`
- `.local/runtime-service-smoke-kuo5r3xs/human-preview.json`
- `.local/runtime-service-smoke-kuo5r3xs/human-preview.txt`（読みやすい日本語表記）
- `.local/runtime-service-smoke-kuo5r3xs/summary.json`
- 通常入力: `.local/runtime-service-smoke-8htetyfe/`

## Verification

- `python3 -B -m unittest discover -s tests -v`: **106 tests PASS**。
  Runtime Service / Worker / Secret Handoff / isolationの回帰なし。
- `python3 -B src/ccw/isolation.py`: Landlock ABI 3、confinement available。
- README smoke全PASS: smoke.py、b1_smoke.py、secret_handoff_probe.py、
  secret_boundary_probe.py、secret_abc_probe.py、secret_consumer_window_probe.py、
  runtime_service_smoke.py。実資料用--materialもPASS。
- boundary probe初回はsandbox内の匿名socket送信EPERM。
  ABC初回は同期timeout、consumer-window初回はcurrent consumer未起動でFAIL。
  後者2件も同じsandbox制約と推定し、承認された通常ローカル実行で各1回再検証し全PASS。
  修正・隔離緩和・追加retryはしていない。
- 再検証成果物: `.local/secret-boundary-v3hvhg1t/`、`.local/secret-abc-v5yejfr4/`、
  `.local/secret-consumer-window-ll2dvduo/`。
- leak検証は公開dummyの平文・Base64・hexと既存fault注入で実施。
  旧baselineの既知漏洩再現を、現行実装の新規Findingとは数えない。

## Security / checkpoint

今回の検証範囲で新規Findingなし。F-1の修正後Independent Reviewは未実施。
Activity APIのcredential / shell / URL capability追加なし。fallbackなし。
実Secret・実credential・実Claude・paid inference・外部送信・Production変更なし。
既存Service内のmemory-only random dummyだけを使用した。
Known Residual Risks（root、trusted Supervisor、kernel、same-UID native attacker、
lifecycle、Python zeroization、root単位concurrency、Production別UID／systemd／
credential方式／実Claude出力境界）に変更なし。

この4ファイルを明示選択してlocal commitする。生成データは.local/に保持しcommitしない。
push / PR / publishなし。次の自然な一段階は、この限定差分のIndependent Reviewで
F-1のclosureと実資料Previewの検証根拠を確認すること。Production Gateとは別。
