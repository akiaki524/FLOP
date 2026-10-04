# 検証記録

## 現在の判定（2026-09-07）

最新のcode/offline結果は[Pre-VPS final remediation](pre-vps-final-remediation.json)を参照してください。以下の123件とDocker測定は旧baselineの履歴です。current implementationは`MAX_BODY=4 MiB`、`json.loads`前のJSON depth 32 / structure 32,768のpre-budgetを使います。旧32 MiBのartifactを現行実装の測定結果として扱いません。

**Local Persistent Storage Durability gate: COMPLETE。** 実Docker artifact `durability-results-recreation-20260907-192144-4562d78c` を確認し、Local結果 `LOCAL_DURABILITY_PASS_WITH_HARDENING_WARNINGS` を受理した。isolation boundaryとpersistent storage durabilityはPASS、noexec/nosuid/nodevはWARN、`production_hardening_gate=PENDING`。過去のSTOPPED reportやhelper単体の `full_durability_gate=PENDING` は改変せず、host process matrixとの統合判定を [機械可読review](final-regression-20260907.json) に記録した。

Observer本体7ファイルを変更せず、実測manifestの全15 source hashが一致した。A削除→同一named volumeでB作成→poll_seq=201から203への継続、resolved_seq=103、OPEN gap104..199、DEGRADED、全保存message保持を確認した。SQLite backup APIで回収した32 KiBのDBはSHA-256一致、read-only再検査でintegrity/quick_check=ok、application_id/user_version一致。詳細は [durability検証](durability-verification.md) を参照。

final offline regressionは **123 tests PASS、skip 0**。以下の2実行で確認した。

```text
env PYTHONPATH=src:tests python3 -B -m unittest test_observer test_crash test_live_compatibility test_live_smoke test_soak test_durability test_container_recreation test_durability_diagnostics
118 tests / 18.920 s / OK

env PYTHONPATH=src:tests python3 -B -m unittest test_http
5 tests / 0.833 s / OK
```

HTTP suiteの最初のsandbox内試行はsocket作成のPermissionErrorで0 testsだった。同じ未変更commandを承認済みsandbox外で実行し、127.0.0.1だけで5 testsがPASSした。その他118 testsには保存済みlive responseのoffline replay、31ケースのprocess durability matrixを含む。追加live通信、Docker操作、soak再実行、Observer本体変更は行っていない。

Localでのprocess crashとDocker recreationは確認済みだが、Docker内の全crash matrix、Windows/WSL/VM再起動、電源断、VPS production実環境の耐久性を確認したとは扱わない。[VPS移行前checklist](vps-migration-checklist.md) に残りのsecurity/operations gateと監査・承認を整理した。

以下は経緯と過去の個別実行記録。

実装基準: 提示されたObserver Specification v0.3。外部サイトへの取得、upstream repositoryの参照・copyは行っていません。確認日: 2026-09-06、Python 3.12.3。

現在のTechnocore target/baselineは **0.12.1**。ユーザーから、reaper修正に伴うroute・parameter・response shape・documented API capの変更はないとの確認が提供されたため、期待versionのみ追従しました。architecture、sequence/generation/gap設計、既存P0 tests、5xx/503のtransient handling、backoff、stillborn_seconds=43200の扱いは変更していません。

0.12.1追従後の再検証: 既存 `test_observer.py` 23件と追加 `test_live_smoke.py` 11件が成功。既存P0 testファイルは変更していません。base-image隔離とObserver-code/live検証の範囲は [live smoke計画](live-smoke-plan.md) に記録しています。

## Live smoke後の修正・再検証（2026-09-06）

修正後のlive再実行も `docs/smoke-results-20260906-125741-7d70a083/` でoffline/live PASSを確認した。現在sourceとarchiveの8 hash、6 body hash、request計画・間隔、config取得、隔離検査、SQLite整合が一致。新しい200件のoffline replayでもflagsなし・raw/text保持・poll=resolved=749を確認した。新規ネットワーク取得や既存P0 test変更は行っていない。下記41件は前回のテスト実行記録であり、今回の証跡検証で全件再実行したという意味ではない。soakは [計画作成](soak-plan.md) まで完了し未開始。

通常Ubuntu terminalでの隔離container試験はoffline/liveともPASS、liveは6 GETでCOMPLETEでした。保存証跡の確認で、configの参照位置とcontent期待型の2点に実装上の誤りを検出し、修正しました。version差分によるAPI変更ではありません。[詳細と未確定項目](smoke-review-20260906.md)を参照してください。

修正後、次の個別コマンドがすべて成功しました。

```text
python3 -B -m unittest discover -s tests -p test_live_compatibility.py -v
7 tests: OK（保存証跡を使う2件も実行、skipなし）

python3 -B -m unittest discover -s tests -p test_live_smoke.py -v
11 tests: OK

python3 -B -m unittest discover -s tests -p test_observer.py -v
23 tests: OK
```

今回の再検証は計41件です。既存P0 testファイルとsequence/generation/gap、5xx/503/backoffの処理は変更していません。HTTP・SIGKILL試験の過去結果は下記に残し、今回の再実行件数には含めません。追加テストは外部通信せず、保存済み200件を一時DBへ投入してraw record・textの一致、flagsなし、poll/resolved=725、OPEN gapなしを確認します。privateな保存証跡がない環境ではその2件だけskipし、synthetic fixtureの5件は実行します。

