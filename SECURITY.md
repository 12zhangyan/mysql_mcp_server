## MySQL Security Configuration

### Creating a Restricted MySQL User

It's crucial to create a dedicated MySQL user with minimal permissions for the MCP server. Never use the root account or a user with full administrative privileges.

#### 1. Create a new MySQL user

```sql
-- Connect as root or administrator
CREATE USER 'mcp_user'@'localhost' IDENTIFIED BY 'your_secure_password';
```

#### 2. Grant minimal required permissions

Basic read-only access (recommended for exploration and analysis):
```sql
-- Grant SELECT permission only
GRANT SELECT ON your_database.* TO 'mcp_user'@'localhost';
```

Do not grant `INSERT`, `UPDATE`, `DELETE`, DDL, routine execution, file, lock, or
temporary-table privileges to the MCP account. The server rejects those operations,
and MySQL permissions provide the final security boundary.

#### 3. Apply the permissions
```sql
FLUSH PRIVILEGES;
```

### Additional Security Measures

1. **Network Access**
   - Restrict the user to connecting only from localhost if the MCP server runs on the same machine
   - If remote access is needed, specify exact IP addresses rather than using wildcards

2. **Query Restrictions**
   - Consider using VIEWs to further restrict data access
   - Set an appropriate `max_queries_per_hour` limit:
   ```sql
   ALTER USER 'mcp_user'@'localhost' 
   WITH MAX_QUERIES_PER_HOUR 1000;
   ```

3. **Data Access Control**
   - Grant access only to specific tables when possible
   - Use column-level permissions for sensitive data:
   ```sql
   GRANT SELECT (public_column1, public_column2) 
   ON your_database.sensitive_table TO 'mcp_user'@'localhost';
   ```

4. **Regular Auditing**
   - Enable MySQL audit logging for the MCP user
   - Regularly review logs for unusual patterns
   - Periodically review and adjust permissions

### Environment Configuration

When setting up the MCP server, use these restricted credentials in your environment:

```bash
MYSQL_USER=mcp_user
MYSQL_PASSWORD=your_secure_password
MYSQL_DATABASE=your_database
MYSQL_HOST=localhost
```

### Monitoring Usage

To monitor the MCP user's database usage:

```sql
-- Check current connections
SELECT * FROM information_schema.PROCESSLIST 
WHERE user = 'mcp_user';

-- View user privileges
SHOW GRANTS FOR 'mcp_user'@'localhost';

-- Check resource limits
SELECT * FROM mysql.user 
WHERE user = 'mcp_user' AND host = 'localhost';
```

### Best Practices

1. **Regular Password Rotation**
   - Change the MCP user's password periodically
   - Use strong, randomly generated passwords
   - Update application configurations after password changes

2. **Permission Review**
   - Regularly audit granted permissions
   - Remove unnecessary privileges
   - Keep permissions as restrictive as possible

3. **Access Patterns**
   - Monitor query patterns for potential issues
   - Set up alerts for unusual activity
   - Maintain detailed logs of database access

4. **Data Protection**
   - Consider encrypting sensitive columns
   - Use SSL/TLS for database connections (see **SSL/TLS Support** below)
   - Implement data masking where appropriate

### Secure Remote Access (SSH Tunneling)

If your MySQL server is not on the same network as the MCP server, **do not expose MySQL directly to the internet.** Instead, use the built-in SSH tunneling support:

1. Enable SSH tunneling by setting `MYSQL_SSH_ENABLE=true`.
2. Configure your SSH jump host credentials and private key path.
3. The MCP server will establish a secure encrypted tunnel and connect to MySQL over `localhost`, keeping your database port closed to the outside world.

### SSL/TLS Support

For production environments, always encrypt the connection between the MCP server and MySQL.

Use the `MYSQL_SSL_MODE` environment variable to control encryption:
- `DISABLED`: Allows plaintext only when explicitly selected; do not use in production.
- `REQUIRED`: Ensures the connection is encrypted.
- `VERIFY_CA`: Encrypts and verifies the server's certificate against a CA.
- `VERIFY_IDENTITY`: Encrypts and verifies that the server's hostname matches the certificate.

`REQUIRED` is the default, and the MCP verifies after connection that the
connector reports an encrypted session. Specify the CA certificate path using
`MYSQL_SSL_CA` when using either verification mode.

### SQL Injection Protection

