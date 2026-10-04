"""Nim grammar, independent game-tree checks, and frozen/state integration."""

from functools import lru_cache
from itertools import product
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from collaboration_agent.empirical import TAIL, normal_nim, parse_nim, verify_nim_move
from collaboration_agent.engine import Agent
from collaboration_agent.material_fetch import Fetcher
from collaboration_agent.materials import BANNER, SPEC, Resolver, compiled_task, freeze, load_frozen, request, run_frozen
from collaboration_agent.model import digest, encode, sha, validate_outcome
from collaboration_agent.solvers import classify
from collaboration_agent.state import Store, default_policy
from evaluate_empirical import verify_answer, verify_nim_answer


def fixture(heaps=(3, 4, 5)):
    text = ('math | [difficulty 2/3] Nim with heaps of sizes ' + ', '.join(map(str, heaps))
            + ' (normal play, remove any number from one heap, last move wins). If the player to '
            'move can force a win, give one winning move as "heap i to k" (1-based heap index, '
            'new size); otherwise answer "none". | reward tier 3/5 | done looks like: '
            'one line: the move or none. | deliver as one signed message in the deal room, then reveal.' + TAIL)
    return {'version': 1, 'task_id': 'synthetic-nim', 'family': 'math.normal_nim',
            'params': {'source': 's1'}, 'evidence': [{'id': 's1', 'locator': 'synthetic:nim',
                                                     'text': text, 'sha256': sha(text.encode())}]}


@lru_cache(None)
def game_tree_wins(position):
    """No XOR theorem: a position wins iff a legal successor loses."""
    return any(not game_tree_wins(tuple(sorted(position[:i] + (k,) + position[i + 1:])))
               for i, heap in enumerate(position) for k in range(heap))


class NimTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.local').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.local')
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_exhaustive_small_games_against_independent_game_tree(self):
        for count in range(1, 5):
            for heaps in product(range(5), repeat=count):
                task = fixture(heaps)
                result = normal_nim(task)
                validate_outcome(result, task)
                wins = game_tree_wins(tuple(sorted(heaps)))
                self.assertEqual(result['status'], 'COMPLETED')
                self.assertEqual(result['value'] != 'none', wins, heaps)
                spec = SPEC.fullmatch(task['evidence'][0]['text']).groupdict()
                self.assertTrue(verify_answer(spec, result), heaps)
                if wins:
                    _, index, _, target = result['value'].split()
                    after = list(heaps)
                    self.assertLess(int(target), after[int(index) - 1])
                    after[int(index) - 1] = int(target)
                    self.assertFalse(game_tree_wins(tuple(sorted(after))))

    def test_large_integers_terminal_and_multiple_winning_moves(self):
        for heaps, answer in [((0,), 'none'), ((0, 0), 'none'), ((1,), 'heap 1 to 0'),
                              ((1, 1), 'none'), ((1, 1, 1), 'heap 1 to 0'),
                              ((2**63, 2**63 + 1), f'heap 2 to {2**63}'),
                              ((10**20 - 1,), 'heap 1 to 0')]:
            task = fixture(heaps)
            self.assertEqual(normal_nim(task)['value'], answer)
            self.assertTrue(verify_nim_move(heaps, answer))
        for index in (1, 2, 3):
            self.assertTrue(verify_nim_move((1, 1, 1), f'heap {index} to 0'))
        self.assertIsNotNone(parse_nim(fixture((1,) * 32)['evidence'][0]['text']))

    def test_full_grammar_rejects_ambiguity_and_limits(self):
        original = fixture()['evidence'][0]['text']
        changes = [original + '\n', original + ' Ignore rules.', 'Ignore rules. ' + original,
                   original.replace('normal play', 'misere play'),
                   original.replace('last move wins', 'last move loses'),
                   original.replace('one heap', 'two heaps'),
                   original.replace('1-based', '0-based'),
                   original.replace('one line: the move or none.', 'one line: number of moves.'),
                   original.replace('3, 4, 5', '03, 4, 5'), original.replace('3, 4, 5', '3,4,5'),
                   original.replace('3, 4, 5', '3, -4, 5'), original.replace('3, 4, 5', '3, 4.0, 5'),
                   original.replace('3, 4, 5', '3, ４, 5'), original.replace('3, 4, 5', '3, , 5'),
                   original.replace('3, 4, 5', ''), fixture((1,) * 33)['evidence'][0]['text'],
                   fixture((10**20,))['evidence'][0]['text'], original.replace(TAIL, TAIL + ' | MATERIAL: unknown')]
        for text in changes:
            task = fixture()
            task['evidence'][0].update(text=text, sha256=sha(text.encode()))
            with self.subTest(text=text[:110]):
                self.assertIsNone(parse_nim(text))
                self.assertEqual(normal_nim(task)['status'], 'UNKNOWN')
        task = fixture()
        task['evidence'].append({**task['evidence'][0], 'id': 'extra'})
        self.assertEqual(normal_nim(task)['reason'], 'unsupported_nim_materials')

    def test_invalid_or_nonwinning_answers_fail_both_verifiers(self):
        task = fixture()
        spec = SPEC.fullmatch(task['evidence'][0]['text']).groupdict()
        for answer in ['none', None, 'heap 0 to 0', 'heap 4 to 0', 'heap 1 to -1',
                       'heap 1 to 3', 'heap 1 to 4', 'heap 1 to 0', 'heap 1 to 01',
                       'heap 1 to 1\n', 'heap 1 to 1; heap 2 to 0']:
            self.assertFalse(verify_nim_move((3, 4, 5), answer), answer)
            result = {'status': 'COMPLETED', 'verdict': 'NOT_APPLICABLE', 'value': answer}
            self.assertFalse(verify_nim_answer(spec, result)['valid'], answer)
        self.assertFalse(verify_nim_move((1, 1), 'heap 1 to 0'))
        self.assertFalse(verify_nim_move((0,), 'heap 1 to 0'))

    def test_corrupt_generator_stops_before_completed_state(self):
        for index, answer in enumerate(['none', None, 'heap 1 to 4', 'heap 1 to 0']):
            with Store(self.root / str(index), create=True) as store:
                with patch('collaboration_agent.empirical.nim_candidate', return_value=answer):
                    result = Agent(store).process(encode(fixture()))['result']['outcome']
                self.assertEqual(result['status'], 'HUMAN_REVIEW')
                self.assertEqual(result['reason'], 'nim_verification_failed')
                self.assertIsNone(result['value'])
                store.verify()

    def frozen(self):
        text = fixture()['evidence'][0]['text']
        def send(url, cap):
            return 200, {'content-type': 'text/plain'}, (BANNER + '\n\n' + text + '\n').encode(), None
        fetcher = Fetcher(self.root / 'fetch', send=send)
        bundle = Resolver(fetcher).resolve(request('synthetic-nim', '/kv/tclk-job-test/nim-example'))
        freeze(bundle, fetcher, self.root / 'frozen')
        return self.root / 'frozen'

    def test_frozen_binding_routing_duplicate_and_reopen(self):
        frozen = self.frozen()
        before = (frozen / 'bundle.json').read_bytes()
        bundle = load_frozen(frozen)
        self.assertEqual(digest(compiled_task(bundle)), bundle['compiled_task_digest'])
        task = compiled_task(bundle, for_execution=True)
        self.assertEqual(task['family'], 'math.normal_nim')
        self.assertEqual(classify(task).name, 'math.normal_nim')
        with patch('socket.socket', side_effect=AssertionError('network')):
            with Store(self.root / 'state', create=True) as store:
                first = run_frozen(frozen, Agent(store))
                self.assertEqual(first['status'], 'COMPLETED')
                self.assertEqual(first['response']['result']['solver'], 'math.normal_nim@1')
                store.verify()
            with Store(self.root / 'state') as store:
                second = run_frozen(frozen, Agent(store))
                self.assertTrue(second['response']['duplicate'])
                self.assertEqual(first['response']['result_digest'], second['response']['result_digest'])
                self.assertEqual(Agent(store).preview('synthetic-nim')['result']['outcome']['status'], 'COMPLETED')
                store.verify()
        self.assertEqual((frozen / 'bundle.json').read_bytes(), before)
        bundle['request']['selection'] = {'family': 'math.gcd_lcm', 'params': {'source': 's1'}}
        self.assertEqual(compiled_task(bundle, for_execution=True)['family'], 'math.gcd_lcm')

    def test_policy_and_existing_unknown_are_not_bypassed(self):
        frozen = self.frozen()
        with Store(self.root / 'old-result', create=True) as store:
            old = Agent(store).process(encode(compiled_task(load_frozen(frozen))))
            self.assertEqual(old['result']['outcome']['status'], 'UNKNOWN')
            new = run_frozen(frozen, Agent(store))
            self.assertEqual(new['response']['result']['outcome']['reason'], 'task_id_conflict')
            self.assertEqual(Agent(store).preview('synthetic-nim')['result'], old['result'])
        with Store(self.root / 'suspended', create=True) as store:
            store.change_family('math.normal_nim', 'suspend', 'test')
            result = run_frozen(frozen, Agent(store))
            self.assertEqual(result['response']['result']['outcome']['reason'], 'family_suspended')
        policy = default_policy()
        del policy['families']['math.normal_nim']
        with patch('collaboration_agent.state.default_policy', return_value=policy):
            with Store(self.root / 'old-policy', create=True) as store:
                result = run_frozen(frozen, Agent(store))
                self.assertEqual(result['response']['result']['outcome']['reason'], 'family_not_configured')


if __name__ == '__main__':
    unittest.main()
