"""Fail-closed validation for SQL exposed by the read-only MCP tool."""

from __future__ import annotations

import hashlib
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

ALLOWED_FIRST_KEYWORDS = {
    "SELECT",
    "SHOW",
    "DESCRIBE",
    "DESC",
    "EXPLAIN",
    "WITH",
    "TABLE",
}

BLOCKED_KEYWORDS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "REPLACE",
    "CREATE",
    "ALTER",
    "DROP",
    "TRUNCATE",
    "RENAME",
    "GRANT",
    "REVOKE",
    "SET",
    "USE",
    "CALL",
    "DO",
    "LOAD",
    "HANDLER",
    "LOCK",
    "UNLOCK",
    "START",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE",
    "ANALYZE",
    "OPTIMIZE",
    "REPAIR",
    "INSTALL",
    "UNINSTALL",
    "KILL",
    "RESET",
    "PURGE",
}

BLOCKED_FUNCTIONS = {
    "GET_LOCK",
    "RELEASE_LOCK",
    "RELEASE_ALL_LOCKS",
    "LOAD_FILE",
    "SLEEP",
    "BENCHMARK",
    "SYS_EXEC",
    "SYS_EVAL",
    "LAST_INSERT_ID",
    "MASTER_POS_WAIT",
    "SOURCE_POS_WAIT",
    "WAIT_FOR_EXECUTED_GTID_SET",
    "LOCKING_SERVICE_GET_LOCKS",
    "LOCKING_SERVICE_RELEASE_LOCKS",
    "NEXTVAL",
    "SETVAL",
}

SYSTEM_DATABASES = {"information_schema", "mysql", "performance_schema", "sys"}

# sqlglot represents well-known functions with dedicated AST classes. Only
# unresolved/Anonymous calls need an explicit allowlist because they may be a
# stored function or UDF with effects outside transaction read-only controls.
SAFE_ANONYMOUS_FUNCTIONS = {
    "CURDATE",
    "CURRENT_DATE",
    "CURRENT_TIME",
    "CURRENT_TIMESTAMP",
    "CURTIME",
    "LOCALTIME",
    "LOCALTIMESTAMP",
    "NOW",
    "SYSDATE",
    "UTC_DATE",
    "UTC_TIME",
    "UTC_TIMESTAMP",
    "CONNECTION_ID",
    "SESSION_USER",
    "SYSTEM_USER",
    "USER",
    "UUID",
    "UUID_TO_BIN",
    "BIN_TO_UUID",
    "IS_UUID",
}


class ReadOnlyViolation(ValueError):
    """Raised when a statement is not provably read-only."""


def _sanitize_sql(sql: str) -> tuple[str, list[int]]:
    """
    Replace literals, quoted identifiers and comments with spaces.

    The returned semicolon positions only include semicolons outside those regions.
    MySQL executable comments are rejected because they can conceal SQL.
    """
    output = list(sql)
    semicolons: list[int] = []
    index = 0
    length = len(sql)

    def blank(start: int, end: int) -> None:
        for position in range(start, end):
            if output[position] not in "\r\n":
                output[position] = " "

    while index < length:
        char = sql[index]

        if char in {"'", '"', "`"}:
            quote = char
            start = index
            index += 1
            while index < length:
                if sql[index] == "\\" and quote != "`":
                    index += 2
                    continue
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    blank(start, index)
                    break
                index += 1
            else:
                raise ReadOnlyViolation("Unterminated quoted value or identifier")
            continue

        if sql.startswith("/*", index):
            if sql.startswith("/*!", index):
                raise ReadOnlyViolation("MySQL executable comments are not allowed")
            start = index
            end = sql.find("*/", index + 2)
            if end < 0:
                raise ReadOnlyViolation("Unterminated block comment")
            index = end + 2
            blank(start, index)
            continue

        if char == "#":
            start = index
            end = sql.find("\n", index + 1)
            index = length if end < 0 else end
            blank(start, index)
            continue

        if sql.startswith("--", index) and (
            index + 2 == length or sql[index + 2].isspace()
        ):
            start = index
            end = sql.find("\n", index + 2)
            index = length if end < 0 else end
            blank(start, index)
            continue

        if char == ";":
            semicolons.append(index)
        index += 1

    return "".join(output), semicolons


