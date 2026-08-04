"""Connection profile loading, validation, hot reload, and access policy."""

from __future__ import annotations

import math
import os
import re
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .credential_store import get_keyring_password, run_credential_command

PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_$]+$")
CREDENTIAL_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9_.:/-]+$")
RESULT_FORMATS = {"csv", "json"}
SSL_MODES = {"DISABLED", "REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY"}
CREDENTIAL_PROVIDERS = {"keyring", "command"}
DEFAULT_MASK_COLUMNS = (
    "password",
    "passwd",
    "*secret*",
    "*token*",
    "*api_key*",
    "*private_key*",
    "*ssn*",
    "*id_card*",
    "*phone*",
    "*email*",
)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    raise ValueError(f"Expected a boolean value, got {type(value).__name__}")


def _bounded_int(
    value: Any,
    *,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    number = default if value is None else int(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def _bounded_float(
    value: Any,
    *,
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    number = default if value is None else float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def _validate_identifier(name: str, *, label: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(name):
        raise ValueError(f"Invalid {label} '{name}': use letters, numbers, '_' or '$'")
    return name


@dataclass(frozen=True)
class SshConfig:
    enabled: bool = False
    host: str | None = None
    port: int = 22
    user: str | None = None
    key_path: str | None = None
    remote_host: str = "localhost"
    remote_port: int = 3306
    local_port: int = 0
    startup_timeout: float = 5.0


@dataclass(frozen=True)
class ConnectionProfile:
    name: str
    host: str
    port: int
    user: str
    password: str | None = None
    password_env: str | None = None
    credential_provider: str | None = None
    credential_ref: str = ""
    credential_service: str = "readonly-db-mcp"
    credential_command: tuple[str, ...] = ()
    credential_timeout_seconds: float = 5.0
    database: str | None = None
    allowed_databases: tuple[str, ...] = ()
    allowed_functions: tuple[str, ...] = ()
    allow_system_databases: bool = False
    description: str = ""
    charset: str = "utf8mb4"
    collation: str = "utf8mb4_unicode_ci"
    sql_mode: str = "TRADITIONAL"
    connect_timeout: int = 10
    query_timeout_ms: int = 30_000
    max_rows: int = 500
    max_cell_length: int = 20_000
    mask_columns: tuple[str, ...] = DEFAULT_MASK_COLUMNS
    result_format: str = "csv"
    pool_size: int = 5
    audit_enabled: bool = True
    audit_log_file: str | None = None
    audit_log_max_bytes: int = 10_000_000
    audit_log_backup_count: int = 10
    audit_hmac_key_env: str | None = None
    audit_required_context: tuple[str, ...] = ()
    audit_fail_closed: bool = False
    audit_fsync: bool = False
    auth_plugin: str | None = None
    use_pure: bool = False
    raise_on_warnings: bool = False
    ssl_mode: str = "REQUIRED"
    ssl_ca: str | None = None
    ssh: SshConfig = SshConfig()

    def resolve_password(self) -> str:
        if self.password_env:
            value = os.getenv(self.password_env)
            if value is None:
                raise ValueError(
                    f"Connection '{self.name}' requires environment variable "
                    f"'{self.password_env}'"
                )
            return value
        if self.password is not None:
            return self.password
        if self.credential_provider == "keyring":
            return get_keyring_password(self.credential_service, self.credential_ref)
        if self.credential_provider == "command":
            return run_credential_command(
                self.credential_command,
                self.credential_timeout_seconds,
            )
        raise ValueError(
            f"Connection '{self.name}' does not have a credential provider"
        )

    def runtime_status(self) -> tuple[bool, str]:
        if self.credential_provider != "command":
            try:
                self.resolve_password()
            except ValueError as exc:
                return False, str(exc)
        if self.audit_hmac_key_env and os.getenv(self.audit_hmac_key_env) is None:
            return (
                False,
                f"Connection '{self.name}' requires audit signing environment "
                f"variable '{self.audit_hmac_key_env}'",
            )
        if self.credential_provider == "command":
            return True, "configured; credential command is checked when used"
        return True, "ready"


@dataclass(frozen=True)
class ConnectionRegistry:
    profiles: dict[str, ConnectionProfile]
    default: str | None
    source: str
    errors: dict[str, str] = field(default_factory=dict)
    routes: dict[str, dict[str, "RouteTarget"]] = field(default_factory=dict)

    def get(self, name: str | None = None) -> ConnectionProfile:
        selected = name or self.default
        if selected is None:
            details = "; ".join(
                f"{profile}: {error}" for profile, error in self.errors.items()
            )
            raise ValueError(f"No valid MySQL connections are configured. {details}")
        try:
            return self.profiles[selected]
        except KeyError as exc:
            if selected in self.errors:
                raise ValueError(
                    f"Connection '{selected}' is invalid: {self.errors[selected]}"
                ) from exc
            available = ", ".join(sorted(self.profiles)) or "(none)"
            raise ValueError(
                f"Unknown connection '{selected}'. Available connections: {available}"
            ) from exc

    def resolve(
        self, name: str | None = None, database: str | None = None
    ) -> "ResolvedTarget":
        selected_name = name or self.default
        if selected_name and database and selected_name in self.routes:
            route = self.routes[selected_name].get(database)
            if route is not None:
                profile = self.get(route.connection)
                selected_database = ensure_database_allowed(profile, route.database)
                return ResolvedTarget(
                    profile=profile,
                    database=selected_database,
                    requested_connection=selected_name,
                    requested_database=database,
                    route_applied=True,
                )
        if (
            selected_name
            and selected_name in self.routes
            and selected_name not in self.profiles
        ):
            available = ", ".join(sorted(self.routes[selected_name]))
            raise ValueError(
                f"Logical connection '{selected_name}' requires a database route. "
                f"Available database aliases: {available}"
            )
        profile = self.get(selected_name)
        try:
            selected_database = ensure_database_allowed(profile, database)
        except ValueError as exc:
            candidates = sorted(
                candidate.name
                for candidate in self.profiles.values()
                if candidate.name != profile.name
                and database
                and (
                    candidate.database == database
                    or database in candidate.allowed_databases
                )
            )
            routes = sorted(self.routes.get(selected_name or "", {}))
            hints = []
            if candidates:
                hints.append(
                    f"Connections declaring database '{database}': "
                    + ", ".join(candidates)
                )
            if routes:
                hints.append("route aliases: " + ", ".join(routes))
            if not hints:
                raise
            raise ValueError(f"{exc}. " + "; ".join(hints)) from exc
        return ResolvedTarget(
            profile=profile,
            database=selected_database,
            requested_connection=selected_name,
            requested_database=database,
            route_applied=False,
        )


@dataclass(frozen=True)
class RouteTarget:
    connection: str
    database: str


@dataclass(frozen=True)
class ResolvedTarget:
    profile: ConnectionProfile
    database: str | None
    requested_connection: str | None
    requested_database: str | None
    route_applied: bool


_cache_lock = threading.RLock()
_registry_cache_key: tuple[Any, ...] | None = None
_registry_cache: ConnectionRegistry | None = None


def clear_connection_registry_cache() -> None:
    global _registry_cache_key, _registry_cache
    with _cache_lock:
        _registry_cache_key = None
        _registry_cache = None


def _validate_profile_name(name: str) -> str:
    if not PROFILE_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"Invalid connection name '{name}': use letters, numbers, '.', '_' or '-'"
        )
    return name


def _credential_fields(name: str, values: dict[str, Any]) -> tuple[
    str | None,
    str | None,
    str | None,
    str,
    str,
    tuple[str, ...],
    float,
]:
    password_env = values.get("password_env")
    has_password = "password" in values
    provider_value = values.get("credential_provider")
    provider = str(provider_value).strip().lower() if provider_value else None
    source_count = sum((bool(password_env), has_password, provider is not None))
    if source_count != 1:
        raise ValueError(
            f"Connection '{name}' must define exactly one of password_env, "
            "password, or credential_provider"
        )

    if password_env:
        env_name = str(password_env)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
            raise ValueError(
                f"Connection '{name}' has invalid password_env '{env_name}'"
            )
        return None, env_name, None, "", "readonly-db-mcp", (), 5.0
    if has_password:
        return str(values["password"]), None, None, "", "readonly-db-mcp", (), 5.0

    if provider not in CREDENTIAL_PROVIDERS:
        supported = ", ".join(sorted(CREDENTIAL_PROVIDERS))
        raise ValueError(
            f"Connection '{name}'.credential_provider must be one of: {supported}"
        )
    timeout = _bounded_float(
        values.get("credential_timeout_seconds"),
        name=f"Connection '{name}'.credential_timeout_seconds",
        default=5.0,
        minimum=0.1,
        maximum=60.0,
    )
    if provider == "keyring":
        reference = str(values.get("credential_ref", "")).strip()
        service = str(values.get("credential_service", "readonly-db-mcp")).strip()
        if not reference or not CREDENTIAL_REFERENCE_PATTERN.fullmatch(reference):
            raise ValueError(
                f"Connection '{name}'.credential_ref must use letters, numbers, "
                "'.', '_', ':', '/' or '-'"
            )
        if not service or not CREDENTIAL_REFERENCE_PATTERN.fullmatch(service):
            raise ValueError(
                f"Connection '{name}'.credential_service contains invalid characters"
            )
        if values.get("credential_command"):
            raise ValueError(
                f"Connection '{name}'.credential_command is only valid for the "
                "command provider"
            )
        return None, None, provider, reference, service, (), timeout

    raw_command = values.get("credential_command")
    if not isinstance(raw_command, list) or not 1 <= len(raw_command) <= 32:
        raise ValueError(
            f"Connection '{name}'.credential_command must be an array with 1-32 items"
        )
    if any(not isinstance(item, str) for item in raw_command):
        raise ValueError(
            f"Connection '{name}'.credential_command items must be strings"
        )
    command = tuple(raw_command)
    if any(not item or "\x00" in item for item in command):
        raise ValueError(
            f"Connection '{name}'.credential_command items must be non-empty strings"
        )
    if values.get("credential_ref") or values.get("credential_service"):
        raise ValueError(
            f"Connection '{name}'.credential_ref and credential_service are only "
            "valid for the keyring provider"
        )
    return None, None, provider, "", "readonly-db-mcp", command, timeout


def _profile_from_toml(name: str, values: dict[str, Any]) -> ConnectionProfile:
    _validate_profile_name(name)
    user = values.get("user")
    if not user:
        raise ValueError(f"Connection '{name}' must define user")
    (
        password,
        password_env,
        credential_provider,
        credential_ref,
        credential_service,
        credential_command,
        credential_timeout_seconds,
    ) = _credential_fields(name, values)

    database = str(values["database"]) if values.get("database") else None
    if database:
        _validate_identifier(database, label="database")

    raw_allowed = values.get("allowed_databases") or []
    if not isinstance(raw_allowed, list):
        raise ValueError(f"Connection '{name}'.allowed_databases must be an array")
    allowed_databases = tuple(
        _validate_identifier(str(item), label="allowed database")
        for item in raw_allowed
    )
    raw_allowed_functions = values.get("allowed_functions") or []
    if not isinstance(raw_allowed_functions, list):
        raise ValueError(f"Connection '{name}'.allowed_functions must be an array")
    allowed_functions = tuple(
        _validate_identifier(str(item), label="allowed function").upper()
        for item in raw_allowed_functions
    )
    raw_mask_columns = values.get("mask_columns", DEFAULT_MASK_COLUMNS)
    if not isinstance(raw_mask_columns, (list, tuple)):
        raise ValueError(f"Connection '{name}'.mask_columns must be an array")
    mask_columns = tuple(
        str(item).strip().lower() for item in raw_mask_columns if str(item).strip()
    )
    if database and allowed_databases and database not in allowed_databases:
        raise ValueError(
            f"Connection '{name}' default database '{database}' is not in "
            "allowed_databases"
        )

    result_format = str(values.get("result_format", "csv")).lower()
    if result_format not in RESULT_FORMATS:
        raise ValueError(f"Connection '{name}'.result_format must be csv or json")
    ssl_mode = str(values.get("ssl_mode", "REQUIRED")).upper()
    if ssl_mode not in SSL_MODES:
        raise ValueError(
            f"Connection '{name}'.ssl_mode must be DISABLED, REQUIRED, "
            "VERIFY_CA or VERIFY_IDENTITY"
        )

    ssl_ca = str(values["ssl_ca"]) if values.get("ssl_ca") else None
    if ssl_mode in {"VERIFY_CA", "VERIFY_IDENTITY"} and not ssl_ca:
        raise ValueError(f"Connection '{name}'.ssl_mode={ssl_mode} requires ssl_ca")

    raw_required_context = values.get("audit_required_context") or []
    if not isinstance(raw_required_context, list):
        raise ValueError(f"Connection '{name}'.audit_required_context must be an array")
    required_context = tuple(str(item) for item in raw_required_context)
    invalid_context = sorted(set(required_context) - {"actor", "purpose", "ticket_id"})
    if invalid_context:
        raise ValueError(
            f"Connection '{name}'.audit_required_context contains unsupported "
            f"fields: {', '.join(invalid_context)}"
        )
    audit_log_file = (
        str(values["audit_log_file"]) if values.get("audit_log_file") else None
    )
    audit_enabled = _as_bool(values.get("audit_enabled"), True)
    audit_fail_closed = _as_bool(values.get("audit_fail_closed"))
    if audit_fail_closed and not audit_log_file:
        raise ValueError(
            f"Connection '{name}'.audit_fail_closed requires audit_log_file"
        )
    audit_hmac_key_env = (
        str(values["audit_hmac_key_env"]) if values.get("audit_hmac_key_env") else None
    )
    if audit_hmac_key_env and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", audit_hmac_key_env
    ):
        raise ValueError(
            f"Connection '{name}' has invalid audit_hmac_key_env "
            f"'{audit_hmac_key_env}'"
        )
    if not audit_enabled and (
        audit_fail_closed or required_context or audit_hmac_key_env
    ):
        raise ValueError(
            f"Connection '{name}' cannot require audit context, signing, or "
            "fail-closed behavior when audit_enabled=false"
        )

    ssh_values = values.get("ssh") or {}
    if not isinstance(ssh_values, dict):
        raise ValueError(f"Connection '{name}'.ssh must be a table")

    return ConnectionProfile(
        name=name,
        description=str(values.get("description", "")),
        host=str(values.get("host", "localhost")),
        port=_bounded_int(
            values.get("port"),
            name=f"Connection '{name}'.port",
            default=3306,
            minimum=1,
            maximum=65535,
        ),
        user=str(user),
        password=password,
        password_env=password_env,
        credential_provider=credential_provider,
        credential_ref=credential_ref,
        credential_service=credential_service,
        credential_command=credential_command,
        credential_timeout_seconds=credential_timeout_seconds,
        database=database,
        allowed_databases=allowed_databases,
        allowed_functions=allowed_functions,
        allow_system_databases=_as_bool(values.get("allow_system_databases")),
        charset=str(values.get("charset", "utf8mb4")),
        collation=str(values.get("collation", "utf8mb4_unicode_ci")),
        sql_mode=str(values.get("sql_mode", "TRADITIONAL")),
        connect_timeout=_bounded_int(
            values.get("connect_timeout"),
            name=f"Connection '{name}'.connect_timeout",
            default=10,
            minimum=1,
            maximum=300,
        ),
        query_timeout_ms=_bounded_int(
            values.get("query_timeout_ms"),
            name=f"Connection '{name}'.query_timeout_ms",
            default=30_000,
            minimum=100,
            maximum=300_000,
        ),
        max_rows=_bounded_int(
            values.get("max_rows"),
            name=f"Connection '{name}'.max_rows",
            default=500,
            minimum=1,
            maximum=1000,
        ),
        max_cell_length=_bounded_int(
            values.get("max_cell_length"),
            name=f"Connection '{name}'.max_cell_length",
            default=20_000,
            minimum=100,
            maximum=1_000_000,
        ),
        mask_columns=mask_columns,
        result_format=result_format,
        pool_size=_bounded_int(
            values.get("pool_size"),
            name=f"Connection '{name}'.pool_size",
            default=5,
            minimum=0,
            maximum=32,
        ),
        audit_enabled=audit_enabled,
        audit_log_file=audit_log_file,
        audit_log_max_bytes=_bounded_int(
            values.get("audit_log_max_bytes"),
            name=f"Connection '{name}'.audit_log_max_bytes",
            default=10_000_000,
            minimum=10_000,
            maximum=1_000_000_000,
        ),
        audit_log_backup_count=_bounded_int(
            values.get("audit_log_backup_count"),
            name=f"Connection '{name}'.audit_log_backup_count",
            default=10,
            minimum=1,
            maximum=100,
        ),
        audit_hmac_key_env=audit_hmac_key_env,
        audit_required_context=required_context,
        audit_fail_closed=audit_fail_closed,
        audit_fsync=_as_bool(values.get("audit_fsync")),
        auth_plugin=(str(values["auth_plugin"]) if values.get("auth_plugin") else None),
        use_pure=_as_bool(values.get("use_pure")),
        raise_on_warnings=_as_bool(values.get("raise_on_warnings")),
        ssl_mode=ssl_mode,
        ssl_ca=ssl_ca,
        ssh=SshConfig(
            enabled=_as_bool(ssh_values.get("enabled")),
            host=str(ssh_values["host"]) if ssh_values.get("host") else None,
            port=_bounded_int(
                ssh_values.get("port"),
                name=f"Connection '{name}'.ssh.port",
                default=22,
                minimum=1,
                maximum=65535,
            ),
            user=str(ssh_values["user"]) if ssh_values.get("user") else None,
            key_path=(
                str(ssh_values["key_path"]) if ssh_values.get("key_path") else None
            ),
            remote_host=str(ssh_values.get("remote_host", "localhost")),
            remote_port=_bounded_int(
                ssh_values.get("remote_port"),
                name=f"Connection '{name}'.ssh.remote_port",
                default=3306,
                minimum=1,
                maximum=65535,
            ),
            local_port=_bounded_int(
                ssh_values.get("local_port"),
                name=f"Connection '{name}'.ssh.local_port",
                default=0,
                minimum=0,
                maximum=65535,
            ),
            startup_timeout=_bounded_float(
                ssh_values.get("startup_timeout"),
                name=f"Connection '{name}'.ssh.startup_timeout",
                default=5.0,
                minimum=0.1,
                maximum=60,
            ),
        ),
    )


def _legacy_profile() -> ConnectionProfile:
    user = os.getenv("MYSQL_USER")
    password = os.getenv("MYSQL_PASSWORD")
    if not user:
        raise ValueError(
            "Missing required database configuration: MYSQL_USER is required"
        )
    if password is None:
        raise ValueError(
            "Missing required database configuration: MYSQL_PASSWORD must be set"
        )

    database = os.getenv("MYSQL_DATABASE") or None
    raw_allowed = os.getenv("MYSQL_ALLOWED_DATABASES", "")
    allowed = tuple(
        _validate_identifier(item.strip(), label="allowed database")
        for item in raw_allowed.split(",")
        if item.strip()
    )
    allowed_functions = tuple(
        _validate_identifier(item.strip(), label="allowed function").upper()
        for item in os.getenv("MYSQL_ALLOWED_FUNCTIONS", "").split(",")
        if item.strip()
    )
    raw_mask_columns = os.getenv("MYSQL_MASK_COLUMNS")
    mask_columns = (
        DEFAULT_MASK_COLUMNS
        if raw_mask_columns is None
        else tuple(
            item.strip().lower() for item in raw_mask_columns.split(",") if item.strip()
        )
    )
    if database and allowed and database not in allowed:
        raise ValueError(
            f"MYSQL_DATABASE '{database}' is not in MYSQL_ALLOWED_DATABASES"
        )
    result_format = os.getenv("MYSQL_RESULT_FORMAT", "csv").lower()
    if result_format not in RESULT_FORMATS:
        raise ValueError("MYSQL_RESULT_FORMAT must be csv or json")
    ssl_mode = os.getenv("MYSQL_SSL_MODE", "REQUIRED").upper()
    if ssl_mode not in SSL_MODES:
        raise ValueError(
            "MYSQL_SSL_MODE must be DISABLED, REQUIRED, VERIFY_CA or VERIFY_IDENTITY"
        )
    ssl_ca = os.getenv("MYSQL_SSL_CA") or None
    if ssl_mode in {"VERIFY_CA", "VERIFY_IDENTITY"} and not ssl_ca:
        raise ValueError(f"MYSQL_SSL_MODE={ssl_mode} requires MYSQL_SSL_CA")

    required_context = tuple(
        item.strip()
        for item in os.getenv("MYSQL_AUDIT_REQUIRED_CONTEXT", "").split(",")
        if item.strip()
    )
    invalid_context = sorted(set(required_context) - {"actor", "purpose", "ticket_id"})
    if invalid_context:
        raise ValueError(
            "MYSQL_AUDIT_REQUIRED_CONTEXT contains unsupported fields: "
            + ", ".join(invalid_context)
        )
    audit_log_file = os.getenv("MYSQL_AUDIT_LOG_FILE") or None
    audit_enabled = _env_bool("MYSQL_AUDIT_ENABLED", True)
    audit_fail_closed = _env_bool("MYSQL_AUDIT_FAIL_CLOSED")
    if audit_fail_closed and not audit_log_file:
        raise ValueError("MYSQL_AUDIT_FAIL_CLOSED requires MYSQL_AUDIT_LOG_FILE")
    audit_hmac_key_env = os.getenv("MYSQL_AUDIT_HMAC_KEY_ENV") or None
    if audit_hmac_key_env and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", audit_hmac_key_env
    ):
        raise ValueError("MYSQL_AUDIT_HMAC_KEY_ENV is not a valid environment name")
    if not audit_enabled and (
        audit_fail_closed or required_context or audit_hmac_key_env
    ):
        raise ValueError(
            "Audit context, signing, and fail-closed behavior require "
            "MYSQL_AUDIT_ENABLED=true"
        )

    return ConnectionProfile(
        name="default",
        description="Legacy MYSQL_* environment configuration",
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=_bounded_int(
            os.getenv("MYSQL_PORT"),
            name="MYSQL_PORT",
            default=3306,
            minimum=1,
            maximum=65535,
        ),
        user=user,
        password=password,
        database=database,
        allowed_databases=allowed,
        allowed_functions=allowed_functions,
        allow_system_databases=_env_bool("MYSQL_ALLOW_SYSTEM_DATABASES"),
        charset=os.getenv("MYSQL_CHARSET", "utf8mb4"),
        collation=os.getenv("MYSQL_COLLATION", "utf8mb4_unicode_ci"),
        sql_mode=os.getenv("MYSQL_SQL_MODE", "TRADITIONAL"),
        connect_timeout=_bounded_int(
            os.getenv("MYSQL_CONNECT_TIMEOUT"),
            name="MYSQL_CONNECT_TIMEOUT",
            default=10,
            minimum=1,
            maximum=300,
        ),
        query_timeout_ms=_bounded_int(
            os.getenv("MYSQL_QUERY_TIMEOUT_MS"),
            name="MYSQL_QUERY_TIMEOUT_MS",
            default=30_000,
            minimum=100,
            maximum=300_000,
        ),
        max_rows=_bounded_int(
            os.getenv("MYSQL_MAX_ROWS"),
            name="MYSQL_MAX_ROWS",
            default=500,
            minimum=1,
            maximum=1000,
        ),
        max_cell_length=_bounded_int(
            os.getenv("MYSQL_MAX_CELL_LENGTH"),
            name="MYSQL_MAX_CELL_LENGTH",
            default=20_000,
            minimum=100,
            maximum=1_000_000,
        ),
        mask_columns=mask_columns,
        result_format=result_format,
        pool_size=_bounded_int(
            os.getenv("MYSQL_POOL_SIZE"),
            name="MYSQL_POOL_SIZE",
            default=0,
            minimum=0,
            maximum=32,
        ),
        audit_enabled=audit_enabled,
        audit_log_file=audit_log_file,
        audit_log_max_bytes=_bounded_int(
            os.getenv("MYSQL_AUDIT_LOG_MAX_BYTES"),
            name="MYSQL_AUDIT_LOG_MAX_BYTES",
            default=10_000_000,
            minimum=10_000,
            maximum=1_000_000_000,
        ),
        audit_log_backup_count=_bounded_int(
            os.getenv("MYSQL_AUDIT_LOG_BACKUP_COUNT"),
            name="MYSQL_AUDIT_LOG_BACKUP_COUNT",
            default=10,
            minimum=1,
            maximum=100,
        ),
        audit_hmac_key_env=audit_hmac_key_env,
        audit_required_context=required_context,
        audit_fail_closed=audit_fail_closed,
        audit_fsync=_env_bool("MYSQL_AUDIT_FSYNC"),
        auth_plugin=os.getenv("MYSQL_AUTH_PLUGIN") or None,
        use_pure=_env_bool("MYSQL_USE_PURE"),
        raise_on_warnings=_env_bool("MYSQL_RAISE_ON_WARNINGS"),
        ssl_mode=ssl_mode,
        ssl_ca=ssl_ca,
        ssh=SshConfig(
            enabled=_env_bool("MYSQL_SSH_ENABLE"),
            host=os.getenv("MYSQL_SSH_HOST") or None,
            port=_bounded_int(
                os.getenv("MYSQL_SSH_PORT"),
                name="MYSQL_SSH_PORT",
                default=22,
                minimum=1,
                maximum=65535,
            ),
            user=os.getenv("MYSQL_SSH_USER") or None,
            key_path=os.getenv("MYSQL_SSH_KEY_PATH") or None,
            remote_host=os.getenv("MYSQL_SSH_REMOTE_HOST", "localhost"),
            remote_port=_bounded_int(
                os.getenv("MYSQL_SSH_REMOTE_PORT"),
                name="MYSQL_SSH_REMOTE_PORT",
                default=3306,
                minimum=1,
                maximum=65535,
            ),
            local_port=_bounded_int(
                os.getenv("MYSQL_LOCAL_PORT"),
                name="MYSQL_LOCAL_PORT",
                default=3330,
                minimum=0,
                maximum=65535,
            ),
            startup_timeout=_bounded_float(
                os.getenv("MYSQL_SSH_STARTUP_TIMEOUT"),
                name="MYSQL_SSH_STARTUP_TIMEOUT",
                default=5.0,
                minimum=0.1,
                maximum=60,
            ),
        ),
    )


def _load_file_registry(path: Path) -> ConnectionRegistry:
    with path.open("rb") as file:
        document = tomllib.load(file)

    connection_values = document.get("connections")
    if not isinstance(connection_values, dict) or not connection_values:
        raise ValueError(
            "Profiles file must contain at least one [connections.NAME] table"
        )

    profiles: dict[str, ConnectionProfile] = {}
    errors: dict[str, str] = {}
    for name, values in connection_values.items():
        if not isinstance(values, dict):
            errors[name] = "connection entry must be a TOML table"
            continue
        try:
            profiles[name] = _profile_from_toml(name, values)
        except (TypeError, ValueError) as exc:
            errors[name] = str(exc)

    requested_default = str(
        os.getenv("MYSQL_DEFAULT_CONNECTION")
        or document.get("default")
        or next(iter(connection_values))
    )
    default = requested_default if requested_default in profiles else None
    if default is None and profiles:
        errors["@default"] = (
            f"Default connection '{requested_default}' is invalid or undefined; "
            f"using '{next(iter(profiles))}'"
        )
        default = next(iter(profiles))

    routes: dict[str, dict[str, RouteTarget]] = {}
    route_values = document.get("routes") or {}
    if not isinstance(route_values, dict):
        errors["@routes"] = "routes must be a TOML table"
    else:
        for logical_name, mappings in route_values.items():
            try:
                _validate_profile_name(str(logical_name))
                if not isinstance(mappings, dict) or not mappings:
                    raise ValueError("route group must be a non-empty TOML table")
            except (TypeError, ValueError) as exc:
                errors[f"@route.{logical_name}"] = str(exc)
                continue
            targets: dict[str, RouteTarget] = {}
            for alias, target_values in mappings.items():
                error_key = f"@route.{logical_name}.{alias}"
                try:
                    alias_name = _validate_identifier(
                        str(alias), label="route database alias"
                    )
                    if not isinstance(target_values, dict):
                        raise ValueError("route target must be an inline TOML table")
                    target_connection = _validate_profile_name(
                        str(target_values.get("connection", ""))
                    )
                    target_database = _validate_identifier(
                        str(target_values.get("database", "")),
                        label="route target database",
                    )
                    if target_connection not in profiles:
                        raise ValueError(
                            f"route references unknown or invalid connection "
                            f"'{target_connection}'"
                        )
                    ensure_database_allowed(
                        profiles[target_connection], target_database
                    )
                    targets[alias_name] = RouteTarget(
                        connection=target_connection,
                        database=target_database,
                    )
                except (TypeError, ValueError) as exc:
                    errors[error_key] = str(exc)
            if targets:
                routes[str(logical_name)] = targets

    return ConnectionRegistry(
        profiles=profiles,
        default=default,
        source=str(path),
        errors=errors,
        routes=routes,
    )


def load_connection_registry(*, force_reload: bool = False) -> ConnectionRegistry:
    """Hot-reload named profiles, or fall back to legacy MYSQL_* variables."""
    configured_path = os.getenv("MYSQL_PROFILES_FILE") or os.getenv(
        "MYSQL_CONNECTIONS_FILE"
    )
    if not configured_path:
        profile = _legacy_profile()
        return ConnectionRegistry(
            profiles={profile.name: profile},
            default=profile.name,
            source="environment",
        )

    path = Path(configured_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError("MySQL profiles file does not exist")
    stat = path.stat()
    cache_key = (
        str(path),
        stat.st_mtime_ns,
        stat.st_size,
        os.getenv("MYSQL_DEFAULT_CONNECTION"),
    )

    global _registry_cache_key, _registry_cache
    with _cache_lock:
        if (
            not force_reload
            and _registry_cache_key == cache_key
            and _registry_cache is not None
        ):
            return _registry_cache
        registry = _load_file_registry(path)
        _registry_cache_key = cache_key
        _registry_cache = registry
        return registry


def ensure_database_allowed(
    profile: ConnectionProfile, database: str | None
) -> str | None:
    selected = database or profile.database
    if selected:
        _validate_identifier(selected, label="database")
    if (
        selected
        and profile.allowed_databases
        and selected not in profile.allowed_databases
    ):
        allowed = ", ".join(profile.allowed_databases)
        raise ValueError(
            f"Database '{selected}' is not allowed for connection "
            f"'{profile.name}'. Allowed databases: {allowed}"
        )
    return selected


def build_connector_config(
    profile: ConnectionProfile,
    *,
    database: str | None = None,
    host: str | None = None,
    port: int | None = None,
    query_timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Build mysql-connector arguments without exposing profile-only metadata."""
    selected_database = ensure_database_allowed(profile, database)
    timeout_ms = query_timeout_ms or profile.query_timeout_ms
    socket_timeout = max(1, math.ceil(timeout_ms / 1000))
    config: dict[str, Any] = {
        "host": host or profile.host,
        "port": port or profile.port,
        "user": profile.user,
        "password": profile.resolve_password(),
        "charset": profile.charset,
        "collation": profile.collation,
        "autocommit": False,
        "sql_mode": profile.sql_mode,
        "connect_timeout": profile.connect_timeout,
        "read_timeout": socket_timeout,
        "write_timeout": socket_timeout,
        "auth_plugin": profile.auth_plugin,
        "use_pure": profile.use_pure,
        "raise_on_warnings": profile.raise_on_warnings,
    }
    if selected_database:
        config["database"] = selected_database

    if profile.ssl_mode == "DISABLED":
        config["ssl_disabled"] = True
    elif profile.ssl_mode == "REQUIRED":
        config["ssl_disabled"] = False
    elif profile.ssl_mode in {"VERIFY_CA", "VERIFY_IDENTITY"}:
        config["ssl_verify_cert"] = True
        if profile.ssl_mode == "VERIFY_IDENTITY":
            config["ssl_verify_identity"] = True
        if profile.ssl_ca:
            config["ssl_ca"] = profile.ssl_ca

    config = {key: value for key, value in config.items() if value is not None}
    if config.get("charset") == "":
        del config["charset"]
    if config.get("collation") == "":
        del config["collation"]
    return config
