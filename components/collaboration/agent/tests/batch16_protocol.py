"""Batch 16 evidence preparation. Public reads are explicit, bounded subcommands.

No signer credentials or environment secrets are read. Source artifacts stay separate
from historical evidence. The Node replay has only an in-memory transport.

Historical check/seal commands require the Private JSON original
(docs/real-pilot-protocol-batch16-20260919.json) and saved internal evidence.
The public snapshot excludes that original; these commands are not standalone
public verification. Retained synthetic unit tests and offline smokes are separate.
"""
import argparse
import ast
import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.local/batch16'
OFFICIAL = ROOT / '.local/research/tclk'
PIN = '5cc4ab93efbc8999a3a7e1471b639deca25998ea'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(name, value):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.write('\n')


def tracked_hashes():
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).split(b'\0')
    result = {p.decode(): sha((ROOT / p.decode()).read_bytes()) for p in paths if p}
    for number in range(2, 16):
        for p in sorted((ROOT / f'.local/batch{number}').rglob('*')):
            if p.is_file() and not p.is_symlink():
                result[str(p.relative_to(ROOT))] = sha(p.read_bytes())
    return result


def prepare():
    OUT.mkdir(exist_ok=False)
    save('baseline.json', {'at': datetime.now(timezone.utc).isoformat(),
         'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
         'status': subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT).decode(),
         'hashes': tracked_hashes()})
    board, sources, kinds = {}, [], Counter()
    for session in ('01', '02', '03'):
        parent = ROOT / f'.local/batch15/session-{session}'
        paths = sorted(parent.glob('page-*.bin'))
        if session != '03':
            paths.append(parent / 'export.bin')
        for path in paths:
            raw = path.read_bytes()
            origin = str(path.relative_to(ROOT))
            if path.name == 'export.bin':
                rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            else:
                parsed = json.loads(raw)
                rows = parsed['messages']
            sources.append({'path': origin, 'sha256': sha(raw), 'bytes': len(raw), 'records': len(rows)})
            for row in rows:
                seq = row['seq']
                if seq in board:
                    assert board[seq]['record'] == row, ('conflicting seq', seq)
                    board[seq]['origins'].append(origin)
                else:
                    board[seq] = {'record': row, 'origins': [origin]}
    records = []
    for seq, item in sorted(board.items()):
        row = dict(item['record'])
        # Python JSON integers retain exact decimal digits from the original bytes.
        # This adapter changes the JSON TYPE, never guesses or repairs nonce digits.
        if type(row.get('nonce')) is int:
            row['nonce'] = str(row['nonce'])
        row['_origins'] = item['origins']
        records.append(row)
        if row['text'].startswith('tclk1 '):
            try:
                kinds[json.loads(row['text'][6:]).get('type', 'missing')] += 1
            except ValueError:
                kinds['bad_json'] += 1
        else:
            kinds['non_frame'] += 1
    save('board.json', records)
    save('sources.json', {'sources': sources, 'counts': kinds, 'unique_records': len(records),
         'seq_min': min(board), 'seq_max': max(board),
         'missing_seq_in_union': max(board) - min(board) + 1 - len(board),
         'excluded': [{'path': '.local/batch15/session-03/export.bin',
                       'reason': 'historically reported truncated response; no repair attempted'}]})
    print(json.dumps({'sources': len(sources), 'unique_records': len(records), 'counts': kinds}))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('redirect forbidden')


