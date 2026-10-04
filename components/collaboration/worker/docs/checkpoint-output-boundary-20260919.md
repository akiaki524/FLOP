# Output Boundary Offline Implementation — 2026-09-19

開始: main / `38eb1113cadb45149a807c871724a2df627395e8` / clean。
Real Claude、実credential、Network、Production、外部writeは未使用。Real enable経路なし。

## 構造

既存Activity socket → task digest admission → consumed.json永続化 →
隔離したFake Runtime（起動1回）→ bounded stdout/stderr → Secret scan →
厳格JSON/schema/引用検証 → canonical envelope size検査 → typed ReviewResult → 表示専用Preview。
既存lock、single-use、ready handshake、Landlock/seccomp、子process cleanupを再利用。
汎用provider選択、tool dispatch、shell、URL取得、retry/fallbackは追加していない。

主防御はモデル相当processへSecretを渡さないこと。旧private envelopeのdummy_credentialを除去し、
requestのみを渡す。環境は空、workspaceは空かつread-only、Service memory/FDも既存保護を維持。
Service生成のrandom dummyはmemory内のsecondary scanにだけ使用する。
Fake adversaryはテスト専用の公開canaryを独立に知っており、実モデル入力から受け取らない。

受信上限はstdout/stderr合計32,768 bytes（ready byteを含む）。超過時は子を停止・回収し、
途中出力を公開しない。非zero終了・timeout・不正readyも拒否する。
JSONの重複key・非有限値・不正UTF-8・不正schema・未知field・不正引用・別providerを拒否。
文字列中のURL/command/instructionはdataとして受理できるが、tool request fieldは拒否する。
最終canonical result全体も32,768 bytes以下。UTF-8→ASCII JSON展開とenvelope追加分を含む。
Activity client側でもcanonical budgetと既存review validatorを再確認する。

scanは平文、Base64、URL-safe Base64、hex（大小）の既知値をstdout/stderrのchunk境界をまたいで検査し、
JSON escape復元後も確認する。一致時は全文withholdし、部分的なredaction結果も返さない。
stderrは常に破棄。任意変換・分割された意味的漏洩・部分Secretを全て検出する保証ではない。
scanをcredential隔離の代用にしてはならない。

PreviewはASCII JSON textを生成するだけ。untrusted_output=true、display_only=true、
runtime_kind=offline-fake、external_actions_executed=falseを明示。
terminal control・bidi文字もescapeし、HTML/Markdown renderer・URL fetch・tool executorへ接続しない。
Real表記の準備はあるが現行RuntimeClientはoffline=trueのみ受理する。

## F-2 / Evidence

attempt消費はdummy生成・runtime起動より前。以後の失敗で消費を戻さない。
同rootの再起動・再requestは拒否し、既存Evidenceを書き換えない。
将来の再実行にはHumanの新しいauthorizationが必要。このServiceに再許可APIはない。
rootを作り直して不明attemptを自動再実行することも認めない。

Evidence v2は固定metadataのみ: offline種別、消費済み、実際にPopenできた起動数、
exchange正常完了の有無、成功/拒否、failure、retry/fallbackなし、外部action未実行。
failureはtimeout / runtime_failure / oversized_output / malformed_output /
secret_withheld / internal_failure。runtime_failureは起動・handoff・非zero終了等の観測であり、
providerが応答したとの主張ではない。複数異常時は最初に検知した停止理由であり網羅的診断ではない。
runtime_completedはexit 0とcleanupを含むexchange成功時のみtrue。
provider_usage / provider_chargeはnull。開始・失敗から課金有無や消費token数を推測しない。
prompt、raw output、stderr本文、Secret、canary、scan一致内容・そのdigestをEvidenceへ保存しない。

SIGKILL/crash/保存障害ではconsumedのみ、または不完全Evidenceが残り得る。結果不明・再試行禁止。
成功Evidenceはvalidation成功を示し、Activityへの配信完了証明ではない。
旧B0/B1/B3 brokerのraw保存方式は本Serviceへ流用せず、既存経路も変更していない。

## Verification

独立した隔離Fakeでランダム正常出力、命令/URL文字列、不正schema・引用・追加tool field、
重複key、不正UTF-8、平文/Base64/hex/JSON escape/chunk分割/ stderrのcanary、
stdout/stderr超過、canonical展開、timeout、runtime failure、内部失敗を検査。
全失敗後の消費保持・同root再実行拒否・Evidence不変・canary非保存を確認する。
最終envelopeの32,768 bytes丁度と1 byte超過も検査。
socket生成・exec・非許可file read・workspace writeの拒否は既存boundary probeで確認。
既存F-1のadmission canonical budget回帰も保持。

実測結果:

- `python3 -B -m unittest discover -s tests -v`: 107 tests PASS（48.609秒）。
- その後にenvelope丁度/1 byte超過テストを追加し、Runtime Service専用suite:
  12 tests PASS（16.223秒）。製品codeは全体suite時と同じ。
- isolation: Landlock ABI 3、confinement available。
- README全Smoke PASS: smoke、b1、secret_handoff、secret_boundary、secret_abc、
  secret_consumer_window、runtime_service。runtime_serviceの--materialもPASS。
- 本Batchの検証失敗・修正再試行なし。既存境界へのregressionは観測なし。
- `git diff --check`: PASS。

生成データはgit対象外の.local/のみ。主な成果物:

- `.local/runtime-service-smoke-oyy2k694/`（通常Preview）
- `.local/runtime-service-smoke-gerlix4y/`（保存済み公開資料Preview、取得なし）
- `.local/smoke-eie5xqgt/`、`.local/b1-smoke-4xp82qar/`
- `.local/secret-handoff-dqoun44u/`、`.local/secret-boundary-pwti0fla/`
- `.local/secret-abc-f4gbhm_g/`、`.local/secret-consumer-window-yx3irjgp/`

旧baselineの漏洩を再現する既存probeの成果物は公開dummy専用。本ServiceのEvidenceと区別する。

## Security / 次のGate

本検証範囲で新しいSecurity Findingは観測していない。独立Reviewは未実施。
F-2は巻戻し修正ではなく明示的なattempt消費semanticsとして採用。
schema/引用一致は出力内容の正しさやprompt injection耐性の証明ではなく、Humanが内容を判断する。
trusted Supervisor/OS/Python、同UID脅威範囲、終了signal、memory zeroization等の既存残Riskは維持。

次はこの差分のIndependent Review。その後Real直前のHuman Gateで、credentialをmodelから
分離する実adapter構造、実CLI/version/flag/effective tools、network/egress、認証方式、
provider出力mapping・内部retry/fallback・usage/課金を別途確認する。
今回のFake検証を実Claudeのこれらの保証へ転用しない。push / PR / publishなし。
