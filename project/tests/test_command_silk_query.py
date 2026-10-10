import argparse
import hashlib
import json
import re
import shlex
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from silk.management.commands.silk_query import EXAMPLES, SUBCOMMANDS, Command
from silk.models import Profile, Request, Response, SQLQuery
from silk.reporting.data import iso
from silk.reporting.guide import GUIDE

from .factories import RequestMinFactory, recorded_request
from .test_reporting_data import ReportTestCase
from .test_reporting_render import assert_text_is_in_json

ROOT = Path(__file__).resolve().parents[2]
PREFIX = 'python manage.py silk_query'


def run(*args):
    """Runs the command and returns what it wrote to standard output; nothing may go to standard error."""
    stdout, stderr = StringIO(), StringIO()
    call_command('silk_query', *[str(arg) for arg in args], stdout=stdout, stderr=stderr)
    assert stderr.getvalue() == '', stderr.getvalue()
    return stdout.getvalue()


def parsers():
    parser = Command().create_parser('manage.py', 'silk_query')
    subparsers = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    return parser, subparsers.choices


def documented_commands(text):
    """The silk_query command lines found in a document, without the prefix."""
    return [
        match.group(1).strip()
        for match in re.finditer(r'^\s*(?:\$ )?%s\b(.*)$' % re.escape(PREFIX), text, re.M)
    ]


class CommandTestCase(ReportTestCase):

    def invocations(self):
        """One invocation of every subcommand, and one of every option of each."""
        request = self.slow.pk[:8]
        return [
            ['guide'],
            ['summary'],
            ['summary', '--since', '7d', '--max-requests', '1', '--limit', '1'],
            ['requests'],
            ['requests', '--sort', 'time', '--asc', '--limit', '1', '--offset', '1'],
            ['requests', '--path', '/api/boards/', '--path-contains', 'api', '--view', 'boards:list', '--method',
             'get', '--status', '2xx', '--since', '1d', '--until', '0s', '--min-time', '1', '--min-queries', '1',
             '--min-query-time', '1', '--sort', 'query-time'],
            ['request', request],
            ['request', request, '--headers', '--bodies', '--cprofile', '--curl', '--base-url', 'http://test',
             '--max-chars', '0', '--limit', '2', '--offset', '1'],
            ['queries', request],
            ['queries', request, '--sort', 'time', '--shape', self.repeated_shape, '--kind', 'cypher',
             '--min-time', '1', '--limit', '2', '--offset', '1', '--max-chars', '10'],
            ['query', self.sql.pk],
            ['query', self.repeated[0].pk, '--full-traceback', '--max-chars', '10'],
            ['shapes'],
            ['shapes', '--view', 'training:openings', '--max-requests', '1', '--limit', '1', '--max-chars', '10'],
            ['profiles'],
            ['profiles', '--name', 'load', '--function', 'do_', '--request', request, '--limit', '1'],
            ['profile', self.profile.pk],
            ['profile', self.profile.pk, '--limit', '1', '--offset', '1', '--max-chars', '10'],
        ]


