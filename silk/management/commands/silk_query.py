import argparse
import textwrap

from django.core.management.base import (
    BaseCommand,
    CommandError,
    DjangoHelpFormatter,
)

from silk.reporting import analysis, data, render
from silk.reporting.guide import guide

COMMAND = data.COMMAND

# Subcommand -> one-line purpose, in the order they are listed in the help.
SUBCOMMANDS = {
    'guide': 'How to investigate a slow request, the terms used in the output and the limits of the data.',
    'summary': 'Totals and per-view statistics: count, median, p95 and maximum time, queries.',
    'requests': 'List recorded requests, filtered and ordered.',
    'request': 'One request: timing split, query shapes, hints, profiles; optionally headers, bodies, cProfile, curl.',
    'queries': 'The query executions of one request, as a timeline or by duration.',
    'query': 'One query execution: full text, EXPLAIN output, call site and traceback.',
    'shapes': 'Query shapes aggregated across requests, costliest first.',
    'profiles': 'List blocks recorded with silk_profile.',
    'profile': 'One silk_profile block and the shapes of its queries.',
}

DESCRIPTION = """\
Reads the requests, queries and profiles that silk recorded, as plain text or JSON.
Read-only: nothing is created, changed or deleted.

Run `%s guide` first. It gives the order in which to use the
subcommands and explains the terms used in their output.""" % COMMAND

EXAMPLES = {
    None: (
        'guide',
        'summary --since 2h',
        'requests --sort time --limit 10',
        'request 96de4a15',
    ),
    'guide': (
        'guide',
    ),
    'summary': (
        'summary',
        'summary --since 24h --path-contains /api/',
        'summary --view training:openings --format json',
    ),
    'requests': (
        'requests',
        'requests --sort time --limit 10',
        'requests --view training:openings --min-time 500',
        'requests --status 5xx --since 2h',
        'requests --sort queries --min-queries 50 --offset 20',
    ),
    'request': (
        'request 96de4a15',
        'request 96de4a15 --cprofile',
        'request 96de4a15 --headers --bodies --max-chars 0',
        'request 96de4a15 --curl --base-url http://localhost:8000',
        'request 96de4a15 --format json',
    ),
    'queries': (
        'queries 96de4a15',
        'queries 96de4a15 --shape 9879c756',
        'queries 96de4a15 --sort time --limit 5',
        'queries 96de4a15 --kind cypher --min-time 50',
    ),
    'query': (
        'query 91',
        'query 91 --full-traceback',
        'query 91 --max-chars 0 --format json',
    ),
    'shapes': (
        'shapes',
        'shapes --view training:openings',
        'shapes --since 1h --max-requests 200 --limit 10',
    ),
    'profiles': (
        'profiles',
        'profiles --name openings',
        'profiles --request 96de4a15',
    ),
    'profile': (
        'profile 3',
        'profile 3 --format json',
    ),
}


def _epilog(name):
    return 'examples:\n' + '\n'.join('  {} {}'.format(COMMAND, example) for example in EXAMPLES[name])


def _count(value):
    try:
        number = int(value)
    except ValueError:
        number = -1
    if number < 0:
        raise argparse.ArgumentTypeError('%r is not a whole number of 0 or more' % value)
    return number


def _milliseconds(value):
    try:
        number = float(value)
    except ValueError:
        number = -1
    if number < 0:
        raise argparse.ArgumentTypeError('%r is not a number of milliseconds of 0 or more' % value)
    return number


class HelpFormatter(argparse.RawDescriptionHelpFormatter, DjangoHelpFormatter):
    """Keeps the line breaks of the description and of the examples."""


def _common():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        '--format', choices=('text', 'json'), default='text',
        help='Output format: text or json. json prints one JSON document with the same data. Default: text.',
    )
    return parser


def _paging(rows):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        '--limit', type=_count, default=data.LIST_LIMIT, metavar='N',
        help='Maximum number of %s to print; 0 prints all. Default: %d.' % (rows, data.LIST_LIMIT),
    )
    parser.add_argument(
        '--offset', type=_count, default=0, metavar='N',
        help='Number of %s to skip, to page through a long list. Default: 0.' % rows,
    )
    return parser


def _max_chars(what):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        '--max-chars', type=_count, default=None, metavar='N',
        help='Maximum characters printed of %s; 0 prints it whole. Default: %d in a table row, %d elsewhere.'
             % (what, data.ROW_CHARS, data.DETAIL_CHARS),
    )
    return parser


