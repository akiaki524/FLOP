# Evidence Adapters

Analyzerは下記のObserver保存形式だけを読む。独自の合成Bundleは入力形式ではない。

## `full-capture-archive`（推奨・Default）

`technocore_full_capture` の Archive `format=1`（`components/observer/src/technocore_full_capture/archive.py`, `spool_archive.py`）。

| 項目 | 内容 |
|---|---|
| 配置 | spool-archive directory（`segment-<20桁>/capture.json` + `shard-<12桁>.jsonl`、任意で `manifest.sqlite`）または単一Archive directory |
| 読む範囲 | 各segmentの `capture.json` が示す `shards` 番までの公開済みshard。checkpoint前の後続shardは `SHARD_PENDING_CHECKPOINT` として次回に回す。`.pending` は読まない |
| 検証 | Observerの `Archive.inspect` / `_verify` / `_open` の**一部を再現**：capture.jsonのschema・型検証（key集合、format、room、status、整数、`message_count == cursor - anchor_seq`）、shardのheader鍵集合、`payload_sha256`、`payload_bytes`、`count`、shard内seq連続、`last_sha256`、`previous_sha256` chain、shard間の `first_seq` 連続（anchor+1から）、`generation` / `anchor_seq` の一定性、checkpointの `tip_sha256` / `cursor` / `message_count` / `payload_bytes` / `archive_bytes`（読んだshardのbyte数合計）一致 |
| 再現しないもの | ディレクトリ内の未知entry・shard集合の欠番/余剰検査（`CAPTURE_FOREIGN_DIRECTORY_ENTRY` / `ARCHIVE_SET_MISMATCH`）、checkpoint未反映の後続shardの検証（次回runで読む）、checkpointの `updated_at` 等の時刻fieldの意味検証 |
| 保証の範囲 | 上記は**鍵なしhashによる自己整合**の確認のみ。Archive全体を書き換えhashを再計算できる者への改ざん検出、HTTP受信rawとの一致、全履歴の完全性は保証しない |
| 失敗時 | 当該shardを `QUARANTINED`（`EVIDENCE_CONFLICT` Finding）とし、他のshard / Sourceの解析は続ける。`capture.json` の `shards` は実在するshard file（1..kが連続、k∈{shards, shards+1}）と照合し、不一致は `ARCHIVE_SET_MISMATCH` でそのstreamを隔離（宣言件数だけ走査しない）。record `seq`・header `format` は `True`/`1.0` を受け付けず実際の整数のみ |
| 増分 | shard単位のSHA-256で既読判定。最大seqを進捗にしない。既読shardの内容変化は `unit_changes` として保持（immutableなのは公開済みshardだけ。稼働中に更新される `manifest.sqlite` とObserver state snapshotの更新は改ざん扱いしない）。`max_new_records_per_run` は実際にNEWとなるrecord数で数え、未処理分を持つunitは「既読」にせず次runで続きを取り込む |
| Record hash | `sha256(line bytes)` = Archive行（canonical JSON + LF）。HTTP受信rawではない（`NO_HTTP_RAW_IN_ARCHIVE`） |
| Locator | `<source id>:<segment>/shard-N.jsonl#L<line>`。host絶対pathを含めない。外部媒体へ移管しても `path_map` で同じlocatorを辿れる |
| epoch | `manifest.sqlite` を読んだ場合のみ `segments.epoch` から付与。読まない場合は segment 名がstream識別子で、segment間の不連続は `STREAM_DISCONTINUITY`（GAPか世代境界か不明）として出す |

### `manifest.sqlite`（opt-in: `read_manifest: true`）

GAP / BOOTSTRAP_UNOBSERVED_PREFIX / CONFLICT / LATE_OBSERVATION / GENERATION_BOUNDARY / BOUNDARY_OBSERVATION receiptを読む。`mode=ro`、`query_only`、timeout 0.25秒、1回の短い読取transaction。Observer実装にも manifest を `mode=ro` で開く読取経路がある（`multi_room.py`）。

