# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
