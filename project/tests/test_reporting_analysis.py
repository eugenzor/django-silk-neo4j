from django.test import SimpleTestCase

from silk.reporting import analysis

from .data import tracebacks


def shape_id(text, kind=None):
    return analysis.shape(text, kind or analysis.kind(text))[0]


class TestKind(SimpleTestCase):

    def test_sql_keywords(self):
        for text in (
            'SELECT "a"."id" FROM "a"',
            'select 1',
            'INSERT INTO "a" ("b") VALUES (1)',
            'UPDATE "a" SET "b" = 1',
            'DELETE FROM "a" WHERE "a"."id" = 1',
            'SAVEPOINT "s1"',
            'CREATE TABLE "a" ("id" integer)',
            'CREATE INDEX "a_b" ON "a" ("b")',
            'PRAGMA foreign_keys = OFF',
        ):
            with self.subTest(text=text):
                self.assertEqual(analysis.kind(text), 'sql')

    def test_cypher_keywords(self):
        for text in (
            'MATCH (n:Opening) RETURN n',
            'OPTIONAL MATCH (n)-[r]->(m) RETURN m',
            'CALL dbms.components() yield versions, edition return versions[0], edition',
            'MERGE (n:Opening {bmn: 1})',
            'UNWIND [1, 2] AS x RETURN x',
            'CREATE (n:Opening {bmn: 1})',
            'DETACH DELETE n',
        ):
            with self.subTest(text=text):
                self.assertEqual(analysis.kind(text), 'cypher')

    def test_leading_whitespace_is_ignored(self):
        self.assertEqual(analysis.kind('\n            MATCH (root:Opening) RETURN root'), 'cypher')
        self.assertEqual(analysis.kind('  SELECT 1'), 'sql')

    def test_delete(self):
        self.assertEqual(analysis.kind('DELETE FROM "authtoken_token" WHERE "authtoken_token"."key" = abc'), 'sql')
        self.assertEqual(analysis.kind('DELETE n'), 'cypher')

    def test_with(self):
        self.assertEqual(analysis.kind('WITH x AS (SELECT 1) SELECT * FROM x'), 'sql')
        self.assertEqual(analysis.kind('WITH opening MATCH (opening)<-[r1]-(parent) RETURN parent'), 'cypher')

    def test_explain_output_means_sql(self):
        self.assertEqual(analysis.kind('EXPLAIN something', has_analysis=True), 'sql')
        self.assertEqual(analysis.kind('MATCH (n) RETURN n', has_analysis=True), 'sql')
        self.assertEqual(analysis.kind('EXPLAIN MATCH (n) RETURN n'), 'cypher')

    def test_unrecognised(self):
        self.assertEqual(analysis.kind('VACUUM'), 'other')
        self.assertEqual(analysis.kind(''), 'other')
        self.assertEqual(analysis.kind(None), 'other')
        # A keyword is a whole word.
        self.assertEqual(analysis.kind('MATCHES'), 'other')


