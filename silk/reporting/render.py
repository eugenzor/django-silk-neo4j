"""Renders a report built by ``silk.reporting.data`` as plain text or as JSON.

Both work from the same dict, so they carry the same data. The text is plain:
no colour, no cursor codes, whether or not it goes to a terminal.
"""
import json
import re

from silk.reporting import analysis
from silk.reporting.data import short_id

MISSING = '-'

# Control characters found in recorded text (a body, a query) are written out,
# so that the output never carries an escape sequence.
_CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]')

_KINDS = '%(sql)d sql, %(cypher)d cypher, %(other)d other'


def render_json(report):
    """One JSON document, with the notes on cuts and the follow-up commands last, as in the text."""
    document = {key: value for key, value in report.items() if key not in ('truncated', 'next')}
    document['truncated'] = report['truncated']
    document['next'] = report['next']
    return json.dumps(document, ensure_ascii=False, default=str)


def render_text(report):
    lines = _RENDERERS[report['report']](report)
    text = '\n'.join(lines).replace('\r\n', '\n')
    return _CONTROL.sub(lambda match: '\\x%02x' % ord(match.group()), text)


def _value(value, key=None):
    """One value as text. A missing value is shown as such and never as 0."""
    if value is None or value == '':
        return MISSING
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, float):
        return '%.1f' % value if key == 'avg_queries' else '%.2f' % value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _fields(report, keys):
    return ['{}: {}'.format(key, _value(report[key], key)) for key in keys]


def _cell(row, column):
    value = row[column]
    if column == 'call_site':
        return analysis.describe_frame(value)
    if column == 'text':
        text = ' '.join(value.split())
        if row.get('text_chars', 0) > len(value):
            text += ' ...[%d chars]' % row['text_chars']
        return text
    return ' '.join(_value(value, column).split()) or MISSING


def _table(rows, columns, convert=None):
    """Aligned columns two spaces apart. The last column is free text and is not padded."""
    convert = convert or {}
    cells = [
        [convert[column](row[column]) if column in convert and row[column] else _cell(row, column) for column in columns]
        for row in rows
    ]
    numeric = [
        all(isinstance(row[column], (int, float)) or row[column] is None for row in rows)
        and any(row[column] is not None for row in rows)
        for column in columns
    ]
    widths = [max(len(line[index]) for line in [columns] + cells) for index in range(len(columns))]
    lines = []
    for line in [list(columns)] + cells:
        padded = []
        for index, cell in enumerate(line):
            if index == len(columns) - 1:
                padded.append(cell)
            elif numeric[index]:
                padded.append(cell.rjust(widths[index]))
            else:
                padded.append(cell.ljust(widths[index]))
        lines.append('  '.join(padded).rstrip())
    return lines


def _notice(entry):
    if 'rows' in entry:
        shown = 'cut to %d chars in %d row(s), the longest has %d' % (entry['shown'], entry['rows'], entry['total'])
    elif entry['unit'] == 'rows' and entry.get('offset'):
        if entry['shown']:
            first = entry['offset'] + 1
            shown = 'rows %d-%d of %d shown' % (first, entry['offset'] + entry['shown'], entry['total'])
        else:
            shown = 'no rows at offset %d, there are %d' % (entry['offset'], entry['total'])
    elif entry['unit'] == 'requests':
        shown = '%d of %d matching requests analysed' % (entry['shown'], entry['total'])
    else:
        shown = '%d of %d %s shown' % (entry['shown'], entry['total'], entry['unit'])
    return '[{}: {}; {}]'.format(entry['key'], shown, entry['option'])


def _notices(report, *keys):
    """The truncation notices of a part of the report, placed right after it."""
    return [
        _notice(entry) for entry in report['truncated']
        if entry['key'] in keys or entry['key'].split('.')[0] in keys
    ]


def _filters(report):
    if not report.get('filters'):
        return []
    # Spelled as the options that were given.
    return ['filters: ' + ' '.join(
        '--{} {}'.format(name.replace('_', '-'), value) for name, value in report['filters'].items()
    )]


def _empty(report, message):
    return [
        '',
        message,
        'recorded_requests: %d' % report['empty']['recorded_requests'],
        'latest_request_start: %s' % _value(report['empty']['latest_request_start']),
    ]


def _no_request(report):
    if report.get('filters'):
        return 'Nothing matched: no recorded request passes these filters.'
    return 'Nothing matched: no request has been recorded.'


def _next(report):
    lines = ['', 'NEXT']
    lines.extend('  ' + command for command in report['next'])
    return lines


