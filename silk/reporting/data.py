"""Reads the recorded data and builds one report per ``silk_query`` subcommand.

A report is a dict of plain values. Its `truncated` list has an entry for every
cut made to keep the output bounded, and its `next` list holds follow-up
commands. Both renderers work from that dict, so text and JSON carry the same
data.
"""
import binascii
import json
import re
import textwrap
from collections import Counter
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone

import sqlparse
from django.conf import settings
from django.db.models import Case, Count, F, FloatField, IntegerField, Q, Sum, When
from django.db.models.functions import Coalesce, Length, Right, Substr
from django.utils import timezone

from silk.code_generation.curl import curl_cmd
from silk.models import Profile, Request, Response, SQLQuery
from silk.reporting import analysis

COMMAND = 'python manage.py silk_query'

LIST_LIMIT = 20
ROW_CHARS = 200
DETAIL_CHARS = 4000
SUMMARY_REQUESTS = 1000
SHAPES_REQUESTS = 50
SHORT_ID_CHARS = 8

REQUEST_SORTS = {
    'start': 'start_time',
    'time': 'time_taken',
    'queries': 'num_sql_queries',
    'query-time': 'queries_time',
    'path': 'path',
}
QUERY_SORTS = ('start', 'time')

_DURATION = re.compile(r'(\d+)\s*([smhdw])', re.I)
_DURATION_UNITS = {'s': 'seconds', 'm': 'minutes', 'h': 'hours', 'd': 'days', 'w': 'weeks'}
_STATUS_CLASS = re.compile(r'([1-5])xx', re.I)
_STATUS_CODE = re.compile(r'[1-5]\d\d')
_CHARSET = re.compile(r'charset=([\w-]+)', re.I)

# Enough of a query to tell its kind without loading its text.
_HEAD_CHARS = 64
_IDS_PER_QUERY = 500
_HAS_PLAN = Case(
    When(Q(analysis__isnull=True) | Q(analysis=''), then=0),
    default=1,
    output_field=IntegerField(),
)


class ReportError(Exception):
    """Invalid input. The message names the value and what is accepted."""


def iso(moment):
    """ISO 8601 in UTC, to the millisecond."""
    if moment is None:
        return None
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment)
    moment = moment.astimezone(datetime_timezone.utc)
    return moment.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def short_id(request_id):
    return request_id[:SHORT_ID_CHARS] if request_id else request_id


def parse_moment(value, option='--since'):
    """Turns `15m`, `2h`, `7d` (ago) or an ISO 8601 timestamp into a datetime to filter on."""
    text = str(value).strip()
    match = _DURATION.fullmatch(text)
    if match:
        unit = _DURATION_UNITS[match.group(2).lower()]
        return timezone.now() - timedelta(**{unit: int(match.group(1))})
    try:
        moment = datetime.fromisoformat(text[:-1] + '+00:00' if text.endswith(('Z', 'z')) else text)
    except ValueError:
        raise ReportError(
            'Invalid %s value %r. Use a duration such as 15m, 2h or 7d (units: s, m, h, d, w) '
            'or an ISO 8601 timestamp such as 2026-10-10T08:00:00Z.' % (option, value)
        )
    if timezone.is_naive(moment):
        moment = moment.replace(tzinfo=datetime_timezone.utc)
    if not settings.USE_TZ:
        moment = timezone.make_naive(moment)
    return moment


def parse_status(value):
    """Turns `404` or `5xx` into an inclusive (lowest, highest) pair of status codes."""
    text = str(value).strip()
    match = _STATUS_CLASS.fullmatch(text)
    if match:
        lowest = int(match.group(1)) * 100
        return lowest, lowest + 99
    if _STATUS_CODE.fullmatch(text):
        return int(text), int(text)
    raise ReportError(
        'Invalid --status value %r. Use a status code such as 404 or a status class: 1xx, 2xx, 3xx, 4xx or 5xx.'
        % (value,)
    )


def _with_queries_time(requests):
    if 'queries_time' in requests.query.annotations:
        return requests
    return requests.annotate(
        queries_time=Coalesce(Sum('queries__time_taken'), 0.0, output_field=FloatField())
    )