class TestShape(SimpleTestCase):

    def test_quoted_strings(self):
        a = "MATCH (opening) WHERE elementId(opening)='4:440c51d5:1' WITH opening RETURN opening"
        b = "MATCH (opening) WHERE elementId(opening)='4:99fe:207' WITH opening RETURN opening"
        self.assertEqual(shape_id(a), shape_id(b))
        self.assertEqual(
            analysis.normalise(a, 'cypher'),
            'MATCH (opening) WHERE elementId(opening)=? WITH opening RETURN opening',
        )

    def test_quoted_string_with_escaped_quote(self):
        a = r"MATCH (p:Progress {user_uid: 'it\'s', n: 1}) RETURN p"
        b = "MATCH (p:Progress {user_uid: 'other', n: 2}) RETURN p"
        self.assertEqual(shape_id(a), shape_id(b))

    def test_double_quotes_are_strings_in_cypher_and_identifiers_in_sql(self):
        self.assertEqual(
            shape_id('MATCH (n {name: "a"}) RETURN n'),
            shape_id('MATCH (n {name: "b"}) RETURN n'),
        )
        self.assertNotEqual(
            shape_id('SELECT "a"."id" FROM "a"'),
            shape_id('SELECT "b"."id" FROM "b"'),
        )

    def test_numbers(self):
        a = 'SELECT "django_site"."id" FROM "django_site" WHERE "django_site"."id" = 1 LIMIT 21'
        b = 'SELECT "django_site"."id" FROM "django_site" WHERE "django_site"."id" = 23 LIMIT 5'
        self.assertEqual(shape_id(a), shape_id(b))
        self.assertTrue(analysis.normalise(a, 'sql').endswith('WHERE "django_site"."id" = ? LIMIT ?'))
        self.assertEqual(
            shape_id('MATCH (n:Board) WHERE n.rating > 1500.5 RETURN n LIMIT 10'),
            shape_id('MATCH (n:Board) WHERE n.rating > 20 RETURN n LIMIT 3'),
        )

    def test_unquoted_sql_values(self):
        a = 'SELECT 1 AS "a" FROM "django_session" WHERE "django_session"."session_key" = rrhj0un4enkkf7 LIMIT 1'
        b = 'SELECT 1 AS "a" FROM "django_session" WHERE "django_session"."session_key" = zz91 LIMIT 1'
        self.assertEqual(shape_id(a), shape_id(b))
        self.assertEqual(
            analysis.normalise(a, 'sql'),
            'SELECT ? AS "a" FROM "django_session" WHERE "django_session"."session_key" = ? LIMIT ?',
        )

    def test_unquoted_sql_values_in_a_condition_list(self):
        a = ('SELECT 1 AS "a" FROM "e" WHERE (NOT ("e"."id" = 4) AND "e"."email" = a@example.com '
             'AND "e"."created" >= 2026-10-10 10:00:00+00:00 AND "e"."verified") LIMIT 1')
        b = ('SELECT 1 AS "a" FROM "e" WHERE (NOT ("e"."id" = 17) AND "e"."email" = someone.else@example.org '
             'AND "e"."created" >= 2025-01-02 03:04:05.678901+00:00 AND "e"."verified") LIMIT 1')
        self.assertEqual(shape_id(a), shape_id(b))
        self.assertEqual(
            analysis.normalise(a, 'sql'),
            'SELECT ? AS "a" FROM "e" WHERE (NOT ("e"."id" = ?) AND "e"."email" = ? '
            'AND "e"."created" >= ? AND "e"."verified") LIMIT ?',
        )

    def test_unquoted_sql_values_in_an_update(self):
        a = 'UPDATE "users_user" SET "last_login" = 2026-10-10 10:00:00, "level" = 3 WHERE "users_user"."id" = 19'
        b = 'UPDATE "users_user" SET "last_login" = 2026-01-01 00:00:00.5, "level" = 12 WHERE "users_user"."id" = 2'
        self.assertEqual(shape_id(a), shape_id(b))
        self.assertEqual(
            analysis.normalise(a, 'sql'),
            'UPDATE "users_user" SET "last_login" = ?, "level" = ? WHERE "users_user"."id" = ?',
        )

    def test_empty_sql_value(self):
        a = 'UPDATE "users_user" SET "first_name" = , "last_name" = Smith WHERE ("users_user"."uid" =  AND "users_user"."id" = 19)'
        b = 'UPDATE "users_user" SET "first_name" = Ann, "last_name" =  WHERE ("users_user"."uid" = x AND "users_user"."id" = 2)'
        self.assertEqual(shape_id(a), shape_id(b))
        self.assertEqual(
            analysis.normalise(a, 'sql'),
            'UPDATE "users_user" SET "first_name" = ?, "last_name" = ? '
            'WHERE ("users_user"."uid" = ? AND "users_user"."id" = ?)',
        )

    def test_sql_value_inside_a_function_call(self):
        a = 'SELECT 1 AS "a" FROM "e" WHERE UPPER("e"."email"::text) = UPPER(a@example.com) LIMIT 1'
        self.assertEqual(
            analysis.normalise(a, 'sql'),
            'SELECT ? AS "a" FROM "e" WHERE UPPER("e"."email"::text) = UPPER(?) LIMIT ?',
        )

    def test_call_arguments_and_indexes_are_not_lists(self):
        a = 'CALL dbms.components() yield versions, edition return versions[0], edition'
        self.assertEqual(
            analysis.normalise(a, 'cypher'),
            'CALL dbms.components() yield versions, edition return versions[?], edition',
        )
        self.assertNotEqual(shape_id('RETURN point(1, 2)'), shape_id('RETURN point(1)'))

    def test_variable_length_path(self):
        self.assertEqual(
            analysis.normalise('MATCH (a)-[:REL*0..3]->(b) RETURN b.bmn', 'cypher'),
            'MATCH (a)-[:REL*?..?]->(b) RETURN b.bmn',
        )

    def test_sql_like(self):
        a = 'SELECT "a"."id" FROM "a" WHERE "a"."name" LIKE %foo% ESCAPE \'\\\''
        b = 'SELECT "a"."id" FROM "a" WHERE "a"."name" LIKE %something else% ESCAPE \'\\\''
        self.assertEqual(shape_id(a), shape_id(b))
        self.assertEqual(
            analysis.normalise(b, 'sql'),
            'SELECT "a"."id" FROM "a" WHERE "a"."name" LIKE ? ESCAPE ?',
        )

    def test_lower_case_keyword_inside_a_value(self):
        a = 'SELECT "a"."id" FROM "a" WHERE "a"."name" = rock and roll or else LIMIT 21'
        b = 'SELECT "a"."id" FROM "a" WHERE "a"."name" = jazz LIMIT 21'
        self.assertEqual(shape_id(a), shape_id(b))

    def test_sql_column_comparisons_are_kept(self):
        text = 'SELECT "a"."id" FROM "a" INNER JOIN "b" T3 ON ("a"."id" = T3."a_id") WHERE "a"."x" = "a"."y"'
        self.assertEqual(analysis.normalise(text, 'sql'), text)

    def test_in_list_length(self):
        a = 'SELECT "a"."id" FROM "a" WHERE "a"."id" IN (1, 2, 3)'
        b = 'SELECT "a"."id" FROM "a" WHERE "a"."id" IN (4)'
        c = 'SELECT "a"."id" FROM "a" WHERE "a"."name" IN (anna, bob smith, carl, dan)'
        self.assertEqual(shape_id(a), shape_id(b))
        self.assertEqual(analysis.normalise(a, 'sql'), 'SELECT "a"."id" FROM "a" WHERE "a"."id" IN (?+)')
        self.assertEqual(
            analysis.normalise(c, 'sql'),
            'SELECT "a"."id" FROM "a" WHERE "a"."name" IN (?+)',
        )
        self.assertEqual(
            shape_id("MATCH (n) WHERE n.uid IN ['a', 'b', 'c'] RETURN n"),
            shape_id("MATCH (n) WHERE n.uid IN ['d'] RETURN n"),
        )

    def test_in_subquery_is_kept(self):
        text = 'SELECT "a"."id" FROM "a" WHERE "a"."id" IN (SELECT U0."a_id" FROM "b" U0)'
        self.assertEqual(analysis.normalise(text, 'sql'), text)

    def test_different_structure(self):
        self.assertNotEqual(
            shape_id('SELECT "a"."id" FROM "a" WHERE "a"."id" = 1'),
            shape_id('SELECT "b"."id" FROM "b" WHERE "b"."id" = 1'),
        )
        self.assertNotEqual(
            shape_id('MATCH (n:Opening) RETURN n'),
            shape_id('MATCH (n:Board) RETURN n'),
        )

    def test_identifiers_with_digits_are_kept(self):
        cypher = 'MATCH (opening)<-[r1:`RELATED_OPENING`]-(parent_r1:Opening) RETURN parent_r1 LIMIT 5'
        self.assertEqual(
            analysis.normalise(cypher, 'cypher'),
            'MATCH (opening)<-[r1:`RELATED_OPENING`]-(parent_r1:Opening) RETURN parent_r1 LIMIT ?',
        )
        sql = 'SELECT T3."id" FROM "a" T3 WHERE T3."id" = 7'
        self.assertEqual(analysis.normalise(sql, 'sql'), 'SELECT T3."id" FROM "a" T3 WHERE T3."id" = ?')

    def test_whitespace(self):
        self.assertEqual(
            shape_id('\n            MATCH (root:Opening)\n            RETURN root.bmn\n        '),
            shape_id('MATCH (root:Opening) RETURN root.bmn'),
        )

    def test_id_is_stable(self):
        text = 'SELECT "a"."id" FROM "a" WHERE "a"."id" = 1'
        first = shape_id(text)
        self.assertEqual(first, shape_id(text))
        self.assertRegex(first, r'^[0-9a-f]{8}$')
        # Pinned, so a change to the normalisation that renames every shape is noticed.
        self.assertEqual(first, analysis.shape_id('SELECT "a"."id" FROM "a" WHERE "a"."id" = ?'))
        self.assertEqual(analysis.shape_id('MATCH (n) RETURN n'), 'a8ad4abe')

    def test_value_containing_and_splits_the_shape(self):
        # Known limitation, see "Risks / Trade-offs" in
        # openspec/changes/add-silk-query-command/design.md: recorded SQL values
        # are unquoted, so a value is taken to end at the first ` AND `.
        simple = 'SELECT "a"."id" FROM "a" WHERE "a"."name" = rock'
        with_and = 'SELECT "a"."id" FROM "a" WHERE "a"."name" = rock AND roll'
        self.assertNotEqual(shape_id(simple), shape_id(with_and))
        self.assertEqual(
            analysis.normalise(with_and, 'sql'),
            'SELECT "a"."id" FROM "a" WHERE "a"."name" = ? AND roll',
        )


