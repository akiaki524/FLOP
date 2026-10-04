"""Batch 7: disjoint public offers, freeze first, independent offline scoring.

Acquire is an explicit bounded public GET operation. Replay never constructs a
Fetcher. Existing empirical answer verifiers supply correctness, not solver output.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from unittest.mock import patch

from collect_public import parse
from evaluate_empirical import (Agent, Store, check, compiled_task, load_frozen,
                                metrics, oracle, run_frozen, save, sha,
                                validate_outcome, verify_answer,
                                verify_document_answer, verify_nim_answer)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from collaboration_agent.material_fetch import Fetcher
from collaboration_agent.materials import Resolver, freeze, request
from verify_bounded import verify_bounded, verify_gcd_lcm


def verify_agent_field(spec, result, materials):
    """Independent JSON decoding, field semantics and exact citation verification."""
    if not spec or spec['family'] != 'extraction' or spec['done'] != (
            'one line: the exact value or phrase from the cited document (quote it), nothing else'):
        return None
    url = 'https://technocore.chat/.well-known/agent.json'
    reviewed = '05ee7a33a4c9e30b6b8a936f5c727274634a30dbbc4732d4820733a653236e48'
    questions = {'What is the maximum number of notes allowed per namespace?': 'notes',
                 'What is the schema_version?': 'schema', 'What is the prefix for ephemeral rooms?': 'ephemeral'}
    kinds = [kind for question, kind in questions.items() if spec['ask'] == f'From {url}: {question}']
    if len(kinds) != 1 or len(materials) != 2:
        return None
    docs = [m for m in materials if m.get('role') == 'cited_material' and m.get('url') == url]
    specs = [m for m in materials if m.get('role') == 'full_spec']
    if len(docs) != 1 or len(specs) != 1:
        return None
    doc, full_spec = docs[0], specs[0]
    if sha(doc['text'].encode()) != reviewed or doc['normalized_sha256'] != reviewed:
        return None
    data = json.loads(doc['text'])
    kind = kinds[0]
    if kind == 'notes':
        key, value = 'notes_per_namespace', data['limits']['notes_per_namespace']
    elif kind == 'schema':
        key, value = 'schema_version', data['schema_version']
    else:
        matches = [(k, v) for k, v in data['conventions']['room_classes'].items()
                   if v.split(' — ', 1)[0] == 'ephemeral']
        if len(matches) != 1:
            return None
        key, value = matches[0][0], matches[0][0]
    expected = json.dumps(str(value), ensure_ascii=False)
    lines = []
    for i, row in enumerate(doc['text'].splitlines(), 1):
        try:
            pair = json.loads('{' + row.strip().removesuffix(',') + '}')
        except ValueError:
            continue
        if list(pair) == [key]:
            lines.append((i, row))
    if len(lines) != 1:
        return None
    number, quote = lines[0]
    evidence = [{'source_id': full_spec['id'], 'sha256': full_spec['normalized_sha256'],
                 'locator': full_spec['url'], 'selector': {'kind': 'text'}, 'quote': full_spec['text']},
                {'source_id': doc['id'], 'sha256': reviewed, 'locator': url,
                 'selector': {'kind': 'lines', 'first': number, 'last': number}, 'quote': quote}]
    valid = (result.get('status') == 'COMPLETED' and result.get('verdict') == 'NOT_APPLICABLE'
             and result.get('value') == expected and result.get('evidence') == evidence)
    return {'kind': kind, 'expected_answer': expected, 'source_sha256': reviewed, 'line': number,
            'valid': valid, 'scope': 'quoted frozen source; not a current deployment guarantee'}


def locked_code(root):
    lock = json.loads((root / 'implementation-lock.json').read_bytes())
    check(all(sha((ROOT / p).read_bytes()) == h for p, h in lock['files'].items()),
          'PRE_BASELINE_IMPLEMENTATION_CHANGED')
    return lock


def offers(raw):
    for index, record in enumerate(parse(raw)['messages']):
        if record.get('text', '').startswith('tclk1 '):
            frame = parse(record['text'][6:].encode())
            if isinstance(frame, dict) and frame.get('type') == 'offer':
                yield index, record, frame


def select_fresh(raw, old_raw):
    """Exclude old job IDs, identical jobs/texts and within-sample repeats."""
    identity = lambda j: json.dumps(j, sort_keys=True, default=str)
    old = list(offers(old_raw))
    ids = {f['job']['id'] for _, _, f in old if f.get('job', {}).get('id')}
    jobs = {identity(f.get('job')) for _, _, f in old}
    texts = {r['text'] for _, r, _ in old}
    selected, excluded = [], []
    seen_ids, seen_jobs = set(), set()
    for index, record, frame in offers(raw):
        job = frame.get('job')
        if not isinstance(job, dict):
            excluded.append({'seq': record['seq'], 'reason': 'missing_or_malformed_job'})
            continue
        job_id, key = job.get('id'), identity(job)
        reason = ('previous_offer' if (job_id and job_id in ids) or key in jobs or record['text'] in texts
                  else 'duplicate_offer' if (job_id and job_id in seen_ids) or key in seen_jobs else None)
        if reason:
            excluded.append({'seq': record['seq'], 'reason': reason})
            continue
        seen_ids.add(job_id)
        seen_jobs.add(key)
        selected.append({'id': f"native-{record['seq']}", 'job_id': job_id,
                         'context': job.get('context', ''), 'origin': {
                             'record_index': index, 'seq': record['seq'], 'raw_sha256': sha(raw),
                             'record_text_sha256': sha(record['text'].encode())}})
    return selected, excluded


def acquire(root):
    locked_code(root)
    raw = (root / 'snapshot/raw.json').read_bytes()
    provenance = json.loads((root / 'snapshot/provenance.json').read_bytes())
    check(sha(raw) == provenance['raw_sha256'], 'SNAPSHOT_DIGEST_MISMATCH')
    old_raw = (ROOT / '.local/batch2/snapshot/raw.json').read_bytes()
    old_manifest = json.loads((ROOT / '.local/batch2/dataset/manifest.json').read_bytes())
    check(sha(old_raw) == old_manifest['snapshot']['raw_sha256'], 'OLD_SNAPSHOT_CHANGED')
    selected, excluded = select_fresh(raw, old_raw)
    save(root / 'selection.json', {'cases': selected, 'excluded': excluded,
                                 'old_snapshot_sha256': sha(old_raw),
                                 'snapshot_sha256': sha(raw)})
    frozen = root / 'frozen'
    frozen.mkdir(exist_ok=False)
    fetcher = Fetcher(root / 'acquisition')
    cases = []
    for case in selected:
        bundle = Resolver(fetcher).resolve(request(case['id'], case['context'], origin=case['origin']))
        cases.append({**case, 'bundle_sha256': freeze(bundle, fetcher, frozen / case['id'])})
    locked_code(root)
    manifest = {'frozen_at': datetime.now(timezone.utc).isoformat(), 'cases': cases,
                'snapshot_sha256': sha(raw), 'old_snapshot_sha256': sha(old_raw),
                'selection_sha256': sha((root / 'selection.json').read_bytes()),
                'lock_sha256': sha((root / 'implementation-lock.json').read_bytes()),
                'source_status': 'SOURCE_UNVERIFIED',
                'network': {'fetches': fetcher.count, 'bytes': fetcher.total_bytes,
                            'cache_hits': fetcher.cache_hits,
                            'fetch_errors': dict(Counter(m['fetch_error'] for m in fetcher.cache.values()
                                                         if m['fetch_error']))},
                'evaluator_sha256': sha(Path(__file__).read_bytes())}
    save(frozen / 'manifest.json', manifest)
    print(json.dumps({'frozen_tasks': len(cases), 'excluded': excluded, 'network': manifest['network']}))


def replay(root, output, baseline):
    if baseline:
        locked_code(root)
    manifest = json.loads((root / 'frozen/manifest.json').read_bytes())
    raw = (root / 'snapshot/raw.json').read_bytes()
    old_raw = (ROOT / '.local/batch2/snapshot/raw.json').read_bytes()
    check(sha(raw) == manifest['snapshot_sha256'] and sha(old_raw) == manifest['old_snapshot_sha256'],
          'SNAPSHOT_DIGEST_MISMATCH')
    check(sha((root / 'selection.json').read_bytes()) == manifest['selection_sha256']
          and sha((root / 'implementation-lock.json').read_bytes()) == manifest['lock_sha256'],
          'FREEZE_BINDING_MISMATCH')
    selected, _ = select_fresh(raw, old_raw)
    check(selected == [{k: c[k] for k in ('id', 'job_id', 'context', 'origin')}
                       for c in manifest['cases']], 'FRESH_SELECTION_CHANGED')
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    with patch('socket.socket', side_effect=AssertionError('offline replay network attempt')):
        with Store(output / 'state', create=True) as store, Store(output / 'repeat', create=True) as repeat:
            agent, second = Agent(store), Agent(repeat)
            for case in manifest['cases']:
                path = root / 'frozen' / case['id']
                check(sha((path / 'bundle.json').read_bytes()) == case['bundle_sha256'], 'BUNDLE_CHANGED')
                bundle = load_frozen(path)
                check(bundle['request']['context'] == case['context']
                      and bundle['request']['origin'] == case['origin'], 'OFFER_BINDING_MISMATCH')
                result = run_frozen(path, agent)
                check(result == run_frozen(path, second), 'REPLAY_MISMATCH')
                answer = result.get('response', {}).get('result', {}).get('outcome', {})
                task, state = None, None
                if result.get('agent_reached'):
                    task = compiled_task(bundle, for_execution=True)
                    validate_outcome(answer, task)
                    duplicate = run_frozen(path, agent)
                    check(duplicate['response']['duplicate'] and duplicate['response']['result_digest']
                          == result['response']['result_digest'], 'DUPLICATE_MISMATCH')
                    row = store.db.execute('SELECT solver,result FROM runs WHERE fingerprint=?',
                                           (result['response']['result']['fingerprint'],)).fetchone()
                    check(row is not None and json.loads(row['result']) == result['response']['result'],
                          'FINAL_STATE_MISMATCH')
                    state = {'solver': row['solver'], 'final_status': answer['status']}
                    save(output / (case['id'] + '-preview.json'), agent.preview(case['id']))
                spec = bundle['spec']
                field_proof = verify_agent_field(spec, answer, bundle['materials'])
                bounded_proof = verify_bounded(spec, answer, bundle['materials'])
                gcd_proof = verify_gcd_lcm(bundle, answer)
                rows.append({'id': case['id'], 'job_id': case['job_id'],
                             'bundle_sha256': case['bundle_sha256'], 'origin': case['origin'],
                             'ask': (spec or {}).get('ask'), 'done': (spec or {}).get('done'),
                             'source_family': (spec or {}).get('family', 'unresolved'),
                             'runtime_family': task['family'] if task else None,
                             'material_complete': bundle['completeness'] == 'COMPLETE_WITHIN_SCOPE',
                             'errors': bundle['errors'],
                             'materials': [{k: m.get(k) for k in ('id', 'url', 'role', 'status', 'normalized_sha256', 'raw_sha256')}
                                           for m in bundle['materials']],
                             'result': result, 'state': state, 'replay_identical': True,
                             'verified': verify_answer(spec, answer, bundle['materials'])
                                         or bool(field_proof and field_proof['valid'])
                                         or bool(bounded_proof and bounded_proof['valid'])
                                         or bool(gcd_proof and gcd_proof['valid']),
                             'gcd_lcm_verification': gcd_proof,
                             'bounded_verification': bounded_proof,
                             'agent_field_verification': field_proof,
                             'validation_verification': oracle(spec),
                             'nim_verification': verify_nim_answer(spec, answer),
                             'document_verification': verify_document_answer(spec, answer, bundle['materials'])})
            save(output / 'audit.json', store.verify())
            save(output / 'repeat-audit.json', repeat.verify())
    summary = {'batch': 7, 'baseline': baseline, 'at': datetime.now(timezone.utc).isoformat(),
               'source_status': 'SOURCE_UNVERIFIED', 'completion_scope': 'local answer only; no external action or receipt',
               'holdout': 'all fresh cases evaluated; no uninspected post-adaptation holdout claim',
               'snapshot_sha256': sha(raw), 'freeze_manifest_sha256': sha((root / 'frozen/manifest.json').read_bytes()),
               'implementation_lock_sha256': manifest['lock_sha256'],
               'code': {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in sorted((ROOT / 'src/collaboration_agent').glob('*.py'))},
               'evaluator_sha256': sha(Path(__file__).read_bytes()),
               'metrics': metrics(rows), 'source_families': dict(Counter(r['source_family'] for r in rows)),
               'completed_families': sorted({r['runtime_family'] for r in rows if r['verified']}),
               'replay_network_requests': 0, 'external_writes': 0,
               'results_sha256': save(output / 'results.json', rows), 'rows': rows}
    summary['passed'] = summary['metrics']['false_complete'] == 0
    save(output / 'summary.json', summary)
    print(json.dumps({k: summary[k] for k in ('baseline', 'metrics', 'completed_families', 'passed')}, indent=2))
    check(summary['passed'], 'FALSE_COMPLETE')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('acquire', 'replay'))
    parser.add_argument('--root', type=Path, default=ROOT / '.local/batch7')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    if args.command == 'acquire':
        acquire(args.root)
    else:
        parser.error('--output required for replay') if args.output is None else replay(args.root, args.output, args.baseline)
