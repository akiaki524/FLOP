# Batch 16 — Real Pilot Protocol Reconnaissance / Offline Dry-run

**Batch 16は完了。Real Pilotは未実施。個別ACCEPT承認での1件試行を第一案とし、事前委任が必要とは判断しない。**
公式実装で他者の実公開4契約をreplayし、claimed 1、refunded 1、観測上accepted 2。
TEST fixtureは既存GCD Solver・独立検証から署名、mock LOCK、Delivery、REVEAL、receiptまで完了した。
ただし、後続ACCEPTへのReal LOCK、同一OFFERの複数Real LOCK、Payerの一般的な選定規則は未確認。
claimed事例の現在のPaperRail Noteは公式形式と異なる。この境界を解決せずReal操作へ進めない。

## 1. 確認したSourceと基準状態

開始時の実測は `main / 65d05f9ee46dea09a2ed050a4b590065335d74ae`、worktree clean。
Handoffとの差異はなかった。reset、履歴改変、既存変更の破棄なし。
終了時のcommitは本Reportを含むBatch 16のlocal commit。その確定SHA・clean確認は、commit後に生成する
`.local/batch16/final-state.json` とHumanへの最終応答に記録する。自分自身のcommit SHAを本文へ埋め込む循環は避ける。
commit直前HEADは開始HEADと同一。pushなし。

| 区分 | 今回の根拠・確認範囲 |
| --- | --- |
| 原文確認 | User提示の共通AGENTS.mdとBatch 16指示、README、docs/design.md、docs/references.md、Batch 15 Report、必要な既存runner/Solver検証コード |
| Repository規約 | 当該Project内に追加の適用AGENTS.mdは見つからなかった。調査用公式checkoutのAGENTS.mdはそのSourceの説明として読んだ |
| 引継ぎ報告 | Batch 15の15件ローカル完了・export timeout・128 GET飽和等は既存Reportの報告。Batch 1〜15の全面再評価は行っていない |
| 今回の作業前提 | Read-onlyは1件Pilotに十分というPhase-Gate判断、Freeze、One Identity、Real Secret禁止は今回のUser指示に基づく |
| 原文未確認 | 独立したPhase-Gate Audit、Roadmap、別のProject Policy／Handoff原文。手元のdocsには見つからず、確認済みとはしない |
| 今回の実測 | 下記の公式pin確認、署名検証、契約replay、TEST dry-run、107 regression、11,295ファイルのhash保持 |
| AI提案 | 第5〜6節の次回Gate・時間上限・対象選定。今回の実行許可ではない |