読むたびにObserverの `ArchiveWorker._record` / `verify` 契約で全receiptを検証する：entry_idが1から連続、`chain_i = sha256(chain_{i-1} + document_i)`（初期値64個の0）を**再計算**して保存値と一致、`document.entry_id` 一致、`PRAGMA quick_check`、`state` の `through_entry` / `receipt_hash` / `messages` / `gaps` / `unconfirmed` が再計算値と一致。さらにMESSAGE receiptの `record_sha256` と、**検証に通った全shard行**（既読で `SKIPPED_UNCHANGED` のshardも、行ごとの `(stream, seq) -> sha256` を都度計算。Storeへ再insertはしない）と照合する。照合はObserver `verify()` と同じく**manifest→Archiveの方向**：各MESSAGE receiptに、manifestの `segments` 行（Observerは同一transactionで記録する）が存在し、`seq` がその `first_seq`〜`last_seq` に入り（欠落は `MANIFEST_SEGMENT_MISSING`、範囲外は `MANIFEST_RECORD_MISMATCH`）、かつ同じ `(segment, seq)` と同じhashのverified Archive行が必要（無ければ `MANIFEST_RECORD_MISMATCH`、Archiveに存在しないsegmentなら `MANIFEST_SEGMENT_MISSING`）。逆方向（receiptの無いArchive行）は、Archiveがmanifestより1 shard先行できるためinvalidにしない。全shardを検証できなかったsegmentのreceiptは判定を保留する。segment tipとcheckpointも照合する。manifest `state.room` が設定sourceのroomと一致しない場合（別roomのmanifest）は `MANIFEST_ROOM_MISMATCH` で隔離する。`segments` 行の `shards` / `first_seq` / `last_seq` が整数でない、または `tip_sha256` が文字列でない場合は `MANIFEST_SEGMENT_INVALID`、GAP等の `seq` / `end_seq` が整数でない場合、またはreceipt documentにNaN / Infinity / `1e999` 等の有限でない数値がある場合は `MANIFEST_RECEIPT_INVALID` で隔離する（Archive行・snapshot rowのJSONも同じ厳格parseで、有限でない数値は不正Evidence）（SQLiteの列affinityは型を強制しないため、算術・比較の前に検証する）。どれかが崩れたmanifestは `QUARANTINED`（`EVIDENCE_CONFLICT`）とし、そのGAP/CONFLICT/LATE等は取り込まず、以前取り込んだmanifest由来のCoverageと `source_progress` を破棄し、そのsourceのcached recordのepochもNULLに戻す（record本体・hash・refは消さない。epochは `rebuild` 後の再取込で正常なmanifestから再付与される）。

- header上WAL modeのmanifestや `-journal` / `-wal` がある状態では開かない（読取専用openでもSource側に `-shm` 等を作り得るため）。

- manifestはArchive Workerが更新する稼働DB（rollback journal）なので、読取transaction中はWriterのcommitを短時間待たせ得る。**移管済み・停止中のArchiveではtrue、稼働中VPSでの有効化は運用承認後**とする。
- LATE_OBSERVATIONはGAP範囲内の後着seqとして扱い、GAP記録は消さずに `RECOVERED_BY_LATE_OBSERVATION` / `PARTIALLY_RECOVERED` を付ける。

## `observer-state-sqlite`（snapshotのみ）

`technocore_observer` の `state.sqlite`（schema v2, application_id `0x54434F42`）の `messages` / `gaps`。

