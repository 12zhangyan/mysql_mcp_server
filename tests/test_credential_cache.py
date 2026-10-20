import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mysql.connector import Error, InterfaceError

from mysql_mcp_server.config import load_connection_registry
from mysql_mcp_server.credential_store import CredentialCache, credential_cache
from mysql_mcp_server.server import call_tool


@pytest.fixture(autouse=True)
def empty_credential_cache():
    credential_cache.clear()
    yield
    credential_cache.clear()


def _profiles(tmp_path: Path, monkeypatch, body: str) -> None:
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        '[connections.secure]\nhost = "db.example"\nuser = "reader"\n'
        'database = "app"\nallowed_databases = ["app"]\npool_size = 0\n'
        'ssl_mode = "DISABLED"\n' + body,
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))
    load_connection_registry(force_reload=True)


def _command_profile(tmp_path, monkeypatch, cache_seconds: int) -> None:
    _profiles(
        tmp_path,
        monkeypatch,
        'credential_provider = "command"\n'
        'credential_command = ["secret-tool"]\n'
        f"credential_cache_seconds = {cache_seconds}\n",
    )


def _connection(rows=((1,),), execute=None):
    cursor = MagicMock()
    cursor.description = [("id",)]
    cursor.fetchmany.return_value = list(rows)
    cursor.fetchall.return_value = list(rows)
    if execute is not None:
        cursor.execute.side_effect = execute
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = connection_cursor = cursor
    return connection, connection_cursor


async def _query(sql: str = "SELECT id FROM users"):
    return await call_tool(
        "execute_sql",
        {"query": sql, "connection": "secure", "result_format": "json"},
    )


async def test_keyring_is_read_once_per_request_including_retry(tmp_path, monkeypatch):
    _profiles(
        tmp_path,
        monkeypatch,
        'credential_provider = "keyring"\ncredential_ref = "secure-db"\n',
    )
    failed, failed_cursor = _connection()

    def fail_query(sql):
        if sql.startswith("SELECT id"):
            raise InterfaceError(msg="unread", errno=-1)

    failed_cursor.execute.side_effect = fail_query
    succeeded, _ = _connection()
    with (
        patch(
            "mysql_mcp_server.config.get_keyring_password",
            return_value="runtime-secret",
        ) as keyring_lookup,
        patch(
            "mysql_mcp_server.server.connect", side_effect=[failed, succeeded]
        ) as connect,
    ):
        response = await _query()

    assert json.loads(response[0].text)["retry_count"] == 1
    assert keyring_lookup.call_count == 1
    assert [call.kwargs["password"] for call in connect.call_args_list] == [
        "runtime-secret",
        "runtime-secret",
    ]


async def test_command_runs_once_per_request_when_cache_is_disabled(
    tmp_path, monkeypatch
):
    _command_profile(tmp_path, monkeypatch, 0)
    with (
        patch(
            "mysql_mcp_server.config.run_credential_command",
            return_value="cmd-secret",
        ) as command,
        patch(
            "mysql_mcp_server.server.connect",
            side_effect=[_connection()[0], _connection()[0]],
        ),
    ):
        await _query()
        await _query()

    assert command.call_count == 2


async def test_command_cache_reuses_secret_until_ttl_expires(tmp_path, monkeypatch):
    _command_profile(tmp_path, monkeypatch, 30)
    now = [1000.0]
    monkeypatch.setattr(credential_cache, "_clock", lambda: now[0])
    with (
        patch(
            "mysql_mcp_server.config.run_credential_command",
            return_value="cmd-secret",
        ) as command,
        patch(
            "mysql_mcp_server.server.connect",
            side_effect=[_connection()[0] for _ in range(4)],
        ),
    ):
        for _ in range(3):
            await _query()
        now[0] += 31
        await _query()

    assert command.call_count == 2


async def test_rejected_cached_credential_is_reloaded_on_next_request(
    tmp_path, monkeypatch, caplog
):
    _command_profile(tmp_path, monkeypatch, 300)
    with (
        patch(
            "mysql_mcp_server.config.run_credential_command",
            side_effect=["old-secret", "new-secret"],
        ) as command,
        patch(
            "mysql_mcp_server.server.connect",
            side_effect=[
                Error(msg="Access denied for user 'reader'", errno=1045),
                _connection()[0],
            ],
        ) as connect,
    ):
        denied = await _query()
        allowed = await _query()

    assert denied.isError is True
    assert json.loads(denied.content[0].text)["errno"] == 1045
    assert allowed[0].text
    assert command.call_count == 2
    assert connect.call_count == 2
    assert connect.call_args_list[1].kwargs["password"] == "new-secret"
    assert "old-secret" not in caplog.text
    assert "new-secret" not in caplog.text


async def test_forced_reload_drops_cached_credentials(tmp_path, monkeypatch):
    _command_profile(tmp_path, monkeypatch, 300)
    with (
        patch(
            "mysql_mcp_server.config.run_credential_command",
            return_value="cmd-secret",
        ) as command,
        patch(
            "mysql_mcp_server.server.connect",
            side_effect=[_connection()[0], _connection()[0]],
        ),
    ):
        await _query()
        load_connection_registry(force_reload=True)
        await _query()

    assert command.call_count == 2


@pytest.mark.parametrize("value", [-1, 301])
def test_cache_seconds_is_bounded(tmp_path, monkeypatch, value):
    _profiles(
        tmp_path,
        monkeypatch,
        'credential_provider = "keyring"\ncredential_ref = "secure-db"\n'
        f"credential_cache_seconds = {value}\n",
    )
    registry = load_connection_registry(force_reload=True)
    assert "credential_cache_seconds" in json.dumps(registry.errors)


def test_cache_seconds_requires_a_secret_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_PASSWORD", "x")
    _profiles(
        tmp_path,
        monkeypatch,
        'password_env = "SECURE_PASSWORD"\ncredential_cache_seconds = 30\n',
    )
    registry = load_connection_registry(force_reload=True)
    assert "only valid for the keyring or command provider" in json.dumps(
        registry.errors
    )


def test_cache_disabled_always_calls_loader():
    cache = CredentialCache(clock=lambda: 0.0)
    loader = MagicMock(return_value="v")

    assert cache.get_or_load("k", 0, loader) == "v"
    assert cache.get_or_load("k", 0, loader) == "v"
    assert loader.call_count == 2
