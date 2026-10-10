import json
import re

from django.test import SimpleTestCase

from silk.reporting import data, render

from .factories import recorded_request, timed_query
from .test_reporting_data import ReportTestCase

TIMESTAMP = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z')
# Durations, counts and query ids; request ids; shape ids.
NUMBER = re.compile(r'(?<![\w.:-])\d+(?:\.\d+)?(?![\w.:-])')
REQUEST_ID = re.compile(r'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b')
SHORT_ID = re.compile(r'(?<![\w-])[0-9a-f]{8}(?![\w-])')


def leaves(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from leaves(item)
    else:
        yield value


def assert_text_is_in_json(test, text, document):
    """Every number and id of the text output must come from a value of the JSON document."""
    numbers = set()
    strings = []
    for leaf in leaves(document):
        if isinstance(leaf, bool) or leaf is None:
            continue
        if isinstance(leaf, float):
            numbers.update(('%.2f' % leaf, '%.1f' % leaf))
        elif isinstance(leaf, int):
            numbers.add(str(leaf))
        else:
            strings.append(leaf)
    strings = '\n'.join(strings)
    tokens = NUMBER.findall(text) + REQUEST_ID.findall(text) + SHORT_ID.findall(text)
    test.assertTrue(tokens)
    for token in tokens:
        test.assertTrue(token in numbers or token in strings, '%r is in the text but not in the JSON' % token)


class TestTable(SimpleTestCase):

    def test_alignment(self):
        rows = [
            {'id': 7, 'kind': 'sql', 'time_ms': 2.0, 'text': 'SELECT 1'},
            {'id': 1234, 'kind': 'cypher', 'time_ms': None, 'text': 'MATCH (n)\n  RETURN n'},
        ]
        self.assertEqual(render._table(rows, ('id', 'kind', 'time_ms', 'text')), [
            '  id  kind    time_ms  text',
            '   7  sql        2.00  SELECT 1',
            '1234  cypher        -  MATCH (n) RETURN n',
        ])

    def test_cut_text_states_its_length(self):
        rows = [{'shape': 'a8ad4abe', 'text': 'MATCH (n', 'text_chars': 18}]
        self.assertEqual(render._table(rows, ('shape', 'text'))[1], 'a8ad4abe  MATCH (n ...[18 chars]')

    def test_missing_values(self):
        self.assertEqual(render._value(None), '-')
        self.assertEqual(render._value(''), '-')
        self.assertEqual(render._value(0), '0')
        self.assertEqual(render._value(0.0), '0.00')
        self.assertEqual(render._value(False), 'no')


class TestTextRenderer(ReportTestCase):

    def test_request_layout(self):
        text = render.render_text(data.request_detail(self.slow.pk))
        lines = text.split('\n')
        self.assertEqual(lines[0], 'request %s' % self.slow.pk)
        self.assertEqual(lines[1:4], ['method: GET', 'path: /api/training/openings/', 'query_params: {"depth": "3"}'])
        self.assertIn('time_ms: 2000.00', lines)
        self.assertIn('queries_ms: 302.00', lines)
        self.assertIn('outside_queries_ms: 1698.00', lines)
        self.assertIn('queries: 10 (3 sql, 7 cypher, 0 other)', lines)
        for section in ('SHAPES (4, by total time)', 'HINTS', 'PROFILES', 'SECTIONS (printed only with their option)', 'NEXT'):
            self.assertIn(section, lines)
        header = lines[lines.index('SHAPES (4, by total time)') + 1]
        self.assertEqual(header.split(), [
            'shape', 'kind', 'count', 'total_ms', 'avg_ms', 'max_ms', 'slowest_query', 'call_site', 'text',
        ])
        row = lines[lines.index('SHAPES (4, by total time)') + 2]
        self.assertEqual(
            row.split()[:7], [self.root_shape, 'cypher', '1', '280.00', '280.00', '280.00', str(self.root.pk)],
        )
        self.assertEqual(
            ' '.join(row.split()[7:]), '/backend/training/views.py:128 in get MATCH (root:Opening) RETURN root',
        )
        self.assertIn(
            '- [repeated_shape] shape %s ran 6 times (18.00 ms in total) from '
            '/backend/training/serializers.py:114 in get_moves_from_parent: likely N+1 pattern, '
            'fetch these rows in one query' % self.repeated_shape,
            lines,
        )
        self.assertEqual(lines[lines.index('NEXT') + 1], '  ' + data.request_detail(self.slow.pk)['next'][0])
        # The individual queries are not printed.
        self.assertNotIn("elementId(o)='3'", text)
        self.assertLess(len(lines), 60)

    def test_sections_are_listed_and_not_printed(self):
        lines = render.render_text(data.request_detail(self.slow.pk)).split('\n')
        start = lines.index('SECTIONS (printed only with their option)')
        self.assertEqual([line.split() for line in lines[start + 1:start + 8]], [
            ['option', 'section', 'size'],
            ['--headers', 'request_headers', '2', 'headers'],
            ['--headers', 'response_headers', '0', 'headers'],
            ['--bodies', 'request_body', '0', 'chars'],
            ['--bodies', 'response_body', '0', 'bytes'],
            ['--cprofile', 'cprofile', '0', 'chars'],
            ['--curl', 'curl', '-'],
        ])
        for title in ('REQUEST HEADERS', 'REQUEST BODY', 'RESPONSE BODY', 'CPROFILE', 'CURL'):
            self.assertNotIn(title, lines)

    def test_sections_are_printed_when_asked_for(self):
        report = data.request_detail(self.slow.pk, headers=True, bodies=True, cprofile=True, curl=True)
        lines = render.render_text(report).split('\n')
        self.assertEqual(lines[lines.index('REQUEST HEADERS') + 1], 'host: localhost:8000')
        self.assertEqual(lines[lines.index('RESPONSE HEADERS') + 1], '(none)')
        self.assertEqual(lines[lines.index('REQUEST BODY') + 1], '(empty)')
        self.assertIn('SILKY_PYTHON_PROFILER', lines[lines.index('CPROFILE') + 1])
        self.assertEqual(
            ' '.join(lines[lines.index('CURL') + 1].split()),
            'curl -X GET http://localhost:8000/api/training/openings/?depth=3',
        )

    def test_no_hint_and_no_queries(self):
        request = recorded_request(time_ms=20)
        lines = render.render_text(data.request_detail(request.pk)).split('\n')
        self.assertEqual(lines[lines.index('SHAPES') + 1], 'No queries were recorded for this request.')
        self.assertEqual(lines[lines.index('HINTS') + 1], 'No hint applies.')
        self.assertIn('queries_ms: 0.00', lines)
        self.assertIn('outside_queries_ms: 20.00', lines)
        self.assertNotIn('PROFILES', lines)

    def test_missing_values_are_not_zero(self):
        request = recorded_request(time_ms=None, status_code=None, path='/unfinished/', view_name='')
        lines = render.render_text(data.request_detail(request.pk)).split('\n')
        for line in ('status: -', 'end_time: -', 'time_ms: -', 'outside_queries_ms: -', 'view: -', 'query_params: -'):
            self.assertIn(line, lines)
        lines = render.render_text(data.request_list({'path': '/unfinished/'})).split('\n')
        columns = lines[lines.index('') + 1].split()
        cells = dict(zip(columns, lines[lines.index('') + 2].split()))
        self.assertEqual((cells['status'], cells['time_ms'], cells['view']), ('-', '-', '-'))
        # No queries is a real zero, not a missing value.
        self.assertEqual((cells['queries'], cells['queries_ms']), ('0', '0.00'))

    def test_durations_are_labelled_in_milliseconds(self):
        outputs = [
            render.render_text(data.request_list()),
            render.render_text(data.summary()),
            render.render_text(data.request_detail(self.slow.pk)),
            render.render_text(data.request_queries(self.slow.pk)),
            render.render_text(data.query_detail(self.root.pk)),
            render.render_text(data.shape_list()),
            render.render_text(data.profile_list()),
            render.render_text(data.profile_detail(self.profile.pk)),
        ]
        for text in outputs:
            lines = text.split('\n')
            with self.subTest(report=lines[0]):
                labelled = set()
                for line in lines[:lines.index('NEXT')]:
                    if re.fullmatch(r'\w+: -?\d+\.\d\d', line):
                        labelled.add(line.split(':')[0])
                    cells = line.split()
                    if cells and all(re.fullmatch(r'[a-z0-9_]+', cell) for cell in cells) and len(cells) > 3:
                        header = cells
                        index = lines.index(line)
                        values = lines[index + 1].split()
                        labelled.update(
                            name for name, value in zip(header, values) if re.fullmatch(r'\d+\.\d\d', value)
                        )
                self.assertTrue(labelled)
                for name in labelled:
                    self.assertTrue(name.endswith('_ms'), '%s holds a duration but is not labelled in ms' % name)

    def test_timestamps_are_iso_8601_utc(self):
        text = render.render_text(data.request_detail(self.slow.pk))
        self.assertRegex(text, r'\nstart_time: %s\n' % TIMESTAMP.pattern)
        self.assertRegex(text, r'\nend_time: %s\n' % TIMESTAMP.pattern)
        row = render.render_text(data.request_list()).split('\n')[5]
        self.assertRegex(row.split()[1], '^%s$' % TIMESTAMP.pattern)
        summary = render.render_text(data.summary())
        self.assertRegex(summary, r'\nstart_time_from: {}\nstart_time_to: {}\n'.format(TIMESTAMP.pattern, TIMESTAMP.pattern))

    def test_no_escape_sequences(self):
        timed_query(self.fast, "MATCH (n) WHERE n.name = '\x1b[31mred\x1b[0m\x07' RETURN n", 1)
        outputs = [
            render.render_text(data.request_list()),
            render.render_text(data.request_detail(self.fast.pk)),
            render.render_text(data.request_queries(self.fast.pk)),
            render.render_text(data.query_detail(data.request_queries(self.fast.pk)['queries'][-1]['id'])),
            render.render_text(data.shape_list()),
        ]
        for text in outputs:
            self.assertIsNone(re.search(r'[\x00-\x09\x0b-\x1f\x7f]', text), text)
        # What was recorded is still readable.
        self.assertIn(r"n.name = '\x1b[31mred\x1b[0m\x07'", outputs[3])

    def test_request_list_layout(self):
        lines = render.render_text(data.request_list()).split('\n')
        self.assertEqual(lines[:4], ['requests', 'matching: 2', 'sort: start desc', ''])
        self.assertEqual(lines[4].split(), [
            'id', 'start_time', 'method', 'status', 'time_ms', 'queries_ms', 'queries', 'sql', 'cypher', 'view',
            'path',
        ])
        self.assertEqual(lines[5].split()[0], self.fast.pk[:8])
        self.assertEqual(lines[6].split()[2:], [
            'GET', '200', '2000.00', '302.00', '10', '3', '7', 'training:openings', '/api/training/openings/',
        ])
        self.assertEqual(lines[-3:], [
            '  python manage.py silk_query request %s' % self.fast.pk[:8],
            '  python manage.py silk_query requests --sort time',
            '  python manage.py silk_query summary',
        ])
        self.assertEqual(lines[-4], 'NEXT')

    def test_filters_are_shown(self):
        lines = render.render_text(data.request_list({'view': 'boards:list', 'min_time': 10})).split('\n')
        self.assertIn('filters: --view boards:list --min-time 10', lines)

    def test_nothing_matched(self):
        lines = render.render_text(data.request_list({'since': '1m'})).split('\n')
        self.assertIn('Nothing matched: no recorded request passes these filters.', lines)
        self.assertIn('recorded_requests: 2', lines)
        self.assertIn('latest_request_start: %s' % data.iso(self.fast.start_time), lines)

    def test_query_layout(self):
        lines = render.render_text(data.query_detail(self.repeated[0].pk)).split('\n')
        self.assertEqual(lines[0], 'query %s' % self.repeated[0].pk)
        self.assertIn('kind: cypher', lines)
        self.assertIn('call_site: /backend/training/serializers.py:114 in get_moves_from_parent', lines)
        self.assertEqual(lines[lines.index('TEXT') + 1], "MATCH (o) WHERE elementId(o)='0' RETURN o")
        self.assertIn('No plan is recorded for Cypher queries', lines[lines.index('PLAN') + 1])
        start = lines.index('TRACEBACK (application frames, innermost first)')
        self.assertEqual(lines[start + 1:start + 6], [
            '  /backend/training/serializers.py:114 in get_moves_from_parent',
            '    if not obj.parent:',
            '  /backend/training/views.py:133 in get',
            '    ).data,',
            '[traceback: 2 of 24 frames shown; application frames only, --full-traceback prints every frame]',
        ])
        self.assertNotIn('tables:', '\n'.join(lines))

    def test_query_full_traceback(self):
        lines = render.render_text(data.query_detail(self.repeated[0].pk, full_traceback=True)).split('\n')
        start = lines.index('TRACEBACK (every frame, innermost first)')
        frames = [line for line in lines[start + 1:lines.index('NEXT')] if re.match(r'  \S', line)]
        self.assertEqual(len(frames), 24)
        self.assertIn('site-packages/neomodel', frames[1])
        self.assertFalse([line for line in lines if line.startswith('[traceback')])

    def test_sql_query_layout(self):
        lines = render.render_text(data.query_detail(self.sql.pk)).split('\n')
        self.assertIn('tables: "users_user", "t"', lines)
        self.assertIn('joins: 1', lines)
        self.assertIn('call_site: unknown', lines)
        self.assertEqual(lines[lines.index('PLAN') + 1], 'Index Scan using users_user_pkey')
        self.assertIn('No application frame: every recorded frame is library code.', lines)

    def test_profiles_explain_where_blocks_come_from(self):
        self.profile.delete()
        text = render.render_text(data.profile_list())
        self.assertIn('no profile block has been recorded', text)
        self.assertIn('silk_profile decorator or context manager', text)
        self.assertIn('dynamic profiling', text)
        self.assertIn('recorded_profiles: 0', text)


class TestTruncation(ReportTestCase):

    def test_cut_list(self):
        for n in range(30):
            recorded_request(time_ms=n)
        lines = render.render_text(data.request_list()).split('\n')
        self.assertEqual(lines[1], 'matching: 32')
        rows = lines[5:lines.index('NEXT') - 2]
        self.assertEqual(len(rows), 20)
        self.assertEqual(
            lines[lines.index('NEXT') - 2],
            '[requests: 20 of 32 rows shown; --offset 20 shows the next rows, --limit N shows more (0 for all)]',
        )
        lines = render.render_text(data.request_list(limit=5, offset=20)).split('\n')
        self.assertIn(
            '[requests: rows 21-25 of 32 shown; --offset 25 shows the next rows, --limit N shows more (0 for all)]',
            lines,
        )
        lines = render.render_text(data.request_list(offset=40)).split('\n')
        self.assertIn('[requests: no rows at offset 40, there are 32; --limit N shows more (0 for all)]', lines)
        text = render.render_text(data.request_list(limit=0))
        self.assertNotIn('[requests', text)
        self.assertEqual(len(text.split('\n')[5:-5]), 32)

    def test_cut_query_text_in_a_list(self):
        long_query = 'MATCH (n) WHERE n.uid IN [%s] RETURN n' % ', '.join("'u%d'" % n for n in range(100))
        query = timed_query(self.fast, long_query, 500)
        lines = render.render_text(data.request_queries(self.fast.pk, sort='time')).split('\n')
        row = next(line for line in lines if line.startswith(str(query.pk)))
        self.assertTrue(row.endswith('  %s ...[%d chars]' % (long_query[:200], len(long_query))))
        self.assertIn(
            '[queries.text: cut to 200 chars in 1 row(s), the longest has %d; --max-chars N shows more (0 for all)]'
            % len(long_query),
            lines,
        )

    def test_cut_query_text_in_a_detail_view(self):
        long_query = 'MATCH (n) WHERE n.uid IN [%s] RETURN n' % ', '.join("'u%d'" % n for n in range(900))
        query = timed_query(self.fast, long_query, 500)
        lines = render.render_text(data.query_detail(query.pk)).split('\n')
        text = lines[lines.index('TEXT') + 1]
        self.assertEqual(text, long_query[:4000])
        self.assertEqual(
            lines[lines.index('TEXT') + 2],
            '[text: 4000 of %d chars shown; --max-chars 0 prints it whole]' % len(long_query),
        )

    def test_no_limit(self):
        long_query = 'MATCH (n) WHERE n.uid IN [%s] RETURN n' % ', '.join("'u%d'" % n for n in range(900))
        query = timed_query(self.fast, long_query, 500)
        text = render.render_text(data.query_detail(query.pk, max_chars=0))
        self.assertIn(long_query, text)
        self.assertNotIn('[text:', text)
        text = render.render_text(data.request_queries(self.fast.pk, max_chars=0))
        self.assertIn(long_query, text)
        self.assertNotIn('chars]', text)
        self.assertNotIn('[queries.text', text)

    def test_custom_limit(self):
        lines = render.render_text(data.request_detail(self.slow.pk, max_chars=10, limit=1)).split('\n')
        start = lines.index('SHAPES (4, by total time)')
        self.assertTrue(lines[start + 2].endswith('MATCH (roo ...[32 chars]'))
        self.assertEqual(lines[start + 3:start + 5], [
            '[shapes: 1 of 4 rows shown; --offset 1 shows the next rows, --limit N shows more (0 for all)]',
            '[shapes.text: cut to 10 chars in 1 row(s), the longest has 32; --max-chars N shows more (0 for all)]',
        ])

    def test_summary_coverage(self):
        lines = render.render_text(data.summary(max_requests=1)).split('\n')
        self.assertEqual(lines[1:4], [
            'requests_matching: 2',
            'requests_analysed: 1',
            '[requests_analysed: 1 of 2 matching requests analysed; these are the most recent ones, '
            '--max-requests N analyses more (0 for all)]',
        ])


class TestJsonRenderer(ReportTestCase):

    def check(self, report):
        document = json.loads(render.render_json(report))
        self.assertIsInstance(document, dict)
        self.assertEqual(document, json.loads(json.dumps(report, default=str)))
        self.assertIsInstance(document['truncated'], list)
        self.assertIsInstance(document['next'], list)
        self.assertTrue(document['next'])
        assert_text_is_in_json(self, render.render_text(report), document)
        return document

    def test_summary(self):
        document = self.check(data.summary())
        self.assertEqual(document['views'][0]['median_ms'], 2000.0)

    def test_requests(self):
        document = self.check(data.request_list())
        self.assertEqual(document['requests'][1]['id'], self.slow.pk)
        self.assertEqual(document['requests'][1]['time_ms'], 2000.0)

    def test_request(self):
        document = self.check(data.request_detail(self.slow.pk))
        self.assertEqual(document['id'], self.slow.pk)
        self.assertEqual(len(document['shapes']), 4)
        self.assertEqual(document['hints'][0]['hint'], 'repeated_shape')
        self.assertEqual(document['queries_by_kind'], {'sql': 3, 'cypher': 7, 'other': 0})

    def test_request_with_sections(self):
        report = data.request_detail(self.slow.pk, headers=True, bodies=True, cprofile=True, curl=True)
        document = self.check(report)
        self.assertEqual(document['request_headers']['host'], 'localhost:8000')
        self.assertIsNone(document['cprofile'])

    def test_queries(self):
        document = self.check(data.request_queries(self.slow.pk))
        self.assertEqual(len(document['queries']), 10)

    def test_query(self):
        document = self.check(data.query_detail(self.repeated[0].pk))
        self.assertEqual(document['call_site']['line'], 114)
        self.check(data.query_detail(self.sql.pk))

    def test_shapes(self):
        document = self.check(data.shape_list())
        self.assertEqual(document['shapes'][0]['shape'], self.root_shape)

    def test_profiles(self):
        document = self.check(data.profile_list())
        self.assertEqual(document['profiles'][0]['id'], self.profile.pk)

    def test_profile(self):
        document = self.check(data.profile_detail(self.profile.pk))
        self.assertEqual(document['time_ms'], 300.0)

    def test_nothing_matched(self):
        document = self.check(data.request_list({'since': '1m'}))
        self.assertEqual(document['empty']['recorded_requests'], 2)
        self.assertEqual(document['requests'], [])

    def test_truncation_is_carried(self):
        document = self.check(data.request_detail(self.slow.pk, limit=1, max_chars=10))
        self.assertEqual([entry['key'] for entry in document['truncated']], ['shapes', 'shapes.text'])
        self.assertEqual(document['shapes'][0]['text'], 'MATCH (roo')
        self.assertEqual(document['shapes'][0]['text_chars'], 32)

    def test_non_ascii_text_is_kept(self):
        query = timed_query(self.fast, "MATCH (n) WHERE n.name = 'caf\u00e9' RETURN n", 1)
        output = render.render_json(data.query_detail(query.pk))
        self.assertIn('caf\u00e9', output)
        self.assertNotIn('\n', output)
