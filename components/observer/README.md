# Technocore Read-only Observer

Observer Specification v0.3（target: Technocore 0.12.1）に基づく、Python stdlibのみの取得・保存層です。既存の `technocore_observer` はpublic messageを検証してSQLiteへ保存し、prefix gapを永続化します。このObserver coreはAnalyzer、署名、External Write、Observer stateの自動recovery・自動resyncを実装していません。

Projectとしての**Current Observer要求仕様**は [SPEC.md](SPEC.md) を正本とします。`SPEC.md` は目標・保存・並行観測・復旧・通知等の要求を定め、実装済み／Production適合済みを意味しません。Runtimeの事実はCurrent Runtime Evidenceを優先します。

独立したopt-in `technocore_full_capture` subsystemには、Capture continuity gapのbounded recoveryが含まれます。既存Observer coreとは別の経路で、導入・Live / Production利用にはHuman Gateが必要です。Signer / Wallet / External Writeは含みません。この統合はProduction readinessを意味しません。[Candidate integration contract](docs/capture-candidate-integration.md)を参照してください。 V2 Upgrade Harnessのoffline実装とHuman Gateは[Current V2 contract](docs/capture-upgrade-v2.md)に記録しています。Production GOではありません。

Pre-VPS P1対応でschema v2・明示的なoffline manual resync・保存容量/JSON予算を追加しました。[修正と検証](docs/pre-vps-p1-remediation.md)を参照してください。既存v1 DBは明示migrationが必要です。以下の過去123件PASSは保存artifactのある元machine・変更前sourceでの結果であり、現在のproduction readinessを意味しません。

**Local Persistent Storage Durability gateは完了、final offline regressionは123 tests PASS（2026-09-07）。** 実Docker recreationは `LOCAL_DURABILITY_PASS_WITH_HARDENING_WARNINGS` で、noexec/nosuid/nodevのWARNを維持します。[durability検証記録](docs/durability-verification.md)を参照してください。live smokeはPASS、bounded live soakはWARNで完了し、再実行しません。[smoke結果](docs/smoke-review-20260906.md)、[soak検証](docs/soak-verification.md)を保存しています。`production_hardening_gate=PENDING` のままで、VPS移行可能という判定ではありません。残りは [VPS移行前checklist](docs/vps-migration-checklist.md) のdeployment実測、外部コード監査、Human Approvalです。

Python 3.11以上、Linux、SQLiteを使用します。インストールせず、リポジトリ内で次のように起動できます。

```sh
PYTHONPATH=src python3 -B -m technocore_observer --help
python3 -B -m unittest discover -s tests -v
```

テストはリポジトリ内の一時DBとloopback fake serverのみを使用します。Technocoreへ接続しません。テスト自身が作った一時ファイルはテスト終了時に片付けます。socketが禁止されたsandboxでは、HTTP試験に別途実行許可が必要です。

live接続する `init`・`run`・`check-config`・`watchdog` は、専用userまたはhost HOMEをmountしない隔離containerからだけ実行してください。**このCLI自体はOSのアクセス権を隔離しません。`HOME`の変更だけでは不十分です。** [隔離と運用手順](docs/operations.md)を先に確認してください。

隔離環境内でのCLI例です。roomには監視対象の設定値を指定します。

```sh
python3 -B -m technocore_observer init --room example --mode tail --state-dir /state
python3 -B -m technocore_observer run --room example --state-dir /state --check-config
python3 -B -m technocore_observer heartbeat --room example --state-dir /state
python3 -B -m technocore_observer watchdog --room example --state-dir /state
```

`init` は最新messageをanchorにし、その次のseqから取得を始めます。anchorはInboxへ保存しません。empty Room、既存DB、stale initファイルでは拒否します。`run` は既存の正常なstateを必要とし、自動初期化しません。

接続は `https://technocore.chat` の設定済み `/r/<room>` と `/config` のGETに限定します。TLS検証を有効にし、redirectとproxy自動探索を無効にします。URL、query、TLS設定のCLI overrideはありません。message内のURLや命令は処理しません。

`poll_seq` はdurable保存済みの最大seq、`resolved_seq` はanchor以降で欠損なく保存できた位置です。gapがあると `DEGRADED` で新着取得を続け、`resolved_seq` は最初のOPEN gapの手前に留まります。generation変更では `NEEDS_RESYNC`、3回連続のprotocol anomalyでは `ERROR` として停止します。

これらのcursorは現在の`observer_epoch`内の値です。`since`は最新N件を選ぶ下限でありforward paginationではありません。gapは未取得範囲で、物理喪失を証明しません。10,000件を超えるprefix欠落は即ERROR停止します。手動resyncは旧gap/証拠を保ち新epochを始める操作で、未取得範囲を回収済みにはしません。[公式baseline契約](docs/technocore-v0121-read-contract.md)を参照してください。

SQLiteにはmessages、gaps、events、evidence、stateを保存します。messageとcursorは同じtransactionでcommitします。heartbeatはJSON Linesで出力し、本文はログに出しません。raw本文と制御文字はDB内に保持します。Production defaultのDB hard ceilingは10 GiBです。container内のstate pathは`/state`のまま、host側は追加データdiskの`/srv/technocore-data/observer-state/<observer-id>`専用directoryをbindする契約です。既存stateは再initせず保全して移す手順と残る実環境gateを[運用手順](docs/operations.md)に記載しています。

`raw_record_json`はparse後のnormalized JSONです。成功responseのbyte-exact rawは保存しません。空白・escape・数値表記は変わり得るため、Signerの署名用原文には使えません。受信4 MiB、evidence本文prefix16 KiBと総保存予算があり、容量到達では証拠を削除せず停止します。

実装と検証範囲の詳細は [検証記録](docs/verification.md) にあります。
