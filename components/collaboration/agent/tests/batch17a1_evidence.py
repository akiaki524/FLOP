"""Offline evidence: hash historical artifacts; capture only sanitized test results."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.local/batch17a1'


def save(name, data):
    with (OUT / name).open('x') as stream:
        json.dump(data, stream, indent=2)
        stream.write('\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(label, command):
    result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=600)
    # Test harness emits sanitized names and codes; never persist raw exceptions.
    if command[0] == 'node':
        data = json.loads(result.stdout)
    else:
        lines = result.stderr.decode(errors='replace').splitlines()
        data = {'test_lines': [line for line in lines if line.startswith('test_')],
                'conclusion': [line for line in lines if line.startswith(('Ran ', 'OK', 'FAILED'))]}
    data.update(returncode=result.returncode, stderr_bytes=len(result.stderr),
                stderr_sha256=hashlib.sha256(result.stderr).hexdigest())
    save(label + '.json', data)
    print(json.dumps({'label': label, 'returncode': result.returncode,
                      'conclusion': data.get('conclusion'), 'passed': data.get('passed'),
                      'failed': data.get('failed')}))
    return result.returncode


if __name__ == '__main__':
    mode = sys.argv[1]
    if mode == 'baseline':
        OUT.mkdir(exist_ok=True)
        roots = [ROOT / 'docs', ROOT / '.local/review17a']
        roots += [ROOT / '.local' / ('batch' + str(i)) for i in range(2, 17)]
        roots += [ROOT / '.local/batch17a']
        files = {str(p.relative_to(ROOT)): sha(p) for root in roots for p in root.rglob('*')
                 if p.is_file() and not p.is_symlink()}
        save('baseline.json', {'at': datetime.now(timezone.utc).isoformat(),
             'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
             'files': files})
        print(json.dumps({'historical_files': len(files)}))
    elif mode == 'check':
        baseline = json.loads((OUT / 'baseline.json').read_text())
        changed = [p for p, expected in baseline['files'].items()
                   if not (ROOT / p).is_file() or sha(ROOT / p) != expected]
        save(sys.argv[2] + '.json', {'checked': len(baseline['files']), 'changed': changed})
        print(json.dumps({'checked': len(baseline['files']), 'changed': changed}))
        sys.exit(bool(changed))
    elif mode == 'run':
        sys.exit(run(sys.argv[2], sys.argv[3:]))
    elif mode == 'nonce':
        path = ROOT / '.local/batch16/board.json'
        board = json.loads(path.read_text())
        nonces = [str(row['nonce']) for row in board if row.get('nonce') is not None]
        result = {'source': str(path.relative_to(ROOT)), 'sha256': sha(path),
                  'scope': 'Historical public board; not Project DID attribution',
                  'rows': len(board), 'nonce_digits': dict(sorted(Counter(map(len, nonces)).items())),
                  'unsafe_js_integer_count': sum(int(n) > 2**53 - 1 for n in nonces),
                  'earliest_timestamp': min(row['ts'] for row in board),
                  'latest_timestamp': max(row['ts'] for row in board),
                  'project_did': None, 'project_did_last_nonce': 'UNCONFIRMED',
                  'primary_room': 'tclk-offers'}
        save('nonce-observation.json', result)
        print(json.dumps(result))
    elif mode == 'final':
        result = {'at': datetime.now(timezone.utc).isoformat(),
                  'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                  'worktree': subprocess.check_output(['git', 'status', '--short'], cwd=ROOT, text=True),
                  'real_secret_access': False, 'external_writes': 0, 'push': False,
                  'batch17b_started': False}
        save(sys.argv[2], result)
        print(json.dumps(result))
