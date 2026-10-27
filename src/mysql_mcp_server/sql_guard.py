"""Fail-closed validation for SQL exposed by the read-only MCP tool."""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from typing import Any, cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from .errors import QueryFailure

ALLOWED_FIRST_KEYWORDS = {
    "SELECT",
    "SHOW",
    "DESCRIBE",
    "DESC",
    "EXPLAIN",
    "WITH",
    "TABLE",
}

# Reserved MySQL words: they cannot be unquoted identifiers, so any other
# occurrence is rejected except the read-only forms in _is_read_only_keyword_use.
# INSERT, UPDATE, DELETE and REPLACE can follow WITH or EXPLAIN.
RESERVED_BLOCKED_KEYWORDS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "REPLACE",
    "CREATE",
    "ALTER",
    "DROP",
    "RENAME",
    "GRANT",
    "REVOKE",
    "SET",
    "USE",
    "CALL",
    "LOAD",
    "LOCK",
    "UNLOCK",
    "RELEASE",
    "ANALYZE",
    "OPTIMIZE",
    "KILL",
    "PURGE",
}

# Non-reserved words that only begin statements; elsewhere MySQL reads them as
# identifiers (a `start` column). One statement cannot contain another, so they
# are blocked as the first keyword, which ALLOWED_FIRST_KEYWORDS already does.
STATEMENT_ONLY_KEYWORDS = {
    "TRUNCATE",
    "DO",
    "HANDLER",
    "START",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "REPAIR",
    "INSTALL",
    "UNINSTALL",
    "RESET",
}

BLOCKED_KEYWORDS = RESERVED_BLOCKED_KEYWORDS | STATEMENT_ONLY_KEYWORDS

# String functions spelled like statements; no statement puts `(` after them.
_KEYWORD_FUNCTIONS = {"INSERT", "REPLACE"}

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

_BLOCKED_FUNCTION_PATTERN = re.compile(
    r"\b(" + "|".join(sorted(BLOCKED_FUNCTIONS)) + r")\s*\(", re.IGNORECASE
)

SYSTEM_DATABASES = {"information_schema", "mysql", "performance_schema", "sys"}

# SHOW statements are parsed separately from ordinary table expressions by
# sqlglot. Only schema-scoped metadata forms are safe for user-supplied SQL;
# server-wide forms (DATABASES, PROCESSLIST, GRANTS, BINARY LOGS, ...) can leak
# information outside a profile's database allowlist.
SCOPED_SHOW_KINDS = {
    "COLUMNS",
    "CREATE TABLE",
    "CREATE VIEW",
    "EVENTS",
    "INDEX",
    "INDEXES",
    "KEYS",
    "OPEN TABLES",
    "TABLE STATUS",
    "TABLES",
    "TRIGGERS",
}

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


