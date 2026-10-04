# Claude Runtime Service v1 — Offline checkpoint (2026-09-19)

## 開始状態

- Repository: `<REPO_ROOT>/components/collaboration/worker`
- Branch: `main`、HEAD: `565414609934bd9f276c66a7df8abf1309060bda`
- Worktree: clean。別Repository、Production、実credentialには接続していない。

## 構成と再利用

```text
Trusted offline supervisor: private root / fixed task digest / socket provisioning
    Activity process: RuntimeClient + ReviewRequest (credentialなし)
        ↓ bounded JSON frame over supplied anonymous socket
    runtime_service.py: one-shot dedicated process
        ↓ private envelope, only after runtime ready
    runtime_fixture.py: non-dumpable + Landlock/seccomp + fixed fake review
        ↓ output checked against credential-independent deterministic report
    Service → ReviewResult only → Activity → standalone offline Human Preview
```

`runtime_interface.py`はcredential layerをimportしない。公開型は
`ReviewRequest(locator, text, timeout_ms)`と`ReviewResult(task_sha256, report, offline)`、
APIは接続済みsocketを受け取る`RuntimeClient.review()`。
root／起動設定／credential参照／env／argv／model／toolを選ぶ引数はない。
clientを迂回したwire入力にもServiceが同じ検査を適用する。

`runtime_service.py`は独立entrypointでありWorker CLIやToolには登録しない。
依存方向はActivity → interface、Service → interface / private runtime / Secret Handoff。
資料処理は既存`FixtureProvider`とreview／引用検証を再利用する。
既存Claude adapter、B1 launcher、B2/B3/B4 Real launcher、Human brokerの契約・ledgerを調査した。
dummy値を既存brokerのraw-response保存経路へ通す必要はないため接続せず、Real Gateも変更しない。

Secret Handoffの`exchange()`を再利用し、匿名socketのstdin/stdout/stderr、ready待ち、
空env、FD非継承、単回起動、出力合計上限、wall timeout、kill/reap、子孫cleanupを維持する。
既存`isolation.enter(..., secret_peer=parent)`がruntimeのstdio peerを確認し、
read-only Landlock / seccompへ入る。credential受領前にnon-dumpable化する。
Secret Handoff A/B/Cや既存consumerの出力破棄方針は変更しない。

## Interface / authority / budgets

| 項目 | v1 Offline制限 |
| --- | --- |
| operation | `review_saved_document`のみ。保存資料1件の固定TODOレビュー |
| Task許可 | supervisorが`policy.json`へ固定したlocator＋本文のSHA-256に完全一致 |
| wire schema | version=1、operation、task={locator,text}、timeout_msのみ。未知／重複field、NaN、不正型を拒否 |
| 入力 | JSON frame最大65,536 bytes、本文30,000文字、locator 1,000文字。locatorを取得・openしない |
| timeout | requester指定50〜5,000 ms、既定2,000 ms。Service側で検査しruntimeへ適用 |
| admission | 1秒のtotal wall deadline。不完全／遅いframeも停止 |
| concurrency | 同じtrusted service rootあたり1。process間flock、待ちqueueなし |
| execution | rootあたり許可Task1件・attempt1回。credential生成前にconsumedを永続化 |
| 結果 | public envelope最大32,768 bytes。private stdout＋stderr＋readyも32,768 bytes以下 |
| failure | 固定refused envelope。raw error、exception context、stderrを返さない |
| retry / fallback | 自動再試行、別credential、別provider、Claude APIへのfallbackなし |

Supervisorは0700 rootに`policy.json`と空の0700 `runtime/`を事前用意する。
policyは`{"version":1,"task_sha256":"<canonical task digest>"}`。
Service起動引数はrootのみ、stdinはSupervisor作成の接続済み匿名socket。
起動手順はsmokeのtrusted harnessが示す。Activity向けのroot作成／許可発行APIはない。
複数rootは独立instanceとなるため、root作成権限をActivityへ与えてはならない。

## Credential / output boundary

Serviceがnon-dumpable化してからrandom dummy値をmemory内で生成する。
実credential source、setup-token、/login、1Password、credential file、Secret Manager、
実LLM／API／networkの実装・選択肢はない。dummy値をargv・env・task・policyへ置かない。
private runtimeは値のdummy形式を検査し、固定Fixtureを実行する。
Secret保持側にprompt解釈、planning、shell、任意tool、URLアクセスを持たせない。