def fetch_group(group, requests, byte_budget, seconds):
    """Fixed URLs supplied by code or validated official-derived room/note names."""
    start, total, ledger = time.monotonic(), 0, []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    directory = OUT / group
    directory.mkdir(exist_ok=False)
    for url, filename, limit in requests:
        if time.monotonic() - start >= seconds:
            break
        at, tick = datetime.now(timezone.utc).isoformat(), time.monotonic()
        entry = {'url': url, 'at': at, 'path': str((directory / filename).relative_to(ROOT))}
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'Collaboration-Agent-Batch16-readonly',
                                                          'Accept': '*/*'}, method='GET')
            with opener.open(request, timeout=min(15, seconds - (tick - start))) as response:
                chunks, size = [], 0
                while True:
                    if time.monotonic() - tick > 15 or time.monotonic() - start >= seconds:
                        raise TimeoutError('bounded read deadline')
                    chunk = response.read(min(65536, limit - size + 1))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    total += len(chunk)
                    if size > limit or total > byte_budget:
                        raise RuntimeError('bounded read bytes exceeded')
                data = b''.join(chunks)
                (directory / filename).write_bytes(data)
                entry.update(status=response.status, bytes=len(data), sha256=sha(data),
                             headers={k: response.headers.get(k) for k in
                                      ('Date', 'Content-Type', 'X-Room-Generation', 'Retry-After')})
        except Exception as error:
            entry.update(error=type(error).__name__ + ': ' + str(error))
        entry['elapsed_seconds'] = time.monotonic() - tick
        ledger.append(entry)
        if 'error' in entry:
            break  # no automatic retry, including partial-response cases
        time.sleep(1)
    save(group + '-ledger.json', {'requests': ledger, 'bytes': total,
         'limits': {'requests': len(requests), 'bytes': byte_budget, 'seconds': seconds, 'retries': 0}})
    print(json.dumps(ledger, ensure_ascii=True))


def sources():
    # Preserve a sandbox DNS failure separately; only one escalated attempt is allowed.
    group = 'official-retry' if (OUT / 'official').exists() else 'official'
    fetch_group(group, [
        ('https://api.github.com/repos/flop-labs/tclk/commits/main', 'main.json', 512000),
        (f'https://raw.githubusercontent.com/flop-labs/tclk/{PIN}/src/transcript.ts', 'transcript.ts', 128000),
        (f'https://raw.githubusercontent.com/flop-labs/tclk/{PIN}/SPEC.md', 'SPEC.md', 128000),
        (f'https://raw.githubusercontent.com/flop-labs/tclk/{PIN}/package.json', 'package.json', 32000),
        ('https://technocore.chat/llms.txt', 'llms.txt', 128000),
    ], 1000000, 120)


def dependencies():
    lock = (OFFICIAL / 'pnpm-lock.yaml').read_text()
    packages = [('@noble', 'curves'), ('@noble', 'hashes'), ('@scure', 'base')]
    fetch_group('dependencies', [
        (f'https://registry.npmjs.org/{scope}/{name}/-/{name}-2.4.0.tgz', name + '.tgz', 4000000)
        for scope, name in packages], 12000000, 120)
    manifest = []
    for scope, name in packages:
        data = (OUT / 'dependencies' / (name + '.tgz')).read_bytes()
        integrity = re.search(re.escape(f"'{scope}/{name}@2.4.0':")
                              + r'\n\s+resolution: \{integrity: (sha512-[^}]+)\}', lock)[1]
        actual = 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode()
        assert actual == integrity, 'package integrity mismatch'
        target = OUT / 'official-runtime/node_modules' / scope / name
        target.mkdir(parents=True, exist_ok=False)
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                parts = Path(member.name).parts
                assert parts[0] == 'package' and '..' not in parts
                path = target.joinpath(*parts[1:])
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('xb') as dest:
                    dest.write(archive.extractfile(member).read())
        manifest.append({'package': scope + '/' + name, 'version': '2.4.0', 'integrity': integrity})
    save('dependency-integrities.json', manifest)


def rooms(remaining=False):
    selected = json.loads((OUT / 'selected.json').read_text())
    if remaining:
        selected = selected[2:]
    requests = []
    for item in selected:
        room = item['room']
        assert re.fullmatch(r'mb-p-tclk-[0-9a-f]{16}', room)
        requests.append((f'https://technocore.chat/r/{room}/export', room + '.jsonl', 262144))
        note = item['paper_note']
        assert re.fullmatch(r'tclk-paper-[0-9a-f]{2}', note['ns'])
        assert re.fullmatch(r'[0-9a-f]{14}', note['key'])
        requests.append((f"https://technocore.chat/kv/{note['ns']}/{note['key']}", room + '.note', 32768))
    assert len(requests) <= 8
    # The remaining group is the four not-yet-attempted URLs from the same plan,
    # not a retry of the absent Note or an extension of the total 8-request budget.
    fetch_group('public-remaining' if remaining else 'public', requests, 2097152, 90 if remaining else 90)