class TestCallSite(SimpleTestCase):

    def test_parse(self):
        frames = analysis.parse_traceback(tracebacks.NEOMODEL)
        self.assertEqual(len(frames), 24)
        self.assertEqual(frames[0], {
            'file': '/usr/local/lib/python3.11/site-packages/silk/sql.py',
            'line': 115,
            'function': 'execute_cypher',
            'source': "tb = ''.join(reversed(traceback.format_stack()))",
        })
        self.assertEqual(frames[-1]['function'], '_bootstrap')

    def test_parse_empty(self):
        self.assertEqual(analysis.parse_traceback(''), [])
        self.assertEqual(analysis.parse_traceback(None), [])
        self.assertEqual(analysis.parse_traceback('Traceback #1'), [])

    def test_neomodel_query_from_a_serializer(self):
        self.assertEqual(analysis.call_site(tracebacks.NEOMODEL), {
            'file': '/backend/training/serializers.py',
            'line': 114,
            'function': 'get_moves_from_parent',
            'source': 'if not obj.parent:',
        })
        frames = analysis.application_frames(analysis.parse_traceback(tracebacks.NEOMODEL))
        self.assertEqual(
            [(frame['file'], frame['line']) for frame in frames],
            [('/backend/training/serializers.py', 114), ('/backend/training/views.py', 133)],
        )

    def test_neomodel_query_from_a_view(self):
        frame = analysis.call_site(tracebacks.NEOMODEL_FROM_VIEW)
        self.assertEqual((frame['file'], frame['line'], frame['function']), ('/backend/training/views.py', 128, 'get'))
        self.assertEqual(analysis.describe_frame(frame), '/backend/training/views.py:128 in get')

    def test_other_python_version(self):
        frame = analysis.call_site(tracebacks.OTHER_PYTHON)
        self.assertEqual(frame, {
            'file': '/backend/users/services.py',
            'line': 41,
            'function': 'current_rating',
            'source': 'return Rating.objects.get(user=user)',
        })

    def test_library_frames_only(self):
        self.assertIsNone(analysis.call_site(tracebacks.LIBRARY_ONLY))
        self.assertEqual(analysis.application_frames(analysis.parse_traceback(tracebacks.LIBRARY_ONLY)), [])
        self.assertEqual(analysis.describe_frame(None), 'unknown')

    def test_innermost_frame_inside_silk(self):
        frame = analysis.call_site(tracebacks.SILK_CHECKOUT)
        self.assertEqual((frame['file'], frame['line']), ('/backend/training/views.py', 133))
        frames = analysis.application_frames(analysis.parse_traceback(tracebacks.SILK_CHECKOUT))
        self.assertEqual(len(frames), 1)


