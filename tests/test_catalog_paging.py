"""Catalog pagination, batch filters, and dump_schema grouping."""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock, patch

import pytest

from mysql_mcp_server.metadata import (MAX_TABLE_NAMES, catalog_name_filter,
                                       group_schema_dump,
                                       parse_dump_schema_kinds, table_filter)
from mysql_mcp_server.server import _catalog_query, call_tool

COLUMN_FIELDS = [
    "TABLE_NAME",
    "COLUMN_NAME",
    "ORDINAL_POSITION",
    "COLUMN_TYPE",
    "IS_NULLABLE",
    "COLUMN_DEFAULT",
    "EXTRA",
    "COLUMN_COMMENT",
]
TABLE_FIELDS = ["TABLE_NAME", "TABLE_TYPE", "ENGINE", "TABLE_ROWS", "TABLE_COMMENT"]
LIMIT_RE = re.compile(r"LIMIT\s+(\d+)(?:\s+OFFSET\s+(\d+))?", re.I)


def catalog_rows(table_count: int = 220, columns_per_table: int = 12):
    rows = []
    for table_index in range(table_count):
        table = f"t{table_index:03d}"
        for ordinal in range(1, columns_per_table + 1):
            rows.append(
                (
                    table,
                    f"c{ordinal:02d}",
                    ordinal,
                    "int",
                    "YES",
                    None,
                    "",
                    f"col {ordinal}",
                )
            )
    return rows


def table_rows_from_columns(rows):
    seen = []
    names = set()
    for table_name, *_rest in rows:
        if table_name in names:
            continue
        names.add(table_name)
        seen.append((table_name, "BASE TABLE", "InnoDB", 0, f"{table_name} comment"))
    return seen


def apply_table_predicate(sql: str, source: list[tuple]) -> list[tuple]:
    equal = re.search(r"TABLE_NAME = '([A-Za-z0-9_$]+)'", sql)
    if equal:
        return [row for row in source if row[0] == equal.group(1)]
    match_in = re.search(r"TABLE_NAME IN \(([^)]+)\)", sql)
    if match_in:
        allowed = {part.strip().strip("'") for part in match_in.group(1).split(",")}
        return [row for row in source if row[0] in allowed]
    like = re.search(r"TABLE_NAME LIKE '([^']+)'", sql)
    if like:
        converted = []
        raw = like.group(1)
        index = 0
        while index < len(raw):
            if raw[index] == "\\" and index + 1 < len(raw):
                converted.append(re.escape(raw[index + 1]))
                index += 2
                continue
            if raw[index] == "%":
                converted.append(".*")
            elif raw[index] == "_":
                converted.append(".")
            else:
                converted.append(re.escape(raw[index]))
            index += 1
        matcher = re.compile("^" + "".join(converted) + "$")
        return [row for row in source if matcher.match(str(row[0]))]
    return source


def connection_applying_sql_pages(column_rows, table_rows):
    cursor = MagicMock()
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor

    def execute(sql):
        if "information_schema.COLUMNS" in sql:
            source = apply_table_predicate(sql, column_rows)
            fields = COLUMN_FIELDS
        elif "information_schema.TABLES" in sql:
            source = apply_table_predicate(sql, table_rows)
            fields = TABLE_FIELDS
        else:
            return
        match = LIMIT_RE.search(sql)
        offset = int(match.group(2) or 0) if match else 0
        limit = int(match.group(1)) if match else len(source)
        page = source[offset : offset + limit]
        cursor.description = [(field,) for field in fields]
        cursor.fetchall.return_value = page
        cursor.fetchmany.return_value = page

    cursor.execute.side_effect = execute
    return connection, cursor


async def _json_tool(name, arguments):
    result = await call_tool(name, {**arguments, "result_format": "json"})
    assert not getattr(result, "isError", False), result[0].text
    return json.loads(result[0].text), result


def test_catalog_sql_is_stably_ordered_and_scoped():
    sql = _catalog_query(
        "columns",
        "hl_ubp_ex",
        catalog_name_filter(table_pattern="hl_*"),
    )
    assert "FROM information_schema.COLUMNS" in sql
    assert "TABLE_SCHEMA = 'hl_ubp_ex'" in sql
    assert "TABLE_NAME LIKE 'hl\\_%'" in sql
    assert "ESCAPE '\\\\'" in sql
    assert sql.endswith("ORDER BY TABLE_NAME, ORDINAL_POSITION, COLUMN_NAME")
    with pytest.raises(ValueError, match="100"):
        table_filter(["x"] * (MAX_TABLE_NAMES + 1))
    assert "IN (" in table_filter(["z", "a"] + [f"t{i}" for i in range(98)])
    with pytest.raises(ValueError, match="only one"):
        catalog_name_filter(table="orders", table_pattern="ord*")
    with pytest.raises(ValueError, match="inspect_catalog"):
        parse_dump_schema_kinds(["indexes"])


def test_group_schema_dump_keeps_every_delivered_column():
    rows = [list(row) for row in catalog_rows(2, 3)]
    grouped = group_schema_dump(
        COLUMN_FIELDS,
        rows,
        table_meta={"t000": {"engine": "InnoDB"}},
        include_columns=True,
    )
    assert [entry["table_name"] for entry in grouped] == ["t000", "t001"]
    assert grouped[0]["engine"] == "InnoDB"
    assert [column["name"] for column in grouped[0]["columns"]] == ["c01", "c02", "c03"]
    assert grouped[0]["columns"][0]["ordinal"] == 1


