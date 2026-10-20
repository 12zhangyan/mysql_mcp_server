"""Opt-in localhost-only acceptance tests. No CREATE/INSERT/UPDATE/DELETE.

Set MYSQL_MCP_LOCAL_TESTS=1 and provide MYSQL_USER/MYSQL_PASSWORD in the process
environment. Credentials and existing database contents are never printed.
"""

import json
import asyncio
import os
import re
import sys

import mysql.connector
import pytest

from mysql_mcp_server.server import _bounded_query_sql, call_tool

pytestmark = pytest.mark.skipif(
    os.getenv("MYSQL_MCP_LOCAL_TESTS") != "1",
    reason="Opt-in local MySQL acceptance tests",
)

SOURCE = "WITH data AS (SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6) "
MASKING_SOURCE = (
    "WITH mobile_devices AS (SELECT 7 AS id, 'active' AS status, "
    "5 AS token_count, '2026-10-14' AS PasswordUpdatedAt, 1 AS EmailEnabled, "
    "'synthetic-sensitive-value' AS password UNION ALL "
    "SELECT 8, 'inactive', 2, '2026-10-13', 0, 'synthetic-sensitive-value') "
)
QUERIES = [
    SOURCE + "SELECT n FROM data ORDER BY n",
    SOURCE + "SELECT n FROM data ORDER BY n DESC LIMIT 4 OFFSET 1",
    SOURCE + "SELECT n FROM data ORDER BY n LIMIT 0",
    SOURCE + "SELECT n FROM data ORDER BY n LIMIT 2, 3",
    SOURCE
    + "SELECT n FROM data WHERE n < 3 UNION ALL SELECT n FROM data WHERE n > 4 ORDER BY n DESC",
    SOURCE
    + "SELECT n FROM (SELECT n FROM data ORDER BY n DESC LIMIT 4) AS nested ORDER BY n",
    SOURCE
    + "SELECT MOD(n, 2) AS g, SUM(n) AS total FROM data GROUP BY MOD(n, 2) ORDER BY g",
    SOURCE + "SELECT n, ROW_NUMBER() OVER (ORDER BY n DESC) AS rn FROM data ORDER BY n",
    SOURCE
    + "SELECT n FROM data WHERE n IN (SELECT n FROM data WHERE n > 2) ORDER BY n LIMIT 3",
    "SELECT 'a\\\\b' AS text_value, '中文' AS label UNION ALL SELECT 'x', '调拨' ORDER BY text_value",
]


@pytest.fixture
def local_db(monkeypatch):
    # The opt-in cannot redirect this fixture to a remote host or SSH profile.
    monkeypatch.setenv("MYSQL_HOST", "127.0.0.1")
    monkeypatch.setenv("MYSQL_PORT", "3306")
    monkeypatch.setenv("MYSQL_SSL_MODE", "DISABLED")
    monkeypatch.setenv("MYSQL_POOL_SIZE", "0")
    monkeypatch.setenv("MYSQL_ALLOWED_DATABASES", "")
    monkeypatch.setenv("MYSQL_ALLOW_SYSTEM_DATABASES", "false")
    monkeypatch.setenv("MYSQL_AUDIT_ENABLED", "false")
    monkeypatch.setenv("MYSQL_SQL_MODE", "TRADITIONAL")
    monkeypatch.delenv("MYSQL_DATABASE", raising=False)
    monkeypatch.delenv("MYSQL_MASK_COLUMNS", raising=False)
    monkeypatch.delenv("MYSQL_SSH_HOST", raising=False)
    monkeypatch.setenv("MYSQL_SSH_ENABLE", "false")
    try:
        conn = mysql.connector.connect(
            host="127.0.0.1",
            port=3306,
            user=os.environ["MYSQL_USER"],
            password=os.environ["MYSQL_PASSWORD"],
            connection_timeout=5,
            sql_mode="TRADITIONAL",
            charset="utf8mb4",
        )
    except Exception:
        pytest.fail(
            "Local MySQL connection failed; connection details suppressed",
            pytrace=False,
        )
    cursor = conn.cursor()
    cursor.execute("SET SESSION TRANSACTION READ ONLY")
    cursor.execute("START TRANSACTION READ ONLY")
    try:
        yield conn, cursor
    finally:
        conn.rollback()
        cursor.close()
        conn.close()


@pytest.mark.parametrize(
    "query", QUERIES, ids=[f"shape-{i}" for i in range(len(QUERIES))]
)
def test_mysql_paging_rewrite_matches_original_result(local_db, query):
    _, cursor = local_db
    cursor.execute(query)
    original = cursor.fetchall()
    for offset in (0, 1, 3, 6, 10):
        rewritten = _bounded_query_sql(query, row_limit=2, page_offset=offset)
        assert rewritten is not None
        cursor.execute(rewritten)
        actual = cursor.fetchall()
        assert actual == original[offset : offset + 3]


