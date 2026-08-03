import asyncio
import hashlib
import json
import logging
import threading
from unittest.mock import MagicMock, patch

import pytest
from mysql.connector import Error, InterfaceError

from mysql_mcp_server.results import QueryResult
from mysql_mcp_server.server import (
    _apply_server_query_timeout,
    _assess_grants,
    _verify_connection_transport,
    call_tool,
    check_connection,
    execute_query,
    read_resource,
)
from pydantic import AnyUrl


def fake_connection(rows, columns):
    cursor = MagicMock()
    cursor.description = [(column,) for column in columns]
    cursor.fetchmany.return_value = rows
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    return connection, cursor


@pytest.mark.asyncio
async def test_json_pagination_executes_read_only_controls_and_audits(caplog):
    connection, cursor = fake_connection(
        [(1, "a"), (2, "b"), (3, "c")],
        ["id", "name"],
    )
    with (
        patch(
            "mysql_mcp_server.server._open_connection",
            return_value=(connection, {"database": "app"}),
        ),
        caplog.at_level(logging.INFO, logger="mysql_mcp_server.audit"),
    ):
        response = await call_tool(
            "execute_sql",
            {
                "query": "SELECT id, name FROM users",
                "database": "app",
                "max_rows": 2,
                "result_format": "json",
            },
        )

    payload = json.loads(response[0].text)
    assert payload["rows"] == [[1, "a"], [2, "b"]]
    assert payload["truncated"] is True
    assert payload["next_offset"] == 2
    commands = [call.args[0] for call in cursor.execute.call_args_list]
    assert commands == [
        "SET SESSION MAX_EXECUTION_TIME = 30000",
        "SET SESSION TRANSACTION READ ONLY",
        "START TRANSACTION READ ONLY",
        "SELECT id, name FROM users",
    ]
    connection.shutdown.assert_called_once_with()
    connection.rollback.assert_not_called()
    assert "SELECT id, name" not in caplog.text
    assert '"status":"success"' in caplog.text
    assert '"status":"started"' in caplog.text
    assert '"schema_version":1' in caplog.text
    assert '"read_only_enforced":true' in caplog.text


def test_required_tls_rejects_plaintext_connection():
    connection = MagicMock()
    connection.is_secure = False
    profile = MagicMock(ssl_mode="REQUIRED", name="prod")

    with pytest.raises(RuntimeError, match="requires TLS"):
        _verify_connection_transport(profile, connection)

    connection.close.assert_called_once_with()


def test_required_tls_accepts_connector_c_extension_cipher():
    connection = MagicMock()
    connection.is_secure = False
    connection._cmysql.get_ssl_cipher.return_value = "TLS_AES_256_GCM_SHA384"
    profile = MagicMock(ssl_mode="REQUIRED", name="prod")

    _verify_connection_transport(profile, connection)

    connection._cmysql.get_ssl_cipher.assert_called_once_with()
    connection.close.assert_not_called()


def test_required_tls_rejects_empty_c_extension_cipher():
    connection = MagicMock()
    connection.is_secure = False
    connection._cmysql.get_ssl_cipher.return_value = None
    profile = MagicMock(ssl_mode="REQUIRED", name="prod")

    with pytest.raises(RuntimeError, match="requires TLS"):
        _verify_connection_transport(profile, connection)

    connection.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_denied_write_is_audited_before_opening_a_connection(caplog):
    with (
        patch("mysql_mcp_server.server._open_connection") as connector,
        caplog.at_level(logging.INFO, logger="mysql_mcp_server.audit"),
    ):
        response = await call_tool(
            "execute_sql",
            {
                "query": "DELETE FROM users WHERE id = 1",
                "database": "app",
                "audit_context": {
                    "actor": "alice",
                    "purpose": "verify policy",
                    "ticket_id": "SEC-7",
                },
            },
        )

    assert "statements are blocked" in response[0].text
    connector.assert_not_called()
    assert '"status":"denied"' in caplog.text
    assert '"policy":"denied"' in caplog.text
    assert '"actor":"alice"' in caplog.text
    assert "DELETE FROM users" not in caplog.text