## 実行結果

`python3 -B -m unittest discover -s tests -v` でHTTP以外の24テストが成功しました。HTTP fixtureのsocket作成はsandboxが拒否したため、この最初のコマンド全体は非0終了です。

承認後、`python3 -B -m unittest discover -s tests -p test_http.py -v` をsandbox外で実行し、127.0.0.1のみを使用するHTTP 5テストが成功しました。

最終確認で同一process内のterminal status gateとstale init sidecar拒否を補強し、2テストを追加しました。関連する再検証結果は次のとおりです。

```text
python3 -B -m unittest discover -s tests -p test_observer.py -v
23 tests: OK

python3 -B -m unittest discover -s tests -p test_crash.py -v
3 tests: OK
```

変更のなかったHTTP 5件と合わせ、**合計31テストメソッド成功**です。一括コマンドの全件成功を主張するものではありません。SIGKILLの各停止点や各response variantはsubtestに含まれます。CLIの `--help` も正常終了を確認しました。

| 項目 | 検証内容 |
| --- | --- |
| Normal / empty | contiguous保存、anchor除外、empty last_seqでcursor不変 |
| Gaps | 101..499と700..899を保存、poll=1099/resolved=100、DEGRADED |
| Ordering | duplicate、seq==cursor、seq<cursor、hole、descending、invalid seq型 |
| Envelope | generation欠落/型異常、room/count/first/last不一致、messages型異常 |
| Content | ts/text/from欠落、型異常、unknown fields、raw record保存、flags |
| HTTP format | malformed/truncated JSON、不正UTF-8、重複key、NaN、wrong Content-Type |
| Body limit（旧baseline） | 当時は実HTTP fixtureで32 MiB+1を送信。current implementationはMAX_BODY=4 MiB、4 MiB+1のloopback fixtureとJSON pre-budgetを検証 |
| HTTP failure | 3xx非追従、400/404停止、429/500/503 retry、timeout/reset |
| HTTP security | proxy環境変数の無効化、TLS検証有効、固定query、URL/path制限 |
| Rate handling | Retry-After秒/日付、600秒clamp、invalid値、wait_held=false delay |
| Generation | emptyを含む変更でInbox非保存、event/evidence、NEEDS_RESYNC、再起動拒否 |
| Anomaly retry | 同一cursorで3回異常、ERROR、valid responseでcounter reset |
| Runtime crash | 受信直後、message INSERT中、gap INSERT後、cursor UPDATE前、COMMIT前後でSIGKILL |
| Init crash | temp作成後、schema作成中、state INSERT後、COMMIT後、rename前後でSIGKILL |
| State | DB不存在、application_id/user_version、破損DB、論理cursor不整合、room不一致 |
| Disk / permission | 実際のSQLITE_FULL、transaction途中のwrite失敗注入、接続PermissionError注入 |
| Lock | 別process二つのうち一方のみflock成功、二つ目は即拒否 |
| SQLite | WAL/FULL/foreign_keys読戻し、WAL不存在で起動、read-only reader |
| Display | ANSI/NUL/BiDi/newline/長い本文をDB保持、表示で制御文字除去 |
| Deployment check | VERSION_CHANGED、retention hint、watchdogのlag/generation判定 |

SIGKILL試験はprocess crash時のSQLite rollback/commitを確認します。電源断、filesystemやstorage deviceのfsync違反を再現したものではありません。permissionの一部はfault injectionであり、専用OS userの権限試験は未実施です。

## 未完了

soak helperの実装、host上のoffline検証、Docker内6秒offline検証は完了した。その後のlive soakは300試行で完走し、通信失敗2回・503が1回のためWARNを維持する。新着4件の保存、wait_held=falseの1回、watchdog 11回、backup 12件を確認した。再実行は行わない。telemetryだけを最小修正し、新規5件を含む30件のoffline regressionが成功した。[検証記録](soak-verification.md)を参照。既存P0 testファイルとObserver本体処理は変更していない。

Persistent Storage DurabilityのLocal gateは、host filesystem上の31ケースと実Docker named-volume recreationで完了した。COMMIT前後の実停止はhost process matrix、volume保持・A削除/B再作成はDockerで検証した。[durability検証記録](durability-verification.md)を参照。VPS production hardeningは未達。追加live通信は行っていない。

- Containerfileから作るdeployment imageと永続volumeでの隔離・運用検証。smokeのbase image＋source zip＋tmpfsでの隔離はPASS。
- limit=201の200件応答はclampと整合するが、保持件数が200件超だったことは独立に確認していない。これを強制する追加requestは行わない。
- VPS productionでのmount hardening、deployment image/永続stateを用いた再開、backup/recovery、supervisor/watchdogの実環境確認。Local durability完了からproduction PASSを推定しない。
- Claude/Fableの独立コード監査、Human Approval、VPS移行。

これらが完了するまでVPS migration gateは未達です。外部監査サービスへの送信・公開、git commit/pushは行っていません。
