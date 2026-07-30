import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from typing import Optional, Tuple

import anyio
from mysql.connector import connect, Error
from mcp.server import Server
from mcp.types import (
    Resource,
    Tool,
    TextContent,
    ToolAnnotations,
    ResourceTemplate,
    Prompt,
    PromptArgument,
    PromptMessage,
    GetPromptResult,
)
from pydantic import AnyUrl
from dotenv import load_dotenv

from .audit import (
    AuditWriteError,
    audit_sink,
    build_audit_context,
    current_audit_context,
    reset_audit_context,
    set_audit_context,
    validate_required_audit_context,
)
from .config import (
    ConnectionProfile,
    build_connector_config,
    ensure_database_allowed,
    load_connection_registry,
)
from .results import QueryResult, serialize_value
from .runtime import (
    QueryControl,
    close_runtime_resources,
    connection_pool_manager,
    ssh_tunnel_manager,
)
from .sql_guard import (
    query_fingerprint,
    query_type,
    validate_database_access,
    validate_function_safety,
    validate_read_only_query,
)

# Load environment variables from .env file if it exists.
# This allows for easy local configuration of database and SSH credentials.
load_dotenv()

# Configure logging to provide visibility into server operations.
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mysql_mcp_server")
audit_logger = logging.getLogger("mysql_mcp_server.audit")

# System databases that are typically filtered out from resource listings.
SYSTEM_DATABASES = {"information_schema", "mysql", "performance_schema", "sys"}


def validate_identifier(name: str) -> str:
    """
    Validate a MySQL identifier (table or database name) to prevent SQL injection.
    Only allows alphanumeric characters, underscores, and dollar signs.
    """
    if not re.match(r"^[a-zA-Z0-9_$]+$", name):
        raise ValueError(
            f"Invalid identifier '{name}': only alphanumeric, underscore, and $ are allowed"
        )
    return name


def parse_table_arg(name: str) -> Tuple[Optional[str], str]:
    """Split an optional 'database.table' argument into (db, table) parts, validating each."""
    if "." in name:
        db, tbl = name.split(".", 1)
        return validate_identifier(db), validate_identifier(tbl)
    return None, validate_identifier(name)


@contextmanager
def maybe_ssh_tunnel(connection: str | None = None):
    """Yield a reusable direct or SSH-tunneled endpoint."""
    profile = load_connection_registry().get(connection)
    endpoint = ssh_tunnel_manager.endpoint(profile)
    yield endpoint.host, endpoint.port


def get_db_config(
    host=None,
    port=None,
    connection: str | None = None,
    database: str | None = None,
    query_timeout_ms: int | None = None,
):
    """
    Construct mysql-connector settings for a named profile.

    When MYSQL_PROFILES_FILE is not configured, this keeps compatibility with the
    original MYSQL_* environment variables.
    """
    if database:
        validate_identifier(database)
    profile = load_connection_registry().get(connection)
    return build_connector_config(
        profile,
        database=database,
        host=host,
        port=port,
        query_timeout_ms=query_timeout_ms,
    )


def _start_read_only_transaction(cursor) -> None:
    """Ask MySQL to enforce read-only mode before executing any exposed query."""
    cursor.execute("SET SESSION TRANSACTION READ ONLY")
    cursor.execute("START TRANSACTION READ ONLY")


def _apply_server_query_timeout(cursor, timeout_ms: int) -> str:
    """Set a session-only statement timeout with MySQL/MariaDB compatibility."""
    try:
        cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {timeout_ms}")
        return "mysql"
    except Error as mysql_error:
        if getattr(mysql_error, "errno", None) not in {1064, 1193}:
            raise
        try:
            cursor.execute(f"SET SESSION max_statement_time = {timeout_ms / 1000:.3f}")
            return "mariadb"
        except Error as maria_error:
            if getattr(maria_error, "errno", None) not in {1064, 1193}:
                raise
            logger.warning(
                "Server-side query timeout is unavailable; connector socket "
                "timeouts remain active"
            )
            return "connector"


def _open_connection(
    profile: ConnectionProfile,
    *,
    database: str | None,
    query_timeout_ms: int,
):
    endpoint = ssh_tunnel_manager.endpoint(profile)
    config = build_connector_config(
        profile,
        database=database,
        host=endpoint.host,
        port=endpoint.port,
        query_timeout_ms=query_timeout_ms,
    )
    connection = connection_pool_manager.get_connection(
        profile,
        endpoint,
        config,
        connect_factory=connect,
    )
    return connection, config


# Create the MCP Server instance.
app = Server("mysql_mcp_server")


def _database_listing_query(profile: ConnectionProfile) -> str:
    query = (
        "SELECT SCHEMA_NAME AS database_name "
        "FROM information_schema.SCHEMATA "
        "WHERE SCHEMA_NAME NOT IN "
        "('information_schema','mysql','performance_schema','sys')"
    )
    if profile.allowed_databases:
        allowed = ",".join(f"'{name}'" for name in profile.allowed_databases)
        query += f" AND SCHEMA_NAME IN ({allowed})"
    return query + " ORDER BY SCHEMA_NAME"


