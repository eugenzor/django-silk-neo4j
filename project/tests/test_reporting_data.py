import base64
import json
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from silk.models import Profile, Request, Response, SQLQuery
from silk.reporting import analysis, data

from .data import tracebacks
from .factories import (
    ProfileFactory,
    RequestMinFactory,
    ResponseFactory,
    recorded_request,
    timed_profile,
    timed_query,
)

SQL = 'SELECT "a"."id" FROM "a" WHERE "a"."id" = %d LIMIT 21'
CYPHER = "MATCH (o) WHERE elementId(o)='%d' RETURN o"


def ids(report, key='requests'):
    return [row['id'] for row in report[key]]


class TestFactories(TestCase):

    def test_request_with_response_queries_and_profile(self):
        request = recorded_request(time_ms=250, status_code=201, path='/a/', method='GET')
        sql = timed_query(request, SQL % 1, 10, offset_ms=5, analysis='Seq Scan on a')
        cypher = timed_query(request, CYPHER % 1, 20, offset_ms=30, traceback=tracebacks.NEOMODEL)
        profile = timed_profile(request, 40, queries=[sql, cypher], name='block')

        request = Request.objects.get(pk=request.pk)
        self.assertEqual(request.time_taken, 250)
        self.assertEqual(request.response.status_code, 201)
        self.assertEqual(request.num_sql_queries, 2)
        self.assertEqual(
            list(SQLQuery.objects.order_by('start_time').values_list('time_taken', flat=True)), [10, 20],
        )
        self.assertEqual(sql.start_time - request.start_time, timedelta(milliseconds=5))
        profile = Profile.objects.get(pk=profile.pk)
        self.assertEqual(profile.time_taken, 40)
        self.assertEqual(profile.queries.count(), 2)
        self.assertEqual(profile.request_id, request.pk)

    def test_request_without_response(self):
        request = recorded_request(time_ms=None, status_code=None)
        request = Request.objects.get(pk=request.pk)
        self.assertIsNone(request.time_taken)
        self.assertFalse(Response.objects.filter(request=request).exists())

    def test_profile_factory(self):
        profile = ProfileFactory()
        self.assertEqual(Profile.objects.get(pk=profile.pk).func_name, 'do_work')


class TestParsing(TestCase):

    def test_duration(self):
        now = timezone.now()
        with mock.patch('django.utils.timezone.now', return_value=now):
            self.assertEqual(data.parse_moment('15m'), now - timedelta(minutes=15))
            self.assertEqual(data.parse_moment('2h'), now - timedelta(hours=2))
            self.assertEqual(data.parse_moment('7d'), now - timedelta(days=7))
            self.assertEqual(data.parse_moment('30s'), now - timedelta(seconds=30))
            self.assertEqual(data.parse_moment('1w'), now - timedelta(weeks=1))

    def test_timestamp(self):
        expected = timezone.datetime(2026, 10, 10, 8, 0, tzinfo=timezone.timezone.utc)
        self.assertEqual(data.parse_moment('2026-10-10T08:00:00Z'), expected)
        self.assertEqual(data.parse_moment('2026-10-10T10:00:00+02:00'), expected)
        # A timestamp without an offset is UTC.
        self.assertEqual(data.parse_moment('2026-10-10 08:00:00'), expected)
        self.assertEqual(data.parse_moment('2026-10-10'), expected.replace(hour=0))

    def test_invalid_moment(self):
        for value in ('yesterday', '15', '2x', ''):
            with self.subTest(value=value):
                with self.assertRaises(data.ReportError) as raised:
                    data.parse_moment(value, '--until')
                self.assertIn('--until', str(raised.exception))
                self.assertIn('15m, 2h or 7d', str(raised.exception))
                self.assertIn('ISO 8601', str(raised.exception))

    def test_status(self):
        self.assertEqual(data.parse_status('404'), (404, 404))
        self.assertEqual(data.parse_status('5xx'), (500, 599))
        self.assertEqual(data.parse_status('2XX'), (200, 299))

    def test_invalid_status(self):
        for value in ('abc', '6xx', '99', '4x'):
            with self.subTest(value=value):
                with self.assertRaises(data.ReportError) as raised:
                    data.parse_status(value)
                self.assertIn(repr(value), str(raised.exception))
                self.assertIn('5xx', str(raised.exception))

    def test_iso(self):
        moment = timezone.datetime(2026, 10, 10, 12, 30, 1, 500000, tzinfo=timezone.timezone(timedelta(hours=2)))
        self.assertEqual(data.iso(moment), '2026-10-10T10:30:01.500Z')
        self.assertIsNone(data.iso(None))


