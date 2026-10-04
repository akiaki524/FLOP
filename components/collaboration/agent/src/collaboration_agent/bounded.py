"""Small observed families: typed reference comparison, inline tables, graphs.

All data is parsed, never executed. The board envelope and query operators are
closed; numeric inputs and complete bounded rows/edges are not answer lookups.
"""

import re

from .empirical import DELIVERABLE, DONE, ENDING, NUMBER, REFERENCE, TAIL
from .model import check, citation, fields, outcome, source_for


def board_end(done):
    return (r' \| reward tier [1-5]/5' + re.escape(' | done looks like: ' + done
            + ' | deliver as one signed message in the deal room, then reveal.' + TAIL))


INVERSE = (r'\[difficulty [1-3]/3\] Find the modular inverse of (' + NUMBER
           + r') modulo (' + NUMBER + r') \((' + NUMBER + r') is prime\), i\.e\. the x in \[1, ('
           + NUMBER + r')\] with (' + NUMBER + r')·x ≡ 1 \(mod (' + NUMBER + r')\)\.')
INTEGER_REFERENCE = re.compile(re.escape('validation | Validate a deliverable. TASK that was posted: "')
                               + INVERSE + re.escape(REFERENCE) + '(' + NUMBER + ')'
                               + re.escape(DELIVERABLE) + '(' + NUMBER + ')'
                               + re.escape(ENDING) + board_end(DONE))


def parse_integer_reference(text):
    match = INTEGER_REFERENCE.fullmatch(text)
    if not match:
        return None
    a, p, repeated_p, upper, repeated_a, final_p, reference, delivered = map(int, match.groups())
    if not (1 <= a < p and p == repeated_p == final_p and a == repeated_a and upper == p - 1
            and 1 <= reference < p):
        return None
    # This is comparison to a supplied reference, NOT recomputation of the inverse
    # or verification of the quoted author's primality claim.
    return reference, delivered


def integer_reference_candidate(reference, delivered):
    return (f'PASS: The integer matches the supplied reference ({reference}).' if reference == delivered
            else f'FAIL: Expected integer {reference}, but the deliverable gives {delivered}.')


def integer_reference(task):
    fields(task['params'], 'source')
    source = source_for(task, task['params']['source'])
    values = parse_integer_reference(source['text']) if len(task['evidence']) == 1 else None
    if values is None:
        return outcome('UNKNOWN', 'unsupported_integer_reference')
    reference, delivered = values
    answer = integer_reference_candidate(reference, delivered)
    # Independently decode the output decision and both named values.
    equal = str(reference) == str(delivered)
    pattern = (r'PASS: The integer matches the supplied reference \(([0-9]+)\)\.' if equal
               else r'FAIL: Expected integer ([0-9]+), but the deliverable gives ([0-9]+)\.')
    parsed = re.fullmatch(pattern, answer) if isinstance(answer, str) else None
    check(parsed is not None and parsed.groups() == (
          (str(reference),) if equal else (str(reference), str(delivered))), 'integer_reference_verification_failed')
    return outcome('COMPLETED', 'integer_reference_verified', value=answer,
                   verdict='MATCH' if equal else 'MISMATCH',
                   evidence=[citation(source, {'kind': 'text'}, source['text'])])


TABLE_INTRO = ('inference | From the note the table at the end of this note '
               '(rows: seq | payer | amount | asset | proto | time): ')
TABLE_QUERIES = {
    'payer_order': ('sort all rows by payer (ASCII order), then by seq ascending, and output the seq '
                    'values in that order, comma-separated.',
                    'one line: all seq values in the sorted order, comma-separated.'),
    'time_extrema': ('output the seq of the row with the earliest time and the seq of the row '
                     'with the latest time, as "<earliest_seq> <latest_seq>" (ties: lower seq).',
                     'one line: two seq values.'),
}
TABLE_HEADS = tuple((kind, re.compile(re.escape(TABLE_INTRO + ask) + board_end(done)
                              + re.escape(' | MATERIAL: seq | payer | amount | asset | proto | time ')))
                    for kind, (ask, done) in TABLE_QUERIES.items())
ROW = re.compile('(' + NUMBER + r') \| ([A-Za-z0-9]{1,64}) \| (' + NUMBER
                 + r') \| ([A-Z][A-Z0-9]{0,15}) \| ([a-z][a-z0-9_-]{0,31}) \| '
                 r'((?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9])')


def parse_table(text):
    for kind, head in TABLE_HEADS:
        match = head.match(text)
        if not match:
            continue
        rows, position = [], match.end()
        while position < len(text) and len(rows) < 64:
            row = ROW.match(text, position)
            if not row:
                return None
            seq, payer, amount, asset, proto, time = row.groups()
            rows.append((int(seq), payer, int(amount), asset, proto, time))
            position = row.end()
            if position == len(text):
                break
            if text[position] != ' ':
                return None
            position += 1
            if position == len(text):
                return None
        if not rows or position != len(text) or len({r[0] for r in rows}) != len(rows):
            return None
        return kind, rows
    return None