def validate_read_only_query(sql: str) -> str:
    """Return a normalized single statement, or reject it before connecting."""
    if not isinstance(sql, str) or not sql.strip():
        raise ReadOnlyViolation("Query is required")
    if len(sql) > 100_000:
        raise ReadOnlyViolation("Query exceeds the 100000 character safety limit")

    sanitized, semicolons = _sanitize_sql(sql)
    sanitized_trimmed = sanitized.rstrip()
    if semicolons:
        if len(semicolons) != 1 or semicolons[0] != len(sanitized_trimmed) - 1:
            raise ReadOnlyViolation("Only one SQL statement is allowed")
        sanitized_trimmed = sanitized_trimmed[:-1].rstrip()

    normalized_query = sql.strip()
    if normalized_query.endswith(";"):
        normalized_query = normalized_query[:-1].rstrip()

    if ":=" in sanitized_trimmed:
        raise ReadOnlyViolation("Session variable assignment is not allowed")

    tokens = [
        token.upper()
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", sanitized_trimmed)
    ]
    if not tokens:
        raise ReadOnlyViolation("Query does not contain a SQL statement")
    if tokens[0] not in ALLOWED_FIRST_KEYWORDS:
        raise ReadOnlyViolation(
            f"Only read-only SQL is allowed; '{tokens[0]}' statements are blocked"
        )

    blocked = next((token for token in tokens if token in BLOCKED_KEYWORDS), None)
    if blocked:
        raise ReadOnlyViolation(
            f"Only read-only SQL is allowed; keyword '{blocked}' is blocked"
        )
    if "INTO" in tokens:
        raise ReadOnlyViolation("SELECT ... INTO is not allowed")

    for function_name in BLOCKED_FUNCTIONS:
        if re.search(rf"\b{function_name}\s*\(", sanitized_trimmed, re.IGNORECASE):
            raise ReadOnlyViolation(f"Function '{function_name}' is not allowed")

    if re.search(r"\bFOR\s+UPDATE\b", sanitized_trimmed, re.IGNORECASE):
        raise ReadOnlyViolation("Locking reads are not allowed")
    if re.search(r"\bLOCK\s+IN\s+SHARE\s+MODE\b", sanitized_trimmed, re.IGNORECASE):
        raise ReadOnlyViolation("Locking reads are not allowed")
    if re.search(r"\bFOR\s+SHARE\b", sanitized_trimmed, re.IGNORECASE):
        raise ReadOnlyViolation("Locking reads are not allowed")
    if re.search(r"\bNEXT\s+VALUE\s+FOR\b", sanitized_trimmed, re.IGNORECASE):
        raise ReadOnlyViolation("Sequence advancement is not allowed")

    return normalized_query


def query_fingerprint(sql: str) -> str:
    """Create a stable audit identifier without logging SQL or literal values."""
    try:
        sanitized, _ = _sanitize_sql(sql)
        sanitized = re.sub(
            r"(?<![A-Za-z0-9_$])(?:0x[0-9A-Fa-f]+|\d+(?:\.\d+)?)(?![A-Za-z0-9_$])",
            "?",
            sanitized,
        )
        normalized = " ".join(sanitized.upper().split())
    except ReadOnlyViolation:
        # Malformed SQL must still receive a non-reversible audit identifier.
        normalized = " ".join(sql.upper().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def query_type(sql: str) -> str:
    """Best-effort statement classification for allowed and denied audit events."""
    try:
        sanitized, _ = _sanitize_sql(sql)
    except ReadOnlyViolation:
        sanitized = sql
    match = re.search(r"\b[A-Za-z]+\b", sanitized)
    return match.group(0).upper() if match else "UNKNOWN"


def validate_database_access(
    sql: str,
    *,
    selected_database: str | None,
    allowed_databases: tuple[str, ...],
    allow_system_databases: bool,
    internal: bool = False,
) -> set[str]:
    """
    Parse table references with the MySQL dialect and enforce schema policy.

    Unqualified tables use selected_database. CTE aliases have no database and
    therefore do not create false positives like regex-based `alias.column` checks.
    """
    try:
        statement = sqlglot.parse_one(sql, read="mysql")
    except ParseError as exc:
        raise ReadOnlyViolation(
            f"Query could not be parsed safely as MySQL SQL: {exc}"
        ) from exc

    accessed: set[str] = set()
    if selected_database:
        accessed.add(selected_database)
    for table in statement.find_all(exp.Table):
        database = table.db
        if database:
            accessed.add(database)
    for dotted in statement.find_all(exp.Dot):
        if isinstance(dotted.expression, exp.Func) and isinstance(
            dotted.this, exp.Identifier
        ):
            accessed.add(dotted.this.name)

    if not internal and not allow_system_databases:
        blocked_system = sorted(accessed & SYSTEM_DATABASES)
        if blocked_system:
            raise ReadOnlyViolation(
                "System database access is blocked: " + ", ".join(blocked_system)
            )

    if allowed_databases:
        disallowed = sorted(
            database
            for database in accessed
            if database not in allowed_databases
            and not (internal and database in SYSTEM_DATABASES)
        )
        if disallowed:
            raise ReadOnlyViolation(
                "Query references databases outside allowed_databases: "
                + ", ".join(disallowed)
            )
    return accessed


def validate_function_safety(
    sql: str, *, allowed_functions: tuple[str, ...] = ()
) -> set[str]:
    """
    Reject unresolved functions unless they are known-safe or explicitly allowed.

    MySQL stored functions and native UDFs use SELECT syntax but may perform
    database or external side effects. This check keeps a write-capable account
    outside the read-only correctness boundary.
    """
    try:
        statement = sqlglot.parse_one(sql, read="mysql")
    except ParseError as exc:
        raise ReadOnlyViolation(
            f"Query could not be parsed safely as MySQL SQL: {exc}"
        ) from exc

    configured = {name.upper() for name in allowed_functions}
    anonymous = {
        function.name.upper()
        for function in statement.find_all(exp.Anonymous)
        if function.name
    }
    unsafe = sorted(anonymous - SAFE_ANONYMOUS_FUNCTIONS - configured)
    if unsafe:
        raise ReadOnlyViolation(
            "Stored, UDF, or unrecognized functions are blocked by default: "
            + ", ".join(unsafe)
            + ". Add only reviewed deterministic functions to allowed_functions."
        )
    return anonymous
