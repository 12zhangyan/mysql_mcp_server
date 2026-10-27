# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- `SHOW OPEN TABLES` must name a database (`SHOW OPEN TABLES FROM app`). Without
  `FROM`, MySQL lists open tables in every database, so the statement returned
  database and table names outside `allowed_databases`, including system schemas.
- Schema-qualified function calls such as `app.now()` must be listed in
  `allowed_functions`. MySQL resolves them as stored functions even when the name
  matches a built-in, but the guard exempted them through its built-in safe list,
  so a stored function named `now` or `uuid` ran without review and could call
  functions the guard blocks, such as `SLEEP` or `GET_LOCK`.

### Fixed
- Read-only SQL that spells a blocked statement keyword is no longer rejected:
  the `REPLACE()`, `INSERT()` and `TRUNCATE()` functions, `CHARACTER SET` /
  `CHAR SET` in `CAST`, `USE INDEX` / `USE KEY` hints, and unquoted identifiers
  named after non-reserved statement words such as `start`, `begin` or `commit`.
  Reserved statement keywords remain blocked everywhere else, and statement words
  are still rejected where a statement begins.

## [0.9.10] - 2026-10-27

### Fixed
- MCP paging now runs the query as written. It previously executed SQL regenerated
  by sqlglot, which can change results and column names: `7 DIV 2` ran as
  `CAST(7 / 2 AS SIGNED)` and returned 4 instead of 3, and `IFNULL(x, 0)` came back
  as a `COALESCE(x, 0)` column. Only the trailing top-level `LIMIT` is appended or
  replaced; a query whose `LIMIT` cannot be located exactly is paged while reading.
- `SHOW`, `DESCRIBE`, `EXPLAIN` and other results without a pushed-down `LIMIT`
  are truncated at `max_rows` with a `next_offset` instead of failing with
  `InternalError` ("Unread result found"). The failed cleanup also returned the
  connection to the pool with its result unread, so every later query on that
  pool failed until the MCP server restarted. The remaining rows are now read and
  discarded before the cursor closes.

## [0.9.9] - 2026-10-26

### Security
- Values read from a JSON document by path are masked when a key on the path matches
  an unqualified mask pattern. `profile->>'$.phone'`, `JSON_EXTRACT(profile,
  '$.contact.phone')`, `JSON_VALUE`, every path of a multi-path `JSON_EXTRACT`, and
  JSON_TABLE columns (including root and `NESTED PATH` segments) previously returned
  the value in plain text even though the same key was masked inside a returned
  JSON document. Wildcard, recursive (`**`) and computed paths are treated as
  sensitive; the fallback used when analysis fails applies the same rule.

### Changed
- CI now runs the opt-in MySQL acceptance tests against its MySQL 8.0 service,
  seeding a commented table for the read-only metadata test; they were previously
  skipped on every run.
- CI tests against the declared minimum sqlglot (26.0.0) and the version locked into
  the npm package, in addition to the newest release, because SQL safety and masking
  analysis depend on sqlglot internals.

## [0.9.8] - 2026-10-25

