"""Regression coverage for safe query plans and actionable database failures."""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from mysql.connector import Error

from mysql_mcp_server.server import call_tool
from mysql_mcp_server.sql_guard import (
    validate_function_safety,
    validate_read_only_query,
)


@pytest.fixture(autouse=True)
def query_scope(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "app")
    monkeypatch.setenv("MYSQL_ALLOWED_DATABASES", "app")


def plan_connection(rows, columns):
    cursor = MagicMock()
    cursor.description = [(column,) for column in columns]
    cursor.fetchmany.return_value = rows
    cursor.fetchall.return_value = rows
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    return connection, cursor


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id FROM app.orders ORDER BY id LIMIT 7 OFFSET 2",
        "WITH recent AS (SELECT id FROM app.orders) SELECT id FROM recent",
        "SELECT id FROM app.orders UNION ALL SELECT id FROM app.items",
        "SELECT /*+ NO_MERGE(orders) */ id FROM app.orders",
        "-- an ordinary comment\nSELECT 1",
    ],
)
async def test_explain_preserves_query_and_uses_existing_controls(query, caplog):
    connection, cursor = plan_connection(
        [(1, "orders", "range", 7)], ["id", "table", "type", "rows"]
    )
    with (
        patch(
            "mysql_mcp_server.server._open_connection",
            return_value=(connection, {"database": "app"}),
        ),
        caplog.at_level(logging.INFO, logger="mysql_mcp_server.audit"),
    ):
        response = await call_tool(
            "explain_sql",
            {
                "query": query,
                "database": "app",
                "max_rows": 10,
                "result_format": "json",
                "timeout_ms": 1000,
            },
        )
    payload = json.loads(response[0].text)
    assert payload["columns"] == ["id", "table", "type", "rows"]
    assert payload["rows"] == [[1, "orders", "range", 7]]
    assert payload["connection"] == "default" and payload["database"] == "app"
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert statements[-1] == "EXPLAIN " + query
    assert "SET SESSION MAX_EXECUTION_TIME = 1000" in statements
    assert "START TRANSACTION READ ONLY" in statements
    cursor.fetchall.assert_not_called()
    connection.rollback.assert_called_once_with()
    connection.close.assert_called_once_with()
    assert '"operation":"explain_sql"' in caplog.text
    assert '"status":"success"' in caplog.text


async def test_explain_paginates_plan_without_rewriting_original_query():
    query = "SELECT id FROM orders ORDER BY id LIMIT 7 OFFSET 2"
    connection, cursor = plan_connection([], ["id", "table"])
    cursor.fetchmany.side_effect = [[(1, "first")], [(2, "second"), (3, "third")]]
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ):
        response = await call_tool(
            "explain_sql",
            {
                "query": query,
                "max_rows": 1,
                "offset": 1,
                "result_format": "json",
            },
        )
    payload = json.loads(response[0].text)
    assert payload["rows"] == [[2, "second"]]
    assert payload["truncated"] is True and payload["next_offset"] == 2
    assert cursor.execute.call_args.args == ("EXPLAIN " + query,)
    assert [call.kwargs["size"] for call in cursor.fetchmany.call_args_list] == [1, 2]
    cursor.fetchall.assert_not_called()
    connection.shutdown.assert_called_once_with()
    connection.close.assert_called_once_with()


async def test_explain_keeps_response_budget_and_masking(monkeypatch):
    monkeypatch.setenv("MYSQL_MASK_COLUMNS", "Extra")
    connection, cursor = plan_connection(
        [(index, "x" * 2000, "sensitive-plan-note") for index in range(5)],
        ["id", "table", "Extra"],
    )
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ):
        response = await call_tool(
            "explain_sql",
            {
                "query": "SELECT id FROM orders",
                "max_rows": 5,
                "max_response_bytes": 4096,
                "result_format": "json",
            },
        )
    payload = json.loads(response[0].text)
    assert len(response[0].text.encode("utf-8")) <= 4096
    assert payload["masked_columns"] == ["Extra"]
    assert "sensitive-plan-note" not in response[0].text
    assert payload["truncated"] and payload["next_offset"] == len(payload["rows"])
    assert "response_bytes" in payload["truncation_reasons"]


