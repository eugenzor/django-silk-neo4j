# recorded-data-cli Specification

## Purpose

Lets LLM agents and scripts read and analyse the requests, queries and profiles that silk has recorded, from a management command instead of the web UI, in output sized and structured for investigating performance problems.

## Requirements

### Requirement: Single read-only command with subcommands
The system SHALL provide a management command `silk_query` whose first argument selects one of the subcommands `guide`, `summary`, `requests`, `request`, `queries`, `query`, `shapes`, `profiles` and `profile`. No subcommand SHALL create, change or delete recorded data.

#### Scenario: Subcommand runs
- **WHEN** `silk_query requests` is run in a project with silk installed
- **THEN** the command prints the request list to standard output and exits with status 0

#### Scenario: No subcommand given
- **WHEN** `silk_query` is run without a subcommand
- **THEN** the command exits with a non-zero status and the error names every available subcommand

#### Scenario: Data is left untouched
- **WHEN** any subcommand is run against a database holding recorded requests, queries and profiles
- **THEN** the number and content of the recorded rows are the same after the command as before it

### Requirement: Self-describing help
The command SHALL describe itself well enough to be used without other documentation. `--help` on the command SHALL list every subcommand with a one-line purpose and point to `guide`. `--help` on a subcommand SHALL describe every argument and option, including accepted values and defaults, and SHALL include at least one example invocation.

#### Scenario: Top-level help
- **WHEN** `silk_query --help` is run
- **THEN** the output lists each subcommand with its purpose and tells the reader to run `silk_query guide` first

#### Scenario: Subcommand help
- **WHEN** `silk_query requests --help` is run
- **THEN** the output describes each filter and ordering option with its accepted values and default, and shows at least one example command

### Requirement: Investigation guide
The `guide` subcommand SHALL print a short guide covering the recommended order of subcommands for investigating a slow request, the meaning of the terms used in the output (shape, kind, call site, hint), and the limits of the recorded data, including that time outside queries cannot be attributed unless the Python profiler was enabled and that Cypher queries carry no `EXPLAIN` output.

#### Scenario: Guide names the workflow
- **WHEN** `silk_query guide` is run
- **THEN** the output names every other subcommand and states in which order to use them

#### Scenario: Guide works without data
- **WHEN** `silk_query guide` is run and no request has been recorded
- **THEN** the guide is printed and the command exits with status 0

### Requirement: Plain text and JSON output
Every subcommand SHALL print plain text by default and SHALL print a single JSON document when `--format json` is given. Text output SHALL contain no ANSI colour or cursor codes. The JSON document SHALL carry the same data as the text output for the same arguments.

#### Scenario: Default text output
- **WHEN** a subcommand is run without `--format`
- **THEN** the output is plain text with no escape sequences, whether or not standard output is a terminal

#### Scenario: JSON output
- **WHEN** `silk_query request <id> --format json` is run
- **THEN** standard output parses as one JSON object containing the request's fields, its query shapes and its hints

### Requirement: Consistent units and timestamps
All durations SHALL be reported in milliseconds and labelled as such. All timestamps SHALL be printed in ISO 8601 format in UTC. A missing value SHALL be shown explicitly and never as zero.

#### Scenario: Request without a response
- **WHEN** a recorded request has no response and no end time
- **THEN** its status and duration are shown as missing, not as `0`

### Requirement: Request ids accept unique prefixes
Lists SHALL show a shortened request id, and every argument that takes a request id SHALL accept either the full id or any prefix that matches exactly one request.

#### Scenario: Prefix matches one request
- **WHEN** `silk_query request 96de4a15` is run and one recorded request id starts with `96de4a15`
- **THEN** that request is shown, with its full id

#### Scenario: Prefix matches several requests
- **WHEN** a request id prefix matches more than one request
- **THEN** the command exits with a non-zero status and the error lists the matching ids

### Requirement: Bounded output with explicit truncation
Every subcommand SHALL limit its output by default: lists to a default number of rows, and long text values (query text, bodies, tracebacks, profiler output) to a default length. Whenever rows or text are cut, the output SHALL state how much was shown out of how much exists and name the option that shows the rest.

#### Scenario: List longer than the limit
- **WHEN** `silk_query requests` is run and more requests match than the default limit
- **THEN** the output states the number shown and the total matching, and names the options that page through or raise the limit

#### Scenario: Long text is cut
- **WHEN** a query's text is longer than the default text length
- **THEN** the text is cut, followed by a notice giving its full length and the option that prints it whole

#### Scenario: Limit lifted
- **WHEN** the option that removes the text limit is given
- **THEN** text values are printed in full with no truncation notice

### Requirement: Follow-up commands
Text output of every subcommand except `guide` SHALL end with a short list of concrete follow-up commands, filled in with ids taken from the output just printed. JSON output SHALL carry the same commands in a dedicated field.

#### Scenario: Request list points to a request
- **WHEN** `silk_query requests` prints at least one request
- **THEN** the output ends with a ready-to-run `silk_query request <id>` command using an id from the list

