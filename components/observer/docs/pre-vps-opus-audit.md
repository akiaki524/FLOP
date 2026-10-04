# Pre-VPS Independent Implementation Audit — Claude Opus 5

2026-09-07 / Observer Specification v0.3 / Technocore baseline 0.12.1 / Independent Reviewer (Red Team)

**Verdict: GO WITH CONDITIONS** — P0 0件 / P1 5件 / P2 7件 / P3 4件。

この監査ではcodeを一切変更していない。実行したのはrepository内のread、保存済みartifactの再検証、repository内offline testの実行、およびrepository内一時directoryでの読み取り専用な挙動確認だけである。live Technocore通信、Docker操作、package install、repo外write、secret探索は行っていない。

## 0. 監査で独立に確認できたこと

| 確認項目 | 結果 |
| --- | --- |
| offline regression再実行 | `python3 -B -m unittest discover -s tests` → **Ran 123 tests / OK / 18.459s**。実装側の主張と一致 |
| source hash照合 | `durability-results-recreation-20260907-192144-4562d78c` の `process_ready.source_hashes` 8件と `source-hashes.json` 15件が、現在のrepositoryのfileと**全件SHA-256一致** |
| recreation artifact | A(`...-1`)→削除→B(`...-2`)の実event列、container/volume ID、21項目のconfiguration check、14項目のprocess probeが実データとして存在。捏造の兆候なし |
| live smoke artifact | 6 GETすべて`method=GET`、path は `/config` と `/r/<room>` のみ。write requestの痕跡なし |
| hardening profile分離 | `durability_payload.preflight_policy` はLOCAL profileで3 mount flagのみWARN化。`CORE_CHECKS` 11項目はprofileに関係なくhard fail。`isolation_boundary` も `CORE_CHECKS` のみから算出。**strictはstrictのまま**（`test_container_recreation.test_local_exception_is_exact_and_strict_default_remains` が回帰を守っている） |
| src内のenvironment依存 | `os.environ` / `getenv` / `expanduser` / `Path.home` / `subprocess` / `eval` の使用は **0件** |

test suiteの質は総じて高い。実SIGKILL（`test_crash.py`）、実loopback HTTP serverでのredirect非追従検証（`test_http.py:82`）、実SQLITE_FULL（`test_observer.py:227`）、実flock競合（`test_crash.py:92`）を使っており、mockで本物のfailureを隠している箇所は主要pathには見当たらなかった。以下のfindingは、その上でなお残る問題である。

---

## 1. Read-only guarantee — 重大な問題なし

`SafeClient` は唯一のnetwork出口であり、以下を独立に確認した。

- `_get` のpath allowlistは `("/config", "/r/" + self.room)` の**完全一致**。`room` は `[a-z0-9][a-z0-9_-]{0,47}` に限定されるため `/`・`?`・`.` を含めず、`/say`・`/say-signed`・path traversalへ到達する経路がない（`test_observer.py:294` が `"../keys"`, `"a?write=1"`, `"a/b"` を拒否確認）。
- `method="GET"` は固定。body付きrequestを組み立てる関数が存在しない。
- `NoRedirect.redirect_request` が `None` を返し、301/302/303/307/308は `HTTPError` として素通しされる。**実HTTP serverに `Location: /forbidden-write-lane` を返させた回帰test**があり、requestが1回しか発生しないことをserver側で数えている（`test_http.py:82-91`）。
- `ProxyHandler({})` によりproxy自動探索無効。`HTTP_PROXY` 環境変数を設定した状態での回帰testあり（`test_http.py:69`）。
- `ORIGIN` はmodule定数。CLI override・環境変数overrideは存在しない（test moduleのみが `patch` で差し替える）。
- **message由来のURLを解釈する経路は存在しない。** `content_values` はmessageをSQLiteへ格納するだけで、`text` 内のURLも `unknown` fieldも一切fetchしない（`test_observer.py:172` が `"unknown": "https://evil.invalid/"` を含むrecordで確認）。

「GET-onlyだから安全」という誤った前提には**なっていない**。docsも `vps-migration-checklist.md:30` で「アプリのpath制限とnetwork firewallの保証を混同しない」と明記しており、境界の理解は正確である。

唯一の指摘は P3-1（defense in depth）に記載する。

## 2. Secret / Signer isolation — 重大な問題なし

- private key / seed / wallet / 署名処理を行うcodeは `src/` に存在しない。
- schemaが構造的にSigner機能を禁じている点は特筆に値する。`messages.trust TEXT ... CHECK(trust='untrusted')` と `signature_verified INTEGER DEFAULT NULL CHECK(signature_verified IS NULL)` により、**Observerは「検証済み」を記録することがSQL levelで不可能**。`ingest_source CHECK(ingest_source='poll')` も同様。これはboundaryをcommentではなくconstraintで表現しており、良い設計である。
- HOME・host filesystemへのアクセスは、`--state-dir` で指定されたdirectoryと `shutil.disk_usage` のみ。`_private()` が `lstat` ベースでsymlink・所有者不一致・mode不一致を拒否する。
- Docker socketはObserver本体から一切参照しない。参照するのはtest helper (`container_isolation.py`, `run_*.py`) のみで、deployment `Containerfile` は `COPY src/ /app/src/` だけなのでimageに入らない。

boundaryの曖昧さは P2-2（watchdogが同一UIDと書き込み権限を要求する）に記載する。