@pytest.mark.parametrize(
    "query",
    [
        "EXPLAIN SELECT 1",
        "EXPLAIN ANALYZE SELECT 1",
        "ANALYZE SELECT 1",
        "SHOW TABLES",
        "DESCRIBE orders",
        "DELETE FROM orders",
        "SELECT 1; SELECT 2",
        "WITH gone AS (SELECT 1) DELETE FROM orders",
        "SELECT * FROM orders FOR UPDATE",
        "SELECT SLEEP(10)",
        "SELECT unreviewed_function()",
        "SELECT * FROM other.orders",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM app.orders UNION SELECT * FROM other.orders",
        "WITH scoped AS (SELECT * FROM other.orders) SELECT * FROM scoped",
        "SELECT 1 INTO OUTFILE '/unused'",
        "SELECT FROM",
    ],
)
async def test_explain_rejects_unsafe_or_out_of_scope_queries_before_connect(
    query, caplog
):
    with (
        patch("mysql_mcp_server.server._open_connection") as opened,
        caplog.at_level(logging.INFO, logger="mysql_mcp_server.audit"),
    ):
        response = await call_tool("explain_sql", {"query": query})
    assert response.isError
    opened.assert_not_called()
    assert '"operation":"explain_sql"' in caplog.text
    assert '"status":"denied"' in caplog.text


