"""Fixed, scoped metadata SQL. User search text is a UTF-8 hex literal."""

from __future__ import annotations

import re


def table_filter(names: list[str] | None) -> str:
    if names is None:
        return ""
    if (
        not isinstance(names, list)
        or not 1 <= len(names) <= 20
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_$]+", name)
            for name in names
        )
    ):
        raise ValueError(
            "table_names must contain 1 to 20 bare table names in the selected database"
        )
    return (
        " AND TABLE_NAME IN ("
        + ",".join(f"'{name}'" for name in sorted(set(names)))
        + ")"
    )


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
