"""Fixed, scoped metadata SQL. User search text is a UTF-8 hex literal."""

from __future__ import annotations

import re
from typing import Any

MAX_TABLE_NAMES = 100
DUMP_SCHEMA_KINDS = ("tables", "columns")
BARE_TABLE_NAME = re.compile(r"[A-Za-z0-9_$]+")
TABLE_PATTERN = re.compile(r"[A-Za-z0-9_$*?-]+")
SCOPE_EXCLUSIVE = "Use only one of table_name/table, table_names, or table_pattern"


def table_filter(names: list[str] | None) -> str:
    if names is None:
        return ""
    if (
        not isinstance(names, list)
        or not 1 <= len(names) <= MAX_TABLE_NAMES
        or any(
            not isinstance(name, str) or not BARE_TABLE_NAME.fullmatch(name)
            for name in names
        )
    ):
        raise ValueError(
            "table_names must contain 1 to 100 bare table names in the selected "
            "database; names beyond that limit are rejected rather than truncated"
        )
    return (
        " AND TABLE_NAME IN ("
        + ",".join(f"'{name}'" for name in sorted(set(names)))
        + ")"
    )


def table_pattern_like(pattern: str) -> str:
    """Convert a restricted shell-style table glob to an escaped SQL LIKE value."""
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= 128:
        raise ValueError(
            "table_pattern must be a non-empty string up to 128 characters"
        )
    if not TABLE_PATTERN.fullmatch(pattern):
        raise ValueError(
            "table_pattern may contain only letters, numbers, _, $, -, *, or ?"
        )
    return (
        pattern.replace("\\", "\\\\")
        .replace("_", "\\_")
        .replace("*", "%")
        .replace("?", "_")
    )


def table_pattern_filter(pattern: str) -> str:
    return f" AND TABLE_NAME LIKE '{table_pattern_like(pattern)}' ESCAPE '\\\\'"


def catalog_name_filter(
    *,
    table: str | None = None,
    table_names: list[str] | None = None,
    table_pattern: str | None = None,
) -> str:
    """Build a mutually exclusive TABLE_NAME predicate for catalog projections."""
    selected = sum(value is not None for value in (table, table_names, table_pattern))
    if selected > 1:
        raise ValueError(SCOPE_EXCLUSIVE)
    if table is not None:
        if not isinstance(table, str) or not BARE_TABLE_NAME.fullmatch(table):
            raise ValueError(
                f"Invalid identifier '{table}': only alphanumeric, underscore, and $ are allowed"
            )
        return f" AND TABLE_NAME = '{table}'"
    if table_names is not None:
        return table_filter(table_names)
    if table_pattern is not None:
        return table_pattern_filter(table_pattern)
    return ""


def parse_dump_schema_kinds(kinds: list[str] | None) -> tuple[str, ...]:
    if kinds is None:
        return DUMP_SCHEMA_KINDS
    if not isinstance(kinds, list) or not kinds:
        raise ValueError("kinds must be a non-empty array of tables and/or columns")
    if any(not isinstance(kind, str) for kind in kinds):
        raise ValueError("kinds must be a non-empty array of tables and/or columns")
    normalized = tuple(dict.fromkeys(kinds))
    unknown = [kind for kind in normalized if kind not in DUMP_SCHEMA_KINDS]
    if unknown:
        raise ValueError(
            "dump_schema kinds may only be tables and columns; use inspect_catalog "
            "for indexes, constraints, foreign_keys, and views"
        )
    return normalized