def table_candidate(kind, rows):
    if kind == 'payer_order':
        return ','.join(str(r[0]) for r in sorted(rows, key=lambda r: (r[1], r[0])))
    first = min(rows, key=lambda r: (r[5], r[0]))
    latest = max(r[5] for r in rows)
    last = min((r for r in rows if r[5] == latest), key=lambda r: r[0])
    return f'{first[0]} {last[0]}'


def verify_table_answer(kind, rows, answer):
    if not isinstance(answer, str):
        return False
    separator = ',' if kind == 'payer_order' else ' '
    parts = answer.split(separator)
    if any(re.fullmatch(NUMBER, p) is None for p in parts):
        return False
    numbers = list(map(int, parts))
    by_seq = {r[0]: r for r in rows}
    if kind == 'payer_order':
        if len(numbers) != len(rows) or len(set(numbers)) != len(rows) or set(numbers) != set(by_seq):
            return False
        keys = [(by_seq[n][1].encode('ascii'), n) for n in numbers]
        return all(a < b for a, b in zip(keys, keys[1:]))
    if kind != 'time_extrema' or len(numbers) != 2 or any(n not in by_seq for n in numbers):
        return False
    def seconds(row):
        h, m, s = map(int, row[5].split(':'))
        return h * 3600 + m * 60 + s
    early, late = map(by_seq.__getitem__, numbers)
    # Universal comparisons, independent of candidate min/max and string times.
    return all((seconds(early), early[0]) <= (seconds(r), r[0])
               and (-seconds(late), late[0]) <= (-seconds(r), r[0]) for r in rows)


def inline_table(task):
    fields(task['params'], 'source')
    source = source_for(task, task['params']['source'])
    parsed = parse_table(source['text']) if len(task['evidence']) == 1 else None
    if parsed is None:
        return outcome('UNKNOWN', 'unsupported_inline_table')
    kind, rows = parsed
    answer = table_candidate(kind, rows)
    check(verify_table_answer(kind, rows, answer), 'table_verification_failed')
    return outcome('COMPLETED', 'inline_table_verified', value=answer, verdict='NOT_APPLICABLE',
                   evidence=[citation(source, {'kind': 'text'}, source['text'])])


EDGE = NUMBER + '-' + NUMBER + ':' + NUMBER
GRAPH = re.compile(r'math \| \[difficulty [1-3]/3\] Undirected weighted graph on nodes 0\.\.('
                   + NUMBER + r'), edges \(a-b:w\): (' + EDGE + '(?:, ' + EDGE + r'){0,119})'
                   + r'\. What is the length of the shortest path from node (' + NUMBER
                   + ') to node (' + NUMBER + r')\?' + board_end('one line: the length.'))


def parse_graph(text):
    match = GRAPH.fullmatch(text)
    if not match:
        return None
    last, raw_edges, start, target = match.groups()
    n, start, target = int(last) + 1, int(start), int(target)
    if not 2 <= n <= 16 or not (0 <= start < n and 0 <= target < n):
        return None
    edges, seen = [], set()
    for raw in raw_edges.split(', '):
        a, b, weight = map(int, raw.replace('-', ':').split(':'))
        key = tuple(sorted((a, b)))
        if not (0 <= a < n and 0 <= b < n and a != b and 1 <= weight <= 10**9) or key in seen:
            return None
        seen.add(key)
        edges.append((a, b, weight))
    return n, edges, start, target


def graph_candidate(n, edges, start, target):
    """Dijkstra with a bounded linear priority scan; positive integer weights only."""
    distance, visited = [None] * n, set()
    distance[start] = 0
    for _ in range(n):
        available = [v for v in range(n) if v not in visited and distance[v] is not None]
        if not available:
            return None
        current = min(available, key=lambda v: distance[v])
        if current == target:
            return str(distance[current])
        visited.add(current)
        for a, b, weight in edges:
            neighbor = b if a == current else a if b == current else None
            if neighbor is not None:
                cost = distance[current] + weight
                if distance[neighbor] is None or cost < distance[neighbor]:
                    distance[neighbor] = cost
    return None


def verify_graph_answer(graph, answer):
    """Independent Floyd-Warshall check, including unreachable/no-answer cases."""
    n, edges, start, target = graph
    infinity = sum(w for _, _, w in edges) + 1
    matrix = [[0 if a == b else infinity for b in range(n)] for a in range(n)]
    for a, b, weight in edges:
        matrix[a][b] = matrix[b][a] = weight
    for mid in range(n):
        for a in range(n):
            for b in range(n):
                matrix[a][b] = min(matrix[a][b], matrix[a][mid] + matrix[mid][b])
    expected = matrix[start][target]
    return answer is None if expected == infinity else answer == str(expected)


def shortest_path(task):
    fields(task['params'], 'source')
    source = source_for(task, task['params']['source'])
    graph = parse_graph(source['text']) if len(task['evidence']) == 1 else None
    if graph is None:
        return outcome('UNKNOWN', 'unsupported_shortest_path')
    answer = graph_candidate(*graph)
    check(verify_graph_answer(graph, answer), 'shortest_path_verification_failed')
    if answer is None:
        return outcome('UNKNOWN', 'unreachable_length_not_in_answer_contract')
    return outcome('COMPLETED', 'shortest_path_verified', value=answer, verdict='NOT_APPLICABLE',
                   evidence=[citation(source, {'kind': 'text'}, source['text'])])