def _filters():
    parser = argparse.ArgumentParser(add_help=False)
    group = parser.add_argument_group('request filters', 'A request must pass every filter given.')
    group.add_argument('--path', help='Request path, exact match, e.g. /api/training/openings/. Default: any.')
    group.add_argument(
        '--path-contains', metavar='TEXT', help='Text the request path must contain, e.g. /api/. Default: any.',
    )
    group.add_argument(
        '--view', help='View name as Django resolves it, exact match, e.g. training:openings. Default: any.',
    )
    group.add_argument('--method', help='HTTP method, e.g. GET or POST; case does not matter. Default: any.')
    group.add_argument(
        '--status', help='Response status code such as 404, or a class: 1xx, 2xx, 3xx, 4xx, 5xx. Default: any.',
    )
    group.add_argument(
        '--since', metavar='WHEN',
        help='Only requests that started at or after WHEN: a duration ago such as 30s, 15m, 2h, 7d, 1w, '
             'or an ISO 8601 timestamp such as 2026-10-10T08:00:00Z (UTC when no offset is given). '
             'Default: no lower bound.',
    )
    group.add_argument(
        '--until', metavar='WHEN',
        help='Only requests that started at or before WHEN, in the same forms as --since. Default: no upper bound.',
    )
    group.add_argument(
        '--min-time', type=_milliseconds, metavar='MS',
        help='Minimum overall time of the request, in milliseconds. Default: none.',
    )
    group.add_argument(
        '--min-queries', type=_count, metavar='N', help='Minimum number of queries of the request. Default: none.',
    )
    group.add_argument(
        '--min-query-time', type=_milliseconds, metavar='MS',
        help='Minimum time the request spent in queries, in milliseconds. Default: none.',
    )
    return parser


FILTERS = (
    'path', 'path_contains', 'view', 'method', 'status', 'since', 'until', 'min_time', 'min_queries',
    'min_query_time',
)