@pytest.mark.asyncio
async def test_unknown_udf_is_denied_before_opening_a_connection(caplog):
    with (
        patch("mysql_mcp_server.server._open_connection") as connector,
        caplog.at_level(logging.INFO, logger="mysql_mcp_server.audit"),
    ):
        response = await call_tool(
            "execute_sql",
            {
                "query": "SELECT potentially_writing_udf(id) FROM users",
                "database": "app",
            },
        )

    assert "unrecognized functions are blocked" in response[0].text
    connector.assert_not_called()
    assert '"status":"denied"' in caplog.text
    assert "potentially_writing_udf" not in caplog.text


@pytest.mark.asyncio
async def test_mysql_error_details_are_redacted_from_tool_response():
    connection, cursor = fake_connection([], ["id"])

    def execute(sql):
        if sql == "SELECT id FROM users":
            raise Error(
                msg="Access denied for user sensitive_user@secret.internal",
                errno=1045,
                sqlstate="28000",
            )

    cursor.execute.side_effect = execute
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ):
        response = await call_tool(
            "execute_sql",
            {"query": "SELECT id FROM users", "database": "app"},
        )

    assert "errno=1045" in response[0].text
    assert "sqlstate=28000" in response[0].text
    assert "sensitive_user" not in response[0].text
    assert "secret.internal" not in response[0].text


@pytest.mark.asyncio
async def test_errno_minus_one_is_retried_once_without_exposing_details(caplog):
    failed_connection, failed_cursor = fake_connection([], ["id"])
    successful_connection, successful_cursor = fake_connection(
        [(7,)],
        ["id"],
    )

    def fail_first_attempt(sql):
        if sql == "DESCRIBE users":
            raise InterfaceError(
                msg="Unread result from sensitive.internal",
                errno=-1,
            )

    failed_cursor.execute.side_effect = fail_first_attempt
    with (
        patch(
            "mysql_mcp_server.server._open_connection",
            side_effect=[
                (failed_connection, {"database": "app"}),
                (successful_connection, {"database": "app"}),
            ],
        ) as connector,
        caplog.at_level(logging.WARNING, logger="mysql_mcp_server"),
    ):
        response = await call_tool(
            "execute_sql",
            {
                "query": "DESCRIBE users",
                "database": "app",
                "result_format": "json",
            },
        )

    assert connector.call_count == 2
    payload = json.loads(response[0].text)
    assert payload["rows"] == [[7]]
    assert payload["retry_count"] == 1
    assert "sensitive.internal" not in response[0].text
    assert "phase=execute" in caplog.text
    assert "Unread result" not in caplog.text


@pytest.mark.asyncio
async def test_persistent_errno_minus_one_reports_safe_phase_and_type():
    connections = []

    def failing_connection():
        connection, cursor = fake_connection([], ["table"])

        def execute(sql):
            if sql == "SHOW TABLES LIKE 'orders'":
                raise InterfaceError(
                    msg="Connection details must remain private",
                    errno=-1,
                )

        cursor.execute.side_effect = execute
        connections.append(connection)
        return connection, {"database": "app"}

    with patch(
        "mysql_mcp_server.server._open_connection",
        side_effect=[failing_connection(), failing_connection()],
    ):
        response = await call_tool(
            "execute_sql",
            {
                "query": "SHOW TABLES LIKE 'orders'",
                "database": "app",
            },
        )

    text = response[0].text
    assert "error_type=InterfaceError" in text
    assert "phase=execute" in text
    assert "errno=-1" in text
    assert "Connection details" not in text


