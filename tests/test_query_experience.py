"""Behavioral coverage for discovery, bounded results and recoverable failures."""

import asyncio
import json
import threading
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from mysql.connector import ProgrammingError

from mysql_mcp_server.config import ConnectionProfile, load_connection_registry
from mysql_mcp_server.errors import QueryFailure
from mysql_mcp_server.metadata import (
    detailed_schema_sql,
    search_tables_sql,
    table_filter,
)
from mysql_mcp_server.results import QueryResult
from mysql_mcp_server.runtime import QueryAdmission
from mysql_mcp_server.server import call_tool, execute_query, list_tools
from mysql_mcp_server.sql_guard import (
    validate_database_access,
    validate_function_safety,
    validate_read_only_query,
)


def connection_with(rows, columns):
    cursor = MagicMock()
    cursor.description = [(column,) for column in columns]
    cursor.fetchall.return_value = rows
    cursor.fetchmany.return_value = rows
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    return connection, cursor


@pytest.mark.parametrize(
    "errno,code",
    [(1146, "TABLE_NOT_FOUND"), (1054, "COLUMN_NOT_FOUND"), (1064, "SQL_SYNTAX_ERROR")],
)
@pytest.mark.parametrize("phase", ["execute", "fetch"])
async def test_sql_failure_is_actionable_safe_and_not_retried(
    errno, code, phase, caplog
):
    conn, cursor = connection_with([], ["id"])
    exc = ProgrammingError(
        msg="private-password private-host SQL-literal", errno=errno, sqlstate="42S02"
    )
    if phase == "fetch":
        cursor.fetchall.side_effect = exc
    else:
        cursor.execute.side_effect = lambda sql: (
            (_ for _ in ()).throw(exc) if sql.startswith("SELECT") else None
        )
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(conn, {"database": "app"}),
    ) as opened:
        result = await call_tool(
            "execute_sql", {"query": "SELECT id FROM absent", "database": "app"}
        )
    payload = json.loads(result[0].text)
    assert result.isError and result.structuredContent == payload
    assert payload["code"] == code and payload["phase"] == phase
    assert payload["retryable"] is False and payload["next_action"]
    assert payload["database"] == "app" and payload["connection"] == "default"
    assert "private-" not in result[0].text + caplog.text
    assert "SQL-literal" not in result[0].text + caplog.text
    opened.assert_called_once()


async def test_discovery_tools_expose_continuation_and_batch_arguments():
    tools = {tool.name: tool for tool in await list_tools()}
    for name in ("get_schema_info", "inspect_catalog"):
        assert {"offset", "max_rows", "table_names", "max_response_bytes"} <= tools[
            name
        ].inputSchema["properties"].keys()
    assert "search" in tools["list_tables"].inputSchema["properties"]
    assert "do not guess" in tools["execute_sql"].description


@pytest.mark.parametrize(
    "tool,extra",
    [("get_schema_info", {"detail": True}), ("inspect_catalog", {"kind": "columns"})],
)
async def test_metadata_batch_and_paging_reach_execution(tool, extra):
    conn, cursor = connection_with([("a",), ("b",), ("c",)], ["TABLE_NAME"])
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(conn, {"database": "app"}),
    ):
        result = await call_tool(
            tool,
            {
                "database": "app",
                "table_names": ["orders", "items"],
                "max_rows": 2,
                "offset": 4,
                "result_format": "json",
                **extra,
            },
        )
    payload = json.loads(result[0].text)
    assert payload["next_offset"] == 6 and len(payload["rows"]) == 2
    sql = cursor.execute.call_args.args[0]
    assert "'items', 'orders'" in sql and "LIMIT 3 OFFSET 4" in sql
    if extra.get("detail"):
        assert (
            "STATISTICS" in sql and "KEY_COLUMN_USAGE" in sql and "COLUMN_TYPE" in sql
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"table_names": []},
        {"table_names": ["x"] * 21},
        {"table_names": ["other.users"]},
        {"table_names": ["x' OR 1=1 --"]},
        {"table_names": ["users"], "table_name": "users"},
        {"table_name": "other.users", "database": "app"},
        {"detail": "true"},
    ],
)
async def test_invalid_metadata_scope_fails_before_connecting(arguments):
    with patch("mysql_mcp_server.server._open_connection") as opened:
        result = await call_tool("get_schema_info", {"database": "app", **arguments})
    assert result.isError
    opened.assert_not_called()


