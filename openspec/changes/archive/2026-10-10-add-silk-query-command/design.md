# Design

## Context

See `proposal.md` for the motivation. The facts that shape the approach:

- All recorded data lives in four models in `silk/models.py`: `Request`, `Response`, `SQLQuery` and `Profile`. The views in `silk/views/` read them directly and keep their filter state in the HTTP session, so their query logic cannot be called from a command as it is.
- `SQLQuery` holds both SQL and Cypher. `silk/neo4j.py` registers Cypher queries through the same collector, with `analysis` always empty. Nothing in the row says which kind it is.
- Recorded SQL has its parameters interpolated with `q % params` in `silk/sql.py`, so string values appear unquoted (`WHERE "auth_user"."username" = eugene`). Recorded Cypher has parameters rendered as quoted literals by `render_cypher`.
- A traceback is stored innermost frame first, as `File "...", line N, in func` followed by the source line. In the chess-training data it averages 9 KB and 60 frames, of which one or two are application code. Rows recorded under an older image carry paths of a different Python version (`/usr/local/lib/python3.11/...` while the stack now runs 3.14).
- The data set can be large and unbounded: chess-training sets `SILKY_MAX_RECORDED_REQUESTS_CHECK_PERCENT = 0`, so nothing is collected automatically, and silk is loaded in its production settings.
- The fork is merged from `jazzband/master` regularly, so edits to upstream files cost conflicts later.
- Supported range is Python 3.10+ and Django 5.2+, on SQLite, PostgreSQL and MySQL.

## Goals / Non-Goals

**Goals:**

- One or two calls take an agent from "this view is slow" to the query shape and source line responsible.
- Output small enough to read whole by default, with every cut announced.
- Text and JSON output that cannot drift apart.
- No edits to existing silk modules.

**Non-Goals:**

- Sharing query logic with the views. They stay as they are.
- Exact SQL or Cypher parsing. Kind, shape and call site are heuristics and are documented as such.
- Live data: the command reads what was recorded and does not re-run queries or `EXPLAIN`.

## Decisions

### One command with argparse subparsers

`silk_query` registers its subcommands with `parser.add_subparsers(dest='subcommand', required=True)`, which Django's `CommandParser` supports. Each subparser gets its own `help`, `description` and an `epilog` with examples, rendered with `RawDescriptionHelpFormatter` so the examples keep their line breaks. Filters shared by `requests`, `summary` and `shapes` come from one parent parser.

*Alternative: one command per view (`silk_requests`, `silk_request`, ...).* Rejected: nine entries in `manage.py help` and no single place for an agent to discover the set.

*Alternative: one flat command with `--request`, `--query` and mode flags.* Rejected: the valid combinations of flags become the hard part of the help text.

### A separate package that builds reports as plain data

New package `silk/reporting/`, with the command as a thin layer over it:

```
silk/management/commands/silk_query.py   argument parsing, dispatch, errors
silk/reporting/data.py                   ORM reads -> plain dicts and lists
silk/reporting/analysis.py               kind, shape, call site, hints, percentiles
silk/reporting/render.py                 report -> text, report -> JSON
silk/reporting/guide.py                  guide text
```

Each subcommand builds one report: a dict of plain values, including a `truncated` entry for every cut and a `next` list of follow-up commands. Both renderers take that dict. JSON output is the dict; text output is a rendering of it. This is what keeps the two formats carrying the same data, and it lets the analysis functions be tested without a database.

*Alternative: render with Django templates like the views.* Rejected: whitespace control in templates is awkward for aligned plain text, and JSON would need a second code path.

### Text layout

- A header line naming the object, then `key: value` lines.
- Tables as aligned columns separated by two spaces, with a header row and the units in the column name (`time_ms`). The free-text column (path, query text) goes last so alignment padding stays small.
- Sections in capitals (`SHAPES`, `HINTS`, `NEXT`).
- Lists show the first 8 characters of a request id; detail views show the full id.

*Alternative: Markdown tables.* Rejected: the pipes and separator row add characters without adding information.

*Alternative: JSON by default.* Rejected: for tabular data the repeated keys roughly double the size, and the main reader is an agent paying per token.

### Bounds and defaults

| What | Default | Option |
|---|---|---|
| Rows in a list | 20 | `--limit`, `--offset` |
| Characters of query text in a list row | 200 | `--max-chars` |
| Characters of query text, body, plan or profiler output in a detail view | 4000 | `--max-chars` (`0` removes the limit) |
| Requests analysed by `summary` | 1000 most recent matching | `--max-requests` |
| Requests analysed by `shapes` | 50 most recent matching | `--max-requests` |

Aggregates are bounded by a count of requests and not by a default time window. A default such as "last 24 hours" returns nothing on a development machine whose data is older, while a count bound always returns the latest data and always costs about the same. A time window stays available through `--since` and `--until`.

### Keeping reads cheap