class TestSubcommands(CommandTestCase):

    def test_every_subcommand_runs(self):
        invocations = self.invocations()
        self.assertEqual({invocation[0] for invocation in invocations}, set(SUBCOMMANDS))
        for invocation in invocations:
            with self.subTest(invocation=invocation):
                text = run(*invocation)
                self.assertTrue(text.strip())
                self.assertTrue(text.endswith('\n'))
                self.assertNotIn('\x1b', text)
                if invocation[0] != 'guide':
                    lines = text.rstrip('\n').split('\n')
                    self.assertIn('NEXT', lines)
                    for line in lines[lines.index('NEXT') + 1:]:
                        self.assertTrue(line.startswith('  %s ' % PREFIX), line)

    def test_every_subcommand_prints_json(self):
        for invocation in self.invocations():
            with self.subTest(invocation=invocation):
                document = json.loads(run(*invocation, '--format', 'json'))
                self.assertIsInstance(document, dict)
                self.assertEqual(document['report'], invocation[0])
                self.assertEqual(list(document)[-2:], ['truncated', 'next'])
                if invocation[0] != 'guide':
                    assert_text_is_in_json(self, run(*invocation), document)

    def test_follow_up_commands_run(self):
        parser, _ = parsers()
        for invocation in self.invocations():
            for command in json.loads(run(*invocation, '--format', 'json'))['next']:
                with self.subTest(command=command):
                    self.assertTrue(command.startswith(PREFIX + ' '))
                    arguments = shlex.split(command[len(PREFIX):])
                    parser.parse_args(arguments)
                    self.assertTrue(run(*arguments).strip())

    def test_requests(self):
        lines = run('requests').split('\n')
        self.assertEqual(lines[0], 'requests')
        self.assertEqual([line.split()[0] for line in lines[5:7]], [self.fast.pk[:8], self.slow.pk[:8]])
        self.assertIn('  {} request {}'.format(PREFIX, self.fast.pk[:8]), lines)

    def test_request_by_prefix_shows_the_full_id(self):
        self.assertEqual(run('request', self.slow.pk[:8]).split('\n')[0], 'request %s' % self.slow.pk)
        document = json.loads(run('request', self.slow.pk[:8], '--format', 'json'))
        self.assertEqual(document['id'], self.slow.pk)
        self.assertEqual(len(document['shapes']), 4)
        self.assertEqual(document['hints'][0]['hint'], 'repeated_shape')

    def test_request_does_not_print_its_queries(self):
        text = run('request', self.slow.pk)
        self.assertIn('queries: 10 (3 sql, 7 cypher, 0 other)', text)
        self.assertEqual(text.count('elementId(o)='), 1)

    def test_filters(self):
        document = json.loads(run('requests', '--view', 'training:openings', '--min-time', '500', '--format', 'json'))
        self.assertEqual([row['id'] for row in document['requests']], [self.slow.pk])
        self.assertEqual(document['filters'], {'view': 'training:openings', 'min_time': 500.0})
        document = json.loads(run('requests', '--view', 'training:openings', '--min-time', '5000', '--format', 'json'))
        self.assertEqual(document['requests'], [])

    def test_sort(self):
        slowest = json.loads(run('requests', '--sort', 'time', '--format', 'json'))['requests']
        fastest = json.loads(run('requests', '--sort', 'time', '--asc', '--format', 'json'))['requests']
        self.assertEqual([row['id'] for row in slowest], [self.slow.pk, self.fast.pk])
        self.assertEqual([row['id'] for row in fastest], [self.fast.pk, self.slow.pk])

    def test_queries_of_one_shape(self):
        document = json.loads(run('queries', self.slow.pk[:8], '--shape', self.repeated_shape, '--format', 'json'))
        self.assertEqual({row['shape'] for row in document['queries']}, {self.repeated_shape})
        self.assertEqual(len(document['queries']), 6)

    def test_max_chars(self):
        text = run('queries', self.slow.pk, '--max-chars', '10')
        self.assertIn('[queries.text: cut to 10 chars in 10 row(s)', text)
        self.assertNotIn('[queries.text', run('queries', self.slow.pk, '--max-chars', '0'))