def remaining():
    rooms(remaining=True)


def select():
    analysis = json.loads((OUT / 'board-analysis.json').read_text())
    # Deliberate representative selection, not an unbiased cohort or success estimate.
    group = next(g for g in analysis['groups'] if g['offer_seq'] == 6946407)
    picks = [{**a, 'offer_id': group['offer']['id'], 'offer_seq': group['offer_seq'],
              'reason': 'same offer, first two observed valid accepts, board activity hints only'}
             for a in group['accepts'][:2]]
    gcd = [g for g in analysis['groups'] if 'Compute gcd(' in g['offer'].get('job', {}).get('context', '')]
    exact = next((g for g in gcd if g['offer_seq'] == 6948666), None)
    chosen = exact or next(g for g in gcd if len(g['accepts']) > 1)
    for a in chosen['accepts'][:2]:
        picks.append({**a, 'offer_id': chosen['offer']['id'], 'offer_seq': chosen['offer_seq'],
                      'reason': 'saved deterministic GCD task representative'})
    save('selected.json', picks)
    print(json.dumps({'selected': picks, 'gcd_offer': chosen['offer'],
                      'rejections': analysis['rejected']}, indent=2))


def lossless():
    data, manifest = {}, []
    for path in sorted(OUT.glob('public*/*.jsonl')):
        raw = path.read_bytes()
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        converted = 0
        for row in rows:
            if type(row.get('nonce')) is int:
                row['nonce'] = str(row['nonce'])
                converted += 1
        data[path.stem] = rows
        manifest.append({'path': str(path.relative_to(ROOT)), 'sha256': sha(raw),
                         'records': len(rows), 'integer_nonce_to_exact_string': converted})
    save('public-lossless.json', data)
    save('public-lossless-provenance.json', manifest)
    print(json.dumps(manifest))


def regression():
    result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                            cwd=ROOT, capture_output=True, timeout=180)
    (OUT / 'regression.log').write_bytes(result.stdout + result.stderr)
    save('regression.json', {'command': 'python3 -B -m unittest discover -s tests -v',
         'returncode': result.returncode, 'log_sha256': sha(result.stdout + result.stderr)})
    print((result.stdout + result.stderr).decode()[-1000:])
    assert result.returncode == 0