@pytest.mark.asyncio
async def test_inspect_catalog_pages_a_large_database_without_silent_truncation():
    columns = catalog_rows(220, 12)
    tables = table_rows_from_columns(columns)
    connection, cursor = connection_applying_sql_pages(columns, tables)
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ):
        first, _ = await _json_tool(
            "inspect_catalog", {"kind": "columns", "database": "app"}
        )
        assert first["truncated"] is True
        assert first["next_offset"] == 500
        assert first["returned_rows"] == 500
        assert first["rows"][0][:2] != columns[500][:2]

        shifted, _ = await _json_tool(
            "inspect_catalog",
            {"kind": "columns", "database": "app", "offset": 500},
        )
        assert shifted["rows"][0][:2] == list(columns[500][:2])
        assert shifted["rows"] != first["rows"]

        repeat, _ = await _json_tool(
            "inspect_catalog",
            {"kind": "columns", "database": "app", "offset": 500},
        )
        assert repeat["rows"] == shifted["rows"]

        collected = []
        offset = 0
        for _ in range(20):
            page, _ = await _json_tool(
                "inspect_catalog",
                {"kind": "columns", "database": "app", "offset": offset},
            )
            collected.extend(tuple(row[:2]) for row in page["rows"])
            if not page["truncated"]:
                assert page["next_offset"] is None
                break
            assert page["next_offset"] > offset
            offset = page["next_offset"]
        else:
            raise AssertionError("catalog paging did not terminate")

        expected = {(row[0], row[1]) for row in columns}
        assert len(collected) == len(columns) == 2640
        assert set(collected) == expected

        legacy, _ = await _json_tool(
            "inspect_catalog", {"kind": "columns", "database": "app"}
        )
        assert legacy["truncated"] is True
        assert legacy["rows"] == first["rows"]

        named, _ = await _json_tool(
            "inspect_catalog",
            {
                "kind": "columns",
                "database": "app",
                "table_name": "t010",
                "max_rows": 100,
            },
        )
        assert {row[0] for row in named["rows"]} == {"t010"}
        assert named["truncated"] is False
        assert len(named["rows"]) == 12

        patterned, _ = await _json_tool(
            "inspect_catalog",
            {
                "kind": "columns",
                "database": "app",
                "table_pattern": "t01?",
                "max_rows": 500,
            },
        )
        assert patterned["truncated"] is False
        assert {row[0] for row in patterned["rows"]} == {f"t01{i}" for i in range(10)}

        batched, _ = await _json_tool(
            "inspect_catalog",
            {
                "kind": "columns",
                "database": "app",
                "table_names": ["t000", "t010", "t219"],
                "max_rows": 500,
            },
        )
        assert batched["truncated"] is False
        assert {row[0] for row in batched["rows"]} == {"t000", "t010", "t219"}
        assert "LIMIT 501" in cursor.execute.call_args.args[0]

        schema_page, _ = await _json_tool(
            "get_schema_info",
            {"database": "app", "table_pattern": "t000", "max_rows": 5},
        )
        assert schema_page["truncated"] is True
        assert schema_page["next_offset"] == 5

        dumped, _ = await _json_tool(
            "dump_schema",
            {"database": "app", "max_rows": 20, "offset": 0},
        )
        assert dumped["truncated"] is True
        assert dumped["next_offset"] == 20
        assert dumped["returned_rows"] == 20
        assert dumped["tables"][0]["table_name"] == "t000"
        assert dumped["tables"][0]["columns"][0]["name"] == "c01"
        assert dumped["tables"][0]["engine"] == "InnoDB"
        delivered = sum(len(table["columns"]) for table in dumped["tables"])
        assert delivered == 20


@pytest.mark.asyncio
async def test_table_names_limit_is_enforced_not_silently_truncated():
    with patch("mysql_mcp_server.server._open_connection") as opened:
        rejected = await call_tool(
            "inspect_catalog",
            {
                "kind": "columns",
                "database": "app",
                "table_names": [f"t{i}" for i in range(101)],
            },
        )
        accepted_sql = []

        async def capture(query, **kwargs):
            accepted_sql.append(query)
            return []

        with patch("mysql_mcp_server.server.run_query", side_effect=capture):
            accepted = await call_tool(
                "inspect_catalog",
                {
                    "kind": "columns",
                    "database": "app",
                    "table_names": [f"t{i}" for i in range(100)],
                },
            )
    assert rejected.isError
    opened.assert_not_called()
    assert accepted == []
    assert accepted_sql[0].count("'t") == 100


@pytest.mark.asyncio
async def test_execute_sql_still_rejects_information_schema():
    with patch("mysql_mcp_server.server._open_connection") as opened:
        result = await call_tool(
            "execute_sql",
            {
                "query": "SELECT * FROM information_schema.COLUMNS",
                "database": "app",
            },
        )
    assert result.isError
    opened.assert_not_called()
    assert "inspect_catalog" in result[0].text


@pytest.mark.asyncio
async def test_inspect_catalog_indexes_and_views_use_the_same_offset_contract():
    connection, cursor = connection_applying_sql_pages([], [])
    cursor.execute.side_effect = None
    cursor.description = [("TABLE_NAME",), ("INDEX_NAME",)]
    cursor.fetchall.return_value = [
        ("t000", "PRIMARY"),
        ("t000", "idx_a"),
        ("t001", "PRIMARY"),
    ]
    cursor.fetchmany.return_value = cursor.fetchall.return_value
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ):
        payload, _ = await _json_tool(
            "inspect_catalog",
            {"kind": "indexes", "database": "app", "max_rows": 2, "offset": 1},
        )
    sql = cursor.execute.call_args.args[0]
    assert "FROM information_schema.STATISTICS" in sql
    assert "LIMIT 3 OFFSET 1" in sql
    assert payload["truncated"] is True
    assert payload["next_offset"] == 3
    assert payload["returned_rows"] == 2