async def test_explain_requires_configured_audit_attribution(monkeypatch, tmp_path):
    monkeypatch.setenv("MYSQL_AUDIT_REQUIRED_CONTEXT", "purpose")
    monkeypatch.setenv("MYSQL_AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    with patch("mysql_mcp_server.server._open_connection") as opened:
        response = await call_tool("explain_sql", {"query": "SELECT 1"})
    assert response.isError and "purpose" in response[0].text
    opened.assert_not_called()


@pytest.mark.parametrize(
    "hint",
    [
        "SET_VAR(max_execution_time=0)",
        "set_var(sort_buffer_size=1000000000)",
        "MAX_EXECUTION_TIME(999999)",
        "max_execution_time (0)",
        "RESOURCE_GROUP(unapproved)",
        "NO_MERGE(t)\nSET_VAR(sql_mode='')",
    ],
)
@pytest.mark.parametrize("tool", ["execute_sql", "query", "explain_sql"])
async def test_policy_overriding_hints_are_denied_before_connect(tool, hint):
    with patch("mysql_mcp_server.server._open_connection") as opened:
        response = await call_tool(tool, {"query": f"SELECT /*+ {hint} */ 1"})
    assert response.isError and "Optimizer hints" in response[0].text
    opened.assert_not_called()


@pytest.mark.parametrize(
    "query",
    [
        "SELECT '/*+ SET_VAR(max_execution_time=0) */' AS note",
        "SELECT 1 /* SET_VAR(max_execution_time=0) */",
        "SELECT /*+ NO_MERGE(t) */ id FROM orders t",
        "SELECT 1 -- /*+ MAX_EXECUTION_TIME(999999) */",
    ],
)
def test_ordinary_comments_literals_and_plan_hints_remain_allowed(query):
    assert validate_read_only_query(query) == query
    assert not validate_function_safety(query)


def test_safe_hint_does_not_hide_an_actual_unreviewed_function():
    with pytest.raises(ValueError, match="UNREVIEWED_FUNCTION"):
        validate_function_safety(
            "SELECT /*+ NO_MERGE(t) */ unreviewed_function() FROM orders t"
        )


@pytest.mark.parametrize(
    "errno,code,retryable",
    [
        (3024, "QUERY_TIMEOUT", False),
        (1969, "QUERY_TIMEOUT", False),
        (1205, "LOCK_WAIT_TIMEOUT", True),
        (1213, "DEADLOCK", True),
        (1040, "CONNECTION_BUSY", True),
        (1203, "CONNECTION_BUSY", True),
        (2002, "CONNECTION_FAILED", True),
        (2003, "CONNECTION_FAILED", True),
        (2005, "CONNECTION_FAILED", False),
        (2006, "CONNECTION_LOST", True),
        (2013, "CONNECTION_LOST", True),
        (2055, "CONNECTION_LOST", True),
        (1317, "QUERY_INTERRUPTED", False),
        (9999, "DATABASE_QUERY_FAILED", False),
    ],
)
@pytest.mark.parametrize("phase", ["connect", "execute", "fetch"])
async def test_database_failures_are_structured_redacted_and_not_replayed(
    errno, code, retryable, phase, caplog
):
    connection, cursor = plan_connection([], ["id"])
    error = Error(msg="private-driver-data-and-query-literals", errno=errno)
    if phase == "execute":

        def execute(sql):
            if sql.startswith("SELECT"):
                raise error

        cursor.execute.side_effect = execute
    elif phase == "fetch":
        cursor.fetchall.side_effect = error
    with (
        patch(
            "mysql_mcp_server.server._open_connection",
            return_value=(connection, {"database": "app"}),
            side_effect=error if phase == "connect" else None,
        ) as opened,
        caplog.at_level(logging.INFO, logger="mysql_mcp_server.audit"),
    ):
        response = await call_tool("execute_sql", {"query": "SELECT 1"})
    payload = json.loads(response[0].text)
    assert response.isError and response.structuredContent == payload
    assert payload["code"] == code and payload["retryable"] is retryable
    assert payload["phase"] == phase and payload["errno"] == errno
    assert payload["connection"] == "default" and payload["database"] == "app"
    assert payload["next_action"] and payload["query_id"]
    assert "private-driver-data" not in response[0].text + caplog.text
    opened.assert_called_once()
    if phase != "connect":
        connection.close.assert_called_once_with()
    expected_status = "timeout" if code == "QUERY_TIMEOUT" else "error"
    assert f'"status":"{expected_status}"' in caplog.text


async def test_mcp_stdio_advertises_plans_and_delivers_structured_errors():
    """Exercise the real MCP wire protocol with a fake, local-only DB adapter."""
    import sys
    from pathlib import Path

    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    child_code = """
import asyncio
import json
from unittest.mock import MagicMock
from mysql.connector.errors import ReadTimeoutError
import mysql_mcp_server.server as server

cursor = MagicMock()
cursor.description = [(name,) for name in ('id', 'table', 'type', 'rows')]
cursor.fetchmany.return_value = [(1, 'orders', 'range', 7)]
def execute(sql):
    if sql.startswith('EXPLAIN FORMAT=JSON '):
        assert sql == 'EXPLAIN FORMAT=JSON SELECT id FROM orders LIMIT 7 OFFSET 2'
        cursor.description = [('EXPLAIN',)]
        cursor.fetchmany.return_value = [(json.dumps({'query_block': {'plan': 'x' * 1200}}),)]
    elif sql.startswith('EXPLAIN '):
        assert sql == 'EXPLAIN SELECT id FROM orders LIMIT 7 OFFSET 2'
    elif sql.startswith('SELECT '):
        raise ReadTimeoutError(msg='private-driver-message')
cursor.execute.side_effect = execute
connection = MagicMock()
connection.cursor.return_value.__enter__.return_value = cursor
server._open_connection = lambda *args, **kwargs: (connection, {'database': 'app'})
asyncio.run(server.main())
"""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", child_code],
        env={
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "MCP_TRANSPORT": "stdio",
            "MYSQL_USER": "protocol_test",
            "MYSQL_PASSWORD": "unused",
            "MYSQL_DATABASE": "app",
            "MYSQL_ALLOWED_DATABASES": "app",
            "MYSQL_PROFILES_FILE": "",
            "MYSQL_CONNECTIONS_FILE": "",
            "MYSQL_SSL_MODE": "DISABLED",
            "MYSQL_SSH_ENABLE": "false",
        },
    )
    with anyio.fail_after(20):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                assert tools["explain_sql"].annotations.readOnlyHint is True
                plan = await client.call_tool(
                    "explain_sql",
                    {
                        "query": "SELECT id FROM orders LIMIT 7 OFFSET 2",
                        "result_format": "json",
                    },
                )
                assert not plan.isError
                assert json.loads(plan.content[0].text)["rows"] == [
                    [1, "orders", "range", 7]
                ]
                error = await client.call_tool("execute_sql", {"query": "SELECT 1"})
                assert error.isError
                assert error.structuredContent["code"] == "QUERY_TIMEOUT"
                assert json.loads(error.content[0].text) == error.structuredContent
                assert "private-driver-message" not in error.content[0].text
                assert error.structuredContent["error_type"] == "ReadTimeoutError"
                parse_error = await client.call_tool(
                    "execute_sql",
                    {
                        "query": "SELECT 'private-parser-marker' FROM",
                    },
                )
                assert parse_error.isError
                assert parse_error.structuredContent["code"] == "SQL_PARSE_ERROR"
                assert "private-parser-marker" not in parse_error.content[0].text
                options = {
                    "query": "SELECT id FROM orders LIMIT 7 OFFSET 2",
                    "plan_output": "chunks",
                    "max_rows": 2,
                }
                chunks = []
                for page in range(10):
                    part = await client.call_tool("explain_sql", options)
                    assert not part.isError
                    payload = json.loads(part.content[0].text)
                    chunks.extend(row[1] for row in payload["rows"])
                    if not payload["truncated"]:
                        break
                    options.update(
                        offset=payload["next_offset"],
                        expected_plan_id=payload["plan"]["id"],
                    )
                else:
                    pytest.fail("STDIO plan pagination did not terminate")
                assert json.loads("".join(chunks))["query_block"]["plan"] == "x" * 1200
                denied = await client.call_tool(
                    "explain_sql",
                    {
                        "query": "EXPLAIN ANALYZE SELECT 1",
                    },
                )
                assert denied.isError


