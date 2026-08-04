from pathlib import Path
import json
from unittest.mock import patch

import pytest

from mysql_mcp_server.config import (
    build_connector_config,
    ensure_database_allowed,
    load_connection_registry,
)
from mysql_mcp_server.server import call_tool


def test_loads_named_profiles_with_password_env(tmp_path: Path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
default = "test"

[connections.dev]
host = "dev-db"
user = "reader"
password_env = "DEV_DB_PASSWORD"
database = "app_dev"

[connections.test]
description = "Shared test"
host = "test-db"
port = 3307
user = "reader"
password_env = "TEST_DB_PASSWORD"
database = "app_test"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))
    monkeypatch.setenv("DEV_DB_PASSWORD", "dev-secret")
    monkeypatch.setenv("TEST_DB_PASSWORD", "test-secret")

    registry = load_connection_registry()

    assert registry.default == "test"
    assert set(registry.profiles) == {"dev", "test"}
    assert registry.get("test").port == 3307
    assert registry.get("test").resolve_password() == "test-secret"


def test_connector_config_is_not_autocommit(tmp_path: Path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.dev]
host = "dev-db"
user = "reader"
password_env = "DEV_DB_PASSWORD"
database = "app_dev"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))
    monkeypatch.setenv("DEV_DB_PASSWORD", "secret")

    profile = load_connection_registry().get()
    config = build_connector_config(profile, database="audit")

    assert config["autocommit"] is False
    assert config["database"] == "audit"
    assert config["ssl_disabled"] is False
    assert profile.ssl_mode == "REQUIRED"
    assert "password" in profile.mask_columns


def test_verified_tls_requires_ca_file(tmp_path: Path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.prod]
host = "prod-db"
user = "reader"
password_env = "PROD_DB_PASSWORD"
ssl_mode = "VERIFY_IDENTITY"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))
    monkeypatch.setenv("PROD_DB_PASSWORD", "secret")

    registry = load_connection_registry()

    assert "requires ssl_ca" in registry.errors["prod"]


def test_missing_profile_password_fails_explicitly(tmp_path: Path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.prod]
host = "prod-db"
user = "reader"
password_env = "MISSING_PROD_PASSWORD"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))
    monkeypatch.delenv("MISSING_PROD_PASSWORD", raising=False)

    profile = load_connection_registry().get("prod")
    ready, message = profile.runtime_status()
    assert ready is False
    assert "MISSING_PROD_PASSWORD" in message


@pytest.mark.asyncio
async def test_list_connections_never_exposes_credentials(tmp_path: Path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
default = "prod"

[connections.prod]
description = "Production replica"
host = "secret.internal"
user = "sensitive_user"
password_env = "PROD_DB_PASSWORD"
database = "app"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))
    monkeypatch.setenv("PROD_DB_PASSWORD", "super-secret-password")

    response = await call_tool("list_connections", {})
    text = response[0].text

    payload = __import__("json").loads(text)
    assert payload["default"] == "prod"
    assert payload["source"] == "profiles_file"
    assert payload["profiles"][0]["description"] == "Production replica"
    assert payload["database_routes"] == {"app": ["prod"]}
    assert "never switches environments implicitly" in payload["selection_guidance"]
    assert str(profiles_file) not in text
    assert "super-secret-password" not in text
    assert "sensitive_user" not in text
    assert "secret.internal" not in text


@pytest.mark.asyncio
async def test_wrong_connection_database_pair_suggests_declared_profile(
    tmp_path: Path, monkeypatch, caplog
):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
default = "test"

[connections.test]
host = "test-db"
user = "reader"
password = "secret"
database = "core"
allowed_databases = ["core"]

[connections.test-gts]
host = "test-db"
user = "reader"
password = "secret"
database = "gts"
allowed_databases = ["gts"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))
    caplog.set_level("INFO", logger="mysql_mcp_server.audit")

    with patch("mysql_mcp_server.server._open_connection") as connector:
        response = await call_tool(
            "execute_sql",
            {
                "query": "SELECT id FROM orders",
                "connection": "test",
                "database": "gts",
            },
        )

    assert "Connections declaring database 'gts': test-gts" in response[0].text
    assert '"status":"denied"' in caplog.text
    assert '"requested_connection":"test"' in caplog.text
    assert '"requested_database":"gts"' in caplog.text
    connector.assert_not_called()


def test_invalid_profile_does_not_disable_valid_profiles(tmp_path: Path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
default = "good"

[connections.good]
host = "good-db"
user = "reader"
password_env = "GOOD_PASSWORD"
database = "app"

[connections.bad]
host = "bad-db"
password_env = "BAD_PASSWORD"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))
    monkeypatch.setenv("GOOD_PASSWORD", "secret")

    registry = load_connection_registry()

    assert registry.get("good").host == "good-db"
    assert "bad" in registry.errors
    with pytest.raises(ValueError, match="invalid"):
        registry.get("bad")


def test_profile_file_hot_reloads_after_change(tmp_path: Path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.dev]
host = "first"
user = "reader"
password = "secret"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))
    assert load_connection_registry().get().host == "first"

    profiles_file.write_text(
        """
[connections.dev]
host = "second-longer"
user = "reader"
password = "secret"
""",
        encoding="utf-8",
    )

    assert load_connection_registry().get().host == "second-longer"