def execution(pk, text, time_ms, frame=None):
    kind = analysis.kind(text)
    shape, shape_text = analysis.shape(text, kind)
    return {
        'id': pk, 'text': text, 'kind': kind, 'shape': shape, 'shape_text': shape_text,
        'time_ms': time_ms, 'call_site': frame,
    }


VIEW_FRAME = {'file': '/backend/training/views.py', 'line': 133, 'function': 'get', 'source': ').data,'}


def repeated(count, time_ms=1.0, frame=VIEW_FRAME):
    return [
        execution(n, "MATCH (o) WHERE elementId(o)='%d' RETURN o" % n, time_ms, frame)
        for n in range(1, count + 1)
    ]


def hint_names(*args, **kwargs):
    return [hint['hint'] for hint in analysis.hints(*args, **kwargs)]


class TestHints(SimpleTestCase):

    def test_repeated_shape_at_threshold(self):
        found = analysis.hints(None, repeated(5))
        self.assertEqual([hint['hint'] for hint in found], ['repeated_shape'])
        hint = found[0]
        self.assertEqual(hint['count'], 5)
        self.assertEqual(hint['total_ms'], 5.0)
        self.assertEqual(hint['call_site'], VIEW_FRAME)
        self.assertIn(hint['shape'], hint['message'])
        self.assertIn('ran 5 times', hint['message'])
        self.assertIn('5.00 ms', hint['message'])
        self.assertIn('/backend/training/views.py:133 in get', hint['message'])
        self.assertIn('likely N+1 pattern', hint['message'])

    def test_repeated_shape_below_threshold(self):
        self.assertEqual(hint_names(None, repeated(4)), [])

    def test_repeated_shape_without_call_site(self):
        hint = analysis.hints(None, repeated(5, frame=None))[0]
        self.assertIsNone(hint['call_site'])
        self.assertIn('unknown call site', hint['message'])

    def test_identical_queries_at_threshold(self):
        executions = [execution(1, 'SELECT 1', 2.0), execution(2, 'SELECT 1', 3.0), execution(3, 'SELECT 2', 1.0)]
        found = analysis.hints(None, executions)
        self.assertEqual([hint['hint'] for hint in found], ['identical_queries'])
        hint = found[0]
        self.assertEqual((hint['distinct'], hint['count'], hint['total_ms']), (1, 2, 5.0))
        self.assertEqual((hint['query'], hint['query_count']), (1, 2))
        self.assertIn('2 executions', hint['message'])

    def test_identical_queries_below_threshold(self):
        # The same shape, but no text runs twice.
        executions = [execution(1, 'SELECT 1', 2.0), execution(2, 'SELECT 2', 3.0)]
        self.assertEqual(hint_names(None, executions), [])

    def test_dominant_query_at_threshold(self):
        executions = [execution(1, 'SELECT 1', 50.0), execution(2, 'MATCH (n) RETURN n', 1.0)]
        found = analysis.hints(100, executions)
        self.assertEqual([hint['hint'] for hint in found], ['dominant_query'])
        self.assertEqual((found[0]['query'], found[0]['time_ms'], found[0]['share_percent']), (1, 50.0, 50))
        self.assertIn('query 1', found[0]['message'])

    def test_dominant_query_below_threshold(self):
        executions = [execution(1, 'SELECT 1', 49.9), execution(2, 'MATCH (n) RETURN n', 1.0)]
        self.assertEqual(hint_names(100, executions), [])

    def test_time_outside_queries_at_threshold(self):
        # 100 ms outside queries is both half of the request and the minimum.
        executions = [execution(1, 'SELECT 1', 60.0), execution(2, 'MATCH (n) RETURN n', 40.0)]
        found = analysis.hints(200, executions)
        self.assertEqual([hint['hint'] for hint in found], ['time_outside_queries'])
        self.assertEqual(found[0]['outside_queries_ms'], 100.0)
        self.assertFalse(found[0]['profiler_output'])

    def test_time_outside_queries_below_share(self):
        executions = [execution(1, 'SELECT 1', 60.0), execution(2, 'MATCH (n) RETURN n', 41.0)]
        self.assertEqual(hint_names(200, executions), [])

    def test_time_outside_queries_below_minimum(self):
        # 99 ms is all of the request, but too little to be worth a hint.
        self.assertEqual(hint_names(99, []), [])
        self.assertEqual(hint_names(100, []), ['time_outside_queries'])

    def test_time_outside_queries_without_profiler(self):
        executions = [execution(1, 'SELECT 1', 200.0), execution(2, 'MATCH (n) RETURN n', 100.0)]
        hint = analysis.hints(2000, executions)[0]
        self.assertEqual(hint['outside_queries_ms'], 1700.0)
        self.assertIn('1700.00 ms', hint['message'])
        self.assertIn('cannot attribute', hint['message'])
        self.assertIn('SILKY_PYTHON_PROFILER', hint['message'])

    def test_time_outside_queries_with_profiler(self):
        hint = analysis.hints(2000, [], has_profiler_output=True)[0]
        self.assertTrue(hint['profiler_output'])
        self.assertIn('--cprofile', hint['message'])
        self.assertNotIn('cannot attribute', hint['message'])

    def test_none_apply(self):
        executions = [execution(1, 'SELECT 1', 30.0), execution(2, 'MATCH (n) RETURN n', 30.0)]
        self.assertEqual(analysis.hints(100, executions), [])

    def test_request_without_a_duration(self):
        self.assertEqual(hint_names(None, [execution(1, 'SELECT 1', 30.0)]), [])
        self.assertEqual(hint_names(None, repeated(5)), ['repeated_shape'])

    def test_several_hints(self):
        executions = repeated(310, time_ms=1.0) + [execution(999, 'SELECT 1', 1000.0)]
        self.assertEqual(hint_names(1500, executions), ['repeated_shape', 'dominant_query'])


