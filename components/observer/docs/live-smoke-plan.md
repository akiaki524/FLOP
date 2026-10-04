# Technocore Observer — bounded live smoke plan

Target: **Technocore 0.12.1**, Observer Specification v0.3 §70。
期待versionのみ0.12.1に追従し、既存のarchitecture、sequence/generation/gap、P0、5xx/503、backoffは変更しません。

## 現在の確認状況と承認範囲

ユーザーが通常Ubuntu terminalで実行したbase-image隔離試験は全項目PASSでした。
containerは `observer-isolation-d07a7daff61b`、image IDは以下です。

```text
sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
```

これはユーザー提示の実測結果です。その後、Observerコードを含むoffline/live smokeも通常Ubuntu terminalで完了しました。保存証跡の確認結果と2点の修正は [smoke review](smoke-review-20260906.md) に記録しています。以下は実行した計画です。

追加Autonomy policyにより、同じsecret/privilege境界内の実装、Docker build、offline試験、container作成/起動/停止/削除、仕様で許可された少数のTechnocore GETは個別承認不要です。soakはsmoke review後の次段階であり、まだ開始していません。sudo、system package install、WSL内Engine導入、host network/namespace、privileged、secretやDocker socketのmountは行いません。

このCodex sessionはuser namespaceの差によりDocker socketへ接続できません。権限変更や再承認で回避せず、Docker Desktopへ接続できる通常Ubuntu terminalで、準備済みhelperを実行します。

## 実行command

リポジトリのルートで、計画表示は次です。Docker接続・外部通信・container作成はありません。

```sh
python3 -B tests/run_container_smoke.py
```

offline preflightとlive smokeを順に実行するcommandです。

```sh
python3 -B tests/run_container_smoke.py --execute
```

helperは既存の固定image IDだけを使い、image pull/buildやregistryのmetadata取得を呼び出しません。Observerと診断コードのPythonファイルだけをzip化し、非rootの `docker exec --interactive` のstdinから `/state/observer.zip` へ渡します。host filesystemのbind mountはありません。

これは固定base image上で実際のObserver sourceを検証する方式です。専用の派生imageは今回buildしません。`Containerfile` のbase digestも同じ値に固定しましたが、将来buildするdeployment imageには改めて隔離検証が必要です。

helperが実行する主要commandの形は次です。`<name>` は実行ごとに生成するcontainer名です。全Docker呼び出しで、repo内に新規作成した空のclient設定directoryとローカルUnix socketを明示します。既存のDocker認証設定・remote context・DOCKER_*環境変数は引き継ぎません。

```text
docker --config=<repo内の専用directory> --host=unix:///var/run/docker.sock image inspect --format {{.Id}} <固定image ID>

docker ... create --name <name> --pull=never --network=none
  --user=65532:65532 --read-only --cap-drop=ALL
  --security-opt=no-new-privileges:true --pids-limit=32 --memory=256m
  --restart=no --init
  --tmpfs=/state:rw,noexec,nosuid,nodev,size=64m,mode=0700,uid=65532,gid=65532
  --env=HOME=/nonexistent --env=PYTHONDONTWRITEBYTECODE=1
  --entrypoint=python3 <固定image ID> -I -B -c <有界の待機コード>

docker ... container inspect <name>
docker ... start <name>
docker ... exec --interactive --user=65532:65532 <name> python3 -I -B -c <source受信コード>
docker ... exec --user=65532:65532 <name> python3 -I -B -c <診断起動コード> offline
docker ... exec --user=65532:65532 <name> python3 -I -B -c <結果転送コード>
docker ... stop --time=5 <name>
docker ... rm <name>
```

完全なargvとPythonコードは計画表示commandで確認できます。offlineがPASSした場合だけ、同じsource/image IDを使った別containerを `--network=bridge` で作成し、同じ検査後に `live` 診断を実行します。host networkではなく、port公開もありません。実行中のcontainerから結果を回収してから、その実行で作ったcontainerだけを停止・削除します。

## 接続範囲とrequest上限