class Command(BaseCommand):
    help = DESCRIPTION
    # It only reads silk's tables, and a warning of the project's system checks
    # would be printed ahead of every report.
    requires_system_checks = []

    def create_parser(self, prog_name, subcommand, **kwargs):
        kwargs.setdefault('formatter_class', HelpFormatter)
        parser = super().create_parser(prog_name, subcommand, **kwargs)
        parser.epilog = _epilog(None)
        return parser

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest='subcommand', title='subcommands')

        def add(name, *parents, description=None):
            return subparsers.add_parser(
                name, help=SUBCOMMANDS[name], description=textwrap.fill(description or SUBCOMMANDS[name], 79),
                epilog=_epilog(name),
                formatter_class=argparse.RawDescriptionHelpFormatter, parents=[*parents, _common()],
            )

        add('guide')

        summary = add(
            'summary', _filters(), _paging('views'),
            description='Totals for the matching requests and one row per view, ordered by the total time of the '
                        'view. Statistics cover the most recent matching requests, up to --max-requests.',
        )
        summary.add_argument(
            '--max-requests', type=_count, default=data.SUMMARY_REQUESTS, metavar='N',
            help='Number of most recent matching requests to analyse; 0 analyses all. Default: %d.'
                 % data.SUMMARY_REQUESTS,
        )

        requests = add(
            'requests', _filters(), _paging('requests'),
            description='One row per request: shortened id, start time, method, status, overall time, time in '
                        'queries, number of queries by kind, view name and path.',
        )
        requests.add_argument(
            '--sort', choices=tuple(data.REQUEST_SORTS), default='start',
            help='Order by: start (start time), time (overall time), queries (number of queries), '
                 'query-time (time in queries) or path. Highest or most recent first. Default: start.',
        )
        requests.add_argument(
            '--asc', action='store_true', help='Ascending order: lowest or oldest first. Default: descending.',
        )

        request = add(
            'request', _paging('query shapes'), _max_chars('a shape, a body, the cProfile output or the curl command'),
            description='One request: its timing split, its query shapes ordered by total time (not its '
                        'individual queries), hints, and its silk_profile blocks. Headers, bodies, cProfile '
                        'output and a curl command are printed only with their option; their sizes are always '
                        'listed.',
        )
        request.add_argument('id', help='Request id, or any prefix of it that matches one request.')
        request.add_argument(
            '--headers', action='store_true', help='Print the request and response headers. Default: not printed.',
        )
        request.add_argument(
            '--bodies', action='store_true',
            help='Print the request and response bodies as silk recorded them. A response body that is not text '
                 'is reported by content type and size. Default: not printed.',
        )
        request.add_argument(
            '--cprofile', action='store_true',
            help='Print the cProfile output, recorded when SILKY_PYTHON_PROFILER is on. Default: not printed.',
        )
        request.add_argument(
            '--curl', action='store_true', help='Print a curl command that repeats the request. Default: not printed.',
        )
        request.add_argument(
            '--base-url', metavar='URL',
            help='Scheme and host for --curl, e.g. https://staging.example.com. '
                 'Default: http:// and the recorded Host header.',
        )

        queries = add(
            'queries', _paging('queries'), _max_chars('the query text'),
            description='The query executions of one request: id, offset from the start of the request, duration, '
                        'kind, shape id, call site and text.',
        )
        queries.add_argument('request_id', help='Request id, or any prefix of it that matches one request.')
        queries.add_argument(
            '--sort', choices=data.QUERY_SORTS, default='start',
            help='Order by: start (the order the queries started in) or time (longest first). Default: start.',
        )
        queries.add_argument(
            '--shape', metavar='SHAPE_ID',
            help='Only executions of this shape; the id is in the output of `request` and `shapes`. Default: any.',
        )
        queries.add_argument(
            '--kind', choices=analysis.KINDS, help='Only queries of this kind: sql, cypher or other. Default: any.',
        )
        queries.add_argument(
            '--min-time', type=_milliseconds, metavar='MS',
            help='Only queries that took at least this many milliseconds. Default: none.',
        )

        query = add(
            'query', _max_chars('the query text or the plan'),
            description='One query execution: the request it belongs to, kind, shape id, offset, duration, the '
                        'formatted text, tables and joins (SQL), EXPLAIN output when recorded, call site and '
                        'traceback.',
        )
        query.add_argument('id', help='Query id, an integer from the output of `queries` or `shapes`.')
        query.add_argument(
            '--full-traceback', action='store_true',
            help='Print every recorded frame, library frames included. Default: application frames only.',
        )

        shapes = add(
            'shapes', _filters(), _paging('shapes'), _max_chars('the shape text'),
            description='Query shapes found in the most recent matching requests, up to --max-requests: id, kind, '
                        'executions, requests it appears in, total, average and maximum time, the id and call '
                        'site of its slowest execution, and text. Ordered by total time.',
        )
        shapes.add_argument(
            '--max-requests', type=_count, default=data.SHAPES_REQUESTS, metavar='N',
            help='Number of most recent matching requests to analyse; 0 analyses all. Default: %d.'
                 % data.SHAPES_REQUESTS,
        )

        profiles = add(
            'profiles', _paging('profiles'),
            description='Blocks recorded with the silk_profile decorator or context manager, or with dynamic '
                        'profiling, most recent first: id, request, duration, queries, time in queries, line, '
                        'function, file and name.',
        )
        profiles.add_argument(
            '--name', metavar='TEXT', help='Text the profile name must contain; case does not matter. Default: any.',
        )
        profiles.add_argument(
            '--function', metavar='TEXT',
            help='Text the function name must contain; case does not matter. Default: any.',
        )
        profiles.add_argument(
            '--request', metavar='REQUEST_ID',
            help='Only profiles of this request: its id or any prefix that matches one request. Default: any.',
        )

        profile = add('profile', _paging('query shapes'), _max_chars('the shape text'))
        profile.add_argument('id', help='Profile id, an integer from the output of `profiles` or `request`.')

    def handle(self, *args, **options):
        subcommand = options.get('subcommand')
        if not subcommand:
            raise CommandError(
                'A subcommand is required. Choose from: {}. Start with `{} guide`.'.format(', '.join(SUBCOMMANDS), COMMAND)
            )
        try:
            report = getattr(self, 'report_' + subcommand)(options)
        except data.ReportError as error:
            raise CommandError(str(error))
        if options['format'] == 'json':
            self.stdout.write(render.render_json(report))
        else:
            self.stdout.write(render.render_text(report))

    def report_guide(self, options):
        return guide()

    def report_summary(self, options):
        return data.summary(
            {name: options[name] for name in FILTERS}, max_requests=options['max_requests'],
            limit=options['limit'], offset=options['offset'],
        )

    def report_requests(self, options):
        return data.request_list(
            {name: options[name] for name in FILTERS}, sort=options['sort'], ascending=options['asc'],
            limit=options['limit'], offset=options['offset'],
        )

    def report_request(self, options):
        return data.request_detail(
            options['id'], limit=options['limit'], offset=options['offset'], max_chars=options['max_chars'],
            headers=options['headers'], bodies=options['bodies'], cprofile=options['cprofile'],
            curl=options['curl'], base_url=options['base_url'],
        )

    def report_queries(self, options):
        return data.request_queries(
            options['request_id'], sort=options['sort'], shape=options['shape'], kind=options['kind'],
            min_time=options['min_time'], limit=options['limit'], offset=options['offset'],
            max_chars=options['max_chars'],
        )

    def report_query(self, options):
        return data.query_detail(
            options['id'], full_traceback=options['full_traceback'], max_chars=options['max_chars'],
        )

    def report_shapes(self, options):
        return data.shape_list(
            {name: options[name] for name in FILTERS}, max_requests=options['max_requests'],
            limit=options['limit'], offset=options['offset'], max_chars=options['max_chars'],
        )

    def report_profiles(self, options):
        return data.profile_list(
            name=options['name'], function=options['function'], request=options['request'],
            limit=options['limit'], offset=options['offset'],
        )

    def report_profile(self, options):
        return data.profile_detail(
            options['id'], limit=options['limit'], offset=options['offset'], max_chars=options['max_chars'],
        )
