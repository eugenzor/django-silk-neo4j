"""The text printed by ``silk_query guide``."""
from silk.reporting.data import COMMAND

GUIDE = """\
silk_query reads what silk recorded: requests, their SQL and Cypher queries, and profile blocks.
It only reads. Run it as: {command} <subcommand> [options]

OUTPUT
  Plain text by default; --format json prints one JSON document with the same data.
  Durations are in milliseconds (names end in _ms). Timestamps are ISO 8601 in UTC.
  A missing value is printed as "-", never as 0.
  Output is bounded. When rows or text are cut, a line in [brackets] says how much was shown
  and which option shows the rest (--limit, --offset, --max-chars, --max-requests).
  Each output ends with NEXT: follow-up commands with the ids already filled in.
  Lists show the first 8 characters of a request id; any unique prefix is accepted as an id.

WORKFLOW FOR A SLOW REQUEST
  1. summary                         which views take the time: count, median, p95, max, queries
  2. requests --sort time            the slowest requests; narrow with --view, --path, --since, ...
  3. request <id>                    one request: time in and outside queries, query shapes, hints
  4. queries <id> --shape <shape>    every execution of one shape, in order, with its call site
  5. query <query-id>                one query: full text, EXPLAIN output, traceback
  Step 3 is often enough: its HINTS name the shape, the count and the source line.

OTHER SUBCOMMANDS
  shapes                the query shapes that cost the most across many requests
  profiles              blocks recorded with the silk_profile decorator or context manager
  profile <id>          one such block and the shapes of its queries
  guide                 this text

TERMS
  kind       sql, cypher or other. silk stores every query in one table with no marker, so the
             kind is told from the leading keyword. A heuristic.
  shape      the query with its literals replaced by "?", and lists of literals by "(?+)".
             Queries that differ only in their values share a shape. The shape id (8 hex
             characters) depends only on that text, so it is the same in every request.
  call site  the innermost frame of the query's traceback that is application code: not an
             installed package, the standard library or silk. "unknown" when there is none.
  hint       a pattern worth a look, with the numbers behind it:
             repeated_shape        one shape ran 5 times or more: likely N+1 pattern
             identical_queries     the same query text ran more than once
             dominant_query        one query took at least half of the request
             time_outside_queries  at least half of the request, and at least 100 ms, was
                                   not spent in queries

LIMITS OF THE DATA
  - Time outside queries (Python code, templates, serialisation, calls to other services) is
    not attributed to anything unless SILKY_PYTHON_PROFILER was on when the request was
    recorded. If it was, `request <id> --cprofile` prints the cProfile output.
  - Cypher queries carry no EXPLAIN output. SQL queries carry the plan silk got when it
    recorded them; nothing is run again.
  - The recorded time of a Cypher query covers sending it and receiving the first response,
    not consuming every record.
  - SQL is recorded with its parameters interpolated and unquoted, so shapes are a heuristic:
    a value containing " AND " or ")" can give the same query two shapes. Totals stay correct.
  - Only what silk recorded is here: see SILKY_INTERCEPT_PERCENT, SILKY_IGNORE_PATHS and
    SILKY_MAX_RECORDED_REQUESTS.
  - summary and shapes analyse the most recent matching requests, up to --max-requests, and
    say how many they covered.

HEADERS AND BODIES
  They are printed only with `request <id> --headers` and `--bodies`. silk masks the values
  of SILKY_SENSITIVE_KEYS, the Authorization header and, with SILKY_HIDE_COOKIES, cookies
  when it records a request. Everything else is printed as recorded, so treat it as data
  that may be sensitive. Like any management command, this one is not subject to
  SILKY_AUTHENTICATION.

Each subcommand has its own help: {command} <subcommand> --help
""".format(command=COMMAND)


def guide():
    """The `guide` report."""
    return {'report': 'guide', 'text': GUIDE.rstrip('\n'), 'truncated': [], 'next': ['%s summary' % COMMAND]}