def group_schema_dump(
    columns: list[str],
    rows: list[list[Any]],
    *,
    table_meta: dict[str, dict[str, Any]] | None = None,
    include_columns: bool = True,
) -> list[dict[str, Any]]:
    """Group a stable catalog page by table without dropping delivered rows."""
    index = {name: position for position, name in enumerate(columns)}
    grouped: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def start_table(name: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {"table_name": name}
        if table_meta and name in table_meta:
            entry.update(table_meta[name])
        if include_columns:
            entry["columns"] = []
        grouped.append(entry)
        return entry

    if "TABLE_NAME" not in index:
        raise ValueError("schema dump rows must include TABLE_NAME")

    if include_columns:
        required = (
            "COLUMN_NAME",
            "COLUMN_TYPE",
            "IS_NULLABLE",
            "COLUMN_DEFAULT",
            "COLUMN_COMMENT",
            "ORDINAL_POSITION",
        )
        missing = [name for name in required if name not in index]
        if missing:
            raise ValueError(
                "schema dump column pages must include " + ", ".join(required)
            )
        extra_index = index.get("EXTRA")
        for row in rows:
            table_name = row[index["TABLE_NAME"]]
            if current is None or current["table_name"] != table_name:
                current = start_table(table_name)
            column = {
                "name": row[index["COLUMN_NAME"]],
                "type": row[index["COLUMN_TYPE"]],
                "nullable": row[index["IS_NULLABLE"]],
                "default": row[index["COLUMN_DEFAULT"]],
                "comment": row[index["COLUMN_COMMENT"]],
                "ordinal": row[index["ORDINAL_POSITION"]],
            }
            if extra_index is not None:
                column["extra"] = row[extra_index]
            current["columns"].append(column)
        return grouped

    for row in rows:
        table_name = row[index["TABLE_NAME"]]
        meta = {
            key.lower(): row[position]
            for key, position in index.items()
            if key != "TABLE_NAME"
        }
        if table_meta and table_name in table_meta:
            meta = {**table_meta[table_name], **meta}
        grouped.append({"table_name": table_name, **meta})
    return grouped


def table_dump_meta(
    columns: list[str], rows: list[list[Any]]
) -> dict[str, dict[str, Any]]:
    """Map inspect_catalog(kind=tables) rows to dump_schema table objects."""
    index = {name: position for position, name in enumerate(columns)}
    if "TABLE_NAME" not in index:
        raise ValueError("table pages must include TABLE_NAME")
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        mapped[row[index["TABLE_NAME"]]] = {
            field.lower(): row[position]
            for field, position in index.items()
            if field != "TABLE_NAME"
        }
    return mapped


def search_tables_sql(schema: str, filters: str, search: str) -> str:
    if not isinstance(search, str) or not search.strip() or len(search) > 128:
        raise ValueError("search must be non-empty text up to 128 characters")
    # No quoting or SQL-mode-dependent escaping of user text.
    literal = f"CONVERT(X'{search.encode('utf-8').hex()}' USING utf8mb4)"
    parts = []
    for field, catalog in (
        ("TABLE_NAME", "TABLES"),
        ("TABLE_COMMENT", "TABLES"),
        ("COLUMN_NAME", "COLUMNS"),
        ("COLUMN_COMMENT", "COLUMNS"),
    ):
        column = "COLUMN_NAME" if catalog == "COLUMNS" else "NULL"
        parts.append(
            f"SELECT TABLE_NAME, '{field}' AS MATCH_FIELD, {column} AS COLUMN_NAME, "
            f"{field} AS MATCH_TEXT FROM information_schema.{catalog} "
            f"WHERE TABLE_SCHEMA = {schema}{filters} AND LOCATE(LOWER({literal}), LOWER({field})) > 0"
        )
    return " UNION ALL ".join(parts) + " ORDER BY TABLE_NAME, MATCH_FIELD, COLUMN_NAME"


def detailed_schema_sql(schema: str, filters: str) -> str:
    """Flat union: each column/index member/FK is pageable without GROUP_CONCAT loss."""
    fields = (
        "KIND",
        "TABLE_NAME",
        "OBJECT_NAME",
        "ORDINAL_POSITION",
        "COLUMN_NAME",
        "COLUMN_TYPE",
        "IS_NULLABLE",
        "COLUMN_DEFAULT",
        "EXTRA",
        "COMMENT",
        "NON_UNIQUE",
        "INDEX_TYPE",
        "SUB_PART",
        "REFERENCED_TABLE_SCHEMA",
        "REFERENCED_TABLE_NAME",
        "REFERENCED_COLUMN_NAME",
    )
    projections = [
        (
            "TABLES",
            {
                "KIND": "'table'",
                "OBJECT_NAME": "TABLE_NAME",
                "COMMENT": "TABLE_COMMENT",
            },
        ),
        (
            "COLUMNS",
            {
                "KIND": "'column'",
                "OBJECT_NAME": "COLUMN_NAME",
                "ORDINAL_POSITION": "ORDINAL_POSITION",
                "COLUMN_NAME": "COLUMN_NAME",
                "COLUMN_TYPE": "COLUMN_TYPE",
                "IS_NULLABLE": "IS_NULLABLE",
                "COLUMN_DEFAULT": "COLUMN_DEFAULT",
                "EXTRA": "EXTRA",
                "COMMENT": "COLUMN_COMMENT",
            },
        ),
        (
            "STATISTICS",
            {
                "KIND": "'index'",
                "OBJECT_NAME": "INDEX_NAME",
                "ORDINAL_POSITION": "SEQ_IN_INDEX",
                "COLUMN_NAME": "COLUMN_NAME",
                "NON_UNIQUE": "NON_UNIQUE",
                "INDEX_TYPE": "INDEX_TYPE",
                "SUB_PART": "SUB_PART",
                "COMMENT": "INDEX_COMMENT",
            },
        ),
        (
            "KEY_COLUMN_USAGE",
            {
                "KIND": "'foreign_key'",
                "OBJECT_NAME": "CONSTRAINT_NAME",
                "ORDINAL_POSITION": "ORDINAL_POSITION",
                "COLUMN_NAME": "COLUMN_NAME",
                "REFERENCED_TABLE_SCHEMA": "REFERENCED_TABLE_SCHEMA",
                "REFERENCED_TABLE_NAME": "REFERENCED_TABLE_NAME",
                "REFERENCED_COLUMN_NAME": "REFERENCED_COLUMN_NAME",
            },
        ),
    ]
    parts = []
    for catalog, values in projections:
        values["TABLE_NAME"] = "TABLE_NAME"
        projection = ", ".join(
            f"{values.get(field, 'NULL')} AS `{field}`" for field in fields
        )
        extra = (
            " AND REFERENCED_TABLE_NAME IS NOT NULL"
            if catalog == "KEY_COLUMN_USAGE"
            else ""
        )
        parts.append(
            f"SELECT {projection} FROM information_schema.{catalog} WHERE TABLE_SCHEMA = {schema}{filters}{extra}"
        )
    return (
        " UNION ALL ".join(parts)
        + " ORDER BY TABLE_NAME, KIND, OBJECT_NAME, ORDINAL_POSITION, COLUMN_NAME"
    )