class TestRequestFilters(TestCase):

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.now = now
        cls.fast = recorded_request(
            time_ms=50, status_code=200, path='/api/boards/', method='GET', view_name='boards:list',
            start_time=now - timedelta(minutes=5),
        )
        cls.slow = recorded_request(
            time_ms=900, status_code=200, path='/api/training/openings/', method='GET',
            view_name='training:openings', start_time=now - timedelta(hours=3),
        )
        cls.failed = recorded_request(
            time_ms=300, status_code=503, path='/api/training/progress/', method='POST',
            view_name='training:progress', start_time=now - timedelta(days=2),
        )
        cls.missing = recorded_request(
            time_ms=20, status_code=404, path='/nothing/', method='GET', view_name='',
            start_time=now - timedelta(days=10),
        )
        cls.unfinished = recorded_request(
            time_ms=None, status_code=None, path='/api/boards/', method='GET', view_name='boards:list',
            start_time=now - timedelta(days=20),
        )
        for n in range(3):
            timed_query(cls.slow, CYPHER % n, 100)
        timed_query(cls.failed, SQL % 1, 250)
        timed_query(cls.fast, SQL % 2, 5)

    def filtered(self, **filters):
        return set(data.filter_requests(filters).values_list('pk', flat=True))

    def test_no_filter(self):
        self.assertEqual(len(self.filtered()), 5)

    def test_path(self):
        self.assertEqual(self.filtered(path='/api/boards/'), {self.fast.pk, self.unfinished.pk})
        self.assertEqual(self.filtered(path='/api/boards'), set())

    def test_path_contains(self):
        self.assertEqual(self.filtered(path_contains='training'), {self.slow.pk, self.failed.pk})

    def test_view(self):
        self.assertEqual(self.filtered(view='training:openings'), {self.slow.pk})

    def test_method(self):
        self.assertEqual(self.filtered(method='post'), {self.failed.pk})

    def test_status_code(self):
        self.assertEqual(self.filtered(status='404'), {self.missing.pk})

    def test_status_class(self):
        self.assertEqual(self.filtered(status='5xx'), {self.failed.pk})
        self.assertEqual(self.filtered(status='2xx'), {self.fast.pk, self.slow.pk})

    def test_since_duration(self):
        self.assertEqual(self.filtered(since='2h'), {self.fast.pk})
        self.assertEqual(self.filtered(since='7d'), {self.fast.pk, self.slow.pk, self.failed.pk})

    def test_since_timestamp(self):
        moment = (self.now - timedelta(days=1)).isoformat()
        self.assertEqual(self.filtered(since=moment), {self.fast.pk, self.slow.pk})

    def test_until(self):
        self.assertEqual(self.filtered(until='7d'), {self.missing.pk, self.unfinished.pk})
        self.assertEqual(self.filtered(since='15d', until='1d'), {self.failed.pk, self.missing.pk})

    def test_min_time(self):
        self.assertEqual(self.filtered(min_time=300), {self.slow.pk, self.failed.pk})

    def test_min_queries(self):
        self.assertEqual(self.filtered(min_queries=3), {self.slow.pk})
        self.assertEqual(self.filtered(min_queries=1), {self.fast.pk, self.slow.pk, self.failed.pk})

    def test_min_query_time(self):
        self.assertEqual(self.filtered(min_query_time=250), {self.slow.pk, self.failed.pk})
        self.assertEqual(self.filtered(min_query_time=300), {self.slow.pk})

    def test_combined(self):
        self.assertEqual(self.filtered(view='training:openings', min_time=500), {self.slow.pk})
        self.assertEqual(self.filtered(view='training:openings', min_time=1000), set())
        self.assertEqual(self.filtered(path_contains='api', method='GET', status='2xx', since='1d'), {self.slow.pk, self.fast.pk})
        self.assertEqual(self.filtered(path_contains='api', min_query_time=10, min_queries=2), {self.slow.pk})

    def test_invalid_values(self):
        with self.assertRaises(data.ReportError):
            self.filtered(status='nope')
        with self.assertRaises(data.ReportError):
            self.filtered(since='nope')

    def test_order_by_start(self):
        newest_first = [self.fast.pk, self.slow.pk, self.failed.pk, self.missing.pk, self.unfinished.pk]
        self.assertEqual(ids(data.request_list()), newest_first)
        self.assertEqual(ids(data.request_list(sort='start', ascending=True)), newest_first[::-1])

    def test_order_by_time(self):
        finished = {'min_time': 0}
        slowest_first = [self.slow.pk, self.failed.pk, self.fast.pk, self.missing.pk]
        self.assertEqual(ids(data.request_list(finished, sort='time')), slowest_first)
        self.assertEqual(ids(data.request_list(finished, sort='time', ascending=True)), slowest_first[::-1])
        # A request that never finished has no duration and sorts last either way.
        self.assertEqual(ids(data.request_list(sort='time'))[-1], self.unfinished.pk)
        self.assertEqual(ids(data.request_list(sort='time', ascending=True))[-1], self.unfinished.pk)

    def test_order_by_queries(self):
        with_queries = {'min_queries': 1}
        self.assertEqual(
            ids(data.request_list(with_queries, sort='queries')), [self.slow.pk, self.fast.pk, self.failed.pk],
        )
        self.assertEqual(
            ids(data.request_list(with_queries, sort='queries', ascending=True))[-1], self.slow.pk,
        )

    def test_order_by_query_time(self):
        with_queries = {'min_queries': 1}
        most_first = [self.slow.pk, self.failed.pk, self.fast.pk]
        self.assertEqual(ids(data.request_list(with_queries, sort='query-time')), most_first)
        self.assertEqual(ids(data.request_list(with_queries, sort='query-time', ascending=True)), most_first[::-1])
        self.assertEqual(ids(data.request_list({'min_query_time': 250}, sort='query-time')), most_first[:2])
        # Requests without queries count as zero time in queries.
        self.assertEqual(ids(data.request_list(sort='query-time'))[:3], most_first)

    def test_order_by_path(self):
        finished = {'min_time': 0}
        paths = ['/nothing/', '/api/training/progress/', '/api/training/openings/', '/api/boards/']
        self.assertEqual([row['path'] for row in data.request_list(finished, sort='path')['requests']], paths)
        self.assertEqual(
            [row['path'] for row in data.request_list(finished, sort='path', ascending=True)['requests']],
            paths[::-1],
        )

    def test_invalid_sort(self):
        with self.assertRaises(data.ReportError) as raised:
            data.request_list(sort='size')
        for name in ('start', 'time', 'queries', 'query-time', 'path'):
            self.assertIn(name, str(raised.exception))

    def test_limit_and_offset(self):
        report = data.request_list(limit=2)
        self.assertEqual(ids(report), [self.fast.pk, self.slow.pk])
        self.assertEqual(report['total'], 5)
        self.assertEqual(report['truncated'], [{
            'key': 'requests', 'shown': 2, 'total': 5, 'unit': 'rows', 'offset': 0,
            'option': '--offset 2 shows the next rows, --limit N shows more (0 for all)',
        }])
        report = data.request_list(limit=2, offset=4)
        self.assertEqual(ids(report), [self.unfinished.pk])
        self.assertEqual(report['truncated'][0]['option'], '--limit N shows more (0 for all)')
        self.assertEqual(len(data.request_list(limit=0)['requests']), 5)
        self.assertEqual(data.request_list(limit=0)['truncated'], [])

    def test_request_rows(self):
        report = data.request_list()
        self.assertEqual(report['requests'][1], {
            'id': self.slow.pk,
            'start_time': data.iso(self.slow.start_time),
            'method': 'GET',
            'status': 200,
            'time_ms': 900.0,
            'queries_ms': 300.0,
            'queries': 3,
            'sql': 0,
            'cypher': 3,
            'other': 0,
            'view': 'training:openings',
            'path': '/api/training/openings/',
        })
        failed = report['requests'][2]
        self.assertEqual((failed['sql'], failed['cypher'], failed['queries_ms']), (1, 0, 250.0))
        unfinished = report['requests'][4]
        self.assertIsNone(unfinished['status'])
        self.assertIsNone(unfinished['time_ms'])
        self.assertEqual(unfinished['queries'], 0)
        self.assertEqual(unfinished['queries_ms'], 0.0)
        self.assertIsNone(report['requests'][3]['view'])
        self.assertEqual(report['next'][0], 'python manage.py silk_query request %s' % self.fast.pk[:8])

    def test_next_keeps_the_filters(self):
        report = data.request_list({'view': 'training:openings', 'min_time': 500})
        self.assertIn(
            'python manage.py silk_query requests --view training:openings --min-time 500 --sort time',
            report['next'],
        )

    def test_nothing_matches(self):
        report = data.request_list({'since': '1m'})
        self.assertEqual(report['requests'], [])
        self.assertEqual(report['total'], 0)
        self.assertEqual(report['empty'], {
            'recorded_requests': 5, 'latest_request_start': data.iso(self.fast.start_time),
        })
        self.assertEqual(report['next'], ['python manage.py silk_query requests'])