runtime stdoutはprivate memoryでのみ受け取り、credential非依存に算出した既存Fixtureの
canonical reportと完全一致する場合だけ公開する。返すのは再構成reportでありraw bytesではない。
stderrは常に破棄する。通常／Base64／hexのcredential echo、summary改変も照合で拒否する。
これは**決定的Offline Fixture専用**の公開条件であり、実Claudeの自由な出力に使える
一般的なSecret除去機構を実装したものではない。

永続化は`consumed.json`と定数fieldだけの`evidence.json`。secret、secret digest、raw response、
private envelope、environment値、例外本文は収録しない。DBや汎用logging経路は追加していない。
smokeの`human-preview.json`はstandaloneのOffline成果物であり、既存Human brokerの
provenance検証・送信承認を取得したartifactではない。

## Verification

- `python3 -B -m unittest discover -s tests -v`: **105 tests PASS**（新Service 9 testsを含む）。
- 新Serviceのtargeted suiteもPASS。schema／未許可Task／Capability追加field／二重実行を拒否。
- dummy credential failure、runtime失敗、timeout、oversized／malformed output、credential echo、
  ready前の隔離失敗を注入し、許可が消費済みでfallbackがないことを検証。
- 出力・error・生成file・Evidenceにdummy値とBase64／hex表現がないことを確認。
- Service memory／FDへの同UIDの`/proc`読取りはPermissionError。
- runtimeの環境は空、外部file／policy read・write・socket生成・execをEPERM/EACCESで拒否。
- process間lock、実行中の第二要求拒否、request／result budgetを検証。
- `python3 -B tests/runtime_service_smoke.py`: **PASS**。
  別Activity process → Service process → confined runtime → Activity → Human Preview。
  Activity processにcredential／service実装のimportがないことを確認。
- `python3 -B src/ccw/isolation.py`: Landlock ABI 3。
- READMEの`smoke.py`、`b1_smoke.py`、`secret_handoff_probe.py`、`secret_boundary_probe.py`、
  `secret_abc_probe.py`、`secret_consumer_window_probe.py`: **全PASS**。

成果物例（全てgit対象外）:

- `.local/runtime-service-smoke-01oan3jx/`
- `.local/smoke-z_h506ha/`、`.local/b1-smoke-9he4lt1x/`
- `.local/secret-handoff-r0tq__zn/`、`.local/secret-boundary-g91cx6za/`
- `.local/secret-abc-x0fjxmm_/`、`.local/secret-consumer-window-cnyoqgjy/`

初回のsandbox内testは匿名socketのsendでEPERMとなり停止した。
許可を受けた通常ローカル実行でPASS。Worker／runtime自身のLandlock/seccompは弱めていない。
外部network利用やProduction GOの承認ではない。

## Security / residual / recovery

新しい既存基盤のSecurity Findingは確認していない。runtime echoは到達可能な合成faultとして
注入し、raw出力を公開すると漏洩する影響に対して、完全照合とstderr破棄が拒否することを確認した。
実Claudeに同じ保証が成立すると推測してClosed扱いにはしない。

信頼対象はSupervisor、Service/bootstrapコード、OS／Pythonとprivate rootの管理者。
Pythonのimport分離はOS権限分離の代わりではない。同一UIDの未隔離攻撃者によるroot／code改変・
独自instance作成を防ぐProduction権限構成は未実装。Activityへ接続以外の管理権限を渡さない
前提のOffline Component検証であり、既存Workerにはsocket capabilityを追加していない。
Serviceは固定trusted orchestration process、taskを扱うruntimeがLandlock/seccomp対象。
別service user、Activityの運用時隔離、Serviceのfilesystem/network制限は今後の設計対象。

SIGKILL/native crash等でcleanupできない既知のprocess lifecycle残Riskは維持する。
runtimeはparent-death SIGKILLで停止しfork不可だが、完全なcgroup cleanup保証ではない。
親強制終了でroot lockが解放されてもconsumedを自動復活させない。
応答消失・中断時はconsumed／Evidenceを保全し、成功と推測せず停止する。再実行APIはない。
Python memoryの完全zeroization、core以外のOS保存領域、malicious trusted runtimeの
timing等のcovert channelは保証しない。credential方式・実Claude出力検証は意図的に未解決。

実Secret、課金、重要network追加、Production mutation、外部write、push/publishのHuman Gateは
発生していない。次はHumanが公開技術資料1件を選び、保存本文を同じOffline interfaceへ固定して
Previewを確認する段階。実credential／実Claudeへの接続前には別の明示的Human Gateが必要。
local commitはEngineering recovery pointであり、本番承認ではない。