def summarize():
    def load(name):
        return json.loads((OUT / name).read_text())
    baseline = load('baseline.json')
    changed = [path for path, expected in baseline['hashes'].items()
               if not (ROOT / path).is_file() or sha((ROOT / path).read_bytes()) != expected]
    assert not changed, ('historical artifacts changed', changed)
    assert subprocess.check_output(['git', 'status', '--porcelain'], cwd=OFFICIAL) == b''
    analysis, real, solver = load('board-analysis.json'), load('real-replay.json'), load('solver.json')
    assessments = []
    for item in real:
        state = item['state']
        deliveries = item['deliveries']
        payer_reviews = [r for r in deliveries if r['sender'] == state['payerDid']
                         and r['line'].startswith('review ') and r['verification']['ok']]
        worker_delivery = [r for r in deliveries if r['sender'] == state['payeeDid']
                           and r['verification']['ok']]
        assessments.append({
            'contract': item['contract'], 'room': item['room'], 'offer_id': item['offer_id'],
            'offer_seq': item['offer_seq'], 'accept_seq': item['seq'], 'observed_accept_rank': item['observed_rank'],
            'raw_sha256': item['raw_sha256'], 'records': item['records'],
            'verified_record_count': sum(r['ok'] for r in item['signature']),
            'direct_export_api': item['direct_export_api'], 'protocol_state': state['status'],
            'steps': item['steps'],
            'paper_assessment': ('CURRENT_RECORD_CONSISTENT_NOT_VALUE_PROOF' if item['paperConsistent'] else
                                 'UNSUPPORTED_CURRENT_NOTE_FORMAT' if item['records'] else 'NOT_OBSERVED_NOTE_404'),
            'paper_status': (item['paper'] or {}).get('status'),
            'signed_payer_review': [{'seq': r['seq'], 'text': r['line']} for r in payer_reviews],
            'signed_payee_delivery': [{'seq': r['seq'], 'text': r['line']} for r in worker_delivery],
            'matches_our_independently_verified_gcd_answer': any(
                r['line'] == solver['delivery_candidate'] for r in worker_delivery),
        })
    ledgers = {name: load(name + '-ledger.json') for name in
               ('official', 'official-retry', 'dependencies', 'public', 'public-remaining')}
    public_attempts = ledgers['public']['requests'] + ledgers['public-remaining']['requests']
    artifact_hashes = {}
    for path in sorted(OUT.rglob('*')):
        if path.is_file() and not path.is_symlink() and 'official-runtime' not in path.parts:
            artifact_hashes[str(path.relative_to(ROOT))] = sha(path.read_bytes())
    summary = {
        'batch': 16, 'at': datetime.now(timezone.utc).isoformat(),
        'start': {'head': baseline['head'], 'worktree': 'clean; measured before harness creation'},
        'before_commit_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
        'final_head_evidence': '.local/batch16/final-state.json (written after local commit; also in final user response)',
        'source_categories': {
            'directly_read_local': ['README.md', 'docs/design.md', 'docs/references.md',
                                   'docs/export-reliability-saturation-batch15-20260919.md',
                                   'official SPEC, core, signing helper, golden vectors, audit example'],
            'user_policy': 'Freeze, identity continuity, permission limits, post-B15 phase-gate decision',
            'unavailable_originals': ['Phase-Gate Audit', 'Roadmap', 'separate Project Policy/Handoff'],
            'unverified': ['Production tclk revision', 'payer general selection algorithm',
                           'later ACCEPT actually LOCKed', 'multiple LOCKs from same OFFER in real records']},
        'official_runtime': load('runtime-manifest.json'), 'dependencies': load('dependency-integrities.json'),
        'saved_sources': load('sources.json'),
        'board': {'counts': analysis['counts'], 'multiple_offer_count': analysis['multiple_offer_count'],
                  'unique_contracts': analysis['unique_contracts'], 'rejected_accepts': analysis['rejected']},
        'limitations': load('limitations.json'), 'public_plan': load('selected.json'),
        'read_ledgers': ledgers,
        'public_read_summary': {'attempts': len(public_attempts),
                               'successful_body_bytes': sum(r.get('bytes', 0) for r in public_attempts),
                               'http_200': sum(r.get('status') == 200 for r in public_attempts),
                               'http_404': sum('404' in r.get('error', '') for r in public_attempts),
                               'retries': 0, 'rate': 'serial; at least 1 second after successful request'},
        'real_contracts': assessments,
        'dryrun': {key: value for key, value in load('dryrun-final.json').items() if key != 'transcript'},
        'golden': load('golden.json'), 'regression': load('regression.json'),
        'solver': {'source': solver['source'], 'bundle_sha256': solver['bundle_sha256'],
                   'proof': solver['proof'], 'delivery_candidate': solver['delivery_candidate']},
        'historical_integrity': {'checked': len(baseline['hashes']), 'changed': changed,
                                'official_checkout_clean': True},
        'execution_incidents': ['sandbox DNS failure; one escalated acquisition, failure ledger retained',
                               'solver wrapper null spec; one fix then PASS',
                               'TEST amount=0 rejected; one fix to positive paper amount then PASS',
                               '404 Note stops first read group; only four previously unattempted URLs subsequently read'],
        'external_writes': 0, 'real_signer_access': False, 'next_phase_started': False,
        'artifact_sha256': artifact_hashes,
    }
    dest = ROOT / 'docs/real-pilot-protocol-batch16-20260919.json'
    with dest.open('x') as stream:
        json.dump(summary, stream, ensure_ascii=True, indent=2)
        stream.write('\n')
    print(json.dumps({'historical_integrity': summary['historical_integrity'],
                      'board': summary['board'], 'public_read': summary['public_read_summary'],
                      'real': [(r['contract'][:18], r['protocol_state'], r['paper_assessment']) for r in assessments]}))


def seal():
    status = subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT).decode()
    assert not status, 'final worktree must be clean'
    save('final-state.json', {'at': datetime.now(timezone.utc).isoformat(),
         'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
         'worktree': 'clean', 'push': False, 'external_writes': 0,
         'report_sha256': sha((ROOT / 'docs/real-pilot-protocol-batch16-20260919.md').read_bytes()),
         'evidence_sha256': sha((ROOT / 'docs/real-pilot-protocol-batch16-20260919.json').read_bytes())})
    print((OUT / 'final-state.json').read_text())