def _is_read_only_keyword_use(
    sanitized: str, tokens: list[re.Match[str]], index: int
) -> bool:
    """Recognize the read-only constructs that spell a reserved blocked word."""
    word = tokens[index].group(0).upper()
    following = sanitized[tokens[index].end() :]
    previous = tokens[index - 1].group(0).upper() if index else ""
    following_word = (
        tokens[index + 1].group(0).upper() if index + 1 < len(tokens) else ""
    )
    if word in _KEYWORD_FUNCTIONS:
        return re.match(r"\s*\(", following) is not None
    if word == "SET":
        # CHARACTER SET / CHAR SET name a character set in CAST, CONVERT, ...
        return previous in {"CHAR", "CHARACTER"}
    if word == "USE":
        # Index hint: USE INDEX (...) / USE KEY (...).
        return following_word in {"INDEX", "KEY"}
    return False


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
            comment_prefix = sql[index : index + 4].upper()
            if sql.startswith("/*!", index) or comment_prefix == "/*M!":
                raise ReadOnlyViolation(
                    "MySQL and MariaDB executable comments are not allowed"
                )
            start = index
            end = sql.find("*/", index + 2)
            if end < 0:
                raise ReadOnlyViolation("Unterminated block comment")
            if sql.startswith("/*+", start) and re.search(
                r"\b(?:SET_VAR|MAX_EXECUTION_TIME|RESOURCE_GROUP)\s*\(",
                sql[start + 3 : end],
                re.IGNORECASE,
            ):
                raise ReadOnlyViolation(
                    "Optimizer hints that change session variables, execution "
                    "timeouts or resource groups are not allowed; use timeout_ms "
                    "within the connection profile limit"
                )
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

    token_matches = list(re.finditer(r"[A-Za-z_][A-Za-z0-9_$]*", sanitized_trimmed))
    tokens = [match.group(0).upper() for match in token_matches]
    if not tokens:
        raise ReadOnlyViolation("Query does not contain a SQL statement")
    if tokens[0] not in ALLOWED_FIRST_KEYWORDS:
        raise ReadOnlyViolation(
            f"Only read-only SQL is allowed; '{tokens[0]}' statements are blocked"
        )

    if re.search(r"\bFOR\s+UPDATE\b", sanitized_trimmed, re.IGNORECASE):
        raise ReadOnlyViolation(
            "Locking read 'FOR UPDATE' is not allowed by the read-only policy"
        )
    if re.search(r"\bLOCK\s+IN\s+SHARE\s+MODE\b", sanitized_trimmed, re.IGNORECASE):
        raise ReadOnlyViolation(
            "Locking read 'LOCK IN SHARE MODE' is not allowed by the read-only policy"
        )
    if re.search(r"\bFOR\s+SHARE\b", sanitized_trimmed, re.IGNORECASE):
        raise ReadOnlyViolation(
            "Locking read 'FOR SHARE' is not allowed by the read-only policy"
        )
    if re.search(r"\bNEXT\s+VALUE\s+FOR\b", sanitized_trimmed, re.IGNORECASE):
        raise ReadOnlyViolation("Sequence advancement is not allowed")
    if "INTO" in tokens:
        raise ReadOnlyViolation("SELECT ... INTO is not allowed")

    show_create = bool(
        re.match(r"^\s*SHOW\s+CREATE\s+(?:TABLE|VIEW)\b", sanitized_trimmed, re.I)
    )
    blocked = next(
        (
            token
            for index, token in enumerate(tokens)
            if token in RESERVED_BLOCKED_KEYWORDS
            and not (show_create and token == "CREATE")
            and not _is_read_only_keyword_use(sanitized_trimmed, token_matches, index)
        ),
        None,
    )
    if blocked:
        raise ReadOnlyViolation(
            f"Only read-only SQL is allowed; keyword '{blocked}' is blocked"
        )

    blocked_function = _BLOCKED_FUNCTION_PATTERN.search(sanitized_trimmed)
    if blocked_function:
        raise ReadOnlyViolation(
            f"Function '{blocked_function.group(1).upper()}' is not allowed"
        )

    return normalized_query


def parse_mysql_query(sql: str) -> exp.Expression:
    """Expose parser coordinates, never its SQL-bearing diagnostic text."""
    try:
        return cast(exp.Expression, sqlglot.parse_one(sql, read="mysql"))
    except (ParseError, TokenError) as exc:
        position: dict[str, Any] = {}
        if isinstance(exc, ParseError) and exc.errors:
            for field, name in (("line", "line"), ("col", "column")):
                value = exc.errors[0].get(field)
                if type(value) is int and value > 0:
                    position[name] = value
        raise QueryFailure(
            "SQL_PARSE_ERROR",
            "The query could not be parsed safely as MySQL SQL.",
            "Check MySQL syntax at the reported position before correcting the query; "
            "do not repeat unchanged SQL. No database connection was opened.",
            phase="validation",
            error_type=type(exc).__name__,
            query_id=query_fingerprint(sql),
            **position,
        ) from None


