"""Batch 6: original frozen offers, independent answer checks, fresh offline replays.

The Batch 3 evaluator's completion-is-failure metric remains a historical contract.
Scores reviewed validation, normal-play Nim and source-bound document limits.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from collaboration_agent.engine import Agent
from collaboration_agent.materials import compiled_task, load_frozen, run_frozen
from collaboration_agent.model import check, encode, sha, validate_outcome
from collaboration_agent.state import Store


# Manual classification of observed text, not routing or an answer lookup table.
TEMPLATES = {
    'native-6343689': 'docs.message_limit',
    'native-6343703': 'attest.signed_line',
    'native-6343713': 'validation.http_status_with_explanation',
    'native-6343727': 'validation.ordered_seq_pair',
    'native-6343730': 'validation.ordered_seq_pair',
    'native-6343741': 'docs.wait_limit',
    'native-6343746': 'docs.message_limit',
    'native-6343749': 'math.normal_nim',
    'native-6343753': 'protocol.transcript_fold',
    'native-6343759': 'tables.offer_lock_counts',
    'native-6343793': 'validation.nested_ambiguous',
    'native-6343883': 'tables.lock_count',
}


def oracle(spec):
    """Independent delimiter parser, never imports solver grammar or solver answers."""
    if not spec or spec['family'] != 'validation':
        return None
    if spec['done'] != 'one line: PASS or FAIL, then one sentence.':
        return None
    ask = spec['ask']
    prefix = ('Validate a deliverable. TASK that was posted: "From the note the table below '
              '(rows: seq | payer | amount | asset | proto | time): output the seq of the row '
              'with the earliest time and the seq of the row with the latest time, as '
              '"<earliest_seq> <latest_seq>" (ties: lower seq).". ')
    ref = 'REFERENCE ANSWER the task\'s author holds (private to you as validator): "'
    delim = '". DELIVERABLE submitted by a worker: "'
    end = ('". Does the deliverable give the reference answer (same values, order where order '
           'is asked, nothing invented)? Reply PASS or FAIL, then one sentence naming the '
           'exact match or the exact discrepancy.')
    if not ask.startswith(prefix + ref) or not ask.endswith(end):
        return None
    parts = ask[len(prefix + ref):-len(end)].split(delim)
    if len(parts) != 2:
        return None
    for pair in parts:
        tokens = pair.split(' ')
        if len(tokens) != 2 or any(not t.isascii() or not t.isdigit() or len(t) > 20
                                   or str(int(t)) != t for t in tokens):
            return None
    # Canonical strings compare identically iff the ordered integer values do.
    return {'reference': parts[0], 'delivered': parts[1],
            'decision': 'PASS' if parts[0] == parts[1] else 'FAIL'}


def verify_answer(spec, result, materials=None):
    if spec and spec['family'] == 'math':
        proof = verify_nim_answer(spec, result)
        return bool(proof and proof['valid'])
    if spec and spec['family'] in {'api', 'document', 'protocol'}:
        proof = verify_document_answer(spec, result, materials)
        return bool(proof and proof['valid'])
    expected = oracle(spec)
    if expected is None or result['status'] != 'COMPLETED':
        return False
    answer = result.get('value')
    if not isinstance(answer, str):
        return False
    if expected['decision'] == 'PASS':
        matched = re.fullmatch(r'PASS: The earliest and latest seq values match the reference in order \(([0-9]+ [0-9]+)\)\.', answer)
        return bool(matched and matched[1] == expected['reference'] and result['verdict'] == 'MATCH')
    matched = re.fullmatch(r'FAIL: Expected earliest/latest seq ([0-9]+ [0-9]+), but the deliverable gives ([0-9]+ [0-9]+)\.', answer)
    return bool(matched and matched[1] == expected['reference'] and matched[2] == expected['delivered']
                and result['verdict'] == 'MISMATCH')


def verify_nim_answer(spec, result):
    """Independent parser + binary-column verification; no solver imports or answers."""
    if not spec or spec['family'] != 'math' or spec['done'] != 'one line: the move or none.':
        return None
    intro = re.fullmatch(r'\[difficulty [1-3]/3\] (.*)', spec['ask'])
    if not intro:
        return None
    start = 'Nim with heaps of sizes '
    end = (' (normal play, remove any number from one heap, last move wins). If the player to '
           'move can force a win, give one winning move as "heap i to k" (1-based heap index, '
           'new size); otherwise answer "none".')
    ask = intro[1]
    if not ask.startswith(start) or not ask.endswith(end):
        return None
    tokens = ask[len(start):-len(end)].split(', ')
    if not 1 <= len(tokens) <= 32 or any(not t.isascii() or not t.isdigit() or len(t) > 20
                                         or str(int(t)) != t for t in tokens):
        return None
    heaps = list(map(int, tokens))

    def parity_value(position):
        width = max(1, max(position).bit_length())
        bits = [format(h, f'0{width}b') for h in position]
        return int(''.join(str(column.count('1') % 2) for column in zip(*bits)), 2)

    before = parity_value(heaps)
    answer = result.get('value')
    legal = False
    after = None
    if isinstance(answer, str):
        parts = answer.split(' ')
        if (len(parts) == 4 and parts[0] == 'heap' and parts[2] == 'to'
                and all(p.isascii() and p.isdigit() and len(p) <= 20 and str(int(p)) == p
                        for p in (parts[1], parts[3]))):
            index, target = int(parts[1]), int(parts[3])
            legal = 1 <= index <= len(heaps) and 0 <= target < heaps[index - 1]
            if legal:
                after = heaps.copy()
                after[index - 1] = target
    valid = (result.get('status') == 'COMPLETED' and result.get('verdict') == 'NOT_APPLICABLE'
             and ((answer == 'none' and before == 0)
                  or (legal and before != 0 and parity_value(after) == 0)))
    # Bounded enumeration adds an independent legal-successor cross-check for the real task.
    winners = None
    if sum(heaps) <= 512:
        winners = []
        for index, size in enumerate(heaps):
            for target in range(size):
                successor = heaps.copy()
                successor[index] = target
                if parity_value(successor) == 0:
                    winners.append(f'heap {index + 1} to {target}')
        valid = valid and (not winners if answer == 'none' else answer in winners)
    return {'heaps_before': heaps, 'answer': answer, 'legal_move': legal,
            'nim_sum_before': before, 'heaps_after': after,
            'nim_sum_after': parity_value(after) if after is not None else None,
            'enumerated_legal_moves': sum(heaps) if winners is not None else None,
            'winning_moves': winners, 'valid': bool(valid)}


def verify_document_answer(spec, result, materials):
    """Independent task mapping, delimiter extraction and literal citation checks."""
    if not spec or spec['done'] != 'one line: the exact value or phrase from the cited document (quote it), nothing else':
        return None
    readme = 'https://raw.githubusercontent.com/flop-labs/technocore-chat/main/README.md'
    message_questions = {
        ('api', f'From {readme}: What is the maximum character limit for messages in the chat?'),
        ('document', f'From {readme}: What is the maximum size in characters for a message body?'),
    }
    wait_question = ('protocol', "From https://technocore.chat/llms.txt: What is the maximum duration in seconds that can be specified for the 'wait' parameter?")
    question = (spec['family'], spec['ask'])
    if question in message_questions:
        kind, url, reviewed = 'message_characters', readme, '4fd57fb18f5f9a4c16771238f5e3d2c77b10c3592c8315da23101f30719e02b3'
    elif question == wait_question:
        kind, url, reviewed = 'wait_seconds', 'https://technocore.chat/llms.txt', '40e0bebabcc105a2931805b68e200f1d5fc212e32ca14501ac4d85d105a2bb54'
    else:
        return None
    if not materials or len(materials) != 2:
        return None
    documents = [m for m in materials if m.get('role') == 'cited_material' and m.get('url') == url]
    specs = [m for m in materials if m.get('role') == 'full_spec']
    if len(documents) != 1 or len(specs) != 1:
        return None
    document, full_spec = documents[0], specs[0]
    text = document.get('text')
    if not isinstance(text, str) or sha(text.encode()) != reviewed or document.get('normalized_sha256') != reviewed:
        return None
    rows = text.splitlines()
    if kind == 'message_characters':
        found = [(i, row) for i, row in enumerate(rows, 1) if row.startswith('Names match `')
                 and '. Messages ≤ ' in row and ', notes ≤ ' in row]
        if len(found) != 1:
            return None
        line, row = found[0]
        phrase = 'Messages ≤ ' + row.split('. Messages ≤ ', 1)[1].split(', notes ≤ ', 1)[0]
        expected = '"' + phrase + '"'
        evidence_lines = [line]
    else:
        found = [(i, row.removeprefix('WAITING: wait=<seconds>, 0 to ').split(',', 1)[0])
                 for i, row in enumerate(rows, 1) if row.startswith('WAITING: wait=<seconds>, 0 to ')]
        clamps = [(i, row.split('wait clamps to 0..', 1)[1].split(',', 1)[0])
                  for i, row in enumerate(rows, 1) if 'wait clamps to 0..' in row]
        if len(found) != 1 or len(clamps) != 1 or found[0][1] != clamps[0][1]:
            return None
        expected = '"' + found[0][1] + '"'
        evidence_lines = [found[0][0], clamps[0][0]]
    refs = [{'source_id': full_spec['id'], 'sha256': full_spec['normalized_sha256'],
             'locator': full_spec['url'], 'selector': {'kind': 'text'}, 'quote': full_spec['text']}]
    refs += [{'source_id': document['id'], 'sha256': reviewed, 'locator': url,
              'selector': {'kind': 'lines', 'first': line, 'last': line}, 'quote': rows[line - 1]}
             for line in evidence_lines]
    valid = (result.get('status') == 'COMPLETED' and result.get('verdict') == 'NOT_APPLICABLE'
             and result.get('value') == expected and result.get('evidence') == refs)
    return {'kind': kind, 'source_url': url, 'source_sha256': reviewed, 'lines': evidence_lines,
            'expected_answer': expected, 'valid': valid, 'scope': 'quoted frozen document, not live deployment'}


def save(path, value):
    raw = encode(value)
    with path.open('xb') as stream:
        stream.write(raw)
    return sha(raw)


def metrics(rows, baseline=False):
    results = [r['batch3'] if baseline else r['result'] for r in rows]
    return {'tasks': len(rows), 'material_complete': sum(r['material_complete'] for r in rows),
            'classifier_reached': sum(bool(r.get('agent_reached')) for r in results),
            'solver_reached': sum(bool(r.get('solver_reached')) for r in results),
            **dict(Counter({s: sum(r['status'] == s for r in results)
                            for s in ('COMPLETED', 'HUMAN_REVIEW', 'UNKNOWN')})),
            'false_complete': sum(r['result']['status'] == 'COMPLETED' and not r['verified'] for r in rows)
                              if not baseline else sum(r['status'] == 'COMPLETED' for r in results)}


def evaluate(output):
    output.mkdir(parents=True, exist_ok=False)
    dataset = ROOT / '.local/batch2/dataset'
    manifest = json.loads((dataset / 'manifest.json').read_bytes())
    raw = (ROOT / '.local/batch2/snapshot/raw.json').read_bytes()
    check(sha(raw) == manifest['snapshot']['raw_sha256'], 'ORIGINAL_OFFER_DIGEST_MISMATCH')
    messages = json.loads(raw)['messages']
    previous = json.loads((ROOT / 'docs/empirical-evaluation-batch5-20260919.json').read_bytes())
    previous_rows = {r['id']: r for r in previous['rows']}
    rows = []
    frozen_digests = {}
    # Any accidental network use during replay is a hard error.
    with patch('socket.socket', side_effect=AssertionError('offline replay network attempt')):
        for split in ('development', 'holdout'):
            data = (dataset / (split + '.json')).read_bytes()
            check(sha(data) == manifest[split + '_sha256'], 'DATASET_DIGEST_MISMATCH')
            cases = {c['id']: c for c in json.loads(data)['cases'] if c['category'] == 'native_task'}
            frozen = ROOT / '.local/batch3' / split
            fm = json.loads((frozen / 'manifest.json').read_bytes())
            check(fm['dataset_sha256'] == sha(data), 'DATASET_BINDING_MISMATCH')
            baseline = {r['id']: r for r in json.loads((frozen / 'evaluation/results.json').read_bytes())}
            check(set(cases) == {c['id'] for c in fm['cases']} == set(baseline), 'CASE_SET_MISMATCH')
            with Store(output / (split + '-state'), create=True) as store, Store(output / (split + '-repeat'), create=True) as repeat:
                agent, second = Agent(store), Agent(repeat)
                for case in fm['cases']:
                    case_id = case['id']
                    path = frozen / case_id
                    bundle_sha = sha((path / 'bundle.json').read_bytes())
                    check(bundle_sha == case['bundle_sha256'], 'FROZEN_DIGEST_MISMATCH')
                    frozen_digests[case_id] = bundle_sha
                    bundle = load_frozen(path)
                    original = cases[case_id]
                    origin = bundle['request']['origin']
                    check(all(origin[k] == v for k, v in original['provenance'].items()), 'OFFER_BINDING_MISMATCH')
                    record = messages[origin['record_index']]
                    offer_text = original['task']['evidence'][0]['text']
                    check(record['text'] == offer_text and record['seq'] == origin['seq']
                          and sha(offer_text.encode()) == origin['record_text_sha256'], 'OFFER_BINDING_MISMATCH')
                    offer = json.loads(offer_text[6:])
                    check(offer['type'] == 'offer' and offer['job'].get('context', '') == bundle['request']['context'], 'OFFER_CONTEXT_MISMATCH')
                    result = run_frozen(path, agent)
                    replay = run_frozen(path, second)
                    check(result == replay, 'REPLAY_MISMATCH')
                    answer = result.get('response', {}).get('result', {}).get('outcome')
                    verified = False
                    runtime_task = None
                    state = None
                    if result.get('agent_reached'):
                        task = compiled_task(bundle, for_execution=True)
                        runtime_task = {'family': task['family'], 'params': task['params'],
                                        'digest': result['response']['result']['task_digest'],
                                        'frozen_compiled_digest': bundle['compiled_task_digest']}
                        validate_outcome(answer, task)
                        duplicate = run_frozen(path, agent)
                        check(duplicate['response']['duplicate'] and duplicate['response']['result_digest']
                              == result['response']['result_digest'], 'DUPLICATE_MISMATCH')
                        preview = agent.preview(case_id)
                        save(output / (case_id + '-preview.json'), preview)
                        verified = verify_answer(bundle['spec'], answer, bundle['materials'])
                        record = store.db.execute('SELECT solver, result FROM runs WHERE fingerprint=?',
                                  (result['response']['result']['fingerprint'],)).fetchone()
                        check(record is not None and json.loads(record['result']) == result['response']['result'],
                              'FINAL_STATE_MISMATCH')
                        state = {'admitted': True, 'solver': record['solver'],
                                 'final_status': json.loads(record['result'])['outcome']['status']}
                    template = TEMPLATES.get(case_id, 'unresolved.no_reference')
                    if template == 'unresolved.no_reference':
                        check('MISSING_TASK_REFERENCE' in bundle['errors'], 'UNCLASSIFIED_CASE')
                    old = previous_rows[case_id]
                    check(old['bundle_sha256'] == bundle_sha, 'BATCH5_INPUT_CHANGED')
                    if template not in {'docs.message_limit', 'docs.wait_limit'}:
                        check(result == old['result'], 'BATCH5_RESULT_REGRESSION')
                    rows.append({'id': case_id, 'split': split, 'template': template,
                                 'job_id': offer['job']['id'], 'offer_sha256': origin['record_text_sha256'],
                                 'bundle_sha256': bundle_sha, 'context': bundle['request']['context'],
                                 'ask': (bundle['spec'] or {}).get('ask'),
                                 'done': (bundle['spec'] or {}).get('done'),
                                 'reference_state': case['classification'],
                                 'material_complete': bundle['completeness'] == 'COMPLETE_WITHIN_SCOPE',
                                 'materials': [{k: n.get(k) for k in ('id', 'reference', 'role', 'status', 'normalized_sha256', 'raw_sha256')}
                                               for n in bundle['materials']],
                                 'errors': bundle['errors'], 'batch3': baseline[case_id]['result'],
                                 'batch5': old['result'], 'runtime_task': runtime_task, 'state': state,
                                 'nim_verification': verify_nim_answer(bundle['spec'], answer or {}),
                                 'document_verification': verify_document_answer(bundle['spec'], answer or {}, bundle['materials']),
                                 'result': result, 'verified': verified, 'oracle': oracle(bundle['spec']),
                                 'replay_identical': result == replay,
                                 'reason': answer['reason'] if answer else result['reason']})
                save(output / (split + '-audit.json'), store.verify())
                save(output / (split + '-repeat-audit.json'), repeat.verify())
    check(len(rows) == 25, 'EXPECTED_25_TASKS')
    completed_families = sorted({r['runtime_task']['family'] for r in rows if r['verified']})
    summary = {'batch': 6, 'starting_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
               'source_status': 'SOURCE_UNVERIFIED', 'completion_scope': 'local answer; no submission or payer receipt',
               'holdout': 'original 20/5 split preserved; both splits inspected for classification before implementation; NOT unseen',
               'batch3': metrics(rows, True), 'batch4': previous['batch4'], 'batch5': previous['batch5'], 'batch6': metrics(rows),
               'completed_families': completed_families,
               'batch5_unchanged_results': sum(r['batch5'] == r['result'] for r in rows),
               'templates': {t: metrics([r for r in rows if r['template'] == t]) for t in sorted({r['template'] for r in rows})},
               'splits': {s: metrics([r for r in rows if r['split'] == s]) for s in ('development', 'holdout')},
               'network_requests': 0, 'external_writes': 0, 'frozen_bundle_digests': frozen_digests,
               'code': {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in sorted((ROOT / 'src/collaboration_agent').glob('*.py'))
                        + [Path(__file__).resolve(), ROOT / 'tests/test_empirical.py', ROOT / 'tests/test_nim.py', ROOT / 'tests/test_message_limit.py']},
               'results_sha256': save(output / 'results.json', rows), 'rows': rows}
    summary['passed'] = (summary['batch6']['COMPLETED'] >= 6 and len(completed_families) >= 3
                         and summary['batch6']['false_complete'] == 0
                         and summary['batch5_unchanged_results'] == 22)
    save(output / 'summary.json', summary)
    print(json.dumps({'output': str(output), 'batch5': summary['batch5'], 'batch6': summary['batch6'],
                      'completed_families': completed_families,
                      'passed': summary['passed']}, indent=2))
    check(summary['passed'], 'REAL_COMPLETION_GATE_FAILED')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    evaluate(parser.parse_args().output)