def filter_requests(filters=None):
    """Returns the recorded requests that pass every given filter."""
    filters = filters or {}
    requests = Request.objects.all()
    if filters.get('path') is not None:
        requests = requests.filter(path=filters['path'])
    if filters.get('path_contains') is not None:
        requests = requests.filter(path__contains=filters['path_contains'])
    if filters.get('view') is not None:
        requests = requests.filter(view_name=filters['view'])
    if filters.get('method') is not None:
        requests = requests.filter(method=str(filters['method']).upper())
    if filters.get('status') is not None:
        lowest, highest = parse_status(filters['status'])
        requests = requests.filter(response__status_code__gte=lowest, response__status_code__lte=highest)
    if filters.get('since') is not None:
        requests = requests.filter(start_time__gte=parse_moment(filters['since'], '--since'))
    if filters.get('until') is not None:
        requests = requests.filter(start_time__lte=parse_moment(filters['until'], '--until'))
    if filters.get('min_time') is not None:
        requests = requests.filter(time_taken__gte=filters['min_time'])
    if filters.get('min_queries') is not None:
        requests = requests.filter(num_sql_queries__gte=filters['min_queries'])
    if filters.get('min_query_time') is not None:
        requests = _with_queries_time(requests).filter(queries_time__gte=filters['min_query_time'])
    return requests


def resolve_request_id(value):
    """Returns the full id of the request whose id is `value` or starts with it."""
    value = str(value).strip()
    matches = []
    if value:
        if Request.objects.filter(pk=value).exists():
            return value
        matches = list(
            Request.objects.filter(pk__startswith=value).order_by('-start_time', 'pk').values_list('pk', flat=True)[:11]
        )
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ReportError('No request matches id {!r}. List requests with `{} requests`.'.format(value, COMMAND))
    count = 'more than 10 requests, among them' if len(matches) > 10 else '%d requests:' % len(matches)
    raise ReportError(
        'Request id prefix %r is ambiguous, it matches %s %s. Use more characters of the id.'
        % (value, count, ', '.join(matches[:10]))
    )


def query_stats(request_ids, kinds=True):
    """Returns {request id: {'time_ms': ..., 'kinds': Counter}} for the given requests.

    One grouped query per 500 requests. Query text and tracebacks are not loaded:
    the kind is told from the start of the text.
    """
    stats = {}
    request_ids = list(request_ids)
    for start in range(0, len(request_ids), _IDS_PER_QUERY):
        queries = SQLQuery.objects.filter(request_id__in=request_ids[start:start + _IDS_PER_QUERY])
        if kinds:
            queries = queries.annotate(head=Substr('query', 1, _HEAD_CHARS), has_plan=_HAS_PLAN)
            queries = queries.values_list('request_id', 'head', 'has_plan')
        else:
            queries = queries.values_list('request_id')
        for row in queries.annotate(total=Sum('time_taken'), count=Count('pk')).order_by():
            stat = stats.setdefault(row[0], {'time_ms': 0.0, 'kinds': Counter()})
            stat['time_ms'] += row[-2] or 0.0
            if kinds:
                stat['kinds'][analysis.kind(row[1], row[2])] += row[-1]
    return stats


def _kinds(counter):
    return {kind: counter.get(kind, 0) for kind in analysis.KINDS}


def _report(name, /, **fields):
    report = {'report': name}
    report.update(fields)
    report['truncated'] = []
    report['next'] = []
    return report


def _given(filters):
    return {name: value for name, value in (filters or {}).items() if value is not None}


def _recorded():
    latest = Request.objects.order_by('-start_time').values_list('start_time', flat=True).first()
    return {'recorded_requests': Request.objects.count(), 'latest_request_start': iso(latest)}


def _chars(max_chars, default):
    return default if max_chars is None else max_chars


def _clip(report, key, text, max_chars, default=DETAIL_CHARS):
    """Cuts a long text and records the cut. A limit of 0 keeps everything."""
    text = text or ''
    limit = _chars(max_chars, default)
    if limit and len(text) > limit:
        report['truncated'].append({
            'key': key, 'shown': limit, 'total': len(text), 'unit': 'chars',
            'option': '--max-chars 0 prints it whole',
        })
        return text[:limit]
    return text


def _clip_rows(report, key, rows, max_chars):
    """Cuts the text of list rows; each row keeps its full length in `text_chars`."""
    limit = _chars(max_chars, ROW_CHARS)
    cut = 0
    longest = 0
    for row in rows:
        row['text_chars'] = len(row['text'])
        if limit and len(row['text']) > limit:
            cut += 1
            longest = max(longest, len(row['text']))
            row['text'] = row['text'][:limit]
    if cut:
        report['truncated'].append({
            'key': key + '.text', 'shown': limit, 'total': longest, 'unit': 'chars', 'rows': cut,
            'option': '--max-chars N shows more (0 for all)',
        })


