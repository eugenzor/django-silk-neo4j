Querying recorded data from the command line
============================================

``silk_query`` is a management command that reads what Silk recorded (requests, their SQL and Cypher queries, and ``silk_profile`` blocks) and prints it as plain text or JSON. It exists so that a script or an LLM agent can investigate a slow request from a shell, without the web UI.

.. code-block:: bash

    python manage.py silk_query guide

The command only reads: no subcommand creates, changes or deletes recorded data. It needs no setting and no migration.

Like any management command it runs with shell access to the project, so ``SILKY_AUTHENTICATION`` and ``SILKY_AUTHORISATION`` do not apply to it. This is the same level of trust as ``manage.py shell``.

Workflow
--------

To find out why a view is slow, go from the overview to the query:

1. ``summary`` shows which views take the time.
2. ``requests --sort time`` lists the slowest requests. Narrow the list with ``--view``, ``--path``, ``--since`` and the other filters.
3. ``request`` shows one request: time spent in and outside queries, its query shapes, and hints. This is often enough, because the hints name the shape, the number of executions and the source line.
4. ``queries`` with ``--shape`` lists every execution of one shape, in order, with its call site.
5. ``query`` shows one query in full, with its ``EXPLAIN`` output and traceback.

Every output ends with a ``NEXT`` section holding the follow-up commands with the ids filled in, so each step gives the command for the following one.

Subcommands
-----------

``--help`` on the command lists the subcommands, and ``--help`` on a subcommand describes every option with its default and gives examples.

guide
~~~~~

Prints the workflow above, the meaning of the terms used in the output and the limits of the recorded data. It needs no recorded data.

.. code-block:: bash

    python manage.py silk_query guide

summary
~~~~~~~

Totals for the matching requests and one row per view, ordered by the total time of the view.

.. code-block:: bash

    python manage.py silk_query summary --path-contains /api/ --limit 3

.. code-block:: text

    summary
    requests_matching: 80
    requests_analysed: 80
    filters: --path-contains /api/
    start_time_from: 2026-10-10T06:12:18.206Z
    start_time_to: 2026-10-10T08:24:27.248Z
    median_ms: 34.13
    p95_ms: 953.19
    max_ms: 2186.81

    VIEWS (16, by total time)
    requests  median_ms   p95_ms   max_ms  total_ms  avg_queries  avg_queries_ms  view
          10     925.30  1727.81  2186.81   8226.70        248.7          321.58  training:openings
           4      36.70   633.29   738.03    838.45          2.0          183.36  training:training.views.ProgressStatsView
           4     186.52   230.94   238.77    791.26         22.2           18.33  rest_register
    [views: 3 of 16 rows shown; --offset 3 shows the next rows, --limit N shows more (0 for all)]

    NEXT
      python manage.py silk_query requests --path-contains /api/ --view training:openings --sort time
      python manage.py silk_query shapes --path-contains /api/ --view training:openings

Statistics cover the most recent matching requests, 1000 by default. When more requests match, the output says how many were analysed, and ``--max-requests`` raises the bound.

requests
~~~~~~~~

One row per request. ``--sort`` takes ``start`` (the default), ``time``, ``queries``, ``query-time`` or ``path``, highest first; ``--asc`` reverses the order.

.. code-block:: bash

    python manage.py silk_query requests --sort time --limit 3

.. code-block:: text

    requests
    matching: 3213
    sort: time desc

    id        start_time                method  status  time_ms  queries_ms  queries  sql  cypher  view               path
    96de4a15  2026-10-10T07:39:58.981Z  GET        200  2186.81     1323.91      352    0     352  training:openings  /api/training/openings/
    46041533  2026-10-10T08:15:30.531Z  GET        200  1166.82      366.97      352    1     351  training:openings  /api/training/openings/
    827667f9  2026-10-10T07:52:40.384Z  GET        200  1009.54      349.98      351    0     351  training:openings  /api/training/openings/
    [requests: 3 of 3213 rows shown; --offset 3 shows the next rows, --limit N shows more (0 for all)]

    NEXT
      python manage.py silk_query request 96de4a15
      python manage.py silk_query summary

The same filters work for ``requests``, ``summary`` and ``shapes``, and a request must pass all of them:

.. code-block:: bash

    python manage.py silk_query requests --view training:openings --min-time 500
    python manage.py silk_query requests --status 5xx --since 2h
    python manage.py silk_query requests --path-contains /api/ --method POST --min-queries 50