---

## Findings

### P1 — FIX BEFORE VPS

---

#### P1-1. 単一responseでdurable inboxが恒久的に破壊される（forward cursor jumpに上限がない）

**severity:** P1
**affected:** `src/technocore_observer/protocol.py:125` (`validate_envelope`), `src/technocore_observer/observer.py:47-53` (`poll_once`), `src/technocore_observer/storage.py:346-359` (`Store.save`)

**問題:**
`validate_envelope` は `seq` を「1以上、2^63-1以下、内部contiguous、昇順」しか検査しない。`poll_once` は「`seq <= poll_seq` を含まない」しか追加検査しない。したがって**現在の `poll_seq` からどれだけ前方へ飛んでいても正常なresponseとして受理され、commitされる。** `save` はその差分をOPEN gapとして記録し、`poll_seq` をserver提示値へ進める。

**failure scenario:**
Technocore側のbug、あるいはTechnocoreの侵害により、1件のmessageが `seq = 2^63-1` として返される。これは1回のresponseで完結する。

**evidence（監査中に実測）:**

```
poll_once -> (True, 0)
poll_seq = 9223372036854775807   resolved_seq = 100   status = DEGRADED
gaps     = [(101, 9223372036854775806)]
next legit poll -> (True, 1.18)
next legit poll -> (True, 2.15)
next legit poll -> (False, 4.23)
final status = ERROR
```

以後、正常なseq=101のmessageは `OLD_RECORD_IN_NORMAL_POLL` となり、3回で `ERROR` に至って停止する。そして**この状態から回復する手段が存在しない**：`gaps.status` は `CHECK(status='OPEN')` で固定、gapを閉じるcodeは皆無（`grep "UPDATE gaps\|DELETE FROM"` → 0件）、resync commandは未実装、`_verify` の `STATE_CURSOR_MISMATCH` / `STATE_SEQUENCE_ACCOUNTING_FAILED` によりDBの手作業修正も拒否される。唯一の復旧はDB破棄であり、Observerが存在する目的そのもの（蓄積されたcorpus）を失う。

現行実装は**データについてはfail-closed**（偽の内容をvalidとして記録しない）だが、**stateについてはfail-destructive**である。P0にしなかったのは、secret露出もwrite capability獲得も伴わず、発生には Technocore 側の異常が必要で、かつObserverは停止して監査可能な記録を残すためだが、この線引きはHumanが上書きしてよい。

**recommended fix:**
`poll_once` で、`envelope["messages"][0]["seq"] - s["poll_seq"]` が閾値Kを超えるresponseを `ProtocolAnomaly`（commitせず、cursorを進めず）として扱う。K はconfig由来の値ではなく保守的な固定値（例: 100,000）でよい。「巨大gapは人間が承認してから受理する」というsemanticsにするのが本来のleast-surprise。あわせて `_http_failure` と同じく evidence を保存すること。

---

#### P1-2. `since` の実semanticsが未検証であり、誤っていた場合は「取得可能だったmessageの恒久的喪失」が静かに起きる

**severity:** P1
**affected:** `src/technocore_observer/http.py:37-43` (`SafeClient.poll`), `src/technocore_observer/storage.py:346-355` (prefix gap生成), `docs/live-smoke-plan.md` / `docs/smoke-review-20260906.md`

**問題:**
cursor正当性の全体が「`GET /r/<room>?since=N&limit=200` は seq=N+1 から**昇順に古い方から**最大200件を返す」という前提に依存している。この前提は**live artifactから証明されていない**。

**evidence:**
`docs/smoke-results-20260906-125741-7d70a083/live/report.json` の request 3:

```
query  : {"since": 0, "limit": 200, "format": "json", ...}
結果   : count=200, first_seq=550, last_seq=749   （roomのtailは749）
```

`since=0` を指定したのに返ってきたのは **tail直前の200件** である。これは次の2つの仮説と**同程度に整合する**。

- 仮説A: serverは `since` 以降の古い方から返す。550が最古の保持messageで、保持件数がたまたま200 = limitちょうどだった。
- 仮説B: serverは `since` を下限としてのみ扱い、常に**最新のlimit件**を返す。

request 4 の `limit=201` も同一bodyを返しており、これも「clampが200」と「保持が200件しかない」の両方に整合する。request 5 は `since=749`（tail）で空、これも識別に寄与しない。**現在保存されている6 GETのどれもA/Bを区別できない。**

docsはこの点を `smoke-review-20260906.md:106` / `vps-migration-checklist.md:51` で「保持件数が200件超だったことを独立確認していない」と記載しているが、これは**保持件数の話であって `since` semanticsの話ではない**。両者は別の問題であり、後者の帰結の方が重い。

**failure scenario:**
仮説Bが正しい場合。Observerを保守やrestartで20分止め、その間にroomへ250件が投稿される。再開後の `poll(since=poll_seq)` はserverから最新200件を受け取り、`first > poll_seq+1` なので**50件のprefix gapをOPENとして記録し、cursorをその先へ進める**。しかしその50件はまだserverに保持されていた可能性が高く、`since` を小さくした読み取りで取得できたはずである。Observerは「取得可能だったmessage」を「恒久的に失われたmessage」として記録し、P1-3により `resolved_seq` は永久に凍結する。

