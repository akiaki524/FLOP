"""Ordering affects read allocation only; exploration and hard bounds survive."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tests'),str(ROOT/'src')]
from readonly_intake import FetchSchedule, SessionFetcher, intake_lane
from collaboration_agent.materials import BANNER, Resolver, request
from collaboration_agent.model import Invalid

def case(i,context): return {'id':f't-{i}','context':context}

class FetchSchedulingTests(unittest.TestCase):
    def test_visible_reference_rule_not_task_id_or_known_answer(self):
        for context in ('/kv/ns/key','https://technocore.chat/kv/ns/key','math | arbitrary ask | full spec: /kv/ns/key'):
            a=case(1,context); b={**a,'id':'unknown-task','job_id':'known-looking-answer','answer':'42'}
            self.assertEqual(intake_lane(a),'efficient')
            self.assertEqual(intake_lane(a),intake_lane(b))
        for context in ('inference | sort rows | full spec: /kv/ns/key',
                        'novel | compute | full spec: /kv/ns/key','math | inline only',
                        'math | ask | full spec: https://evil.invalid/key','/kv/p-private/key',
                        '/kv/ns/key?extra=1','','https://technocore.chat/llms.txt',None):
            self.assertEqual(intake_lane(case(1,context)),'explore')

    def test_two_lanes_cost_fair_and_fifo_not_family_exclusion(self):
        cases=[case(i,('/kv/ns/k' if i%2 else 'unknown | full spec: /kv/ns/k')+str(i)) for i in range(40)]
        schedule=FetchSchedule(cases); seen=[]
        while (entry:=schedule.take()) is not None:
            lane,c=entry; seen.append(c['id'])
            schedule.charge(lane,4 if lane=='efficient' else 1)
            if all(schedule.position[k]<len(schedule.pending[k]) for k in schedule.pending):
                self.assertLessEqual(abs(schedule.spent['efficient']-schedule.spent['explore']),4)
        self.assertEqual(len(seen),len(set(seen)))
        self.assertEqual(set(seen),{c['id'] for c in cases})
        for lane in schedule.pending:
            self.assertEqual([s for s in seen if s in {c['id'] for c in schedule.pending[lane]}],
                             [c['id'] for c in schedule.pending[lane]])
        self.assertEqual(seen[0],'t-0')  # exploration wins the tie

    def test_cache_hits_zero_cost_and_empty_lane_borrow_do_not_stall(self):
        cases=[case(i,'/kv/ns/key') for i in range(100)]
        schedule=FetchSchedule(cases); seen=[]
        while (entry:=schedule.take()) is not None:
            lane,c=entry; seen.append(c); schedule.charge(lane,0 if seen[:-1] else 1)
        self.assertEqual(len(seen),100)
        self.assertEqual(schedule.spent,{'efficient':1,'explore':0})
        with self.assertRaises(Invalid): schedule.charge('efficient',5)
        with self.assertRaises(Invalid): schedule.charge('efficient',True)

    def test_hard_128_get_limit_and_every_untried_task_still_resolved(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.local') as tmp:
            calls=[]
            def send(url,cap):
                calls.append(url)
                return 200,{'content-type':'text/plain'},(BANNER+'\n\nbad spec\n').encode(),None
            fetcher=SessionFetcher(Path(tmp)/'fetch',send=send)
            cases=[case(i,('/kv/ns/k' if i%2 else 'unknown | full spec: /kv/ns/k')+str(i)) for i in range(180)]
            schedule=FetchSchedule(cases); bundles=[]
            while (entry:=schedule.take()) is not None:
                lane,c=entry; before=fetcher.count
                bundles.append(Resolver(fetcher).resolve(request(c['id'],c['context'])))
                schedule.charge(lane,fetcher.count-before)
            self.assertEqual(len(calls),128)
            self.assertEqual(len(bundles),180)
            self.assertEqual(sum('FETCH_BUDGET_EXCEEDED' in b['errors'] for b in bundles),52)
            self.assertEqual(schedule.spent,{'efficient':64,'explore':64})
            self.assertTrue(all(b['completeness']=='INCOMPLETE' for b in bundles))

if __name__=='__main__': unittest.main()
