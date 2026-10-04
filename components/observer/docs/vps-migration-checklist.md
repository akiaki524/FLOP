# VPS移行前checklist

2026-09-07 / Observer Specification v0.3 / Technocore baseline 0.12.1。

最新: ユーザー提示のOpus focused re-reviewで旧P1 5件はCLOSED（P0/P1新規0）。今回のNEW-1/NEW-2・NEW-4/NEW-5の差分とoffline結果は[final remediation artifact](pre-vps-final-remediation.json)を参照。今回差分の独立reviewは別途必要です。

追記: [P1修正・再現結果](pre-vps-p1-remediation.md)と[公式baseline read契約](technocore-v0121-read-contract.md)を記録。newest-N semanticsを確定し、forward jump停止・manual epoch resync・容量/JSON予算を実装した。変更後offline regressionは148 tests PASS / skip 0（このmachineの保存artifactあり）。[最終artifact](pre-vps-p1-final.json)を参照。P1のcode/offline対応は完了したが、production gateと独立再reviewは未完了。VPS deploymentは実施していない。

**Local Persistent Storage Durability: COMPLETE。production_hardening_gate: PENDING。VPS migration gate: PENDING。** このchecklistは次の設計・監査用であり、VPSへの実行指示や新しい通信・設定変更の承認ではない。

## 完了したLocal検証

| 項目 | 判定・証跡 |
| --- | --- |
| Separate repository / stdlib / Observerのみ | 完了。upstream copy、Analyzer/Signer/write capabilityなし |
| Explicit init / atomic SQLite / state fail-closed | PASS。init/runtime crash、missing/corrupt state、application_id/user_version、single-writerをoffline検証 |
| Sequence / generation / gap / content / HTTP | PASS。strict ordering、content tolerant storage、OPEN gap accounting、世代変更停止、有限anomaly retry、429/5xx/backoff、log safetyのregression |
| Heartbeat / 独立watchdog | 実装・offline PASS。保存済みlive soakのwatchdog 11回OK |
| Secret isolation / live smoke | 承認済みcontainerの隔離・6 GET smoke PASS。実keyの読み取りなし |
| Bounded live soak | 実施完了、元判定WARNを維持。300試行、保存4 messages・gap 0・64 MiB tmpfs。通信失敗2回・503が1回から回復。長期容量/catch-upの証明ではない。再実行しない |
| Local persistent storage durability | COMPLETE。host process31ケース＋実Docker named-volume A削除/B再作成。state/messages/cursor/OPEN gap保持、since=201から継続 |
| Local mount hardening | noexec/nosuid/nodevはWARNを明示。その他のcore isolationはすべてPASS。Local profileの例外をproductionに転用しない |
| 修正前offline regression（歴史的結果） | 123 tests PASS、skip 0は保存artifactのある元machine・当時sourceの結果。[旧機械可読結果](final-regression-20260907.json)。変更後は上記148件のartifactを参照 |

[総合verification](verification.md)、[durability review](durability-verification.md)、[soak結果](soak-verification.md)を証跡とする。Local完了はDocker内の全crash matrix、Windows/WSL/VM再起動、電源断、storage deviceのfsync保証を意味しない。

## Production security gate — 未完了

