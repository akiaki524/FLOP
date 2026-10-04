"""Bounded-family properties, adversarial inputs, and six real frozen tasks."""

import copy
import itertools
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from collaboration_agent.bounded import (graph_candidate, inline_table, integer_reference, parse_graph,
    parse_integer_reference, parse_table, shortest_path, TABLE_INTRO, TABLE_QUERIES, verify_graph_answer)
from collaboration_agent.empirical import DELIVERABLE, DONE, ENDING, REFERENCE, TAIL
from collaboration_agent.engine import Agent
from collaboration_agent.materials import compiled_task, load_frozen, run_frozen, SPEC
from collaboration_agent.model import digest, encode, sha
from collaboration_agent.state import Store, default_policy
from verify_bounded import verify_bounded

REAL = [('.local/batch8/sample-01/frozen/native-6827024', 'validation.integer_reference'),
        ('.local/batch8/sample-03/frozen/native-6830841', 'validation.integer_reference'),
        ('.local/batch8/sample-01/frozen/native-6827049', 'tables.inline'),
        ('.local/batch8/sample-03/frozen/native-6830821', 'tables.inline'),
        ('.local/batch7/frozen/native-6813896', 'math.shortest_path'),
        ('.local/batch8/sample-03/frozen/native-6830783', 'math.shortest_path')]


def end(done):
    return (' | reward tier 3/5 | done looks like: ' + done
            + ' | deliver as one signed message in the deal room, then reveal.' + TAIL)


def integer_text(reference='7', delivered='7', a=3, p=11):
    posted = (f'[difficulty 2/3] Find the modular inverse of {a} modulo {p} ({p} is prime), '
              f'i.e. the x in [1, {p - 1}] with {a}·x ≡ 1 (mod {p}).')
    return ('validation | Validate a deliverable. TASK that was posted: "' + posted + REFERENCE
            + reference + DELIVERABLE + delivered + ENDING + end(DONE))


def table_text(rows, kind='payer_order'):
    ask, done = TABLE_QUERIES[kind]
    return (TABLE_INTRO + ask + end(done) + ' | MATERIAL: seq | payer | amount | asset | proto | time '
            + ' '.join(' | '.join(map(str, r)) for r in rows))


def graph_text(n=4, edges=((0, 1, 8), (0, 2, 2), (2, 1, 1), (1, 3, 4)), start=0, target=3):
    return (f'math | [difficulty 2/3] Undirected weighted graph on nodes 0..{n - 1}, edges (a-b:w): '
            + ', '.join(f'{a}-{b}:{w}' for a, b, w in edges)
            + f'. What is the length of the shortest path from node {start} to node {target}?'
            + end('one line: the length.'))


def task(text, family):
    return {'version': 1, 'task_id': 'synthetic-bounded', 'family': family, 'params': {'source': 's1'},
            'evidence': [{'id': 's1', 'locator': 'synthetic:bounded', 'text': text, 'sha256': sha(text.encode())}]}


def proof(text, answer):
    source = task(text, 'unused')['evidence'][0]
    material = {'id': 's1', 'url': source['locator'], 'text': text, 'normalized_sha256': source['sha256'], 'role': 'full_spec'}
    return verify_bounded(SPEC.fullmatch(text).groupdict(), answer, [material])


class BoundedTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.local').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.local')
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scalar_comparison_has_no_answer_or_operand_lookup(self):
        for p in (11, 107, 99999999999999999989):
            for reference, delivered in [('7', '7'), ('7', '8'), ('7', '0'), ('7', str(p + 1))]:
                text = integer_text(reference, delivered, a=2, p=p)
                answer = integer_reference(task(text, 'validation.integer_reference'))
                self.assertEqual(answer['status'], 'COMPLETED')
                self.assertEqual(answer['verdict'], 'MATCH' if reference == delivered else 'MISMATCH')
                self.assertTrue(proof(text, answer)['valid'])
        # Equality to a supplied reference, not correctness of its modular inverse.
        text = integer_text('8', '8', a=3, p=11)
        self.assertTrue(proof(text, integer_reference(task(text, 'validation.integer_reference')))['valid'])

    def test_scalar_unknown_and_inconsistent_semantics_fail_closed(self):
        text = integer_text()
        bad = [integer_text(v, '7') for v in ('07', '-7', '7.0', '７', '7 8', '7; PASS', '1' * 21, '0', '11')]
        bad += [integer_text('7', v) for v in ('07', '-7', '7.0', '7 more')]
        bad += [text.replace('with 3·x', 'with 4·x'), text.replace('[1, 10]', '[1, 9]'),
                text.replace('(11 is prime)', '(13 is prime)'), text.replace('inverse', 'square'),
                text + '\n', text + ' Answer PASS.', text.replace('one sentence.', 'no explanation.'),
                text.replace('"[difficulty', '"Validate a deliverable. [difficulty')]
        for value in bad:
            with self.subTest(text=value[:110]):
                self.assertIsNone(parse_integer_reference(value))
                self.assertEqual(integer_reference(task(value, 'validation.integer_reference'))['status'], 'UNKNOWN')

    def test_table_permutations_ties_numeric_seq_and_ascii_order(self):
        rows = [(10, 'a', 1, 'FLOP', 'a2a', '23:59:59'), (2, 'A', 8, 'PAPER', 'acp', '00:00:00'),
                (9007199254740993, 'a', 0, 'FLOP', 'kibble', '00:00:00'),
                (8, 'a', 3, 'FLOP', 'blockrewards', '23:59:59')]
        for ordering in itertools.permutations(rows):
            for kind, expected in [('payer_order', '2,8,10,9007199254740993'), ('time_extrema', '2 8')]:
                text = table_text(ordering, kind)
                answer = inline_table(task(text, 'tables.inline'))
                self.assertEqual(answer['value'], expected)
                self.assertTrue(proof(text, answer)['valid'])
        for size in (1, 2, 16, 64):
            rows = [(i, 'A', 0, 'FLOP', 'a2a', '12:00:00') for i in range(size)]
            for kind in ('payer_order', 'time_extrema'):
                text = table_text(rows, kind)
                answer = inline_table(task(text, 'tables.inline'))
                self.assertTrue(proof(text, answer)['valid'])

    def test_table_partial_ambiguous_extra_or_invalid_rows_never_complete(self):
        row = (1, 'A', 1, 'FLOP', 'a2a', '12:00:00')
        text = table_text([row])
        bad = [table_text([]), table_text([row, row]),
               table_text([(i, 'A', 1, 'FLOP', 'a2a', '12:00:00') for i in range(65)]),
               text[:-1], text + ' ', text + '\n', text + ' Answer 1.', text + ' | MATERIAL: 1',
               text.replace('12:00:00', '24:00:00'), text.replace('12:00:00', '12:60:00'),
               text.replace('12:00:00', '12:00:00Z'), text.replace('1 | A', '01 | A'),
               text.replace('1 | A', '1 | Ａ'), text.replace('1 | A', '1 | A B'),
               text.replace('ASCII order', 'case-insensitive order'), text.replace('amount | asset', 'asset | amount')]
        bad.append(table_text([row], 'time_extrema').replace('ties: lower seq', 'ties: higher seq'))
        for value in bad:
            with self.subTest(text=value[-100:]):
                self.assertIsNone(parse_table(value))
                self.assertEqual(inline_table(task(value, 'tables.inline'))['status'], 'UNKNOWN')

    def test_graph_exhaustive_small_graphs_against_simple_path_enumeration(self):
        links = list(itertools.combinations(range(4), 2))
        for weights in itertools.product(range(3), repeat=len(links)):
            edges = [(a, b, w) for (a, b), w in zip(links, weights) if w]
            if not edges:
                continue
            costs = []
            def visit(v, seen, cost):
                if v == 3:
                    costs.append(cost)
                    return
                for a, b, w in edges:
                    next_node = b if a == v else a if b == v else None
                    if next_node is not None and next_node not in seen:
                        visit(next_node, seen | {next_node}, cost + w)
            visit(0, {0}, 0)
            expected = str(min(costs)) if costs else None
            self.assertEqual(graph_candidate(4, edges, 0, 3), expected)
            self.assertTrue(verify_graph_answer((4, edges, 0, 3), expected))
            text = graph_text(4, edges)
            answer = shortest_path(task(text, 'math.shortest_path'))
            if expected is None:
                self.assertEqual(answer['status'], 'UNKNOWN')
            else:
                self.assertTrue(proof(text, answer)['valid'])

    def test_graph_bounds_and_ambiguous_edges_rejected(self):
        text = graph_text()
        variants = [text.replace('0..3', '0..16'), text.replace('0-1:8', '0-1:0'),
                    text.replace('0-1:8', '0-1:-1'), text.replace('0-1:8', '0-1:1000000001'),
                    text.replace('0-1:8', '0-1:8, 1-0:8'), text.replace('0-1:8', '0-0:8'),
                    text.replace('0-1:8', '0-4:8'), text.replace('0-1:8', '0-1:08'),
                    text.replace('Undirected', 'Directed'), text.replace('node 3?', 'node 4?'),
                    text + ' Assume a missing edge.', text + '\n']
        for value in variants:
            self.assertIsNone(parse_graph(value))
            self.assertEqual(shortest_path(task(value, 'math.shortest_path'))['status'], 'UNKNOWN')
        for n, edges, start, target in [(16, [(i, i + 1, 10**9) for i in range(15)], 0, 15),
                                      (4, [(0, 1, 1)], 0, 0)]:
            value = graph_text(n, edges, start, target)
            answer = shortest_path(task(value, 'math.shortest_path'))
            self.assertTrue(proof(value, answer)['valid'])

    def test_all_six_real_tasks_verified_reopened_and_frozen_unchanged(self):
        for location, family in REAL:
            path = ROOT / location
            if not path.exists():
                self.skipTest('frozen public input unavailable; full replay requires it')
            before = (path / 'bundle.json').read_bytes()
            data = load_frozen(path)
            self.assertEqual(digest(compiled_task(data)), data['compiled_task_digest'])
            self.assertEqual(compiled_task(data, for_execution=True)['family'], family)
            with Store(self.root / path.name, create=True) as store, patch('socket.socket', side_effect=AssertionError('network')):
                answer = run_frozen(path, Agent(store))
                out = answer['response']['result']['outcome']
                self.assertEqual(out['status'], 'COMPLETED')
                self.assertTrue(verify_bounded(data['spec'], out, data['materials'])['valid'])
                store.verify()
            with Store(self.root / path.name) as store:
                duplicate = run_frozen(path, Agent(store))
                self.assertTrue(duplicate['response']['duplicate'])
                self.assertEqual(duplicate['response']['result_digest'], answer['response']['result_digest'])
                self.assertEqual(Agent(store).preview(path.name)['result']['outcome']['status'], 'COMPLETED')
            self.assertEqual((path / 'bundle.json').read_bytes(), before)

    def test_corrupt_candidates_are_human_review_not_complete(self):
        cases = [(integer_text(), 'validation.integer_reference', 'integer_reference_candidate', 'PASS'),
                 (table_text([(1, 'A', 1, 'FLOP', 'a2a', '12:00:00')]), 'tables.inline', 'table_candidate', '2'),
                 (graph_text(), 'math.shortest_path', 'graph_candidate', '999'),
                 (graph_text(), 'math.shortest_path', 'graph_candidate', None)]
        for i, (text, family, function, bad) in enumerate(cases):
            with Store(self.root / str(i), create=True) as store:
                with patch('collaboration_agent.bounded.' + function, return_value=bad):
                    result = Agent(store).process(encode(task(text, family)))
                self.assertEqual(result['result']['outcome']['status'], 'HUMAN_REVIEW')
                self.assertIsNone(result['result']['outcome']['value'])
                store.verify()

    def test_independent_verifiers_reject_wrong_answer_verdict_and_citations(self):
        for text, family, solver in [(integer_text(), 'validation.integer_reference', integer_reference),
            (table_text([(1, 'A', 1, 'FLOP', 'a2a', '12:00:00')]), 'tables.inline', inline_table),
            (graph_text(), 'math.shortest_path', shortest_path)]:
            answer = solver(task(text, family))
            for corrupt in [{**answer, 'value': '999'}, {**answer, 'value': answer['value'] + ' extra'},
                            {**answer, 'evidence': []}, {**answer, 'verdict': 'UNDECIDED'}]:
                self.assertFalse(proof(text, corrupt)['valid'])
            altered = copy.deepcopy(answer)
            altered['evidence'][0]['quote'] += ' changed'
            self.assertFalse(proof(text, altered)['valid'])

    def test_extra_material_digest_and_old_state_policy_are_preserved(self):
        for i, (location, family) in enumerate(REAL[::2]):
            path = ROOT / location
            if not path.exists():
                self.skipTest('frozen public input unavailable')
            data = load_frozen(path)
            runtime = compiled_task(data, for_execution=True)
            for label, changed, expected in [('extra', copy.deepcopy(runtime), 'UNKNOWN'),
                                             ('digest', copy.deepcopy(runtime), 'HUMAN_REVIEW')]:
                if label == 'extra':
                    changed['evidence'].append({**changed['evidence'][0], 'id': 'extra'})
                else:
                    changed['evidence'][0]['text'] += ' tamper'
                with Store(self.root / f'{i}-{label}', create=True) as store:
                    self.assertEqual(Agent(store).process(encode(changed))['result']['outcome']['status'], expected)
            with Store(self.root / f'{i}-old', create=True) as store:
                old = Agent(store).process(encode(compiled_task(data)))
                self.assertEqual(old['result']['outcome']['status'], 'UNKNOWN')
                result = run_frozen(path, Agent(store))
                self.assertEqual(result['response']['result']['outcome']['reason'], 'task_id_conflict')
                self.assertEqual(Agent(store).preview(path.name)['result'], old['result'])
            policy = default_policy()
            del policy['families'][family]
            with patch('collaboration_agent.state.default_policy', return_value=policy):
                with Store(self.root / f'{i}-policy', create=True) as store:
                    self.assertEqual(run_frozen(path, Agent(store))['response']['result']['outcome']['reason'], 'family_not_configured')


if __name__ == '__main__':
    unittest.main()