async def test_real_byte_limited_pages_deliver_all_cte_rows(local_db):
    query = SOURCE + "SELECT n, REPEAT('调拨', 400) AS description FROM data ORDER BY n"
    offset, ids = 0, []
    for _ in range(10):
        result = await call_tool(
            "execute_sql",
            {
                "query": query,
                "result_format": "json",
                "max_response_bytes": 4096,
                "offset": offset,
            },
        )
        assert not getattr(result, "isError", False)
        assert len(result[0].text.encode("utf-8")) <= 4096
        page = json.loads(result[0].text)
        ids.extend(row[0] for row in page["rows"])
        if not page["truncated"]:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert ids == [1, 2, 3, 4, 5, 6]


async def test_real_metadata_detail_and_search_pages(local_db, monkeypatch):
    _, cursor = local_db
    cursor.execute(
        "SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys') ORDER BY TABLE_SCHEMA, TABLE_NAME LIMIT 1"
    )
    target = cursor.fetchone()
    if not target:
        pytest.skip("No existing user table for read-only metadata acceptance")
    database, table = target
    if not re.fullmatch(r"[A-Za-z0-9_$]+", database) or not re.fullmatch(
        r"[A-Za-z0-9_$]+", table
    ):
        pytest.skip("First existing table is outside supported identifier syntax")
    monkeypatch.setenv("MYSQL_DATABASE", database)
    params = {
        "database": database,
        "table_names": [table],
        "detail": True,
        "result_format": "json",
    }
    first = await call_tool("get_schema_info", params)
    assert not getattr(first, "isError", False)
    baseline = json.loads(first[0].text)
    assert baseline["rows"]
    collected, offset = [], 0
    for _ in range(1000):
        result = await call_tool(
            "get_schema_info", {**params, "max_rows": 2, "offset": offset}
        )
        assert not getattr(result, "isError", False)
        page = json.loads(result[0].text)
        collected.extend(page["rows"])
        if not page["truncated"]:
            break
        offset = page["next_offset"]
    assert collected[: len(baseline["rows"])] == baseline["rows"]
    assert {row[0] for row in collected} >= {"table", "column"}
    for search in (table, "调拨", "%' OR 1=1 --"):
        result = await call_tool(
            "list_tables",
            {"database": database, "search": search, "result_format": "json"},
        )
        assert not getattr(result, "isError", False)
        payload = json.loads(result[0].text)
        assert payload["columns"] == [
            "TABLE_NAME",
            "MATCH_FIELD",
            "COLUMN_NAME",
            "MATCH_TEXT",
        ]
        if search == table:
            assert any(row[0] == table for row in payload["rows"])


async def test_real_missing_table_and_column_have_recovery_hints(local_db):
    for query, code in [
        (
            "SELECT n FROM mysql.__mcp_missing_table_7e7b4d",
            "TABLE_NOT_FOUND",
        ),
        (SOURCE + "SELECT missing_column FROM data", "COLUMN_NOT_FOUND"),
    ]:
        # The metadata reference here is only to trigger MySQL 1146 locally.
        from mysql_mcp_server.errors import QueryFailure
        from mysql_mcp_server.server import execute_query

        with pytest.raises(QueryFailure) as error:
            await execute_query(query, internal=True)
        assert error.value.payload["code"] == code
        assert error.value.payload["retryable"] is False


def test_chinese_comment_search_and_literal_wildcards_on_mysql(local_db):
    from mysql_mcp_server.metadata import search_tables_sql

    _, cursor = local_db
    source = (
        "WITH fixture_tables AS ("
        "SELECT 'app' AS TABLE_SCHEMA, 'orders' AS TABLE_NAME, '调拨单' AS TABLE_COMMENT "
        "UNION ALL SELECT 'outside', 'secret_table', '调拨单'), "
        "fixture_columns AS ("
        "SELECT 'app' AS TABLE_SCHEMA, 'orders' AS TABLE_NAME, 'operator' AS COLUMN_NAME, '调拨人' AS COLUMN_COMMENT) "
    )
    for keyword, expected in (
        ("调拨", 2),
        ("orders", 1),
        ("operator", 1),
        ("%", 0),
        ("' OR 1=1 --", 0),
    ):
        sql = (
            search_tables_sql("'app'", "", keyword)
            .replace("information_schema.TABLES", "fixture_tables")
            .replace("information_schema.COLUMNS", "fixture_columns")
        )
        cursor.execute(_bounded_query_sql(source + sql, row_limit=10, page_offset=0))
        rows = cursor.fetchall()
        assert len(rows) == expected
        assert all(row[0] == "orders" for row in rows)