class TestHelp(TestCase):

    def test_top_level_help(self):
        parser, subparsers = parsers()
        text = parser.format_help()
        self.assertEqual(list(subparsers), list(SUBCOMMANDS))
        for name, purpose in SUBCOMMANDS.items():
            self.assertRegex(text, r'\n    %s +\S' % name)
            self.assertIn(' '.join(purpose.split()[:4]), ' '.join(text.split()))
        self.assertIn('Run `python manage.py silk_query guide` first', text)
        self.assertIn('Read-only', text)
        self.assertIn('\n  python manage.py silk_query guide\n', text)

    def test_subcommand_help(self):
        _, subparsers = parsers()
        for name, subparser in subparsers.items():
            with self.subTest(subcommand=name):
                text = subparser.format_help()
                examples = [line.strip() for line in text.split('\n') if line.strip().startswith(PREFIX)]
                self.assertTrue(examples)
                self.assertEqual(examples, ['{} {}'.format(PREFIX, example) for example in EXAMPLES[name]])
                for example in EXAMPLES[name]:
                    self.assertEqual(example.split()[0], name)
                self.assertIn('--format {text,json}', text)

    def test_every_argument_is_described(self):
        _, subparsers = parsers()
        for name, subparser in subparsers.items():
            for action in subparser._actions:
                if isinstance(action, argparse._HelpAction):
                    continue
                with self.subTest(subcommand=name, argument=action.dest):
                    self.assertTrue(action.help)
                    if action.option_strings:
                        self.assertIn('Default: ', action.help)
                        if action.default not in (None, False):
                            self.assertIn('Default: %s.' % action.default, action.help)
                    for choice in action.choices or ():
                        self.assertIn(choice, action.help)

    def test_requests_help(self):
        _, subparsers = parsers()
        text = ' '.join(subparsers['requests'].format_help().split())
        for option in ('--path', '--path-contains', '--view', '--method', '--status', '--since', '--until',
                       '--min-time', '--min-queries', '--min-query-time', '--sort', '--asc', '--limit', '--offset'):
            self.assertIn(option + ' ', text)
        self.assertIn('--sort {start,time,queries,query-time,path}', text)
        self.assertIn('Default: start.', text)
        self.assertIn('15m, 2h, 7d', text)
        self.assertIn('1xx, 2xx, 3xx, 4xx, 5xx', text)

    def test_examples_are_accepted(self):
        parser, _ = parsers()
        for examples in EXAMPLES.values():
            for example in examples:
                with self.subTest(example=example):
                    parser.parse_args(shlex.split(example))