def _note_rows(report, key, shown, total, offset):
    if shown < total:
        following = offset + shown
        option = '--limit N shows more (0 for all)'
        if shown and following < total:
            option = '--offset %d shows the next rows, %s' % (following, option)
        report['truncated'].append({
            'key': key, 'shown': shown, 'total': total, 'unit': 'rows', 'offset': offset, 'option': option,
        })


def _slice(rows, limit, offset):
    return rows[offset:offset + limit] if limit else rows[offset:]


def _page(report, key, rows, limit, offset):
    page = _slice(rows, limit, offset)
    _note_rows(report, key, len(page), len(rows), offset)
    return page


def _executions(queries, tracebacks=False):
    """Yields one dict per query execution, with its kind and shape.

    The traceback is loaded only when asked for, and only to take the call
    site from it.
    """
    fields = ['pk', 'request_id', 'query', 'start_time', 'time_taken', 'has_plan']
    if tracebacks:
        fields.append('traceback')
    for row in queries.annotate(has_plan=_HAS_PLAN).values_list(*fields).iterator():
        kind = analysis.kind(row[2], row[5])
        shape, shape_text = analysis.shape(row[2], kind)
        execution = {
            'id': row[0],
            'request': row[1],
            'text': row[2],
            'start_time': row[3],
            'time_ms': row[4],
            'kind': kind,
            'shape': shape,
            'shape_text': shape_text,
        }
        if tracebacks:
            execution['call_site'] = analysis.call_site(row[6])
        yield execution


def _shape_rows(report, shapes, limit, offset, max_chars, columns):
    rows = [{column: shape[column] for column in columns} for shape in _page(report, 'shapes', shapes, limit, offset)]
    _clip_rows(report, 'shapes', rows, max_chars)
    return rows


def _profile_rows(profiles, limit=0, offset=0):
    profiles = profiles.annotate(query_count=Count('queries'), queries_time=Sum('queries__time_taken'))
    return [
        {
            'id': row['pk'],
            'name': row['name'] or None,
            'function': row['func_name'] or None,
            'file': row['file_path'] or None,
            'line': row['line_num'],
            'time_ms': analysis.ms(row['time_taken']),
            'queries': row['query_count'],
            'queries_ms': analysis.ms(row['queries_time'] or 0.0),
            'request': row['request_id'],
        }
        for row in _slice(profiles.values(
            'pk', 'name', 'func_name', 'file_path', 'line_num', 'time_taken', 'request_id', 'query_count',
            'queries_time',
        ), limit, offset)
    ]


def _filter_arguments(filters):
    """The given filters as command-line arguments, to carry them into follow-up commands."""
    arguments = ''
    for name, value in _given(filters).items():
        text = str(value)
        if not re.fullmatch(r'[\w:./@%+-]+', text):
            text = "'%s'" % text.replace("'", "'\\''")
        arguments += ' --{} {}'.format(name.replace('_', '-'), text)
    return arguments


def request_list(filters=None, sort='start', ascending=False, limit=LIST_LIMIT, offset=0):
    """The `requests` report: one row per request."""
    if sort not in REQUEST_SORTS:
        raise ReportError('Invalid --sort value {!r}. Choose from: {}.'.format(sort, ', '.join(REQUEST_SORTS)))
    requests = filter_requests(filters)
    total = requests.count()
    if sort == 'query-time':
        # Joins and groups the queries of every matching request, so it is
        # only done when the order depends on it.
        requests = _with_queries_time(requests)
    order = F(REQUEST_SORTS[sort])
    if sort == 'time':
        order = order.asc(nulls_last=True) if ascending else order.desc(nulls_last=True)
    else:
        order = order.asc() if ascending else order.desc()
    ordering = [order, 'pk'] if sort == 'start' else [order, '-start_time', 'pk']
    page = list(_slice(requests.order_by(*ordering).values(
        'pk', 'start_time', 'method', 'path', 'view_name', 'time_taken', 'num_sql_queries', 'response__status_code',
    ), limit, offset))
    stats = query_stats([row['pk'] for row in page])

    report = _report(
        'requests', filters=_given(filters), sort=sort, order='asc' if ascending else 'desc',
        total=total, offset=offset, requests=[],
    )
    for row in page:
        stat = stats.get(row['pk'], {'time_ms': 0.0, 'kinds': Counter()})
        entry = {
            'id': row['pk'],
            'start_time': iso(row['start_time']),
            'method': row['method'],
            'status': row['response__status_code'],
            'time_ms': analysis.ms(row['time_taken']),
            'queries_ms': analysis.ms(stat['time_ms']),
            'queries': row['num_sql_queries'],
        }
        entry.update(_kinds(stat['kinds']))
        entry['view'] = row['view_name'] or None
        entry['path'] = row['path']
        report['requests'].append(entry)
    _note_rows(report, 'requests', len(page), total, offset)

    if not total:
        report['empty'] = _recorded()
        if report['empty']['recorded_requests'] and report['filters']:
            report['next'].append('%s requests' % COMMAND)
        else:
            report['next'].append('%s guide' % COMMAND)
    elif page:
        report['next'].append('{} request {}'.format(COMMAND, short_id(page[0]['pk'])))
        if sort != 'time':
            report['next'].append('{} requests{} --sort time'.format(COMMAND, _filter_arguments(filters)))
        report['next'].append('{} summary{}'.format(COMMAND, _filter_arguments(filters)))
    else:
        report['next'].append('{} requests{}'.format(COMMAND, _filter_arguments(filters)))
    return report


