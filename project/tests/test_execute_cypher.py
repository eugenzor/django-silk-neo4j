import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from django.test import TestCase

from silk.collector import DataCollector
from silk.config import SilkyConfig
from silk.models import Request, SQLQuery
from silk.neo4j import _wrap_async, _wrap_sync, patch_neo4j, render_cypher

from .util import delete_all_models

try:
    import neo4j
except ImportError:  # pragma: no cover
    neo4j = None


class TestRenderCypher(TestCase):
    def test_no_parameters(self):
        self.assertEqual('MATCH (n) RETURN n', render_cypher('MATCH (n) RETURN n'))

    def test_positional_and_keyword_parameters(self):
        rendered = render_cypher('MATCH (n {a: $a, b: $b}) RETURN n', {'a': 1}, {'b': 'x'})
        self.assertEqual("MATCH (n {a: 1, b: 'x'}) RETURN n", rendered)

    def test_name_sharing_a_prefix(self):
        rendered = render_cypher('RETURN $a, $ab', {'a': 1, 'ab': 2})
        self.assertEqual('RETURN 1, 2', rendered)

    def test_unknown_parameter_left_untouched(self):
        self.assertEqual('RETURN $missing, 1', render_cypher('RETURN $missing, $a', {'a': 1}))

    def test_literals(self):
        rendered = render_cypher(
            'RETURN $none, $flag, $text, $items, $props',
            {'none': None, 'flag': True, 'text': "it's", 'items': [1, 'a'], 'props': {'k': 1.5}},
        )
        self.assertEqual("RETURN null, true, 'it\\'s', [1, 'a'], {k: 1.5}", rendered)

    def test_query_object(self):
        query = Mock(spec_set=['text'], text='RETURN $a')
        self.assertEqual('RETURN 1', render_cypher(query, {'a': 1}))


class CypherTestCase(TestCase):
    def setUp(self):
        super().setUp()
        delete_all_models(SQLQuery)
        self.original = Mock(return_value='result')
        self.run = _wrap_sync(self.original)
        self.session = Mock()

    def tearDown(self):
        DataCollector().clear()
        super().tearDown()


class TestCallNoRequest(CypherTestCase):
    def test_called_and_not_recorded(self):
        DataCollector().configure(request=None)
        result = self.run(self.session, 'RETURN $a', {'a': 1}, b=2)
        self.assertEqual('result', result)
        self.original.assert_called_once_with(self.session, 'RETURN $a', {'a': 1}, b=2)
        self.assertEqual(0, len(DataCollector().queries))


class TestCallRequest(CypherTestCase):
    def setUp(self):
        super().setUp()
        self.request = Request.objects.create(path='/path/to/somewhere')
        DataCollector().configure(request=self.request, should_profile=False)

    def test_recorded(self):
        result = self.run(self.session, 'MATCH (n {id: $id}) RETURN n', {'id': 7})
        self.assertEqual('result', result)
        self.original.assert_called_once_with(self.session, 'MATCH (n {id: $id}) RETURN n', {'id': 7})
        query = list(DataCollector().queries.values())[0]
        self.assertEqual('MATCH (n {id: 7}) RETURN n', query['query'])
        self.assertEqual(self.request, query['request'])
        self.assertIsNone(query['analysis'])
        self.assertTrue(query['traceback'])
        self.assertLessEqual(query['start_time'], query['end_time'])

    def test_saved_as_sql_query(self):
        self.run(self.session, 'RETURN 1')
        DataCollector().finalise()
        self.assertEqual('RETURN 1', SQLQuery.objects.get(request=self.request).query)

    def test_ignored_query(self):
        SilkyConfig().SILKY_IGNORE_QUERIES = ['RETURN 1']
        try:
            self.run(self.session, 'RETURN 1')
            self.run(self.session, 'RETURN 2')
        finally:
            SilkyConfig().SILKY_IGNORE_QUERIES = []
        self.assertEqual(['RETURN 2'], [q['query'] for q in DataCollector().queries.values()])

    def test_error_is_recorded_and_raised(self):
        self.original.side_effect = ValueError('boom')
        with self.assertRaises(ValueError):
            self.run(self.session, 'RETURN 1')
        self.assertEqual(1, len(DataCollector().queries))

    def test_unrenderable_parameters_do_not_break_the_query(self):
        result = self.run(self.session, 'RETURN $a', 'not a dict')
        self.assertEqual('result', result)
        self.assertEqual('RETURN $a', list(DataCollector().queries.values())[0]['query'])

    def test_async(self):
        original = AsyncMock(return_value='async result')
        run = _wrap_async(original)
        result = asyncio.run(run(self.session, 'RETURN $a', a=1))
        self.assertEqual('async result', result)
        original.assert_awaited_once_with(self.session, 'RETURN $a', None, a=1)
        self.assertEqual('RETURN 1', list(DataCollector().queries.values())[0]['query'])


@unittest.skipIf(neo4j is None, 'neo4j driver is not installed')
class TestPatchNeo4j(TestCase):
    classes = (
        'Session', 'Transaction', 'ManagedTransaction',
        'AsyncSession', 'AsyncTransaction', 'AsyncManagedTransaction',
    )

    def test_all_run_methods_are_wrapped_once(self):
        patch_neo4j()
        wrapped = {name: getattr(neo4j, name).run for name in self.classes}
        for name, run in wrapped.items():
            self.assertTrue(hasattr(run, '_silk_original'), name)
            self.assertFalse(hasattr(run._silk_original, '_silk_original'), name)
        patch_neo4j()
        for name, run in wrapped.items():
            self.assertIs(run, getattr(neo4j, name).run, name)

    def test_async_classes_get_coroutine_wrappers(self):
        patch_neo4j()
        self.assertTrue(asyncio.iscoroutinefunction(neo4j.AsyncSession.run))
        self.assertFalse(asyncio.iscoroutinefunction(neo4j.Session.run))