durability fixtureの `"gap": (103, [200, 201])` は、まさにこのB的挙動を**正常なprefix gapとして期待する**形で書かれており、test suiteはA/Bを区別しない。testが実装と同じ未検証前提を共有している例である。

**recommended fix:**
VPS移行前に、read-only GETを**1回**追加して確定させる。保持件数が250件以上あるroomに対し、tailより十分小さい `since=S` を指定し、`first_seq == S+1` となることを確認する。結果を `live-smoke-plan.md` の必須項目に追加し、artifactとして保存する。仮説Bだった場合はObserverの追従設計そのものを見直す必要がある（追加pollingでの巻き戻し取得、またはgapを「取得未試行」として区別する）。

---

#### P1-3. OPEN gapは構造的に永久に閉じられず、最初のprefix gapで `resolved_seq` が恒久凍結する

**severity:** P1
**affected:** `src/technocore_observer/storage.py:50` (`gaps ... status TEXT NOT NULL CHECK(status='OPEN')`), `storage.py:146-152` (`_verify` identity check), `storage.py:357-360` (`save`), `docs/operations.md:38`

**問題:**
3つの制約が重なって、**gapを閉じる将来のcodeを書くこと自体がschema migrationを要求する**状態になっている。

1. `gaps.status` は `CHECK(status='OPEN')` で固定されており、`CLOSED` / `ABANDONED` を記録できない。sqlite3で人手UPDATEすることすらできない。
2. `save` は `has_gap` が真である限り `resolved = s["resolved_seq"]` を据え置く。gapを削除する経路もない。
3. `_verify:146-152` は `messages` / `gaps` 全行が `(room, observer_epoch, server_generation)` の**単一組**と一致することを要求する。つまりDBは1 generation分しか保持できず、generation変更後にresyncしても旧corpusと新corpusを同居させられない。`observer_epoch` は `initialize` で1に固定され、増分されるcodeがない。

**failure scenario:**
liveの `/config` は `retention_seconds` と `room_ring_bytes` を**両方nullで返す**（`smoke-results-20260906-125741-7d70a083/live/report.json` の `config_unavailable_settings`）。したがって保持窓は運用側から観測できない。一方P1-2の観測は保持窓が200件程度である可能性を示唆する。この条件下では、**数十分のObserver停止で確実にprefix gapが発生する**。一度発生すれば `status=DEGRADED` と `resolved_seq` の凍結は当該DBの寿命の間ずっと続く。`poll_seq` だけが伸び続け、Analyzerが「`resolved_seq` まで消費する」contractであれば pipeline全体が最初のgapで永久停止する。`gap_detected` 以降にObserverが集め続けるmessageは、誰にも消費されないまま蓄積する。

`recovery_deadline_hint` も、`retention_seconds` が live で取得不能なため常にNULLであり（`heartbeat.oldest_gap_recovery_deadline_hint: null` が live smoke / soak 双方で確認できる）、運用上の判断材料にならない。

**recommended fix:**
production dataが `user_version=1` の下に溜まる**前に**決めること。稼働中のdurable inboxをmigrationするのは後からでは高くつく。最低限:

- `gaps.status` のCHECKを `IN ('OPEN','CLOSED','ABANDONED')` へ緩め、`_verify` の `resolved` 算出を「最初の**OPEN** gap」に限定する（現在は全gap行がOPEN前提）。codeを今書かなくても、schemaに余地を残しておくだけで将来のmigrationを回避できる。
- gap発生時のHuman review手順（何を確認し、何を根拠にABANDONEDと判断し、`resolved_seq` をどう前進させるか）を `operations.md` に明文化する。`vps-migration-checklist.md:41` に「Human review手順を明文化する」とあるが、上記のschema制約により**手順だけでは実行不可能**である点が現状のdocsに書かれていない。
- generation変更後の運用（DB退避 → 新DBで再init）が唯一の道であることを明記する。

---

#### P1-4. 継続的なHTTP 429/5xxでevidence tableが無制限に増加する（remote-triggerable disk exhaustion）

**severity:** P1
**affected:** `src/technocore_observer/observer.py:67-79` (`_http_failure`), `src/technocore_observer/storage.py:231-237` (`_evidence`), `storage.py:307-320` (`Store.failure`)

**問題:**
`_http_failure` は429と5xxで毎回 `store.failure(kind, reply)` を呼び、`_evidence` が `reply.body`（最大32 MiB）を `body_bytes BLOB` としてINSERTする。429/5xxは `consecutive_protocol_anomalies` を増やさないため**終端状態に到達せず、無限にretryし続ける**。backoffの上限は60秒、`Retry-After` の上限は600秒。evidenceのrotation・上限・削除はcodeに存在せず、`operations.md:71` も「自動削除・rotation・backupは未実装」と認めている。

**failure scenario:**
Technocore前段のCDNが大きめのHTML error pageを付けた503を数時間返す。backoff上限60秒で1日あたり約1,440 evidence行。error bodyが1 MiBなら約1.4 GB/日、32 MiBなら最悪約46 GB/日。`heartbeat` は `disk_free_bytes` を出力するが、それを見て動作を変えるcodeはない。state volumeが満杯になると `SQLITE_FULL` でrollbackし非0終了する（`test_observer.py:227` が確認済み）ので**cursor整合性は守られる**が、VPSでstate領域を他と共有していればhost全体に波及する。