def summary(filters=None, max_requests=SUMMARY_REQUESTS, limit=LIST_LIMIT, offset=0):
    """The `summary` report: totals and per-view statistics over the most recent matching requests."""
    requests = filter_requests(filters)
    matching = requests.count()
    recent = requests.order_by('-start_time', 'pk').values_list(
        'pk', 'view_name', 'time_taken', 'num_sql_queries', 'start_time',
    )
    rows = list(_slice(recent, max_requests, 0))
    stats = query_stats([row[0] for row in rows], kinds=False)

    views = {}
    for pk, view_name, time_taken, num_queries, _ in rows:
        view = views.setdefault(view_name or None, {'times': [], 'queries': 0, 'queries_ms': 0.0, 'requests': 0})
        view['requests'] += 1
        view['times'].append(time_taken)
        view['queries'] += num_queries
        view['queries_ms'] += stats.get(pk, {'time_ms': 0.0})['time_ms']
    view_rows = []
    for name, view in views.items():
        times = [value for value in view['times'] if value is not None]
        view_rows.append({
            'requests': view['requests'],
            'median_ms': analysis.ms(analysis.median(times)),
            'p95_ms': analysis.ms(analysis.percentile(times, 95)),
            'max_ms': analysis.ms(max(times)) if times else None,
            'total_ms': analysis.ms(sum(times)) if times else None,
            'avg_queries': round(view['queries'] / view['requests'], 1),
            'avg_queries_ms': analysis.ms(view['queries_ms'] / view['requests']),
            'view': name,
        })
    view_rows.sort(key=lambda row: (-(row['total_ms'] or 0.0), row['view'] or ''))

    times = [row[2] for row in rows if row[2] is not None]
    starts = [row[4] for row in rows]
    report = _report(
        'summary', filters=_given(filters), requests_matching=matching, requests_analysed=len(rows),
        start_time_from=iso(min(starts)) if starts else None,
        start_time_to=iso(max(starts)) if starts else None,
        median_ms=analysis.ms(analysis.median(times)),
        p95_ms=analysis.ms(analysis.percentile(times, 95)),
        max_ms=analysis.ms(max(times)) if times else None,
        views_total=len(view_rows),
    )
    if len(rows) < matching:
        report['truncated'].append({
            'key': 'requests_analysed', 'shown': len(rows), 'total': matching, 'unit': 'requests',
            'option': 'these are the most recent ones, --max-requests N analyses more (0 for all)',
        })
    report['views'] = _page(report, 'views', view_rows, limit, offset)

    arguments = _filter_arguments(filters)
    if not matching:
        report['empty'] = _recorded()
        report['next'].append('{} {}'.format(COMMAND, 'summary' if report['empty']['recorded_requests'] else 'guide'))
    else:
        top = next((row['view'] for row in report['views'] if row['view']), None)
        if top and not (filters or {}).get('view'):
            report['next'].append('{} requests{} --view {} --sort time'.format(COMMAND, arguments, top))
            report['next'].append('{} shapes{} --view {}'.format(COMMAND, arguments, top))
        else:
            report['next'].append('{} requests{} --sort time'.format(COMMAND, arguments))
            report['next'].append('{} shapes{}'.format(COMMAND, arguments))
    return report


