"""Observed validation, Nim and document templates; public prose stays inert."""

import re

from .model import check, citation, document_value, fields, outcome, sha, source_for


POSTED_TASK = ('From the note the table below (rows: seq | payer | amount | asset | proto | time): '
               'output the seq of the row with the earliest time and the seq of the row with the '
               'latest time, as "<earliest_seq> <latest_seq>" (ties: lower seq).')
PREFIX = 'Validate a deliverable. TASK that was posted: "' + POSTED_TASK
REFERENCE = '". REFERENCE ANSWER the task\'s author holds (private to you as validator): "'
DELIVERABLE = '". DELIVERABLE submitted by a worker: "'
ENDING = ('". Does the deliverable give the reference answer (same values, order where order is '
          'asked, nothing invented)? Reply PASS or FAIL, then one sentence naming the exact match '
          'or the exact discrepancy.')
DONE = 'one line: PASS or FAIL, then one sentence.'
# Match the observed board envelope too. Added MATERIAL or directives are unsupported.
TAIL = (' Paid in FLOP or PAPER on the paper rail (testnet-era: no value moves until the FLOP escrow exists).'
        ' | PROTOCOL: after accepting, post a heartbeat frame in the derived deal room '
        'mb-p-tclk-<first 16 hex of contract> (this creates it); lock, reveal and receipt then '
        'land where the reference tclk fold expects them. | CREDIT: passes are ranked under '
        'your DID; tasks feed and questions: /r/blockrewards.')
NUMBER = r'(?:0|[1-9][0-9]{0,19})'
PAIR = NUMBER + ' ' + NUMBER
PATTERN = re.compile(re.escape('validation | ' + PREFIX + REFERENCE)
                     + '(' + PAIR + ')' + re.escape(DELIVERABLE)
                     + '(' + PAIR + ')' + re.escape(ENDING + ' | reward tier ')
                     + r'[1-5]' + re.escape('/5 | done looks like: ' + DONE
                     + ' | deliver as one signed message in the deal room, then reveal.' + TAIL))


def validation_pair(task):
    fields(task['params'], '')
    if len(task['evidence']) != 1:
        return outcome('UNKNOWN', 'unsupported_validation_materials')
    source = task['evidence'][0]
    match = PATTERN.fullmatch(source['text'])
    if not match:
        return outcome('UNKNOWN', 'unsupported_validation_template')
    reference, delivered = match.groups()
    expected = tuple(map(int, reference.split(' ')))
    actual = tuple(map(int, delivered.split(' ')))
    equal = actual == expected
    if equal:
        answer = f'PASS: The earliest and latest seq values match the reference in order ({reference}).'
    else:
        answer = f'FAIL: Expected earliest/latest seq {reference}, but the deliverable gives {delivered}.'
    # Separate representation check before emitting a completed answer. No table is being
    # recomputed: this task explicitly asks to compare with the author's supplied reference.
    check((reference == delivered) == equal, 'validation_verification_failed')
    check(answer.startswith('PASS: ' if equal else 'FAIL: ') and '\n' not in answer,
          'validation_verification_failed')
    return outcome('COMPLETED', 'ordered_seq_reference_validation', value=answer,
                   verdict='MATCH' if equal else 'MISMATCH',
                   evidence=[citation(source, {'kind': 'text'}, source['text'])])


NIM_START = 'Nim with heaps of sizes '
NIM_END = (' (normal play, remove any number from one heap, last move wins). If the player to '
           'move can force a win, give one winning move as "heap i to k" (1-based heap index, '
           'new size); otherwise answer "none".')
NIM_PATTERN = re.compile(r'math \| \[difficulty [1-3]/3\] ' + re.escape(NIM_START)
                         + '(' + NUMBER + '(?:, ' + NUMBER + '){0,31})'
                         + re.escape(NIM_END) + r' \| reward tier [1-5]/5'
                         + re.escape(' | done looks like: one line: the move or none.'
                         + ' | deliver as one signed message in the deal room, then reveal.' + TAIL))


