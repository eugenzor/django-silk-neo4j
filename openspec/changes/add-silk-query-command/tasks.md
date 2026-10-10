# Tasks

Tests live in `project/tests/` and run from `project/` with `DB_ENGINE=sqlite3 DB_NAME=":memory:" pytest tests/<file>` (the `sqlite3` tox environment sets the same variables).

## 1. Analysis functions

- [x] 1.1 Create the `silk/reporting/` package with `analysis.py` and implement `kind(text, has_analysis)` as described in design.md; verify with unit tests in `project/tests/test_reporting_analysis.py` covering `SELECT`, `MATCH`, `CALL`, `DELETE FROM` vs `DELETE n`, `WITH x AS (` vs `WITH opening`, a row with `EXPLAIN` output, and an unrecognised statement returning `other`
- [x] 1.2 Implement shape normalisation and the 8-character shape id; verify with tests that queries differing only in quoted strings, numbers, unquoted SQL right-hand values and `IN` list length share an id, that different tables or node labels do not, that identifiers such as `r1` and `T3` are kept, and that the id is the same across two calls
- [x] 1.3 Add a test that pins the documented limitation: an unquoted SQL value containing ` AND ` produces a different shape than the same query with a simple value; verify the test passes and carries a comment pointing to the risk in design.md
- [x] 1.4 Implement traceback parsing, call-site selection and the application-frames reduction; verify with tests using a real traceback from a neomodel query (library frames, then a `views.py` frame, then Django frames), a traceback recorded under a different Python version path, one with only library frames returning unknown, and one whose innermost frame is inside `silk`
- [x] 1.5 Implement the four hints with their thresholds and evidence, and the median and 95th-percentile helper; verify with tests at and just below each threshold, with a request that triggers none, and with percentile inputs of one, two and many values

## 2. Reading recorded data

- [x] 2.1 Add `SQLQueryFactory` usage helpers and a `ProfileFactory` to `project/tests/factories.py` as needed to build a request with a response, timed queries of both kinds and a profile; verify the factories create valid rows in a test
- [x] 2.2 Implement in `silk/reporting/data.py` the request filters (path, path substring, view, method, status code and class, `--since`/`--until` as duration or ISO timestamp, minimum time, minimum queries, minimum time in queries), ordering, limit and offset; verify with tests in `project/tests/test_reporting_data.py` for each filter, each ordering in both directions, and combined filters
- [x] 2.3 Fetch time in queries for a page of requests with one grouped query, and apply the `Sum` annotation only when sorting or filtering by it; verify with `assertNumQueries` that listing 20 requests ordered by start time issues a fixed number of queries independent of the number of rows
- [x] 2.4 Implement request id resolution by exact id or unique prefix; verify with tests for an exact match, a unique prefix, an ambiguous prefix listing the candidates, and no match
- [x] 2.5 Implement the report builders for `summary`, `requests`, `request`, `queries`, `query`, `shapes`, `profiles` and `profile`, each returning a plain dict with `truncated` and `next` entries and deferring `traceback` and `analysis` where they are not printed; verify with tests that each report contains the fields its spec requirement lists, that `summary` and `shapes` state the number of requests analysed, and that `shapes` loads one traceback per shape
- [x] 2.6 Implement the optional request sections (headers, bodies with binary detection, cProfile text, curl with `--base-url`); verify with tests that they are absent by default with their sizes listed, present when requested, that a non-text response body is reported by content type and size, and that a missing profiler output names `SILKY_PYTHON_PROFILER`

## 3. Rendering

- [x] 3.1 Implement the text renderer in `silk/reporting/render.py` (header, `key: value` lines, aligned tables with units in column names, capitalised sections, truncation notices, `NEXT` list); verify with tests in `project/tests/test_reporting_render.py` that output has no escape sequences, that missing values are not rendered as `0`, that durations are labelled in milliseconds and timestamps are ISO 8601 UTC
- [x] 3.2 Implement text and row truncation with `--max-chars`, `--limit` and `--offset`, each cut stating shown versus total and the option that lifts it; verify with tests for a cut list, a cut query text, and `--max-chars 0` printing in full with no notice
- [x] 3.3 Implement the JSON renderer as a serialisation of the same report dict; verify with a test per subcommand that the output parses as one JSON object and that every id, count and duration shown in the text output is present in it

## 4. Command

- [x] 4.1 Add `silk/management/commands/silk_query.py` with the subparsers, the shared filter parent parser, `--format`, and dispatch to the report builders and renderers; verify with `call_command` tests in `project/tests/test_command_silk_query.py` that each subcommand exits cleanly and writes only to stdout
- [x] 4.2 Write `help`, `description` and an examples `epilog` for the command and every subcommand, with accepted values and defaults on every option; verify with tests that top-level help lists every subcommand and mentions `guide`, and that each subcommand's help contains an example line starting with `python manage.py silk_query`
- [x] 4.3 Raise `CommandError` for a missing subcommand, an unknown or ambiguous id, and invalid `--sort`, `--since`, `--status` and `--format` values, each naming the accepted values; verify with tests asserting the error text and that nothing is written to stdout
- [x] 4.4 Handle empty results with exit status 0, stating that nothing matched, the total number of recorded requests and the start time of the most recent one; verify with tests for an empty database and for filters that exclude every row
- [x] 4.5 Add a test that snapshots row counts and a checksum of the four silk tables, runs every subcommand, and verifies both are unchanged

## 5. Guide and documentation

- [x] 5.1 Write the guide text in `silk/reporting/guide.py` (workflow order, glossary of shape, kind, call site and hint, limits of the data, note on headers and bodies) and wire the `guide` subcommand; verify with tests that it names every other subcommand, runs with an empty database, and that every subcommand registered in the parser appears in it
- [x] 5.2 Add `docs/silk_query.rst` with the workflow, one worked example per subcommand and the output conventions, and link it from the toctree in `docs/index.rst`; verify the `rst-linter` pre-commit hook passes and that each documented command line is accepted by the parser in a test
- [x] 5.3 Add a "Querying recorded data from the command line" section to `README.md` with a table-of-contents entry, and a line under the fork heading in `CHANGELOG.md`; verify the README examples match the commands tested in 5.2

## 6. Integration check

- [x] 6.1 Run the full test suite on SQLite and PostgreSQL through tox and the pre-commit hooks (`flake8`, `isort`, `autopep8`, `pyupgrade`); verify all pass with no changes to files outside `silk/reporting/`, the new command, `project/tests/`, `docs/`, `README.md` and `CHANGELOG.md`
- [x] 6.2 Install the working tree into the running chess-training backend container without changing its requirements file, then walk `summary` -> `requests --sort time` -> `request <id>` -> `queries <id> --shape <shape>` -> `query <id>` for `/api/training/openings/`; verify the 352-query request is reported with its shapes, a repeated-shape hint and a call site in `training/views.py`, and that the default `request` output is under 200 lines
