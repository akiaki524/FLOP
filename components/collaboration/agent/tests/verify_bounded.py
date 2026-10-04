"""Independent development oracles; no imports of runtime grammar or algorithms."""

import re

from collaboration_agent.model import sha


TAIL = (' Paid in FLOP or PAPER on the paper rail (testnet-era: no value moves until the FLOP escrow exists).'
        ' | PROTOCOL: after accepting, post a heartbeat frame in the derived deal room '
        'mb-p-tclk-<first 16 hex of contract> (this creates it); lock, reveal and receipt then '
        'land where the reference tclk fold expects them. | CREDIT: passes are ranked under '
        'your DID; tasks feed and questions: /r/blockrewards.')


def integer(text):
    return (text.isascii() and text.isdigit() and len(text) <= 20
            and str(int(text)) == text)


def verify_gcd_lcm(bundle, result):
    """Existing GCD/LCM capability: independent parser and Bezout certificate.

    This scoring oracle does not import the solver, math.gcd, or math.lcm.
    The original derived-ask citation is checked against the frozen request.
    """
    spec, materials = bundle.get('spec'), bundle.get('materials', [])
    if (not spec or spec.get('family') != 'math' or len(materials) != 1
            or spec.get('done') != 'one line: gcd=<g> lcm=<l>.' or spec.get('tail') != TAIL):
        return None
    source = materials[0]
    if source.get('role') not in {'full_spec', 'inline_spec'}:
        return None
    heading = re.fullmatch(r'\[difficulty [1-3]/3\] (.+)', spec.get('ask', ''))
    if not heading:
        return None
    ask = heading[1]
    if not ask.startswith('Compute gcd(') or not ask.endswith(').'):
        return None
    operands = ask[len('Compute gcd('):-2].split(') and lcm(')
    if len(operands) != 2:
        return None
    left, right = [part.split(', ') for part in operands]
    if left != right or len(left) != 2 or any(
            not re.fullmatch(r'0|[1-9][0-9]{0,99}', value) for value in left):
        return None
    text = source.get('text', '')
    envelope = ('math | ' + spec['ask'] + ' | reward tier ')
    ending = ('/5 | done looks like: ' + spec['done']
              + ' | deliver as one signed message in the deal room, then reveal.' + TAIL)
    if (not any(text == envelope + str(tier) + ending for tier in range(1, 6))
            or sha(text.encode()) != source.get('normalized_sha256')):
        return None
    a, b = map(int, left)
    old_r, r, old_x, x, old_y, y = a, b, 1, 0, 0, 1
    while r:
        q = old_r // r
        old_r, r = r, old_r - q * r
        old_x, x = x, old_x - q * x
        old_y, y = y, old_y - q * y
    g = old_r
    certificate = (old_x * a + old_y * b == g and
                   (a == b == 0 if g == 0 else a % g == b % g == 0))
    l = a * b // g if g else 0
    expected = f'gcd={g} lcm={l}'
    citation = {'source_id': 'ask', 'locator': 'derived:ask:' + bundle['request_digest'],
                'sha256': sha(ask.encode()), 'selector': {'kind': 'text'}, 'quote': ask}
    valid = (certificate and result.get('status') == 'COMPLETED'
             and result.get('verdict') == 'NOT_APPLICABLE' and result.get('value') == expected
             and result.get('evidence') == [citation])
    return {'kind': 'gcd_lcm', 'operands': [a, b], 'gcd': g, 'lcm': l,
            'bezout_coefficients': [old_x, old_y], 'certificate_valid': certificate,
            'method': 'extended Euclidean Bezout certificate and product identity; independent source/output parsing',
            'expected_answer': expected, 'valid': bool(valid)}