このリスクの現実味は自前のartifactが示している: soakのheartbeatは `disk_free_bytes: 64544768`（= 64 MiB tmpfs）だった。**その構成では32 MiBのerror bodyが2件でstate領域が枯渇する。**

`vps-migration-checklist.md:39` に「disk free、DB/WAL/evidence/log増加…を決める」とあるが、これはpolicyを決めるという項目であり、code levelの上限は依然として存在しない。

**recommended fix:**
(a) evidenceに保存するbody長を上限（例: 64 KiB prefix）にし、全文hashは別途 `_get` 側で計算して列に持つ。(b) 同一 `anomaly_type` の連続failureについてはevidenceを間引く（初回 + N回ごと）か、行数・総bytesの上限を設けて古い行を落とす。(c) `disk_free_bytes` に下限閾値を設け、下回ったら新規evidence書き込みを止めて `ERROR` で停止する。

---

#### P1-5. `MAX_BODY = 32 MiB` は実測の正常最大値の約400倍で、記載されている256 MiB containerでは1 responseでOOMに至る

**severity:** P1
**affected:** `src/technocore_observer/protocol.py:10` (`MAX_BODY`), `src/technocore_observer/http.py:81` (`response.read(MAX_BODY + 1)`), `protocol.py:79-94` (`decode_reply`), `docs/operations.md:20-24`

**問題:**
`decode_reply` は**body全体をJSON parseしてから** `validate_envelope` の `count` 検査と `poll_once:50` の `len(messages) > 200` 検査を行う。したがって200件制限はmemory保護として機能しない。唯一の防壁は `MAX_BODY = 32 MiB` である。

**evidence（監査中に実測）:**

```
body 17.4 MiB (正常なenvelope形状、400,000 messages)
→ 解析後のPython object peak: 101.4 MiB
```

32 MiBへ外挿すると約190 MiB。これにbody bytes 32 MiB とdecode後のstr 32 MiB が加わり合計約254 MiBとなり、`operations.md:20` に記載された `--memory=256m` を実質的に使い切る。一方、liveで観測された正常な最大bodyは **200 messagesで79,888 bytes**（`smoke-results-.../live/report.json` request 3）である。上限が実測値の約400倍に設定されている。

**failure scenario:**
Technocoreまたはその前段が32 MiB弱のresponseを1回返すだけで、containerがOOM killされる。SQLiteは無傷（commitしていない）なのでdata lossはないが、supervisorがrestartしても同じresponseで再度死ぬ可能性があり、remote単発のavailability DoSになる。`vps-migration-checklist.md:39` の「32 MiB response時の最大使用量を保証しない」は、この未解決点を正しく認識しているが、対処はされていない。

**recommended fix:**
`MAX_BODY` を実測に基づく値（例: 1 MiB。200 messages × 400 bytes ≒ 80 KiB に対し十分な余裕がある）へ引き下げる。超過は現行どおり `BODY_TOO_LARGE` の `ProtocolAnomaly` として扱われるので、3回でERROR停止する挙動はそのまま活きる。あわせて `--memory` の実測値を再測定して `operations.md` を更新する。

---

### P2 — FIX / IMPROVE SOON

---

#### P2-1. Docker isolation証跡はすべて「stdin RPC driverをentrypointに持つtest image」のものであり、deployment imageは未build・未検証

**severity:** P2
**affected:** `Containerfile`, `tests/Containerfile.durability`, `tests/run_durability.py:25` (`ENTRYPOINT`), `docs/durability-verification.md`

**問題:**
recreation reportの `expected_entrypoint: true` が検証しているentrypointは

```
["python3", "-I", "-B", "/app/tests/durability_payload.py", "--root", "/state", "--container"]
```

であり、production entrypoint `["python3", "-B", "-m", "technocore_observer"]` ではない。mount・namespace・capability・UIDの検証としては転用可能だが、「Docker isolation verification 完了」という要約は、**stdinからJSON commandを受け付けるdriverが動いているimage**についての結論である。docsは `Containerfile` の冒頭commentと `operations.md:9` でこれを認めているが、要約行では区別がつきにくい。

さらに具体的な差分が1つある。検証されたimageはすべて `python3 -I`（= `-E` + `-s`）で起動しているのに対し、**production entrypointは `-I` を持たない**。production imageは `PYTHONPATH=/app/src` に依存しているため `-I` を付けられない。結果として deployment container では `PYTHONPATH` / `PYTHONHOME` / `PYTHONSTARTUP` / user site-packages がmodule解決に影響し得る。`environment_names_allowlisted` checkはtest helperのassertionであって、runtimeの制御ではない。

**recommended fix:**
`src/technocore_observer` を既定 `sys.path` 上（site-packages、または `WORKDIR /app` 直下）へ配置し、entrypointを `["python3", "-I", "-B", "-m", "technocore_observer"]` にして `PYTHONPATH` 依存を外す。そのうえで deployment image に対して同じ21 configuration checks + 14 process probeを実行し、artifactを取得する。`vps-migration-checklist.md:25` の項目に「entrypointの `-I` 有無」を明記する。

---

#### P2-2. `heartbeat` / `watchdog` はObserverと同一UIDかつstate directoryへの書き込み権限を要求する（独立watchdogの権限分離ができない）

