[![Tests](https://github.com/12zhangyan/mysql_mcp_server/actions/workflows/test.yml/badge.svg)](https://github.com/12zhangyan/mysql_mcp_server/actions)
[![npm version](https://img.shields.io/npm/v/%40yanzhang123%2Freadonly-db-mcp)](https://www.npmjs.com/package/@yanzhang123/readonly-db-mcp)
[![npm provenance](https://img.shields.io/badge/npm-provenance-blue)](https://www.npmjs.com/package/@yanzhang123/readonly-db-mcp)

# MySQL MCP Server

An enterprise-auditable, strictly read-only Model Context Protocol (MCP) server
for exploring multiple MySQL environments and databases from one process.

The supported npm distribution for this fork is
[`@yanzhang123/readonly-db-mcp`](https://www.npmjs.com/package/@yanzhang123/readonly-db-mcp).
It includes the matching Python wheel and a cross-platform launcher, so MCP
clients do not need a repository checkout or a separate package installation.

> **Note**: MySQL MCP Server supports both standard input/output (STDIO) and Streamable HTTP (SSE) transport modes. The SSE mode is recommended for remote/self-hosted deployments.

> [!IMPORTANT]
> Read-only enforcement does not trust the MySQL account grants. Even if the
> supplied account has `INSERT`, `UPDATE`, `DELETE`, or DDL privileges, the MCP
> SQL gate only accepts reviewed read statement families, runs them in a
> read-only transaction, and always rolls back. A database-level `SELECT`-only
> account is still strongly recommended as independent defense in depth.

## Features
- **Named connection profiles** for dev/test/staging/prod and multiple servers
- Select a `connection` and `database` independently on every tool call
- **Strictly read-only SQL** with fail-closed validation, a MySQL read-only transaction, and unconditional rollback
- Discover connections, databases, tables, schemas, and sample rows
- Return-size protection (`MYSQL_MAX_ROWS`, hard maximum 1000)
- Legacy single-connection `MYSQL_*` configuration remains supported
- **SSE/HTTP transport support** (`MCP_TRANSPORT=sse`)
- Per-profile SSL/TLS and SSH tunneling
- Passwords can stay in environment variables instead of the profiles file

## Installation

### npm / npx

Requirements: Node.js 18+, npm 9+, and Python 3.11+ available on `PATH`.

Run the current release without installing it globally:

```bash
npx -y @yanzhang123/readonly-db-mcp
```

For controlled production rollouts, pin the reviewed version:

```bash
npx -y @yanzhang123/readonly-db-mcp@0.7.3
```

On first use, the launcher creates a versioned virtual environment in the user
cache and installs the bundled wheel plus exact Python dependencies from a
version-controlled SHA-256 lock file. Downloads use the configured pip index
and the completed environment is cached by wheel-and-lock fingerprint. Override
Python with `MYSQL_MCP_PYTHON` or the cache location with
`MYSQL_MCP_NPM_CACHE_DIR`.

For MCP clients, use `npx` as the command and
`["-y", "@yanzhang123/readonly-db-mcp"]` as its arguments.

### Minimal MCP client configuration

Create `mysql-connections.toml` from
[`mysql-connections.example.toml`](mysql-connections.example.toml), keep
passwords in environment variables, and use an absolute path:

```json
{
  "mcpServers": {
    "mysql-readonly": {
      "command": "npx",
      "args": ["-y", "@yanzhang123/readonly-db-mcp@0.7.3"],
      "env": {
        "MYSQL_PROFILES_FILE": "C:/absolute/path/mysql-connections.toml",
        "MYSQL_DEV_PASSWORD": "set-in-the-client-secret-store"
      }
    }
  }
}
```

On Windows clients that do not resolve npm command shims correctly, set
`"command": "npx.cmd"`. Do not commit the populated profile, passwords, audit
HMAC keys, connection strings, or client configuration containing secrets.

After startup, call `validate_connections`, then `check_connection`, before
running discovery queries. `check_connection` reports an irreversible account
fingerprint and privilege classification without returning the username or raw
grant statements.

### Manual Python installation

```bash
pip install mysql-mcp-server
```

The Python package remains available for source-oriented deployments, but the
scoped npm package is the documented distribution path for this fork.

## Configuration

### Recommended: named connections

Copy [`mysql-connections.example.toml`](mysql-connections.example.toml) to an ignored local file named `mysql-connections.toml`, then define all environments in one place:

```toml
default = "dev"

[connections.dev]
description = "Local development"
host = "127.0.0.1"
port = 3306
user = "readonly_user"
password_env = "MYSQL_DEV_PASSWORD"
database = "app_dev"
allowed_databases = ["app_dev", "analytics_dev"]
query_timeout_ms = 30000
max_rows = 500
max_cell_length = 20000
result_format = "json"
mask_columns = ["password", "passwd", "*secret*", "*token*", "*api_key*", "*private_key*", "ssn", "id_card", "phone", "email"]
pool_size = 3
audit_enabled = true

[connections.prod]
description = "Production read replica"
host = "prod-read.example.internal"
user = "readonly_user"
password_env = "MYSQL_PROD_PASSWORD"
database = "app"
allowed_databases = ["app", "reporting"]
allowed_functions = [] # reviewed deterministic UDF/stored-function names only
query_timeout_ms = 15000
max_rows = 200
result_format = "json"
mask_columns = ["password", "passwd", "*secret*", "*token*", "*api_key*", "*private_key*", "ssn", "id_card", "phone", "email"]
pool_size = 5
ssl_mode = "VERIFY_CA"
ssl_ca = "C:/certs/company-ca.pem"

# Enterprise audit controls
audit_enabled = true
audit_log_file = "C:/var/log/mysql-mcp/audit.jsonl"
audit_log_max_bytes = 10000000
audit_log_backup_count = 10
audit_hmac_key_env = "MYSQL_MCP_AUDIT_SIGNING_KEY"
audit_required_context = ["actor", "purpose", "ticket_id"]
audit_fail_closed = true
audit_fsync = true
```

Point the MCP process at that file and supply only the secrets through environment variables:

```bash
MYSQL_PROFILES_FILE=C:/absolute/path/mysql-connections.toml
MYSQL_DEV_PASSWORD=...
MYSQL_PROD_PASSWORD=...
MYSQL_MCP_AUDIT_SIGNING_KEY=... # secret-manager supplied; never commit
MYSQL_DEFAULT_CONNECTION=dev  # optional; overrides `default` in TOML
MYSQL_MAX_ROWS=500             # optional; 1-1000, default 500
MYSQL_QUERY_TIMEOUT_MS=30000   # full-call timeout, 100-300000 ms
MYSQL_MAX_CELL_LENGTH=20000    # truncate oversized values
MYSQL_RESULT_FORMAT=json       # csv or json
MYSQL_MASK_COLUMNS=password,passwd,*secret*,*token*,*api_key*,*private_key*,ssn,id_card,phone,email
MYSQL_POOL_SIZE=0              # legacy mode; named profiles default to 5
MYSQL_ALLOWED_DATABASES=app,reporting
MYSQL_ALLOWED_FUNCTIONS=              # optional reviewed function names
```

`password_env` is recommended. A `password` field is accepted for local-only setups, but `mysql-connections.toml` is gitignored because it may contain secrets. Use an absolute `MYSQL_PROFILES_FILE` path in desktop MCP clients because their working directory is not guaranteed.

Each tool call can now target an environment without restarting the server:

```json
{
  "connection": "prod",
  "database": "app",
  "query": "SELECT COUNT(*) FROM orders",
  "audit_context": {
    "actor": "reporting-service",
    "purpose": "month-end reconciliation",
    "ticket_id": "FIN-2026-042"
  }
}
```

There is deliberately no process-global “switch” tool: explicit per-call selection prevents one concurrent MCP client from silently changing another client's active environment.

The profiles file is hot-reloaded when its size or modification time changes.
One invalid profile is reported by `validate_connections` without disabling other
valid profiles. Password environment variables are resolved only when their
profile is used, so an unavailable production credential does not break local
development.

### Legacy single connection

If `MYSQL_PROFILES_FILE` is not set, the original environment variables still work:

```bash
MYSQL_HOST=localhost     # Database host
MYSQL_PORT=3306         # Optional: Database port (defaults to 3306 if not specified)
MYSQL_USER=readonly_user
MYSQL_PASSWORD=your_password
MYSQL_DATABASE=your_database # Optional: Omit for multi-database mode

# Advanced Configuration
MYSQL_SSL_MODE=REQUIRED  # DISABLED, REQUIRED, VERIFY_CA, VERIFY_IDENTITY
MYSQL_SSL_CA=            # required for VERIFY_CA / VERIFY_IDENTITY
MYSQL_CONNECT_TIMEOUT=10 # Timeout in seconds
MYSQL_QUERY_TIMEOUT_MS=30000
MYSQL_MAX_ROWS=500
MYSQL_MAX_CELL_LENGTH=20000
MYSQL_RESULT_FORMAT=json
MYSQL_POOL_SIZE=0
MYSQL_AUDIT_ENABLED=true
MYSQL_AUDIT_LOG_FILE=C:/var/log/mysql-mcp/audit.jsonl
MYSQL_AUDIT_LOG_MAX_BYTES=10000000
MYSQL_AUDIT_LOG_BACKUP_COUNT=10
MYSQL_AUDIT_HMAC_KEY_ENV=MYSQL_MCP_AUDIT_SIGNING_KEY
MYSQL_MCP_AUDIT_SIGNING_KEY=replace-with-secret-manager-value
MYSQL_AUDIT_REQUIRED_CONTEXT=actor,purpose,ticket_id
MYSQL_AUDIT_FAIL_CLOSED=true
MYSQL_AUDIT_FSYNC=true
MYSQL_ALLOWED_DATABASES=your_database

# Connection behaviour (Optional)
MYSQL_SQL_MODE=TRADITIONAL           # SQL mode applied to the connection (default: TRADITIONAL)

# Compatibility (Optional)
MYSQL_CHARSET=utf8mb4
MYSQL_COLLATION=utf8mb4_unicode_ci
MYSQL_AUTH_PLUGIN=       # e.g., mysql_native_password for older MySQL versions
MYSQL_USE_PURE=false     # Force the pure-Python connector (default: false)
MYSQL_RAISE_ON_WARNINGS=false        # Raise on SQL warnings (default: false)

# SSE Transport (Optional)
MCP_TRANSPORT=stdio      # stdio or sse
MCP_SSE_HOST=127.0.0.1   # Safe default: loopback only
PORT=8000                # HTTP port (fallback for MCP_SSE_PORT)
MCP_SSE_ALLOWED_HOSTS=   # Comma-separated allowed Host headers (default: localhost:{port},127.0.0.1:{port})
MCP_SSE_BEARER_TOKEN=    # Optional; at least 32 characters
# Set only when an authenticated reverse proxy is the sole network entry point:
MCP_SSE_TRUST_PROXY_AUTH=false

# SSH Tunneling (Optional)
MYSQL_SSH_ENABLE=false   # Set to true to enable
MYSQL_SSH_HOST=          # SSH jump host
MYSQL_SSH_PORT=22        # SSH port
MYSQL_SSH_USER=          # SSH username
MYSQL_SSH_KEY_PATH=      # Path to SSH private key
MYSQL_SSH_REMOTE_HOST=localhost # Host from the perspective of the jump host
MYSQL_SSH_REMOTE_PORT=3306
MYSQL_LOCAL_PORT=0       # 0 selects a free port; the tunnel is reused
```

### `.env` file loading

On startup the server automatically loads a `.env` file via `python-dotenv`, so for local use you can simply:

```bash
cp .env.example .env   # then edit with your credentials
```

The file is read from the **process working directory** (and parent directories), which works when you run the server yourself from the project folder.

> ⚠️ **Claude Code / Claude Desktop:** these hosts launch the server from their own working directory, so the project's `.env` will **not** be found and you'll see `Missing required database configuration`. Put your `MYSQL_*` values in the `env` block of the MCP config (shown in the Usage section below) rather than relying on `.env`.

### Multi-database mode

When `MYSQL_DATABASE` is not set, the server operates in multi-database mode:
- `list_resources` returns all user databases (system databases are filtered out)
- Pass `database` to any data tool, or use fully qualified names such as `mydb.mytable`
- `USE` and multiple statements are intentionally blocked

## Available Tools

All tools are declared with `readOnlyHint=true` and `destructiveHint=false`.

### `list_connections`
Lists named profiles, default database, policy limits and readiness. It never returns hosts, usernames, passwords, or SSH key paths.

### `validate_connections`
Forces a configuration reload and reports valid/invalid profiles plus missing
password environment variables without opening database connections.

### `check_connection`
Runs `SELECT`/`SHOW GRANTS` health checks and returns MySQL version, current
database, an irreversible account fingerprint, global read-only state, grant
count, latency and active profile policy. Raw usernames and grant statements are
not returned. It classifies non-read privileges and warns when the account does
not provide read-only defense in depth.

### `list_databases`
Lists accessible non-system databases.
- **Arguments:** `connection` (optional)

### `list_tables`
Lists tables and views in a database.
- **Arguments:** `connection`, `database` (both optional)

### `execute_sql`
Executes exactly one read-only statement.
- **Arguments:** `query` (required); `connection`, `database`, `max_rows`, `offset`, `timeout_ms`, `result_format`, `audit_context` (optional unless required by the profile)
- **Allowed statement families:** `SELECT`, `WITH`, `SHOW`, `DESCRIBE`, `DESC`, `EXPLAIN`, `TABLE`
- **Always blocked:** DML, DDL, `USE`, transaction control, locks, `SELECT ... INTO`, session-variable assignment, MySQL executable comments, and multiple statements
- **Function policy:** unresolved stored functions/UDFs are blocked by default because `SELECT function(...)` can hide database or external side effects. `allowed_functions` is an explicit reviewed-function exception.
- **Defense in depth:** validation occurs before connecting; MySQL then executes inside `START TRANSACTION READ ONLY`. Completed calls roll back; truncated/cancelled calls close the socket so MySQL rolls back and stops producing unread rows.
- **Cross-database:** use the `database` argument or a qualified `database.table` name
- **Timeout/cancellation:** profile timeout, Connector socket timeout and MySQL/MariaDB statement timeout are combined. MCP cancellation closes the active socket; no `KILL` privilege is required.
- **Pagination:** JSON output includes `truncated` and `next_offset`; pass that value back as `offset`.
- **Formats:** `json` preserves structured rows and metadata; `csv` uses RFC-style quoting and explicit `NULL`.
- **Attribution:** `audit_context` supports `actor`, `purpose`, and `ticket_id`. A profile can require any of these fields before a connection is opened.

### `get_schema_info`
Provides detailed metadata about database structures.
- **Arguments:** `table_name`, `connection`, `database` (all optional)
- **Output:** Column names, types, nullability, default values, and comments.
- **Cross-database:** Pass `database.table` to query a table outside `MYSQL_DATABASE`; bare names use the configured database.
- **Identifier rules:** Names must contain only alphanumeric characters, underscores, and `$` (dots are allowed as a separator between database and table names).

### `get_table_sample`
Fetches a representative sample of data.
- **Arguments:** `table_name` (required), `limit` (optional integer, max 100), `offset`, `connection`, `database`, `timeout_ms`, `result_format`
- **Use Case:** Quickly understand data formats and content without fetching large result sets.
- **Cross-database:** Pass `database.table` to sample a table outside `MYSQL_DATABASE`; bare names use the configured database.
- **Identifier rules:** Names must contain only alphanumeric characters, underscores, and `$` (dots are allowed as a separator between database and table names).

## Available Prompts

In addition to tools, the server exposes **MCP prompts** — guided, multi-step workflows that a client can launch on demand. In Claude Code they appear as slash commands (`/mcp__<server>__<prompt>`); in Claude Desktop they appear in the prompts (`+`) menu.

| Prompt | Arguments | Description |
| --- | --- | --- |
| `explore_database` | `connection`, `database` *(optional)* | Systematically explore the selected database: discover tables, inspect schemas, sample data, and summarize it. |
| `analyze_table` | `table_name` *(required)*; `connection`, `database` *(optional)* | Deep-dive into a specific table. Accepts `database.table` notation. |

**Example (Claude Code):**
```
/mcp__mysql__explore_database
/mcp__mysql__analyze_table customers
```

Both prompts orchestrate the read-only discovery, schema, and sampling tools.

## Usage
### With Claude Desktop
Add this to your `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "mysql-readonly": {
      "command": "npx",
      "args": ["-y", "@yanzhang123/readonly-db-mcp@0.7.3"],
      "env": {
        "MYSQL_PROFILES_FILE": "C:/absolute/path/mysql-connections.toml",
        "MYSQL_DEV_PASSWORD": "your_dev_password",
        "MYSQL_PROD_PASSWORD": "your_prod_password"
      }
    }
  }
}
```

For more detailed examples and agent-specific guidance, see [MCP_USECASES.md](MCP_USECASES.md).

### With Visual Studio Code
Add this to your `mcp.json`:
```json
{
  "mcpServers": {
    "mysql-readonly": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@yanzhang123/readonly-db-mcp@0.7.3"],
      "env": {
        "MYSQL_PROFILES_FILE": "C:/absolute/path/mysql-connections.toml",
        "MYSQL_DEV_PASSWORD": "your_dev_password",
        "MYSQL_PROD_PASSWORD": "your_prod_password"
      }
    }
  }
}
```

Use `npx.cmd` instead of `npx` if required by your Windows MCP host.

### Debugging with MCP Inspector
While MySQL MCP Server isn't intended to be run standalone or directly from the command line with Python, you can use the MCP Inspector to debug it.

The MCP Inspector provides a convenient way to test and debug your MCP implementation:

```bash
# Install dependencies
pip install -r requirements.txt
# Use the MCP Inspector for debugging (do not run directly with Python)
```

The MySQL MCP Server is designed to be integrated with AI applications like Claude Desktop and should not be run directly as a standalone Python program.

## Development
```bash
# Clone the repository
git clone https://github.com/12zhangyan/mysql_mcp_server.git
cd mysql_mcp_server
# Create virtual environment
python -m venv venv
# Linux/macOS: source venv/bin/activate
# Windows PowerShell: .\venv\Scripts\Activate.ps1
# Install development dependencies
pip install -r requirements-dev.txt
# Copy the example config and edit with your credentials
# Linux/macOS: cp .env.example .env
# Windows PowerShell: Copy-Item .env.example .env
# Edit .env with your MySQL connection details
# Run tests
pytest
```

## Security Considerations
- **Read-only SQL gate:** Only result-producing read statements are accepted; write/DDL/transaction/locking constructs are rejected before a connection is opened.
- **Database enforcement:** Every exposed query runs with `autocommit=false` in a read-only transaction. Normal completion rolls back; cancellation or truncation closes the socket and forces server-side rollback.
- **Write-capable credentials:** Read-only behavior does not depend on account grants. Unrecognized stored functions/UDFs, side-effecting functions, sequence advancement and locking reads are rejected because they can hide effects inside `SELECT`.
- **Database allowlist:** `allowed_databases` is enforced with a MySQL AST parser for tools and resource URIs; system schemas are blocked by default.
- **Resource limits:** Calls have full-operation, socket and server statement timeouts; at most 1000 rows are returned and oversized cells are truncated.
- **Cancellation:** Cancelling the MCP request closes the active connector socket. The worker does not continue silently after the response is abandoned.
- **Enterprise audit:** Versioned UTC JSONL events include an event ID, MCP request ID, operation, caller-supplied attribution, policy decision, target database, literal-free query fingerprint, duration, result size and outcome. SQL text and result data are never logged. Optional rotation, fsync, HMAC signatures, required context and fail-closed behavior are supported.
- **Two-phase audit:** In fail-closed mode, an fsynced `started` event is persisted before opening a database connection, followed by the terminal outcome event.
- **Result masking:** Sensitive output names are redacted by default. Queries whose expression tree references a sensitive source column are conservatively redacted even when aliases or CTEs hide the original name. Review `mask_columns` for each schema; setting it to an empty list explicitly disables masking.
- **Encrypted by default:** Database TLS defaults to `REQUIRED` and the connected session is rejected if the connector reports plaintext. Production profiles should use `VERIFY_CA` or `VERIFY_IDENTITY` with `ssl_ca`.
- **Parser hardening:** MySQL `/*!...*/` and MariaDB `/*M!...*/` executable comments are rejected, and server-wide `SHOW` variants are blocked unless they can be scoped to an allowed database.
- **Identifier Validation:** Table and database names passed to `get_schema_info` and `get_table_sample` are validated against a strict whitelist (alphanumeric, underscore, and `$` only; a single dot is allowed as a `database.table` separator). Other special characters are rejected to prevent SQL injection.
- **Encrypted Access:** Full support for SSL/TLS and SSH Tunneling for secure remote connections.
- **Log Privacy:** SQL text, passwords, hosts, usernames and SSH private-key paths are not included in tool discovery or audit events.
- **Diagnostic Privacy:** Connection diagnostics expose an account fingerprint and privilege summary, not raw usernames or `SHOW GRANTS`; configuration paths and database-supplied error text are redacted.
- **Least Privilege:** A dedicated `SELECT` account is recommended as additional defense, but it is not assumed by the MCP read-only enforcement. `check_connection` warns about broader grants without disabling the profile.
- **SSE is loopback-only by default.** Configure `MCP_SSE_BEARER_TOKEN` with at least 32 characters to enable built-in bearer authentication. A non-loopback bind is refused unless bearer authentication is enabled or `MCP_SSE_TRUST_PROXY_AUTH=true` explicitly confirms that an authenticated reverse proxy is the only network entry point. Example with nginx and HTTP Basic Auth:

  ```nginx
  location /sse {
      auth_basic "MCP";
      auth_basic_user_file /etc/nginx/.htpasswd;
      proxy_pass http://127.0.0.1:8000;
      proxy_set_header Host $host;
      proxy_buffering off;
  }
  location /messages/ {
      auth_basic "MCP";
      auth_basic_user_file /etc/nginx/.htpasswd;
      proxy_pass http://127.0.0.1:8000;
      proxy_set_header Host $host;
  }
  ```

  Keep `MCP_SSE_HOST=127.0.0.1` so the proxy is the sole public entry point. If a container requires `0.0.0.0`, set `MCP_SSE_TRUST_PROXY_AUTH=true` only after network policy prevents direct access. Set `MCP_SSE_ALLOWED_HOSTS` to the hostname forwarded by the proxy.

See [SECURITY.md](SECURITY.md) and [ENTERPRISE_DEPLOYMENT.md](ENTERPRISE_DEPLOYMENT.md) for deployment and audit guidance.
Maintainers should follow [RELEASING.md](RELEASING.md) for synchronized Python/npm version releases.

## Security Best Practices
This MCP implementation requires database access to function. For security:
1. **Prefer a dedicated MySQL user** with `SELECT` only as defense in depth; MCP enforcement remains read-only even when broader credentials are supplied
2. **Never use root credentials** or administrative accounts
3. **Restrict `allowed_databases`** to the schemas each profile needs
4. **Require audit attribution** and protect the optional HMAC key in a secret manager
5. **Review masking patterns, audit events, dependency updates, and account grants regularly**

See [MySQL Security Configuration Guide](https://github.com/12zhangyan/mysql_mcp_server/blob/main/SECURITY.md) for detailed instructions on:
- Creating a restricted MySQL user
- Setting appropriate permissions
- Monitoring database access
- Security best practices

⚠️ IMPORTANT: Always follow the principle of least privilege when configuring database access.

## License
MIT License - see LICENSE file for details.

## Contributing
1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request
