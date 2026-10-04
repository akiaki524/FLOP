# Technocore v0.12.1 read contract

根拠: ユーザーが公式sourceを確認し、この作業へ提示した内容。
Tag `v0.12.1`、commit `2670ebb11ead1ab9b67307e518f8e8479fc9db47`。
このagentによるupstream取得・独立source再検証・live deployment確認は行っていない。以下を固定baselineの実装契約として採用し、P1-2のsemantics不確定を解消した。

`store.read_messages()`の契約は **seq > sinceを満たす最新limit件を選び、選ばれたものをoldest-firstで返す**。公式stateful testsもnewest-firstでscan、limit件取得、reverseという独立モデルで同じ振る舞いを検証している。`since/limit`はforward paginationではない。

| 定数・動作 | v0.12.1の意味 |
| --- | --- |
| READ_BUDGET | 1 MiB。通常`/r`は有界なtail read |
| MAX_LIMIT / DEFAULT_LIMIT | 200 / 50。Observerのpoll limitは200 |
| MAX_TEXT_CHARS | 4096 **characters**。bytesではない |
| per-room ring | 約10 MiB。古いhistoryはbyte単位のcompactionで落ちる |
| 通常roomのreap | 7日間inactiveのroomを削除。各messageの7日TTLではない |
| first-message-only room | 24時間でreapされ得る |
| global room-byte pressure | 次回appendでroomの保証floor=1 MiBまでcompactされ得る |
| export | `/r/<room>/export`は別のretained-ring raw JSONL snapshot path。Observerのallowlistには追加しない |

disk上にretainedであることと、通常`/r`から現在取得できることは異なる。若いmessageも保持・read可能とは限らない。`first_seq > since+1`から物理喪失を証明できず、sinceを小さくした再pollでも最新N件より前へ遡れる保証はない。

Observerのgapは **UNOBSERVED_NORMAL_READ / physical_loss=UNKNOWN** とする。小さなgapの後もDEGRADEDでtailの監視を続けるが、resolved_seqは現在epoch内の最初のgap手前で止める。raw export、自動backfill、自動loss解消は行わない。手動resyncは別epochへの明示的な開始であって、欠落の回収ではない。

保存済みlive responseは200件・79,888 bytesだった。この観測は上記契約と整合するが、live conformanceの独立証明ではない。追加GETは不要として0回。将来行う少数GETはdeployment conformanceの証拠として区別する。

## Observerのbody/JSON受入れ予算

HTTP capは32 MiBから **4 MiB** に縮小。1 MiBのtailデータがJSONで非BMP文字をescapeするとUTF-8から最大約3倍になる場合を考慮し、残り約1 MiBをrecord境界・metadata・envelope用の余白にした。4096非BMP文字だけでUTF-8=16 KiB、JSON escape=48 KiBになり得るため、4096 bytesとして計算しない。

200×4096文字の全組合せが1 MiB tailに同居するわけではない。ASCII最大textの200件と、1 MiB程度のUTF-8 tailに収まる63件の最大非BMP text（JSONは約3 MiB）の両方を試験する。未知field全体の公式最大byte数や全serializer詳細までは提示されていないため、4 MiBを「あらゆる正当responseの数学的最大値」とは主張しない。超過はboundedなprotocol anomalyとして保存し、cursorを進めない。

さらにdecode前にdepth=32、string開始・構造separator等の合計=32,768というJSON予算を適用する。小さい数値やdictが大量にあるbodyによるPython object増加を制限する。未知contentはこの予算内でのみ型変化を許容して保存する。

256 MiB RLIMIT_ASの子processでparseと実Observer/SQLite保存を確認する。RSSにはPython、受信body、Unicode string、object、SQLiteを含める。これはLinux processの実測であり、256 MiB cgroup/container内の余裕・page cache・production imageを検証したものではない。後者はVPS前のdeployment gateに残す。