def parse_nim(text):
    """Full board template, 1..32 heaps of canonical nonnegative <=20-digit ints."""
    match = NIM_PATTERN.fullmatch(text)
    return tuple(map(int, match[1].split(', '))) if match else None


def nim_candidate(heaps):
    total = 0
    for heap in heaps:
        total ^= heap
    if total == 0:
        return 'none'
    for i, heap in enumerate(heaps, 1):
        target = heap ^ total
        if target < heap:
            return f'heap {i} to {target}'
    return None  # Internal failure must never be formatted as a losing position.


def verify_nim_move(heaps, answer):
    """Validate the answer, independently of candidate generation (bit parity, no XOR)."""
    if (not isinstance(heaps, (tuple, list)) or not 1 <= len(heaps) <= 32
            or any(type(h) is not int or not 0 <= h < 10**20 for h in heaps)
            or type(answer) is not str):
        return False

    def balanced(position):
        return all(sum((h >> bit) & 1 for h in position) % 2 == 0
                   for bit in range(max(position).bit_length()))

    if answer == 'none':
        return balanced(heaps)  # Includes the terminal all-zero position.
    move = re.fullmatch(r'heap ([1-9][0-9]?) to (' + NUMBER + ')', answer)
    if not move:
        return False
    index, target = map(int, move.groups())
    if not 1 <= index <= len(heaps) or not 0 <= target < heaps[index - 1]:
        return False
    after = list(heaps)
    after[index - 1] = target
    return not balanced(heaps) and balanced(after)


def normal_nim(task):
    fields(task['params'], 'source')
    source = source_for(task, task['params']['source'])
    if len(task['evidence']) != 1:
        return outcome('UNKNOWN', 'unsupported_nim_materials')
    heaps = parse_nim(source['text'])
    if heaps is None:
        return outcome('UNKNOWN', 'unsupported_nim_template')
    answer = nim_candidate(heaps)
    check(verify_nim_move(heaps, answer), 'nim_verification_failed')
    return outcome('COMPLETED', 'normal_nim_verified', value=answer,
                   verdict='NOT_APPLICABLE',
                   evidence=[citation(source, {'kind': 'text'}, source['text'])])


MESSAGE_README_URL = 'https://raw.githubusercontent.com/flop-labs/technocore-chat/main/README.md'
# Reviewed source version, not an answer lookup. Any changed bytes require review.
MESSAGE_README_SHA256 = '4fd57fb18f5f9a4c16771238f5e3d2c77b10c3592c8315da23101f30719e02b3'
MESSAGE_QUESTIONS = (
    ('api', 'What is the maximum character limit for messages in the chat?'),
    ('document', 'What is the maximum size in characters for a message body?'),
)
MESSAGE_PATTERNS = tuple(re.compile(re.escape(f'{family} | From {MESSAGE_README_URL}: {question}')
                         + r' \| reward tier [1-5]/5'
                         + re.escape(' | done looks like: one line: the exact value or phrase '
                         'from the cited document (quote it), nothing else'
                         ' | deliver as one signed message in the deal room, then reveal.' + TAIL))
                         for family, question in MESSAGE_QUESTIONS)


def message_limit_template(text):
    return any(pattern.fullmatch(text) for pattern in MESSAGE_PATTERNS)


def message_limit_quote(text):
    """Extract the unique message-character phrase, not note/body/URL limits."""
    lines = text.splitlines()
    matches = [(i + 1, match[0]) for i, line in enumerate(lines)
               for match in re.finditer(r'Messages ≤ [1-9][0-9]{0,8} chars', line)]
    if len(matches) != 1:
        return None
    number, phrase = matches[0]
    if '## API' not in lines[:number - 1] or '### Invariants worth knowing' not in lines[number:]:
        return None
    return number, f'"{phrase}"'