def _sync_resource_query(
    profile: ConnectionProfile,
    query: str,
    *,
    database: str | None = None,
    max_rows: int = 1000,
) -> tuple[list[str], list[tuple]]:
    started = time.monotonic()
    selected_database = database or profile.database
    policy_allowed = False
    connection_object = None
    try:
        ready, readiness = profile.runtime_status()
        if not ready:
            raise ValueError(readiness)
        validate_required_audit_context(profile, current_audit_context())
        query = validate_read_only_query(query)
        selected_database = ensure_database_allowed(profile, database)
        if selected_database in SYSTEM_DATABASES and not profile.allow_system_databases:
            raise ValueError(
                f"System database '{selected_database}' is blocked for connection "
                f"'{profile.name}'"
            )
        validate_database_access(
            query,
            selected_database=selected_database,
            allowed_databases=profile.allowed_databases,
            allow_system_databases=profile.allow_system_databases,
            internal=True,
        )
        validate_function_safety(query, allowed_functions=profile.allowed_functions)
        audit_sink.preflight(profile)
        policy_allowed = True
        connection_object, _ = _open_connection(
            profile,
            database=selected_database,
            query_timeout_ms=profile.query_timeout_ms,
        )
        with connection_object.cursor() as cursor:
            _apply_server_query_timeout(cursor, profile.query_timeout_ms)
            _start_read_only_transaction(cursor)
            cursor.execute(query)
            columns = [str(item[0]) for item in cursor.description]
            row_limit = min(max_rows, profile.max_rows)
            raw_rows = list(cursor.fetchmany(size=row_limit + 1))
            truncated = len(raw_rows) > row_limit
            rows = raw_rows[:row_limit]
            _write_audit_event(
                profile,
                query=query,
                status="success",
                duration_ms=round((time.monotonic() - started) * 1000),
                database=selected_database,
                internal=True,
                row_count=len(rows),
                truncated=truncated,
            )
            return columns, rows
    except Exception as exc:
        _write_audit_event(
            profile,
            query=query,
            status="error" if policy_allowed else "denied",
            policy="allowed" if policy_allowed else "denied",
            duration_ms=round((time.monotonic() - started) * 1000),
            database=selected_database,
            internal=True,
            error_type=type(exc).__name__,
        )
        raise
    finally:
        if connection_object is not None:
            try:
                connection_object.rollback()
            finally:
                connection_object.close()


@app.list_resources()
async def list_resources() -> list[Resource]:
    """List connection, database, or table resources without policy bypasses."""
    request_id, client_name, client_version = _request_identity()
    audit_token = set_audit_context(
        build_audit_context(
            None,
            request_id=request_id,
            operation="list_resources",
            client_name=client_name,
            client_version=client_version,
        )
    )

    def _sync_list():
        registry = load_connection_registry()
        if len(registry.profiles) > 1:
            return [
                Resource(
                    uri=f"mysql://connection/{profile.name}",
                    name=f"connection_{profile.name}",
                    mimeType="text/plain",
                    description=profile.description
                    or f"MySQL connection: {profile.name}",
                )
                for profile in registry.profiles.values()
            ]

        profile = registry.get()
        if not profile.database:
            _, rows = _sync_resource_query(
                profile,
                _database_listing_query(profile),
            )
            return [
                Resource(
                    uri=f"mysql://database/{row[0]}",
                    name=f"database_{row[0]}",
                    mimeType="text/plain",
                    description=f"MySQL database: {row[0]}",
                )
                for row in rows
            ]

        _, rows = _sync_resource_query(profile, "SHOW TABLES")
        return [
            Resource(
                uri=f"mysql://{row[0]}/data",
                name=f"table_{row[0]}",
                mimeType="text/plain",
                description=f"Data in table: {row[0]}",
            )
            for row in rows
        ]

    try:
        return await anyio.to_thread.run_sync(_sync_list)
    except Error as exc:
        logger.error("Failed to list resources (%s)", type(exc).__name__)
        return []
    finally:
        reset_audit_context(audit_token)


@app.list_resource_templates()
async def list_resource_templates() -> list[ResourceTemplate]:
    """
    Returns available resource templates. Currently returns an empty list,
    but implemented for better compatibility with tools like Visual Studio Code.
    """
    return []


@app.read_resource()
async def read_resource(uri: AnyUrl) -> str:
    """Read resources through the same profile allowlist and read-only controls."""
    request_id, client_name, client_version = _request_identity()
    audit_token = set_audit_context(
        build_audit_context(
            None,
            request_id=request_id,
            operation="read_resource",
            client_name=client_name,
            client_version=client_version,
        )
    )
    uri_str = str(uri)
    try:
        if not uri_str.startswith("mysql://"):
            raise ValueError(f"Invalid URI scheme: {uri_str}")
        parts = uri_str[8:].split("/")
        connection = parts[1] if len(parts) == 2 and parts[0] == "connection" else None
        profile = load_connection_registry().get(connection)

        def _sync_read() -> str:
            if connection:
                _, rows = _sync_resource_query(
                    profile,
                    _database_listing_query(profile),
                )
                databases = [str(row[0]) for row in rows]
                return "\n".join(
                    [f"Databases on connection '{connection}':"] + databases
                )

            if len(parts) == 2 and parts[0] == "database":
                database_name = validate_identifier(parts[1])
                ensure_database_allowed(profile, database_name)
                _, rows = _sync_resource_query(
                    profile,
                    f"SHOW TABLES FROM `{database_name}`",
                    database=database_name,
                )
                return "\n".join(
                    [f"Tables in database '{database_name}':"]
                    + [str(row[0]) for row in rows]
                )

            if not parts or len(parts) > 2:
                raise ValueError(f"Invalid MySQL resource URI: {uri_str}")
            table = validate_identifier(parts[0])
            columns, rows = _sync_resource_query(
                profile,
                f"SELECT * FROM `{table}` LIMIT 100",
                max_rows=100,
            )
            result = QueryResult(
                connection=profile.name,
                database=profile.database,
                columns=columns,
                rows=[
                    [serialize_value(value, profile.max_cell_length) for value in row]
                    for row in rows
                ],
                offset=0,
                truncated=False,
                duration_ms=0,
                query_id=query_fingerprint(f"SELECT * FROM `{table}` LIMIT 100"),
            )
            return result.render(profile.result_format)

        try:
            return await anyio.to_thread.run_sync(_sync_read)
        except Error as exc:
            errno = getattr(exc, "errno", None)
            suffix = f" (errno={errno})" if errno is not None else ""
            raise RuntimeError(f"Database read failed{suffix}") from exc
    finally:
        reset_audit_context(audit_token)