### Requirement: Actionable errors and empty results
Invalid input SHALL produce a non-zero exit status and an error on standard error that names the offending value and the accepted values or format. A valid query that matches nothing SHALL exit with status 0 and state that nothing matched, how many requests are recorded in total and when the most recent one started.

#### Scenario: Unknown id
- **WHEN** `silk_query request` is given an id that matches no request
- **THEN** the command exits with a non-zero status and the error says no request matches and how to list requests

#### Scenario: Invalid option value
- **WHEN** `--sort` is given a value that is not an accepted ordering
- **THEN** the command exits with a non-zero status and the error lists the accepted orderings

#### Scenario: Filters match nothing
- **WHEN** `silk_query requests --since 5m` is run and the most recent request is older than five minutes
- **THEN** the command exits with status 0 and reports that nothing matched, the total number of recorded requests and the start time of the most recent one

### Requirement: Request filters
The `requests`, `summary` and `shapes` subcommands SHALL accept the same filters: exact path, path substring, view name, HTTP method, status code or status class (such as `5xx`), a start-time window given as a relative duration (such as `15m`, `2h`, `7d`) or an ISO 8601 timestamp, minimum overall time, minimum number of queries and minimum time spent in queries. Filters SHALL combine with logical AND.

#### Scenario: Filter by view and minimum time
- **WHEN** `silk_query requests --view training:openings --min-time 500` is run
- **THEN** only requests of that view that took at least 500 ms are listed

#### Scenario: Filter by status class
- **WHEN** `silk_query requests --status 5xx` is run
- **THEN** only requests whose response status is between 500 and 599 are listed

#### Scenario: Relative time window
- **WHEN** `silk_query requests --since 2h` is run
- **THEN** only requests that started within the last two hours are listed

### Requirement: Request list
The `requests` subcommand SHALL list one request per row with its shortened id, start time, method, path, view name, response status, overall time, time spent in queries and number of queries. It SHALL support ordering by start time, overall time, number of queries, time spent in queries and path, in either direction, with most recent first as the default.

#### Scenario: Slowest requests first
- **WHEN** `silk_query requests --sort time` is run
- **THEN** requests are listed from the longest overall time to the shortest

#### Scenario: Ascending order
- **WHEN** an ordering is combined with the ascending option
- **THEN** the same requests are listed in the opposite order

### Requirement: Summary across requests
The `summary` subcommand SHALL report, for the requests matching the filters, the number of requests and the time range they cover, and a per-view table with request count, median, 95th percentile and maximum overall time, average number of queries and average time spent in queries, ordered by the view's total time. Statistics SHALL be computed over a bounded number of the most recent matching requests, and the output SHALL state how many requests were covered.

#### Scenario: Per-view statistics
- **WHEN** `silk_query summary` is run with requests recorded for several views
- **THEN** each view appears once with its request count, median, 95th percentile and maximum time, average query count and average time in queries

#### Scenario: Coverage is stated
- **WHEN** more requests match than the bound on analysed requests
- **THEN** the output states how many requests were analysed out of how many match and names the option that raises the bound

### Requirement: Query kind
Each query SHALL be labelled with a kind of `sql`, `cypher` or `other`, derived from its text and from whether it carries `EXPLAIN` output. Wherever queries are counted for a request, the count SHALL be broken down by kind.

#### Scenario: Cypher query
- **WHEN** a recorded query's text starts with `MATCH`
- **THEN** its kind is `cypher`

#### Scenario: SQL query
- **WHEN** a recorded query's text starts with `SELECT`
- **THEN** its kind is `sql`

#### Scenario: Ambiguous leading keyword
- **WHEN** a recorded query starts with `WITH` followed by a common table expression
- **THEN** its kind is `sql`, and a query starting with `WITH` followed by a Cypher variable projection is `cypher`

### Requirement: Query shapes
Queries SHALL be grouped into shapes. A shape is the query text with string and numeric literals replaced by a placeholder, lists of literals collapsed to one placeholder regardless of length, and whitespace normalised. Each shape SHALL have a short id that is identical for the same shape in any request, so that it can be passed between subcommands.

#### Scenario: Same query with different values
- **WHEN** two queries differ only in the values of their literals
- **THEN** they have the same shape id

#### Scenario: Lists of different length
- **WHEN** two queries differ only in the number of literals inside an `IN (...)` list
- **THEN** they have the same shape id

#### Scenario: Different structure
- **WHEN** two queries select from different tables or match different node labels
- **THEN** they have different shape ids

### Requirement: Call site
Each query SHALL be given a call site: the file, line, function name and source line of the innermost frame in its recorded traceback that is application code. Frames from installed packages, the standard library and silk itself SHALL NOT be chosen. When no frame qualifies, the call site SHALL be reported as unknown.

#### Scenario: Query issued from a view through an ORM
- **WHEN** a query's traceback passes through library frames and then a frame in the project's own `views.py`
- **THEN** the call site is that `views.py` frame, with its line number and function name

#### Scenario: Traceback has only library frames
- **WHEN** no frame of a query's traceback is application code
- **THEN** the call site is reported as unknown