def message_limit(task):
    fields(task['params'], 'spec document')
    spec = source_for(task, task['params']['spec'])
    document = source_for(task, task['params']['document'])
    if len(task['evidence']) != 2 or spec['id'] == document['id']:
        return outcome('UNKNOWN', 'unsupported_message_limit_materials')
    if not message_limit_template(spec['text']):
        return outcome('UNKNOWN', 'unsupported_message_limit_template')
    if document['locator'] != MESSAGE_README_URL:
        return outcome('UNKNOWN', 'message_limit_source_mismatch')
    if sha(document['text'].encode()) != MESSAGE_README_SHA256:
        return outcome('UNKNOWN', 'message_limit_revision_unreviewed')
    extracted = message_limit_quote(document['text'])
    check(extracted is not None, 'message_limit_verification_failed')
    number, answer = extracted
    rows = document['text'].splitlines()
    # Independent delimiter check, separate from the regex that generated the quote.
    anchors = [i for i, row in enumerate(rows, 1) if row.startswith('Names match `')
               and '. Messages ≤ ' in row and ', notes ≤ ' in row]
    check(anchors == [number], 'message_limit_verification_failed')
    value_and_unit = rows[number - 1].split('. Messages ≤ ', 1)[1].split(', notes ≤ ', 1)[0]
    check(answer == '"Messages ≤ ' + value_and_unit + '"', 'message_limit_verification_failed')
    return outcome('COMPLETED', 'document_message_limit_verified', value=answer,
                   verdict='NOT_APPLICABLE', evidence=[
                       citation(spec, {'kind': 'text'}, spec['text']),
                       citation(document, {'kind': 'lines', 'first': number, 'last': number}, rows[number - 1])])


WAIT_URL = 'https://technocore.chat/llms.txt'
WAIT_SHA256 = '40e0bebabcc105a2931805b68e200f1d5fc212e32ca14501ac4d85d105a2bb54'
WAIT_PATTERN = re.compile(re.escape("protocol | From " + WAIT_URL
                          + ": What is the maximum duration in seconds that can be specified for the 'wait' parameter?")
                          + r' \| reward tier [1-5]/5'
                          + re.escape(' | done looks like: one line: the exact value or phrase '
                          'from the cited document (quote it), nothing else'
                          ' | deliver as one signed message in the deal room, then reveal.' + TAIL))


def quoted_limit_template(text):
    return message_limit_template(text) or WAIT_PATTERN.fullmatch(text) is not None


def wait_limit_quote(text):
    matches = [(i, m[1]) for i, row in enumerate(text.splitlines(), 1)
               if (m := re.fullmatch(r'WAITING: wait=<seconds>, 0 to ([1-9][0-9]{0,8}), '
                                    r'and only together with since=\. It returns', row))]
    return (matches[0][0], '"' + matches[0][1] + '"') if len(matches) == 1 else None


def quoted_limit(task):
    fields(task['params'], 'spec document')
    spec = source_for(task, task['params']['spec'])
    if message_limit_template(spec['text']):
        return message_limit(task)
    if not WAIT_PATTERN.fullmatch(spec['text']):
        return outcome('UNKNOWN', 'unsupported_quoted_limit_template')
    document = source_for(task, task['params']['document'])
    if len(task['evidence']) != 2 or spec['id'] == document['id']:
        return outcome('UNKNOWN', 'unsupported_wait_limit_materials')
    if document['locator'] != WAIT_URL:
        return outcome('UNKNOWN', 'wait_limit_source_mismatch')
    if sha(document['text'].encode()) != WAIT_SHA256:
        return outcome('UNKNOWN', 'wait_limit_revision_unreviewed')
    extracted = wait_limit_quote(document['text'])
    check(extracted is not None, 'wait_limit_verification_failed')
    number, answer = extracted
    rows = document['text'].splitlines()
    confirmations = [(i, row.split('wait clamps to 0..', 1)[1].split(',', 1)[0])
                     for i, row in enumerate(rows, 1) if 'wait clamps to 0..' in row]
    check(len(confirmations) == 1 and answer == '"' + confirmations[0][1] + '"',
          'wait_limit_verification_failed')
    return outcome('COMPLETED', 'document_wait_limit_verified', value=answer,
                   verdict='NOT_APPLICABLE', evidence=[citation(spec, {'kind': 'text'}, spec['text'])]
                   + [citation(document, {'kind': 'lines', 'first': n, 'last': n}, rows[n - 1])
                      for n in (number, confirmations[0][0])])


