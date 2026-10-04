"""Offline Batch 17A integration and evidence tools; no dependency installation."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.local/batch17a1'
CANDIDATE = ROOT / '.local/batch17a/candidate.json'
RUNTIME = ROOT / '.local/batch16/official-runtime'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.write('\n')


def make_candidate(state_path):
    # Existing solver and independent verification produce the trusted TEST admission input.
    sys.path.insert(0, str(ROOT / 'src'))
    from unittest.mock import patch
    from collaboration_agent.materials import load_frozen, run_frozen
    from collaboration_agent.engine import Agent
    from collaboration_agent.state import Store
    from verify_bounded import verify_gcd_lcm
    path = ROOT / '.local/batch15/session-01/frozen/native-6948666'
    with patch('socket.socket', side_effect=AssertionError('offline')):
        bundle = load_frozen(path)
        with Store(state_path, create=True) as store:
            result = run_frozen(path, Agent(store))['response']['result']['outcome']
        proof = verify_gcd_lcm(bundle, result)
        assert proof['valid']
    return {'source_digest': sha((path / 'bundle.json').read_bytes()),
         'answer': result['value'], 'independent_verification': proof,
         'family': 'math.gcd_lcm', 'heartbeat_required': True,
         'task_format_evidence': 'Batch16 GCD contract 00d2d493c303559c and frozen full spec',
         'source': str(path.relative_to(ROOT))}


class OfflinePilotSignerTest(unittest.TestCase):
    def test_typed_signer_integration(self):
        """Real official crypto; ephemeral keys; mock effects; no public transport."""
        if not (RUNTIME / 'src/index.js').is_file() or not CANDIDATE.is_file():
            self.skipTest('Batch16/17A local evidence unavailable in clean checkout')
        OUT.mkdir(exist_ok=True)
        evidence = Path(tempfile.mkdtemp(prefix='verification-', dir=OUT))
        candidate = make_candidate(evidence / 'solver-state')
        self.assertEqual(candidate, json.loads(CANDIDATE.read_text()))
        result = subprocess.run(['node', 'tests/pilot_signer.mjs'], cwd=ROOT,
                                capture_output=True, timeout=450)
        # Never copy raw stderr or exception objects into durable evidence.
        summary = json.loads(result.stdout)
        save(evidence / 'summary.json', summary)
        save(evidence / 'execution.json', {'at': datetime.now(timezone.utc).isoformat(),
             'returncode': result.returncode, 'stderr_bytes': len(result.stderr),
             'stderr_sha256': sha(result.stderr), 'candidate_verified': True})
        self.assertEqual(result.returncode, 0, 'See sanitized Batch17A verification summary')
        self.assertEqual(summary['failed'], 0)
        self.assertEqual(summary['passed'], summary['planned'])
        self.assertGreaterEqual(summary['planned'], 58)
        self.assertEqual(summary['public_writes'], 0)


if __name__ == '__main__':
    unittest.main()