@app.list_tools()
async def list_tools() -> list[Tool]:
    """Define discovery and strictly read-only database tools."""
    connection_property = {
        "type": "string",
        "description": (
            "Named connection from MYSQL_PROFILES_FILE. "
            "Omit to use the configured default."
        ),
    }
    database_property = {
        "type": "string",
        "description": "Database/schema for this call. Omit to use the profile default.",
    }
    audit_context_property = {
        "type": "object",
        "description": (
            "Optional enterprise audit attribution. Profiles may require selected "
            "fields before any query is executed."
        ),
        "additionalProperties": False,
        "properties": {
            "actor": {
                "type": "string",
                "maxLength": 128,
                "description": "Human or service identity responsible for the request.",
            },
            "purpose": {
                "type": "string",
                "maxLength": 256,
                "description": "Business purpose for the database access.",
            },
            "ticket_id": {
                "type": "string",
                "maxLength": 128,
                "description": "Change, incident, case, or work-item identifier.",
            },
        },
    }
    target_properties = {
        "connection": connection_property,
        "database": database_property,
        "audit_context": audit_context_property,
    }
    result_properties = {
        "max_rows": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "description": "Maximum rows returned for this page.",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1_000_000,
            "description": "Rows to skip before returning this page.",
        },
        "result_format": {
            "type": "string",
            "enum": ["csv", "json"],
            "description": "Override the profile result format.",
        },
        "timeout_ms": {
            "type": "integer",
            "minimum": 100,
            "maximum": 300_000,
            "description": (
                "Per-call timeout. It may lower but never exceed the profile limit."
            ),
        },
    }
    return [
        Tool(
            name="list_connections",
            description=(
                "List configured MySQL connection profiles without exposing passwords. "
                "Call this first when multiple environments are configured."
            ),
            inputSchema={"type": "object", "properties": {}},
            annotations=ToolAnnotations(
                title="List Connections",
                readOnlyHint=True,
                destructiveHint=False,
            ),
        ),
        Tool(
            name="validate_connections",
            description=(
                "Reload and validate all profile configuration and password "
                "environment references without connecting to MySQL."
            ),
            inputSchema={"type": "object", "properties": {}},
            annotations=ToolAnnotations(
                title="Validate Connections",
                readOnlyHint=True,
                destructiveHint=False,
            ),
        ),
        Tool(
            name="check_connection",
            description=(
                "Run read-only health checks and SHOW GRANTS on one connection."
            ),
            inputSchema={
                "type": "object",
                "properties": target_properties,
            },
            annotations=ToolAnnotations(
                title="Check Connection",
                readOnlyHint=True,
                destructiveHint=False,
            ),
        ),
        Tool(
            name="list_databases",
            description="List accessible non-system databases on a connection.",
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": connection_property,
                    "audit_context": audit_context_property,
                    "result_format": result_properties["result_format"],
                    "timeout_ms": result_properties["timeout_ms"],
                },
            },
            annotations=ToolAnnotations(
                title="List Databases",
                readOnlyHint=True,
                destructiveHint=False,
            ),
        ),
        Tool(
            name="list_tables",
            description="List tables and views in a selected database.",
            inputSchema={
                "type": "object",
                "properties": {
                    **target_properties,
                    "result_format": result_properties["result_format"],
                    "timeout_ms": result_properties["timeout_ms"],
                },
            },
            annotations=ToolAnnotations(
                title="List Tables",
                readOnlyHint=True,
                destructiveHint=False,
            ),
        ),
        Tool(
            name="execute_sql",
            description=(
                "Execute one strictly read-only SQL statement. Only SELECT, WITH, "
                "SHOW, DESCRIBE, DESC, EXPLAIN, and TABLE are accepted. Writes, "
                "DDL, transaction commands, locking reads, SELECT INTO, and "
                "multiple statements are rejected before connecting."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "One read-only SQL statement.",
                    },
                    **target_properties,
                    **result_properties,
                },
                "required": ["query"],
            },
            annotations=ToolAnnotations(
                title="Execute Read-Only SQL",
                readOnlyHint=True,
                destructiveHint=False,
            ),
        ),
        Tool(
            name="get_schema_info",
            description=(
                "Get column metadata for one table or all tables in the selected "
                "database. A table may be written as database.table."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Optional bare table name or database.table.",
                    },
                    **target_properties,
                    "result_format": result_properties["result_format"],
                    "timeout_ms": result_properties["timeout_ms"],
                },
            },
            annotations=ToolAnnotations(
                title="Get Schema Info",
                readOnlyHint=True,
                destructiveHint=False,
            ),
        ),
        Tool(
            name="get_table_sample",
            description="Fetch a small sample from a table without modifying data.",
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Bare table name or database.table.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "Number of rows to return (default 5, max 100).",
                    },
                    **target_properties,
                    "offset": result_properties["offset"],
                    "result_format": result_properties["result_format"],
                    "timeout_ms": result_properties["timeout_ms"],
                },
                "required": ["table_name"],
            },
            annotations=ToolAnnotations(
                title="Get Table Sample",
                readOnlyHint=True,
                destructiveHint=False,
            ),
        ),
    ]