class TestErrors(CommandTestCase):

    def fails(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with self.assertRaises(CommandError) as raised:
            call_command('silk_query', *args, stdout=stdout, stderr=stderr)
        self.assertEqual(stdout.getvalue(), '')
        return str(raised.exception)

    def test_missing_subcommand(self):
        message = self.fails()
        self.assertIn('A subcommand is required', message)
        for name in SUBCOMMANDS:
            self.assertIn(name, message)

    def test_unknown_subcommand(self):
        message = self.fails('request_list')
        for name in SUBCOMMANDS:
            self.assertIn(name, message)

    def test_unknown_request(self):
        for args in (['request', 'ffffffff'], ['queries', 'ffffffff'], ['profiles', '--request', 'ffffffff']):
            with self.subTest(args=args):
                message = self.fails(*args)
                self.assertIn("No request matches id 'ffffffff'", message)
                self.assertIn('python manage.py silk_query requests', message)

    def test_ambiguous_request(self):
        first = RequestMinFactory(id='abcd0000-78f1-40c5-b34d-be125c4af704')
        second = RequestMinFactory(id='abcd1111-78f1-40c5-b34d-be125c4af704')
        message = self.fails('request', 'abcd')
        self.assertIn("'abcd' is ambiguous", message)
        self.assertIn(first.pk, message)
        self.assertIn(second.pk, message)

    def test_unknown_query_and_profile(self):
        self.assertIn('No query has id 987654', self.fails('query', '987654'))
        self.assertIn('python manage.py silk_query queries <request-id>', self.fails('query', '987654'))
        self.assertIn('integer', self.fails('query', self.slow.pk[:8]))
        self.assertIn('No profile has id 987654', self.fails('profile', '987654'))
        self.assertIn('python manage.py silk_query profiles', self.fails('profile', '987654'))

    def test_invalid_sort(self):
        message = self.fails('requests', '--sort', 'size')
        self.assertIn("'size'", message)
        for name in ('start', 'time', 'queries', 'query-time', 'path'):
            self.assertIn(name, message)
        message = self.fails('queries', self.slow.pk, '--sort', 'path')
        self.assertIn('start', message)
        self.assertIn('time', message)

    def test_invalid_since_and_until(self):
        for option in ('--since', '--until'):
            for subcommand in ('requests', 'summary', 'shapes'):
                with self.subTest(option=option, subcommand=subcommand):
                    message = self.fails(subcommand, option, 'yesterday')
                    self.assertIn("Invalid %s value 'yesterday'" % option, message)
                    self.assertIn('15m, 2h or 7d', message)
                    self.assertIn('ISO 8601 timestamp such as 2026-10-10T08:00:00Z', message)

    def test_invalid_status(self):
        message = self.fails('requests', '--status', 'error')
        self.assertIn("Invalid --status value 'error'", message)
        self.assertIn('404', message)
        self.assertIn('1xx, 2xx, 3xx, 4xx or 5xx', message)

    def test_invalid_format(self):
        message = self.fails('requests', '--format', 'yaml')
        self.assertIn("'yaml'", message)
        self.assertIn('text', message)
        self.assertIn('json', message)

    def test_invalid_numbers(self):
        self.assertIn('whole number of 0 or more', self.fails('requests', '--limit', '-1'))
        self.assertIn('whole number of 0 or more', self.fails('requests', '--offset', 'two'))
        self.assertIn('milliseconds', self.fails('requests', '--min-time', 'fast'))
        self.assertIn('sql', self.fails('queries', self.slow.pk, '--kind', 'graphql'))


class TestEmptyResults(TestCase):

    def test_empty_database(self):
        for args in (['summary'], ['requests'], ['shapes'], ['profiles']):
            with self.subTest(args=args):
                text = run(*args)
                self.assertIn('Nothing matched', text)
                self.assertIn('\nrecorded_requests: 0\n', text)
                self.assertIn('\nlatest_request_start: -\n', text)
                self.assertIn('\nNEXT\n  %s ' % PREFIX, text)
                document = json.loads(run(*args, '--format', 'json'))
                self.assertEqual(document['empty']['recorded_requests'], 0)
                self.assertIsNone(document['empty']['latest_request_start'])

    def test_filters_exclude_every_row(self):
        old = recorded_request(start_time=timezone.now() - timedelta(hours=3))
        latest = recorded_request(start_time=timezone.now() - timedelta(hours=1))
        for subcommand in ('requests', 'summary', 'shapes'):
            with self.subTest(subcommand=subcommand):
                text = run(subcommand, '--since', '5m')
                self.assertIn('Nothing matched: no recorded request passes these filters.', text)
                self.assertIn('\nrecorded_requests: 2\n', text)
                self.assertIn('\nlatest_request_start: %s\n' % iso(latest.start_time), text)
                self.assertIn('filters: --since 5m', text)
        self.assertNotIn('Nothing matched', run('requests', '--since', '2h'))
        self.assertIn(old.pk[:8], run('requests', '--since', '4h'))

    def test_request_without_matching_queries(self):
        request = recorded_request()
        text = run('queries', request.pk)
        self.assertIn('Nothing matched: this request recorded no queries.', text)
        self.assertIn('\nrecorded_requests: 1\n', text)

    def test_no_profiles(self):
        recorded_request()
        text = run('profiles')
        self.assertIn('no profile block has been recorded', text)
        self.assertIn('silk_profile decorator or context manager', text)
        self.assertIn('\nrecorded_requests: 1\n', text)


class TestReadOnly(CommandTestCase):

    def snapshot(self):
        tables = (Request, Response, SQLQuery, Profile, Profile.queries.through)
        counts = [model.objects.count() for model in tables]
        digest = hashlib.sha256()
        for model in tables:
            for row in model.objects.order_by('pk').values():
                digest.update(json.dumps(row, sort_keys=True, default=str).encode())
        return counts, digest.hexdigest()

    def test_recorded_data_is_unchanged(self):
        before = self.snapshot()
        self.assertEqual(before[0], [2, 2, 11, 1, 3])
        for invocation in self.invocations():
            run(*invocation)
            run(*invocation, '--format', 'json')
        self.assertEqual(self.snapshot(), before)

    def test_only_selects_are_issued(self):
        with CaptureQueriesContext(connection) as captured:
            for invocation in self.invocations():
                run(*invocation)
        self.assertTrue(captured.captured_queries)
        for query in captured.captured_queries:
            # PostgreSQL streams the queries of a request through a cursor declared for the SELECT.
            self.assertRegex(query['sql'], r'^\s*(SELECT|DECLARE \S+ [A-Z ]+ FOR SELECT) ')


class TestGuide(TestCase):

    def test_names_every_subcommand_registered_in_the_parser(self):
        _, subparsers = parsers()
        text = run('guide')
        for name in subparsers:
            self.assertRegex(text, r'(?m)^ +(\d\. )?%s\b' % name)

    def test_states_the_order(self):
        text = run('guide')
        order = [text.index('\n  %d. %s' % (step, name)) for step, name in enumerate(
            ('summary', 'requests', 'request <id>', 'queries <id>', 'query <query-id>'), start=1,
        )]
        self.assertEqual(order, sorted(order))

    def test_explains_terms_and_limits(self):
        text = run('guide')
        for term in ('kind ', 'shape ', 'call site ', 'hint '):
            self.assertIn('\n  ' + term, text)
        self.assertIn('SILKY_PYTHON_PROFILER', text)
        self.assertIn('Cypher queries carry no EXPLAIN output', text)
        self.assertIn('--headers', text)
        self.assertIn('SILKY_SENSITIVE_KEYS', text)

    def test_runs_without_data(self):
        self.assertEqual(Request.objects.count(), 0)
        self.assertEqual(run('guide'), GUIDE)
        with self.assertNumQueries(0):
            run('guide')

    def test_json(self):
        document = json.loads(run('guide', '--format', 'json'))
        self.assertEqual(document['text'] + '\n', GUIDE)

    def test_has_no_long_lines(self):
        self.assertLessEqual(max(len(line) for line in GUIDE.split('\n')), 100)


class TestDocumentation(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.page = (ROOT / 'docs' / 'silk_query.rst').read_text()
        cls.readme = (ROOT / 'README.md').read_text()

    def test_documented_commands_are_accepted(self):
        parser, _ = parsers()
        commands = documented_commands(self.page)
        self.assertGreater(len(commands), 20)
        for command in commands:
            with self.subTest(command=command):
                parser.parse_args(shlex.split(command))

    def test_every_subcommand_has_an_example(self):
        documented = {command.split()[0] for command in documented_commands(self.page)}
        self.assertEqual(documented, set(SUBCOMMANDS))
        for name in SUBCOMMANDS:
            self.assertIn('\n    python manage.py silk_query %s' % name, self.page)

    def test_page_is_in_the_toctree(self):
        self.assertIn('\n   silk_query\n', (ROOT / 'docs' / 'index.rst').read_text())

    def test_readme_examples_are_the_documented_ones(self):
        documented = set(documented_commands(self.page))
        commands = documented_commands(self.readme)
        self.assertGreaterEqual(len(commands), 5)
        for command in commands:
            with self.subTest(command=command):
                self.assertIn(command, documented)

    def test_readme_section(self):
        self.assertIn('\n## Querying recorded data from the command line\n', self.readme)
        self.assertIn(
            '* [Querying recorded data from the command line](#querying-recorded-data-from-the-command-line)',
            self.readme,
        )

    def test_changelog(self):
        changelog = (ROOT / 'CHANGELOG.md').read_text()
        fork = changelog.split('**Fork (django-silk-neo4j):**')[1].split('\n## ')[0]
        self.assertIn('`silk_query`', fork)
