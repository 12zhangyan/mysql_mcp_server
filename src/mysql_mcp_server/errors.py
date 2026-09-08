"""Safe, actionable failures without driver messages or SQL literals."""

from __future__ import annotations

from typing import Any


class QueryFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        next_action: str,
        *,
        retryable: bool = False,
        **context: Any,
    ):
        self.payload = {
            "code": code,
            "message": message,
            "retryable": retryable,
            "next_action": next_action,
            **context,
        }
        reference = ",".join(
            f"{key}={context[key]}"
            for key in ("error_type", "phase", "errno", "sqlstate")
            if context.get(key) is not None
        )
        super().__init__(f"{message} ({reference})" if reference else message)


def database_failure(exc, *, phase: str, **context: Any) -> QueryFailure:
    errno = getattr(exc, "errno", None)
    code, message, action = {
        1146: (
            "TABLE_NOT_FOUND",
            "A referenced table does not exist in the selected database.",
            "Keep the intended connection/database; call list_tables (optionally with search), then get_schema_info. Correct the SQL using verified names; do not repeat unchanged SQL or switch databases automatically.",
        ),
        1054: (
            "COLUMN_NOT_FOUND",
            "A referenced column could not be resolved.",
            "Call get_schema_info for the referenced tables and verify column names and aliases before correcting the SQL.",
        ),
        1064: (
            "SQL_SYNTAX_ERROR",
            "MySQL could not parse the SQL statement.",
            "Check MySQL syntax, aliases and server-version compatibility; do not repeat unchanged SQL.",
        ),
        1049: (
            "DATABASE_NOT_FOUND",
            "The selected database does not exist.",
            "Check list_connections and list_databases for the intended environment.",
        ),
        1045: (
            "AUTHENTICATION_FAILED",
            "Database authentication failed.",
            "Ask the connection owner to check credentials; never include credentials in SQL or tool messages.",
        ),
        1044: (
            "ACCESS_DENIED",
            "Database access was denied.",
            "Check the intended connection and its allowed database scope.",
        ),
        1142: (
            "ACCESS_DENIED",
            "Table access was denied.",
            "Ask the connection owner to verify read permissions; do not switch identities automatically.",
        ),
    }.get(
        1146 if errno == 1109 else errno if isinstance(errno, int) else 0,
        (
            "DATABASE_QUERY_FAILED",
            "MySQL read-only query failed",
            "Check the safe error code and target context before retrying. Do not repeat a deterministic SQL failure unchanged.",
        ),
    )
    return QueryFailure(
        code,
        message,
        action,
        error_type=type(exc).__name__,
        phase=phase,
        errno=errno,
        sqlstate=getattr(exc, "sqlstate", None),
        **context,
    )