**severity:** P2
**affected:** `src/technocore_observer/storage.py:73-81` (`_private`), `storage.py:255` (`_connect(path, "ro")`), `docs/operations.md:44`

**問題:** 2点ある。

1. `_private` は `info.st_uid != os.geteuid()` で `UNSAFE_STATE_PATH` を投げる。したがって **watchdogを別userやrootで動かすことは不可能**。0700 / uid 65532 のstate directoryに対し、`heartbeat` / `watchdog` を実行できるのは Observer と同一UIDだけである。
2. `readonly=True` の `Store` は SQLite URI `mode=ro` を使うが、**WAL databaseを開く過程でstate directoryへ書き込みが発生する**。

**evidence（監査中に実測）:** SIGKILLでcrashさせて `-wal` を残し、`-shm` を削除した状態で readonly Store を開いた:

```
files       : ['observer.lock', 'state.sqlite', 'state.sqlite-wal']
RO HEARTBEAT (no shm) OK: 102
files after : ['observer.lock', 'state.sqlite', 'state.sqlite-shm', 'state.sqlite-wal']
```

`state.sqlite-shm` が**readonly openによって新規作成された**。`operations.md:44` に「SQLiteがWAL参照のために必要とするsidecarへのアクセスは発生し得ます」とあり事実は認識されているが、同じ段落の「tableは更新しません」と併読すると「state領域はread-onlyでよい」と誤読しやすい。

**failure scenario:**
VPSで `vps-migration-checklist.md:27-28` の方針どおり「dedicated Observer user / 0700 directory」を実装し、監視のためwatchdogを別の低権限userやread-only bindで動かす設計にすると、**watchdogが起動時点で `UNSAFE_STATE_PATH` あるいはSQLite open失敗で常に落ちる**。「独立watchdog」は network sample の独立性はあっても、**権限の独立性はない**。

**recommended fix:**
`vps-migration-checklist.md` に「watchdog / heartbeat は Observer と同一UIDで、state directoryへ書き込み可能な状態で実行する必要がある」を明記する。権限分離が要件なら、watchdogがDBを開かずObserverのheartbeat JSON出力（stdout）だけを読む形へ設計変更する。

---

#### P2-3. `raw_record_json` はserver byteの保存ではなく再serializeであり、成功pathではresponse bytesが一切残らない

**severity:** P2
**affected:** `src/technocore_observer/protocol.py:220` (`content_values`), `src/technocore_observer/storage.py:338-344` (`Store.save`), `README.md`, `docs/operations.md:65`

**問題:**
`raw_record_json = json_dump(message)` は、`json.loads`（`parse_float=_float`）で得たPython objectを `ensure_ascii=True` で再serializeしたものである。列名は `raw_record_json`、READMEは「raw本文…をDB内に保持します」と書いているが、byte一致ではない。`evidence.body_bytes` にrawが残るのは **anomaly / generation change / init のときだけ**で、正常にingestされたmessageのresponse bytesはどこにも残らない。

**evidence（監査中に実測）:**

```
server bytes    : {"seq":1,"ts":1.10,"from":"a","text":"t","nonce":1e2}
raw_record_json : {"seq":1,"ts":1.1,"from":"a","text":"t","nonce":100.0}
```

**failure scenario:**
architecture は `Observer → SQLite Inbox → Analyzer → Human Approval → Signer` である。将来 `sig` fieldをAnalyzer/Signer段で検証しようとすると、署名対象の正確なbytesが必要になるが、Inboxからは復元できない。人間の紛争解決（「Technocoreが実際に何を返したか」）にも答えられない。live 0.12.1 の `ts` は string、`nonce` は integer なので現時点で実害は出ないが、`content_values` は数値 `ts` への型drift を明示的に許容しているため、drift時に静かに情報が落ちる。

なお `test_live_compatibility.py:118` の round-trip test は `json.loads(row["raw_record_json"]) == obj["messages"]`、すなわち**parse済み同士の比較**なのでこの損失を検出できない。testが実装と同じ前提を共有している例である。

**recommended fix:**
どちらかを選ぶ。(a) response bodyのSHA-256（と、必要なら直近N件のbody本体）をenvelope単位で保存し、messageからそのevidence行を参照できるようにする。(b) 保存しないと決めるなら、列名を `normalized_record_json` 等に改め、README / `operations.md` / v0.3仕様に「成功pathではserver bytesを保持しない」と明記し、Signer設計がそれを前提にしないようにする。

---

#### P2-4. single-writer保証はCLIにあり `Store` にはない

**severity:** P2
**affected:** `src/technocore_observer/storage.py:240-280` (`Store.__init__`), `src/technocore_observer/cli.py:77` (`with StateLock(...)`)

**問題:**
flockを取得するのは `cli._execute` であり、`Store` は read-write modeでもlockを要求しない。`Store(directory, room)` を直接構築すれば（`durability_payload.Driver` は明示的にlockを取るが、将来のAnalyzerや運用scriptがそうする保証はない）、2つのwriterが並走できる。

**failure scenario:**
writer AとBが同時に `poll_once` を実行。Aが101..102をcommitした後、Bは stale な `s["poll_seq"]=100` を使って `OLD_RECORD_IN_NORMAL_POLL` の判定を行うため、この事前checkは意味を失う。実際には `save` がtransaction内でstateを読み直し、`messages` のPRIMARY KEY衝突が `IntegrityError` → `UNEXPECTED_CONSTRAINT_COLLISION` として rollback するため、**現状はconstraintのおかげでfail-closedになる**。しかし安全性がlockではなく制約に依存しており、宣言された不変条件（`storage.py:1` の docstring「guarded by a process-lifetime flock」）と実装が一致していない。