class TestGroupShapes(SimpleTestCase):

    def test_aggregates(self):
        other = {'file': '/backend/training/serializers.py', 'line': 114, 'function': 'f', 'source': ''}
        executions = repeated(3, time_ms=2.0) + [
            execution(10, "MATCH (o) WHERE elementId(o)='x' RETURN o", 10.0, other),
            execution(11, 'SELECT 1', 100.0),
            execution(12, 'SELECT 2', None),
        ]
        select, match = analysis.group_shapes(executions)
        self.assertEqual((select['kind'], select['count'], select['total_ms']), ('sql', 2, 100.0))
        self.assertEqual((select['avg_ms'], select['max_ms'], select['slowest_query']), (50.0, 100.0, 11))
        self.assertIsNone(select['call_site'])
        self.assertEqual((match['kind'], match['count'], match['total_ms']), ('cypher', 4, 16.0))
        self.assertEqual((match['avg_ms'], match['max_ms'], match['slowest_query']), (4.0, 10.0, 10))
        # The most frequent call site, not the slowest one.
        self.assertEqual(match['call_site'], VIEW_FRAME)
        self.assertEqual(match['call_sites'], 2)
        self.assertEqual(match['text'], 'MATCH (o) WHERE elementId(o)=? RETURN o')


class TestPercentiles(SimpleTestCase):

    def test_no_values(self):
        self.assertIsNone(analysis.median([]))
        self.assertIsNone(analysis.percentile([None], 95))

    def test_one_value(self):
        self.assertEqual(analysis.median([7.0]), 7.0)
        self.assertEqual(analysis.percentile([7.0], 95), 7.0)

    def test_two_values(self):
        self.assertEqual(analysis.median([10.0, 20.0]), 15.0)
        self.assertAlmostEqual(analysis.percentile([20.0, 10.0], 95), 19.5)

    def test_many_values(self):
        values = list(range(1, 101))
        self.assertEqual(analysis.median(values), 50.5)
        self.assertAlmostEqual(analysis.percentile(values, 95), 95.05)
        self.assertEqual(analysis.percentile(values, 100), 100)

    def test_missing_values_are_skipped(self):
        self.assertEqual(analysis.median([None, 1.0, 3.0, None]), 2.0)