def verify_bounded(spec, result, materials):
    if not spec or not materials or len(materials) != 1:
        return None
    source = materials[0]
    if source.get('role') not in {'full_spec', 'inline_spec'} or not isinstance(source.get('text'), str):
        return None
    expected, proof = None, None
    verdict = 'NOT_APPLICABLE'
    if spec['family'] == 'validation' and spec['tail'] == TAIL:
        if spec['done'] != 'one line: PASS or FAIL, then one sentence.':
            return None
        start = 'Validate a deliverable. TASK that was posted: "'
        reference = '". REFERENCE ANSWER the task\'s author holds (private to you as validator): "'
        deliverable = '". DELIVERABLE submitted by a worker: "'
        end = ('". Does the deliverable give the reference answer (same values, order where order '
               'is asked, nothing invented)? Reply PASS or FAIL, then one sentence naming the '
               'exact match or the exact discrepancy.')
        ask = spec['ask']
        if not ask.startswith(start) or not ask.endswith(end):
            return None
        parts = ask[len(start):-len(end)].split(reference)
        if len(parts) != 2:
            return None
        posted, values = parts
        pair = values.split(deliverable)
        if len(pair) != 2 or not all(integer(p) for p in pair):
            return None
        match = re.fullmatch(r'\[difficulty [1-3]/3\] Find the modular inverse of ([0-9]+) modulo '
                             r'([0-9]+) \(([0-9]+) is prime\), i\.e\. the x in \[1, ([0-9]+)\] '
                             r'with ([0-9]+)·x ≡ 1 \(mod ([0-9]+)\)\.', posted)
        if not match or not all(integer(p) for p in match.groups()):
            return None
        a, p, p2, upper, a2, p3 = map(int, match.groups())
        if not (a == a2 and p == p2 == p3 and 1 <= a < p and upper + 1 == p and 1 <= int(pair[0]) < p):
            return None
        equal = pair[0] == pair[1]
        expected = (f'PASS: The integer matches the supplied reference ({pair[0]}).' if equal
                    else f'FAIL: Expected integer {pair[0]}, but the deliverable gives {pair[1]}.')
        verdict = 'MATCH' if equal else 'MISMATCH'
        proof = {'kind': 'integer_reference', 'reference': pair[0], 'delivered': pair[1],
                 'decision': 'PASS' if equal else 'FAIL', 'scope': 'comparison to author reference, not inverse correctness'}
    elif spec['family'] == 'inference':
        prefix = ('From the note the table at the end of this note '
                  '(rows: seq | payer | amount | asset | proto | time): ')
        queries = {
            'sort all rows by payer (ASCII order), then by seq ascending, and output the seq values in that order, comma-separated.':
                ('payer_order', 'one line: all seq values in the sorted order, comma-separated.'),
            'output the seq of the row with the earliest time and the seq of the row with the latest time, as "<earliest_seq> <latest_seq>" (ties: lower seq).':
                ('time_extrema', 'one line: two seq values.'),
        }
        if not spec['ask'].startswith(prefix) or spec['ask'][len(prefix):] not in queries:
            return None
        kind, done = queries[spec['ask'][len(prefix):]]
        header = TAIL + ' | MATERIAL: seq | payer | amount | asset | proto | time '
        if spec['done'] != done or not spec['tail'].startswith(header):
            return None
        body = spec['tail'][len(header):]
        tokens = body.split()
        if ' '.join(tokens) != body or len(tokens) % 11 or not 1 <= len(tokens) // 11 <= 64:
            return None
        rows = []
        for offset in range(0, len(tokens), 11):
            row = tokens[offset:offset + 11]
            if row[1::2] != ['|'] * 5:
                return None
            seq, payer, amount, asset, proto, time = row[::2]
            if (not integer(seq) or not integer(amount) or not payer.isascii() or not payer.isalnum()
                    or not 1 <= len(payer) <= 64 or not re.fullmatch(r'[A-Z][A-Z0-9]{0,15}', asset)
                    or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', proto)):
                return None
            clock = time.split(':')
            if len(clock) != 3 or any(len(t) != 2 or not t.isascii() or not t.isdigit() for t in clock):
                return None
            h, m, s = map(int, clock)
            if h >= 24 or m >= 60 or s >= 60:
                return None
            rows.append((int(seq), payer.encode('ascii'), h * 3600 + m * 60 + s))
        if len({r[0] for r in rows}) != len(rows):
            return None
        if kind == 'payer_order':
            order = [None] * len(rows)
            for seq, payer, _ in rows:
                rank = sum((p, n) < (payer, seq) for n, p, _ in rows)
                order[rank] = seq
            expected = ','.join(map(str, order))
        else:
            earliest = min(time for _, _, time in rows)
            latest = max(time for _, _, time in rows)
            order = [min(n for n, _, t in rows if t == earliest), min(n for n, _, t in rows if t == latest)]
            expected = ' '.join(map(str, order))
        proof = {'kind': kind, 'row_count': len(rows), 'selected_seq': order,
                 'method': 'independent whitespace/column parser; ranks or seconds extrema'}
    elif spec['family'] == 'math' and spec['tail'] == TAIL:
        if spec['done'] != 'one line: the length.':
            return None
        ask = re.fullmatch(r'\[difficulty [1-3]/3\] Undirected weighted graph on nodes 0\.\.([0-9]+), '
                           r'edges \(a-b:w\): (.+)\. What is the length of the shortest path from node '
                           r'([0-9]+) to node ([0-9]+)\?', spec['ask'])
        if not ask or not all(integer(ask[i]) for i in (1, 3, 4)):
            return None
        n, start, target = int(ask[1]) + 1, int(ask[3]), int(ask[4])
        if not 2 <= n <= 16 or not (0 <= start < n and 0 <= target < n):
            return None
        edges, seen = [], set()
        for raw in ask[2].split(', '):
            fields = raw.split(':')
            if len(fields) != 2:
                return None
            fields = fields[0].split('-') + [fields[1]]
            if len(fields) != 3 or not all(integer(f) for f in fields):
                return None
            a, b, weight = map(int, fields)
            edge = frozenset((a, b))
            if len(edge) != 2 or edge in seen or max(a, b) >= n or not 1 <= weight <= 10**9:
                return None
            seen.add(edge)
            edges.append((a, b, weight))
        if not 1 <= len(edges) <= 120:
            return None
        # Bellman-Ford, independent of runtime Dijkstra and Floyd-Warshall.
        distance = [None] * n
        distance[start] = 0
        for _ in range(n - 1):
            old = distance.copy()
            for a, b, weight in edges:
                for left, right in ((a, b), (b, a)):
                    if old[left] is not None:
                        new = old[left] + weight
                        if distance[right] is None or new < distance[right]:
                            distance[right] = new
        if distance[target] is None:
            return None  # Unreachable answer format is unsupported, not a completion.
        expected = str(distance[target])
        proof = {'kind': 'shortest_path', 'vertices': n, 'edges': len(edges),
                 'start': start, 'target': target, 'distances': distance, 'method': 'Bellman-Ford'}
    if proof is None:
        return None
    evidence = [{'source_id': source['id'], 'locator': source.get('url', source.get('reference')),
                 'sha256': source['normalized_sha256'], 'selector': {'kind': 'text'}, 'quote': source['text']}]
    valid = (sha(source['text'].encode()) == source['normalized_sha256'] and result.get('status') == 'COMPLETED'
             and result.get('value') == expected and result.get('verdict') == verdict and result.get('evidence') == evidence)
    return {**proof, 'expected_answer': expected, 'valid': valid}
