"""Finite public-board intake, gap accounting and session-pooled Material reads.

No daemon, solver policy changes, arbitrary URLs, persistent cache or implicit retry.
Replay uses the existing frozen evaluator unchanged.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from collect_public import parse
from evaluate_fresh import select_fresh, locked_code
from collaboration_agent.material_fetch import Fetcher, allowed_url, transport, TIMEOUT, EXPORT_TIMEOUT
from collaboration_agent.materials import Resolver, freeze, request, root_reference
from collaboration_agent.model import Invalid, check, encode, sha

BOARD='https://technocore.chat/r/tclk-offers'
EXPORT=BOARD+'/export'
POLLS=6
INTERVAL=30
TOTAL_FETCHES=128
TOTAL_BYTES=8*1024*1024
PAGE_BYTES=2*1024*1024
EXPORT_BYTES=10*1024*1024
MAX_RECOVERY=600
MAX_RECORDS=POLLS*200+MAX_RECOVERY
SESSION_SECONDS=600
SCHEDULING_POLICY={
    'version':'observed-fetch-efficiency-v1',
    'efficient':'allowlisted direct Note without preview, or full-spec Note reference with math preview',
    'explore':'all other cases, including unknown patterns and invalid references',
    'allocation':'choose nonempty lane with fewer actual GETs; ties explore first; FIFO within lane',
    'atomic_task_max_gets':4,
    'empty_lane':'other lane borrows unused capacity',
    'scope':'ordering only; no solver selection or acceptance changes; no ID/answer lookup',
}

def intake_lane(case):
    """Use only visible request structure, never fetched bytes or known answers.

    Historical B12/B13 direct Notes and math previews had high completeness per
    read. Opaque Notes are NOT presumed solver-compatible; exploration remains.
    """
    context=case['context']
    if type(context) is not str:
        return 'explore'
    try:
        reference,preview=root_reference(context)
        if reference:
            _,kind=allowed_url(reference)
            if kind=='technocore_note' and (not preview or preview.startswith('math | ')):
                return 'efficient'
    except Invalid:
        pass
    return 'explore'

class FetchSchedule:
    """Finite two-lane worklist; fair by actual GET cost, not Task count.

    While both lanes have work their read-charge difference is at most one
    atomic task (four GETs). All cases, even after cutoff, still get a ledger.
    """
    def __init__(self, cases):
        self.pending={lane:[] for lane in ('efficient','explore')}
        for case in cases: self.pending[intake_lane(case)].append(case)
        self.position=dict.fromkeys(self.pending,0)
        self.spent=dict.fromkeys(self.pending,0)

    def take(self):
        ready=[lane for lane in self.pending if self.position[lane]<len(self.pending[lane])]
        if not ready: return None
        lane=min(ready,key=lambda lane:(self.spent[lane],lane!='explore'))
        case=self.pending[lane][self.position[lane]]
        self.position[lane]+=1
        return lane,case

    def charge(self, lane, actual_gets):
        check(lane in self.spent and type(actual_gets) is int and 0<=actual_gets<=4,'INVALID_SCHEDULE_CHARGE')
        self.spent[lane]+=actual_gets

def stamp(): return datetime.now(timezone.utc).isoformat()
def save(path, value):
    with path.open('xb') as stream: stream.write(encode(value))

def room_url(since=None):
    check(since is None or type(since) is int and 0 <= since < 10**16, 'INVALID_CURSOR')
    return BOARD+'?format=json&limit=200'+(f'&since={since}' if since is not None else '')

def page_records(raw, since=None):
    """Validate the *returned* range. since is a filter, NOT forward pagination."""
    check(len(raw)<=PAGE_BYTES,'INTAKE_PAGE_TOO_LARGE')
    doc=parse(raw)
    check(type(doc) is dict and doc.get('room')=='tclk-offers','INTAKE_ROOM_MISMATCH')
    check(type(doc.get('generation')) is int and doc['generation']>=0,'INTAKE_GENERATION_INVALID')
    rows=doc.get('messages')
    check(type(rows) is list and len(rows)<=200 and type(doc.get('count')) is int
          and doc['count']==len(rows),'INTAKE_COUNT_INVALID')
    check(all(type(r) is dict and type(r.get('seq')) is int and 0<=r['seq']<10**16
              and type(r.get('text')) is str and len(r['text'])<=4096 for r in rows),'INTAKE_RECORD_INVALID')
    seqs=[r['seq'] for r in rows]
    check(seqs==sorted(set(seqs)),'INTAKE_ORDER_INVALID')
    check(type(doc.get('last_seq')) is int and doc['last_seq']>=0,'INTAKE_RANGE_INVALID')
    check(doc.get('first_seq')==(seqs[0] if seqs else None)
          and (doc['last_seq']==seqs[-1] if seqs else doc['last_seq']==(since or 0)),'INTAKE_RANGE_INVALID')
    check(since is None or all(s>since for s in seqs),'INTAKE_CURSOR_IGNORED')
    return doc

def gaps(records):
    seqs=sorted(records)
    return [[a+1,b-1] for a,b in zip(seqs,seqs[1:]) if b>a+1]

def recover_export(raw, headers, generation, records, *, max_recovery=MAX_RECOVERY):
    """Only complete JSONL rows inside already observed gaps; never follow links.

    A partial/oversize export is not absence evidence. Only independently parsed,
    complete lines can fill exact missing seqs under the same generation.
    """
    check(len(raw)<=EXPORT_BYTES+1,'INTAKE_EXPORT_TOO_LARGE')
    check(headers.get('x-room-generation')==str(generation),'INTAKE_GENERATION_CHANGED')
    check(headers.get('content-type','').split(';')[0] in ('application/x-ndjson','application/jsonl'),
          'INTAKE_EXPORT_TYPE')
    ranges=gaps(records)
    missing=lambda seq:any(a<=seq<=b for a,b in ranges)
    recovered={}
    corrupt=0
    previous=None
    lines=raw.splitlines(keepends=True)
    check(len(lines)<=150000,'INTAKE_EXPORT_LINE_LIMIT')
    for line in lines:
        if not line.endswith(b'\n'):
            continue
        if len(line)>64000:
            corrupt+=1; continue
        try:
            row=parse(line)
        except (ValueError,UnicodeError,RecursionError):
            corrupt+=1; continue
        if not isinstance(row,dict) or type(row.get('seq')) is not int:
            corrupt+=1; continue
        seq=row['seq']
        check(previous is None or seq>previous,'INTAKE_EXPORT_ORDER_INVALID')
        previous=seq
        if seq in records:
            check(row==records[seq],'INTAKE_RECORD_CONFLICT')
        if missing(seq) and len(recovered)<max_recovery:
            check(type(row.get('text')) is str and len(row['text'])<=4096,'INTAKE_RECORD_INVALID')
            recovered[seq]=row
    return recovered, {'parsed_lines':len(lines),'corrupt_lines':corrupt,
                       'partial_final_line':bool(lines and not lines[-1].endswith(b'\n')),
                       'recovery_cap':max_recovery,'recovered':len(recovered)}

class SessionFetcher:
    """Pool 4 x existing 32-fetch/2-MiB blocks across the finite cohort.

    Exact URL cache (including failures) lives only in this session, <=128 entries.
    Existing per-task limits, URL policy and normalizer still apply unchanged.
    """
    def __init__(self, root, *, send=transport, max_fetches=TOTAL_FETCHES,
                 max_bytes=TOTAL_BYTES, stop_at=None, clock=time.monotonic):
        check(type(max_fetches) is int and 0<=max_fetches<=TOTAL_FETCHES,'INVALID_SESSION_BUDGET')
        check(type(max_bytes) is int and 0<max_bytes<=TOTAL_BYTES,'INVALID_SESSION_BUDGET')
        self.root=Path(root)
        check(self.root.absolute()==self.root.resolve(),'symlink_path')
        self.root.mkdir(exist_ok=False)
        self.send,self.clock=send,clock
        self.stop_at=stop_at if stop_at is not None else clock()+SESSION_SECONDS
        self.max_fetches,self.max_bytes=max_fetches,max_bytes
        self.count=self.total_bytes=self.cache_hits=0
        self.cache,self.owners={},{}
        self.shards=[]

    def fetch(self, reference, task_budget):
        url,_=allowed_url(reference)
        if url in self.cache:
            self.cache_hits+=1
            return dict(self.cache[url],cache_hit=True)
        check(self.clock()<self.stop_at,'SESSION_TIME_BUDGET_EXCEEDED')
        check(self.count<self.max_fetches and self.total_bytes<self.max_bytes,'FETCH_BUDGET_EXCEEDED')
        if not self.shards or self.shards[-1].count>=32 or self.shards[-1].total_bytes>=2097152:
            # Both the number of blocks AND global network bytes remain bounded.
            check(len(self.shards)<4,'FETCH_BUDGET_EXCEEDED')
            def bounded_send(target, cap):
                return self.send(target,min(cap,self.max_bytes-self.total_bytes-1))
            self.shards.append(Fetcher(self.root/f'block-{len(self.shards)+1:02d}',send=bounded_send))
        owner=self.shards[-1]
        result=owner.fetch(url,task_budget)
        self.count+=1
        self.total_bytes+=result['raw_bytes']
        self.cache[url]=result
        self.owners[url]=owner
        return result

    def raw(self, node):
        return self.owners[node['url']].raw(node)

    def summary(self):
        return {'fetches':self.count,'bytes':self.total_bytes,'cache_hits':self.cache_hits,
                'fetch_errors':dict(Counter(m['fetch_error'] for m in self.cache.values() if m['fetch_error'])),
                'blocks':[{'fetches':s.count,'bytes':s.total_bytes} for s in self.shards]}

def public_read(root, name, url, cap, *, export=False):
    started=time.monotonic()
    save(root/(name+'-attempt.json'),{'at':stamp(),'url':url,'method':'GET','cap':cap,
                                    'deadline_seconds':EXPORT_TIMEOUT if export else TIMEOUT})
    status,headers,raw,error=transport(url,cap,intake_export=export)
    with (root/(name+'.bin')).open('xb') as stream: stream.write(raw)
    result={'at':stamp(),'url':url,'http_status':status,'headers':headers,'raw_bytes':len(raw),
            'raw_sha256':sha(raw),'fetch_error':error,'blob':name+'.bin',
            'elapsed_seconds':time.monotonic()-started}
    save(root/(name+'-response.json'),result)
    return result,raw

def collect(root, started):
    records={}
    origins={}
    pages=[]
    cursor=generation=None
    for i in range(POLLS):
        due=started+i*INTERVAL
        if time.monotonic()<due: time.sleep(due-time.monotonic())
        name=f'page-{i+1:02d}'
        response,raw=public_read(root,name,room_url(cursor),PAGE_BYTES)
        entry={'page':i+1,'response':response,'requested_since':cursor,'accepted':False}
        pages.append(entry)
        if response['http_status']!=200 or response['fetch_error']:
            entry['error']=response['fetch_error'] or 'HTTP_FAILURE'; continue
        try:
            check(response['headers'].get('content-type','').split(';')[0]=='application/json','INTAKE_PAGE_TYPE')
            doc=page_records(raw,cursor)
            if generation is not None: check(generation==doc['generation'],'INTAKE_GENERATION_CHANGED')
            for index,row in enumerate(doc['messages']):
                check(row['seq'] not in records or records[row['seq']]==row,'INTAKE_RECORD_CONFLICT')
            generation=doc['generation']
            for index,row in enumerate(doc['messages']):
                records[row['seq']]=row
                origins[row['seq']]={'page':i+1,'record_index':index,'raw_sha256':sha(raw)}
            cursor=doc['last_seq']
            entry.update(accepted=True,count=doc['count'],first_seq=doc['first_seq'],last_seq=cursor,generation=generation)
        except (Invalid,ValueError,TypeError,KeyError) as exc:
            entry['error']=str(exc) if isinstance(exc,Invalid) else 'INTAKE_PAGE_INVALID'
            if entry['error'] in ('INTAKE_GENERATION_CHANGED','INTAKE_RECORD_CONFLICT'):
                break  # Never mix epochs or accept conflicting records.
        print(json.dumps({'page':i+1,'records':len(records),'gaps':gaps(records),'accepted':entry['accepted']}),flush=True)
    original_gaps=gaps(records)
    recovery={'attempted':False,'recovered':0}
    if original_gaps and len(pages)==POLLS and not any(p.get('error') in
            ('INTAKE_GENERATION_CHANGED','INTAKE_RECORD_CONFLICT') for p in pages):
        response,raw=public_read(root,'export',EXPORT,EXPORT_BYTES,export=True)
        recovery={'attempted':True,'response':response,'recovered':0}
        if response['http_status']==200 and response['fetch_error'] in (None,'TOO_LARGE','TIMEOUT','TRUNCATED_RESPONSE'):
            try:
                found,info=recover_export(raw,response['headers'],generation,records)
                recovery.update(info)
                for seq,row in found.items():
                    records[seq]=row
                    origins[seq]={'export':True,'raw_sha256':sha(raw),'seq':seq}
            except (Invalid,ValueError,TypeError) as exc:
                recovery['error']=str(exc) if isinstance(exc,Invalid) else 'INTAKE_EXPORT_INVALID'
    check(len(records)<=MAX_RECORDS,'INTAKE_RECORD_LIMIT')
    snapshot=root/'snapshot'
    snapshot.mkdir()
    raw=encode({'room':'tclk-offers','generation':generation,'messages':[records[k] for k in sorted(records)]})
    with (snapshot/'raw.json').open('xb') as stream: stream.write(raw)
    save(snapshot/'provenance.json',{'raw_sha256':sha(raw),'kind':'merged_observed_and_bounded_recovery',
                                   'source_status':'SOURCE_UNVERIFIED'})
    report={'pages':pages,'generation':generation,'records':len(records),'origins':origins,
            'gaps_before':original_gaps,'recovery':recovery,'gaps_after':gaps(records),
            'scope':'Only first observed seq through last observed seq; no coverage claim before/after.'}
    save(root/'intake.json',report)
    return raw

def acquire(root, raw, stop_at):
    old=(ROOT/'.local/batch2/snapshot/raw.json').read_bytes()
    selected,excluded=select_fresh(raw,old)
    save(root/'selection.json',{'cases':selected,'excluded':excluded,'old_snapshot_sha256':sha(old),'snapshot_sha256':sha(raw)})
    frozen=root/'frozen'; frozen.mkdir()
    fetcher=SessionFetcher(root/'acquisition',stop_at=stop_at)
    cases={}
    ledger=[]
    schedule=FetchSchedule(selected)
    while (entry:=schedule.take()) is not None:
        lane,case=entry
        before=fetcher.count
        bundle=Resolver(fetcher).resolve(request(case['id'],case['context'],origin=case['origin']))
        cases[case['id']]={**case,'bundle_sha256':freeze(bundle,fetcher,frozen/case['id'])}
        schedule.charge(lane,fetcher.count-before)
        deferred=any(e in ('FETCH_BUDGET_EXCEEDED','SESSION_TIME_BUDGET_EXCEEDED') for e in bundle['errors'])
        partial=any(m.get('status')=='RESOLVED' for m in bundle['materials'])
        ledger.append({'id':case['id'],'lane':lane,'scheduled_index':len(ledger),
            'new_fetches':fetcher.count-before,'errors':bundle['errors'],
            'state':('DEFERRED_PARTIAL' if deferred and partial else 'DEFERRED_UNATTEMPTED' if deferred
                     else 'MATERIAL_COMPLETE' if not bundle['errors'] else 'MATERIAL_UNAVAILABLE'),
            'http_responses':sum(m.get('http_status') is not None for m in bundle['materials']),
            'explicit_404':any(m.get('http_status')==404 for m in bundle['materials'])})
    locked_code(root)
    save(root/'material-ledger.json',ledger)
    # Frozen selection/replay retain original seq order; acquisition order is
    # explicit in the separate ledger and cannot silently alter the cohort.
    manifest={'frozen_at':stamp(),'cases':[cases[c['id']] for c in selected],
        'scheduling':{'policy':SCHEDULING_POLICY,'GET_by_lane':schedule.spent},
        'snapshot_sha256':sha(raw),'old_snapshot_sha256':sha(old),
        'selection_sha256':sha((root/'selection.json').read_bytes()),'lock_sha256':sha((root/'implementation-lock.json').read_bytes()),
        'source_status':'SOURCE_UNVERIFIED','network':fetcher.summary(),'intake_sha256':sha((root/'intake.json').read_bytes())}
    save(frozen/'manifest.json',manifest)
    print(json.dumps({'tasks':len(cases),'network':fetcher.summary(),
                      'ledger':dict(Counter(r['state'] for r in ledger))}),flush=True)

def run(root):
    check(root.absolute()==root.resolve(),'symlink_path')
    locked_code(root)
    started=time.monotonic()
    save(root/'session-plan.json',{'at':stamp(),'polls':POLLS,'interval_seconds':INTERVAL,
        'max_page_bytes':PAGE_BYTES,'max_export_fetches':1,'max_export_bytes':EXPORT_BYTES,
        'max_export_seconds':EXPORT_TIMEOUT,'ordinary_GET_seconds':TIMEOUT,
        'max_recovery_records':MAX_RECOVERY,'max_records':MAX_RECORDS,
        'material_fetches':TOTAL_FETCHES,'material_bytes':TOTAL_BYTES,
        'admission_deadline_seconds':SESSION_SECONDS,'max_inflight_overrun_seconds':15,
        'material_order':SCHEDULING_POLICY,'retry':False})
    raw=collect(root,started)
    acquire(root,raw,started+SESSION_SECONDS)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args()
    run(args.root)
