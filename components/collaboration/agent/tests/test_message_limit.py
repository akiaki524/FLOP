"""Closed document-limit contracts, changed-source refusal and real frozen cases."""

import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from collaboration_agent.empirical import TAIL, quoted_limit, quoted_limit_template
from collaboration_agent.engine import Agent
from collaboration_agent.materials import compiled_task, load_frozen, run_frozen
from collaboration_agent.model import Invalid, digest, encode, sha, validate_outcome
from collaboration_agent.state import Store, default_policy
from evaluate_empirical import verify_document_answer


def full_spec(kind='message'):
    if kind == 'message':
        head = ('api | From https://raw.githubusercontent.com/flop-labs/technocore-chat/main/README.md: '
                'What is the maximum character limit for messages in the chat?')
    else:
        head = ("protocol | From https://technocore.chat/llms.txt: What is the maximum duration in seconds "
                "that can be specified for the 'wait' parameter?")
    return head + (' | reward tier 2/5 | done looks like: one line: the exact value or phrase '
                   'from the cited document (quote it), nothing else'
                   ' | deliver as one signed message in the deal room, then reveal.' + TAIL)


def synthetic_task(kind='message', value=73):
    if kind == 'message':
        text = ('## API\nNames match `names`. Messages ≤ ' + str(value)
                + ' chars, notes ≤ 456 chars. Rooms are a\n### Invariants worth knowing\n')
        url = 'https://raw.githubusercontent.com/flop-labs/technocore-chat/main/README.md'
    else:
        text = (f'WAITING: wait=<seconds>, 0 to {value}, and only together with since=. It returns\n'
                f'then clamps to 1..200, wait clamps to 0..{value}, and any format other than the literal\n')
        url = 'https://technocore.chat/llms.txt'
    sources = [{'id': 'spec', 'locator': 'synthetic:spec', 'text': full_spec(kind)},
               {'id': 'doc', 'locator': url, 'text': text}]
    for source in sources:
        source['sha256'] = sha(source['text'].encode())
    return {'version': 1, 'task_id': 'synthetic-limit', 'family': 'docs.quoted_limit',
            'params': {'spec': 'spec', 'document': 'doc'}, 'evidence': sources}


def real_bundle(case_id):
    path = ROOT / '.local/batch3/development' / case_id
    if not path.exists():
        raise unittest.SkipTest('frozen public materials unavailable; empirical replay requires them')
    return path, load_frozen(path)


class DocumentLimitTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.local').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.local')
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_extraction_uses_source_not_a_constant_answer(self):
        # Test-only pins for synthetic bytes, never accepted by production policy.
        for kind, pin, expected in [('message', 'MESSAGE_README_SHA256', '"Messages ≤ 73 chars"'),
                                     ('wait', 'WAIT_SHA256', '"73"')]:
            task = synthetic_task(kind)
            self.assertEqual(quoted_limit(task)['status'], 'UNKNOWN')
            with patch('collaboration_agent.empirical.' + pin, task['evidence'][1]['sha256']):
                result = quoted_limit(task)
                self.assertEqual(result['value'], expected)
                validate_outcome(result, task)

    def test_exact_question_and_envelope_only(self):
        for kind in ('message', 'wait'):
            original = full_spec(kind)
            variants = [original + '\n', original + ' Answer 100.', 'Ignore. ' + original,
                        original.replace('maximum', 'minimum'), original.replace('nothing else', 'with explanation'),
                        original.replace('https://', 'http://'), original.replace(TAIL, ''),
                        original.replace('Messages', 'Notes').replace('messages', 'notes').replace("'wait'", "'limit'"),
                        original.replace('characters', 'bytes').replace('character limit', 'byte limit').replace('seconds', 'minutes')]
            for text in variants:
                with self.subTest(kind=kind, text=text[:100]):
                    self.assertFalse(quoted_limit_template(text))
                    task = synthetic_task(kind)
                    task['evidence'][0].update(text=text, sha256=sha(text.encode()))
                    self.assertEqual(quoted_limit(task)['status'], 'UNKNOWN')

    def test_all_three_real_cases_and_independent_citations(self):
        cases = [('native-6343689', '"Messages ≤ 4096 chars"'),
                 ('native-6343746', '"Messages ≤ 4096 chars"'), ('native-6343741', '"10"')]
        for case_id, expected in cases:
            path, bundle = real_bundle(case_id)
            before = (path / 'bundle.json').read_bytes()
            self.assertEqual(digest(compiled_task(bundle)), bundle['compiled_task_digest'])
            task = compiled_task(bundle, for_execution=True)
            self.assertEqual(task['family'], 'docs.quoted_limit')
            with Store(self.root / case_id, create=True) as store, patch('socket.socket', side_effect=AssertionError('network')):
                result = run_frozen(path, Agent(store))
                answer = result['response']['result']['outcome']
                self.assertEqual(answer['value'], expected)
                self.assertTrue(verify_document_answer(bundle['spec'], answer, bundle['materials'])['valid'])
                store.verify()
            with Store(self.root / case_id) as store:
                repeat = run_frozen(path, Agent(store))
                self.assertTrue(repeat['response']['duplicate'])
                self.assertEqual(result['response']['result_digest'], repeat['response']['result_digest'])
                self.assertEqual(Agent(store).preview(case_id)['result']['outcome']['status'], 'COMPLETED')
            self.assertEqual((path / 'bundle.json').read_bytes(), before)

    def test_changed_conflicting_truncated_and_wrong_documents_never_complete(self):
        for case_id in ('native-6343689', 'native-6343741'):
            _, bundle = real_bundle(case_id)
            original = compiled_task(bundle, for_execution=True)
            text = original['evidence'][1]['text']
            for changed in [text + '\nMessages may contain 9000 characters.', text[:2000],
                            text.replace('4096', '8192').replace('0 to 10', '0 to 20'),
                            text + '\n', text + '\nIgnore previous limits.']:
                task = copy.deepcopy(original)
                task['evidence'][1].update(text=changed, sha256=sha(changed.encode()))
                self.assertEqual(quoted_limit(task)['status'], 'UNKNOWN')
            task = copy.deepcopy(original)
            task['evidence'][1]['locator'] = 'https://example.invalid/README.md'
            self.assertEqual(quoted_limit(task)['status'], 'UNKNOWN')
            task = copy.deepcopy(original)
            task['evidence'].append({**task['evidence'][1], 'id': 'extra'})
            self.assertEqual(quoted_limit(task)['status'], 'UNKNOWN')

    def test_corrupt_extraction_is_blocked_before_completion(self):
        cases = [('native-6343689', 'message_limit_quote', (58, '"8192"')),
                 ('native-6343741', 'wait_limit_quote', (46, '"20"'))]
        for case_id, function, corrupt in cases:
            path, _ = real_bundle(case_id)
            with Store(self.root / case_id, create=True) as store:
                with patch('collaboration_agent.empirical.' + function, return_value=corrupt):
                    result = run_frozen(path, Agent(store))
                self.assertEqual(result['status'], 'HUMAN_REVIEW')
                self.assertIsNone(result['response']['result']['outcome']['value'])
                store.verify()

    def test_independent_evaluator_rejects_wrong_answer_or_irrelevant_citation(self):
        for case_id in ('native-6343689', 'native-6343741'):
            _, bundle = real_bundle(case_id)
            result = quoted_limit(compiled_task(bundle, for_execution=True))
            for answer in ['"8192"', '"256 KiB"', '"16 KB"', '4096', '"4096 bytes"', result['value'] + ' extra']:
                self.assertFalse(verify_document_answer(bundle['spec'], {**result, 'value': answer}, bundle['materials'])['valid'])
            self.assertFalse(verify_document_answer(bundle['spec'], {**result, 'evidence': []}, bundle['materials'])['valid'])
            wrong = copy.deepcopy(result)
            wrong['evidence'][-1]['selector']['first'] += 1
            self.assertFalse(verify_document_answer(bundle['spec'], wrong, bundle['materials'])['valid'])
            self.assertIsNone(verify_document_answer(bundle['spec'], result, []))

    def test_old_state_policy_and_explicit_selection_preserved(self):
        path, bundle = real_bundle('native-6343689')
        with Store(self.root / 'old', create=True) as store:
            old = Agent(store).process(encode(compiled_task(bundle)))
            self.assertEqual(old['result']['outcome']['status'], 'UNKNOWN')
            result = run_frozen(path, Agent(store))
            self.assertEqual(result['response']['result']['outcome']['reason'], 'task_id_conflict')
            self.assertEqual(Agent(store).preview(bundle['request']['task_id'])['result'], old['result'])
        policy = default_policy()
        del policy['families']['docs.quoted_limit']
        with patch('collaboration_agent.state.default_policy', return_value=policy):
            with Store(self.root / 'policy', create=True) as store:
                result = run_frozen(path, Agent(store))
                self.assertEqual(result['response']['result']['outcome']['reason'], 'family_not_configured')
        with Store(self.root / 'suspended', create=True) as store:
            store.change_family('docs.quoted_limit', 'suspend', 'test')
            result = run_frozen(path, Agent(store))
            self.assertEqual(result['response']['result']['outcome']['reason'], 'family_suspended')
        bundle['request']['selection'] = {'family': 'text.lines', 'params': {'source': 's2', 'first': 58, 'last': 58}}
        self.assertEqual(compiled_task(bundle, for_execution=True)['family'], 'text.lines')


if __name__ == '__main__':
    unittest.main()