def shape_list(filters=None, max_requests=SHAPES_REQUESTS, limit=LIST_LIMIT, offset=0, max_chars=None):
    """The `shapes` report: query shapes aggregated over the most recent matching requests."""
    requests = filter_requests(filters)
    matching = requests.count()
    request_ids = list(_slice(requests.order_by('-start_time', 'pk').values_list('pk', flat=True), max_requests, 0))

    def executions():
        # Text and timings only. Tracebacks are large and one per shape is enough.
        for start in range(0, len(request_ids), _IDS_PER_QUERY):
            yield from _executions(SQLQuery.objects.filter(request_id__in=request_ids[start:start + _IDS_PER_QUERY]))

    shapes = analysis.group_shapes(executions())
    report = _report(
        'shapes', filters=_given(filters), requests_matching=matching, requests_analysed=len(request_ids),
        queries_analysed=sum(shape['count'] for shape in shapes), shapes_total=len(shapes),
    )
    if len(request_ids) < matching:
        report['truncated'].append({
            'key': 'requests_analysed', 'shown': len(request_ids), 'total': matching, 'unit': 'requests',
            'option': 'these are the most recent ones, --max-requests N analyses more (0 for all)',
        })
    report['shapes'] = _shape_rows(report, shapes, limit, offset, max_chars, (
        'shape', 'kind', 'count', 'requests', 'total_ms', 'avg_ms', 'max_ms', 'slowest_query', 'slowest_request',
        'call_site', 'text',
    ))
    # The call site of a shape is that of its slowest execution.
    slowest = [row['slowest_query'] for row in report['shapes']]
    tracebacks = dict(SQLQuery.objects.filter(pk__in=slowest).values_list('pk', 'traceback')) if slowest else {}
    for row in report['shapes']:
        row['call_site'] = analysis.call_site(tracebacks.get(row['slowest_query']))

    if not matching:
        report['empty'] = _recorded()
        report['next'].append('{} {}'.format(COMMAND, 'shapes' if report['empty']['recorded_requests'] else 'guide'))
    elif report['shapes']:
        top = report['shapes'][0]
        if top['slowest_request']:
            report['next'].append(
                '{} queries {} --shape {}'.format(COMMAND, short_id(top['slowest_request']), top['shape'])
            )
        report['next'].append('{} query {}'.format(COMMAND, top['slowest_query']))
    else:
        report['next'].append('{} requests{}'.format(COMMAND, _filter_arguments(filters)))
    return report


def _headers(encoded):
    if not encoded:
        return {}
    try:
        headers = json.loads(encoded)
    except ValueError:
        return {}
    return headers if isinstance(headers, dict) else {}


def _header(headers, name):
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _response_body(report, response_id, max_chars):
    """Adds the response body to the report, or its content type and size when it is not text."""
    response = Response.objects.only('body', 'raw_body', 'encoded_headers').get(pk=response_id)
    if response.body:
        report['response_body'] = _clip(report, 'response_body', response.body, max_chars)
        return
    content_type = response.content_type
    try:
        raw = response.raw_body_decoded
    except (binascii.Error, ValueError):
        raw = None
    text = None
    if raw is not None:
        charset = _CHARSET.search(content_type or '')
        try:
            text = raw.decode(charset.group(1) if charset else 'utf-8')
        except (UnicodeDecodeError, LookupError):
            text = None
    if text is None or '\x00' in text:
        report['response_body'] = None
        report['response_body_binary'] = {
            'content_type': content_type, 'bytes': len(raw) if raw is not None else None,
        }
    else:
        report['response_body'] = _clip(report, 'response_body', text, max_chars)


