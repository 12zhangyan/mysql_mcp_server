"""MCP tool definitions: names, descriptions, input schemas and annotations.

The published list is part of the client contract; tests/snapshots/list_tools.json
pins it byte-for-byte. Handlers are registered in server.py with @_tool(...).
"""

from mcp.types import Tool, ToolAnnotations

from .metadata import MAX_TABLE_NAMES

TRUNCATION_GUIDANCE = (
    "Results are capped by the connection max_rows (MYSQL_MAX_ROWS, default 500, "
    "maximum 1000). Always inspect truncated and next_offset. Repeat with the same "
    "filters and offset=next_offset until truncated is false; a single page is not "
    "the complete catalog."
)


def tool_definitions() -> list[Tool]:
    """Return the published tools in their stable order."""
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
        "max_response_bytes": {
            "type": "integer",
            "minimum": 4096,
            "maximum": 4194304,
            "description": "Maximum UTF-8 response text bytes; may only lower the profile budget.",
        },
        "max_rows": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "description": (
                "Maximum rows returned for this page. Defaults to the connection "
                "MYSQL_MAX_ROWS cap. Check truncated/next_offset; one page is not "
                "a complete result."
            ),
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1_000_000,
            "description": (
                "Rows to skip before returning this page. Omit or 0 for the first "
                "page; continue with next_offset until truncated is false."
            ),
        },
        "result_format": {
            "type": "string",
            "enum": ["csv", "json", "compact"],
            "description": "csv preserves legacy data; json or compact includes target, paging, masking and truncation metadata.",
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
    table_pattern_property = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "description": (
            "Optional name glob in the selected database; use * for many "
            "characters and ? for one character. Cannot combine with "
            "table_name/table or table_names."
        ),
    }
    table_names_property = {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_TABLE_NAMES,
        "items": {"type": "string"},
        "description": (
            "Batch of 1-100 bare table names in this database. Lists longer than "
            "100 are rejected. Cannot combine with table_name/table or table_pattern."
        ),
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
                    "offset": result_properties["offset"],
                    "max_rows": result_properties["max_rows"],
                    "audit_context": audit_context_property,
                    "result_format": result_properties["result_format"],
                    "max_response_bytes": result_properties["max_response_bytes"],
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
            description=(
                "List tables and views in a selected database, with optional "
                "name filtering and pagination for large schemas. Optional search "
                "matches names and comments of tables and columns, returning match "
                "evidence. " + TRUNCATION_GUIDANCE
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **target_properties,
                    "search": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "description": "Literal keyword in table/column names or comments; returns one row per matching field.",
                    },
                    "table_name": {
                        "type": "string",
                        "description": "Optional exact bare table name filter.",
                    },
                    "table": {
                        "type": "string",
                        "description": "Compatibility alias for table_name.",
                    },
                    "table_pattern": table_pattern_property,
                    "max_rows": result_properties["max_rows"],
                    "offset": result_properties["offset"],
                    "result_format": result_properties["result_format"],
                    "max_response_bytes": result_properties["max_response_bytes"],
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
                "multiple statements are rejected before connecting. "
                "When table or column names are unknown, first use list_tables and get_schema_info; "
                "do not guess names. Keep connection and database consistent across calls. "
                "After TABLE_NOT_FOUND or COLUMN_NOT_FOUND, inspect metadata and correct SQL; "
                "never repeat the unchanged query or automatically switch databases. "
                "Use a stable unique ORDER BY for pagination; pages are separate reads, not one snapshot."
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
            name="explain_sql",
            description=(
                "Inspect the estimated execution plan for a SELECT/WITH query "
                "before running an expensive query. Pass the original SQL without "
                "EXPLAIN; ANALYZE, writes and multiple statements are rejected. "
                "Preserves the query's original LIMIT/OFFSET; max_rows/offset page "
                "the plan only. Uses the same database allowlist, function policy, "
                "timeouts, masking and audit as execute_sql. Estimates are not "
                "measured execution time. Long plan cells are kept complete; if "
                "the response is too large, use plan_output=chunks and follow "
                "next_offset with expected_plan_id from plan.id. " + TRUNCATION_GUIDANCE
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "One SELECT or WITH query, without EXPLAIN.",
                    },
                    "plan_output": {
                        "type": "string",
                        "enum": ["rows", "chunks"],
                        "default": "rows",
                        "description": "rows keeps complete plan cells. chunks requests "
                        "EXPLAIN FORMAT=JSON and pages its masked text as part/text rows; "
                        "concatenate text in part order. offset/max_rows count chunks. "
                        "Uses json by default; json/compact are required to carry plan.id.",
                    },
                    "expected_plan_id": {
                        "type": "string",
                        "pattern": "^[a-f0-9]{64}$",
                        "description": "For chunks, required when offset>0. Copy plan.id "
                        "from the first page; PLAN_CHANGED means restart at offset=0.",
                    },
                    **target_properties,
                    **result_properties,
                },
                "required": ["query"],
            },
            annotations=ToolAnnotations(
                title="Explain Read-Only SQL",
                readOnlyHint=True,
                destructiveHint=False,
            ),
        ),
        Tool(
            name="query",
            description=(
                "Compatibility alias for execute_sql. Executes one strictly "
                "read-only SQL statement with the same explicit connection, "
                "database, limits, masking, and audit controls. "
                "Verify unknown table/column names using list_tables/get_schema_info first. "
                "After deterministic SQL errors inspect metadata before correcting SQL; do not repeat unchanged SQL."
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
                title="Query (Read-Only Compatibility Alias)",
                readOnlyHint=True,
                destructiveHint=False,
            ),
        ),
        Tool(
            name="get_schema_info",
            description=(
                "Get column metadata for one table, a table_names/table_pattern "
                "batch, or all tables in the selected database. A table may be "
                "written as database.table. " + TRUNCATION_GUIDANCE
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Optional bare table name or database.table.",
                    },
                    "table": {
                        "type": "string",
                        "description": "Compatibility alias for table_name.",
                    },
                    "detail": {
                        "type": "boolean",
                        "default": False,
                        "description": "Return pageable table, column, index (including PRIMARY) and declared foreign-key rows in one result; KIND identifies each row.",
                    },
                    **target_properties,
                    "table_names": table_names_property,
                    "table_pattern": table_pattern_property,
                    "max_rows": result_properties["max_rows"],
                    "offset": result_properties["offset"],
                    "result_format": result_properties["result_format"],
                    "max_response_bytes": result_properties["max_response_bytes"],
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
                    "table": {
                        "type": "string",
                        "description": "Compatibility alias for table_name.",
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
                    "max_response_bytes": result_properties["max_response_bytes"],
                    "timeout_ms": result_properties["timeout_ms"],
                },
                "anyOf": [{"required": ["table_name"]}, {"required": ["table"]}],
            },
            annotations=ToolAnnotations(
                title="Get Table Sample",
                readOnlyHint=True,
                destructiveHint=False,
            ),
        ),
        Tool(
            name="inspect_catalog",
            description=(
                "Inspect an allowlisted metadata projection for the selected user "
                "database without granting arbitrary information_schema access. "
                "kind selects tables, columns, indexes, constraints, foreign_keys, "
                "or views. Omit table_name to page the whole database after a "
                "stable sort. " + TRUNCATION_GUIDANCE
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "tables",
                            "columns",
                            "indexes",
                            "constraints",
                            "foreign_keys",
                            "views",
                        ],
                    },
                    "table_name": {
                        "type": "string",
                        "description": "Optional bare table name filter.",
                    },
                    "table": {
                        "type": "string",
                        "description": "Compatibility alias for table_name.",
                    },
                    **target_properties,
                    "table_names": table_names_property,
                    "table_pattern": table_pattern_property,
                    "max_rows": result_properties["max_rows"],
                    "offset": result_properties["offset"],
                    "result_format": result_properties["result_format"],
                    "max_response_bytes": result_properties["max_response_bytes"],
                    "timeout_ms": result_properties["timeout_ms"],
                },
                "required": ["kind"],
            },
            annotations=ToolAnnotations(
                title="Inspect Controlled Catalog",
                readOnlyHint=True,
                destructiveHint=False,
            ),
        ),
        Tool(
            name="dump_schema",
            description=(
                "Return allowlisted table and column metadata grouped by table for "
                "the selected database. Use this to pull a large schema in pages "
                "instead of calling inspect_catalog once per table. kinds may only "
                "be tables and/or columns; indexes and constraints stay on "
                "inspect_catalog. " + TRUNCATION_GUIDANCE
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "kinds": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "string",
                            "enum": ["tables", "columns"],
                        },
                        "description": (
                            "Metadata sections to include. Defaults to tables and "
                            "columns. Other catalog kinds remain on inspect_catalog."
                        ),
                    },
                    "table_name": {
                        "type": "string",
                        "description": "Optional bare table name filter.",
                    },
                    "table": {
                        "type": "string",
                        "description": "Compatibility alias for table_name.",
                    },
                    **target_properties,
                    "table_names": table_names_property,
                    "table_pattern": table_pattern_property,
                    "max_rows": result_properties["max_rows"],
                    "offset": result_properties["offset"],
                    "max_response_bytes": result_properties["max_response_bytes"],
                    "timeout_ms": result_properties["timeout_ms"],
                },
            },
            annotations=ToolAnnotations(
                title="Dump Grouped Schema",
                readOnlyHint=True,
                destructiveHint=False,
            ),
        ),
    ]