The MCP server includes built-in protection against SQL injection for administrative operations:
- All database and table identifiers provided via resources or tool arguments are strictly validated against a regex whitelist (`^[a-zA-Z0-9_$]+$`).
- Identifiers are automatically quoted with backticks in internal queries.
- `execute_sql` uses a lexer-style guard that ignores ordinary strings and comments but rejects MySQL `/*!...*/` and MariaDB `/*M!...*/` executable comments, multiple statements, DML, DDL, locks, transactions, `SELECT INTO`, and session assignment.
- Stored functions and UDFs are fail-closed unless sqlglot recognizes them as a
  built-in or their reviewed name is explicitly listed in `allowed_functions`.
  Sequence advancement, lock functions and other known side-effecting functions
  remain blocked even inside `SELECT`.
- All exposed SQL then runs inside a MySQL read-only transaction. Normal completion rolls back; cancellation or result truncation closes the socket, forcing rollback and stopping unread-row production.
- Every MCP tool is advertised as read-only and non-destructive.
- Named profiles can define `allowed_databases`; SQL table/function qualifiers and resource URIs are checked against it with a MySQL AST parser.
- User-supplied `SHOW` statements must be safely scoped to an allowed database; server-wide account, process, engine, binary-log, status, and variable inspection is blocked.
- System schemas are blocked for user SQL unless `allow_system_databases=true` is explicitly configured.
- Full-call, connector socket, and MySQL/MariaDB statement timeouts limit resource exhaustion. MCP cancellation actively closes the connector socket.
- Audit logs never contain SQL text, result data, credentials, hosts, usernames,
  or SSH key paths. They contain a literal-free fingerprint plus the policy and
  execution metadata needed for investigation.
- Result cells whose output or source columns match `mask_columns` are replaced
  with `[REDACTED]`. Source-column detection traverses aliases, expressions and
  CTEs; review the patterns for your schema because name-based masking cannot
  replace database views or column-level permissions.

These controls enforce the MCP read-only contract even when the supplied MySQL
account has write privileges. A `SELECT`-only account is still recommended as
an independent containment layer, and `check_connection` reports broader grants.

### Enterprise Audit Controls

For durable audit records, set an absolute `audit_log_file` on each named
profile. The server writes one canonical JSON object per line and rotates it by
size. Events include:

- schema version, UTC timestamp, unique event ID and MCP request ID
- MCP operation and client name/version when exposed by the SDK
- optional `actor`, `purpose`, and `ticket_id`
- connection profile, target database, query type and literal-free fingerprint
- allowed/denied policy decision, internal/user-query flag and read-only
  enforcement marker
- success, denial, error, timeout or cancellation outcome, duration, row count,
  truncation and error type

Set `audit_required_context = ["actor", "purpose", "ticket_id"]` to reject
queries missing attribution before connecting. Set `audit_hmac_key_env` to the
name of a secret-manager supplied environment variable to sign every event with
HMAC-SHA256. `audit_fail_closed=true` requires a durable audit file and makes
audit sink/signing failures fail the MCP call. `audit_fsync=true` trades
throughput for stronger local durability.

Fail-closed query execution uses two-phase audit recording: an fsynced
`status=started` event must be durable before a database connection is opened,
then a terminal success, denial, error, timeout, or cancellation event is
written. Monitoring should alert on started events without a matching terminal
event, which can indicate process termination or an unavailable audit sink.

The audit context and MCP client metadata are caller assertions. For
cryptographically strong human attribution, terminate the remote transport at
an authenticated gateway, map the authenticated principal into request
metadata, and forward the JSONL stream to an append-only SIEM or log service.
Restrict audit file and HMAC-key access at the operating-system level.

Rejected DML, DDL, multi-statement, locking and cross-database attempts are
audited without sending them to MySQL and without recording their SQL text.

Connection diagnostics do not return raw authenticated usernames, grant
statements, profile-file paths, or database-supplied error messages. They expose
an irreversible account fingerprint, grant count, privilege classification,
numeric MySQL error references and policy metadata instead.

### Remote Transport

SSE binds to `127.0.0.1` by default. A non-loopback bind is rejected unless
`MCP_SSE_BEARER_TOKEN` contains at least 32 characters or
`MCP_SSE_TRUST_PROXY_AUTH=true` explicitly declares that an authenticated
reverse proxy is the only entry point. DNS-rebinding Host validation remains
enabled. Never set the proxy-trust flag if clients can reach the MCP port
directly.

### SSH and Connection Reuse

Named SSH tunnels are started once per profile, use a dynamic local port by
default, are health-checked before reuse, restart after failure, and terminate at
server shutdown. Connection pools are isolated by profile, endpoint, database,
and credential fingerprint. Set `pool_size=0` to disable pooling.