@pytest.mark.parametrize("search", ["调拨", "%' OR 1=1 --", "\\", "用户名"])
def test_metadata_search_encodes_text_and_preserves_database_scope(search):
    sql = search_tables_sql("'app'", table_filter(["orders"]), search)
    assert search not in sql
    assert sql.count("TABLE_SCHEMA = 'app'") == 4
    assert sql.count("TABLE_NAME IN ('orders')") == 4
    validate_read_only_query(sql)
    validate_database_access(
        sql,
        selected_database="app",
        allowed_databases=("app",),
        allow_system_databases=False,
        internal=True,
    )
    validate_function_safety(sql)


def result_with(rows):
    return QueryResult(
        connection="dev",
        database="app",
        columns=["id", "text"],
        rows=rows,
        offset=0,
        truncated=False,
        duration_ms=1,
        query_id="q",
    )


@pytest.mark.parametrize("fmt", ["csv", "json", "compact"])
def test_utf8_response_budget_preserves_whole_rows_and_continuation(fmt):
    all_rows = [[i, '调拨,"\n' * 60] for i in range(15)]
    delivered = []
    offset = 0
    while offset < len(all_rows):
        result = replace(result_with(all_rows[offset:]), offset=offset).fit_response(
            fmt, 4096
        )
        assert len(result.render(fmt).encode("utf-8")) <= 4096
        assert result.rows
        delivered.extend(result.rows)
        if result.truncated:
            assert "response_bytes" in result.truncation_reasons
            assert result.next_offset > offset
            offset = result.next_offset
        else:
            break
    assert delivered == all_rows


@pytest.mark.parametrize("fmt", ["csv", "json", "compact"])
def test_oversized_first_row_is_actionable_without_skipping(fmt):
    with pytest.raises(QueryFailure) as error:
        result_with([[1, "大" * 10000]]).fit_response(fmt, 4096)
    assert error.value.payload["code"] == "RESULT_TOO_LARGE"
    assert error.value.payload["offset"] == 0


async def test_cell_truncation_is_distinct_from_row_truncation(monkeypatch):
    monkeypatch.setenv("MYSQL_MAX_CELL_LENGTH", "100")
    conn, _ = connection_with([(1, "长" * 200)], ["id", "text"])
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(conn, {"database": "app"}),
    ):
        response = await call_tool(
            "execute_sql",
            {"query": "SELECT id, text FROM t", "result_format": "compact"},
        )
    meta = json.loads(response[0].text.splitlines()[0])
    assert meta["content_truncated"] is True and meta["truncated"] is False
    assert meta["next_offset"] is None and meta["truncation_reasons"] == ["cell_length"]
    assert meta["connection"] == "default" and meta["database"] == "app"


async def test_budget_cannot_be_raised_above_profile_before_connecting():
    with patch("mysql_mcp_server.server._open_connection") as opened:
        result = await call_tool(
            "execute_sql", {"query": "SELECT 1", "max_response_bytes": 4194304}
        )
    assert result.isError
    opened.assert_not_called()


def admission_profile(**kwargs):
    return ConnectionProfile(
        name="one",
        host="127.0.0.1",
        port=3306,
        user="reader",
        pool_size=1,
        max_concurrent_queries=1,
        **kwargs,
    )


async def test_connection_queue_is_bounded_and_other_profile_can_progress():
    admission = QueryAdmission()
    profile = admission_profile(max_queued_queries=0)
    lease = await admission.acquire(profile)
    try:
        with pytest.raises(QueryFailure) as error:
            await admission.acquire(profile)
        assert error.value.payload["retryable"] is True
        other = await admission.acquire(replace(profile, name="two"))
        other.release_pending()
    finally:
        lease.release_pending()
    final = await admission.acquire(profile)
    final.release_pending()
    assert not admission._active


async def test_queue_timeout_and_cancellation_do_not_leak_slots():
    admission = QueryAdmission()
    profile = admission_profile(queue_timeout_ms=30)
    lease = await admission.acquire(profile)
    try:
        with pytest.raises(QueryFailure):
            await admission.acquire(profile)
        task = asyncio.create_task(admission.acquire(profile))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        lease.release_pending()
    assert not admission._active and not admission._waiting


async def test_successful_queued_query_and_cancel_before_worker_start():
    admission = QueryAdmission()
    profile = admission_profile(max_queued_queries=1)
    first = await admission.acquire(profile)
    second_task = asyncio.create_task(admission.acquire(profile))
    await asyncio.sleep(0.02)
    with pytest.raises(QueryFailure):
        await admission.acquire(profile)
    first.release_pending()
    second = await second_task
    assert second.wait_ms >= 10
    second.release_pending()
    with pytest.raises(RuntimeError, match="cancelled"):
        with second.run():
            pytest.fail("cancelled worker must not start")
    assert not admission._active and not admission._waiting


