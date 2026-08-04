import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from mysql_mcp_server.config import load_connection_registry
from mysql_mcp_server.credential_store import CredentialProviderError
from mysql_mcp_server.credential_store import run_credential_command
from mysql_mcp_server.credentials_cli import credentials_main


class FakeKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, reference):
        return self.values.get((service, reference))

    def set_password(self, service, reference, password):
        self.values[(service, reference)] = password

    def delete_password(self, service, reference):
        del self.values[(service, reference)]


def _keyring_profile(tmp_path: Path, monkeypatch) -> Path:
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.secure]
host = "db.example"
user = "reader"
credential_provider = "keyring"
credential_ref = "secure-db"
database = "app"
allowed_databases = ["app"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))
    return profiles_file


def test_keyring_profile_resolves_without_plaintext_config(tmp_path, monkeypatch):
    _keyring_profile(tmp_path, monkeypatch)
    fake = FakeKeyring()
    fake.set_password("readonly-db-mcp", "secure-db", "runtime-secret")

    with patch(
        "mysql_mcp_server.credential_store._keyring_module",
        return_value=(fake, Exception),
    ):
        profile = load_connection_registry(force_reload=True).get("secure")
        assert profile.resolve_password() == "runtime-secret"
        assert profile.password is None
        assert profile.password_env is None


def test_missing_keyring_credential_does_not_expose_reference(tmp_path, monkeypatch):
    _keyring_profile(tmp_path, monkeypatch)
    with patch(
        "mysql_mcp_server.credential_store._keyring_module",
        return_value=(FakeKeyring(), Exception),
    ):
        with pytest.raises(CredentialProviderError) as error:
            load_connection_registry(force_reload=True).get("secure").resolve_password()
    assert "secure-db" not in str(error.value)


def test_command_provider_uses_argv_without_shell(tmp_path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    executable = sys.executable.replace("\\", "\\\\")
    profiles_file.write_text(
        f"""
[connections.command]
host = "db.example"
user = "reader"
credential_provider = "command"
credential_command = ["{executable}", "-c", "print('command-secret')"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))

    profile = load_connection_registry(force_reload=True).get("command")
    assert profile.resolve_password() == "command-secret"


def test_credentials_cli_stores_and_checks_keyring_secret(tmp_path, monkeypatch):
    profiles_file = _keyring_profile(tmp_path, monkeypatch)
    fake = FakeKeyring()
    with (
        patch(
            "mysql_mcp_server.credential_store._keyring_module",
            return_value=(fake, Exception),
        ),
        patch(
            "mysql_mcp_server.credentials_cli.getpass.getpass",
            side_effect=["new-secret", "new-secret"],
        ),
    ):
        args = ["--profiles-file", str(profiles_file), "set", "secure"]
        assert credentials_main(args) == 0
        assert (
            credentials_main(
                ["--profiles-file", str(profiles_file), "status", "secure"]
            )
            == 0
        )
    assert fake.values[("readonly-db-mcp", "secure-db")] == "new-secret"


def test_multiple_credential_sources_are_rejected(tmp_path, monkeypatch):
    profiles_file = tmp_path / "connections.toml"
    profiles_file.write_text(
        """
[connections.bad]
host = "db.example"
user = "reader"
password = "inline"
credential_provider = "keyring"
credential_ref = "bad"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PROFILES_FILE", str(profiles_file))
    registry = load_connection_registry(force_reload=True)
    assert "exactly one" in registry.errors["bad"]


def test_credential_command_rejects_multiple_output_lines():
    with pytest.raises(CredentialProviderError, match="exactly one"):
        run_credential_command(
            [sys.executable, "-c", "print('secret\\n')"],
            5,
        )