def test_database_allowlist_rejects_overrides(tmp_path: Path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.prod]
host = "prod"
user = "reader"
password = "secret"
database = "app"
allowed_databases = ["app", "reporting"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))
    profile = load_connection_registry().get()

    assert ensure_database_allowed(profile, "reporting") == "reporting"
    with pytest.raises(ValueError, match="not allowed"):
        ensure_database_allowed(profile, "secret")


def test_connector_config_applies_socket_timeouts(tmp_path: Path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.dev]
host = "dev"
user = "reader"
password = "secret"
query_timeout_ms = 1500
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))

    config = build_connector_config(load_connection_registry().get())

    assert config["read_timeout"] == 2
    assert config["write_timeout"] == 2


def test_legacy_safety_limits_are_validated(monkeypatch):
    monkeypatch.setenv("MYSQL_MAX_ROWS", "1001")

    with pytest.raises(ValueError, match="MYSQL_MAX_ROWS"):
        load_connection_registry()


@pytest.mark.asyncio
async def test_validate_connections_reports_profile_local_errors_without_connecting(
    tmp_path: Path, monkeypatch
):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.good]
host = "good"
user = "reader"
password = "secret"

[connections.missing_secret]
host = "other"
user = "reader"
password_env = "NOT_CONFIGURED"

[connections.invalid]
host = "bad"
password = "secret"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))
    monkeypatch.delenv("NOT_CONFIGURED", raising=False)

    with patch("mysql_mcp_server.server.connect") as connector:
        response = await call_tool("validate_connections", {})

    payload = json.loads(response[0].text)
    assert payload["valid"] is False
    assert "invalid" in payload["errors"]
    missing = next(
        item for item in payload["profiles"] if item["name"] == "missing_secret"
    )
    assert missing["ready"] is False
    connector.assert_not_called()


def test_enterprise_audit_profile_settings_are_loaded(tmp_path: Path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.prod]
host = "prod"
user = "reader"
password = "secret"
audit_log_file = "C:/audit/mysql.jsonl"
audit_log_max_bytes = 20000000
audit_log_backup_count = 20
audit_hmac_key_env = "MYSQL_AUDIT_KEY"
audit_required_context = ["actor", "purpose", "ticket_id"]
audit_fail_closed = true
audit_fsync = true
allowed_functions = ["reviewed_metric"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))

    profile = load_connection_registry().get()

    assert profile.audit_log_file == "C:/audit/mysql.jsonl"
    assert profile.audit_log_max_bytes == 20_000_000
    assert profile.audit_log_backup_count == 20
    assert profile.audit_hmac_key_env == "MYSQL_AUDIT_KEY"
    assert profile.audit_required_context == ("actor", "purpose", "ticket_id")
    assert profile.audit_fail_closed is True
    assert profile.audit_fsync is True
    assert profile.allowed_functions == ("REVIEWED_METRIC",)


def test_fail_closed_audit_requires_a_durable_log(tmp_path: Path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.prod]
host = "prod"
user = "reader"
password = "secret"
audit_fail_closed = true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))

    registry = load_connection_registry()

    assert "prod" in registry.errors
    assert "requires audit_log_file" in registry.errors["prod"]


def test_explicit_logical_route_resolves_physical_target(tmp_path: Path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
default = "shared"

[connections.shared]
host = "core-db"
user = "reader"
password = "secret"
database = "core"
allowed_databases = ["core"]

[connections.shared-eam]
host = "eam-db"
user = "reader"
password = "secret"
database = "eam_physical"
allowed_databases = ["eam_physical"]

[routes.shared]
core = { connection = "shared", database = "core" }
eam = { connection = "shared-eam", database = "eam_physical" }
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))

    target = load_connection_registry(force_reload=True).resolve("shared", "eam")
    assert target.profile.name == "shared-eam"
    assert target.database == "eam_physical"
    assert target.requested_connection == "shared"
    assert target.requested_database == "eam"
    assert target.route_applied is True


@pytest.mark.asyncio
async def test_list_databases_returns_logical_aliases_without_connecting(
    tmp_path: Path, monkeypatch
):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.eam]
host = "eam-db"
user = "reader"
password = "secret"
database = "eam_physical"
allowed_databases = ["eam_physical"]

[routes.shared]
eam = { connection = "eam", database = "eam_physical" }
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))

    with patch("mysql_mcp_server.server._open_connection") as connector:
        response = await call_tool(
            "list_databases",
            {"connection": "shared", "result_format": "json"},
        )

    payload = json.loads(response[0].text)
    assert payload["rows"] == [["eam"]]
    assert payload["connection"] == "shared"
    connector.assert_not_called()


def test_invalid_logical_route_is_reported_without_disabling_profiles(
    tmp_path: Path, monkeypatch
):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.good]
host = "db"
user = "reader"
password = "secret"
database = "app"
allowed_databases = ["app"]

[routes.shared]
bad = { connection = "missing", database = "app" }
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))

    registry = load_connection_registry(force_reload=True)
    assert registry.get("good").database == "app"
    assert "unknown or invalid connection" in registry.errors["@route.shared.bad"]