外部HTTP originは **`https://technocore.chat` のTCP 443だけ**です。TLS証明書を検証し、proxy自動探索とredirectを無効化します。response内のURLを使いません。DNSはDockerの設定済みresolver経由で `technocore.chat` を名前解決します。名前解決に伴うDNS通信とDocker Desktopの経路は利用しますが、registry、GitHub、他のHTTP originへの取得は実装していません。

bridge自体はdomain単位のegress firewallではありません。HTTP origin/path/queryは診断コードのallowlistで制限します。

以下の**最大6 GET**だけを順番に実行し、各requestの完了後から次まで最低2秒空けます。`n` は `time.time_ns()` による数値です。

| # | Path | Query・目的 |
| --- | --- | --- |
| 1 | `/config` | version=0.12.1とdeployment値を確認 |
| 2 | `/r/mb-047f3d88ef38` | `format=json&limit=1&n=N`。最新anchor Aとgeneration |
| 3 | `/r/mb-047f3d88ef38` | `format=json&since=0&limit=200&n=N`。最大件数のread |
| 4 | `/r/mb-047f3d88ef38` | `format=json&since=0&limit=201&n=N`。§70のclamp診断 |
| 5 | `/r/mb-047f3d88ef38` | `format=json&since=A&limit=200&wait=10&n=N`。実際のObserverによる1 poll |
| 6 | `/r/observer-smoke-20260906-9bc137a4` | `format=json&limit=1&n=N`。§70のvalid nonexistent Room用に固定した診断Room |

通常Observer clientにlimit=201や別Roomへの自動切替は追加しません。診断専用clientだけが、上記2つの固定Roomとqueryを扱います。

新着が来てemptyにならなかった場合や、保持件数が不足してclampを十分確認できない場合は「未確定」と記録します。6回を越えてquiet状態や429を作りにいきません。wait_held=falseも意図的なwaiter枯渇では発生させません。

HTTP非200、構造異常、generation変更、通信失敗では診断を停止し、自動再試行しません。これは少数requestのsmokeの制御です。本体の5xx/503 transient handlingとbackoffは維持しています。version不一致はreportの `version_matches_baseline=false` として残し、仕様を自動変更しません。

## 結果と保存

結果directoryは `docs/smoke-results-<時刻>-<識別子>/`（0700）です。新規作成パスを実行時に表示します。

- `execution-plan.json`、`source-hashes.json`: 実行計画と実際に渡したsourceのhash。
- `offline-configuration.json`、`live-configuration.json`: container設定の合否。
- `offline/report.json`、`live/report.json`: process検査、seq/generation、cache headers、経過時間など。
- `live/response-N.body`: raw body。terminalには表示しません。
- `live/smoke-inbox.sqlite`: 1 pollのstate/inboxをSQLite backup APIで保全したもの。

raw artifactsはcaptured binary pipeで転送し、固定filename allowlistと容量上限を検査してからrepo内に0600で保存します。Gitでは結果directoryをignoreします。元の秘密鍵や認証設定は読みません。

tmpfsは一時的な試験領域であり、この試験は電源断耐久性や長期運用を実証するものではありません。異常終了時も取得済みresponse/reportの回収を試みますが、初期化前の失敗・timeout・OOMでは一部結果が得られない場合があります。containerのstop/remove失敗は表示して保持し、強制削除や無制限retryはしません。

## ローカル検証状況

smoke helper用11テストが成功しました。許可外path/query、request上限、host UIDでの実行拒否、offline失敗時のlive抑止、実ObserverのSQLite/gap、artifactのpath traversal拒否とpermissions、0.12.1の期待versionと旧versionのdriftを検証しています。実際のDocker内offline/live結果もPASSで、live request数は6でした。修正後のコードは保存responseでoffline再検証した後、通常Ubuntu terminalで同じ6 GETのlive smokeも再実行し、PASSを確認しました。最新証跡は `docs/smoke-results-20260906-125741-7d70a083/` です。soakは [計画](soak-plan.md) のみで未開始です。
