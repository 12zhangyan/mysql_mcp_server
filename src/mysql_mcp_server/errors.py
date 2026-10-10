"""Safe, actionable failures without driver messages or SQL literals."""

from __future__ import annotations

from typing import Any

from mysql.connector.errors import InterfaceError, ReadTimeoutError, WriteTimeoutError

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


def retryable_connector_read(exc: Exception, *, phase: str) -> bool:
    """Retain the narrow legacy InterfaceError retry, never replay socket timeouts."""
    return (
        isinstance(exc, InterfaceError)
        and getattr(exc, "errno", None) == -1
        and phase in {"execute", "fetch"}
    )


def database_failure(exc, *, phase: str, **context: Any) -> QueryFailure:
    errno = getattr(exc, "errno", None)
    if isinstance(exc, (ReadTimeoutError, WriteTimeoutError)):
        return QueryFailure(
            "QUERY_TIMEOUT",
            "The database socket operation exceeded its read or write time limit.",
            "Check database and network health; reduce the query scope or inspect "
            "its plan with explain_sql before retrying. The query was not replayed "
            "automatically and no partial result was delivered.",
            error_type=type(exc).__name__,
            phase=phase,
            errno=errno,
            sqlstate=getattr(exc, "sqlstate", None),
            **context,
        )
    canonical_errno = (
        {
            1109: 1146,
            1969: 3024,
            1203: 1040,
            2002: 2003,
            2005: 2003,
            2006: 2013,
            2055: 2013,
        }.get(errno, errno)
        if isinstance(errno, int)
        else None
    )
    code, message, action = {
        3024: (
            "QUERY_TIMEOUT",
            "The database stopped the query after its execution time limit.",
            "Reduce the query scope or inspect its plan with explain_sql. Do not repeat an expensive query unchanged or raise profile limits automatically.",
        ),
        1205: (
            "LOCK_WAIT_TIMEOUT",
            "The query exceeded the database lock wait limit.",
            "Ask the database owner to inspect blocking transactions or schema changes. Retry with backoff only after contention subsides; do not kill sessions automatically.",
        ),
        1213: (
            "DEADLOCK",
            "The database stopped the transaction after detecting a deadlock.",
            "Retry the read-only query with bounded backoff. If the deadlock recurs, ask the database owner to inspect concurrent transactions.",
        ),
        1040: (
            "CONNECTION_BUSY",
            "The database connection limit has been reached.",
            "Reduce concurrency and retry with bounded backoff on the same connection; ask the database owner to check capacity if this persists.",
        ),
        2003: (
            "CONNECTION_FAILED",
            "A connection to the selected database could not be established.",
            "Check the configured target, network and database availability. Retry only after connectivity is restored; do not switch environments automatically.",
        ),
        2013: (
            "CONNECTION_LOST",
            "The connection was lost before the complete result was received.",
            "Check database and network health, then retry the same page with bounded backoff. An incomplete response is not a successful result; do not switch environments automatically.",
        ),
        1317: (
            "QUERY_INTERRUPTED",
            "The database interrupted the query.",
            "Confirm why the query was cancelled before running it again; do not automatically repeat an interrupted query.",
        ),
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
        canonical_errno if isinstance(canonical_errno, int) else 0,
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
        retryable=errno in {1040, 1203, 1205, 1213, 2002, 2003, 2006, 2013, 2055},
        error_type=type(exc).__name__,
        phase=phase,
        errno=errno,
        sqlstate=getattr(exc, "sqlstate", None),
        **context,
    )