async def test_real_queries_resume_after_waiting_on_one_connection(
    local_db, monkeypatch
):
    from mysql_mcp_server.config import load_connection_registry
    from mysql_mcp_server.runtime import query_admission

    monkeypatch.setenv("MYSQL_MAX_CONCURRENT_QUERIES", "1")
    monkeypatch.setenv("MYSQL_QUEUE_TIMEOUT_MS", "5000")
    profile = load_connection_registry().get()
    holder = await query_admission.acquire(profile)
    tasks = [
        asyncio.create_task(
            call_tool(
                "execute_sql",
                {
                    "query": SOURCE + "SELECT SUM(n) AS total FROM data",
                    "result_format": "json",
                },
            )
        )
        for _ in range(6)
    ]
    try:
        await asyncio.sleep(0.03)
    finally:
        holder.release_pending()
    responses = await asyncio.gather(*tasks)
    for response in responses:
        assert not getattr(response, "isError", False)
        payload = json.loads(response[0].text)
        assert payload["rows"] == [["21"]]
        assert payload["queue_wait_ms"] > 0
    assert not query_admission._active and not query_admission._waiting


@pytest.mark.parametrize("configured", [False, True])
async def test_mcp_stdio_exposes_new_contract_and_safe_errors(local_db, configured):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    environment = dict(os.environ)
    environment.update(
        {
            "MCP_TRANSPORT": "stdio",
            "MYSQL_HOST": "127.0.0.1",
            "MYSQL_PORT": "3306",
            "MYSQL_DATABASE": "",
            "MYSQL_PROFILES_FILE": "",
            "MYSQL_CONNECTIONS_FILE": "",
            "MYSQL_SSH_ENABLE": "false",
            "MYSQL_SSL_MODE": "DISABLED",
        }
    )
    if configured:
        environment["MYSQL_MASK_COLUMNS"] = "password"
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "mysql_mcp_server"], env=environment
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert "search" in tools["list_tables"].inputSchema["properties"]
            assert "detail" in tools["get_schema_info"].inputSchema["properties"]
            success = await client.call_tool(
                "execute_sql", {"query": "SELECT 1 AS n", "result_format": "compact"}
            )
            assert not success.isError
            assert json.loads(success.content[0].text.splitlines()[0])["row_count"] == 1
            wide_query = (
                "WITH source AS (SELECT 'synthetic-sensitive-value' AS password, "
                + ", ".join(f"{index} AS value_{index}" for index in range(80))
                + ") SELECT password AS harmless, "
                + ", ".join(f"value_{index}" for index in range(80))
                + " FROM source"
            )
            wide = await client.call_tool(
                "execute_sql", {"query": wide_query, "result_format": "json"}
            )
            assert not wide.isError
            wide_payload = json.loads(wide.content[0].text)
            expected_value = "[REDACTED]" if configured else "synthetic-sensitive-value"
            assert wide_payload["rows"] == [[expected_value, *range(80)]]
            assert wide_payload["masked_columns"] == (
                ["harmless"] if configured else []
            )
            statistics = await client.call_tool(
                "execute_sql",
                {
                    "query": MASKING_SOURCE
                    + "SELECT COUNT(password) AS n FROM mobile_devices",
                    "result_format": "json",
                },
            )
            assert not statistics.isError
            statistics_payload = json.loads(statistics.content[0].text)
            assert statistics_payload["rows"] == [[2]]
            assert statistics_payload["masked_columns"] == []
            failure = await client.call_tool(
                "execute_sql", {"query": "SELECT missing_column"}
            )
            assert (
                failure.isError
                and failure.structuredContent["code"] == "COLUMN_NOT_FOUND"
            )
            assert json.loads(failure.content[0].text) == failure.structuredContent


@pytest.mark.parametrize("union", [False, True])
@pytest.mark.parametrize("configured", [False, True])
async def test_real_wide_cte_uses_only_configured_masking(
    local_db, monkeypatch, union, configured
):
    if configured:
        monkeypatch.setenv("MYSQL_MASK_COLUMNS", "password")
    public = [f"value_{index}" for index in range(80)]
    query = (
        "WITH source AS (SELECT 'synthetic-sensitive-value' AS password, "
        + ", ".join(f"{index} AS {name}" for index, name in enumerate(public))
        + ") SELECT password AS harmless, "
        + ", ".join(public)
        + " FROM source"
    )
    if union:
        query += " UNION ALL SELECT password, " + ", ".join(public) + " FROM source"
    response = await call_tool("execute_sql", {"query": query, "result_format": "json"})

    assert not getattr(response, "isError", False)
    payload = json.loads(response[0].text)
    expected_value = "[REDACTED]" if configured else "synthetic-sensitive-value"
    assert payload["rows"] == [[expected_value, *range(80)]] * (2 if union else 1)
    assert payload["masked_columns"] == (["harmless"] if configured else [])