@pytest.mark.parametrize("tool", ["execute_sql", "query", "explain_sql"])
async def test_parser_error_returns_only_safe_coordinates_and_context(tool, caplog):
    marker = "parser_test_literal_4821"
    with (
        patch("mysql_mcp_server.server._open_connection") as opened,
        caplog.at_level(logging.INFO),
    ):
        response = await call_tool(tool, {"query": f"SELECT '{marker}' FROM"})
    payload = response.structuredContent
    assert response.isError and payload == json.loads(response[0].text)
    assert payload["code"] == "SQL_PARSE_ERROR" and payload["phase"] == "validation"
    assert payload["database"] == "app" and payload["connection"] == "default"
    assert payload["line"] == 1 and payload["column"] > 0
    assert payload["query_id"] and not payload["retryable"]
    assert marker not in response[0].text + caplog.text
    assert "SELECT '" not in response[0].text
    assert "\x1b" not in response[0].text
    assert '"status":"denied"' in caplog.text
    opened.assert_not_called()


@pytest.mark.parametrize("token_error", [False, True])
async def test_parser_exception_context_is_never_copied_to_response(
    token_error, caplog
):
    from sqlglot.errors import ParseError, TokenError

    marker = "parser_exception_private_context"
    error = (
        TokenError(marker)
        if token_error
        else ParseError(
            marker,
            errors=[
                {
                    "description": marker,
                    "line": True,
                    "col": marker,
                    "start_context": marker,
                    "highlight": marker,
                    "end_context": marker,
                }
            ],
        )
    )
    with (
        patch("mysql_mcp_server.sql_guard.sqlglot.parse_one", side_effect=error),
        patch("mysql_mcp_server.server._open_connection") as opened,
        caplog.at_level(logging.INFO),
    ):
        response = await call_tool("execute_sql", {"query": "SELECT 1"})
    assert response.structuredContent["code"] == "SQL_PARSE_ERROR"
    assert "line" not in response.structuredContent
    assert "column" not in response.structuredContent
    assert marker not in response[0].text + caplog.text
    opened.assert_not_called()