- [ ] 実行予定のdeployment image・base digest・source hash・argv・runtime UID/GIDを固定してreviewする。Local fixture imageのPASSをproduction imageに転用しない。現在のsourceは未commitのため、監査対象は記録済みhashで特定する。
- [ ] production entrypointの`-I`を決着させ、実deployment imageでconfiguration/process probeを実施する。現在のContainerfileには`-I`がなく、fixture側の`-I`のPASSで代替しない（P2-1）。
- [x] Current Sourceのstate配置契約は、追加データdisk上の `/srv/technocore-data/observer-state/<observer-id>` だけをcontainer内 `/state` へbindする方針に更新した。既存DBを保持して移す。host directoryの準備・実bind・移行は未実施でHuman Gate。
- [ ] ownership、directory 0700 / DB 0600、sidecar/lock permissions、起動時所有権、mount optionsを実環境で確認する。noexec/nosuid/nodevを再評価・実測し、結果と判断根拠を記録する。LocalのWARN許容を自動適用しない。
- [ ] nonroot、non-privileged、read-only rootfs、cap-drop ALL、no-new-privileges、seccomp、no host network/namespace/devices、許可したstate以外のpersistent writable pathなしをinspectとprocess probeで確認する。
- [ ] host HOME、`/home`、DID/Signer/seed/private key領域、`~/.local/share/flop`、SSH/wallet keys、Docker socket、host rootをmountせず、secretをimage/env/stdin等に渡さない。実keyを読み取らずmount/namespace/権限を検証する。
- [ ] GET先を `https://technocore.chat/r/<configured-room>` と `/config` に限定する。TLS検証、redirect禁止、proxy自動探索禁止を維持する。deploymentのDNS/egress方針をreviewし、host networkや新しい外部通信を自動追加しない。アプリのpath制限とnetwork firewallの保証を混同しない。

上記が未完了の間は `production_hardening_gate=PENDING` を維持する。host/Docker設定変更、新規bind、system package、権限拡張、repo外write等は別途Human approvalの対象。

## Production storage / operations gate — 未完了

- [x] **NEW-3のCurrent Source上のsizingは解決（Issue #24）**: 16 MiBはlocal safety budgetだったが、Production defaultをDB 10 GiB、WAL 128 MiB、evidence 128 MiB/65,536行、events 262,144行、disk空き下限10 GiBへ更新した。DB 10 GiBはSQLiteのhard ceilingで予約容量ではない。`PRAGMA max_page_count`の変更はschema migrationを必要としない。旧16 MiB到達のnode-01観測はユーザー提示のRuntime Evidenceであり、このrepoから再測定していない。実disk容量・backup余力・rollout可否の確認は残る。
- [ ] NEW-1の80% warning、heartbeat utilization、exit 1 / machine-readable stderrをproduction supervisorへ接続し、容量停止後の[backup→plan→Human approval→限定retention→integrity→restart](operations.md)を実volumeで検証する。backup容量と整理後も不足する場合の停止・容量判断を含める。自動retention/resyncを追加しない。

- [ ] 選定したproduction filesystemとdeployment imageで、同一stateからのprocess/container restart、COMMIT前後停止、OPEN gap/resolved_seq/status保持、保存poll_seqからの再開を実測する。production試験の停止点と合格範囲を実行前に定める。
- [ ] SQLite application_id/user_version/quick_check、WAL/FULL/foreign_keys、flock、permission、容量不足時のfail-closedとsupervisorの異常検知を確認する。Missing WALだけではcorruption扱いにしない。
- [ ] 定期backupとrestore手順を設計・検証する。SQLite backup APIまたはVACUUM INTOを使い、稼働DBの単純cpは禁止。backup/restore先の所有権・permissions・整合性・cursor/gap保持・再開を確認する。Local helperのbackup成功だけでproduction backup運用完了としない。
- [ ] 更新したProduction budgetに対して実filesystemのDB/WAL/backup/log容量と空き下限10 GiB、quota/reserve、保存停止後の保全手順を確認する。10 GiB DB ceilingはdisk予約ではない。4 MiB/JSON予算の256 MiB RLIMIT_AS試験と、実containerのcgroup/page cache含む検証を区別し、後者を実測する。
- [ ] supervisorと独立watchdogの周期・stall閾値・通知先・停止動作を定める。HTTP成功とvalid response、quiet Roomと停止を分けて監視する。ERROR/NEEDS_RESYNCを自動解除しない。通知先への送信やhost設定は今回行わない。
- [ ] gapは通常readで未取得・物理喪失UNKNOWNとして保存する。manual resync/migrationのplanをHumanが確認し、旧証拠を保持してepochを増やす[運用手順](operations.md)をproduction環境で検証する。自動export/recovery/resyncは行わない。
- [ ] watchdog/heartbeatの同一UID・WAL sidecar write permission依存を確認する（P2-2）。full scanの所要時間と監視周期を測る（P2-5）。`--state-dir`を絶対pathで明示する（P2-7）。
- [ ] `raw_record_json`はnormalized JSONで成功wire bytesを保存しないことをAnalyzer/Signer設計へ反映する（P2-3）。host HOME probeのmachine固定をVPSへ転用しない（P3-2）。
- [ ] VPS開始時の明示init/既存state resume、room、version drift確認、少数read-only検証のrequest予算・停止条件をreviewする。baseline 0.12.1を自動更新せず、driftは記録する。今回のsoakを再実行しない。