class TestRequestListQueries(TestCase):

    def record(self, count):
        for n in range(count):
            request = recorded_request(time_ms=10)
            timed_query(request, SQL % n, 1)
            timed_query(request, CYPHER % n, 2)

    def test_fixed_number_of_queries(self):
        self.record(3)
        # The count, the page, and one grouped query for the queries of the page.
        with self.assertNumQueries(3):
            report = data.request_list()
        self.assertEqual(len(report['requests']), 3)
        self.record(25)
        with self.assertNumQueries(3):
            report = data.request_list()
        self.assertEqual(len(report['requests']), 20)
        self.assertEqual(report['requests'][0]['queries_ms'], 3.0)
        self.assertEqual((report['requests'][0]['sql'], report['requests'][0]['cypher']), (1, 1))

    def test_sum_annotation_only_when_needed(self):
        self.record(2)
        with self.assertNumQueries(3) as captured:
            data.request_list()
        self.assertNotIn('SUM(', captured.captured_queries[1]['sql'].upper())
        with self.assertNumQueries(3) as captured:
            data.request_list(sort='query-time')
        self.assertIn('SUM(', captured.captured_queries[1]['sql'].upper())
        with self.assertNumQueries(3) as captured:
            data.request_list({'min_query_time': 1})
        self.assertIn('SUM(', captured.captured_queries[1]['sql'].upper())

    def test_empty_database(self):
        report = data.request_list()
        self.assertEqual(report['empty'], {'recorded_requests': 0, 'latest_request_start': None})
        self.assertEqual(report['next'], ['python manage.py silk_query guide'])