@pytest.mark.parametrize("phase", ["connect", "session_setup", "execute", "fetch"])
@pytest.mark.parametrize("tool", ["execute_sql", "explain_sql"])
@pytest.mark.parametrize("timeout_class", ["ReadTimeoutError", "WriteTimeoutError"])
async def test_socket_timeouts_are_not_replayed_and_discard_connection(
    timeout_class, phase, tool, caplog
):
    from mysql.connector import errors

    error = getattr(errors, timeout_class)(msg="private_socket_timeout_context")
    connection, cursor = plan_connection([], ["id"])
    if phase in {"session_setup", "execute"}:

        def execute(sql):
            if phase == "session_setup" or sql.startswith(("SELECT", "EXPLAIN")):
                raise error

        cursor.execute.side_effect = execute
    if phase == "fetch":
        cursor.fetchmany.side_effect = error
        cursor.fetchall.side_effect = error
    with (
        patch(
            "mysql_mcp_server.server._open_connection",
            return_value=(connection, {"database": "app"}),
            side_effect=error if phase == "connect" else None,
        ) as opened,
        caplog.at_level(logging.INFO),
    ):
        response = await call_tool(tool, {"query": "SELECT 1"})
    payload = response.structuredContent
    assert response.isError and payload == json.loads(response[0].text)
    assert payload["code"] == "QUERY_TIMEOUT" and payload["phase"] == phase
    assert payload["error_type"] == timeout_class
    assert payload["errno"] == -1 and not payload["retryable"]
    assert payload["query_id"] and payload["database"] == "app"
    assert "private_socket_timeout_context" not in response[0].text + caplog.text
    assert '"status":"timeout"' in caplog.text
    opened.assert_called_once()
    if phase != "connect":
        connection.shutdown.assert_called_once_with()
        connection.close.assert_called_once_with()
        connection.rollback.assert_not_called()


@pytest.mark.parametrize("error_class", ["Error", "OperationalError", "PoolError"])
async def test_unknown_negative_errno_does_not_trigger_generic_replay(error_class):
    from mysql.connector import errors

    connection, cursor = plan_connection([], ["id"])
    cursor.fetchall.side_effect = getattr(errors, error_class)(errno=-1)
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ) as opened:
        response = await call_tool("execute_sql", {"query": "SELECT 1"})
    assert response.isError
    opened.assert_called_once()


@pytest.mark.parametrize(
    "plan_text",
    [
        json.dumps({"query_block": {"condition": "中文😀" * 9000}}, ensure_ascii=False),
        "-> Filter\n" + "  -> complex step\n" * 2000,
    ],
    ids=["json", "tree"],
)
async def test_long_plan_cell_is_delivered_complete(plan_text):
    connection, cursor = plan_connection([(plan_text,)], ["EXPLAIN"])
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ):
        response = await call_tool(
            "explain_sql",
            {
                "query": "SELECT 1",
                "result_format": "json",
            },
        )
    payload = json.loads(response[0].text)
    assert payload["rows"] == [[plan_text]]
    assert not payload["content_truncated"] and not payload["truncated"]
    connection.close.assert_called_once_with()


async def test_oversized_plan_returns_chunk_recovery_instead_of_partial_text():
    connection, cursor = plan_connection([("x" * 25000,)], ["EXPLAIN"])
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ):
        response = await call_tool(
            "explain_sql",
            {
                "query": "SELECT 1",
                "result_format": "json",
                "max_response_bytes": 4096,
            },
        )
    assert response.isError
    assert response.structuredContent["code"] == "RESULT_TOO_LARGE"
    assert "plan_output=chunks" in response.structuredContent["next_action"]
    assert "SUBSTRING" not in response.structuredContent["next_action"]
    assert "rows" not in response.structuredContent
    connection.close.assert_called_once_with()