### Security
- Result masking now resolves every result column through its query scopes and
  fails closed. The previous lineage-based tracing returned sensitive values
  unmasked for several query shapes, including unaliased expressions or
  conversions passed through a derived table or CTE and `*`
  (`SELECT * FROM (SELECT CAST(password AS CHAR) FROM users) t`), stars mixed with
  expressions or scalar subqueries (`SELECT *, UPPER(password) FROM users`), and
  table-qualified patterns behind same-named nested columns. Each output is traced
  through aliases, expressions, window functions, scalar and correlated subqueries
  (including outer select-list aliases and derived tables or CTEs nested inside
  subqueries), derived tables, CTEs and their column lists, set operations,
  LATERAL, JSON_TABLE, TABLE statements and stars to base-table columns; an output
  whose source cannot be determined is masked when it could come from a matching
  field, and analysis failures use conservative rules. Result names are taken from
  the SQL as written, and a name produced by more than one projection (for example
  a string literal or alias repeating an expression's text) is not trusted to
  identify that expression. Each scope and column reference is analysed once, so
  masking time grows linearly with query nesting.
- Table-qualified mask patterns (`users.phone`, `app.users.phone`) now resolve table
  aliases (`SELECT u.phone FROM users u`), apply to stars over base tables (including
  the `mysql://<table>/data` resource) and to unqualified columns in joins, keep the
  database through derived tables, use the selected database for tables named
  without one, and distinguish same-named tables joined from different databases.

### Changed
- Values from WHERE, HAVING, EXISTS and IN subqueries no longer cause unrelated
  columns to be masked, and a star over a base table no longer masks every column
  when a scalar subquery reads a sensitive field.
- README, SECURITY and the enterprise deployment guide describe masking as a
  fail-closed static-analysis egress control and recommend database views or
  column-level permissions for fields that must never be exposed.

## [0.9.7] - 2026-10-25

### Security
- Result masking now traces columns surfaced through `*` over a derived table or
  CTE (for example `WITH x AS (SELECT password AS p FROM users) SELECT * FROM x`).
  Previously such a query matched output names only whenever its projection count
  equalled its column count, so an aliased sensitive column was returned unmasked.
  When tracing fails, nested projections of the same name are used conservatively.

### Fixed
- Resource reads (`list_resources`, `read_resource`) now share the per-profile query
  admission with tool calls. Concurrent resource reads could otherwise exhaust the
  Connector/Python pool, which fails instead of waiting, and surface as a generic
  database error instead of a retryable `CONNECTION_BUSY`.
- The npm launcher no longer starts a runtime that another process is still
  reinstalling: a waiting process returns only once the installed runtime matches
  the bundled wheel and dependency lock, not merely when an older marker exists.
- `npm run release:version` and `npm run test:install` no longer fail with
  `EALLOWSCRIPTS` when the user npm config sets `allow-scripts`.
- `mypy` passes again for `src/mysql_mcp_server`.

## [0.9.6] - 2026-10-25

### Security
- Profiles and `MYSQL_SQL_MODE` now reject `sql_mode` values that change how MySQL
  delimits strings (`ANSI_QUOTES`, `NO_BACKSLASH_ESCAPES`, and the combination modes
  `ANSI`, `DB2`, `MAXDB`, `MSSQL`, `ORACLE`, `POSTGRESQL`). The read-only SQL guard
  tokenizes literals with MySQL's default rules, so these modes could make it read a
  query differently from the server. An affected profile is reported as invalid
  without disabling other profiles; mode names are normalized to upper case.
- Every query session explicitly applies the validated `sql_mode`. Previously an
  empty `sql_mode` left the session on the server's global mode. On MySQL 5.7.20+ the
  setting joins the existing combined session `SET`, so no round trip is added.

### Fixed
- `dump_schema` through a logical route alias whose physical database name differs
  no longer fails when loading table metadata, and no longer sends that second query
  to a profile that happens to share the logical route's name.

## [0.9.5] - 2026-10-23

### Changed
- `dump_schema` trims an over-budget page with a binary search instead of
  re-serializing the grouped payload once per dropped row.
- Query results cache their rendered text and remember the format they were fitted
  for, so a tool call no longer renders the payload twice or reloads the connection
  registry to choose the format.
- Audit query fingerprints and statement types are computed once per SQL text.
- Paging pushdown reuses the validated AST without deep-copying it, including when
  generating the paged SQL. Executed SQL is unchanged.
- A cached profile registry is revalidated with a single `stat()` per lookup; file
  identity (`st_dev`/`st_ino`) is part of the cache key, so a retargeted symlink still
  reloads.
- Common cell types (`str`, `int`, `float`, `bool`, `None`) serialize on a fast path,
  and fetched rows are no longer copied before masking.
- Integer limits for profile files and legacy `MYSQL_*` variables come from one shared
  table; legacy defaults (`MYSQL_POOL_SIZE=0`, `MYSQL_LOCAL_PORT=3330`) are unchanged.
- The query tool path and the resource path share one readiness, query-policy and
  connection-release implementation.
- HTTP/SSE/stdio transport startup and bearer authentication moved to
  `mysql_mcp_server.transport`; existing `mysql_mcp_server.server` names remain
  importable.

### Fixed
- A bearer `Authorization` header containing non-ASCII bytes is rejected with HTTP 401
  instead of failing with HTTP 500.

## [0.9.4] - 2026-10-19

### Fixed
- The npm launcher no longer writes Python environment bootstrap output (venv creation
  and pip installs) to stdout on first run. That output now goes to stderr, so the MCP
  protocol stream stays clean for strict clients on the first start after install or
  upgrade. Bootstrap subprocesses no longer inherit the client's stdin.

## [0.9.3] - 2026-10-19

### Changed
- Parse each validated query once and share the AST between database-access checks,
  function-safety checks and paging pushdown; build the pushed-down SQL once instead of
  on every connector retry. LIMIT/OFFSET are attached as literal nodes so sqlglot no
  longer re-parses them. Executed SQL is unchanged.
- Scan for blocked functions with one precompiled pattern; the error now names the
  first blocked call in the query.
- Resolve credentials at most once per request, including connector retries.
- Combine the statement timeout and read-only session settings into one `SET` on
  MySQL 5.7.20+ (detected from the handshake, no extra round trip). MariaDB, older or
  unrecognized servers, and MySQL-compatible servers rejecting the combined statement
  keep the original statement sequence.
- Wake queued queries as soon as a slot is released instead of polling every 10 ms.
- Cache resolved audit log paths and created directories; a removed log directory is
  recreated on the next write. Rotation, fsync and fail-closed behavior are unchanged.
- Move MCP tool definitions to `tool_specs.py` and dispatch tools through a handler
  registry that is checked against the definitions at import. The published tool list
  is unchanged and pinned by a snapshot test.

### Added
- Optional per-connection `credential_cache_seconds` (0-300, default 0) for the
  `keyring` and `command` providers. Cached secrets stay in process memory and are
  dropped on authentication failure, `validate_connections` or shutdown.

## [0.9.2] - 2026-10-14

### Changed
- Disable result masking by default. Only fields explicitly configured with per-connection
  `mask_columns` or legacy `MYSQL_MASK_COLUMNS` are masked; omitted and empty rules
  leave all returned fields and JSON keys visible. No built-in field list is applied.
- Preserve existing explicit masking rules. Unqualified patterns match only field
  names, avoiding accidental masking caused by table/database names.
- With masking configured, preserve `COUNT` and `COUNT(DISTINCT ...)` statistics
  while masking matching raw values and expressions that combine them.
- Reuse one qualified SQL scope when tracing result columns for masking, avoiding
  repeated query preparation and AST copies for wide SELECT/CTE/UNION results.
- Add an opt-in localhost benchmark with synthetic rows and an explicit
  `--mask-sensitive-field` option to verify the configured masking path.

### Added
- Add `explain_sql` for estimated SELECT/CTE/UNION execution plans through the
  existing read-only, database-scope, audit, masking and budget controls. Plan
  pagination preserves the original query's LIMIT/OFFSET; ANALYZE is rejected.
- Classify server/client timeouts, lock waits, deadlocks, connection failures,
  disconnects and connection-limit errors with safe recovery guidance and
  explicit retryability, without adding automatic query replay.
- Exercise plan discovery and structured error delivery over a real MCP STDIO
  session with a mocked database adapter.

### Fixed
- Rebuild cached connection pools when pool_size changes, so live capacity matches
  the configured admission limit after either growing or shrinking a pool.
- Pass an explicit schema to SQLGlot lineage tracing for 30.22 compatibility,
  preserving sensitive-column masking through CTE aliases and UNION queries.
- Sanitize parser failures as SQL_PARSE_ERROR with numeric coordinates and a
  query fingerprint; never return SQLGlot exception text or SQL literals.
- Classify Connector/Python ReadTimeoutError/WriteTimeoutError as QUERY_TIMEOUT,
  discard the active connection and skip automatic replay. Limit the legacy
  errno=-1 retry to InterfaceError during execute/fetch.
- Preserve complete explain_sql plan cells. Oversized rows direct callers to
  budgeted JSON plan chunks with continuation offsets and a plan fingerprint;
  changed plans cannot be silently combined between pages.
- Reject optimizer hints that override session variables, statement timeouts or
  resource groups. Ordinary optimizer hints are no longer mistaken for UDFs.
- Return local deadline errors as structured QUERY_TIMEOUT results, and audit
  server-side statement timeouts as timeout outcomes.

## [0.9.1] - 2026-09-15

### Added
- Paginate `inspect_catalog` and `get_schema_info` with the same `max_rows` /
  `offset` / `truncated` / `next_offset` / `returned_rows` contract as
  `list_tables`. Whole-database `kind=columns` uses a stable
  `TABLE_NAME, ORDINAL_POSITION, COLUMN_NAME` order, so offset is applied instead
  of repeating the first page.
- Filter catalog/schema tools with `table_pattern` (`*` / `?`) or `table_names`
  (1–100 bare names; longer lists are rejected). `table_name`, `table_names`,
  and `table_pattern` stay mutually exclusive.
- Add `dump_schema` to return allowlisted table/column metadata grouped by table.
  Oversized responses stay paged; indexes/constraints/views remain on
  `inspect_catalog`.

### Changed
- Tool descriptions for `list_tables`, `get_schema_info`, `inspect_catalog`, and
  `dump_schema` now require callers to follow `truncated` / `next_offset` and
  not treat a single page as a complete catalog.

## [0.9.0] - 2026-09-08

### Added
- Search table and column names/comments, including Chinese keywords, with
  scoped matching evidence; batch table metadata and detailed column/index/FK
  inspection with complete pagination.
- Limit query response text by UTF-8 bytes, preserve whole rows and continuation
  offsets, and report row, byte-budget and cell-content truncation separately.
- Offer a compact JSON-metadata-plus-CSV format, retaining target database,
  route, masking and pagination context without changing the default CSV mode.
- Bound per-connection query concurrency and queue length; include queue time
  in the overall deadline and retain slots until cancelled workers finish.
- Add opt-in localhost-only, read-only MySQL and MCP STDIO acceptance tests,
  including SQL pagination rewrite comparisons and Chinese metadata search.

### Changed
- Database failures now return safe JSON text and MCP structuredContent with
  error categories, selected target, and recovery guidance. Clients parsing
  the old diagnostic text should use the structured fields instead.
- Guide callers to verify unknown tables/columns and correct deterministic SQL
  errors without repeated unchanged queries or automatic database switching.
- Reject conflicting database and qualified table arguments in schema lookup.

## [0.8.3] - 2026-09-04

### Changed
- Reject unknown transport names and invalid HTTP/SSE port or idle-timeout
  settings at startup instead of silently falling back or failing later.
- Keep the unit suite isolated from live database connections.
- Escape request-controlled identifiers in operational logs so control
  characters cannot create forged log lines.

### Fixed
- Stop reading unbounded resource results after the configured row limit and
  discard connections that still have unread rows.
- Require the documented `/data` action for table resource URIs.
- Generate valid bracketed Host allowlist entries for IPv6 listeners.
- Retire obsolete pools and SSH processes when profiles switch runtime modes,
  and invalidate pooled connections whenever an SSH tunnel is restarted.
- Close and remove connection pools that fail required TLS verification, and
  close database pools before stopping their SSH tunnels during shutdown.

## [0.8.2] - 2026-08-21

### Added
- Add the standard MCP Streamable HTTP transport at `/mcp`, including bounded
  session lifetime, bearer authentication, DNS rebinding protection, and
  DeepSeek Harness configuration examples. Legacy SSE remains available.

## [0.8.1] - 2026-08-13

### Added
- Add exact/glob filtering and pagination to `list_tables`, and accept `table`
  as a compatibility alias for `table_name` in table metadata/sample tools.

### Changed
- Allow database-scoped `SHOW CREATE TABLE` and `SHOW CREATE VIEW` while
  keeping arbitrary system-schema SQL and `USE` blocked.

### Fixed
- Push MCP pagination into eligible read queries and fully consume the bounded
  result, preventing Connector/Python `errno=-1` failures on client-side row
  truncation.
- Return tool failures with MCP `isError: true`.
- Mask password/PWD, mobile, secret and credential-token source columns without
  redacting unrelated sibling columns, and redact sensitive keys inside JSON
  configuration values.
- Avoid broad `*token*` matching that hid business fields such as channel codes
  and token duration values.

## [0.8.0] - 2026-08-04

### Added
- Add OS keyring credentials with an interactive `readonly-db-mcp credentials`
  command, plus a bounded no-shell command provider for enterprise secret
  managers. Credential values are never returned by diagnostics.
- Add explicit logical connection routes so one environment name can map exact
  database aliases to separate physical profiles without implicit guessing.
- Add `inspect_catalog` for allowlisted table, column, index, constraint,
  foreign-key and view metadata while raw system-schema SQL remains blocked.
- Record requested logical targets, resolved physical targets and route use in
  JSON results and audit events.

### Changed
- `list_connections` now reports credential provider types and configured
  logical routes without exposing hosts, usernames, credential references or
  secret values.

## [0.7.6] - 2026-08-03

### Fixed
- Add a `readonly-db-mcp` executable alias matching the unscoped npm package
  name so `npx @yanzhang123/readonly-db-mcp` resolves correctly on Windows
  with npm 10, while retaining the existing
  `mysql-mcp-server-readonly` command.
- Exercise the installed npm command shim in the package installation test.

## [0.7.5] - 2026-08-03

### Fixed
- Prevent MCP client examples from pinning a stale package release. Enterprise
  deployments now use an explicit `x.y.z` placeholder for their approved
  version, while quick-start examples follow the npm `latest` tag.

## [0.7.4] - 2026-08-03

### Fixed
- Bound `get_table_sample` queries with server-side `LIMIT`/`OFFSET` and fully
  consume the small result so truncated samples do not churn pooled sockets.
- Retry one transient Connector/Python client failure with `errno=-1` and
  expose only a redacted lifecycle phase and exception type if it persists.
- Report locking reads such as `SELECT ... FOR UPDATE` with a specific
  read-only policy rejection instead of a generic blocked-keyword error.

### Added
- Add a stateless `query` compatibility alias for clients migrating from older
  MySQL MCP servers.
- Add database-to-profile routing hints and mismatched-target suggestions
  without implicitly switching connections or environments.

## [0.7.3] - 2026-08-03

### Fixed
- Detect negotiated TLS correctly when MySQL Connector/Python uses its C
  extension, whose public `is_secure` property can remain false even when the
  native connection reports an active TLS cipher.
- Keep transport verification fail-closed when neither the public connection
  state nor the native TLS cipher confirms encryption.

## [0.7.2] - 2026-08-03

### Security
- Reject MariaDB `/*M!...*/` as well as MySQL `/*!...*/` executable comments.
- Enforce database scoping for `SHOW` statements and block unsafe server-wide
  variants from user SQL.
- Redact sensitive result columns, including sources hidden by aliases, CTEs,
  or expressions, using configurable per-profile patterns.
- Default database connections to TLS `REQUIRED` and reject a session when the
  connector reports that transport encryption was not established.
- Persist a durable `started` audit event before connecting in fail-closed mode.

### Fixed
- Dispose superseded connection pools during profile hot reload.
- Install exact hash-verified Python dependencies for the npm launcher and
  fingerprint its cache from both the embedded wheel and dependency lock.
- Separate deterministic npm launcher tests from the explicit live package
  installation test.

## [0.7.1] - 2026-08-03

### Fixed
- Align npm release CI with the proven OIDC configuration used by the
  publisher's existing package: explicit registry configuration and a pinned
  npm 11.18.0 client.

## [0.7.0] - 2026-07-30

### Added
- **Enterprise JSONL Audit:** Versioned UTC events now include event/request IDs,
  MCP operation and client metadata, optional actor/purpose/ticket attribution,
  database scope, policy decision, execution outcome and resource metrics.
- **Durable Audit Controls:** Added size rotation, configurable retention,
  optional fsync, HMAC-SHA256 signing, required context and fail-closed mode.
- **Denial Auditing:** SQL guard, database policy and missing-context rejections
  are audited before any database connection is opened.
- **Untrusted Credential Mode:** Read-only enforcement no longer treats account
  grants as a correctness boundary. Unknown stored functions/UDFs, MariaDB
  sequence advancement and additional lock/side-effect functions are blocked.
- **Privilege Assessment:** `check_connection` identifies non-read grants and
  reports whether the account/server provide database-level defense in depth.
- **SSE Authentication:** Added optional bearer authentication for both SSE
  endpoints and explicit authenticated-proxy trust mode.
- **npm Distribution:** Added `@yanzhang123/readonly-db-mcp`, a cross-platform
  launcher that embeds the matching Python wheel, plus version-synchronized
  GitHub Actions publication with OIDC provenance or token fallback.
- Added an enterprise deployment, SIEM and operational security guide.

### Changed
- SSE now binds to `127.0.0.1` by default. Public binds fail safely unless
  bearer authentication or authenticated-proxy trust is explicitly configured.
- General error logs record error types instead of database-supplied details.
- Connection diagnostics redact authenticated usernames, raw grants, absolute
  profile paths, database error text and debug tracebacks by default.
- Resource reads now respect the profile row cap.

### Fixed
- `check_connection` now uses a MySQL-compatible alias for `CURRENT_USER()`;
  the previous alias failed on a real MySQL 8.0 server.
- Malformed and rejected SQL always receives a safe, non-reversible audit
  fingerprint.

## [0.6.0] - 2026-07-29

### Added
- **Timeout and Cancellation:** Full-call deadlines, Connector socket timeouts,
  MySQL/MariaDB statement timeouts, and active socket shutdown on MCP cancellation.
- **Connection Diagnostics:** Added `validate_connections` and `check_connection`.
- **Profile Policies:** Added `allowed_databases`, system-schema policy,
  per-profile result/timeout/row/cell limits, pooling, and audit settings.
- **Structured Results:** Added JSON metadata output, standards-compliant CSV,
  stable value serialization, cell truncation, and offset pagination.
- **Private Audit Events:** Query fingerprints omit literals and record only
  connection, timing, status, row counts, and truncation.

### Changed
- SSH tunnels are dynamically allocated, health-checked, reused, restarted after
  failure, and closed on process shutdown.
- Named connection pools are isolated by effective endpoint, database, and
  credential fingerprint.
- Profile files hot-reload and isolate invalid profiles instead of disabling all
  valid environments.
- Resource reads now use the same database allowlist, read-only transaction,
  timeout, pooling, and rollback controls as tools.

### Security
- Added MySQL AST validation for cross-database access and system-schema blocking.
- Added high-risk function blocking for file reads, advisory locks, sleeps,
  benchmarks, and common command-execution UDFs.

## [0.5.0] - 2026-07-29

### Added
- **Named Connections:** Load dev/test/staging/prod profiles from one TOML file and choose `connection` plus `database` independently on every tool call.
- **Discovery Tools:** Added `list_connections`, `list_databases`, and `list_tables`.
- **Result Limits:** Added `MYSQL_MAX_ROWS` with a hard maximum of 1000 rows.

### Security
- **Strict Read-Only SQL:** `execute_sql` now rejects DML, DDL, transaction control, locks, `SELECT INTO`, variable assignment, executable comments, and multiple statements before connecting.
- **Database Enforcement:** All queries use a MySQL read-only transaction with autocommit disabled and unconditional rollback.
- **Read-Only Metadata:** Every MCP tool is marked read-only and non-destructive.
- **Read-Only Tests:** Removed database-writing integration fixtures and added SQL bypass coverage.

### Changed
- Passwords in named profiles can be referenced through environment variables with `password_env`.
- Legacy `MYSQL_*` configuration remains available when no profiles file is configured.

## [0.4.1] - 2026-06-08

### Fixed
- **Package Metadata:** Split author entry into name-only (`Author:`) + maintainer-with-email (`Maintainer-email:`) so sites that read the legacy `Author` field (e.g. pypistats.org) display the author name correctly.

## [0.4.0] - 2026-06-08

### Added
- **Cross-Database Support:** `get_schema_info` and `get_table_sample` now accept `database.table` notation, making their scope consistent with `execute_sql`. Bare table names continue to use `MYSQL_DATABASE`.
- **MCP Prompts:** Two guided workflow prompts usable as slash commands in supporting clients (e.g. Claude Desktop):
  - `explore_database` — walks through resource discovery, schema inspection, data sampling, and summarization.
  - `analyze_table` — schema + sample + query suggestions for a named table.
- **Package Metadata:** Added `Homepage`, `Repository`, `Issues`, and `Changelog` URLs, SPDX license expression, keywords, and classifiers to PyPI metadata.
- **Reproducible Builds:** Committed `uv.lock` so hosted build environments get pinned dependencies.

### Fixed
- **Multi-Statement Error:** `execute_sql` now returns a clear message ("Only single statements are supported…") instead of MySQL's cryptic "Commands out of sync" error when a multi-statement query is passed.

### Changed
- **Tool Descriptions:** All three tools have richer descriptions that say when to use them and what they return. Contributor credits moved out of tool descriptions.
- **Tool Annotations:** `get_schema_info` and `get_table_sample` now carry `readOnlyHint=True` so clients can distinguish them from destructive operations.

## [0.3.1] - 2026-05-31

### Fixed
- **Strict LLM Compatibility:** Refactored resource names to be 'identifier-safe' (e.g., `table_users` instead of `Table: users`) to ensure compatibility with Google Gemini models and GitHub Copilot (Issue #39).
- **MySQL 5.7 Stability:** Added built-in support for `MYSQL_AUTH_PLUGIN`, `MYSQL_USE_PURE`, and `MYSQL_RAISE_ON_WARNINGS` to stabilize connections to older MySQL servers (Issue #31).

### Added
- **Standalone Execution:** Added `__main__.py` to allow running the package directly via `python -m mysql_mcp_server` (Issue #12).

## [0.3.0] - 2026-05-31

### Fixed
- **Asynchronous Reliability:** Refactored all blocking database and SSH operations to use background threads via `anyio.to_thread.run_sync`. This prevents the server from hanging in environments like Windows 11 (Issue #54).
- **Graceful Error Reporting:** Implemented global exception handling in tool calls to return clear, actionable error messages to AI agents and users instead of silent failures (Issue #50).
- **Metadata Formatting:** Improved result set handling for `DESCRIBE`, `SHOW COLUMNS`, and other inspection queries, including explicit `NULL` value rendering (PR #38).
- **SQL Injection Risk:** Added strict regex validation for all database and table identifiers (PR #86).

### Added
- **Multi-Database Mode:** `MYSQL_DATABASE` is now optional. When omitted, the server lists all available databases and supports `USE <database>` or fully qualified table names (PR #86, Issue #68, #81).
- **SSH Tunneling:** Built-in support for secure remote database connections via an SSH jump host using `MYSQL_SSH_ENABLE` (PR #64, contributed by @GeorgeLeex).
- **New Inspection Tools:**
    - `get_schema_info`: Detailed column metadata, types, and comments.
    - `get_table_sample`: Quick data previews to understand table content (PR #64, contributed by @GeorgeLeex).
- **SSE/HTTP Transport:** Support for running as an HTTP server by setting `MCP_TRANSPORT=sse` (PR #86).
- **SSL/TLS Support:** Added `MYSQL_SSL_MODE` for encrypted connections.
- **Environment Management:** Added `.env` support and `.env.example` file (PR #69).

### Security
- Added `ToolAnnotations` to `execute_sql` to flag potentially destructive operations to AI agents (PR #78).
- Dockerfile now runs as a non-root `appuser` and follows best practices for secret management.
- Masked sensitive information (passwords, SSH keys) in server logs.

### Changed
- Refactored server initialization into distinct STDIO and SSE transport handlers.
- Updated minimum `mcp` dependency to `1.2.0` for improved stability and security.

## [0.2.2] - 2025-04-18

### Fixed
- Fixed handling of SQL commands that return result sets, including `SHOW INDEX`, `SHOW CREATE TABLE`, and `DESCRIBE`
- Added improved error handling for result fetching operations
- Added additional debug output to aid in troubleshooting

## [0.2.1] - 2025-02-15

### Added
- Support for MYSQL_PORT configuration through environment variables
- Documentation for PORT configuration in README

### Changed
- Updated tests to use handler functions directly
- Refactored database configuration to runtime

## [0.2.0] - 2025-01-20

### Added
- Initial release with MCP server implementation
- Support for SQL queries through MCP interface
- Ability to list tables and read data