**recommended fix:**
`Store.__init__` の `readonly=False` 経路で `StateLock` を取得（または既に保持されていることを検証）する。あるいはdocstringとAPI契約を「呼び出し側がflockを保持していること」と明示し、`test_soak.py:221` に加えて「lockなしのStore構築を禁止する」regressionを足す。

---

#### P2-5. `_verify` はStore open毎（= heartbeat / watchdog 実行毎）にmessages全件を走査する

**severity:** P2
**affected:** `src/technocore_observer/storage.py:155-169` (`_verify` の interval accounting), `storage.py:263-265`

**問題:**
`SELECT seq FROM messages UNION ALL SELECT start_seq,end_seq FROM gaps ORDER BY start_seq` を read transaction 内で全件走査する。これは論理的なcursor破損を検出する優れた不変条件だが、**コストがcorpusのサイズに比例し、`heartbeat` と `watchdog` の毎回の実行でも発生する**。長時間動くreaderはWAL checkpointを妨げるため、WALファイルも膨らむ。

**failure scenario:**
supervisorがwatchdogを30秒毎に起動し、数か月でmessagesが数百万件になった状況。毎回の全件走査+sortがwatchdogのlatencyと `--stall-seconds` 判定に影響し、同時にWALが肥大する。

`operations.md:73` はこの性質を正しく開示しているが、**唯一のlive soakでmessagesは4件しか保存されておらず**（P2-6）、実測の裏付けがない。

**recommended fix:**
起動時（Store open）はfull verify、`heartbeat` / `watchdog` は `state` 行 + gap集計のみの軽量pathへ分離する。あるいは `poll_seq` を境に増分検証する。VPS移行前にmessages 10^5〜10^6件のsynthetic DBで所要時間を実測し、checklistへ数値を記録する。

---

#### P2-6. live証跡が薄く、かつ最も強い証跡が再現不能

**severity:** P2
**affected:** `docs/soak-results-live-20260906-133730-1b37726d/report.json`, `tests/test_live_compatibility.py:82`, `.gitignore`, git repository state

**問題:** 3点が重なっている。

1. **60分live soakの情報量がほぼゼロ。** report によると 300 requests / 3603秒で `snapshot_counts.messages = 4`、`gaps = 0`、`poll_seq` は 752→756。`disk_free_bytes: 64544768` から実行媒体は64 MiB tmpfsであり、永続volumeではない。つまりこのsoakは **catch-up path、gap検出、200件page、DB/WAL増加、容量挙動のいずれも検証していない**。HTTP 503×1 と network failure×2 からのrecovery確認としては有効。docsはWARNを維持し「長期容量を保証しない」と明記しており記述は正確だが、「60-minute live soak完了」という要約は実際の情報量より強く読める。
2. **Technocoreの実bytesを検証する2 testがskip可能。** `test_live_compatibility.py:82` は `@unittest.skipUnless(EVIDENCE.is_dir(), ...)` であり、`EVIDENCE` は `docs/smoke-results-20260906-085129-5ac4541a/live`。`.gitignore` は `docs/smoke-results-*/` を除外している。したがって**clone先では 121 PASS / 2 skip になり、実server bytesに対する唯一のregressionが消える。**「123 tests PASS / skip 0」はこのmachine固有の数字である（監査でも123/OKを再現したが、それはartifactが手元にあるため）。
3. **git commitが0件。** `git log` は `does not have any commits yet`。source も artifact も version管理下になく、`source-hashes.json` は内部整合性を示すだけで改竄不能性を担保しない。`vps-migration-checklist.md:25` はこれを認識している。

**recommended fix:**
(1) 現状のまま進めるなら、`vps-migration-checklist.md` の「Bounded live soak」行に「保存message 4件・gap 0件・tmpfs実行」を明記して、情報量を要約行から読み取れるようにする。(2) live evidenceの最小subset（response bodyとreport）をrepository管理下に置くか、skip時に警告を出す。(3) VPS移行前にcommitし、audit対象のtreeをtagで固定する。

---

#### P2-7. `--state-dir` の既定値が相対path `state` である

**severity:** P2
**affected:** `src/technocore_observer/cli.py:54` (`default=Path("state")`), `storage.py:93` (`mkdir(mode=0o700, exist_ok=True)`)

**問題:**
`init` は `StateLock(create=True)` でCWD直下に `state/` を作る。containerでは `WORKDIR /app` がread-onlyなので失敗して気づけるが、VPSでsystemd unitや手作業から直接起動した場合、operatorのHOMEや `/` 配下にdurable inboxが作られ得る。`vps-migration-checklist.md:26` の「専用state path」方針と衝突する。

**recommended fix:** productionでは `--state-dir` を必須にする（`required=True`、または既定値を絶対pathにして相対pathを拒否する）。少なくともchecklistへ「起動commandで `--state-dir` を明示する」を追加する。

---

### P3 — NICE TO HAVE