@pytest.mark.asyncio
async def test_bounded_table_sample_consumes_result_without_socket_shutdown():
    connection, cursor = fake_connection([], ["id"])
    cursor.fetchall.return_value = [(1,), (2,), (3,)]
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ):
        response = await call_tool(
            "get_table_sample",
            {
                "table_name": "orders",
                "database": "app",
                "limit": 2,
                "offset": 4,
                "result_format": "json",
            },
        )

    payload = json.loads(response[0].text)
    assert payload["rows"] == [[1], [2]]
    assert payload["offset"] == 4
    assert payload["next_offset"] == 6
    assert payload["truncated"] is True
    assert payload["retry_count"] == 0
    cursor.fetchall.assert_called_once_with()
    connection.shutdown.assert_not_called()
    connection.rollback.assert_called_once_with()


@pytest.mark.asyncio
async def test_required_audit_context_blocks_query_before_connecting(
    tmp_path, monkeypatch, caplog
):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.prod]
host = "prod"
user = "reader"
password = "secret"
database = "app"
audit_required_context = ["actor", "ticket_id"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))

    with (
        patch("mysql_mcp_server.server._open_connection") as connector,
        caplog.at_level(logging.INFO, logger="mysql_mcp_server.audit"),
    ):
        response = await call_tool(
            "execute_sql",
            {
                "query": "SELECT 1",
                "database": "app",
                "audit_context": {"actor": "alice"},
            },
        )

    assert "ticket_id" in response[0].text
    connector.assert_not_called()
    assert '"status":"denied"' in caplog.text


@pytest.mark.asyncio
async def test_resource_cannot_bypass_required_audit_context(tmp_path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.prod]
host = "prod"
user = "reader"
password = "secret"
database = "app"
audit_required_context = ["actor"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))

    with patch("mysql_mcp_server.server._open_connection") as connector:
        with pytest.raises(ValueError, match="Missing required audit_context"):
            await read_resource(AnyUrl("mysql://users/data"))

    connector.assert_not_called()


