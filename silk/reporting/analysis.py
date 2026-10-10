"""Heuristics over recorded queries: kind, shape, call site, hints and percentiles.

Nothing in this module reads the database, so it works on plain values.
"""
import hashlib
import re
from collections import Counter

SQL = 'sql'
CYPHER = 'cypher'
OTHER = 'other'
KINDS = (SQL, CYPHER, OTHER)

# Hint thresholds. They are fixed: a hint is a pointer for the reader and
# always prints the numbers behind it.
REPEATED_SHAPE_MIN = 5
IDENTICAL_QUERY_MIN = 2
DOMINANT_QUERY_SHARE = 0.5
OUTSIDE_QUERIES_SHARE = 0.5
OUTSIDE_QUERIES_MIN_MS = 100

_SQL_START = re.compile(
    r'(?:(?:SELECT|INSERT|UPDATE|DELETE\s+FROM|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|ALTER|DROP|PRAGMA'
    r'|CREATE\s+(?:TABLE|INDEX|VIEW))\b'
    r'|WITH\s+\w+\s+AS\s*\()',
    re.I,
)
_CYPHER_START = re.compile(
    r'(?:MATCH|OPTIONAL\s+MATCH|MERGE|CALL|UNWIND|RETURN|CREATE|WITH|DELETE|DETACH|SET|FOREACH|USE|SHOW'
    r'|PROFILE|EXPLAIN)\b',
    re.I,
)