## 独立監査・Human Approval — 未完了

- [x] 修正前のOpus独立auditと各P1の再現照合。
- [x] P1修正後sourceのOpus focused re-review（ユーザー提示）：旧P1 5件CLOSED。新規NEW-1〜NEW-5を分類。
- [ ] 今回final remediation差分の独立review。対象source hash、manual retention/backupとapproval/epoch境界、Local/VPS policy差分、regression結果、残る観測限界を含める。外部サービスへの送信・公開は未実施で、別途承認対象。
- [ ] 監査指摘を整理し、必要な修正と該当regressionを完了する。変更後のimage/source hashと検証結果を更新する。
- [ ] Phase 8: production構成・mount hardening・backup/recovery・監視・監査結果を揃えてHuman Approvalを受ける。Local mount例外の承認をproduction承認とは扱わない。
- [ ] Phase 9: 上記gate完了と承認後にVPSへ移行する。

既存limit=201 smokeは200件応答と整合するが、保持件数が200件超だったことを独立確認していない。sinceの選択順はユーザー提示の公式v0.12.1 source確認で解決した。live deployment conformance、quiet/reapedの完全な区別は別の観測限界として残す。追加live通信・投稿・load試験は行っていない。

## Public sanitization P2 closure（2026-09-09）

`python3 -B -m unittest discover -s tests` は、local-only の ignored/untracked smoke evidence がある開発機では **165 PASS / 0 SKIP**、fresh clone / VPS では **163 PASS / 2 SKIP（計165件）** が正常。
2件は `test_live_compatibility.SavedResponseTests.test_saved_config_values` と
`test_live_compatibility.SavedResponseTests.test_saved_200_records_round_trip_through_actual_observer`。
SKIP は local-only smoke evidence の不在によるもので、baseline corruption を意味しない。
SKIP を消すために private/local smoke evidence をコミットしてはならない。
production acceptance では再現可能な offline tests とローカルの過去の compatibility evidence を区別し、VPS の実環境検証は別途行う。

Human が後日 root commit を amend して `pre-vps-v0.3` を移動した場合、その tag は
original Pre-VPS audited baseline (`d86b679`) + Sonnet public-sanitization implementation +
Opus independent sanitization delta review（ユーザー報告: GO WITH CONDITIONS）+
Codex P2 closure + Opus independent P2 closure review を表す。元の Opus audit は後続コードの監査を意味しない。

最新review status（ユーザー提示の後続Opus independent P2 closure review）:
**GO WITH CONDITIONS / P0=0 / P1=0 / P2-A CLOSED / P2-B CLOSED**。
baseline finalization（root commit amend・`pre-vps-v0.3` retag）とPrivate GitHub pushはGOで、
これらの前の追加Opus reviewは不要。これはreview判定の記録であり、各操作は今回未実施。
`public-sanitization-p2-closure-20260909.json` の `post_review` に記録し、
既存の `closure_audited: false` はhistorical pre-review stateとして保持する。
過去の実行artifact・hash・verdictは書き換えない。VPS gateは引き続きPENDING。

### VPS gateへの持ち越し — 未完了

- [ ] **P2-N1**: host account HOME pathとcontainer image内pathが同じ場合（特に `/root` 等）、`host_home_not_visible` probeがfalse FAILになる可能性を確認する。VPS上でproduction isolation evidenceとして採用する前に評価する。
- [ ] **P2-N2**: `run_container_smoke` / durability plan artifactには `OBSERVER_HOST_HOME` / `OBSERVER_HOST_CANARY` の実absolute pathが含まれ得る。plan outputをtracked docs artifactとして保存する場合は、値をredactしてからcommitする。
