"""Finite intake bounds and the exact Batch 12 exhaustion/gap mechanisms."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from collaboration_agent.material_fetch import Fetcher, allowed_url, transport
from collaboration_agent.materials import BANNER, Resolver, request, freeze, load_frozen
from collaboration_agent.model import Invalid, encode
from readonly_intake import (BOARD, EXPORT, EXPORT_BYTES, SessionFetcher, gaps, page_records,
                            recover_export, room_url, collect)

def rows(first,last):
    return [{'seq':i,'text':'inert','nonce':10**19+i} for i in range(first,last+1)]

def page(data,generation=1,since=None):
    return encode({'room':'tclk-offers','count':len(data),'generation':generation,
                   'first_seq':data[0]['seq'] if data else None,
                   'last_seq':data[-1]['seq'] if data else since or 0,'messages':data})

def headers(generation=1):
    return {'x-room-generation':str(generation),'content-type':'application/x-ndjson; charset=utf-8'}

def export(data): return b''.join(encode(r)+b'\n' for r in data)

class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.local')
        self.root=Path(self.tmp.name)
        self.calls=[]
        def send(url,cap):
            self.calls.append((url,cap))
            return 200,{'content-type':'text/plain'},b'safe',None
        self.send=send

    def tearDown(self): self.tmp.cleanup()

    def test_fixed_cursor_urls_and_unchanged_material_allowlist(self):
        self.assertEqual(room_url(12),BOARD+'?format=json&limit=200&since=12')
        for value in (-1,True,'12',10**16):
            with self.assertRaises(Invalid): room_url(value)
        with self.assertRaises(Invalid): allowed_url(EXPORT)
        with patch('socket.getaddrinfo',side_effect=AssertionError('must reject before DNS')):
            for url in (EXPORT+'/say/x/y','https://evil.invalid/r/tclk-offers/export',
                        'https://technocore.chat/r/p-private/export',EXPORT+'?since=1'):
                with self.assertRaises(Invalid): transport(url,100,intake_export=True)
            for cap in (0,True,EXPORT_BYTES+1):
                with self.assertRaises(Invalid): transport(EXPORT,cap,intake_export=True)

    def test_page_validation_and_lossless_large_nonce(self):
        data=page_records(page(rows(10,12)),9)
        self.assertEqual(data['messages'][0]['nonce'],10**19+10)
        self.assertEqual(page_records(page([],since=12),12)['last_seq'],12)
        original=json.loads(page(rows(10,12)))
        for key,value in [('room','other'),('count',2),('generation',True),('last_seq',999),('first_seq',9)]:
            with self.subTest(key=key),self.assertRaises(Invalid): page_records(encode({**original,key:value}),9)
        with self.assertRaises(Invalid): page_records(page(rows(10,12)),10)
        with self.assertRaises(Invalid): page_records(page(list(reversed(rows(10,12)))))
        with self.assertRaises(Invalid): page_records(page(rows(1,201)))

    def test_since_is_not_pagination_gap_and_bounded_recovery(self):
        # A new tail omits 4..6 even when since=3; repeating since cannot page backward.
        observed={r['seq']:r for r in rows(1,3)+page_records(page(rows(7,9)),3)['messages']}
        self.assertEqual(gaps(observed),[[4,6]])
        found,proof=recover_export(export(rows(1,20)),headers(),1,observed)
        self.assertEqual(sorted(found),[4,5,6])
        self.assertEqual(gaps({**observed,**found}),[])
        self.assertEqual(proof['recovered'],3)
        limited,_=recover_export(export(rows(1,20)),headers(),1,observed,max_recovery=2)
        self.assertEqual(sorted(limited),[4,5])
        self.assertEqual(gaps({**observed,**limited}),[[6,6]])

    def test_retention_partial_lines_generation_and_conflicts_are_not_absence(self):
        observed={r['seq']:r for r in rows(1,3)+rows(7,9)}
        found,proof=recover_export(export(rows(6,9))+b'{"seq":10',headers(),1,observed)
        self.assertEqual(sorted(found),[6])
        self.assertTrue(proof['partial_final_line'])
        self.assertEqual(gaps({**observed,**found}),[[4,5]])
        with self.assertRaisesRegex(Invalid,'GENERATION'): recover_export(export(rows(1,9)),headers(2),1,observed)
        changed=rows(1,9); changed[1]['text']='changed'
        with self.assertRaisesRegex(Invalid,'CONFLICT'): recover_export(export(changed),headers(),1,observed)
        with self.assertRaisesRegex(Invalid,'ORDER'): recover_export(export(rows(1,3)+rows(2,9)),headers(),1,observed)

    def test_no_export_without_gap_and_epoch_change_stops_collection(self):
        def fake_read(root,name,url,cap,export=False):
            raw=page(rows(1,2)) if name=='page-01' else page(rows(3,4),generation=2)
            return {'http_status':200,'headers':{'content-type':'application/json'},'fetch_error':None},raw
        with patch('readonly_intake.public_read',side_effect=fake_read) as reader, patch('readonly_intake.INTERVAL',0):
            raw=collect(self.root,0)
        self.assertEqual(reader.call_count,2)
        self.assertEqual([r['seq'] for r in json.loads(raw)['messages']],[1,2])
        ledger=json.loads((self.root/'intake.json').read_bytes())
        self.assertFalse(ledger['recovery']['attempted'])
        self.assertEqual(ledger['pages'][-1]['error'],'INTAKE_GENERATION_CHANGED')

    def test_pool_recovers_stranded_per_poll_quota_without_increasing_total(self):
        # Four old per-poll budgets strand 31+22 slots before a 40-URL burst.
        workload=[1,10,40,1]
        legacy=[]
        index=0
        for poll,count in enumerate(workload):
            f=Fetcher(self.root/f'old-{poll}',send=self.send)
            for _ in range(count):
                index+=1
                try: f.fetch(f'/kv/ns/k-{index}',[0]); legacy.append(True)
                except Invalid: legacy.append(False)
        self.assertEqual(legacy.count(False),8)
        pooled=SessionFetcher(self.root/'pooled',send=self.send)
        for i in range(1,sum(workload)+1): pooled.fetch(f'/kv/ns/k-{i}',[0])
        self.assertEqual(pooled.count,52)
        self.assertEqual([s.count for s in pooled.shards],[32,20])

    def test_exact_url_cache_and_failures_are_session_scoped_no_retry(self):
        sends=[]
        def send(url,cap):
            sends.append(url)
            return None,{},b'','TIMEOUT'
        f=SessionFetcher(self.root/'pool',send=send)
        first=f.fetch('/kv/ns/key',[0]); again=f.fetch('/kv/ns/key',[0])
        self.assertEqual(first['fetch_error'],'TIMEOUT')
        self.assertTrue(again['cache_hit']); self.assertEqual(len(sends),1)
        self.assertEqual(f.summary()['fetch_errors'],{'TIMEOUT':1})
        other=SessionFetcher(self.root/'other',send=send)
        other.fetch('/kv/ns/key',[0]); self.assertEqual(len(sends),2)

    def test_global_count_bytes_deadline_and_per_task_caps(self):
        f=SessionFetcher(self.root/'pool',send=self.send)
        for i in range(128): f.fetch(f'/kv/ns/k-{i}',[0])
        with self.assertRaisesRegex(Invalid,'FETCH_BUDGET'): f.fetch('/kv/ns/k-129',[0])
        self.assertEqual(len(self.calls),128)
        per_task=[0]
        g=SessionFetcher(self.root/'task',send=self.send)
        for i in range(4): g.fetch(f'/kv/ns/k-{i}',per_task)
        with self.assertRaisesRegex(Invalid,'FETCH_BUDGET'): g.fetch('/kv/ns/k-4',per_task)
        tiny=SessionFetcher(self.root/'bytes',max_bytes=5,send=lambda u,c:(200,{},b'x'*(c+1),'TOO_LARGE'))
        tiny.fetch('/kv/ns/key',[0])
        with self.assertRaisesRegex(Invalid,'FETCH_BUDGET'): tiny.fetch('/kv/ns/other',[0])
        expired=SessionFetcher(self.root/'time',send=self.send,clock=lambda:10,stop_at=10)
        with self.assertRaisesRegex(Invalid,'SESSION_TIME'): expired.fetch('/kv/ns/key',[0])
        self.assertEqual(expired.count,0)

    def test_deferred_unattempted_partial_failure_and_404_stay_distinct(self):
        doc='document | From https://technocore.chat/llms.txt: What? | reward tier 1/5 | done looks like: one line. | deliver as one signed message in the deal room, then reveal.'
        wire=(BANNER+'\n\n'+doc+'\n').encode()
        f=SessionFetcher(self.root/'partial',max_fetches=1,send=lambda u,c:(200,{'content-type':'text/plain'},wire,None))
        partial=Resolver(f).resolve(request('partial','/kv/ns/key'))
        self.assertEqual(partial['fetches_for_task'],1)
        self.assertEqual(partial['errors'],['FETCH_BUDGET_EXCEEDED'])
        untouched=Resolver(f).resolve(request('unattempted','/kv/ns/other'))
        self.assertEqual(untouched['fetches_for_task'],0)
        self.assertEqual(untouched['errors'],['FETCH_BUDGET_EXCEEDED'])
        freeze(partial,f,self.root/'frozen')
        self.assertEqual(load_frozen(self.root/'frozen')['errors'],['FETCH_BUDGET_EXCEEDED'])
        for code,error,want in [(404,None,'MISSING'),(503,None,'HTTP_FAILURE'),(None,'DNS_FAILURE','DNS_FAILURE')]:
            fetcher=SessionFetcher(self.root/f'error-{want}',send=lambda u,c:(code,{},b'',error))
            self.assertEqual(Resolver(fetcher).resolve(request('bad','/kv/ns/key'))['errors'],[want])

if __name__=='__main__': unittest.main()