def _text_block(report, title, key, note=None):
    lines = ['', title]
    if report[key] is None:
        lines.append(note or MISSING)
    else:
        lines.append(report[key] if report[key] != '' else '(empty)')
    return lines + _notices(report, key)


def _shapes(report, columns, title):
    lines = ['', '%s (%d, by total time)' % (title, report['shapes_total'])]
    lines += _table(report['shapes'], columns)
    return lines + _notices(report, 'shapes')


def _requests(report):
    lines = ['requests', 'matching: %d' % report['total'], 'sort: {} {}'.format(report['sort'], report['order'])]
    lines += _filters(report)
    if 'empty' in report:
        lines += _empty(report, _no_request(report))
    else:
        columns = ['id', 'start_time', 'method', 'status', 'time_ms', 'queries_ms', 'queries', 'sql', 'cypher']
        if any(row['other'] for row in report['requests']):
            columns.append('other')
        columns += ['view', 'path']
        lines.append('')
        if report['requests']:
            lines += _table(report['requests'], columns, {'id': short_id})
        lines += _notices(report, 'requests')
    return lines + _next(report)


def _summary(report):
    lines = ['summary']
    lines += _fields(report, ('requests_matching', 'requests_analysed'))
    lines += _notices(report, 'requests_analysed')
    lines += _filters(report)
    if 'empty' in report:
        lines += _empty(report, _no_request(report))
    else:
        lines += _fields(report, ('start_time_from', 'start_time_to', 'median_ms', 'p95_ms', 'max_ms'))
        lines += ['', 'VIEWS (%d, by total time)' % report['views_total']]
        lines += _table(report['views'], (
            'requests', 'median_ms', 'p95_ms', 'max_ms', 'total_ms', 'avg_queries', 'avg_queries_ms', 'view',
        ))
        lines += _notices(report, 'views')
    return lines + _next(report)


def _shape_list(report):
    lines = ['shapes']
    lines += _fields(report, ('requests_matching', 'requests_analysed'))
    lines += _notices(report, 'requests_analysed')
    lines += _filters(report)
    if 'empty' in report:
        lines += _empty(report, _no_request(report))
    elif not report['shapes_total']:
        lines += ['', 'The analysed requests recorded no queries.']
    else:
        lines += _fields(report, ('queries_analysed',))
        lines += _shapes(report, (
            'shape', 'kind', 'count', 'requests', 'total_ms', 'avg_ms', 'max_ms', 'slowest_query', 'call_site',
            'text',
        ), 'SHAPES')
    return lines + _next(report)


def _request(report):
    lines = ['request %s' % report['id']]
    lines += _fields(report, (
        'method', 'path', 'query_params', 'view', 'status', 'start_time', 'end_time', 'time_ms', 'queries_ms',
        'outside_queries_ms',
    ))
    lines.append('queries: %d (%s)' % (report['queries'], _KINDS % report['queries_by_kind']))

    if report['queries']:
        lines += _shapes(report, (
            'shape', 'kind', 'count', 'total_ms', 'avg_ms', 'max_ms', 'slowest_query', 'call_site', 'text',
        ), 'SHAPES')
    else:
        lines += ['', 'SHAPES', 'No queries were recorded for this request.']

    lines += ['', 'HINTS']
    if report['hints']:
        lines += ['- [{}] {}'.format(hint['hint'], hint['message']) for hint in report['hints']]
    else:
        lines.append('No hint applies.')

    if report['profiles']:
        lines += ['', 'PROFILES']
        lines += _table(report['profiles'], (
            'id', 'time_ms', 'queries', 'queries_ms', 'line', 'function', 'file', 'name',
        ))

    lines += ['', 'SECTIONS (printed only with their option)']
    lines += _table([
        {
            'section': row['section'], 'option': row['option'],
            'size': '%d %s' % (row['size'], row['unit']) if row['size'] is not None and row['unit'] else None,
        }
        for row in report['sections']
    ], ('option', 'section', 'size'))

    for title, key in (('REQUEST HEADERS', 'request_headers'), ('RESPONSE HEADERS', 'response_headers')):
        if key in report:
            lines += ['', title]
            if report[key] is None:
                lines.append('No response was recorded for this request.')
            else:
                lines += ['{}: {}'.format(name, _value(value)) for name, value in report[key].items()] or ['(none)']
    if 'request_body' in report:
        lines += _text_block(report, 'REQUEST BODY', 'request_body')
        binary = report.get('response_body_binary')
        note = 'No response was recorded for this request.'
        if binary:
            note = 'Not text, so not printed: content type {}, {} bytes.'.format(
                _value(binary['content_type']), _value(binary['bytes']),
            )
        lines += _text_block(report, 'RESPONSE BODY', 'response_body', note)
    if 'cprofile' in report:
        lines += _text_block(report, 'CPROFILE', 'cprofile', report.get('cprofile_note'))
    if 'curl' in report:
        lines += _text_block(report, 'CURL', 'curl')
    return lines + _next(report)