async def test_queue_time_counts_toward_total_timeout(monkeypatch):
    monkeypatch.setenv("MYSQL_MAX_CONCURRENT_QUERIES", "1")
    monkeypatch.setenv("MYSQL_QUEUE_TIMEOUT_MS", "1000")
    from mysql_mcp_server.runtime import query_admission

    profile = load_connection_registry().get()
    holder = await query_admission.acquire(profile)
    try:
        with patch("mysql_mcp_server.server._open_connection") as opened:
            result = await call_tool(
                "execute_sql", {"query": "SELECT 1", "timeout_ms": 100}
            )
        assert result.isError and "cancelled" in result[0].text
        opened.assert_not_called()
        # Allow abandoned worker to observe cancellation while the slot is still held.
        await asyncio.sleep(0.1)
    finally:
        holder.release_pending()
    assert not query_admission._active and not query_admission._waiting


async def test_cancelled_running_worker_retains_slot_until_cleanup(monkeypatch):
    monkeypatch.setenv("MYSQL_MAX_CONCURRENT_QUERIES", "1")
    monkeypatch.setenv("MYSQL_MAX_QUEUED_QUERIES", "0")
    started, release, cleaned = threading.Event(), threading.Event(), threading.Event()
    conn, cursor = connection_with([], ["id"])

    def execute(sql):
        if sql.startswith("SELECT"):
            started.set()
            release.wait(3)

    cursor.execute.side_effect = execute
    conn.close.side_effect = cleaned.set
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(conn, {"database": "app"}),
    ) as opened:
        task = asyncio.create_task(execute_query("SELECT 1"))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            result = await call_tool("execute_sql", {"query": "SELECT 2"})
            assert result.structuredContent["code"] == "CONNECTION_BUSY"
            opened.assert_called_once()
        finally:
            release.set()
            assert await asyncio.to_thread(cleaned.wait, 2)
            await asyncio.sleep(0.05)


def test_new_limits_load_from_named_profiles_and_environment(tmp_path, monkeypatch):
    path = tmp_path / "profiles.toml"
    path.write_text(
        '[connections.local]\nhost="127.0.0.1"\nuser="reader"\npassword="mock"\nmax_response_bytes=8192\nmax_concurrent_queries=2\nmax_queued_queries=3\nqueue_timeout_ms=500\nresult_format="compact"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(path))
    profile = load_connection_registry().get()
    assert (
        profile.max_response_bytes,
        profile.max_concurrent_queries,
        profile.max_queued_queries,
        profile.queue_timeout_ms,
        profile.result_format,
    ) == (8192, 2, 3, 500, "compact")
    monkeypatch.delenv("MYSQL_PROFILES_FILE")
    monkeypatch.setenv("MYSQL_MAX_RESPONSE_BYTES", "8192")
    monkeypatch.setenv("MYSQL_MAX_CONCURRENT_QUERIES", "2")
    monkeypatch.setenv("MYSQL_RESULT_FORMAT", "compact")
    profile = load_connection_registry().get()
    assert (
        profile.max_response_bytes,
        profile.max_concurrent_queries,
        profile.result_format,
    ) == (8192, 2, "compact")


async def test_logical_database_listing_can_continue_without_opening_database(
    tmp_path, monkeypatch
):
    path = tmp_path / "routes.toml"
    path.write_text(
        '[connections.local]\nhost="127.0.0.1"\nuser="reader"\npassword="mock"\ndatabase="app"\n[routes.shared]\na={connection="local",database="app"}\nb={connection="local",database="app"}\nc={connection="local",database="app"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(path))
    with patch("mysql_mcp_server.server._open_connection") as opened:
        first = await call_tool(
            "list_databases",
            {"connection": "shared", "max_rows": 2, "result_format": "json"},
        )
        page = json.loads(first[0].text)
        assert page["rows"] == [["a"], ["b"]] and page["next_offset"] == 2
        second = await call_tool(
            "list_databases",
            {
                "connection": "shared",
                "max_rows": 2,
                "offset": page["next_offset"],
                "result_format": "json",
            },
        )
        assert json.loads(second[0].text)["rows"] == [["c"]]
    opened.assert_not_called()


@pytest.mark.parametrize(
    "name,value",
    [
        ("MYSQL_MAX_CONCURRENT_QUERIES", "0"),
        ("MYSQL_MAX_QUEUED_QUERIES", "129"),
        ("MYSQL_QUEUE_TIMEOUT_MS", "0"),
        ("MYSQL_MAX_RESPONSE_BYTES", "4095"),
    ],
)
def test_invalid_resource_limits_are_rejected(name, value, monkeypatch):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        load_connection_registry(force_reload=True)