@pytest.mark.asyncio
async def test_fail_closed_audit_dependency_blocks_database_access(
    tmp_path, monkeypatch
):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        f"""
[connections.prod]
host = "prod"
user = "reader"
password = "secret"
database = "app"
audit_log_file = "{(tmp_path / "audit.jsonl").as_posix()}"
audit_hmac_key_env = "MISSING_SIGNING_KEY"
audit_fail_closed = true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))
    monkeypatch.delenv("MISSING_SIGNING_KEY", raising=False)

    with patch("mysql_mcp_server.server._open_connection") as connector:
        response = await call_tool(
            "execute_sql",
            {"query": "SELECT 1", "database": "app"},
        )

    assert "MISSING_SIGNING_KEY" in response[0].text
    connector.assert_not_called()


@pytest.mark.asyncio
async def test_offset_discards_rows_before_returning_page():
    connection, cursor = fake_connection([], ["id"])
    cursor.fetchmany.side_effect = [[(1,), (2,)], [(3,), (4,), (5,)]]
    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ):
        result = await execute_query(
            "SELECT id FROM users",
            database="app",
            max_rows=2,
            offset=2,
        )

    assert result.rows == [[3], [4]]
    assert result.next_offset == 4


@pytest.mark.asyncio
async def test_query_timeout_closes_socket_and_returns_explicit_error():
    query_started = threading.Event()
    released = threading.Event()
    cursor = MagicMock()
    cursor.description = [("id",)]
    cursor.fetchmany.return_value = []

    def execute(sql):
        if sql == "SELECT id FROM slow_table":
            query_started.set()
            released.wait(timeout=2)
            raise RuntimeError("socket closed")

    cursor.execute.side_effect = execute
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    connection.shutdown.side_effect = lambda: released.set()

    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ):
        response = await call_tool(
            "execute_sql",
            {
                "query": "SELECT id FROM slow_table",
                "database": "app",
                "timeout_ms": 500,
            },
        )

    assert query_started.is_set()
    assert "exceeded 500 ms" in response[0].text
    connection.shutdown.assert_called_once_with()


@pytest.mark.asyncio
async def test_task_cancellation_closes_socket():
    query_started = threading.Event()
    released = threading.Event()
    cursor = MagicMock()
    cursor.description = [("id",)]

    def execute(sql):
        if sql == "SELECT id FROM slow_table":
            query_started.set()
            released.wait(timeout=2)
            raise RuntimeError("socket closed")

    cursor.execute.side_effect = execute
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    connection.shutdown.side_effect = lambda: released.set()

    with patch(
        "mysql_mcp_server.server._open_connection",
        return_value=(connection, {"database": "app"}),
    ):
        task = asyncio.create_task(
            execute_query(
                "SELECT id FROM slow_table",
                database="app",
                timeout_ms=5000,
            )
        )
        for _ in range(100):
            if query_started.is_set():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    connection.shutdown.assert_called_once_with()


def test_server_timeout_falls_back_to_mariadb():
    cursor = MagicMock()

    def execute(sql):
        if "MAX_EXECUTION_TIME" in sql:
            raise Error(msg="unknown", errno=1193)

    cursor.execute.side_effect = execute

    mode = _apply_server_query_timeout(cursor, 1500)

    assert mode == "mariadb"
    assert cursor.execute.call_args_list[-1].args[0] == (
        "SET SESSION max_statement_time = 1.500"
    )


def test_server_timeout_falls_back_to_connector_for_older_servers():
    cursor = MagicMock()
    cursor.execute.side_effect = [
        Error(msg="unknown mysql variable", errno=1193),
        Error(msg="unknown mariadb variable", errno=1193),
    ]

    assert _apply_server_query_timeout(cursor, 1000) == "connector"


@pytest.mark.asyncio
async def test_check_connection_combines_health_and_grants():
    health = QueryResult(
        connection="default",
        database="app",
        columns=[
            "version",
            "current_database",
            "authenticated_user",
            "global_read_only",
        ],
        rows=[["8.4.0", "app", "reader@%", 1]],
        offset=0,
        truncated=False,
        duration_ms=3,
        query_id="health",
    )
    grants = QueryResult(
        connection="default",
        database="app",
        columns=["Grants"],
        rows=[["GRANT SELECT ON `app`.* TO `reader`@`%`"]],
        offset=0,
        truncated=False,
        duration_ms=2,
        query_id="grants",
    )
    with patch(
        "mysql_mcp_server.server.execute_query",
        side_effect=[health, grants],
    ):
        response = await check_connection(database="app")

    payload = json.loads(response[0].text)
    assert payload["ok"] is True
    assert payload["server"]["version"] == "8.4.0"
    assert (
        payload["server"]["account_fingerprint"]
        == hashlib.sha256(b"reader@%").hexdigest()[:16]
    )
    assert "authenticated_user" not in payload["server"]
    assert payload["grant_count"] == 1
    assert "grants" not in payload
    assert "reader@%" not in response[0].text
    assert payload["read_only_assessment"]["account_select_only"] is True
    assert payload["read_only_assessment"]["defense_in_depth"] is True


def test_grant_assessment_warns_about_write_capable_account():
    assessment = _assess_grants(
        [
            "GRANT USAGE ON *.* TO `app`@`%`",
            "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER "
            "ON `app`.* TO `app`@`%`",
        ],
        0,
    )

    assert assessment["account_select_only"] is False
    assert assessment["defense_in_depth"] is False
    assert assessment["non_read_privileges"] == [
        "ALTER",
        "CREATE",
        "DELETE",
        "INSERT",
        "UPDATE",
    ]
    assert "SELECT-only" in str(assessment["warning"])


@pytest.mark.asyncio
async def test_resource_uri_cannot_bypass_database_allowlist(tmp_path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.prod]
host = "prod"
user = "reader"
password = "secret"
database = "app"
allowed_databases = ["app"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))

    with patch("mysql_mcp_server.server.connect") as connector:
        with pytest.raises(ValueError, match="not allowed"):
            await read_resource(AnyUrl("mysql://database/secret"))

    connector.assert_not_called()


@pytest.mark.asyncio
async def test_resource_uri_cannot_bypass_system_database_policy(tmp_path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.prod]
host = "prod"
user = "reader"
password = "secret"
database = "app"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))

    with pytest.raises(ValueError, match="System database"):
        await read_resource(AnyUrl("mysql://database/mysql"))
