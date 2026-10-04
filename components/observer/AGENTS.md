# AGENTS.md

## Objective

このRepositoryの既存 `technocore_observer` はTechnocoreのRead-only Observerです。
public messageとgap / cursor / evidenceをSQLiteへ保存しますが、Analyzer、Signer、External Write、Observer stateの自動resync / recoveryは担当しません。
別のopt-in `technocore_full_capture` subsystemにはCapture continuity gapのbounded recovery codeが含まれます。これは既存Observer stateの自動resyncではなく、Live / Productionでの起動・導入はHuman Gateです。どちらのsubsystemもSigner / Wallet / External Writeを扱いません。

## Start Here

- [SPEC.md](SPEC.md): ObserverのCurrent要求仕様。対象Room、並行観測、保存・移管、復旧、通知、成功条件を定義する。実装・RuntimeのObserved truthとは区別する。
- [README.md](README.md): Observer契約、Setup、Offline Test、Live境界。
- [docs/operations.md](docs/operations.md): state、backup、resync、運用手順。
- [docs/vps-migration-checklist.md](docs/vps-migration-checklist.md): Production / VPS gate。
- Live / Production Taskではcontrolling Issueと最新Runtime Evidenceも確認する。

## Autonomous Scope

低Risk・ReversibleなRepository調査、実装、Refactor、Offline Test、Debug、Docs、local commitは自律的に進めてよい。
通常のtask用work branch pushはroot `AGENTS.md` と適用中のGlobal AGENTSに従う。
通常検証のためにTechnocore Live、Docker、sudo、systemd、Production stateへ触れない。

## Non-negotiable Boundaries

- TechnocoreのGETを一般にread-onlyと仮定しない。通信は既存の固定host / read path境界を勝手に広げない。
- SQLite state、messages、gap、cursor、evidenceを削除・初期化・書換えしてFailureを隠さない。壊れたstateを空stateへ自動再作成しない。
- `ERROR` / `NEEDS_RESYNC` を自動解除せず、manual resync / retention / recoveryを勝手に追加・実行しない。
- historical soak、one-shot、consumed / superseded operationを「再確認」のために再実行しない。必要ならcontrolling evidenceを確認してHumanへ戻す。
- Secret、Signer / Wallet、host HOME、Docker socket等のsensitive surfaceをObserverへ渡さない。
- Live CLI、実state migration（plan / applyを含む）・resync / retention / restore、Production mutation、sudo / root、Docker / mount / network / permission拡張はHuman Gate。

## Verification / Done

Canonical offline verification:

```bash
python3 -B -m unittest discover -s tests -v
PYTHONPATH=src python3 -B -m technocore_observer --help
```

Loopback socketがsandboxで拒否された場合はfull PASSと主張せず、未検証範囲を明記する。
変更に応じたregressionと `git diff --check` を実施し、Test結果、限界、commit IDを報告する。
Local-only smoke / soak / durability evidenceやSecretをcommitしない。