================================ ==========================================================================
Option                           Keeps requests
================================ ==========================================================================
``--path``                       with exactly this path
``--path-contains``              whose path contains this text
``--view``                       of this view name
``--method``                     with this HTTP method
``--status``                     with this status code (``404``) or status class (``5xx``)
``--since``, ``--until``         that started in this window: a duration ago (``15m``, ``2h``, ``7d``) or an
                                 ISO 8601 timestamp
``--min-time``                   that took at least this many milliseconds
``--min-queries``                with at least this many queries
``--min-query-time``             that spent at least this many milliseconds in queries
================================ ==========================================================================

request
~~~~~~~

One request. It takes the full id or any prefix that matches one request. Queries are grouped into shapes, so a request with hundreds of queries stays short.

.. code-block:: bash

    python manage.py silk_query request 96de4a15

.. code-block:: text

    request 96de4a15-78f1-40c5-b34d-be125c4af704
    method: GET
    path: /api/training/openings/
    query_params: -
    view: training:openings
    status: 200
    start_time: 2026-10-10T07:39:58.981Z
    end_time: 2026-10-10T07:40:01.168Z
    time_ms: 2186.81
    queries_ms: 1323.91
    outside_queries_ms: 862.90
    queries: 352 (0 sql, 352 cypher, 0 other)

    SHAPES (7, by total time)
    shape     kind    count  total_ms  avg_ms  max_ms  slowest_query  call_site                                              text
    9879c756  cypher    246    458.85    1.87   77.02             89  /backend/training/serializers.py:41 in get_frequency   MATCH (opening) WHERE elementId(opening)=? WITH opening MATCH (opening)<-[r1:`RELATED_OPENING`]-(parent_r1:Opening) RETURN parent_r1
    ca299e7b  cypher      1    280.26  280.26  280.26             87  /backend/training/views.py:107 in get                  MATCH (opening:Opening) WHERE NOT EXISTS ((opening)<-[:`RELATED_OPENING`]-(:Opening)) RETURN opening ORDER BY opening.games DESC

    HINTS
    - [repeated_shape] shape 9879c756 ran 246 times (458.85 ms in total) most often from /backend/training/serializers.py:41 in get_frequency: likely N+1 pattern, fetch these rows in one query
    - [identical_queries] 78 identical query text(s) ran more than once: 234 executions, 366.78 ms in total; the most repeated ran 3 times (shape 9879c756, first as query 93)

    SECTIONS (printed only with their option)
    option      section           size
    --headers   request_headers   5 headers
    --headers   response_headers  5 headers
    --bodies    request_body      0 chars
    --bodies    response_body     51219 bytes
    --cprofile  cprofile          0 chars
    --curl      curl              -

    NEXT
      python manage.py silk_query queries 96de4a15-78f1-40c5-b34d-be125c4af704 --shape 9879c756
      python manage.py silk_query queries 96de4a15-78f1-40c5-b34d-be125c4af704 --sort time
      python manage.py silk_query request 96de4a15-78f1-40c5-b34d-be125c4af704 --headers --bodies

Headers, bodies, the cProfile output and a curl command that repeats the request are printed only when asked for:

.. code-block:: bash

    python manage.py silk_query request 96de4a15 --headers --bodies
    python manage.py silk_query request 96de4a15 --cprofile
    python manage.py silk_query request 96de4a15 --curl --base-url http://localhost:8000

Headers and bodies are printed as Silk recorded them. Silk masks the values of ``SILKY_SENSITIVE_KEYS``, the ``Authorization`` header and, with ``SILKY_HIDE_COOKIES``, cookies when it records a request. Anything else is printed as it is, so treat this output as data that may be sensitive. A response body that is not text is reported by content type and size.

The cProfile output exists only for requests recorded with ``SILKY_PYTHON_PROFILER = True``.

queries
~~~~~~~

The query executions of one request, in the order they started. ``--sort time`` lists the longest first, and ``--shape``, ``--kind`` and ``--min-time`` filter them.

.. code-block:: bash

    python manage.py silk_query queries 96de4a15 --shape 9879c756 --limit 3