@pytest.mark.parametrize("result_format", [None, "json", "compact"])
async def test_plan_chunks_reassemble_with_utf8_budget_and_masking(result_format):
    import csv
    import io

    private_marker = "secret_plan_test_marker"
    plan = {
        "query_block": {"description": '中文😀\n"' * 2000, "password": private_marker}
    }
    original = json.dumps(plan, ensure_ascii=False)
    connection, cursor = plan_connection([(original,)], ["EXPLAIN"])
    arguments = {
        "query": "SELECT id FROM orders LIMIT 7 OFFSET 2",
        "plan_output": "chunks",
        "max_rows": 7,
        "max_response_bytes": 4096,
    }
    if result_format is not None:
        arguments["result_format"] = result_format
    chunks = []
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ):
        for page in range(1000):
            response = await call_tool("explain_sql", arguments)
            text = response[0].text
            assert len(text.encode("utf-8")) <= 4096
            assert private_marker not in text
            if result_format == "compact":
                header, body = text.split("\n", 1)
                payload = json.loads(header)
                rows = list(csv.reader(io.StringIO(body)))
                rows = rows[1 : 1 + payload["row_count"]]
            else:
                payload = json.loads(text)
                rows = payload["rows"]
            assert not payload["content_truncated"]
            assert payload["masked_columns"] == ["text"]
            assert rows and len(rows) <= 7
            for part, chunk in rows:
                assert int(part) == len(chunks)
                chunks.append(chunk)
            assert cursor.execute.call_args.args == (
                "EXPLAIN FORMAT=JSON SELECT id FROM orders LIMIT 7 OFFSET 2",
            )
            if not payload["truncated"]:
                break
            assert payload["next_offset"] > arguments.get("offset", 0)
            arguments.update(
                offset=payload["next_offset"], expected_plan_id=payload["plan"]["id"]
            )
        else:
            pytest.fail("plan pagination did not terminate")
    recovered = json.loads("".join(chunks))
    assert recovered["query_block"]["description"] == plan["query_block"]["description"]
    assert recovered["query_block"]["password"] == "[REDACTED]"
    assert len(chunks) == payload["plan"]["total_chunks"]
    assert len("".join(chunks)) == payload["plan"]["characters"]
    cursor.fetchall.assert_not_called()
    assert all(call.kwargs["size"] == 2 for call in cursor.fetchmany.call_args_list)


async def test_changed_plan_is_rejected_before_returning_more_chunks():
    connection, cursor = plan_connection(
        [(json.dumps({"plan": "a" * 3000}),)], ["EXPLAIN"]
    )
    arguments = {"query": "SELECT 1", "plan_output": "chunks", "max_rows": 1}
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ):
        first = json.loads((await call_tool("explain_sql", arguments))[0].text)
        cursor.fetchmany.return_value = [(json.dumps({"plan": "b" * 3000}),)]
        changed = await call_tool(
            "explain_sql",
            {
                **arguments,
                "offset": first["next_offset"],
                "expected_plan_id": first["plan"]["id"],
            },
        )
    assert changed.isError and changed.structuredContent["code"] == "PLAN_CHANGED"
    assert "rows" not in changed.structuredContent


@pytest.mark.parametrize(
    "options",
    [
        {"plan_output": "chunks", "offset": 1},
        {"plan_output": "chunks", "expected_plan_id": "invalid"},
        {"plan_output": "chunks", "result_format": "csv"},
        {"plan_output": "rows", "expected_plan_id": "a" * 64},
        {"plan_output": "invalid"},
    ],
)
async def test_invalid_plan_chunk_options_fail_before_connect(options):
    with patch("mysql_mcp_server.server._open_connection") as opened:
        response = await call_tool("explain_sql", {"query": "SELECT 1", **options})
    assert response.isError
    opened.assert_not_called()


async def test_chunk_mode_does_not_accept_an_unexpected_multirow_plan():
    connection, cursor = plan_connection([("first",), ("second",)], ["EXPLAIN"])
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ):
        response = await call_tool(
            "explain_sql", {"query": "SELECT 1", "plan_output": "chunks"}
        )
    assert (
        response.isError
        and response.structuredContent["code"] == "PLAN_FORMAT_UNSUPPORTED"
    )
    connection.shutdown.assert_called_once_with()


async def test_regular_query_still_applies_cell_length_limit():
    connection, cursor = plan_connection([("x" * 25000,)], ["value"])
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ):
        response = await call_tool(
            "execute_sql",
            {"query": "SELECT body AS value FROM orders", "result_format": "json"},
        )
    payload = json.loads(response[0].text)
    assert payload["content_truncated"]
    assert payload["rows"][0][0].endswith("…[truncated]")