class TestResolveRequestId(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.first = RequestMinFactory(id='96de4a15-78f1-40c5-b34d-be125c4af704')
        cls.second = RequestMinFactory(id='96de4a15-0000-40c5-b34d-be125c4af704')
        cls.third = RequestMinFactory(id='22809383-2400-445b-860d-06ac8f9a642f')

    def test_exact(self):
        self.assertEqual(data.resolve_request_id(self.first.pk), self.first.pk)

    def test_unique_prefix(self):
        self.assertEqual(data.resolve_request_id('2280'), self.third.pk)
        self.assertEqual(data.resolve_request_id('96de4a15-7'), self.first.pk)

    def test_ambiguous_prefix(self):
        with self.assertRaises(data.ReportError) as raised:
            data.resolve_request_id('96de4a15')
        message = str(raised.exception)
        self.assertIn('ambiguous', message)
        self.assertIn('2 requests', message)
        self.assertIn(self.first.pk, message)
        self.assertIn(self.second.pk, message)
        self.assertNotIn(self.third.pk, message)

    def test_ambiguous_prefix_lists_at_most_ten(self):
        for n in range(12):
            RequestMinFactory(id='aaaa%04d-0000-40c5-b34d-be125c4af704' % n)
        with self.assertRaises(data.ReportError) as raised:
            data.resolve_request_id('aaaa')
        message = str(raised.exception)
        self.assertIn('more than 10 requests', message)
        self.assertEqual(message.count('aaaa0'), 10)

    def test_no_match(self):
        for value in ('ffff', '', '%', '96de4a15-78f1-40c5-b34d-be125c4af704-x'):
            with self.subTest(value=value):
                with self.assertRaises(data.ReportError) as raised:
                    data.resolve_request_id(value)
                self.assertIn('No request matches', str(raised.exception))
                self.assertIn('python manage.py silk_query requests', str(raised.exception))


class ReportTestCase(TestCase):
    """One slow request with an N+1 pattern, and one fast request of another view."""

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.slow = recorded_request(
            time_ms=2000, status_code=200, path='/api/training/openings/', method='GET',
            view_name='training:openings', start_time=now - timedelta(minutes=10),
            query_params=json.dumps({'depth': '3'}),
            encoded_headers=json.dumps({'host': 'localhost:8000', 'content-type': 'application/json'}),
        )
        cls.root = timed_query(
            cls.slow, '\n    MATCH (root:Opening)\n    RETURN root\n', 280, offset_ms=10,
            traceback=tracebacks.NEOMODEL_FROM_VIEW,
        )
        cls.repeated = [
            timed_query(cls.slow, CYPHER % n, 3, offset_ms=300 + 5 * n, traceback=tracebacks.NEOMODEL)
            for n in range(6)
        ]
        cls.sql = timed_query(
            cls.slow, 'SELECT "users_user"."id" FROM "users_user" INNER JOIN "t" ON ("t"."user_id" = "users_user"."id") '
                      'WHERE "users_user"."id" = 19 LIMIT 21',
            2, offset_ms=5, traceback=tracebacks.LIBRARY_ONLY, analysis='Index Scan using users_user_pkey',
        )
        cls.duplicate = [timed_query(cls.slow, SQL % 7, 1, offset_ms=400 + n) for n in range(2)]
        cls.profile = timed_profile(cls.slow, 300, queries=[cls.root] + cls.repeated[:2], name='load openings')

        cls.fast = recorded_request(
            time_ms=40, status_code=200, path='/api/boards/', method='GET', view_name='boards:list',
            start_time=now - timedelta(minutes=5),
        )
        cls.fast_query = timed_query(cls.fast, CYPHER % 99, 30, traceback=tracebacks.OTHER_PYTHON)
        cls.repeated_shape = analysis.shape(CYPHER % 0, 'cypher')[0]
        cls.root_shape = analysis.shape('MATCH (root:Opening) RETURN root', 'cypher')[0]


class TestSummary(ReportTestCase):

    def test_fields(self):
        recorded_request(time_ms=60, path='/api/boards/', view_name='boards:list')
        recorded_request(time_ms=None, status_code=None, path='/api/boards/', view_name='boards:list')
        report = data.summary()
        self.assertEqual(report['report'], 'summary')
        self.assertEqual((report['requests_matching'], report['requests_analysed']), (4, 4))
        self.assertEqual(report['start_time_from'], data.iso(self.slow.start_time))
        self.assertGreater(report['start_time_to'], data.iso(self.fast.start_time))
        self.assertEqual((report['median_ms'], report['max_ms']), (60.0, 2000.0))
        self.assertEqual(report['views'], [
            {
                'requests': 1, 'median_ms': 2000.0, 'p95_ms': 2000.0, 'max_ms': 2000.0, 'total_ms': 2000.0,
                'avg_queries': 10.0, 'avg_queries_ms': 302.0, 'view': 'training:openings',
            },
            {
                'requests': 3, 'median_ms': 50.0, 'p95_ms': 59.0, 'max_ms': 60.0, 'total_ms': 100.0,
                'avg_queries': 0.3, 'avg_queries_ms': 10.0, 'view': 'boards:list',
            },
        ])
        self.assertEqual(report['truncated'], [])
        self.assertEqual(report['next'], [
            'python manage.py silk_query requests --view training:openings --sort time',
            'python manage.py silk_query shapes --view training:openings',
        ])

    def test_coverage_is_stated(self):
        report = data.summary(max_requests=1)
        self.assertEqual((report['requests_matching'], report['requests_analysed']), (2, 1))
        # The most recent request is the one analysed.
        self.assertEqual([row['view'] for row in report['views']], ['boards:list'])
        self.assertEqual(report['truncated'], [{
            'key': 'requests_analysed', 'shown': 1, 'total': 2, 'unit': 'requests',
            'option': 'these are the most recent ones, --max-requests N analyses more (0 for all)',
        }])

    def test_filters(self):
        report = data.summary({'view': 'boards:list'})
        self.assertEqual(report['requests_matching'], 1)
        self.assertEqual([row['view'] for row in report['views']], ['boards:list'])
        self.assertEqual(report['filters'], {'view': 'boards:list'})

    def test_view_rows_are_bounded(self):
        report = data.summary(limit=1)
        self.assertEqual(len(report['views']), 1)
        self.assertEqual(report['views_total'], 2)
        self.assertEqual(report['truncated'][0]['key'], 'views')

    def test_fixed_number_of_queries(self):
        # The count, the requests, and one grouped query for their queries.
        with self.assertNumQueries(3):
            data.summary()

    def test_nothing_matches(self):
        report = data.summary({'status': '5xx'})
        self.assertEqual(report['views'], [])
        self.assertEqual(report['empty']['recorded_requests'], 2)
        self.assertIsNone(report['median_ms'])


class TestRequestDetail(ReportTestCase):

    def test_fields(self):
        report = data.request_detail(self.slow.pk[:8])
        self.assertEqual(report['report'], 'request')
        self.assertEqual(report['id'], self.slow.pk)
        self.assertEqual((report['method'], report['path']), ('GET', '/api/training/openings/'))
        self.assertEqual(report['query_params'], {'depth': '3'})
        self.assertEqual((report['view'], report['status']), ('training:openings', 200))
        self.assertEqual(report['start_time'], data.iso(self.slow.start_time))
        self.assertEqual((report['time_ms'], report['queries_ms'], report['outside_queries_ms']), (2000.0, 302.0, 1698.0))
        self.assertEqual(report['queries'], 10)
        self.assertEqual(report['queries_by_kind'], {'sql': 3, 'cypher': 7, 'other': 0})
        self.assertEqual(report['shapes_total'], 4)

    def test_shapes(self):
        shapes = data.request_detail(self.slow.pk)['shapes']
        self.assertEqual([row['total_ms'] for row in shapes], [280.0, 18.0, 2.0, 2.0])
        self.assertEqual(shapes[0], {
            'shape': self.root_shape, 'kind': 'cypher', 'count': 1, 'total_ms': 280.0, 'avg_ms': 280.0,
            'max_ms': 280.0, 'slowest_query': self.root.pk,
            'call_site': {
                'file': '/backend/training/views.py', 'line': 128, 'function': 'get',
                'source': 'rows, _ = db.cypher_query(ROOT_OPENINGS)',
            },
            'text': 'MATCH (root:Opening) RETURN root', 'text_chars': 32,
        })
        repeated = shapes[1]
        self.assertEqual((repeated['shape'], repeated['count'], repeated['avg_ms']), (self.repeated_shape, 6, 3.0))
        self.assertEqual(repeated['call_site']['file'], '/backend/training/serializers.py')
        kinds = {row['kind'] for row in shapes[2:]}
        self.assertEqual(kinds, {'sql'})
        # Only library frames in its traceback, or no traceback that can be read.
        self.assertEqual([row['call_site'] for row in shapes[2:]], [None, None])

    def test_hints(self):
        hints = data.request_detail(self.slow.pk)['hints']
        self.assertEqual(
            [hint['hint'] for hint in hints], ['repeated_shape', 'identical_queries', 'time_outside_queries'],
        )
        self.assertEqual((hints[0]['shape'], hints[0]['count']), (self.repeated_shape, 6))
        self.assertIn('/backend/training/serializers.py:114', hints[0]['message'])
        self.assertEqual(hints[1]['query_count'], 2)
        self.assertEqual(hints[2]['outside_queries_ms'], 1698.0)
        self.assertFalse(hints[2]['profiler_output'])

    def test_no_hint(self):
        request = recorded_request(time_ms=100)
        timed_query(request, SQL % 1, 30)
        timed_query(request, CYPHER % 1, 30)
        self.assertEqual(data.request_detail(request.pk)['hints'], [])

    def test_profiles(self):
        self.assertEqual(data.request_detail(self.slow.pk)['profiles'], [{
            'id': self.profile.pk, 'name': 'load openings', 'function': 'do_work',
            'file': '/app/example_app/views.py', 'line': 10, 'time_ms': 300.0, 'queries': 3, 'queries_ms': 286.0,
            'request': self.slow.pk,
        }])
        self.assertEqual(data.request_detail(self.fast.pk)['profiles'], [])

    def test_next(self):
        self.assertEqual(data.request_detail(self.slow.pk)['next'], [
            'python manage.py silk_query queries {} --shape {}'.format(self.slow.pk, self.root_shape),
            'python manage.py silk_query queries %s --sort time' % self.slow.pk,
            'python manage.py silk_query profile %s' % self.profile.pk,
            'python manage.py silk_query request %s --headers --bodies' % self.slow.pk,
        ])

    def test_shapes_are_bounded(self):
        report = data.request_detail(self.slow.pk, limit=2, max_chars=10)
        self.assertEqual(len(report['shapes']), 2)
        self.assertEqual(report['shapes'][0]['text'], 'MATCH (roo')
        self.assertEqual(report['shapes'][0]['text_chars'], 32)
        self.assertEqual(report['truncated'], [
            {
                'key': 'shapes', 'shown': 2, 'total': 4, 'unit': 'rows', 'offset': 0,
                'option': '--offset 2 shows the next rows, --limit N shows more (0 for all)',
            },
            {
                'key': 'shapes.text', 'shown': 10, 'total': 39, 'unit': 'chars', 'rows': 2,
                'option': '--max-chars N shows more (0 for all)',
            },
        ])

    def test_traceback_and_plan_are_read_once_per_query_only(self):
        with self.assertNumQueries(5) as captured:
            data.request_detail(self.slow.pk)
        statements = [query['sql'] for query in captured.captured_queries]
        self.assertEqual(len([sql for sql in statements if '"traceback"' in sql]), 1)
        # The plan is not loaded: whether there is one is asked of the database.
        self.assertFalse([sql for sql in statements if '"silk_sqlquery"."analysis",' in sql])

    def test_request_without_queries(self):
        request = recorded_request(time_ms=120)
        report = data.request_detail(request.pk)
        self.assertEqual(report['queries'], 0)
        self.assertEqual(report['shapes'], [])
        self.assertEqual((report['queries_ms'], report['outside_queries_ms']), (0.0, 120.0))
        self.assertEqual([hint['hint'] for hint in report['hints']], ['time_outside_queries'])

    def test_request_without_response(self):
        request = recorded_request(time_ms=None, status_code=None)
        report = data.request_detail(request.pk)
        self.assertIsNone(report['status'])
        self.assertIsNone(report['time_ms'])
        self.assertIsNone(report['outside_queries_ms'])
        self.assertIsNone(report['end_time'])
        self.assertEqual(report['hints'], [])

    def test_unknown_request(self):
        with self.assertRaises(data.ReportError):
            data.request_detail('ffffffff')


class TestRequestSections(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.request = recorded_request(
            time_ms=100, status_code=None, path='/api/login/', method='POST',
            encoded_headers=json.dumps({
                'host': 'example.test', 'content-type': 'application/json',
                'authorization': '********************',
            }),
            body='{\n    "password": "********************",\n    "user": "ann"\n}',
            raw_body='{"password": "********************", "user": "ann"}',
        )
        cls.response = ResponseFactory(
            request=cls.request, status_code=200,
            encoded_headers=json.dumps({'Content-Type': 'application/json', 'Vary': 'Cookie'}),
            body='{\n    "ok": true\n}',
            raw_body=base64.b64encode(b'{"ok": true}').decode('ascii'),
        )

    def sections(self, report):
        return {row['section']: row for row in report['sections']}

    def test_absent_by_default_with_sizes(self):
        report = data.request_detail(self.request.pk)
        for key in ('request_headers', 'response_headers', 'request_body', 'response_body', 'cprofile', 'curl'):
            self.assertNotIn(key, report)
        self.assertEqual(report['sections'], [
            {'section': 'request_headers', 'option': '--headers', 'size': 3, 'unit': 'headers'},
            {'section': 'response_headers', 'option': '--headers', 'size': 2, 'unit': 'headers'},
            {'section': 'request_body', 'option': '--bodies', 'size': 61, 'unit': 'chars'},
            {'section': 'response_body', 'option': '--bodies', 'size': 12, 'unit': 'bytes'},
            {'section': 'cprofile', 'option': '--cprofile', 'size': 0, 'unit': 'chars'},
            {'section': 'curl', 'option': '--curl', 'size': None, 'unit': None},
        ])

    def test_bodies_are_not_loaded_by_default(self):
        # The id, the request, its response, its queries and its profiles.
        with self.assertNumQueries(5) as captured:
            data.request_detail(self.request.pk)
        for query in captured.captured_queries:
            # Only their lengths are asked for.
            for column in ('"body"', '"raw_body"', '"pyprofile"'):
                self.assertNotIn(column + ' AS', query['sql'])

    def test_headers(self):
        report = data.request_detail(self.request.pk, headers=True)
        self.assertEqual(report['request_headers']['host'], 'example.test')
        # Masked by silk when the request was recorded.
        self.assertEqual(report['request_headers']['authorization'], '********************')
        self.assertEqual(report['response_headers'], {'Content-Type': 'application/json', 'Vary': 'Cookie'})
        self.assertNotIn('request_body', report)

    def test_bodies(self):
        report = data.request_detail(self.request.pk, bodies=True)
        self.assertEqual(report['request_body'], self.request.body)
        self.assertEqual(report['response_body'], '{\n    "ok": true\n}')
        self.assertNotIn('response_body_binary', report)
        self.assertNotIn('request_headers', report)

    def test_bodies_are_cut(self):
        report = data.request_detail(self.request.pk, bodies=True, max_chars=10)
        self.assertEqual(report['request_body'], self.request.body[:10])
        self.assertIn(
            {'key': 'request_body', 'shown': 10, 'total': 61, 'unit': 'chars', 'option': '--max-chars 0 prints it whole'},
            report['truncated'],
        )
        report = data.request_detail(self.request.pk, bodies=True, max_chars=0)
        self.assertEqual(report['request_body'], self.request.body)
        self.assertEqual(report['truncated'], [])

    def test_text_response_that_silk_did_not_parse(self):
        request = recorded_request()
        Response.objects.filter(request=request).update(
            encoded_headers=json.dumps({'Content-Type': 'text/html; charset=utf-8'}),
            raw_body=base64.b64encode('<p>café</p>'.encode()).decode('ascii'),
        )
        report = data.request_detail(request.pk, bodies=True)
        self.assertEqual(report['response_body'], '<p>café</p>')
        self.assertEqual(self.sections(report)['response_body']['size'], 12)
        self.assertEqual(report['request_body'], '')

    def test_binary_response_body(self):
        request = recorded_request()
        png = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe'
        Response.objects.filter(request=request).update(
            encoded_headers=json.dumps({'Content-Type': 'image/png'}),
            raw_body=base64.b64encode(png).decode('ascii'),
        )
        report = data.request_detail(request.pk, bodies=True)
        self.assertIsNone(report['response_body'])
        self.assertEqual(report['response_body_binary'], {'content_type': 'image/png', 'bytes': len(png)})
        self.assertEqual(self.sections(report)['response_body']['size'], len(png))
        self.assertNotIn('PNG', json.dumps(report))

    def test_request_without_response(self):
        request = recorded_request(status_code=None)
        report = data.request_detail(request.pk, headers=True, bodies=True)
        self.assertIsNone(report['response_headers'])
        self.assertIsNone(report['response_body'])
        self.assertIsNone(self.sections(report)['response_body']['size'])
        self.assertIsNone(self.sections(report)['response_headers']['size'])

    def test_cprofile_not_recorded(self):
        report = data.request_detail(self.request.pk, cprofile=True)
        self.assertIsNone(report['cprofile'])
        self.assertIn('No profiler output was recorded', report['cprofile_note'])
        self.assertIn('SILKY_PYTHON_PROFILER', report['cprofile_note'])

    def test_cprofile(self):
        output = '   12 function calls in 0.010 seconds\n' + 'x' * 5000
        Request.objects.filter(pk=self.request.pk).update(pyprofile=output)
        report = data.request_detail(self.request.pk, cprofile=True)
        self.assertEqual(report['cprofile'], output[:4000])
        self.assertNotIn('cprofile_note', report)
        self.assertEqual(report['truncated'][-1]['total'], len(output))
        self.assertEqual(self.sections(report)['cprofile']['size'], len(output))
        report = data.request_detail(self.request.pk)
        self.assertIn('python manage.py silk_query request %s --cprofile' % self.request.pk, report['next'])

    def test_curl(self):
        report = data.request_detail(self.request.pk, curl=True)
        self.assertTrue(report['curl'].startswith('curl -X POST'))
        self.assertIn("-H 'content-type: application/json'", report['curl'])
        self.assertIn('"user": "ann"', report['curl'])
        self.assertIn('http://example.test/api/login/', report['curl'])
        self.assertNotIn('request_body', report)

    def test_curl_base_url(self):
        report = data.request_detail(self.request.pk, curl=True, base_url='https://staging.example.test/')
        self.assertIn('https://staging.example.test/api/login/', report['curl'])
        self.assertNotIn('http://example.test', report['curl'])

    def test_curl_query_params_and_no_host(self):
        request = recorded_request(path='/api/boards/', method='GET', query_params=json.dumps({'depth': '3'}))
        report = data.request_detail(request.pk, curl=True)
        self.assertIn('http://localhost/api/boards/?depth=3', report['curl'])


class TestRequestQueries(ReportTestCase):

    def test_timeline(self):
        report = data.request_queries(self.slow.pk[:8])
        self.assertEqual(report['report'], 'queries')
        self.assertEqual(report['request'], self.slow.pk)
        self.assertEqual((report['request_queries'], report['total'], report['total_ms']), (10, 10, 302.0))
        self.assertEqual(report['queries_by_kind'], {'sql': 3, 'cypher': 7, 'other': 0})
        offsets = [row['offset_ms'] for row in report['queries']]
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual(offsets[:3], [5.0, 10.0, 300.0])
        self.assertEqual(report['queries'][1], {
            'id': self.root.pk, 'offset_ms': 10.0, 'time_ms': 280.0, 'kind': 'cypher', 'shape': self.root_shape,
            'call_site': {
                'file': '/backend/training/views.py', 'line': 128, 'function': 'get',
                'source': 'rows, _ = db.cypher_query(ROOT_OPENINGS)',
            },
            'text': 'MATCH (root:Opening) RETURN root', 'text_chars': 32,
        })
        self.assertEqual(report['next'], [
            'python manage.py silk_query query %s' % self.sql.pk,
            'python manage.py silk_query request %s' % self.slow.pk,
        ])

    def test_by_duration(self):
        report = data.request_queries(self.slow.pk, sort='time')
        times = [row['time_ms'] for row in report['queries']]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(report['queries'][0]['id'], self.root.pk)

    def test_shape(self):
        report = data.request_queries(self.slow.pk, shape=self.repeated_shape)
        self.assertEqual(ids(report, 'queries'), [query.pk for query in self.repeated])
        self.assertEqual((report['total'], report['total_ms']), (6, 18.0))
        self.assertEqual(report['filters'], {'shape': self.repeated_shape})

    def test_kind(self):
        report = data.request_queries(self.slow.pk, kind='sql')
        self.assertEqual({row['kind'] for row in report['queries']}, {'sql'})
        self.assertEqual(report['total'], 3)

    def test_min_time(self):
        report = data.request_queries(self.slow.pk, min_time=3)
        self.assertEqual(report['total'], 7)
        self.assertEqual(data.request_queries(self.slow.pk, min_time=3, kind='sql')['total'], 0)

    def test_tracebacks_are_read_for_the_page_only(self):
        with mock.patch.object(analysis, 'call_site', wraps=analysis.call_site) as call_site:
            report = data.request_queries(self.slow.pk, limit=3)
        self.assertEqual(call_site.call_count, 3)
        self.assertEqual(report['truncated'][0]['total'], 10)

    def test_text_is_cut(self):
        report = data.request_queries(self.slow.pk, kind='sql', max_chars=20)
        self.assertEqual(report['queries'][0]['text'], 'SELECT "users_user".')
        self.assertEqual(report['queries'][0]['text_chars'], len(' '.join(self.sql.query.split())))
        self.assertEqual(report['truncated'][0]['key'], 'queries.text')
        self.assertEqual(report['truncated'][0]['rows'], 3)

    def test_nothing_matches(self):
        report = data.request_queries(self.slow.pk, shape='00000000')
        self.assertEqual(report['queries'], [])
        self.assertEqual(report['request_queries'], 10)
        self.assertEqual(report['empty']['recorded_requests'], 2)

    def test_invalid_values(self):
        with self.assertRaises(data.ReportError) as raised:
            data.request_queries(self.slow.pk, sort='path')
        self.assertIn('start, time', str(raised.exception))
        with self.assertRaises(data.ReportError) as raised:
            data.request_queries(self.slow.pk, kind='graphql')
        self.assertIn('sql, cypher, other', str(raised.exception))


class TestQueryDetail(ReportTestCase):

    def test_sql_query_with_plan(self):
        report = data.query_detail(self.sql.pk)
        self.assertEqual(report['report'], 'query')
        self.assertEqual((report['id'], report['request'], report['kind']), (self.sql.pk, self.slow.pk, 'sql'))
        self.assertEqual(report['shape'], analysis.shape(self.sql.query, 'sql')[0])
        self.assertEqual((report['offset_ms'], report['time_ms']), (5.0, 2.0))
        self.assertEqual(report['start_time'], data.iso(self.sql.start_time))
        self.assertIn('\nFROM "users_user"\nINNER JOIN "t"', report['text'])
        self.assertEqual(report['tables'], ['"users_user"', '"t"'])
        self.assertEqual(report['joins'], 1)
        self.assertEqual(report['plan'], 'Index Scan using users_user_pkey')
        self.assertNotIn('plan_note', report)
        self.assertIsNone(report['call_site'])
        self.assertEqual(report['next'], [
            'python manage.py silk_query queries {} --shape {}'.format(self.slow.pk, report['shape']),
            'python manage.py silk_query request %s' % self.slow.pk,
        ])

    def test_cypher_query(self):
        report = data.query_detail(str(self.root.pk))
        self.assertEqual(report['kind'], 'cypher')
        self.assertEqual(report['text'], 'MATCH (root:Opening)\nRETURN root')
        self.assertIsNone(report['plan'])
        self.assertIn('No plan is recorded for Cypher queries', report['plan_note'])
        self.assertNotIn('tables', report)
        self.assertNotIn('joins', report)

    def test_sql_query_without_plan(self):
        report = data.query_detail(self.duplicate[0].pk)
        self.assertIsNone(report['plan'])
        self.assertEqual(report['plan_note'], 'No EXPLAIN output was recorded for this query.')

    def test_application_frames(self):
        report = data.query_detail(self.repeated[0].pk)
        self.assertEqual(report['call_site']['file'], '/backend/training/serializers.py')
        self.assertEqual(
            [(frame['file'], frame['line']) for frame in report['traceback']],
            [('/backend/training/serializers.py', 114), ('/backend/training/views.py', 133)],
        )
        self.assertFalse(report['traceback_full'])
        self.assertEqual(report['traceback_frames'], 24)
        self.assertEqual(report['truncated'], [{
            'key': 'traceback', 'shown': 2, 'total': 24, 'unit': 'frames',
            'option': 'application frames only, --full-traceback prints every frame',
        }])

    def test_full_traceback(self):
        report = data.query_detail(self.repeated[0].pk, full_traceback=True)
        self.assertEqual(len(report['traceback']), 24)
        self.assertTrue(report['traceback_full'])
        self.assertIn('site-packages/neomodel', report['traceback'][1]['file'])
        self.assertEqual(report['truncated'], [])

    def test_unreadable_traceback_is_shown_as_stored(self):
        report = data.query_detail(self.duplicate[0].pk)
        self.assertEqual(report['traceback'], [])
        self.assertTrue(report['traceback_raw'].startswith('Traceback #'))

    def test_long_text_is_cut(self):
        query = timed_query(self.fast, 'MATCH (n) WHERE n.uid IN [%s] RETURN n' % ', '.join("'u%d'" % n for n in range(900)), 1)
        report = data.query_detail(query.pk)
        self.assertEqual(len(report['text']), 4000)
        self.assertEqual(report['truncated'][0], {
            'key': 'text', 'shown': 4000, 'total': len(query.query), 'unit': 'chars',
            'option': '--max-chars 0 prints it whole',
        })
        report = data.query_detail(query.pk, max_chars=0)
        self.assertEqual(report['text'], query.query)
        self.assertEqual([entry for entry in report['truncated'] if entry['key'] == 'text'], [])

    def test_query_without_request(self):
        query = timed_query(self.fast, 'SELECT 1', 1)
        SQLQuery.objects.filter(pk=query.pk).update(request=None)
        report = data.query_detail(query.pk)
        self.assertIsNone(report['request'])
        self.assertIsNone(report['offset_ms'])
        self.assertEqual(report['next'], ['python manage.py silk_query requests'])

    def test_unknown_query(self):
        with self.assertRaises(data.ReportError) as raised:
            data.query_detail(987654)
        self.assertIn('No query has id 987654', str(raised.exception))
        with self.assertRaises(data.ReportError) as raised:
            data.query_detail('96de4a15')
        self.assertIn('integer', str(raised.exception))


class TestShapeList(ReportTestCase):

    def test_fields(self):
        report = data.shape_list()
        self.assertEqual(report['report'], 'shapes')
        self.assertEqual((report['requests_matching'], report['requests_analysed']), (2, 2))
        self.assertEqual((report['queries_analysed'], report['shapes_total']), (11, 4))
        self.assertEqual([row['total_ms'] for row in report['shapes']], [280.0, 48.0, 2.0, 2.0])
        repeated = report['shapes'][1]
        self.assertEqual(repeated, {
            'shape': self.repeated_shape, 'kind': 'cypher', 'count': 7, 'requests': 2, 'total_ms': 48.0,
            'avg_ms': 6.86, 'max_ms': 30.0, 'slowest_query': self.fast_query.pk, 'slowest_request': self.fast.pk,
            # Of the slowest execution, which is the one in the other request.
            'call_site': {
                'file': '/backend/users/services.py', 'line': 41, 'function': 'current_rating',
                'source': 'return Rating.objects.get(user=user)',
            },
            'text': 'MATCH (o) WHERE elementId(o)=? RETURN o', 'text_chars': 39,
        })
        self.assertEqual(report['shapes'][0]['requests'], 1)
        self.assertEqual(report['next'], [
            'python manage.py silk_query queries {} --shape {}'.format(self.slow.pk[:8], self.root_shape),
            'python manage.py silk_query query %s' % self.root.pk,
        ])

    def test_one_traceback_per_shape(self):
        with mock.patch.object(analysis, 'call_site', wraps=analysis.call_site) as call_site:
            with self.assertNumQueries(4) as captured:
                report = data.shape_list()
        self.assertEqual(call_site.call_count, len(report['shapes']))
        with_traceback = [query['sql'] for query in captured.captured_queries if '"traceback"' in query['sql']]
        self.assertEqual(len(with_traceback), 1)
        self.assertIn(' IN (', with_traceback[0])

    def test_filters(self):
        report = data.shape_list({'view': 'boards:list'})
        self.assertEqual(report['requests_analysed'], 1)
        self.assertEqual([(row['shape'], row['count']) for row in report['shapes']], [(self.repeated_shape, 1)])

    def test_coverage_is_stated(self):
        report = data.shape_list(max_requests=1)
        self.assertEqual((report['requests_matching'], report['requests_analysed']), (2, 1))
        self.assertEqual(report['shapes_total'], 1)
        self.assertEqual(report['truncated'][0]['key'], 'requests_analysed')
        self.assertEqual((report['truncated'][0]['shown'], report['truncated'][0]['total']), (1, 2))
        self.assertIn('--max-requests', report['truncated'][0]['option'])

    def test_rows_are_bounded(self):
        report = data.shape_list(limit=1, offset=1)
        self.assertEqual([row['shape'] for row in report['shapes']], [self.repeated_shape])
        self.assertEqual(report['truncated'][0]['key'], 'shapes')

    def test_nothing_matches(self):
        report = data.shape_list({'path': '/none/'})
        self.assertEqual(report['shapes'], [])
        self.assertEqual(report['empty']['recorded_requests'], 2)


class TestProfiles(ReportTestCase):

    def test_list(self):
        other = timed_profile(self.fast, 30, name='render', func_name='render_board')
        report = data.profile_list()
        self.assertEqual(report['report'], 'profiles')
        self.assertEqual(report['total'], 2)
        # Most recent first.
        self.assertEqual(ids(report, 'profiles'), [other.pk, self.profile.pk])
        self.assertEqual(report['profiles'][1], {
            'id': self.profile.pk, 'name': 'load openings', 'function': 'do_work',
            'file': '/app/example_app/views.py', 'line': 10, 'time_ms': 300.0, 'queries': 3, 'queries_ms': 286.0,
            'request': self.slow.pk,
        })
        self.assertEqual(report['profiles'][0]['queries'], 0)
        self.assertEqual(report['profiles'][0]['queries_ms'], 0.0)
        self.assertEqual(report['next'], [
            'python manage.py silk_query profile %s' % other.pk,
            'python manage.py silk_query request %s' % self.fast.pk[:8],
        ])
        self.assertEqual(ids(data.profile_list(name='REND'), 'profiles'), [other.pk])
        self.assertEqual(ids(data.profile_list(function='render_'), 'profiles'), [other.pk])
        self.assertEqual(ids(data.profile_list(request=self.slow.pk[:8]), 'profiles'), [self.profile.pk])
        self.assertEqual(ids(data.profile_list(name='load', function='render'), 'profiles'), [])
        self.assertEqual(ids(data.profile_list(limit=1, offset=1), 'profiles'), [self.profile.pk])
        self.assertEqual(data.profile_list(limit=1)['truncated'][0]['total'], 2)

    def test_no_profiles(self):
        Profile.objects.all().delete()
        report = data.profile_list()
        self.assertEqual(report['profiles'], [])
        self.assertEqual(report['empty']['recorded_profiles'], 0)
        self.assertEqual(report['empty']['recorded_requests'], 2)

    def test_filters_match_nothing(self):
        report = data.profile_list(name='nope')
        self.assertEqual(report['empty']['recorded_profiles'], 1)

    def test_detail(self):
        report = data.profile_detail(self.profile.pk)
        self.assertEqual(report['report'], 'profile')
        self.assertEqual((report['id'], report['name'], report['function']), (self.profile.pk, 'load openings', 'do_work'))
        self.assertEqual((report['file'], report['line'], report['request']), ('/app/example_app/views.py', 10, self.slow.pk))
        self.assertEqual((report['time_ms'], report['queries'], report['queries_ms']), (300.0, 3, 286.0))
        self.assertEqual(report['queries_by_kind'], {'sql': 0, 'cypher': 3, 'other': 0})
        self.assertEqual(report['start_time'], data.iso(self.slow.start_time))
        self.assertFalse(report['dynamic'])
        self.assertFalse(report['exception_raised'])
        self.assertEqual(
            [(row['shape'], row['count'], row['total_ms']) for row in report['shapes']],
            [(self.root_shape, 1, 280.0), (self.repeated_shape, 2, 6.0)],
        )
        self.assertEqual(report['shapes'][1]['call_site']['file'], '/backend/training/serializers.py')
        self.assertEqual(report['next'], [
            'python manage.py silk_query query %s' % self.root.pk,
            'python manage.py silk_query request %s' % self.slow.pk,
            'python manage.py silk_query profiles',
        ])

    def test_unknown_profile(self):
        with self.assertRaises(data.ReportError) as raised:
            data.profile_detail(987654)
        self.assertIn('No profile has id 987654', str(raised.exception))
        with self.assertRaises(data.ReportError):
            data.profile_detail('abc')
