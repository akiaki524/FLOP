# FLOP

Technocore の情報を観測・保存し、根拠を確認しながら AI Agent との協働を試す個人プロジェクトです。
情報収集、保存資料の分析、候補の発見、限定した Task の処理を別々の Component に分け、
Human が確認する Preview と、署名・送信の許可境界を設けています。

非エンジニアの個人が AI 支援を使って開発しています。FLOP Labs / Technocore の公式プロジェクトではありません。
この実装の検証結果は、公式な認定・報酬・相手方の承認を示すものではありません。

開発の正本は **Private repository**、Public repository はそこから内容をレビューした配布 snapshot です。
公開準備中の配布 repository が Private でも、開発正本と配布先の役割は変わりません。
この README と Component の案内は、どちらの checkout でも使えます。

## まず Offline で試す

Python 3.12 以上と Bash を使う Linux / WSL 環境で、monorepo のルートから実行します。
追加 install、認証、LLM、外部通信・送信・署名は使いません。合成された数学 Task の処理例です。

~~~bash
cd components/collaboration/agent
demo_dir=$(mktemp -d /tmp/flop-demo.XXXXXX)
PYTHONPATH=src python3 -B -m collaboration_agent --state "$demo_dir/state" init
PYTHONPATH=src python3 -B -m collaboration_agent --state "$demo_dir/state" run examples/math.json
PYTHONPATH=src python3 -B -m collaboration_agent --state "$demo_dir/state" preview example-math
~~~

`run` の結果は `COMPLETED`、回答は `gcd=6 lcm=36` です。
`preview` で Task・根拠・回答・未承認の状態を確認できます。
これはローカル処理の完了であり、投稿や相手方の合格ではありません。
状態と監査記録は新しい一時ディレクトリに残り、既存の運用状態は上書きしません。

## Components と実装範囲

| Component | 役割と確認範囲 |
| --- | --- |
| [Observer](components/observer/README.md) | 公開 message の取得・SQLite 保存・gap 記録。追加 Capture / recovery は別の運用 Gate があり、文書だけで Production readiness を判断しません |
| [DID](components/did/README.md) | upstream Technocore chat の実装と DID / local writer 関連機能。署名付き lane と MCP を含みます。upstream サービスの稼働は本プロジェクトの運用実績ではありません |
| [Signer](components/signer/README.md) | DID の鍵を Agent から分離する policy signing 境界。Offline core と用途別 profile を実装。Real 鍵投入・送信は別の Human Gate |
| [Analyzer](components/analyzer/README.md) | 保存 Evidence の read-only 分析と Finding・報告案。Offline 検証段階で、実 Observer 保存物・Production・実 LLM 接続は未検証 |
| [Scout](components/collaboration/scout/README.md) | 固定ルールで協働候補を抽出する一回実行 CLI。合成 demo と明示的な live 読取りがあり、送信機能はありません |
| [Worker](components/collaboration/worker/README.md) | Human が選んだ資料の処理と返信 Preview。合成経路と条件付き Messages API 候補を分け、CLI Rail の Real 利用は NO-GO |
| [Agent](components/collaboration/agent/README.md) | 既知 Task を Offline で処理し Result / Preview / audit を保存。取得・Signer・Transport の運用は別の Gate で、ローカル回答を協働成功と数えません |

各 Component のコマンドはそのディレクトリを基準にしています。
詳細は各 README、担当と境界は [Component Map](docs/REPOSITORY_MAP.md) と最寄りの `AGENTS.md` を確認してください。

## Evidence と評価結果

合成 fixture、取得時点を固定した実公開資料、ローカル処理結果を区別します。
過去の PASS 件数はその revision・入力・条件での結果で、未知の Task、現在のサービス、
Production 適合や外部での受領を保証しません。Source digest は保存内容との対応を示し、
投稿者や内容の真正性を証明するものではありません。

Private 正本には評価原本を保持します。配布 snapshot では一部の生引用を除外し、
`*.public-summary.json` に集計・digest・選択した provenance を残します。
派生 summary は原文や再実行 fixture の代わりではありません。
対象と制約は [公開準備の手順](docs/PUBLIC_RELEASE_CHECKLIST.md) を参照してください。
配布 snapshot にある `docs/PUBLIC_SNAPSHOT_CONTENTS.md` は、その snapshot の収録内容を説明します。

## 開発・配布・記録の入口

- 継続開発とリリース元の選択は Private 正本で行います。Private の Issue #1 は利用できる場合の maintainer 向け routing です。
- 配布 checkout では文書と、その配布 repo に実在する Issue / PR / CI を使います。Private / legacy の番号・SHA は由来の記録であり、同番号の Public object を指しません。
- Public 側で採用した変更を Private 正本へ取り込み、次の配布で戻さないようにします。初回は clean root、以後は既存配布履歴にレビュー済みの差分を追加します。
- 変更・公開の境界は [OPERATIONS](docs/OPERATIONS.md)、snapshot 準備は [PUBLIC_RELEASE_CHECKLIST](docs/PUBLIC_RELEASE_CHECKLIST.md)、移行の由来は [MONOREPO_MIGRATION](docs/MONOREPO_MIGRATION.md) を参照してください。
- Runtime / Production の事実は別の Current Runtime Evidence、Secret / Credential は GitHub 外で管理します。Private GitHub も Secret 保管庫ではありません。

## Field Notes

These Field Notes are based on my own experiences and work logs, with AI assistance in organizing, drafting, and translating them. I review the text before publication.

[Field Notes](field-notes/README.md) に、AI とこの個人プロジェクトに取り組んだ経験を英語で記録します。
最初の記事は [Why I Started with FLOP](field-notes/01-why-i-started-with-flop.md) です。
記事は当時の経験の記録です。現在の使い方・境界・検証条件はこの repository の文書を正本とします。

## Licensing

repository 全体に一律の OSS license は付与していません。
[LICENSE](LICENSE) に従い、個別条件のない Project 作成部分は **All rights reserved** です。
`components/did/` と `components/did/mcp/` は各 Apache-2.0 LICENSE / NOTICE に従います。
第三者の著作権・必要な出典表記は保持します。公開されていることだけで自由な再利用を許諾したとは扱わないでください。
