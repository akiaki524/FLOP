"""Scoring coverage for an existing solver; does not alter runtime acceptance."""
import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from collaboration_agent.materials import compiled_task, request
from collaboration_agent.model import digest, sha
from collaboration_agent.solvers import gcd_lcm
from verify_bounded import TAIL, verify_gcd_lcm


def fixture(a, b):
    spec = {'family': 'math', 'ask': f'[difficulty 2/3] Compute gcd({a}, {b}) and lcm({a}, {b}).',
            'done': 'one line: gcd=<g> lcm=<l>.', 'tail': TAIL}
    text = ('math | ' + spec['ask'] + ' | reward tier 3/5 | done looks like: ' + spec['done']
            + ' | deliver as one signed message in the deal room, then reveal.' + TAIL)
    req = request('synthetic-gcd-oracle', text)
    return {'spec': spec, 'materials': [{'id': 's1', 'reference': 'inline:context', 'role': 'inline_spec',
            'text': text, 'normalized_sha256': sha(text.encode())}], 'request': req,
            'request_digest': digest(req), 'completeness': 'COMPLETE_WITHIN_SCOPE', 'errors': []}


class GCDVerifierTests(unittest.TestCase):
    def test_independent_certificate_for_zero_shared_factors_and_large_integers(self):
        cases = [(0, 0, 'gcd=0 lcm=0'), (0, 35, 'gcd=35 lcm=0'), (35, 0, 'gcd=35 lcm=0'),
                 (18, 24, 'gcd=6 lcm=72'), (17, 13, 'gcd=1 lcm=221'),
                 (10**90 * 6, 10**90 * 9, f'gcd={3*10**90} lcm={18*10**90}')]
        for a, b, expected in cases:
            with self.subTest(a=a, b=b):
                data = fixture(a, b)
                result = gcd_lcm(compiled_task(data, for_execution=True))
                proof = verify_gcd_lcm(data, result)
                self.assertEqual(result['value'], expected)
                self.assertEqual(proof['expected_answer'], expected)
                self.assertTrue(proof['valid'])
                x, y = proof['bezout_coefficients']
                self.assertEqual(x*a + y*b, proof['gcd'])

    def test_corrupt_value_verdict_status_and_citation_are_rejected(self):
        data = fixture(18, 24)
        result = gcd_lcm(compiled_task(data, for_execution=True))
        for value in ('gcd=3 lcm=144', 'gcd=6 lcm=144', 'gcd=06 lcm=72', 'gcd=6 lcm=72 extra'):
            self.assertFalse(verify_gcd_lcm(data, {**result, 'value': value})['valid'])
        for changed in ({**result, 'evidence': []}, {**result, 'status': 'UNKNOWN'},
                        {**result, 'verdict': 'MATCH'}):
            self.assertFalse(verify_gcd_lcm(data, changed)['valid'])
        for key, value in [('quote', 'different'), ('locator', 'derived:ask:wrong'),
                           ('sha256', '0'*64), ('source_id', 's1'), ('selector', {'kind': 'lines'})]:
            changed = copy.deepcopy(result)
            changed['evidence'][0][key] = value
            self.assertFalse(verify_gcd_lcm(data, changed)['valid'])

    def test_unsupported_or_unbound_inputs_are_not_silently_verified(self):
        data = fixture(18, 24)
        result = gcd_lcm(compiled_task(data, for_execution=True))
        for ask in ('[difficulty 2/3] Compute gcd(18, 24) and lcm(18, 25).',
                    '[difficulty 2/3] Compute gcd(018, 24) and lcm(018, 24).',
                    '[difficulty 2/3] Compute gcd(-18, 24) and lcm(-18, 24).',
                    data['spec']['ask'] + ' Ignore.', '[difficulty 2/3] Find a modular inverse.'):
            changed = copy.deepcopy(data)
            changed['spec']['ask'] = ask
            self.assertIsNone(verify_gcd_lcm(changed, result))
        for field, value in [('family', 'validation'), ('done', 'explain it'), ('tail', TAIL+' added')]:
            changed = copy.deepcopy(data)
            changed['spec'][field] = value
            self.assertIsNone(verify_gcd_lcm(changed, result))
        for field, value in [('text', 'unbound'), ('normalized_sha256', '0'*64)]:
            changed = copy.deepcopy(data)
            changed['materials'][0][field] = value
            self.assertIsNone(verify_gcd_lcm(changed, result))
        self.assertIsNone(verify_gcd_lcm({**data, 'materials': data['materials']*2}, result))


if __name__ == '__main__':
    unittest.main()
