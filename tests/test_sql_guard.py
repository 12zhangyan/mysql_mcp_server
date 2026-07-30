import pytest

from mysql_mcp_server.sql_guard import (
    ReadOnlyViolation,
    query_fingerprint,
    query_type,
    validate_database_access,
    validate_function_safety,
    validate_read_only_query,
)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM users",
        "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent",
        "SHOW TABLES",
        "DESCRIBE users",
        "EXPLAIN SELECT * FROM users",
        "SELECT 'delete from users' AS example",
        "SELECT 1 /* UPDATE users SET admin = 1 */",
        "SELECT ';' AS semicolon",
    ],
)
def test_allows_read_only_queries(query):
    assert validate_read_only_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO users(name) VALUES ('x')",
        "UPDATE users SET admin = 1",
        "DELETE FROM users",
        "DROP TABLE users",
        "WITH doomed AS (SELECT id FROM users) DELETE FROM users",
        "SELECT * FROM users FOR UPDATE",
        "SELECT * FROM users INTO OUTFILE '/tmp/users.csv'",
        "SELECT @value := 1",
        "SELECT GET_LOCK('mcp', 10)",
        "SELECT LOAD_FILE('/etc/passwd')",
        "SELECT SLEEP(10)",
        "SELECT BENCHMARK(1000, SHA1('x'))",
        "SELECT 1; DELETE FROM users",
        "/*!50000 DROP TABLE users */",
    ],
)
def test_blocks_non_read_only_queries(query):
    with pytest.raises(ReadOnlyViolation):
        validate_read_only_query(query)


def test_allows_one_trailing_semicolon():
    assert validate_read_only_query("SELECT 1;") == "SELECT 1"


def test_database_allowlist_uses_ast_not_alias_regex():
    accessed = validate_database_access(
        "SELECT u.id FROM app.users AS u",
        selected_database="app",
        allowed_databases=("app",),
        allow_system_databases=False,
    )
    assert accessed == {"app"}


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM secret.users",
        "SELECT secret.side_effect()",
    ],
)
def test_database_allowlist_blocks_qualified_access(query):
    with pytest.raises(ReadOnlyViolation, match="outside allowed_databases"):
        validate_database_access(
            query,
            selected_database="app",
            allowed_databases=("app",),
            allow_system_databases=False,
        )


def test_system_database_access_is_blocked_for_user_queries():
    with pytest.raises(ReadOnlyViolation, match="System database"):
        validate_database_access(
            "SELECT * FROM mysql.user",
            selected_database=None,
            allowed_databases=(),
            allow_system_databases=False,
        )


def test_internal_metadata_query_can_use_information_schema():
    accessed = validate_database_access(
        "SELECT TABLE_NAME FROM information_schema.TABLES",
        selected_database="app",
        allowed_databases=("app",),
        allow_system_databases=False,
        internal=True,
    )
    assert accessed == {"app", "information_schema"}


def test_query_fingerprint_omits_literal_differences():
    assert query_fingerprint("SELECT * FROM users WHERE id = 1") == query_fingerprint(
        "SELECT * FROM users WHERE id = 999"
    )


def test_malformed_sql_still_has_safe_audit_identity():
    fingerprint = query_fingerprint("SELECT 'unterminated")

    assert len(fingerprint) == 16
    assert query_type("ALTER TABLE users ADD COLUMN x INT") == "ALTER"


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM users FOR SHARE",
        "SELECT NEXT VALUE FOR order_sequence",
        "SELECT NEXTVAL(order_sequence)",
        "SELECT LAST_INSERT_ID(5)",
        "SELECT RELEASE_ALL_LOCKS()",
    ],
)
def test_side_effecting_select_constructs_are_blocked(query):
    with pytest.raises(ReadOnlyViolation):
        validate_read_only_query(query)


def test_unrecognized_udf_or_stored_function_is_fail_closed():
    with pytest.raises(ReadOnlyViolation, match="CUSTOM_FUNC"):
        validate_function_safety("SELECT custom_func(id) FROM users")
    with pytest.raises(ReadOnlyViolation, match="CUSTOM_FUNC"):
        validate_function_safety("SELECT app.custom_func(id) FROM users")


def test_safe_or_explicitly_reviewed_functions_are_allowed():
    assert validate_function_safety("SELECT NOW(), COUNT(*) FROM users") == {"NOW"}
    assert validate_function_safety(
        "SELECT reviewed_metric(id) FROM users",
        allowed_functions=("REVIEWED_METRIC",),
    ) == {"REVIEWED_METRIC"}