- `requests` fetches its page of `Request` rows first, using the indexed `start_time` order, then runs one grouped query for the time in queries of those ids. The `Sum('queries__time_taken')` annotation the UI uses is applied only when the user sorts or filters by time in queries, because it forces a join and grouping over the whole table before the limit.
- Percentiles are computed in Python from `(view_name, time_taken, num_sql_queries)` tuples of the bounded request set. No backend-specific SQL.
- `traceback` and `analysis` are deferred wherever they are not printed. `request` and `queries` load tracebacks for one request, streamed with `.iterator()`. `shapes` works across requests, so it loads query text and timings only, and then one traceback per shape, that of its slowest execution, for a representative call site.

### Query kind by leading keyword

`analysis.kind(text, has_analysis)`:

1. `sql` if the row has `EXPLAIN` output, since only SQL can.
2. `sql` if the text starts with `SELECT`, `INSERT`, `UPDATE`, `DELETE FROM`, `BEGIN`, `COMMIT`, `ROLLBACK`, `SAVEPOINT`, `RELEASE`, `ALTER`, `DROP`, `PRAGMA`, `CREATE TABLE|INDEX|VIEW`, or `WITH <name> AS (`.
3. `cypher` if it starts with `MATCH`, `OPTIONAL MATCH`, `MERGE`, `CALL`, `UNWIND`, `RETURN`, `CREATE`, `WITH`, `DELETE`, `DETACH`, `SET`, `FOREACH`, `USE`, `SHOW`, `PROFILE` or `EXPLAIN`.
4. `other` otherwise.

*Alternative: a `kind` column on `SQLQuery`, set at capture.* More reliable, but it changes an upstream model, adds a migration to every consumer and leaves existing rows unlabelled. It can replace step 2 and 3 later without changing the command's output.

### Shape normalisation

Applied in order to the query text:

1. Collapse runs of whitespace and trim.
2. Replace single-quoted strings with `?`.
3. Replace numbers that are not part of an identifier with `?` (so `LIMIT 21` becomes `LIMIT ?` while `r1` and `T3` stay).
4. For SQL only, replace the right-hand side of a comparison (`=`, `<>`, `!=`, `<`, `>`, `<=`, `>=`, `LIKE`, `ILIKE`) with `?` when it does not start with a quoted identifier, a parenthesis or a placeholder. Django quotes every identifier, so an unquoted right-hand side is an interpolated value. The same applies to the content of `IN (...)` when it is not a subquery.
5. Collapse `(?, ?, ...)` and `[?, ?, ...]` to `(?+)` and `[?+]`.

The shape id is the first 8 hex characters of the SHA-1 of the result. It depends only on the text, so it is the same in every request and survives restarts.

*Alternative: tokenise with `sqlparse`.* It cannot recover the unquoted values either, knows nothing about Cypher, and is slow over thousands of queries.

### Call site by path pattern

Parse the traceback into frames and take the first one (innermost) whose path does not contain `site-packages`, `dist-packages` or `/lib/python`, does not start with `<`, and is not inside the `silk` package. The check is on the recorded path text and not on the running interpreter's `sysconfig` paths, because rows may have been recorded by a different Python version or in a different container than the one running the command.

The reduced traceback printed by `query` is every frame that passes the same test, in recorded order.

### Hint thresholds

Fixed constants in `analysis.py`, stated in the spec: repeated shape at 5 executions, identical text at 2, dominant query at half the request time, time outside queries at half the request time and 100 ms. They are not configurable in this change; a hint is a pointer for the reader, and each one prints the numbers behind it.

### Request id resolution

Try an exact match, then `id__startswith`. Zero matches and several matches are both `CommandError`s; the second lists up to ten matching ids.

### Errors and exit codes

Invalid input and unknown ids raise `CommandError`, which Django prints to standard error and turns into exit status 1. Empty results are normal output with status 0. Report data goes only to `self.stdout`.

### Reproducing a request

`request --curl` reuses `silk.code_generation.curl.curl_cmd`. The UI builds the URL from the request that is viewing the page; the command has none, so it uses `http://` plus the recorded `Host` header, overridable with `--base-url`.

## Risks / Trade-offs

- [Kind is guessed wrong for an unusual statement] → It affects a label and a per-kind count, not the grouping. The guide says kind is a heuristic, and `other` is used when nothing matches.
- [SQL values containing ` AND ` or a closing parenthesis split a shape] → Rule 4 stops at the first such token, so a rare value produces an extra shape. Totals stay correct; only grouping gets finer. Covered by a test that pins the known limitation.
- [Loading 350 tracebacks of 9 KB for `request`] → About 3 MB, streamed, for a single request. Acceptable for a command run by hand or by an agent; `shapes` avoids it as described.
- [`summary` on a very large table] → Bounded by `--max-requests`; the `start_time` index serves the ordering. Filters on view name and path are indexed, the others scan within the bound.
- [Bodies and headers reach an LLM] → They are off by default and already masked at capture by `SILKY_SENSITIVE_KEYS` and `SILKY_HIDE_COOKIES`. The guide and docs say that values outside those keys are printed as recorded.
- [Running against production] → The command opens no transaction of its own and only reads, but it bypasses silk's UI authentication. This is the same trust level as `manage.py shell` and is stated in the docs.
- [Upstream adds its own command or changes the models] → Everything is in new files; the only coupling is the model field names.

## Migration Plan

Additive, with no migration and no setting. Consumers get the command by upgrading the package. Rollback is removing the new files.