.. code-block:: text

    queries of request 96de4a15-78f1-40c5-b34d-be125c4af704
    method: GET
    path: /api/training/openings/
    request_time_ms: 2186.81
    request_queries: 352 (0 sql, 352 cypher, 0 other)
    matching: 246
    total_ms: 458.85
    sort: start
    filters: --shape 9879c756

    id  offset_ms  time_ms  kind    shape     call_site                                             text
    89     642.61    77.02  cypher  9879c756  /backend/training/serializers.py:41 in get_frequency  MATCH (opening) WHERE elementId(opening)='4:440c51d5-3c9b-4f6c-9051-4abf10967294:1' WITH opening MATCH (opening)<-[r1:`RELATED_OPENING`]-(parent_r1:Opening) RETURN parent_r1
    93     979.17     2.42  cypher  9879c756  /backend/training/serializers.py:41 in get_frequency  MATCH (opening) WHERE elementId(opening)='4:440c51d5-3c9b-4f6c-9051-4abf10967294:25' WITH opening MATCH (opening)<-[r1:`RELATED_OPENING`]-(parent_r1:Opening) RETURN parent_r1
    94     983.76     2.38  cypher  9879c756  /backend/training/serializers.py:42 in get_frequency  MATCH (opening) WHERE elementId(opening)='4:440c51d5-3c9b-4f6c-9051-4abf10967294:25' WITH opening MATCH (opening)<-[r1:`RELATED_OPENING`]-(parent_r1:Opening) RETURN parent_r1
    [queries: 3 of 246 rows shown; --offset 3 shows the next rows, --limit N shows more (0 for all)]

    NEXT
      python manage.py silk_query query 89
      python manage.py silk_query request 96de4a15-78f1-40c5-b34d-be125c4af704

query
~~~~~

One query execution. SQL is formatted and comes with its tables, its number of joins and the ``EXPLAIN`` output Silk recorded. The traceback is reduced to application frames; ``--full-traceback`` prints every frame.

.. code-block:: bash

    python manage.py silk_query query 89

.. code-block:: text

    query 89
    request: 96de4a15-78f1-40c5-b34d-be125c4af704
    kind: cypher
    shape: 9879c756
    start_time: 2026-10-10T07:39:59.624Z
    offset_ms: 642.61
    time_ms: 77.02
    call_site: /backend/training/serializers.py:41 in get_frequency

    TEXT
    MATCH (opening) WHERE elementId(opening)='4:440c51d5-3c9b-4f6c-9051-4abf10967294:1' WITH opening MATCH (opening)<-[r1:`RELATED_OPENING`]-(parent_r1:Opening) RETURN parent_r1

    PLAN
    No plan is recorded for Cypher queries: silk runs EXPLAIN for SQL only.

    TRACEBACK (application frames, innermost first)
      /backend/training/serializers.py:41 in get_frequency
        if obj.parent.single():
      /backend/training/views.py:133 in get
        ).data,
    [traceback: 2 of 62 frames shown; application frames only, --full-traceback prints every frame]

    NEXT
      python manage.py silk_query queries 96de4a15-78f1-40c5-b34d-be125c4af704 --shape 9879c756
      python manage.py silk_query request 96de4a15-78f1-40c5-b34d-be125c4af704

shapes
~~~~~~

Query shapes aggregated over the most recent matching requests, 50 by default, costliest first. Use it to find the queries that cost the most overall and not only in one request.

.. code-block:: bash

    python manage.py silk_query shapes --view training:openings --limit 2

.. code-block:: text

    shapes
    requests_matching: 10
    requests_analysed: 10
    filters: --view training:openings
    queries_analysed: 2487

    SHAPES (8, by total time)
    shape     kind    count  requests  total_ms  avg_ms  max_ms  slowest_query  call_site                                                      text
    9879c756  cypher   1734         8   1718.65    0.99   77.02             89  /backend/training/serializers.py:41 in get_frequency           MATCH (opening) WHERE elementId(opening)=? WITH opening MATCH (opening)<-[r1:`RELATED_OPENING`]-(parent_r1:Opening) RETURN parent_r1
    afaf5d12  cypher    642         8    701.33    1.09   84.91             91  /backend/training/serializers.py:114 in get_moves_from_parent  MATCH (opening) WHERE elementId(opening)=? WITH opening MATCH (opening)<-[r1:`RELATED_OPENING`]-(parent_r1:Opening) WITH parent_r1 RETURN count(parent_r1)
    [shapes: 2 of 8 rows shown; --offset 2 shows the next rows, --limit N shows more (0 for all)]

    NEXT
      python manage.py silk_query queries 96de4a15 --shape 9879c756
      python manage.py silk_query query 89

The call site shown for a shape is that of its slowest execution.