- **snapshot契約は明示宣言**：source設定に `snapshot: true`（どのprocessも書き込まないcopy、例：SQLite Online Backup APIの出力）を書いた場合だけ読む。未宣言は `DEFERRED: SNAPSHOT_CONTRACT_NOT_DECLARED`。`-wal` が無いことはsnapshotの証明にならない（Observer writerは稼働時に `journal_mode=WAL` へ切替、initはDELETE：`storage.py`）。
- 宣言済みsnapshotでも `-wal` / `-shm` / `-journal` があれば `DEFERRED`。header（magic、application_id `0x54434F42`、user_version 2）、`PRAGMA quick_check`、必須table、`state` singletonの存在と `state.room == 設定sourceのroom` を確認し（`messages` / `gaps` に `state.room` 以外のroomの行があれば `SNAPSHOT_ROWS_FOR_OTHER_ROOM`。別roomのsnapshotは `DEFERRED: SNAPSHOT_ROOM_MISMATCH`、singleton無しは `SNAPSHOT_STATE_MISSING`。「正常READで0件」にしない）、`immutable=1` で開く（sidecarを作らない）。読取前後で inode / size / mtime / ctime が変われば結果を捨てて `SNAPSHOT_CHANGED_DURING_READ`。
- 稼働DBの読取は `allow_live_sqlite: true` の明示opt-inのみ（Human Gate）。WAL DBの読取専用openはSource側の `-shm` を使う／作る可能性があり、Analyzerはそれを防げない。
- `raw_record_json` はObserverが再serializeしたJSON。Record hashはその文字列のSHA-256。
- Analyzerは停止中DBのcopy作成やbackup実行を自動では行わない。

## 共通

- 署名は `room|nonce|text`（Ed25519 `did:key`）を保存値のまま検証。`seq` / `ts` は署名対象外。Unicode正規化・丸めをしない。検索用正規化は別の派生値。
- 同一 `source|stream|seq` で内容が異なる場合は上書きせず `record_conflicts` に両方を残す。
- 異なるSource・streamの同一性が不明なものは重複排除しない。同一room・generation・seq・同一内容のみ同じ保存recordとして集計上まとめる（参照は全て保持）。
- 媒体未接続（path不在）は `UNAVAILABLE` であり、削除・消失・Fraudとは扱わない。

**Observer state snapshot の不正row**：`messages.raw_record_json` がJSON objectでない・不正JSON・NaN等、または `seq` / `observer_epoch` / `server_generation` / `ingested_at`、`gaps` の `start_seq` / `end_seq` / `detected_at`（有限の数値）/ `status`（文字列）の型が不正なrow（BLOB・Infinity・数値でないtext等）は、そのrowだけをEvidenceとして取り込まず（補正・推測しない）、Coverage `ROW_MALFORMED`（table別件数）を残す。`raw_record_json` の `seq` は、同じrowの `seq` 列と等しい実際の整数でなければならない（欠落・不一致・非整数は不正row）。同じsnapshotの正常rowは通常どおり解析し、sourceは失敗させない。

**Checkpoint検証失敗**：全shardが個別に通っても `capture.json` のtip・cursor・件数・byte合計が合わない場合、そのstreamのshardは `ARCHIVE_CHECKPOINT_MISMATCH` で隔離し、そのrecordは解析にもcacheにも入れない（checkpointが直れば次のrunで読む）。すでに以前のrunでcache済みのrecordは消さない。

**source IDの再指定**：同じsource IDが別のroom / kindを指すようになった場合、旧identityで保存したcache行（records/units/coverage/progress/statusと、そのsourceに由来するunit_changes・そのrecord keyのrecord_conflicts）は破棄して読み直す（他sourceの行、Finding/Review/報告/解決の履歴は消さない）。record_conflictsはkey（`<source_id>|<stream>|<seq>`）の所有者部分で照合するので、`rebuild` でrecordsが消えた後でも対象を特定できる。`rebuild` は `record_conflicts` / `unit_changes` と、identity判定に使う `source_status`（room/kind。runごとに上書き）を保持するため、rebuild後にIDを別room/kindへ向けても旧identityの整合性factは撤回される。同じidentityのrebuildでは保全factは再読込後に再び表示される（`identity_reset` を状態に記録）。path変更だけは検知しない（移管媒体の `path_map` 変更は正当）。
