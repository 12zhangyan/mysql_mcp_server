# MySQL MCP Server: Common Use Cases & Examples

This server is intentionally read-only. Every data tool accepts an optional
`connection` profile and most accept an optional `database`.

## 1. Choose an environment

Start with `list_connections({})`. It returns profile names, the default profile,
each default database, and descriptions without exposing credentials.

If anything is unavailable, call `validate_connections({})`; this reloads the
TOML file and reports profile-local configuration errors. Use
`check_connection({"connection":"test"})` for a real read-only connectivity,
server-version, latency and grants check.

Examples:

- Development: `{"connection": "dev"}`
- Test database override: `{"connection": "test", "database": "orders_test"}`
- Production read replica: `{"connection": "prod"}`

Connection selection is explicit per call so concurrent clients cannot change a
shared global environment.

## 2. Discover databases and tables

- `list_databases({"connection": "test"})`
- `list_tables({"connection": "test", "database": "orders_test"})`

When multiple profiles exist, `list_resources` exposes one resource per connection.
Reading it lists the accessible non-system databases.

## 3. Explore schemas and samples

- All columns in a database:
  `get_schema_info({"connection": "test", "database": "orders_test"})`
- One table:
  `get_schema_info({"connection": "test", "database": "orders_test", "table_name": "orders"})`
- A small sample:
  `get_table_sample({"connection": "test", "database": "orders_test", "table_name": "orders", "limit": 10})`

`table_name` can also be qualified as `database.table`.

## 4. Run read-only analysis

`execute_sql` accepts one `SELECT`, `WITH`, `SHOW`, `DESCRIBE`, `DESC`, `EXPLAIN`,
or `TABLE` statement.

- Count:
  `execute_sql({"connection": "prod", "query": "SELECT COUNT(*) FROM orders"})`
- Join:
  `execute_sql({"connection": "test", "database": "orders_test", "query": "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id LIMIT 10"})`
- Inspect:
  `execute_sql({"connection": "dev", "query": "SHOW CREATE TABLE users"})`
- JSON page:
  `execute_sql({"connection": "prod", "query": "SELECT * FROM orders ORDER BY id", "max_rows": 200, "offset": 0, "result_format": "json", "timeout_ms": 10000})`

When JSON output says `"truncated":true`, pass its `next_offset` value into the
next call. Use an explicit `ORDER BY` when stable pagination matters.

Writes, DDL, `USE`, locks, transaction control, `SELECT INTO`, session-variable
assignment, executable comments, and multiple statements are rejected.
Queries that reference a schema outside the profile's `allowed_databases` are
also rejected, including through MCP resource URIs.

## 5. Attribute an enterprise query

Profiles can require audit attribution. Supply the same context to every
database tool in one workflow:

```json
{
  "connection": "prod",
  "database": "orders",
  "query": "SELECT COUNT(*) FROM orders",
  "audit_context": {
    "actor": "finance-reporting",
    "purpose": "month-end reconciliation",
    "ticket_id": "FIN-2026-042"
  }
}
```

The event records the MCP request/operation, attribution, database, policy
decision, query fingerprint, timing and outcome. It does not record SQL text or
result values. Rejected write or cross-database attempts are also audited before
the connector is opened.

## Security note

The server validates SQL before connecting, blocks unknown stored functions/UDFs,
executes inside a MySQL read-only transaction, limits returned rows, and rolls
back or closes the socket. These controls remain active for write-capable
accounts. A dedicated `SELECT` account is recommended as an additional
containment layer when one is available.