AGENT_DOCUMENT_URL = 'https://technocore.chat/.well-known/agent.json'
AGENT_DOCUMENT_SHA256 = '05ee7a33a4c9e30b6b8a936f5c727274634a30dbbc4732d4820733a653236e48'
AGENT_QUESTIONS = {
    'What is the maximum number of notes allowed per namespace?': 'notes',
    'What is the schema_version?': 'schema',
    'What is the prefix for ephemeral rooms?': 'ephemeral',
}
AGENT_PATTERNS = tuple((re.compile(re.escape(f'extraction | From {AGENT_DOCUMENT_URL}: {question}')
                           + r' \| reward tier [1-5]/5'
                           + re.escape(' | done looks like: one line: the exact value or phrase '
                           'from the cited document (quote it), nothing else'
                           ' | deliver as one signed message in the deal room, then reveal.' + TAIL)), kind)
                       for question, kind in AGENT_QUESTIONS.items())


def agent_field_template(text):
    return next((kind for pattern, kind in AGENT_PATTERNS if pattern.fullmatch(text)), None)


def agent_field_candidate(text, kind):
    """Closed mapping for three observed questions; never a caller-supplied pointer."""
    if kind == 'notes':
        value = document_value(text.encode(), '/limits/notes_per_namespace')
        check(type(value) is int and value > 0, 'agent_field_verification_failed')
    elif kind == 'schema':
        value = document_value(text.encode(), '/schema_version')
        check(type(value) is str and re.fullmatch(r'[0-9]+\.[0-9]+', value),
              'agent_field_verification_failed')
    else:
        classes = document_value(text.encode(), '/conventions/room_classes')
        check(type(classes) is dict, 'agent_field_verification_failed')
        prefixes = [key for key, description in classes.items()
                    if type(description) is str and description.startswith('ephemeral — ')]
        check(len(prefixes) == 1 and re.fullmatch(r'[a-z]-', prefixes[0]),
              'agent_field_verification_failed')
        value = prefixes[0]
    return '"' + str(value) + '"'


def agent_fields(task):
    fields(task['params'], 'spec document')
    spec = source_for(task, task['params']['spec'])
    document = source_for(task, task['params']['document'])
    if len(task['evidence']) != 2 or spec['id'] == document['id']:
        return outcome('UNKNOWN', 'unsupported_agent_field_materials')
    kind = agent_field_template(spec['text'])
    if kind is None:
        return outcome('UNKNOWN', 'unsupported_agent_field_template')
    if document['locator'] != AGENT_DOCUMENT_URL:
        return outcome('UNKNOWN', 'agent_field_source_mismatch')
    if sha(document['text'].encode()) != AGENT_DOCUMENT_SHA256:
        return outcome('UNKNOWN', 'agent_field_revision_unreviewed')
    answer = agent_field_candidate(document['text'], kind)
    # Independently match the literal line in the reviewed document. Wrong JSON
    # pointer, wrong prefix or corrupted candidate cannot reach COMPLETED.
    patterns = {'notes': r'  "notes_per_namespace": ([1-9][0-9]*),',
                'schema': r' "schema_version": "([0-9]+\.[0-9]+)",',
                'ephemeral': r'   "([a-z]-)": "ephemeral — [^"\n]+"'}
    rows = document['text'].splitlines()
    matches = [(i, m[1]) for i, row in enumerate(rows, 1)
               if (m := re.fullmatch(patterns[kind], row))]
    check(len(matches) == 1 and answer == '"' + matches[0][1] + '"',
          'agent_field_verification_failed')
    number = matches[0][0]
    return outcome('COMPLETED', 'document_agent_field_verified', value=answer,
                   verdict='NOT_APPLICABLE', evidence=[citation(spec, {'kind': 'text'}, spec['text']),
                       citation(document, {'kind': 'lines', 'first': number, 'last': number}, rows[number - 1])])