def build_explain_query(sql: str, *, json_plan: bool = False) -> str:
    """Plan a SELECT/CTE/UNION without accepting EXPLAIN options from callers."""
    query = validate_read_only_query(sql)
    if query_type(query) not in {"SELECT", "WITH"}:
        raise ReadOnlyViolation(
            "explain_sql requires a SELECT or WITH query without an EXPLAIN prefix"
        )
    statement = parse_mysql_query(query)
    if not isinstance(statement, exp.Query):
        raise ReadOnlyViolation("explain_sql requires a SELECT or WITH query")
    # Keep the original query, including its LIMIT/OFFSET and optimizer hints.
    # execute_query validates database/function policy on this complete statement.
    return ("EXPLAIN FORMAT=JSON " if json_plan else "EXPLAIN ") + query


# Every audit event of one request re-derives these from the same SQL text;
# a small cache avoids re-sanitizing it per event.
@lru_cache(maxsize=32)
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


@lru_cache(maxsize=32)
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
    statement: exp.Expression | None = None,
) -> set[str]:
    """
    Parse table references with the MySQL dialect and enforce schema policy.

    Unqualified tables use selected_database. CTE aliases have no database and
    therefore do not create false positives like regex-based `alias.column` checks.
    Pass ``statement`` from parse_mysql_query(sql) to reuse one parse.
    """
    if statement is None:
        statement = parse_mysql_query(sql)

    accessed: set[str] = set()
    if selected_database:
        accessed.add(selected_database)

    if isinstance(statement, exp.Show):
        show_kind = str(statement.args.get("this") or "").upper()
        show_database = statement.args.get("db")
        if isinstance(show_database, exp.Identifier):
            accessed.add(show_database.name)
        elif show_database is not None:
            raise ReadOnlyViolation("SHOW database scope could not be validated safely")

        if not internal and show_kind not in SCOPED_SHOW_KINDS:
            raise ReadOnlyViolation(
                f"Server-wide SHOW {show_kind or 'statement'} is blocked; "
                "use the dedicated metadata tools"
            )
        # Unlike SHOW TABLES, OPEN TABLES without FROM lists every database.
        if not internal and show_kind == "OPEN TABLES" and show_database is None:
            raise ReadOnlyViolation(
                "SHOW OPEN TABLES must name an allowed database: "
                "SHOW OPEN TABLES FROM <database>"
            )
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
            guidance = (
                "; use inspect_catalog, get_schema_info, or list_tables for "
                "controlled metadata access"
                if "information_schema" in blocked_system
                else ""
            )
            raise ReadOnlyViolation(
                "System database access is blocked: "
                + ", ".join(blocked_system)
                + guidance
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
    sql: str,
    *,
    allowed_functions: tuple[str, ...] = (),
    statement: exp.Expression | None = None,
) -> set[str]:
    """
    Reject unresolved functions unless they are known-safe or explicitly allowed.

    MySQL stored functions and native UDFs use SELECT syntax but may perform
    database or external side effects. This check keeps a write-capable account
    outside the read-only correctness boundary.
    """
    if statement is None:
        statement = parse_mysql_query(sql)

    configured = {name.upper() for name in allowed_functions}
    # sqlglot also models optimizer hints such as NO_MERGE as Anonymous nodes;
    # they are not callable functions. Policy-changing hints are rejected earlier.
    calls = [
        function
        for function in statement.find_all(exp.Anonymous)
        if function.name and function.find_ancestor(exp.Hint) is None
    ]
    anonymous = {function.name.upper() for function in calls}
    # A schema-qualified call (app.now()) always runs a stored function, even
    # when its name matches a built-in, so only configured names may be called.
    qualified = {
        function.name.upper()
        for function in calls
        if isinstance(function.parent, exp.Dot)
    }
    unsafe = sorted(
        (anonymous - qualified - SAFE_ANONYMOUS_FUNCTIONS - configured)
        | (qualified - configured)
    )
    if unsafe:
        raise ReadOnlyViolation(
            "Stored, UDF, or unrecognized functions are blocked by default: "
            + ", ".join(unsafe)
            + ". Add only reviewed deterministic functions to allowed_functions."
        )
    return anonymous
