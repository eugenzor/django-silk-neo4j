# Proposal

## Why

Everything silk records is only reachable through its web UI, so an LLM agent investigating a slow request cannot read it without a browser, a login and one page load per query. The data is also shaped for a human clicking through pages: in the chess-training stack one request ran 352 queries and each stored traceback averages 9 KB, so a literal dump of that request is about 3 MB. An agent needs the same data from a shell command, grouped and trimmed so that the cause of a slow request is visible in one or two calls.

## What Changes

- Add a read-only management command, `python manage.py silk_query <subcommand>`, that exposes the recorded data:
  - `summary`: totals and a per-view table with median, 95th percentile and maximum time, query counts and time in queries.
  - `requests`: the request list with the UI's filters and orderings, plus filters the UI lacks (status class, path substring, time window).
  - `request <id>`: one request with its timing split, query shapes, hints, profiles, and on demand its headers, bodies, cProfile output and a curl command that reproduces it.
  - `queries <request-id>`: every query execution of a request, as a timeline or by duration, filterable by shape and kind.
  - `query <id>`: one query in full with its `EXPLAIN` output and traceback.
  - `shapes`: query shapes aggregated across many requests, to find the queries that cost the most overall.
  - `profiles` and `profile <id>`: the `silk_profile` blocks and their queries.
  - `guide`: the investigation workflow, the data model and the limits of what silk records.
- Add analysis the UI does not have, because an investigator needs it:
  - Queries are grouped into shapes (the query with its literals replaced), each with a stable id, count and total, average and maximum time.
  - Each query is labelled SQL or Cypher. Silk stores both in one table with no marker.
  - Each query gets a call site: the innermost traceback frame that belongs to the application and not to a library.
  - Each request gets hints: a shape repeated many times, identical queries run more than once, one query that dominates, and a large share of time spent outside queries.
- Output is compact plain text by default and JSON with `--format json`. Every output is bounded by default, says when it was cut and how to get the rest, and ends with the exact follow-up commands.
- Document the command: full `--help` with examples on every subcommand, a README section and a docs page.

Not in this change:

- No change to what silk records. A stored query-kind column and anything that alters the middleware, models or views are left out.
- No write operations. Clearing and garbage collection stay in the existing commands.
- The binary `.prof` file and the call-graph view are not exposed; only the cProfile text that silk stores on the request.
- The follow-up in chess-training (bump the pinned commit, tell agents the command exists, optionally turn on `SILKY_PYTHON_PROFILER`) belongs to that repository.

## Capabilities

### New Capabilities

- `recorded-data-cli`: reading and analysing silk's recorded requests, queries and profiles from the command line, in a form meant for LLM agents and scripts.

### Modified Capabilities

None.

## Impact

- **Code**: a new command in `silk/management/commands/` and a new package beside it for fetching, analysing and rendering. Existing modules are not edited, which keeps merges from `jazzband/master` clean.
- **Dependencies**: none added. `sqlparse` is already required.
- **Database**: no migrations. The command only reads, on every backend silk supports (SQLite, PostgreSQL, MySQL).
- **Security**: the command runs with shell access to the project and bypasses `SILKY_AUTHENTICATION`, like any management command. Headers and bodies are printed only when asked for; they are already masked by `SILKY_SENSITIVE_KEYS` and `SILKY_HIDE_COOKIES` when silk records them.
- **Docs**: `README.md`, a new page under `docs/`, and `CHANGELOG.md`.
- **Consumers**: chess-training gets the command once it bumps the fork commit pinned in `backend/requirements/base.txt`.