def request_detail(request_id, limit=LIST_LIMIT, offset=0, max_chars=None, headers=False, bodies=False,
                   cprofile=False, curl=False, base_url=None):
    """The `request` report: one request, its query shapes, hints and profiles."""
    pk = resolve_request_id(request_id)
    request = Request.objects.filter(pk=pk).annotate(
        body_chars=Length('body'), raw_body_chars=Length('raw_body'), pyprofile_chars=Length('pyprofile'),
    ).values(
        'path', 'query_params', 'method', 'start_time', 'end_time', 'view_name', 'time_taken', 'encoded_headers',
        'body_chars', 'raw_body_chars', 'pyprofile_chars',
    ).get()
    response = Response.objects.filter(request_id=pk).annotate(
        raw_body_chars=Length('raw_body'), raw_body_tail=Right('raw_body', 2),
    ).values('pk', 'status_code', 'encoded_headers', 'raw_body_chars', 'raw_body_tail').first()

    # Every traceback of the request is read, one at a time, for the call sites.
    executions = list(_executions(SQLQuery.objects.filter(request_id=pk).order_by('start_time', 'pk'), tracebacks=True))
    time_ms = request['time_taken']
    queries_ms = sum(execution['time_ms'] or 0.0 for execution in executions)
    shapes = analysis.group_shapes(executions)
    try:
        query_params = json.loads(request['query_params']) if request['query_params'] else None
    except ValueError:
        query_params = request['query_params']

    report = _report(
        'request', id=pk, method=request['method'], path=request['path'], query_params=query_params,
        view=request['view_name'] or None, status=response['status_code'] if response else None,
        start_time=iso(request['start_time']), end_time=iso(request['end_time']),
        time_ms=analysis.ms(time_ms), queries_ms=analysis.ms(queries_ms),
        outside_queries_ms=None if time_ms is None else analysis.ms(max(time_ms - queries_ms, 0.0)),
        queries=len(executions), queries_by_kind=_kinds(Counter(execution['kind'] for execution in executions)),
        shapes_total=len(shapes),
    )
    report['shapes'] = _shape_rows(report, shapes, limit, offset, max_chars, (
        'shape', 'kind', 'count', 'total_ms', 'avg_ms', 'max_ms', 'slowest_query', 'call_site', 'text',
    ))
    report['hints'] = analysis.hints(time_ms, executions, bool(request['pyprofile_chars']))
    report['profiles'] = _profile_rows(Profile.objects.filter(request_id=pk).order_by('start_time', 'pk'))

    request_headers = _headers(request['encoded_headers'])
    response_headers = _headers(response['encoded_headers']) if response else {}
    response_bytes = None
    if response:
        # The raw body is stored base64 encoded.
        response_bytes = response['raw_body_chars'] * 3 // 4 - (response['raw_body_tail'] or '').count('=')
    report['sections'] = [
        {'section': 'request_headers', 'option': '--headers', 'size': len(request_headers), 'unit': 'headers'},
        {'section': 'response_headers', 'option': '--headers',
         'size': len(response_headers) if response else None, 'unit': 'headers'},
        {'section': 'request_body', 'option': '--bodies',
         'size': request['body_chars'] or request['raw_body_chars'], 'unit': 'chars'},
        {'section': 'response_body', 'option': '--bodies', 'size': response_bytes, 'unit': 'bytes'},
        {'section': 'cprofile', 'option': '--cprofile', 'size': request['pyprofile_chars'], 'unit': 'chars'},
        {'section': 'curl', 'option': '--curl', 'size': None, 'unit': None},
    ]

    if headers:
        report['request_headers'] = request_headers
        report['response_headers'] = response_headers if response else None
    if bodies or curl:
        body, raw_body = Request.objects.filter(pk=pk).values_list('body', 'raw_body').get()
    if bodies:
        report['request_body'] = _clip(report, 'request_body', body or raw_body, max_chars)
        if response:
            _response_body(report, response['pk'], max_chars)
        else:
            report['response_body'] = None
    if cprofile:
        if request['pyprofile_chars']:
            pyprofile = Request.objects.filter(pk=pk).values_list('pyprofile', flat=True).get()
            report['cprofile'] = _clip(report, 'cprofile', pyprofile, max_chars)
        else:
            report['cprofile'] = None
            report['cprofile_note'] = (
                'No profiler output was recorded for this request. '
                'Set SILKY_PYTHON_PROFILER = True in the Django settings to record it for new requests.'
            )
    if curl:
        try:
            curl_body = json.loads(raw_body)
        except (ValueError, TypeError):
            curl_body = raw_body
        host = _header(request_headers, 'host')
        base = base_url or 'http://%s' % (host or 'localhost')
        command = curl_cmd(
            url=base.rstrip('/') + request['path'], method=request['method'],
            query_params=query_params if isinstance(query_params, dict) else None,
            # Without a body the content type would leave curl a `-d` with nothing to send.
            body=curl_body, content_type=_header(request_headers, 'content-type') if raw_body else None,
        )
        report['curl'] = _clip(report, 'curl', command.strip(), max_chars)

    if report['shapes']:
        report['next'].append('{} queries {} --shape {}'.format(COMMAND, pk, report['shapes'][0]['shape']))
    if executions:
        report['next'].append('{} queries {} --sort time'.format(COMMAND, pk))
    if request['pyprofile_chars'] and not cprofile:
        report['next'].append('{} request {} --cprofile'.format(COMMAND, pk))
    if report['profiles']:
        report['next'].append('{} profile {}'.format(COMMAND, report['profiles'][0]['id']))
    if not (headers or bodies):
        report['next'].append('{} request {} --headers --bodies'.format(COMMAND, pk))
    return report