def _request_identity() -> tuple[str, str | None, str | None]:
    """Read correlation and client identity from the active MCP request."""
    try:
        request_context = app.request_context
    except LookupError:
        return uuid.uuid4().hex, None, None
    client_params = request_context.session.client_params
    client_info = client_params.clientInfo if client_params else None
    return (
        str(request_context.request_id),
        client_info.name if client_info else None,
        client_info.version if client_info else None,
    )


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch read-only tools with explicit connection and database scope."""
    arguments = arguments or {}
    connection = arguments.get("connection")
    database = arguments.get("database")
    audit_token = None
    try:
        request_id, client_name, client_version = _request_identity()
        audit_token = set_audit_context(
            build_audit_context(
                arguments.get("audit_context"),
                request_id=request_id,
                operation=name,
                client_name=client_name,
                client_version=client_version,
            )
        )
        logger.info(
            "Calling tool %s (connection=%s, database=%s)",
            name,
            connection or "<default>",
            database or "<profile default>",
        )
        if database:
            validate_identifier(database)

        if name in {"list_connections", "validate_connections"}:
            registry = load_connection_registry(
                force_reload=name == "validate_connections"
            )
            profiles = []
            for profile in registry.profiles.values():
                ready, status = profile.runtime_status()
                profiles.append(
                    {
                        "name": profile.name,
                        "default": profile.name == registry.default,
                        "database": profile.database,
                        "allowed_databases": list(profile.allowed_databases),
                        "allowed_functions": list(profile.allowed_functions),
                        "description": profile.description,
                        "ready": ready,
                        "status": status,
                        "ssh": profile.ssh.enabled,
                        "pool_size": profile.pool_size,
                        "query_timeout_ms": profile.query_timeout_ms,
                        "max_rows": profile.max_rows,
                        "result_format": profile.result_format,
                        "audit": {
                            "enabled": profile.audit_enabled,
                            "durable": bool(profile.audit_log_file),
                            "signed": bool(profile.audit_hmac_key_env),
                            "required_context": list(profile.audit_required_context),
                            "fail_closed": profile.audit_fail_closed,
                        },
                    }
                )
            payload = {
                "valid": not registry.errors
                and all(item["ready"] for item in profiles),
                "default": registry.default,
                "source": (
                    "environment"
                    if registry.source == "environment"
                    else "profiles_file"
                ),
                "profiles": profiles,
                "errors": registry.errors,
            }
            return [
                TextContent(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                )
            ]

        if name == "check_connection":
            return await check_connection(connection=connection, database=database)

        if name == "list_databases":
            profile = load_connection_registry().get(connection)
            query = _database_listing_query(profile)
            return await run_query(
                query,
                connection=connection,
                internal=True,
                result_format=arguments.get("result_format"),
                timeout_ms=arguments.get("timeout_ms"),
            )

        if name == "list_tables":
            schema_filter = f"'{database}'" if database else "DATABASE()"
            query = (
                "SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS, TABLE_COMMENT "
                "FROM information_schema.TABLES "
                f"WHERE TABLE_SCHEMA = {schema_filter} "
                "ORDER BY TABLE_NAME"
            )
            return await run_query(
                query,
                connection=connection,
                database=database,
                internal=True,
                result_format=arguments.get("result_format"),
                timeout_ms=arguments.get("timeout_ms"),
            )

        if name == "execute_sql":
            query_argument = arguments.get("query")
            if not isinstance(query_argument, str):
                raise ValueError("Query is required")
            return await run_query(
                query_argument,
                connection=connection,
                database=database,
                max_rows=arguments.get("max_rows"),
                offset=arguments.get("offset", 0),
                result_format=arguments.get("result_format"),
                timeout_ms=arguments.get("timeout_ms"),
            )

        if name == "get_schema_info":
            table_name = arguments.get("table_name")
            if table_name:
                if not isinstance(table_name, str):
                    raise ValueError("table_name must be a string")
                table_database, table = parse_table_arg(table_name)
                selected_database = table_database or database
                profile = load_connection_registry().get(connection)
                ensure_database_allowed(profile, selected_database)
                if (
                    selected_database in SYSTEM_DATABASES
                    and not profile.allow_system_databases
                ):
                    raise ValueError(
                        f"System database '{selected_database}' is blocked for "
                        f"connection '{profile.name}'"
                    )
                schema_filter = (
                    f"'{selected_database}'" if selected_database else "DATABASE()"
                )
                query = (
                    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, "
                    "COLUMN_COMMENT FROM information_schema.COLUMNS "
                    f"WHERE TABLE_SCHEMA = {schema_filter} "
                    f"AND TABLE_NAME = '{table}' ORDER BY ORDINAL_POSITION"
                )
            else:
                schema_filter = f"'{database}'" if database else "DATABASE()"
                query = (
                    "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                    "FROM information_schema.COLUMNS "
                    f"WHERE TABLE_SCHEMA = {schema_filter} "
                    "ORDER BY TABLE_NAME, ORDINAL_POSITION"
                )
            return await run_query(
                query,
                connection=connection,
                database=database,
                internal=True,
                result_format=arguments.get("result_format"),
                timeout_ms=arguments.get("timeout_ms"),
            )

        if name == "get_table_sample":
            table_name = arguments.get("table_name")
            if not isinstance(table_name, str):
                raise ValueError("table_name is required")
            table_database, table = parse_table_arg(table_name)
            limit = int(arguments.get("limit", 5))
            if not 1 <= limit <= 100:
                raise ValueError("limit must be between 1 and 100")
            table_ref = (
                f"`{table_database}`.`{table}`" if table_database else f"`{table}`"
            )
            query = f"SELECT * FROM {table_ref}"
            return await run_query(
                query,
                connection=connection,
                database=database,
                max_rows=limit,
                offset=arguments.get("offset", 0),
                result_format=arguments.get("result_format"),
                timeout_ms=arguments.get("timeout_ms"),
            )

        raise ValueError(f"Unknown tool: {name}")
    except Exception as exc:
        logger.error("Error in call_tool %s (%s)", name, type(exc).__name__)
        return [TextContent(type="text", text=f"Error calling tool {name}: {str(exc)}")]
    finally:
        if audit_token is not None:
            reset_audit_context(audit_token)


@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name="explore_database",
            description=(
                "Systematically explore the database: discover available tables, "
                "inspect their schemas, sample the data, and summarize what's there."
            ),
            arguments=[
                PromptArgument(
                    name="connection",
                    description="Optional named connection profile.",
                    required=False,
                ),
                PromptArgument(
                    name="database",
                    description="Optional database/schema override.",
                    required=False,
                ),
            ],
        ),
        Prompt(
            name="analyze_table",
            description=(
                "Deep-dive into a specific table: retrieve its schema, sample its data, "
                "and suggest useful queries."
            ),
            arguments=[
                PromptArgument(
                    name="table_name",
                    description="Table to analyze. Use database.table notation for cross-database queries.",
                    required=True,
                ),
                PromptArgument(
                    name="connection",
                    description="Optional named connection profile.",
                    required=False,
                ),
                PromptArgument(
                    name="database",
                    description="Optional database/schema override.",
                    required=False,
                ),
            ],
        ),
    ]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict | None) -> GetPromptResult:
    prompt_arguments = arguments or {}
    connection = prompt_arguments.get("connection")
    database = prompt_arguments.get("database")
    target = ", ".join(
        part
        for part in [
            f'connection="{connection}"' if connection else "",
            f'database="{database}"' if database else "",
        ]
        if part
    )
    target_instruction = (
        f"Pass {target} to every database tool call."
        if target
        else "Call list_connections first and use the default connection unless the user selects another."
    )

    if name == "explore_database":
        return GetPromptResult(
            description="Systematic database exploration workflow",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=(
                            "Explore this MySQL database systematically:\n\n"
                            f"Target: {target_instruction}\n"
                            "1. Call list_databases when no database is selected, then "
                            "call list_tables for the target database.\n"
                            "2. Call get_schema_info with no table_name to see all table structures at once, "
                            "or for each table of interest individually.\n"
                            "3. Call get_table_sample on 2–3 representative tables to understand "
                            "data format and content.\n"
                            "4. Summarize: describe what each table stores, note relationships "
                            "(foreign keys, shared ID columns), and suggest 3–5 queries "
                            "an analyst would find useful."
                        ),
                    ),
                )
            ],
        )
    elif name == "analyze_table":
        table_name = prompt_arguments.get("table_name", "")
        return GetPromptResult(
            description=f"Analysis workflow for: {table_name}",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=(
                            f"Analyze the table `{table_name}`:\n\n"
                            f"Target: {target_instruction}\n"
                            f'1. Call get_schema_info with table_name="{table_name}" '
                            "to retrieve column names, types, nullability, and comments.\n"
                            f'2. Call get_table_sample with table_name="{table_name}" '
                            "to see representative rows.\n"
                            "3. Based on the schema and sample, provide:\n"
                            "   - A plain-English description of what this table stores\n"
                            "   - Notable columns (primary keys, foreign keys, important fields)\n"
                            "   - Data quality observations (NULLs, patterns, value ranges)\n"
                            "   - 3–5 example SQL queries useful for analysis"
                        ),
                    ),
                )
            ],
        )
    else:
        raise ValueError(f"Unknown prompt: {name}")


def _write_audit_event(
    profile: ConnectionProfile,
    *,
    query: str,
    status: str,
    duration_ms: int,
    database: str | None = None,
    internal: bool = False,
    policy: str = "allowed",
    row_count: int = 0,
    truncated: bool = False,
    error_type: str | None = None,
) -> None:
    if not profile.audit_enabled:
        return
    fields = {
        "event": "mysql_read_query",
        "connection": profile.name,
        "database": database,
        "query_id": query_fingerprint(query),
        "query_type": query_type(query),
        "policy": policy,
        "internal": internal,
        "read_only_enforced": True,
        "status": status,
        "duration_ms": duration_ms,
        "row_count": row_count,
        "truncated": truncated,
    }
    if error_type:
        fields["error_type"] = error_type
    try:
        _event, serialized = audit_sink.write(profile, fields)
    except AuditWriteError:
        logger.critical(
            "Durable audit event write failed (connection=%s, status=%s)",
            profile.name,
            status,
        )
        if profile.audit_fail_closed:
            raise
        fallback_event = audit_sink.prepare_event(profile, fields, sign=False)
        fallback_event["audit_sink_error"] = True
        serialized = json.dumps(
            fallback_event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    audit_logger.info(serialized)


async def execute_query(
    query: str,
    *,
    connection: str | None = None,
    database: str | None = None,
    max_rows: int | None = None,
    offset: int = 0,
    timeout_ms: int | None = None,
    internal: bool = False,
) -> QueryResult:
    """Execute one validated query with policy, timeout, cancellation and paging."""
    profile = load_connection_registry().get(connection)
    started = time.monotonic()
    selected_database = database or profile.database
    try:
        ready, readiness = profile.runtime_status()
        if not ready:
            raise ValueError(readiness)
        validate_required_audit_context(profile, current_audit_context())
        query = validate_read_only_query(query)
        selected_database = ensure_database_allowed(profile, database)
        if selected_database in SYSTEM_DATABASES and not profile.allow_system_databases:
            raise ValueError(
                f"System database '{selected_database}' is blocked for connection "
                f"'{profile.name}'"
            )
        validate_database_access(
            query,
            selected_database=selected_database,
            allowed_databases=profile.allowed_databases,
            allow_system_databases=profile.allow_system_databases,
            internal=internal,
        )
        validate_function_safety(query, allowed_functions=profile.allowed_functions)

        row_limit = profile.max_rows if max_rows is None else int(max_rows)
        if not 1 <= row_limit <= profile.max_rows:
            raise ValueError(
                f"max_rows must be between 1 and profile limit {profile.max_rows}"
            )
        page_offset = int(offset)
        if not 0 <= page_offset <= 1_000_000:
            raise ValueError("offset must be between 0 and 1000000")
        effective_timeout = (
            profile.query_timeout_ms if timeout_ms is None else int(timeout_ms)
        )
        if not 100 <= effective_timeout <= profile.query_timeout_ms:
            raise ValueError(
                "timeout_ms must be at least 100 and cannot exceed the profile limit "
                f"{profile.query_timeout_ms}"
            )
        audit_sink.preflight(profile)
    except Exception as exc:
        _write_audit_event(
            profile,
            query=query,
            status="denied",
            policy="denied",
            database=selected_database,
            internal=internal,
            duration_ms=round((time.monotonic() - started) * 1000),
            error_type=type(exc).__name__,
        )
        raise

    control = QueryControl()

    def _sync_execute() -> QueryResult:
        connection_object = None
        discard_connection = False
        try:
            connection_object, config = _open_connection(
                profile,
                database=selected_database,
                query_timeout_ms=effective_timeout,
            )
            control.bind(connection_object)
            with connection_object.cursor() as cursor:
                _apply_server_query_timeout(cursor, effective_timeout)
                _start_read_only_transaction(cursor)
                cursor.execute(query)
                if cursor.description is None:
                    raise RuntimeError("Read-only query did not return a result set")

                remaining = page_offset
                while remaining:
                    skipped = cursor.fetchmany(size=min(remaining, 1000))
                    if not skipped:
                        break
                    remaining -= len(skipped)

                columns = [str(description[0]) for description in cursor.description]
                raw_rows = list(cursor.fetchmany(size=row_limit + 1))
                truncated = len(raw_rows) > row_limit
                discard_connection = truncated
                raw_rows = raw_rows[:row_limit]
                rows = [
                    [serialize_value(value, profile.max_cell_length) for value in row]
                    for row in raw_rows
                ]
                return QueryResult(
                    connection=profile.name,
                    database=config.get("database"),
                    columns=columns,
                    rows=rows,
                    offset=page_offset,
                    truncated=truncated,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    query_id=query_fingerprint(query),
                )
        except Error as exc:
            errno = getattr(exc, "errno", None)
            sqlstate = getattr(exc, "sqlstate", None)
            reference = ",".join(
                value
                for value in [
                    f"errno={errno}" if errno is not None else "",
                    f"sqlstate={sqlstate}" if sqlstate else "",
                ]
                if value
            )
            suffix = f" ({reference})" if reference else ""
            raise RuntimeError(f"MySQL read-only query failed{suffix}") from exc
        finally:
            if connection_object is not None:
                if discard_connection:
                    try:
                        connection_object.shutdown()
                    except Exception:
                        logger.debug(
                            "Socket shutdown failed for truncated result cleanup"
                        )
                else:
                    try:
                        connection_object.rollback()
                    except Exception:
                        logger.debug("Rollback failed during connection cleanup")
                try:
                    connection_object.close()
                except Exception:
                    logger.debug("Connection close failed during cleanup")
            control.unbind()

    try:
        with anyio.fail_after(effective_timeout / 1000):
            result = await anyio.to_thread.run_sync(
                _sync_execute,
                abandon_on_cancel=True,
            )
    except TimeoutError as exc:
        control.cancel()
        duration = round((time.monotonic() - started) * 1000)
        _write_audit_event(
            profile,
            query=query,
            status="timeout",
            duration_ms=duration,
            database=selected_database,
            internal=internal,
            error_type="TimeoutError",
        )
        raise TimeoutError(
            f"Read-only query exceeded {effective_timeout} ms and was cancelled"
        ) from exc
    except anyio.get_cancelled_exc_class():
        control.cancel()
        duration = round((time.monotonic() - started) * 1000)
        _write_audit_event(
            profile,
            query=query,
            status="cancelled",
            duration_ms=duration,
            database=selected_database,
            internal=internal,
            error_type="CancelledError",
        )
        raise
    except Exception as exc:
        duration = round((time.monotonic() - started) * 1000)
        _write_audit_event(
            profile,
            query=query,
            status="error",
            duration_ms=duration,
            database=selected_database,
            internal=internal,
            error_type=type(exc).__name__,
        )
        raise

    _write_audit_event(
        profile,
        query=query,
        status="success",
        duration_ms=result.duration_ms,
        database=selected_database,
        internal=internal,
        row_count=len(result.rows),
        truncated=result.truncated,
    )
    return result


async def run_query(
    query: str,
    *,
    connection: str | None = None,
    database: str | None = None,
    max_rows: int | None = None,
    offset: int = 0,
    result_format: str | None = None,
    timeout_ms: int | None = None,
    internal: bool = False,
) -> list[TextContent]:
    profile = load_connection_registry().get(connection)
    selected_format = (result_format or profile.result_format).lower()
    if selected_format not in {"csv", "json"}:
        raise ValueError("result_format must be csv or json")
    result = await execute_query(
        query,
        connection=connection,
        database=database,
        max_rows=max_rows,
        offset=offset,
        timeout_ms=timeout_ms,
        internal=internal,
    )
    return [TextContent(type="text", text=result.render(selected_format))]


READ_ONLY_GRANT_PRIVILEGES = {"SELECT", "SHOW VIEW", "USAGE"}


def _assess_grants(grants: list[str], global_read_only: object) -> dict[str, object]:
    """Identify database privileges that weaken defense in depth."""
    non_read_privileges: set[str] = set()
    for grant in grants:
        match = re.match(r"^\s*GRANT\s+(.+?)\s+ON\s+", grant, re.IGNORECASE)
        if not match:
            non_read_privileges.add("UNPARSED_GRANT")
            continue
        privileges = {
            privilege.strip().upper() for privilege in match.group(1).split(",")
        }
        non_read_privileges.update(privileges - READ_ONLY_GRANT_PRIVILEGES)

    account_select_only = not non_read_privileges
    try:
        server_read_only = bool(int(str(global_read_only)))
    except (TypeError, ValueError):
        server_read_only = bool(global_read_only)
    assessment: dict[str, object] = {
        "account_select_only": account_select_only,
        "server_global_read_only": server_read_only,
        "defense_in_depth": account_select_only or server_read_only,
        "non_read_privileges": sorted(non_read_privileges),
    }
    if not account_select_only:
        assessment["warning"] = (
            "The database account has non-read privileges. The MCP still blocks "
            "writes in software and uses read-only transactions. A SELECT-only "
            "account is recommended as an additional containment layer."
        )
    return assessment


async def check_connection(
    *,
    connection: str | None = None,
    database: str | None = None,
) -> list[TextContent]:
    """Return a credential-safe read-only health and privilege summary."""
    profile = load_connection_registry().get(connection)
    selected_database = ensure_database_allowed(profile, database)
    health = await execute_query(
        "SELECT VERSION() AS version, DATABASE() AS current_database, "
        "CURRENT_USER() AS authenticated_user, "
        "@@global.read_only AS global_read_only",
        connection=profile.name,
        database=selected_database,
        max_rows=1,
        internal=True,
    )
    grants = await execute_query(
        "SHOW GRANTS",
        connection=profile.name,
        database=selected_database,
        max_rows=min(100, profile.max_rows),
        internal=True,
    )
    grant_values = [str(row[0]) for row in grants.rows]
    server_values = dict(zip(health.columns, health.rows[0], strict=False))
    authenticated_user = server_values.pop("authenticated_user", None)
    if authenticated_user is not None:
        server_values["account_fingerprint"] = hashlib.sha256(
            str(authenticated_user).encode("utf-8")
        ).hexdigest()[:16]
    payload = {
        "ok": True,
        "connection": profile.name,
        "database": selected_database,
        "description": profile.description,
        "allowed_databases": list(profile.allowed_databases),
        "ssh": profile.ssh.enabled,
        "pool_size": profile.pool_size,
        "query_timeout_ms": profile.query_timeout_ms,
        "server": server_values,
        "grant_count": len(grant_values),
        "read_only_assessment": _assess_grants(
            grant_values, server_values.get("global_read_only")
        ),
        "duration_ms": health.duration_ms + grants.duration_ms,
    }
    return [
        TextContent(
            type="text",
            text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
    ]


async def main():
    """
    Main entry point for the MCP server.
    Supports both STDIO (default) and SSE (HTTP) transport modes.
    """
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    try:
        if transport == "sse":
            await _run_sse_server()
        else:
            await _run_stdio_server()
    finally:
        close_runtime_resources()


async def _run_stdio_server():
    """Runs the server using standard input/output streams."""
    from mcp.server.stdio import stdio_server

    logger.info("Starting MySQL MCP server (STDIO)...")
    async with stdio_server() as (read_stream, write_stream):
        try:
            await app.run(
                read_stream, write_stream, app.create_initialization_options()
            )
        except Exception as e:
            logger.error(f"Server error: {str(e)}", exc_info=True)
            raise


def _validate_sse_exposure(
    host: str, bearer_token: str | None, trust_proxy_auth: bool
) -> None:
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    public_bind = host not in loopback_hosts
    if bearer_token is not None and len(bearer_token) < 32:
        raise ValueError("MCP_SSE_BEARER_TOKEN must be at least 32 characters")
    if public_bind and not bearer_token and not trust_proxy_auth:
        raise ValueError(
            "Refusing unauthenticated public SSE bind. Configure "
            "MCP_SSE_BEARER_TOKEN, bind to 127.0.0.1, or explicitly set "
            "MCP_SSE_TRUST_PROXY_AUTH=true when an authenticated reverse proxy "
            "is the only network entry point."
        )


async def _run_sse_server():
    """
    Runs the server using Server-Sent Events (SSE) over HTTP.
    Requires 'starlette' and 'uvicorn' dependencies.
    """
    try:
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.routing import Mount, Route
        from starlette.responses import Response
        import uvicorn
    except ImportError:
        logger.error(
            "SSE transport requires additional dependencies. Install with: pip install mysql_mcp_server[sse]"
        )
        raise

    logger.info("Starting MySQL MCP server (SSE)...")

    host = os.getenv("MCP_SSE_HOST", "127.0.0.1")
    port_str = os.getenv("MCP_SSE_PORT") or os.getenv("PORT") or "8000"
    port = int(port_str)
    bearer_token = os.getenv("MCP_SSE_BEARER_TOKEN") or None
    trust_proxy_auth = os.getenv("MCP_SSE_TRUST_PROXY_AUTH", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    _validate_sse_exposure(host, bearer_token, trust_proxy_auth)

    # Build security settings with DNS rebinding protection.
    # allowed_hosts controls which Host header values are accepted; without this
    # a DNS rebinding attack can relay requests through the victim's browser.
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        allowed_hosts_env = os.getenv("MCP_SSE_ALLOWED_HOSTS", "")
        if allowed_hosts_env:
            allowed_hosts = [
                h.strip() for h in allowed_hosts_env.split(",") if h.strip()
            ]
        else:
            allowed_hosts = [f"localhost:{port}", f"127.0.0.1:{port}"]
            if host not in ("0.0.0.0", "127.0.0.1", "localhost", "::"):
                allowed_hosts.append(f"{host}:{port}")

        logger.info(
            "SSE DNS rebinding protection enabled. Allowed hosts: %s. "
            "Override with MCP_SSE_ALLOWED_HOSTS (comma-separated).",
            ", ".join(allowed_hosts),
        )
        security_settings = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
        )
    except ImportError:
        logger.warning(
            "mcp.server.transport_security not available (upgrade mcp>=1.9.0 for DNS rebinding protection). "
            "Running without Origin/Host validation."
        )
        security_settings = None

    sse = (
        SseServerTransport("/messages/", security_settings=security_settings)
        if security_settings is not None
        else SseServerTransport("/messages/")
    )

    async def handle_sse(request):
        """Handler for the SSE connection endpoint."""
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await app.run(streams[0], streams[1], app.create_initialization_options())
        return Response()

    async def health_check(request):
        """Simple health check endpoint."""
        return Response("MySQL MCP Server is running", media_type="text/plain")

    # Define the Starlette application with SSE routes and a health check.
    starlette_app = Starlette(
        routes=[
            Route("/", endpoint=health_check),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ]
    )

    if bearer_token:

        class BearerAuthMiddleware:
            def __init__(self, wrapped_app, token: str):
                self.wrapped_app = wrapped_app
                self.expected = f"Bearer {token}"

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http" and scope.get("path") != "/":
                    headers = dict(scope.get("headers", []))
                    provided = headers.get(b"authorization", b"").decode("latin-1")
                    if not hmac.compare_digest(provided, self.expected):
                        response = Response(
                            "Unauthorized",
                            status_code=401,
                            headers={"WWW-Authenticate": "Bearer"},
                        )
                        await response(scope, receive, send)
                        return
                await self.wrapped_app(scope, receive, send)

        secured_app = BearerAuthMiddleware(starlette_app, bearer_token)
    else:
        secured_app = starlette_app

    # Configure and start the Uvicorn server.
    server_config = uvicorn.Config(secured_app, host=host, port=port, log_level="info")
    server = uvicorn.Server(server_config)
    await server.serve()


if __name__ == "__main__":
    # Start the asyncio event loop.
    asyncio.run(main())
