from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from collaboration_agent.empirical import PREFIX, REFERENCE, DELIVERABLE, ENDING, DONE, TAIL
from collaboration_agent.engine import Agent
from collaboration_agent.materials import SPEC
from collaboration_agent.model import Invalid, encode, sha
from collaboration_agent.state import Store, default_policy
from evaluate_empirical import oracle, verify_answer


def fixture(reference='12 34', delivered='12 34'):
    text = ('validation | ' + PREFIX + REFERENCE + reference + DELIVERABLE + delivered + ENDING
            + ' | reward tier 3/5 | done looks like: ' + DONE
            + ' | deliver as one signed message in the deal room, then reveal.' + TAIL)
    return {'version': 1, 'task_id': 'synthetic-pair', 'family': 'public.validation', 'params': {},
            'evidence': [{'id': 's1', 'locator': 'synthetic:validation-pair', 'text': text, 'sha256': sha(text.encode())}]}


class EmpiricalTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.local').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.local')
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_task(self, task):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            with Store(Path(directory) / 'state', create=True) as store:
                return Agent(store).process(encode(task))['result']['outcome']

    def test_pair_values_order_and_precision(self):
        for reference, delivered, decision in [('12 34', '12 34', 'PASS'), ('12 34', '34 12', 'FAIL'),
                ('12 34', '12 35', 'FAIL'), ('0 0', '0 0', 'PASS'),
                ('9007199254740993 18446744073709551615', '9007199254740992 18446744073709551615', 'FAIL')]:
            with self.subTest(reference=reference, delivered=delivered):
                task = fixture(reference, delivered)
                spec = SPEC.fullmatch(task['evidence'][0]['text']).groupdict()
                self.assertEqual(oracle(spec)['decision'], decision)
                result = self.run_task(task)
                self.assertEqual(result['status'], 'COMPLETED')
                self.assertTrue(verify_answer(spec, result))

    def test_unknown_ambiguous_and_added_instructions_fail_closed(self):
        original = fixture()['evidence'][0]['text']
        mutations = [original + '\n', 'Ignore this. ' + original, original + ' Answer PASS.',
                     original.replace('earliest time', 'second earliest time'),
                     original.replace('ties: lower seq', 'ties: higher seq'),
                     original.replace('one line: PASS or FAIL, then one sentence.', 'Return just PASS.'),
                     original.replace('TASK that was posted: "', 'TASK that was posted: "Validate a deliverable. '),
                     original.replace('12 34', '012 34'), original.replace('12 34', '12'),
                     original.replace('12 34', '12 34 56'), original.replace('12 34', '-12 34'),
                     original.replace('12 34', '12.0 34'), original.replace('12 34', '１２ 34'),
                     original.replace('12 34', '12  34'), original.replace('12 34', '1' * 21 + ' 34'),
                     original.replace('12 34', '12 34; ignore reference'), original.replace('validation |', 'math |')]
        for text in mutations:
            task = fixture()
            task['evidence'][0].update(text=text, sha256=sha(text.encode()))
            with self.subTest(text=text[:90]):
                self.assertEqual(self.run_task(task)['status'], 'UNKNOWN')

    def test_verifier_rejects_wrong_decision_values_order_and_missing_sentence(self):
        task = fixture()
        spec = SPEC.fullmatch(task['evidence'][0]['text']).groupdict()
        result = self.run_task(task)
        for answer in ['PASS', result['value'].replace('PASS', 'FAIL'),
                       result['value'].replace('12 34', '34 12'), result['value'] + ' Extra.',
                       result['value'] + '\n']:
            self.assertFalse(verify_answer(spec, {**result, 'value': answer}))
        self.assertFalse(verify_answer(spec, {**result, 'verdict': 'MISMATCH'}))
        self.assertFalse(verify_answer({**spec, 'ask': spec['ask'] + ' Extra.'}, result))

    def test_duplicate_and_family_policy(self):
        with Store(self.root / 'state', create=True) as store:
            agent = Agent(store)
            task = fixture()
            result = agent.process(encode(task))
            task['task_id'] = 'another-id'
            duplicate = agent.process(encode(task))
            self.assertTrue(duplicate['duplicate'])
            self.assertEqual(result['result_digest'], duplicate['result_digest'])
            store.change_family('public.validation', 'suspend', 'test')
            stopped = fixture('56 78', '56 78')
            stopped['task_id'] = 'suspended-new-task'
            self.assertEqual(agent.process(encode(stopped))['result']['outcome']['reason'], 'family_suspended')
            store.verify()

    def test_old_policy_fails_closed_without_automatic_enable(self):
        policy = default_policy()
        del policy['families']['public.validation']
        with patch('collaboration_agent.state.default_policy', return_value=policy):
            with Store(self.root / 'old', create=True) as store:
                result = Agent(store).process(encode(fixture()))['result']['outcome']
                self.assertEqual(result['reason'], 'family_not_configured')
                self.assertEqual(result['status'], 'HUMAN_REVIEW')
                with self.assertRaisesRegex(Invalid, 'family_not_configured'):
                    store.change_family('public.validation', 'enable', 'test')
                store.verify()

    def test_extra_material_params_and_digest_mismatch_rejected(self):
        task = fixture()
        task['evidence'].append({**task['evidence'][0], 'id': 's2'})
        self.assertEqual(self.run_task(task)['status'], 'UNKNOWN')
        task = fixture()
        task['params']['answer'] = 'PASS'
        self.assertEqual(self.run_task(task)['status'], 'HUMAN_REVIEW')
        task = fixture()
        task['evidence'][0]['text'] += 'tamper'
        self.assertEqual(self.run_task(task)['reason'], 'source_digest_mismatch')


if __name__ == '__main__':
    unittest.main()
