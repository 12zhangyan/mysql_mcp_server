# Enterprise Deployment and Audit Guide

This guide describes a defense-in-depth deployment of MySQL MCP Server. The MCP
enforces its read-only contract even when the supplied database account has
write privileges. Database least privilege remains a recommended independent
containment layer rather than a correctness prerequisite.

## Required Security Baseline

1. Run the MCP as a dedicated operating-system identity.
2. When organizational controls permit it, use one dedicated MySQL account per
   environment with only `SELECT` and `SHOW VIEW`. Broader credentials remain
   supported and are reported as a defense-in-depth warning.
3. Configure `allowed_databases` for every profile.
4. Require verified TLS (`VERIFY_CA` or `VERIFY_IDENTITY`) or an SSH tunnel.
5. Store database passwords, bearer tokens and audit signing keys in a secret
   manager and expose them only as process environment variables.
6. Enable durable, signed, fail-closed audit logging for production profiles.
7. Keep SSE on loopback behind an authenticated gateway, or configure the
   built-in bearer token.
8. Forward JSONL audit events to an append-only SIEM and alert on denials,
   timeouts, repeated errors and privilege-assessment warnings.

## Database Account

The MCP does not require a read-only account. Its SQL gate, AST policy, unknown
function/UDF rejection and MySQL read-only transaction are applied regardless
of grants. If a least-privilege account is available, the following is suggested
DDL for a DBA to review and execute. The MCP server does not execute privilege
changes:

```sql
CREATE USER 'mcp_reader'@'approved_source'
  IDENTIFIED BY 'secret-manager-generated-password'
  REQUIRE SSL;

GRANT SELECT, SHOW VIEW ON `application_database`.*
  TO 'mcp_reader'@'approved_source';
```

Prefer not to grant `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `ALTER`, `DROP`, `FILE`,
`PROCESS`, `EXECUTE`, `CREATE ROUTINE`, `EVENT`, `TRIGGER`, `LOCK TABLES` or
global privileges. `check_connection` returns a `read_only_assessment`, reports
any non-read privileges it finds, and continues operating in strict read-only
mode.

## Production Profile

```toml
[connections.prod]
host = "prod-read.example.internal"
port = 3306
user = "mcp_reader"
password_env = "MYSQL_PROD_PASSWORD"
database = "application_database"
allowed_databases = ["application_database"]
# Unrecognized stored functions/UDFs are blocked. This list should normally
# remain empty. Add only reviewed deterministic functions with no side effects.
allowed_functions = []

ssl_mode = "VERIFY_IDENTITY"
ssl_ca = "/etc/mysql-mcp/company-ca.pem"
query_timeout_ms = 15000
max_rows = 200
max_cell_length = 20000
pool_size = 5
result_format = "json"

audit_enabled = true
audit_log_file = "/var/log/mysql-mcp/audit.jsonl"
audit_log_max_bytes = 10000000
audit_log_backup_count = 20
audit_hmac_key_env = "MYSQL_MCP_AUDIT_SIGNING_KEY"
audit_required_context = ["actor", "purpose", "ticket_id"]
audit_fail_closed = true
audit_fsync = true
```

All database tools accept:

```json
{
  "audit_context": {
    "actor": "reporting-service",
    "purpose": "monthly reconciliation",
    "ticket_id": "FIN-2026-042"
  }
}
```

The server rejects a request missing a profile-required field before opening a
database connection. Context values have length and control-character checks.
Because MCP resource reads cannot carry tool arguments, profiles with required
audit context must be accessed through the database tools; resource reads are
intentionally denied instead of bypassing attribution policy.

## Write-Capable Credential Model

The account privilege assessment is advisory and never weakens query policy.
With a write-capable or administrative account:

- DML, DDL, transaction control, assignment, multiple statements, file output,
  system-schema access and locking reads are still rejected before connection.
- Every connector session uses `autocommit=false`, sets the transaction
  read-only and starts `START TRANSACTION READ ONLY`.
- Completion always rolls back; cancellation or truncated results close the
  socket so the server rolls back.
- Unknown stored functions and native UDFs are rejected because a function call
  inside `SELECT` may write data or invoke external side effects.
- Known lock, wait, file, sequence and session-mutating functions remain blocked.

`allowed_functions` is an explicit escape hatch for a reviewed deterministic
function. Adding a function moves that function into the organization's trusted
computing boundary; do not allow a name whose implementation can be changed by
the same write-capable account.

## Audit Event

Each line is canonical JSON containing:

- `schema_version`, `timestamp`, `event_id`, `request_id`
- `operation`, optional MCP client metadata and supplied audit context
- `connection`, `database`, `query_type`, `query_id`
- `policy`, `internal`, `read_only_enforced`
- `status`, `duration_ms`, `row_count`, `truncated`, optional `error_type`
- optional `signature_algorithm` and `signature`

The query ID is a non-reversible fingerprint with literals removed. SQL text,
result rows, credentials, hosts, usernames and private-key paths are excluded.
Denied SQL receives an audit event even though it never reaches MySQL.

When HMAC signing is enabled, verify a line by removing `signature`, serializing
the remaining object as UTF-8 JSON with sorted keys and compact separators, and
computing HMAC-SHA256 with the signing key. Rotation protects disk usage; an
external append-only log service is still required to detect deletion of whole
files.

`actor`, `purpose`, `ticket_id` and MCP client metadata are caller assertions.
For strong human identity, an authenticated gateway must bind its verified
principal to the MCP request and protect access to the MCP endpoint.

## SSE Deployment

The safe default is:

```dotenv
MCP_TRANSPORT=sse
MCP_SSE_HOST=127.0.0.1
MCP_SSE_PORT=8000
```

For built-in bearer authentication:

```dotenv
MCP_SSE_BEARER_TOKEN=<secret-manager-value-of-at-least-32-characters>
```

The token protects both `/sse` and `/messages/`; `/` remains a non-sensitive
health endpoint. For a reverse proxy, keep the MCP on loopback. A container that
must bind `0.0.0.0` may set `MCP_SSE_TRUST_PROXY_AUTH=true` only when network
policy prevents direct access to the container port.

## Monitoring

Recommended alerts:

- any `policy=denied`
- any `status` of `timeout`, `cancelled`, or `error`
- an audit sink/signing failure
- `check_connection.read_only_assessment.account_select_only=false`
- repeated queries approaching the timeout or row cap
- unexpected profile, database, client, actor or ticket values

Retain audit logs according to organizational and regulatory policy. Restrict
file permissions to the MCP service account and log collector; keep HMAC keys
outside the log host when possible.

## Acceptance Evidence

The 0.7.0 implementation was tested against a real external MySQL
`8.0.46-0ubuntu0.22.04.3` instance whose account had non-read privileges. Only
read-only operations were sent:

- configuration validation, connection health and grants inspection
- allowed-database discovery and system-schema filtering
- table/schema metadata reads
- normal query execution and pagination
- pre-connection rejection of DML, DDL, multiple statements, locking reads and
  system-schema access
- durable JSONL events, required context, HMAC signatures and denial auditing
- absence of SQL text, database host and credentials from audit records
- encrypted transport verification (`TLS_AES_128_GCM_SHA256` on the tested
  connection)

No write or DDL statement was sent to the database during acceptance testing.
Timeout/cancellation socket termination remains covered by deterministic
automated tests rather than an intentionally expensive production query.