### Requirement: Request detail
The `request` subcommand SHALL show one request: its full id, method, path, query parameters, view name, status, start time, overall time, time spent in queries and time outside queries, the number of queries by kind, its query shapes ordered by total time (each with count, total, average and maximum time, kind, text and most frequent call site), its hints, and its `silk_profile` blocks if any.

#### Scenario: Request with many queries
- **WHEN** `silk_query request <id>` is run for a request with 352 queries in 14 shapes
- **THEN** the output shows the timing split and the shapes ordered by total time, and does not print 352 individual queries

#### Scenario: Request without queries
- **WHEN** the request recorded no queries
- **THEN** the output says so and attributes the whole overall time to time outside queries

### Requirement: Optional request sections
The `request` subcommand SHALL print the following only when the matching option is given: request and response headers; request and response bodies; the stored cProfile output; and a curl command that reproduces the request. Bodies SHALL be shown as recorded by silk, which masks sensitive values at capture time, and a response body that is not text SHALL be reported by size instead of printed.

#### Scenario: Default output omits bodies and headers
- **WHEN** `silk_query request <id>` is run without section options
- **THEN** no header or body content is printed, and the output states which sections are available and their sizes

#### Scenario: Profiler output requested but not recorded
- **WHEN** the cProfile section is requested for a request recorded with the Python profiler disabled
- **THEN** the output states that no profiler output was recorded and names the setting that enables it

#### Scenario: Binary response body
- **WHEN** the response body section is requested and the body is not decodable as text
- **THEN** the output reports its content type and size and prints no body content

### Requirement: Request hints
The `request` subcommand SHALL report hints, each stating the evidence behind it: a shape executed five or more times; identical query text executed more than once; a single query that takes at least half of the request's overall time; and time outside queries of at least half the overall time and at least 100 ms. The last hint SHALL state whether profiler output exists for the request. A request with none of these SHALL say that no hint applies.

#### Scenario: Repeated shape
- **WHEN** a request executes one shape 310 times from the same call site
- **THEN** a hint names the shape id, the count, the total time and the call site, and describes it as a likely N+1 pattern

#### Scenario: Time outside queries
- **WHEN** a request took 2000 ms of which 300 ms were spent in queries and no profiler output was recorded
- **THEN** a hint reports 1700 ms outside queries and states that silk cannot attribute it because the Python profiler was not enabled

#### Scenario: Nothing to report
- **WHEN** none of the hint conditions hold
- **THEN** the output states that no hint applies

### Requirement: Query executions of a request
The `queries` subcommand SHALL list the individual query executions of one request, each with its id, offset from the start of the request, duration, kind, shape id, call site and shortened text. It SHALL support ordering by start offset (default) or by duration, and filtering by shape id, by kind and by minimum duration.

#### Scenario: Timeline
- **WHEN** `silk_query queries <request-id>` is run
- **THEN** executions are listed in the order they started, each with its offset from the request start

#### Scenario: One shape only
- **WHEN** `silk_query queries <request-id> --shape <shape-id>` is run
- **THEN** only executions of that shape are listed

### Requirement: Query detail
The `query` subcommand SHALL show one query execution: its id, the request it belongs to, kind, shape id, start offset, duration, full query text formatted for reading, the tables it involves and its number of joins when it is SQL, its `EXPLAIN` output when recorded, its call site, and its traceback reduced to application frames. An option SHALL print the complete traceback instead.

#### Scenario: SQL query with a plan
- **WHEN** `silk_query query <id>` is run for a SQL query recorded with `EXPLAIN` output
- **THEN** the output includes the formatted query, the tables involved and the recorded plan

#### Scenario: Cypher query
- **WHEN** the query is a Cypher query
- **THEN** the output states that no plan is recorded for Cypher queries

#### Scenario: Full traceback
- **WHEN** the full-traceback option is given
- **THEN** every recorded frame is printed, including library frames

### Requirement: Query shapes across requests
The `shapes` subcommand SHALL aggregate query shapes over the requests matching the filters and list each shape with its id, kind, number of executions, number of requests it appears in, total, average and maximum time, the call site of its slowest execution and text, ordered by total time. It SHALL analyse a bounded number of the most recent matching requests and state how many were covered.

#### Scenario: Costliest shapes of a view
- **WHEN** `silk_query shapes --view training:openings` is run
- **THEN** shapes found in recent requests of that view are listed from the highest total time to the lowest, with the number of requests each appeared in

### Requirement: Profiles
The `profiles` subcommand SHALL list recorded `silk_profile` blocks with their id, name, function name, file and line, duration, number of queries, time spent in queries and owning request, filterable by name, function name and request. The `profile` subcommand SHALL show one block with the same fields and its query shapes.

#### Scenario: No profiles recorded
- **WHEN** `silk_query profiles` is run and no profile block has been recorded
- **THEN** the command exits with status 0 and explains that profile blocks come from the `silk_profile` decorator or context manager or from dynamic profiling

#### Scenario: Profile with queries
- **WHEN** `silk_query profile <id>` is run for a block that executed queries
- **THEN** the output shows the block's duration, time in queries and its query shapes