def _queries(report):
    lines = ['queries of request %s' % report['request']]
    lines += _fields(report, ('method', 'path', 'request_time_ms'))
    lines.append('request_queries: %d (%s)' % (report['request_queries'], _KINDS % report['queries_by_kind']))
    lines.append('matching: %d' % report['total'])
    lines += _fields(report, ('total_ms', 'sort'))
    lines += _filters(report)
    if 'empty' in report:
        if report['request_queries']:
            message = 'Nothing matched: no query of this request passes these filters.'
        else:
            message = 'Nothing matched: this request recorded no queries.'
        lines += _empty(report, message)
    else:
        lines.append('')
        if report['queries']:
            lines += _table(report['queries'], ('id', 'offset_ms', 'time_ms', 'kind', 'shape', 'call_site', 'text'))
        lines += _notices(report, 'queries')
    return lines + _next(report)


def _frames(frames):
    lines = []
    for frame in frames:
        lines.append('  ' + analysis.describe_frame(frame))
        if frame['source']:
            lines.append('    ' + frame['source'])
    return lines


def _query(report):
    lines = ['query %s' % report['id']]
    lines += _fields(report, ('request', 'kind', 'shape', 'start_time', 'offset_ms', 'time_ms'))
    if 'tables' in report:
        lines.append('tables: %s' % (', '.join(report['tables']) or MISSING))
        lines.append('joins: %d' % report['joins'])
    lines.append('call_site: %s' % analysis.describe_frame(report['call_site']))
    lines += _text_block(report, 'TEXT', 'text')
    lines += _text_block(report, 'PLAN', 'plan', report.get('plan_note'))

    if report['traceback_full']:
        lines += ['', 'TRACEBACK (every frame, innermost first)']
    else:
        lines += ['', 'TRACEBACK (application frames, innermost first)']
    if report['traceback']:
        lines += _frames(report['traceback'])
    elif 'traceback_raw' in report:
        lines.append(report['traceback_raw'])
    elif report['traceback_frames']:
        lines.append('No application frame: every recorded frame is library code.')
    else:
        lines.append('No traceback was recorded.')
    lines += _notices(report, 'traceback', 'traceback_raw')
    return lines + _next(report)


def _profiles(report):
    lines = ['profiles', 'matching: %d' % report['total']]
    lines += _filters(report)
    if 'empty' in report:
        if report['empty']['recorded_profiles']:
            message = 'Nothing matched: no recorded profile block passes these filters.'
        else:
            message = (
                'Nothing matched: no profile block has been recorded. Profile blocks come from the silk_profile '
                'decorator or context manager (silk.profiling.profiler.silk_profile) and from dynamic profiling '
                '(SILKY_DYNAMIC_PROFILING).'
            )
        lines += _empty(report, message)
        lines.append('recorded_profiles: %d' % report['empty']['recorded_profiles'])
    else:
        lines.append('')
        if report['profiles']:
            lines += _table(report['profiles'], (
                'id', 'request', 'time_ms', 'queries', 'queries_ms', 'line', 'function', 'file', 'name',
            ), {'request': short_id})
        lines += _notices(report, 'profiles')
    return lines + _next(report)


def _profile(report):
    lines = ['profile %s' % report['id']]
    lines += _fields(report, (
        'name', 'function', 'file', 'line', 'end_line', 'request', 'start_time', 'end_time', 'time_ms', 'queries_ms',
    ))
    lines.append('queries: %d (%s)' % (report['queries'], _KINDS % report['queries_by_kind']))
    lines += _fields(report, ('dynamic', 'exception_raised'))
    if report['queries']:
        lines += _shapes(report, (
            'shape', 'kind', 'count', 'total_ms', 'avg_ms', 'max_ms', 'slowest_query', 'call_site', 'text',
        ), 'SHAPES')
    else:
        lines += ['', 'SHAPES', 'No queries were recorded inside this profile block.']
    return lines + _next(report)


def _guide(report):
    return [report['text']]


_RENDERERS = {
    'guide': _guide,
    'summary': _summary,
    'requests': _requests,
    'request': _request,
    'queries': _queries,
    'query': _query,
    'shapes': _shape_list,
    'profiles': _profiles,
    'profile': _profile,
}