**P3-1. `build_opener` が `FileHandler` / `DataHandler` / `FTPHandler` / `HTTPHandler` を保持したまま。**
`src/technocore_observer/http.py:28-31`。`ORIGIN` が定数で path が allowlist されているため現状到達不能だが、`file://` や `data:` を扱えるopenerを持つ必然性がない。`urllib.request.OpenerDirector()` に `HTTPSHandler` / `NoRedirect` / `ProxyHandler({})` / `HTTPErrorProcessor` / `HTTPDefaultErrorHandler` のみを `add_handler` する形にすれば、将来ORIGINやpath処理が緩められた場合の被害を構造的に抑えられる。

**P3-2. isolation probeがhard-codedなhost HOME pathを使っている。**
`tests/durability_payload.py:184`、`tests/container_isolation.py:43`。`no_host_home` / `host_home_absent` はVPSでは別userになるため**常にtrueになり無意味**。VPS profileでは実際のhost HOME pathを引数化するか、`/home` と `/root` の存在有無を見るべき。この状態のまま「host HOME非公開 PASS」をVPSで再利用しないこと。

**P3-3. `_private` がfd ではなく path を再度 lstat する。**
`storage.py:73-81` を `os.open` / `exists` の後に path で呼んでいる（`StateLock.__enter__:98-100`、`Store.__init__:246-254`）。directoryが0700・自UID所有なので実質exploit不能だが、`os.fstat(self.fd)` を使えばTOCTOUの議論自体が消える。

**P3-4. `watchdog` の `EMPTY_OR_REAPED` が exit 0。**
`cli.py:76`、`operations.md:55`。room が reap / reset された状況でObserverが停止していても、supervisorには成功として見える。既知の限界として文書化済みだが、VPSでは exit code の扱いを再検討する価値がある。

---

## 3. Audit areas — 個別評価

| # | 領域 | 評価 |
| --- | --- | --- |
| 1 | Read-only guarantee | **良好。** path/method/redirect/proxy/TLS/message由来URLすべて塞がれており、実HTTP serverによる回帰testもある。指摘は P3-1 のみ |
| 2 | Secret / Signer isolation | **良好。** src内にkey/seed/署名codeなし、環境変数参照0件、Docker socket参照0件。`CHECK(trust='untrusted')` と `CHECK(signature_verified IS NULL)` によるschema levelの境界表現は優れている。境界の曖昧さは P2-2（watchdogの権限）と P2-3（Signerが依存し得ないevidence） |
| 3 | Sequence / cursor correctness | **重大な指摘あり。** contiguous / internal hole / duplicate / old seq / empty response / prefix gap の扱いは正しく、`_verify` の interval accounting は論理破損まで検出する優れた不変条件。ただし**前方への飛びに上限がない**（P1-1）、**`since` semanticsが未検証**（P1-2）、**gapを閉じられない**（P1-3） |
| 4 | Generation / epoch | **良好。** generation不一致は sequence 検証**前**に判定して `NEEDS_RESYNC` で停止し、messageを一切commitしない。`generation` 欠落・bool・負値はすべて `ProtocolAnomaly`。restart後も `Store.__init__` の `RUNNABLE` 判定で開けない。ただし `observer_epoch` は常に1で増分codeがなく、`_verify` が単一identityを強制するため、resync実装時にschema制約に当たる（P1-3に含む） |
| 5 | SQLite durability | **良好。** WAL + `synchronous=FULL` + `foreign_keys=ON` をopen時に実測verify。message / gap / cursor は単一transaction。init は DELETE journal → close → fsync → rename → directory fsync の正しい atomic publish。実SIGKILLによるCOMMIT前後testあり。backup は `sqlite3.Connection.backup` で cp を使わない。**ただしproduction CLIにbackup commandは存在しない**（`init/run/heartbeat/check-config/watchdog` のみ）ので、「backup検証済み」はtest harnessの能力であってproduction運用手順ではない。checklist:38 が未完了項目として持っているのは正しい |
| 6 | HTTP / failure handling | **概ね良好。** timeout / network failure / 429 + Retry-After（負値・非数・日付・巨大値のclamp含む）/ 5xx / redirect / malformed JSON / 重複key / 非有限数 / 不正Content-Type / body上限 / anomaly 3回でERROR、いずれも回帰testあり。failure時にcursorが進まないことは複数testで確認済み。指摘は evidence 増加（P1-4）と `MAX_BODY`（P1-5） |
| 7 | Docker isolation | **profile分離は正しい。** LOCAL profileが緩めるのは3 mount flagのみで、`CORE_CHECKS` 11項目と21 configuration checksはprofileに依存せずhard fail。`isolation_boundary` の算出も `CORE_CHECKS` のみ。`test_container_recreation.py:25/39/106` が strict default と全項目維持を守っている。**他のsecurity checkは緩んでいない。** ただし対象imageがtest image（P2-1）、probeのhost HOME判定がhard-code（P3-2） |
| 8 | Test quality / false confidence | **高品質だが穴がある。** 実SIGKILL・実HTTP server・実SQLITE_FULL・実flockを使っており、mockが本物のfailureを隠す構造にはなっていない。Docker mock testは「Docker実測ではない」と明示的に区別されている。穴は3つ: (a) round-trip testがparse済み同士の比較でserialize損失を検出できない（P2-3）、(b) durability fixtureの `"gap": (103, [200,201])` がP1-2の未検証前提をそのまま正解として埋め込んでいる、(c) 実server bytesに対する唯一のtestがskip可能で成立条件がmachine固有（P2-6） |
| 9 | Evidence claims | **総じて誠実。** docsは overclaim より underclaim 寄りで、`production_hardening_gate=PENDING`、`full_durability_gate=PENDING`、WARNの保持、「Local warningをproduction-readyと扱わない」の明記が一貫している。source hashは実際に現在のfileと一致した。**Local PASSをProduction-readyと誤認させる表現は見つからなかった。** 補正すべきは、要約行が実際の情報量より強く読める2点（soakの中身がmessage 4件であること、Docker isolationの対象がtest imageであること）と、`since` semanticsの未検証が「保持件数の未確認」に吸収されて別問題として立っていないこと |
| 10 | VPS deployment readiness | checklist は網羅的で、指摘の多くを既に未完了項目として抱えている。**追加すべき項目**は次節 |