def request_queries(request_id, sort='start', shape=None, kind=None, min_time=None, limit=LIST_LIMIT, offset=0,
                    max_chars=None):
    """The `queries` report: the query executions of one request."""
    if sort not in QUERY_SORTS:
        raise ReportError('Invalid --sort value {!r}. Choose from: {}.'.format(sort, ', '.join(QUERY_SORTS)))
    if kind is not None and kind not in analysis.KINDS:
        raise ReportError('Invalid --kind value {!r}. Choose from: {}.'.format(kind, ', '.join(analysis.KINDS)))
    pk = resolve_request_id(request_id)
    request = Request.objects.filter(pk=pk).values('method', 'path', 'start_time', 'time_taken').get()
    # Kind and shape come from the text, so the filters run here and the
    # tracebacks are read for the rows that are shown only.
    executions = list(_executions(SQLQuery.objects.filter(request_id=pk)))
    matching = [
        execution for execution in executions
        if (shape is None or execution['shape'] == shape)
        and (kind is None or execution['kind'] == kind)
        and (min_time is None or (execution['time_ms'] is not None and execution['time_ms'] >= min_time))
    ]
    if sort == 'time':
        matching.sort(key=lambda execution: (-(execution['time_ms'] or 0.0), execution['id']))
    else:
        matching.sort(key=lambda execution: (
            execution['start_time'] is None, execution['start_time'] or request['start_time'], execution['id'],
        ))

    report = _report(
        'queries', request=pk, method=request['method'], path=request['path'],
        request_time_ms=analysis.ms(request['time_taken']), request_queries=len(executions),
        queries_by_kind=_kinds(Counter(execution['kind'] for execution in executions)),
        filters=_given({'shape': shape, 'kind': kind, 'min_time': min_time}), sort=sort,
        total=len(matching), total_ms=analysis.ms(sum(execution['time_ms'] or 0.0 for execution in matching)),
        offset=offset,
    )
    page = _page(report, 'queries', matching, limit, offset)
    tracebacks = {}
    if page:
        tracebacks = dict(
            SQLQuery.objects.filter(pk__in=[execution['id'] for execution in page]).values_list('pk', 'traceback')
        )
    report['queries'] = []
    for execution in page:
        offset_ms = None
        if execution['start_time'] is not None:
            offset_ms = analysis.ms((execution['start_time'] - request['start_time']).total_seconds() * 1000)
        report['queries'].append({
            'id': execution['id'],
            'offset_ms': offset_ms,
            'time_ms': analysis.ms(execution['time_ms']),
            'kind': execution['kind'],
            'shape': execution['shape'],
            'call_site': analysis.call_site(tracebacks.get(execution['id'])),
            'text': ' '.join(execution['text'].split()),
        })
    _clip_rows(report, 'queries', report['queries'], max_chars)

    if not matching:
        report['empty'] = _recorded()
        report['next'].append('{} queries {}'.format(COMMAND, pk) if executions else '{} request {}'.format(COMMAND, pk))
    elif page:
        report['next'].append('{} query {}'.format(COMMAND, page[0]['id']))
        report['next'].append('{} request {}'.format(COMMAND, pk))
    else:
        report['next'].append('{} queries {}'.format(COMMAND, pk))
    return report


def _integer_id(value, what, listing):
    try:
        return int(str(value).strip())
    except ValueError:
        raise ReportError(
            'Invalid {} id {!r}. A {} id is an integer; list them with `{} {}`.'.format(what, value, what, COMMAND, listing)
        )