@pytest.mark.parametrize("initial_size,new_size", [(1, 2), (2, 1)])
def test_real_pool_resize_enforces_new_capacity(local_db, initial_size, new_size):
    from dataclasses import replace

    from mysql_mcp_server.config import load_connection_registry, build_connector_config
    from mysql_mcp_server.runtime import ConnectionPoolManager, TunnelEndpoint

    manager = ConnectionPoolManager()
    profile = replace(load_connection_registry().get(), pool_size=initial_size)
    endpoint = TunnelEndpoint("127.0.0.1", 3306)
    config = build_connector_config(profile)
    borrowed = []
    try:
        first = manager.get_connection(profile, endpoint, config)
        first.close()
        resized = replace(profile, pool_size=new_size)
        for _ in range(new_size):
            connection = manager.get_connection(resized, endpoint, config)
            borrowed.append(connection)
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute("START TRANSACTION READ ONLY")
                cursor.execute("SELECT @@transaction_read_only, 1")
                assert cursor.fetchone() == (1, 1)
        with pytest.raises(mysql.connector.errors.PoolError):
            manager.get_connection(resized, endpoint, config)
    finally:
        for connection in borrowed:
            connection.rollback()
            connection.close()
        manager.clear()


@pytest.mark.parametrize(
    "projection,expected,masked_columns",
    [
        (
            "SELECT id, status, token_count, PasswordUpdatedAt, EmailEnabled, "
            "password AS harmless FROM mobile_devices ORDER BY id",
            [
                [7, "active", 5, "2026-10-14", 1, "[REDACTED]"],
                [8, "inactive", 2, "2026-10-13", 0, "[REDACTED]"],
            ],
            ["harmless"],
        ),
        ("SELECT COUNT(password) AS configured_count FROM mobile_devices", [[2]], []),
        (
            "SELECT COUNT(DISTINCT password) AS unique_count FROM mobile_devices",
            [[1]],
            [],
        ),
        (
            "SELECT password AS harmless, COUNT(*) AS n FROM mobile_devices GROUP BY password",
            [["[REDACTED]", 2]],
            ["harmless"],
        ),
    ],
)
async def test_real_business_metadata_and_counts_remain_visible(
    local_db, monkeypatch, projection, expected, masked_columns
):
    monkeypatch.setenv("MYSQL_MASK_COLUMNS", "password")
    response = await call_tool(
        "execute_sql", {"query": MASKING_SOURCE + projection, "result_format": "json"}
    )

    assert not getattr(response, "isError", False)
    payload = json.loads(response[0].text)
    assert payload["rows"] == expected
    assert payload["masked_columns"] == masked_columns


async def test_real_default_preserves_fields_and_nested_json(local_db):
    response = await call_tool(
        "execute_sql",
        {
            "query": "SELECT 'synthetic-password' AS password, "
            "'synthetic-token' AS access_token, 'user@example.test' AS email, "
            '\'{"password":"synthetic-json-value",'
            '"nested":[{"token":"synthetic-token"}]}\' AS config',
            "result_format": "json",
        },
    )
    assert not getattr(response, "isError", False)
    payload = json.loads(response[0].text)
    assert payload["rows"][0][:3] == [
        "synthetic-password",
        "synthetic-token",
        "user@example.test",
    ]
    assert json.loads(payload["rows"][0][3]) == {
        "password": "synthetic-json-value",
        "nested": [{"token": "synthetic-token"}],
    }
    assert payload["masked_columns"] == []


async def test_real_combined_session_setup_enforces_read_only_and_timeout(local_db):
    from unittest.mock import patch

    from mysql_mcp_server import server

    decisions = []
    original = server._supports_combined_session_setup

    def spy(connection_object):
        decisions.append(original(connection_object))
        return decisions[-1]

    with patch.object(server, "_supports_combined_session_setup", side_effect=spy):
        result = await call_tool(
            "execute_sql",
            {
                "query": "SELECT @@SESSION.transaction_read_only AS ro, "
                "@@SESSION.max_execution_time AS met",
                "timeout_ms": 4321,
                "result_format": "json",
            },
        )

    payload = json.loads(result[0].text)
    assert decisions == [True]
    assert payload["rows"] == [[1, 4321]]