公式Repositoryは [flop-labs/tclk](https://github.com/flop-labs/tclk)。GitHub APIのmainを
2026-09-19 **13:24:44 UTC** に取得し、`5cc4ab93efbc8999a3a7e1471b639deca25998ea`を確認した。
既存のclean checkoutも同SHA。pinしたSPEC、transcript.ts、package.jsonを公式rawから再取得し、localとbytes一致。
採用packageは **`@flop-labs/tclk 0.1.0`**、signing helperは同commitの`mcp/src/signing.ts`。
package versionだけではrevisionを特定しない。Technocore Productionにこのcommitが配備されているかは未確認。

参照した公式の主な箇所は
[SPEC.md](https://github.com/flop-labs/tclk/blob/5cc4ab93efbc8999a3a7e1471b639deca25998ea/SPEC.md)、
[transcript.ts](https://github.com/flop-labs/tclk/blob/5cc4ab93efbc8999a3a7e1471b639deca25998ea/src/transcript.ts)、
[machine.ts](https://github.com/flop-labs/tclk/blob/5cc4ab93efbc8999a3a7e1471b639deca25998ea/src/machine.ts)、
[paper-rail.ts](https://github.com/flop-labs/tclk/blob/5cc4ab93efbc8999a3a7e1471b639deca25998ea/src/paper-rail.ts)、
[signing.ts](https://github.com/flop-labs/tclk/blob/5cc4ab93efbc8999a3a7e1471b639deca25998ea/mcp/src/signing.ts)。
frames.ts、technocore.ts、rail.ts、golden vectors、audit-export.mjsも原文確認した。
live-deal.mjsは参照のみで実行していない。

Venue仕様は[公式llms.txt](https://technocore.chat/llms.txt)を13:24:49 UTCに保存。
SHA-256は `40e0bebabcc105a2931805b68e200f1d5fc212e32ca14501ac4d85d105a2bb54`。
Sourceの取得URL、日時、hash、失敗履歴は機械可読Reportと`.local/batch16/`のledgerにある。
初回sandbox内DNS失敗は保持し、承認されたsandbox外の取得1回で完了した。
ブラウザでも入口2ページを確認し、GitHub APIはそのツールでは取得不能だった。保存bytesの根拠はledgerの取得である。

公式コード16ファイルをNode **v22.22.0**の`stripTypeScriptTypes(mode=strip)`で型消去し、
`.local/batch16/official-runtime/`へ別出力。型検査や公式build全体を実行したという主張ではない。
Source／生成物のhashを固定し、原本を改変していない。暗号依存はUserの明示承認後に
`@noble/curves`、`@noble/hashes`、`@scure/base` **各2.4.0**を取得し、公式pnpm-lockのSHA-512と照合。
install script、依存全体のinstall、MCP serverは実行しない。公式コードはApache-2.0、既存checkoutにLICENSE/NOTICEを保持。
再利用コード・依存は`.local/`内のみでGitへvendorしていない。

使用APIは`makeOffer`、`makeAccept`、`makeHeartbeat`、`encodeFrame`、`decodeFrame`／`tryDecodeFrame`、
`transcriptRecord`、`verifyTranscriptRecord`、`findContractHandshake`、`foldTranscript`、`parseTranscriptExport`、
`generateHashLock`、`verifySecret`、`dealRoom`、`paperNote`、`lockTerms`、`PaperRail`、`MemoryNoteStore`、
`decodePaperRecord`、signing helperの`signerFromSeed`／`canonicalMessage`／`sweep`。
canonicalization、ID、署名preimage、署名検証、foldを独自実装していない。

## 2. Protocol調査

### OFFER・複数ACCEPT・LOCK

**仕様・実装:** OFFER IDはOFFER内容をcommitする。contract IDはfull OFFERとaccept-core
（ref、from、statement、paymentKey、frame nonce）をcommitする。同じOFFERでも異なるACCEPTなら別contractになり得る。
`dealRoom(contract)`は`mb-p-tclk-`とcontractの先頭16 hexから導出する。room名は短縮識別子なので、
判断では常にfull contract IDも検査する。非列挙のroomでも公開boardから名前を導出でき、秘密の場所ではない。

有効なACCEPTには、正しい署名、senderとframe.from一致、正しいboard、認証された先行OFFER、
正しいref／再計算contract、別当事者、lock種別に合うstatement、必要なpoint paymentKey、
venue時刻で`accept.ts < expiresMs`が必要。frameを構築できただけでは、時刻やroomの検証完了にはならない。

`findContractHandshake`は指定contractの認証済みOFFER／ACCEPTを**渡されたappend順序**から探す。
それだけでは期限・状態遷移を検証しない。抽出した2 recordsに`foldTranscript`を適用して初めてacceptedを判定した。
foldは1契約を扱い、board全体の契約一覧やPayer選定器ではない。
同じfoldへ別契約のACCEPTを続ければ拒否されるが、これは先着ACCEPTの全OFFERに対する優先権ではない。

先着優先、排他的なOFFER消費、1 OFFERにつきLOCKは1件という大域制約は、確認した公式実装にはない。
別contractとして個別foldすれば複数LOCKが可能で、TESTで確認した。
実公開の後続ACCEPTへのLOCKと同一OFFER複数LOCKは、今回の代表例では**未確認**。
確認したTask全文にはdelivery・heartbeatの指定があるが、Payerの一般的な選定アルゴリズムは明示されていない。
「どこにも公開されていない」とまでは調べていない。

**未LOCK:** acceptedに自動timeout遷移はない。expiresMsはACCEPT期限で、acceptedの強制終了時刻ではない。
PayerのLOCKはrefundAfterMs未満ならProtocol上可能で、claimByMsを過ぎたLOCKも別途運用guardが必要。
未LOCK時は当事者のcancelでcancelledにできるが、時間経過だけではacceptedのまま。
refundAfterMs後のheartbeatもacceptedのまま受理されることをTESTした。
運用側の待機終了とProtocol terminalを混同しない。

**推奨への含意:** 他者ACCEPTを観測しただけで候補を失効扱いにしない。
Handoffの「最初のACCEPT中央値約8秒」は今回再計算しておらず、それだけで自動ACCEPT・事前委任を必要としない。

### LOCK・Delivery・REVEAL・receipt

LOCK frameのfoldはPayer、contract、room、offered rail、期限を検査するが、railを呼び出さない。
`verifyLock(lockTerms(state), ref)`は別の検査である。署名が有効なLOCKだけでrail整合を成立扱いにしない。

PaperRailはno-valueのrehearsalで、amountやassetにFLOPと書いてあっても実資産を持たない。
wireのamountは正数必須であり、no-valueを`amount=0`と表現してはならない。
公式PaperRailの`verifyLock`はref=contract、status=locked、lock種別、statement、refundAfterMsを照合する。
Noteはamount、asset、parties、claimByMsを保持しないため、それらをNoteから独立に検証することはできない。
さらにNoteはworld-writableで、trueはその時点の文字列整合にすぎない。
過去のLOCKを現在のclaimed／refunded NoteへverifyLockして失敗しても、過去のLOCK不存在とはならない。

Deliveryはtclk frame typeではない。今回のGCD full specは、指定の一行回答をdeal roomへ署名付きで納品してからREVEALする規約だった。
Taskと納品の対応は、認証したOFFERのjob、freezeしたfull spec、full contract、導出room、payee署名、回答検証を合わせて判定する。
公式foldは通常の回答やreview本文を非frameとして除外する。署名不良や回答不正とは別の分類である。
実記録では回答がLOCKより先に投稿されていた。Pilot案では、検証済みLOCKとrail確認後にDeliveryする。

hash方式のREVEALで公開するのは**回答ではなく32-byte preimage**。
payeeがACCEPT作成前に生成し、ACCEPTにはhash statementだけを載せる。
生成・保持・復旧は呼出し側の責任で、公式MCPが持続保管してくれるわけではない。
Realでの保管設計は未実装・未許可。TESTではprocess内だけに生成し、TEST署名鍵をログ／成果物へ保存していない。
REVEALは秘密値の不可逆な公開なので、Task・contract・LOCK.ref・期限・承認対象を最後に照合する。
新規送信はrefを必ず含める案とする。旧tclk/1では省略可能で、今回の歴史的REVEAL／REFUNDも公式APIで処理できた。

`claimByMs < refundAfterMs`は構造条件。claimByMsは安全側のclaim目標で、foldは
`claimByMs < reveal.ts < refundAfterMs`の遅いREVEALも受け入れる。
refundAfterMsちょうどからREVEALは拒否、Payerのrefundが可能になる。clock差・Human応答・read／送信結果照合の余裕は別途必要。
「公式foldが通る最後の1 ms」を運用の締切にしない。

heartbeatはaccepted／locked中の当事者のstate-neutralなliveness表明。
Protocol全体では必須でないが、今回のGCD Task全文はACCEPT後のheartbeatによるdeal room作成を要求する。
頻度の一般規則は確認できていない。receiptはclaimed／refunded／cancelled後の当事者のackで、outcomeがterminalと一致する必要がある。
Payer以外にpayeeもreceiptを書ける。receipt自体は納品品質の検証ではなく、同じreceiptの反復もこのpinでは受理される。
receipt件数を成功件数へ加算しない。heartbeatも同様に状態を変えない。

locked後のcancelは不可。refundはPayerが期限後に行う操作で、payee側から代行しない。
相手無応答、timeout、予算切れは運用停止理由であり、cancelled／refundedを推測生成しない。
仕様と実装のこれらの境界を記録するだけとし、FreezeしたRead-only基盤の改修は行わなかった。

### 送信結果不明とnonce

署名preimageは公式helperによるroom・transport nonce・sweep後の正確な本文の組合せ。
同じ送信を識別する記録は、room、full DID、nonceの正確な十進文字列、signature、exact line、
full OFFER／contract ID、ローカル承認IDとbytes digest。seq、venue timestamp、generationは観測後に別途保存する。

**frame nonceとtransport nonceは別物。** ACCEPT内のframe nonceを変えるとcontractが変わり得る。
transport nonceだけを変えても同じframeのcontractは変わらないが、それを再送の安全性と同一視しない。
Venue仕様ではnonceは1〜19桁、同じDID・roomで直近nonceより大きい必要がある。
公式`nextNonce()`のprocess内millisecond増分だけで、既存Identityの過去nonceや別processとの整合を保証するとはしない。
特に過去に19桁nonceを使っている場合の検査・永続化・単一発行者制約は次の設計事項。

read-only reconciliationでは、正確なenvelopeの再取得→公式署名確認→指定contractのfold→rail Noteの独立確認を行える。
見つかれば「観測できた」と判断できるが、fold・rail・Delivery評価はさらに別である。
未観測だけでは未送信と断定できない。保持欠損、room epoch、read失敗、署名検証不能が残れば
**AMBIGUOUS_STOP**とし、別ACCEPT、新しいframe nonce、別候補を作らない。

Venueのnonce replay防止は最新約1 MiBの探索範囲に依存し、room全体の保持期間と同じではない。
同じ署名済みenvelopeさえ、尾部から消えると再受理され得る。ringは約10 MiB、圧迫時は縮小し、
通常7日無書込／単発roomは12時間等で消える。seqはroom再作成で再出発する。
従って「同じbytesなら必ず永久idempotent」「exportにないから送信されていない」は採用しない。
TESTではappend後の応答消失をmockし、完全一致を発見できる場合と、保持窓に無く停止する場合を検証した。Real送信試験は0。

## 3. 実公開Evidenceのreplay

Batch 15の30 page応答と成功した2 exportを優先利用。未完のsession-03 exportは修復せず除外した。
元Evidenceは無変更。元bytesのhash、抽出recordの出典一覧、原文・room・sender・nonce・signature・seq・元tsは別成果物に保持した。
署名対象のtextは再encodeせず、そのまま公式APIへ渡している。

合計23,809 unique records、seq **6934024–6960158**。union内の欠落は**2,326 seq**。
今回のunionは保持export全体も利用するため、Batch 15のadmitted recordsやTask cohortの件数と同じではない。
全23,809件のtransport署名が検証成功。公式frameとしてはOFFER 2,524、ACCEPT 2,218。
ACCEPTのうち先行する認証済みOFFER未取得93、guard拒否2（point paymentKey欠落、期限切れ各1）、
有効ACCEPT records 2,123／unique contracts 2,105。複数valid contractを持つOFFERは490。
残る非frame本文13,338と対応不能／不正frame 5,118を有効なtclk契約として数えない。
主な拒否はcontract等の必須field欠落3,308、非対応type 1,780、accept未知field 13等。
署名の真正性とProtocol適合性の違いを、この実記録でも確認した。

先着順は同一room・同一generationのseqを根拠にし、時間差はvenue tsを使う。
選んだOFFER→各ACCEPT間はそれぞれ39／92／25／453 recordsあり、seq欠落0。
下表の順位はこの保存範囲の**公式実装で有効なACCEPT**の順位で、全時間の普遍的な優先権ではない。
元JSONのgeneration／export headerとseqの根拠は保存応答にある。seq・tsは署名対象外で、venue/exportへの信頼が残る。
公式APIはRFC3339をmillisecondへ変換する。原文のmicrosecond tsは保持するが、fold判定は公式のmillisecond精度。
過去の期限判定へ今回の現在時刻を代入していない。

巨大な整数nonceはPython標準JSONで精度を落とさず解析し、同じ十進digitsの文字列として`transcriptRecord`へ渡した。
これは型のadapterであり、欠落値や失われたdigitsの推測ではない。
公式`parseTranscriptExport`は内部の通常`JSON.parse`に依存し、Batch 15 session-01 exportの**79行目**で
unsafe numeric nonceを拒否した。この直接API制約は未改修。追加取得した非空deal export 2つは直接APIでも解析成功。

追加public readは2 OFFER・4 contractsの4 exportと4 Noteに限定。2026-09-19 **13:31:06–13:32:49 UTC**、
計8 GET、200が6件（うち空export 2）、404 Noteが2件、成功本文**4,469 bytes**、retry 0。
事前上限8要求／2 MiB／3分内。逐次取得し成功後に1秒以上間隔を置いた。
最初の404でcollectorが停止した後、同じ計画内の未試行4 URLだけを取得した。404先は再取得していない。
say／say-signed／set／set-signed、本文中の署名付きURLを取得していない。

| OFFER seq / ACCEPT seq | contract先頭16 hex / 観測順位 | OFFERからACCEPT | deal記録・公式fold | 現在のPaper Note | 相手評価 |
| --- | --- | ---: | --- | --- | --- |
| 6946407 / 6946445 | `0b15d063176e7ac8` / 1 | 9.752秒 | 6 records、claimed、双方receipt有効 | JSON形式で公式decoder非対応。rail整合を認定しない | Payer署名の独立したreviewにPASS表明 |
| 6946407 / 6946498 | `220a059601afd9c0` / 2 | 17.018秒 | 空export、観測上accepted | 404 | 未観測 |
| 6948666 / 6948690 | `00d2d493c303559c` / 1 | 3.191秒 | 3 records、refunded、receipt未観測 | 公式tclkpaper1 refunded形式、termsと整合 | 評価未観測 |
| 6948666 / 6949118 | `9a90d1b9e9a7ba86` / 2 | 66.302秒 | 空export、観測上accepted | 404 | 未観測 |

full IDs、全steps、署名判定、URLとhashはPrivate原本 `real-pilot-protocol-batch16-20260919.json`（公開版では除外、追加summaryなし）に記録。
9 deal recordsは全署名が有効。OFFER／ACCEPT 8 recordsと合わせた17 steps中、14がProtocol上有効、
3は正しく署名された通常のDelivery／reviewで、非frameとしてstate-neutral。

claimed事例ではPayerのLOCKはroom seq 2、REVEAL 3、payee receipt 4、Payer receipt 5、Payer review 6。
reviewはreferenceとの一致をPayerが表明した証拠であり、こちらがreference全文を再検査した結果ではない。
これは**他者のHistorical collaboration**で、Projectが受注・納品して合格した実績へ加算しない。
現在のNoteはJSONのstatus／公開済みsecretだけで、公式PaperRecordに必要なlock／statement／期限を欠く。
署名済みtranscriptのclaimed、PayerのPASS表明、現在のrail形式不整合を別々に保持する。

GCD事例の納品は、今回の既存Solverと独立oracleの回答
`gcd=1 lcm=30662344343776909681828104055662`と一致。
しかしroom seq 3は期限後のPayer refundで、REVEAL・receipt・評価は取得記録に無い。
「回答が正しい」と「Protocol完了」「相手の合格」が同じでない実例である。
refunded Noteの現在の整合は確認できたが、過去LOCK時のNote snapshotを保有しているわけではない。

後続2契約はgeneration 0の空exportと404 Note。未作成、削除・保持期限、その他の欠損を現記録だけで識別できない。
先行OFFER不足93件は今回の取得範囲不足、session-03は保存済み部分応答、5,118件は形式不適合として別分類。
署名欠落／検証不能は今回採用した23,809＋9 recordsでは0だが、将来のrecordsを保証しない。
board上のREVEAL／receiptらしい記録をdeal roomへ移し替えて成功にする加工はしていない。

## 4. Offline Dry-runと検証

新規コードは研究専用の[Python runner](../tests/batch16_protocol.py)と[Node runner](../tests/batch16_protocol.mjs)のみ。
Python runnerの `check`／`seal` は、Privateに保存したJSON原本と内部の検証データを必要とします。公開版だけではHistorical検証を再現できません。
WorkerのSolver／Classifier／acceptance boundary、fetch基盤、Identity、Runtimeの送信機能は変更していない。

保存GCD `native-6948666`のFrozen bundleを既存`run_frozen`→Agent→Solverへ渡し、
既存`verify_gcd_lcm`の独立parser・Bezout certificate・積の関係・引用bindingで検証した。
そのanswerをTEST OFFERに紐付け、TEST OFFERのcontextにもbundle hashをcommitする。
TEST Payer／payeeのephemeral鍵をprocess内で生成し、公式signing helperで署名した。

流れはcandidate → Solve → independent verification → ACCEPT candidate → local TEST signing → official verification
→ MemoryNoteStore上のPaperRail lock／mock Payer LOCK → signed Delivery → TEST REVEAL → claimed／receipt／mock rail整合。
通常Deliveryはfold対象のframeではないため、署名と回答を独立検証する。
mock receiptはReal Counterparty acceptanceではなく、ProjectのReal acceptanceは0。

外部送信不能の根拠は、使用transportが`MemoryNoteStore`と配列へappendする`mockSend`だけであること、
live/client/serverをimportしないこと、fetch／HTTP(S)／TLS／socket／WebSocket／datagramの入口を例外にすること。
Python Solver中もsocketを禁止した。OS network namespaceによる隔離を実施したという主張ではない。
依存取得のnetwork phaseとdry-runは別コマンドで、dry-runはnetwork toolを呼ばない。

検証結果は **TEST 46項目PASS、公式golden vectors 3項目PASS、既存regression 107 tests PASS・skip 0**。
goldenは公式のvector値とtest bodyを保持し、vitestの最小assertion interfaceだけをNode assertへ適合した。
公式全vitest suiteの実行とは区別する。

negative／境界検証には署名改変、sender不一致、room署名bindingとProtocol room違反、
他contractのLOCK、非Payer LOCK、offered rail不一致、期限境界、LOCK前REVEAL、誤preimage／ref、
早いreceipt／誤outcome、ACCEPT／LOCK／REVEAL重複、複数ACCEPT混在、LOCK前cancel／LOCK後cancel拒否、
refund境界、PaperRailの誤ref／誤preimage／期限後claim／早いrefund、非対応JSON Note、送信応答消失を含む。
late REVEAL、acceptedの自動expire不在、terminal receiptの重複受理など、「拒否されない制約」も記録した。

実行中の誤りは、Solver wrapperのnull spec参照とTEST amount=0の2件。
それぞれ1回だけ修正して再実行し成功した。後者は公式wire制約を確認するnegative testにも追加した。
最初の40項目結果と追加後46項目結果は別ファイルに保持。実記録の成績をfixture成功で置換していない。

既存tracked filesとBatch 2〜15の**11,295ファイル**を開始保存hashと照合し、変更0。
公式checkoutもclean。今回新規のrunnerとReportだけをcommit対象にする。
既存regressionに加えて過去全Frozen成績を再計算したとは主張しない。

再実行はRepositoryルートで、既存のpin／依存／保存Evidenceがある状態から次のように行える。
出力名は未作成の名前にする。どちらもOfflineで、既存結果を上書きしない。

```bash
node tests/batch16_protocol.mjs dryrun dryrun-check.json
node tests/batch16_protocol.mjs replay real-replay-check.json
python3 -B -m unittest discover -s tests -v
```

初回作成順はPython `prepare`→明示public-readの`sources`／承認後`dependencies`→Node `build`／`analyze`
→Python `solve`／`select`→明示public-readの`rooms`（今回404後に未試行分`remaining`）→`lossless`
→Node `replay`／`dryrun`／`golden`／`limitations`→Python `regression`／`summarize`。
prepareや取得コマンドは既存出力があると停止する。再現のために今回のEvidenceを削除・上書きしない。
詳細な元bytes・signatures・TEST transcript・logは`.local/batch16/`にあり、Gitには原本を大量収録せずhash付きReportを置く。

## 5. Pilot推奨と次のHuman Gate Packet

**Humanが既に決めた方針:** One Identity／既存Project DIDをAnchor、WorkerとSigner／SecretのCapability分離、
PaperRail／no-value中心、Read-only改善Freeze、Real操作前のHuman supervision。
既存DIDのSeed・Private Key・Wallet・credentialへのアクセス許可は含まれない。
今回DIDの秘密保管場所や実鍵は探索せず、Note更新・広告・委任設定もしていない。

**AI提案:** 第1候補familyはGCD/LCM。Nim・shortest pathも既存Capabilityと独立検証を通る完全なTaskなら候補だが、
まず出力形式と検証が小さいGCDを選ぶ。解けることだけでなく、PaperRail互換性、Payer選定・確認経路、
十分な期限、signed Delivery形式、heartbeat条件まで理解できる候補をHumanが1件選ぶ。
今回はclaimed事例が非対応Note、正解GCD事例がrefundedであり、「そのPayerなら成功する」とは推奨できない。

個別ACCEPT承認はProtocol上可能である。ただし今回の観測ではLOCK先が先着だった2事例しか得ておらず、
Humanの応答時間内に受注できる確率も、後続が選ばれる確率も不明。
次回は十分な時間があり、1件のHuman-supervised rehearsalを受け付ける条件が確認できるPayer／Taskを選ぶ案とする。
そのような候補が無ければ試行を止める。条件付き事前委任へ自動で切り替えない。

以下は**未承認の次回Real Gate案**。Batch 17Aはこの案のOffline設計までであり、この表でReal権限が付与されるわけではない。

| 項目 | AI提案する上限・条件 | Humanがこれから決めること |
| --- | --- | --- |
| 「1件」の単位 | Humanが固定した候補OFFER 1件、ACCEPT試行最大1、観測LOCK最大1、成立=有効LOCK、完了=claimed等を別計数 | この数え方を承認するか |
| Identity | Humanが提示・確認した既存Project DIDのみ。使い捨てReal DIDなし | public DIDと許可する隔離Signer境界の指定。Real鍵アクセスは別の明示許可 |
| 候補 | canonical paper、hash lock、明示no-value、known deterministic template、full spec/hash固定、Solver＋独立検証成功 | 対象full OFFER ID、Payer DID、Task、allowlisted read範囲 |
| ACCEPT | 最終frame bytes、statement、full contract、nonce、署名対象roomをHumanが個別承認。1回だけ | 具体的bytesの承認、承認失効時刻 |
| Heartbeat | Taskが要求する初回1件だけ、ACCEPTの認証済み観測後、対象deal room。本文とframe nonceも固定 | 本Taskに必要か、ACCEPT承認と同時に明示承認するか |
| LOCK待機 | ACCEPT観測後3分を上限。指定contract、Payer署名、room、rail/ref、期限を検証し、現在の公式PaperRail verifyLockも成功 | 3分上限と、未LOCK時の停止を受け入れるか |
| Delivery | 指定contractのLOCK検証後、独立検証済みのexact answerを署名付き1 message。Task/contract/room bindingを再確認 | ACCEPT時にexact Delivery bytesも条件付きで承認するか、別承認にするか |
| REVEAL | 同じcontract、同じstatementを開くpreimage、正しいLOCK.ref。Deliveryの署名済み観測、必要なTask評価条件、時間余裕を満たしたとき1件 | この条件下の1回REVEALを前もって承認するか。Taskが評価先行ならPayer評価まで必須 |
| 期限 | 承認時expiresまで5分以上、claimByまで15分以上、refundAfter−claimByは10分以上、refundAfterまで60分以内を候補条件にする | 数値はProtocol要件でも実測成功条件でもなく保守的な提案。Human応答時間を含めて決定 |
| 最終時刻guard | Delivery／REVEAL前にclaimByまで120秒以上。余裕不足は中止し、late REVEALへ進まない | claimBy前の安全余裕、clockの確認方法 |
| 実行・read予算 | active処理15分以内、reconciliationを含む観測はrefundAfter＋2分までかつ開始後62分以内、明示read合計60回以下・4 MiB以下・最大1回/10秒、429で停止 | 残予算を不明結果の照合へ確保する配分。daemon化しない |
| 公開する情報 | 承認済みDID、ACCEPT/frame、Task指定の回答、必要な初回heartbeat、条件成立後preimageのみ | ローカルpath、内部log、credential、他Task情報が含まれない最終Preview |
| 許可しない操作 | OFFER、LOCK、refund、receiptの自己発行、Note更新、広告、委任、別room・別DID・別候補への送信 | 追加操作が必要なら別Gate。例外を自動追加しない |

ACCEPTが未LOCK、timeout、送信結果不明、cancelledでも、**ACCEPT試行1枠は消費済み**として終了する。
LOCK0を理由に次候補へ自動再挑戦しない。未送信のまま条件不足になっても、このセッションの対象を勝手に差し替えない。
成立数と完了数は別とし、Protocol terminal・rail整合・Payer評価の4判定を最終記録へ必ず残す。

PaperRail Noteのclaimed更新を誰が担うかは**未解決のGate条件**。
公式`PaperRail.claim()`はNoteへのwriteを伴うので、REVEAL送信許可へ暗黙に含めない。
狭いPilotではPayer側が公式形式で処理し、その挙動を事前に確認できる候補を推奨する。
payeeにNote CASを要求する候補なら別の明示許可・設計が必要で、現在の範囲からは除外する。
現在のJSON形式Noteを勝手にtclkpaper1へ変換・上書きして解決しない。

Worst Caseは、公開DIDでの誤契約・誤回答・期限切れ・Payer不応答・正解でもrefund・preimageの早期公開・
保持不足で送信結果を確定できないこと。no-valueでもIdentityの評判、公開情報、作業時間は影響を受ける。
成功率やリスク確率の数値は作っていない。Real資産railや不明な支払約束が混ざれば候補を拒否する。

停止条件は署名／sender／room／contract／rail/ref不一致、資料変更、非対応frame/Note、
Task評価条件不明、独立検証不合格、時間／read予算不足、鍵境界異常、外部応答不明、必要な権限の欠如。
Recoveryは保存した最終bytesと承認、正確なnonce、署名、観測証拠を保全し、限定readで照合してHumanへ返すこと。
見つかった記録は再送せずreplayする。見つからないときは未送信と扱わず停止する。
未LOCKでcancelが必要なら別途Humanが署名付きcancel 1件の可否を判断する。LOCK後は勝手にcancelやPayer refundを行わない。

## 6. Batch 17Aに必要な最小残件

**Mandatory（Offline設計・TESTだけ）:** 既存DIDのpublic AnchorとHuman承認境界の定義、
承認したexact bytes・nonce・frame hash・Task/contractを改変不能に受け渡す仕様、
Workerから分離したTEST Signerの許可操作と期限・回数制約、
hash preimageの生成・保持・失敗時Recovery、transport nonceの永続化と単一発行者、
送信前保存・結果不明時の停止／read-only照合を設計する。
今回見つかったunsafe numeric nonceはlossless adapterを境界仕様へ明記し、Protocolを独自実装しない。
以上に対して次のHuman GateでOffline作業範囲を決める。Real鍵の設定・読み出しは17Aの前提にしない。

**Real GateまでにはMandatory:** 対象1件のPayer選定条件、Delivery／heartbeat／評価要件、
PaperRail Note形式とclaim更新者の確認、上表のHuman判断、時間余裕、観測予算、Signer権限の個別確定。
原文未入手のRoadmap／Auditは可能ならHumanが提示するが、今回明示された方針を確認済み原文に置き換えてはならない。

**後回し可能:** 後続ACCEPTのLOCK率や詳細統計、全Protocol／全Agent調査、
export安定化、gap recovery、fetch budget増加、24H、daemon、Autopilot、新Solver、LLM／CCW、
value rail／PTLC、独自互換parser、全公式test依存のinstall。
今回の問題を理由にRead-only基盤へ大改修を戻さない。

Batch 16の外部作用は許可された公開readのみ。Real ACCEPT／LOCK／Delivery／REVEAL／heartbeat／receipt、
Note・権限設定、Real Signer／Secret／Wallet利用、paid API、Production操作、push／PRは0。
**このReportとlocal commitで停止し、Real PilotおよびBatch 17を自動開始しない。**
