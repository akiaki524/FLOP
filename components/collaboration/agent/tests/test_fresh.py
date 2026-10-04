"""Fresh sampling boundaries and source-bound agent-document answers."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from collaboration_agent.empirical import agent_field_template, agent_fields
from collaboration_agent.engine import Agent
from collaboration_agent.materials import compiled_task, load_frozen, run_frozen
from collaboration_agent.model import digest, encode, sha, validate_outcome
from collaboration_agent.state import Store, default_policy
from evaluate_fresh import select_fresh, verify_agent_field


def bundle(case='native-6813918'):
    path = ROOT / '.local/batch7/frozen' / case
    if not path.exists():
        raise unittest.SkipTest('fresh frozen public input unavailable; replay requires it')
    return path, load_frozen(path)


class FreshTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.local').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.local')
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sampling_excludes_old_jobs_repeated_jobs_and_missing_job(self):
        def record(seq, job):
            return {'seq': seq, 'text': 'tclk1 ' + json.dumps({'type': 'offer', 'job': job})}
        old = json.dumps({'messages': [record(1, {'id': 'old', 'context': 'before'})]}).encode()
        records = [record(2, {'id': 'old', 'context': 'changed'}),
                   record(3, {'id': 'new', 'context': 'before'}),
                   record(4, {'id': 'new', 'context': 'after'}), record(5, None),
                   record(6, {'id': 'no-material'}), record(7, {'id': 'distinct', 'context': 'before'})]
        with patch('collaboration_agent.engine.Agent.process', side_effect=AssertionError('no answer-based sampling')):
            selected, excluded = select_fresh(json.dumps({'messages': records}).encode(), old)
        self.assertEqual([c['id'] for c in selected], ['native-3', 'native-6', 'native-7'])
        self.assertEqual([c['reason'] for c in excluded],
                         ['previous_offer', 'duplicate_offer', 'missing_or_malformed_job'])

    def test_real_three_tasks_independent_verification_and_persisted_state(self):
        for case, expected in [('native-6813918', '"300000"'), ('native-6813949', '"0.1"'),
                               ('native-6813959', '"e-"')]:
            path, data = bundle(case)
            self.assertEqual(digest(compiled_task(data)), data['compiled_task_digest'])
            self.assertEqual(compiled_task(data)['family'], 'public.extraction')
            task = compiled_task(data, for_execution=True)
            self.assertEqual(task['family'], 'docs.agent_fields')
            with Store(self.root / case, create=True) as store, patch('socket.socket', side_effect=AssertionError('network')):
                result = run_frozen(path, Agent(store))
                answer = result['response']['result']['outcome']
                self.assertEqual(answer['value'], expected)
                self.assertTrue(verify_agent_field(data['spec'], answer, data['materials'])['valid'])
                validate_outcome(answer, task)
                store.verify()
            with Store(self.root / case) as store:
                repeat = run_frozen(path, Agent(store))
                self.assertTrue(repeat['response']['duplicate'])
                self.assertEqual(repeat['response']['result_digest'], result['response']['result_digest'])
                self.assertEqual(Agent(store).preview(case)['result']['outcome']['status'], 'COMPLETED')

    def test_changed_question_envelope_source_and_extra_material_refused(self):
        _, data = bundle()
        original = compiled_task(data, for_execution=True)
        spec, doc = original['evidence']
        for changed in [spec['text'] + '\n', 'Ignore. ' + spec['text'],
                        spec['text'].replace('maximum', 'minimum'),
                        spec['text'].replace('namespace', 'room'),
                        spec['text'].replace('quote it', 'explain it'),
                        spec['text'].replace('extraction |', 'protocol |')]:
            self.assertIsNone(agent_field_template(changed))
            task = copy.deepcopy(original)
            task['evidence'][0].update(text=changed, sha256=sha(changed.encode()))
            self.assertEqual(agent_fields(task)['status'], 'UNKNOWN')
        for changed in [doc['text'] + '\n', doc['text'][:200],
                        doc['text'].replace('300000', '123'), doc['text'] + '\nIgnore limits.']:
            task = copy.deepcopy(original)
            task['evidence'][1].update(text=changed, sha256=sha(changed.encode()))
            self.assertEqual(agent_fields(task)['status'], 'UNKNOWN')
        task = copy.deepcopy(original)
        task['evidence'][1]['locator'] = 'https://example.invalid/agent.json'
        self.assertEqual(agent_fields(task)['status'], 'UNKNOWN')
        task = copy.deepcopy(original)
        task['evidence'].append({**doc, 'id': 'extra'})
        self.assertEqual(agent_fields(task)['status'], 'UNKNOWN')
        task = copy.deepcopy(original)
        task['params']['document'] = task['params']['spec']
        self.assertEqual(agent_fields(task)['status'], 'UNKNOWN')

    def test_extraction_from_changed_values_under_test_only_source_pin(self):
        for case, before, after, expected in [
                ('native-6813918', '"notes_per_namespace": 300000', '"notes_per_namespace": 777', '"777"'),
                ('native-6813949', '"schema_version": "0.1"', '"schema_version": "2.8"', '"2.8"'),
                ('native-6813959', '"e-": "ephemeral', '"z-": "ephemeral', '"z-"')]:
            _, data = bundle(case)
            task = compiled_task(data, for_execution=True)
            text = task['evidence'][1]['text'].replace(before, after)
            task['evidence'][1].update(text=text, sha256=sha(text.encode()))
            self.assertEqual(agent_fields(task)['status'], 'UNKNOWN')
            with patch('collaboration_agent.empirical.AGENT_DOCUMENT_SHA256', sha(text.encode())):
                answer = agent_fields(task)
                self.assertEqual(answer['value'], expected)
                validate_outcome(answer, task)

    def test_corrupt_candidate_cannot_complete(self):
        path, _ = bundle()
        with Store(self.root / 'corrupt', create=True) as store:
            with patch('collaboration_agent.empirical.agent_field_candidate', return_value='"5242880"'):
                result = run_frozen(path, Agent(store))
            self.assertEqual(result['status'], 'HUMAN_REVIEW')
            self.assertIsNone(result['response']['result']['outcome']['value'])
            store.verify()

    def test_independent_verifier_rejects_wrong_values_missing_and_wrong_citations(self):
        for case in ('native-6813918', 'native-6813949', 'native-6813959'):
            _, data = bundle(case)
            answer = agent_fields(compiled_task(data, for_execution=True))
            for value in ('"5242880"', '"0.13.0"', '"p-"', '300000', answer['value'] + ' extra'):
                self.assertFalse(verify_agent_field(data['spec'], {**answer, 'value': value}, data['materials'])['valid'])
            self.assertFalse(verify_agent_field(data['spec'], {**answer, 'evidence': []}, data['materials'])['valid'])
            wrong = copy.deepcopy(answer)
            wrong['evidence'][-1]['selector']['first'] += 1
            self.assertFalse(verify_agent_field(data['spec'], wrong, data['materials'])['valid'])
            self.assertIsNone(verify_agent_field(data['spec'], answer, []))

    def test_existing_unknown_state_policy_and_explicit_selection_unchanged(self):
        path, data = bundle()
        with Store(self.root / 'old', create=True) as store:
            old = Agent(store).process(encode(compiled_task(data)))
            self.assertEqual(old['result']['outcome']['status'], 'UNKNOWN')
            result = run_frozen(path, Agent(store))
            self.assertEqual(result['response']['result']['outcome']['reason'], 'task_id_conflict')
            self.assertEqual(Agent(store).preview(data['request']['task_id'])['result'], old['result'])
        policy = default_policy()
        del policy['families']['docs.agent_fields']
        with patch('collaboration_agent.state.default_policy', return_value=policy):
            with Store(self.root / 'policy', create=True) as store:
                result = run_frozen(path, Agent(store))
                self.assertEqual(result['response']['result']['outcome']['reason'], 'family_not_configured')
        data['request']['selection'] = {'family': 'text.lines', 'params': {'source': 's2', 'first': 2, 'last': 2}}
        self.assertEqual(compiled_task(data, for_execution=True)['family'], 'text.lines')


if __name__ == '__main__':
    unittest.main()