profiles and profile
~~~~~~~~~~~~~~~~~~~~

``profiles`` lists the blocks recorded with the ``silk_profile`` decorator or context manager, or with dynamic profiling. ``--name``, ``--function`` and ``--request`` filter the list. ``profile`` shows one block and the shapes of the queries that ran inside it. The output below is for a block named ``load openings`` placed around the first query of the request above.

.. code-block:: bash

    python manage.py silk_query profiles --request 96de4a15
    python manage.py silk_query profile 3

.. code-block:: text

    profile 3
    name: load openings
    function: get
    file: /backend/training/views.py
    line: 101
    end_line: -
    request: 96de4a15-78f1-40c5-b34d-be125c4af704
    start_time: 2026-10-10T07:39:59.050Z
    end_time: 2026-10-10T07:39:59.401Z
    time_ms: 351.20
    queries_ms: 280.26
    queries: 1 (0 sql, 1 cypher, 0 other)
    dynamic: no
    exception_raised: no

    SHAPES (1, by total time)
    shape     kind    count  total_ms  avg_ms  max_ms  slowest_query  call_site                              text
    ca299e7b  cypher      1    280.26  280.26  280.26             87  /backend/training/views.py:107 in get  MATCH (opening:Opening) WHERE NOT EXISTS ((opening)<-[:`RELATED_OPENING`]-(:Opening)) RETURN opening ORDER BY opening.games DESC

    NEXT
      python manage.py silk_query query 87
      python manage.py silk_query request 96de4a15-78f1-40c5-b34d-be125c4af704
      python manage.py silk_query profiles

Output conventions
------------------

- Plain text by default, with no colour, whether or not the output is a terminal. ``--format json`` prints one JSON document with the same data. The cuts are listed under its ``truncated`` key and the follow-up commands under ``next``.
- Durations are in milliseconds, in fields and columns whose names end in ``_ms``.
- Timestamps are ISO 8601 in UTC.
- A missing value is printed as ``-`` (``null`` in JSON) and never as ``0``. A request without a response has no status and no duration.
- Lists show the first 8 characters of a request id. Any prefix that matches one request is accepted wherever a request id is expected.
- Output is bounded. A list prints 20 rows, query text in a table row is cut to 200 characters, and other long text (a query, a plan, a body, the cProfile output) to 4000. When something is cut, a line in square brackets says how much was shown and which option shows the rest: ``--limit`` and ``--offset`` for rows, ``--max-chars`` for text (``0`` removes the limit), ``--max-requests`` for the requests analysed by ``summary`` and ``shapes``.
- Invalid input ends with a non-zero exit status and a message that names the value and what is accepted. A query that matches nothing ends with status 0 and says so, with the number of recorded requests and the start time of the most recent one.

.. code-block:: bash

    python manage.py silk_query request 96de4a15 --format json
    python manage.py silk_query queries 96de4a15 --sort time --limit 50 --offset 50
    python manage.py silk_query query 89 --full-traceback --max-chars 0

Terms
-----

kind
    ``sql``, ``cypher`` or ``other``. Silk stores every query in one table with no marker, so the kind is told from the leading keyword of the query.

shape
    The query with its literals replaced by ``?`` and lists of literals by ``(?+)``. Queries that differ only in their values have the same shape. The shape id depends only on that text, so it is the same in every request and can be passed between subcommands.

call site
    The innermost frame of the traceback of a query that is application code: not an installed package, the standard library or Silk. It is ``unknown`` when there is no such frame.

hint
    A pattern in a request that is worth a look, printed with the numbers behind it:

    - ``repeated_shape``: one shape ran 5 times or more, which usually is an N+1 pattern.
    - ``identical_queries``: the same query text ran more than once.
    - ``dominant_query``: one query took at least half of the request.
    - ``time_outside_queries``: at least half of the request, and at least 100 ms, was not spent in queries.

Limits
------

- Time outside queries is not attributed to anything unless ``SILKY_PYTHON_PROFILER`` was on when the request was recorded.
- Cypher queries carry no ``EXPLAIN`` output. SQL queries carry the plan Silk got when it recorded them; the command runs nothing again.
- Kind, shape and call site are heuristics. Silk records SQL with its parameters interpolated and unquoted, so a value that contains the word ``AND`` in capitals or a closing parenthesis can give one query two shapes. Totals stay correct.
- ``summary`` and ``shapes`` analyse the most recent matching requests up to ``--max-requests`` and say how many they covered.