def query_detail(query_id, full_traceback=False, max_chars=None):
    """The `query` report: one query execution with its plan and traceback."""
    pk = _integer_id(query_id, 'query', 'queries <request-id>')
    query = SQLQuery.objects.filter(pk=pk).first()
    if query is None:
        raise ReportError(
            'No query has id {}. List the queries of a request with `{} queries <request-id>`.'.format(pk, COMMAND)
        )
    kind = analysis.kind(query.query, bool(query.analysis))
    shape, _ = analysis.shape(query.query, kind)
    request_start = None
    if query.request_id:
        request_start = Request.objects.filter(pk=query.request_id).values_list('start_time', flat=True).first()
    offset_ms = None
    if request_start is not None and query.start_time is not None:
        offset_ms = analysis.ms((query.start_time - request_start).total_seconds() * 1000)

    report = _report(
        'query', id=query.pk, request=query.request_id, kind=kind, shape=shape,
        start_time=iso(query.start_time), offset_ms=offset_ms, time_ms=analysis.ms(query.time_taken),
    )
    if kind == analysis.SQL:
        report['tables'] = list(dict.fromkeys(query.tables_involved))
        report['joins'] = query.num_joins
        text = sqlparse.format(query.query, reindent=True, keyword_case='upper')
    else:
        text = textwrap.dedent(query.query).strip()
    report['text'] = _clip(report, 'text', text, max_chars)
    if query.analysis:
        report['plan'] = _clip(report, 'plan', query.analysis, max_chars)
    else:
        report['plan'] = None
        if kind == analysis.CYPHER:
            report['plan_note'] = 'No plan is recorded for Cypher queries: silk runs EXPLAIN for SQL only.'
        else:
            report['plan_note'] = 'No EXPLAIN output was recorded for this query.'

    frames = analysis.parse_traceback(query.traceback)
    shown = frames if full_traceback else analysis.application_frames(frames)
    report['call_site'] = analysis.call_site(query.traceback)
    report['traceback_full'] = bool(full_traceback)
    report['traceback_frames'] = len(frames)
    report['traceback'] = shown
    if len(shown) < len(frames):
        report['truncated'].append({
            'key': 'traceback', 'shown': len(shown), 'total': len(frames), 'unit': 'frames',
            'option': 'application frames only, --full-traceback prints every frame',
        })
    if not frames and (query.traceback or '').strip():
        # Not in the format silk records; shown as it is stored.
        report['traceback_raw'] = _clip(report, 'traceback_raw', query.traceback.strip(), max_chars)

    if query.request_id:
        report['next'].append('{} queries {} --shape {}'.format(COMMAND, query.request_id, shape))
        report['next'].append('{} request {}'.format(COMMAND, query.request_id))
    else:
        report['next'].append('%s requests' % COMMAND)
    return report


def profile_list(name=None, function=None, request=None, limit=LIST_LIMIT, offset=0):
    """The `profiles` report: recorded silk_profile blocks, most recent first."""
    profiles = Profile.objects.all()
    request_id = resolve_request_id(request) if request is not None else None
    if name is not None:
        profiles = profiles.filter(name__icontains=name)
    if function is not None:
        profiles = profiles.filter(func_name__icontains=function)
    if request_id is not None:
        profiles = profiles.filter(request_id=request_id)
    total = profiles.count()
    report = _report(
        'profiles', filters=_given({'name': name, 'function': function, 'request': request_id}),
        total=total, offset=offset,
    )
    report['profiles'] = _profile_rows(profiles.order_by('-start_time', '-pk'), limit, offset)
    _note_rows(report, 'profiles', len(report['profiles']), total, offset)

    if not total:
        report['empty'] = _recorded()
        report['empty']['recorded_profiles'] = Profile.objects.count()
        report['next'].append('{} {}'.format(COMMAND, 'profiles' if report['filters'] else 'requests'))
    elif report['profiles']:
        first = report['profiles'][0]
        report['next'].append('{} profile {}'.format(COMMAND, first['id']))
        if first['request']:
            report['next'].append('{} request {}'.format(COMMAND, short_id(first['request'])))
    else:
        report['next'].append('%s profiles' % COMMAND)
    return report


def profile_detail(profile_id, limit=LIST_LIMIT, offset=0, max_chars=None):
    """The `profile` report: one silk_profile block and the shapes of its queries."""
    pk = _integer_id(profile_id, 'profile', 'profiles')
    rows = _profile_rows(Profile.objects.filter(pk=pk))
    if not rows:
        raise ReportError('No profile has id {}. List profiles with `{} profiles`.'.format(pk, COMMAND))
    profile = Profile.objects.filter(pk=pk).values(
        'start_time', 'end_time', 'end_line_num', 'exception_raised', 'dynamic',
    ).get()
    executions = list(_executions(SQLQuery.objects.filter(profiles=pk).order_by('start_time', 'pk'), tracebacks=True))
    shapes = analysis.group_shapes(executions)

    report = _report('profile', **rows[0])
    report.update(
        end_line=profile['end_line_num'], start_time=iso(profile['start_time']), end_time=iso(profile['end_time']),
        dynamic=profile['dynamic'], exception_raised=profile['exception_raised'],
        queries_by_kind=_kinds(Counter(execution['kind'] for execution in executions)),
        shapes_total=len(shapes),
    )
    report['shapes'] = _shape_rows(report, shapes, limit, offset, max_chars, (
        'shape', 'kind', 'count', 'total_ms', 'avg_ms', 'max_ms', 'slowest_query', 'call_site', 'text',
    ))
    if report['shapes']:
        report['next'].append('{} query {}'.format(COMMAND, report['shapes'][0]['slowest_query']))
    if report['request']:
        report['next'].append('{} request {}'.format(COMMAND, report['request']))
    report['next'].append('%s profiles' % COMMAND)
    return report