## 4. `vps-migration-checklist.md` へ追加すべき項目

既存のchecklistは production hardening / storage / audit の3 gateをよく分解している。以下は現状**書かれていない**、または書かれていても実行不能な項目である。

1. **`since` semanticsのread-only確認**（P1-2）。保持250件超のroomに対し `since` を tail より十分小さく指定し `first_seq == since+1` を確認する。GET 1回。これは既存の「保持件数200件超の未確認」とは別項目として立てる。
2. **前方cursor jumpの上限**（P1-1）。閾値を決め、実装し、regressionを追加してから移行する。
3. **gap閉鎖のschema余地とHuman review手順**（P1-3）。production dataが溜まる前に `gaps.status` のCHECKを決着させる。現行checklist:41 の「Human review手順を明文化する」は、schema制約により手順だけでは実行不能である旨を追記する。
4. **evidence / eventsのcode levelの上限**（P1-4）。policyを決めるだけでなく上限を実装したうえで移行する。
5. **`MAX_BODY` の再設定と memory実測**（P1-5）。checklist:39 の「32 MiB response時の最大使用量」を実測値で埋める。
6. **watchdogの権限要件**（P2-2）。「dedicated Observer user」「0700 directory」項目に、watchdog / heartbeat が同一UID かつ state directory へ書き込み可能でなければならないことを明記する。read-only bindやuser分離を前提にした監視設計を採らない。
7. **deployment imageのentrypoint**（P2-1）。`-I` の有無をimage固定項目に含める。deployment image に対する21 configuration checks + 14 process probe の再実行をchecklistの明示項目にする。
8. **`_verify` full scanの実測**（P2-5）。messages 10^5〜10^6件での Store open / watchdog 所要時間とWAL挙動を測り、閾値と監視周期に反映する。
9. **`--state-dir` の明示必須化**（P2-7）。
10. **isolation probeのVPS対応**（P3-2）。hard-codedなhost HOME pathのまま「host HOME非公開 PASS」を転用しない。

## Final verdict

### GO WITH CONDITIONS

以下を満たせばVPS migration phaseへ進めてよい。

- **移行前必須（P1全件）:** P1-1（forward cursor jump上限）、P1-2（`since` semanticsのGET 1回確認）、P1-3（gap schemaの決着とreview手順）、P1-4（evidence上限）、P1-5（`MAX_BODY` 引き下げ）。P1-2 は read-only な確認であり、結果次第では追従設計の見直しが必要になるため**最初に実施すべき**。
- **移行と並行して可（P2）:** P2-1、P2-2 は VPS 構成設計そのものに影響するので、deployment 手順を書く前に反映すること。P2-3〜P2-7 は移行後の早期対応でよい。

### Counts

| severity | count |
| --- | ---: |
| P0 | **0** |
| P1 | **5** |
| P2 | **7** |
| P3 | **4** |

### Confidence

- **Read-only guarantee / secret isolation: 高。** src全体（1,095行）を通読し、network出口が1箇所であること、環境変数・subprocess・message由来URLの経路が0件であることを直接確認した。
- **Sequence / durability / HTTP: 高。** 実装を通読したうえで、P1-1・P2-2・P2-3・P1-5 は監査中に実際に走らせて挙動を確認した。
- **Docker isolation: 中。** 保存artifactの内容とhelperのpolicy codeは検証したが、Docker自体には接続していないため、artifactが記述どおりの操作から生成されたことは検証していない（source hash一致は確認済み）。
- **Evidence claims: 中〜高。** 主要artifactは実データとして存在し、source hashは現在のrepositoryと一致した。ただしgit commitが0件でartifactもversion管理外のため、改竄不能性は検証していない。

### Biggest remaining unknown

**`GET /r/<room>?since=N&limit=200` の実semantics（P1-2）。**

Observerのcursor正当性の全体がこの1つの前提に乗っている。現在保存されている6 GETは「since以降を古い方から返す」と「常に最新のlimit件を返す」を区別できず、実測（`since=0` → 550..749）はむしろ後者とも整合する。後者だった場合、Observerは**まだserverに存在するmessageを恒久的な喪失として記録し、P1-3によりpipelineが最初のgapで永久停止する**。これはread-only GET 1回で確定できるにもかかわらず未確認であり、VPSで長時間運転を始める前に解消すべき最大の不確定要素である。

次点は、liveの `/config` が `retention_seconds` と `room_ring_bytes` を両方nullで返すため、**gap発生確率と `recovery_deadline_hint` の実用性を運用側が一切見積もれない**こと。