_WHITESPACE = re.compile(r'\s+')
# SQL has no backslash escapes inside strings, Cypher has. Double quotes are
# identifiers in SQL and strings in Cypher.
_SQL_STRING = re.compile(r"'(?:[^']|'')*'")
_CYPHER_STRING = re.compile(r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*\"""")
# A digit that follows `name.` or sits inside a word belongs to an identifier.
_NUMBER = re.compile(r'(?<!\w)(?<!\w\.)\d+(?:\.\d+)?(?!\w)')
_SQL_IN_LIST = re.compile(r'\bIN\s*\((?!\s*SELECT\b)[^()]*\)', re.I)
# silk records SQL with its parameters interpolated and unquoted, and Django
# quotes every identifier, so an unquoted right-hand side is a value. It runs
# up to the next token that can follow a value, and may be empty. Those
# keywords are matched in upper case only, as Django writes them, so that a
# lower-case word inside a value does not end it.
_SQL_COMPARISON = re.compile(
    r'(<>|!=|<=|>=|=|<|>|\b(?i:I?LIKE)\b)\s*'
    r'(?!["`(\s]|\w+\.["`])'
    r'((?:\w+\()?)'
    r'.*?'
    r'(?=(?:\s+|(?<=\s))(?:AND|OR|ORDER\s+BY|GROUP\s+BY|HAVING|LIMIT|OFFSET|WHERE|THEN|ELSE|END|WHEN|ESCAPE'
    r'|UNION|RETURNING|FOR\s+(?:UPDATE|SHARE|NO\s+KEY)|ASC|DESC)\b'
    r'|\s*\)|\s*,\s*["`]|\s*;|\s*$)',
)
# A list of literals, of any length. Not the arguments of a call or an index.
_PLACEHOLDER_LIST = re.compile(r'(?<![\w\])])([(\[])\s*\?(?:\s*,\s*\?)*\s*([)\]])')

_FRAME = re.compile(r'^\s*File "(?P<file>.*)", line (?P<line>\d+), in (?P<function>.*)$')
_LIBRARY_PATH_PARTS = ('site-packages', 'dist-packages', '/lib/python')
_SILK_PATH = re.compile(r'(?:^|/)silk/')


def ms(value):
    """Rounds a duration in milliseconds; a missing duration stays missing."""
    if value is None:
        return None
    return round(float(value), 2)


def kind(text, has_analysis=False):
    """Returns 'sql', 'cypher' or 'other' for a recorded query.

    silk stores both in one table with no marker, so this goes by the leading
    keyword. Only SQL can carry EXPLAIN output.
    """
    if has_analysis:
        return SQL
    text = (text or '').lstrip()
    if _SQL_START.match(text):
        return SQL
    if _CYPHER_START.match(text):
        return CYPHER
    return OTHER


def _sql_value_placeholder(match):
    placeholder = '%s %s?' % match.groups()
    # An empty value leaves nothing between the operator and the next keyword.
    if match.string[match.end():match.end() + 1].isalpha():
        placeholder += ' '
    return placeholder


def normalise(text, query_kind):
    """Returns the query with its literals replaced by '?', its shape."""
    text = _WHITESPACE.sub(' ', text or '').strip()
    strings = _CYPHER_STRING if query_kind == CYPHER else _SQL_STRING
    text = strings.sub('?', text)
    text = _NUMBER.sub('?', text)
    if query_kind == SQL:
        text = _SQL_IN_LIST.sub('IN (?)', text)
        text = _SQL_COMPARISON.sub(_sql_value_placeholder, text)
    return _PLACEHOLDER_LIST.sub(r'\1?+\2', text)


def shape_id(shape_text):
    """A short id that depends only on the shape text, so it is the same in every request."""
    return hashlib.sha1(shape_text.encode('utf-8'), usedforsecurity=False).hexdigest()[:8]


def shape(text, query_kind):
    """Returns (shape id, shape text) for a query."""
    shape_text = normalise(text, query_kind)
    return shape_id(shape_text), shape_text


def parse_traceback(text):
    """Splits a recorded traceback into frames, in recorded order (innermost first)."""
    frames = []
    for line in (text or '').splitlines():
        match = _FRAME.match(line)
        if match:
            frames.append({
                'file': match.group('file'),
                'line': int(match.group('line')),
                'function': match.group('function'),
                'source': '',
            })
        elif frames and not frames[-1]['source']:
            source = line.strip()
            # Newer Pythons may add a line of markers under the source line.
            if source and not set(source) <= set('^~'):
                frames[-1]['source'] = source
    return frames


def is_application_frame(frame):
    """True for a frame that is not from an installed package, the standard library or silk.

    The check is on the recorded path text: the row may have been recorded by a
    different Python version or container than the one reading it.
    """
    path = frame['file'].replace('\\', '/')
    if path.startswith('<'):
        return False
    if any(part in path for part in _LIBRARY_PATH_PARTS):
        return False
    return not _SILK_PATH.search(path)


def application_frames(frames):
    return [frame for frame in frames if is_application_frame(frame)]


def call_site(traceback_text):
    """Returns the innermost application frame of a traceback, or None when there is none."""
    for frame in parse_traceback(traceback_text):
        if is_application_frame(frame):
            return frame
    return None


def describe_frame(frame):
    if not frame:
        return 'unknown'
    return '{file}:{line} in {function}'.format(**frame)


def percentile(values, percent):
    """Linear-interpolated percentile of the values that are not missing; None when there are none."""
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    rank = (len(values) - 1) * percent / 100
    lower = int(rank)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (rank - lower)


def median(values):
    return percentile(values, 50)


def group_shapes(executions):
    """Aggregates query executions by shape, costliest shape first.

    Each execution is a dict with 'id', 'shape', 'shape_text', 'kind' and
    'time_ms', and optionally 'call_site' (a frame) and 'request'.
    """
    groups = {}
    for execution in executions:
        group = groups.get(execution['shape'])
        if group is None:
            group = groups[execution['shape']] = {
                'shape': execution['shape'],
                'kind': execution['kind'],
                'count': 0,
                'total': 0.0,
                'max': None,
                'text': execution['shape_text'],
                'sites': Counter(),
                'frames': {},
                'requests': set(),
                'slowest_query': execution['id'],
                'slowest_request': execution.get('request'),
            }
        time_ms = execution['time_ms']
        group['count'] += 1
        group['total'] += time_ms or 0.0
        if time_ms is not None and (group['max'] is None or time_ms > group['max']):
            group['max'] = time_ms
            group['slowest_query'] = execution['id']
            group['slowest_request'] = execution.get('request')
        if execution.get('request') is not None:
            group['requests'].add(execution['request'])
        frame = execution.get('call_site')
        if frame:
            key = (frame['file'], frame['line'], frame['function'])
            group['sites'][key] += 1
            group['frames'].setdefault(key, frame)
    shapes = []
    for group in groups.values():
        site = group['sites'].most_common(1)
        shapes.append({
            'shape': group['shape'],
            'kind': group['kind'],
            'count': group['count'],
            'total_ms': ms(group['total']),
            'avg_ms': ms(group['total'] / group['count']),
            'max_ms': ms(group['max']),
            'call_site': group['frames'][site[0][0]] if site else None,
            'call_sites': len(group['sites']),
            'requests': len(group['requests']),
            'slowest_query': group['slowest_query'],
            'slowest_request': group['slowest_request'],
            'text': group['text'],
        })
    shapes.sort(key=lambda row: (-row['total_ms'], -row['count'], row['shape']))
    return shapes


def hints(time_ms, executions, has_profiler_output=False):
    """Returns the hints for one request, each with the numbers behind it.

    `time_ms` is the overall time of the request, `executions` its queries as
    dicts with 'id', 'shape', 'shape_text', 'kind', 'text', 'time_ms' and
    'call_site'.
    """
    executions = list(executions)
    found = []

    for row in group_shapes(executions):
        if row['count'] < REPEATED_SHAPE_MIN:
            continue
        if row['call_site'] is None:
            origin = 'from an unknown call site'
        elif row['call_sites'] > 1:
            origin = 'most often from %s' % describe_frame(row['call_site'])
        else:
            origin = 'from %s' % describe_frame(row['call_site'])
        found.append({
            'hint': 'repeated_shape',
            'shape': row['shape'],
            'count': row['count'],
            'total_ms': row['total_ms'],
            'call_site': row['call_site'],
            'message': 'shape %s ran %d times (%.2f ms in total) %s: likely N+1 pattern, fetch these rows in one query'
                       % (row['shape'], row['count'], row['total_ms'], origin),
        })

    texts = {}
    for execution in executions:
        texts.setdefault(execution['text'], []).append(execution)
    repeated = [group for group in texts.values() if len(group) >= IDENTICAL_QUERY_MIN]
    if repeated:
        repeated.sort(key=lambda group: -len(group))
        count = sum(len(group) for group in repeated)
        total = ms(sum(execution['time_ms'] or 0.0 for group in repeated for execution in group))
        top = repeated[0]
        found.append({
            'hint': 'identical_queries',
            'distinct': len(repeated),
            'count': count,
            'total_ms': total,
            'shape': top[0]['shape'],
            'query': top[0]['id'],
            'query_count': len(top),
            'message': '%d identical query text(s) ran more than once: %d executions, %.2f ms in total; '
                       'the most repeated ran %d times (shape %s, first as query %s)'
                       % (len(repeated), count, total, len(top), top[0]['shape'], top[0]['id']),
        })

    timed = [execution for execution in executions if execution['time_ms'] is not None]
    if time_ms and timed:
        slowest = max(timed, key=lambda execution: execution['time_ms'])
        if slowest['time_ms'] >= time_ms * DOMINANT_QUERY_SHARE:
            found.append({
                'hint': 'dominant_query',
                'query': slowest['id'],
                'shape': slowest['shape'],
                'time_ms': ms(slowest['time_ms']),
                'share_percent': round(100 * slowest['time_ms'] / time_ms),
                'message': 'query %s (shape %s) took %.2f ms, %d%% of the request\'s %.2f ms'
                           % (slowest['id'], slowest['shape'], slowest['time_ms'],
                              round(100 * slowest['time_ms'] / time_ms), time_ms),
            })

    if time_ms:
        outside = time_ms - sum(execution['time_ms'] or 0.0 for execution in executions)
        if outside >= time_ms * OUTSIDE_QUERIES_SHARE and outside >= OUTSIDE_QUERIES_MIN_MS:
            if has_profiler_output:
                attribution = 'profiler output is recorded for this request, print it with --cprofile'
            else:
                attribution = ('silk cannot attribute it because the Python profiler was not enabled '
                               '(SILKY_PYTHON_PROFILER)')
            found.append({
                'hint': 'time_outside_queries',
                'outside_queries_ms': ms(outside),
                'share_percent': round(100 * outside / time_ms),
                'profiler_output': bool(has_profiler_output),
                'message': '%.2f ms of %.2f ms (%d%%) were spent outside queries; %s'
                           % (outside, time_ms, round(100 * outside / time_ms), attribution),
            })
    return found