def check():
    sources = json.loads((OUT / 'sources.json').read_text())
    generations, verified = set(), []
    for source in sources['sources']:
        path = ROOT / source['path']
        raw = path.read_bytes()
        response = json.loads(path.with_name(path.stem + '-response.json').read_text())
        assert sha(raw) == source['sha256'] == response['raw_sha256']
        if path.name == 'export.bin':
            generation = int(response['headers']['x-room-generation'])
        else:
            generation = json.loads(raw)['generation']
        generations.add(generation)
        verified.append({'source': source['path'], 'generation': generation,
                         'fetched_at': response['at'], 'sha256': sha(raw)})
    assert generations == {1}, 'mixed room epochs'
    summary = json.loads((ROOT / 'docs/real-pilot-protocol-batch16-20260919.json').read_text())
    assert summary['dryrun']['external_writes'] == 0
    assert len(summary['dryrun']['checks']) == 46
    assert all(x['passed'] for x in summary['dryrun']['checks'])
    assert [r['protocol_state'] for r in summary['real_contracts']] == ['claimed', 'accepted', 'refunded', 'accepted']
    assert all(row['missing_seq'] == 0 for row in summary['limitations']['rankCoverage'])
    baseline = json.loads((OUT / 'baseline.json').read_text())
    assert all(sha((ROOT / p).read_bytes()) == h for p, h in baseline['hashes'].items())
    ast.parse(Path(__file__).read_text())
    result = subprocess.run(['node', '--check', 'tests/batch16_protocol.mjs'], cwd=ROOT,
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode()
    save('final-validation.json', {'at': datetime.now(timezone.utc).isoformat(),
         'historical_hashes_unchanged': len(baseline['hashes']), 'generation': 1,
         'verified_sources': verified, 'report_counts_consistent': True,
         'syntax_checks': 'Python AST and node --check PASS',
         'runner_sha256': {p: sha((ROOT / p).read_bytes()) for p in
                           ('tests/batch16_protocol.py', 'tests/batch16_protocol.mjs')}})
    print(json.dumps({'sources_hash_bound': len(verified), 'generation': 1,
                      'historical_unchanged': len(baseline['hashes']), 'syntax': 'PASS', 'report': 'PASS'}))


def solve():
    sys.path.insert(0, str(ROOT / 'src'))
    from unittest.mock import patch
    from collaboration_agent.engine import Agent
    from collaboration_agent.state import Store
    from collaboration_agent.materials import load_frozen, run_frozen
    from verify_bounded import verify_gcd_lcm
    manifest = json.loads((ROOT / '.local/batch15/session-01/frozen/manifest.json').read_text())
    with patch('socket.socket', side_effect=AssertionError('offline network forbidden')):
        for case in manifest['cases']:
            path = ROOT / '.local/batch15/session-01/frozen' / case['id']
            bundle = load_frozen(path)
            if (bundle.get('spec') or {}).get('ask', '').find('Compute gcd(') < 0:
                continue
            with Store(OUT / 'solver-state', create=True) as state:
                result = run_frozen(path, Agent(state))
                outcome = result.get('response', {}).get('result', {}).get('outcome', {})
                proof = verify_gcd_lcm(bundle, outcome)
                assert proof and proof['valid']
                audit = state.verify()
            save('solver.json', {'source': str(path.relative_to(ROOT)), 'case': case,
                 'bundle_sha256': sha((path / 'bundle.json').read_bytes()),
                 'result': result, 'proof': proof, 'audit': audit,
                 'delivery_candidate': outcome['value'], 'external_writes': 0})
            print(json.dumps({'task': case['id'], 'answer': outcome['value'], 'verified': proof['valid']}))
            return
    raise AssertionError('no saved gcd candidate')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'sources', 'dependencies', 'rooms', 'solve',
                                       'select', 'lossless', 'regression', 'remaining', 'summarize', 'seal', 'check'])
    args = parser.parse_args()
    globals()[args.mode]()
