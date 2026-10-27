import pytest

from mysql_mcp_server.sql_guard import (
    BLOCKED_FUNCTIONS,
    BUILTIN_PURE_FUNCTIONS,
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
        "SHOW CREATE TABLE users",
        "SHOW CREATE VIEW active_users",
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
        "SELECT 1 /*M! INTO OUTFILE '/tmp/leak.txt' */",
        "SELECT 2 /*M! + SLEEP(5) */",
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


@pytest.mark.parametrize(
    "query",
    [
        "SHOW DATABASES",
        "SHOW TABLES FROM mysql",
        "SHOW COLUMNS FROM users FROM secret",
        "SHOW INDEX FROM secret.users",
        "SHOW TRIGGERS FROM secret",
        "SHOW PROCESSLIST",
    ],
)
def test_show_statements_cannot_bypass_database_scope(query):
    with pytest.raises(ReadOnlyViolation):
        validate_database_access(
            query,
            selected_database="app",
            allowed_databases=("app",),
            allow_system_databases=False,
        )


def test_schema_scoped_show_and_internal_grants_remain_available():
    assert validate_database_access(
        "SHOW TABLES FROM app",
        selected_database="app",
        allowed_databases=("app",),
        allow_system_databases=False,
    ) == {"app"}


def test_information_schema_error_points_to_controlled_metadata_tools():
    with pytest.raises(ReadOnlyViolation, match="inspect_catalog"):
        validate_database_access(
            "SELECT * FROM information_schema.COLUMNS",
            selected_database="app",
            allowed_databases=("app",),
            allow_system_databases=False,
        )
    assert validate_database_access(
        "SHOW GRANTS",
        selected_database="app",
        allowed_databases=("app",),
        allow_system_databases=False,
        internal=True,
    ) == {"app"}


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


@pytest.mark.parametrize(
    ("query", "construct"),
    [
        ("SELECT * FROM users FOR UPDATE", "FOR UPDATE"),
        ("SELECT * FROM users FOR SHARE", "FOR SHARE"),
        (
            "SELECT * FROM users LOCK IN SHARE MODE",
            "LOCK IN SHARE MODE",
        ),
    ],
)
def test_locking_reads_have_policy_specific_errors(query, construct):
    with pytest.raises(
        ReadOnlyViolation,
        match=rf"Locking read '{construct}' is not allowed",
    ):
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


@pytest.mark.parametrize(
    ("query", "name"),
    [
        ("SELECT sleep(1)", "SLEEP"),
        ("SELECT id FROM users WHERE Benchmark (1, 1)", "BENCHMARK"),
        ("SELECT 1, get_lock('a', 1), sleep(1)", "GET_LOCK"),
    ],
)
def test_blocked_function_reports_first_match_case_insensitively(query, name):
    with pytest.raises(ReadOnlyViolation, match=rf"Function '{name}' is not allowed"):
        validate_read_only_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT my_sleep_count FROM stats",
        "SELECT sleep FROM schedule",
        "SELECT 'SLEEP(1)' AS text",
    ],
)
def test_blocked_function_names_require_a_real_call(query):
    assert validate_read_only_query(query) == query


@pytest.mark.parametrize(
    "query",
    [
        "SELECT REPLACE(name, 'a', 'b') FROM users",
        "SELECT replace (name, 'a', 'b') FROM users",
        "SELECT TRUNCATE(price, 2) FROM orders",
        "SELECT INSERT(code, 1, 2, 'XX') FROM orders",
        "SELECT CAST(name AS CHAR CHARACTER SET utf8mb4) FROM users",
        "SELECT CONVERT(name, CHAR(10) CHAR SET utf8mb4) FROM users",
        "SELECT id FROM users USE INDEX (idx_name) WHERE name = 'x'",
        "SELECT id FROM users u USE KEY FOR ORDER BY (idx_name) ORDER BY name",
        # Non-reserved statement words are ordinary identifiers in MySQL.
        "SELECT start, begin, commit, reset, handler, do FROM schedule",
        "SELECT s.start FROM schedule s ORDER BY start",
        "DESCRIBE start",
    ],
)
def test_read_only_uses_of_statement_keywords_are_allowed(query):
    assert validate_read_only_query(query) == query


@pytest.mark.parametrize(
    "query",
    [
        "START TRANSACTION",
        "TRUNCATE TABLE users",
        "REPLACE INTO users (id) VALUES (1)",
        "INSERT INTO users (id) VALUES (1)",
        "EXPLAIN REPLACE INTO users (id) VALUES (1)",
        "EXPLAIN INSERT INTO users (id) VALUES (1)",
        "WITH x AS (SELECT 1) UPDATE users SET name = REPLACE(name, 'a', 'b')",
        "WITH x AS (SELECT 1) DELETE FROM users",
        "SELECT 1 FROM users UNION SELECT 1 FROM t FOR UPDATE",
        "SELECT * FROM users LOCK IN SHARE MODE",
        "SELECT REPLACE INTO",
        "SELECT name FROM users SET",
        "SELECT USE FROM users",
        "SELECT 1; START TRANSACTION",
        "SELECT 1; TRUNCATE users",
    ],
)
def test_write_uses_of_statement_keywords_remain_blocked(query):
    with pytest.raises(ReadOnlyViolation):
        validate_read_only_query(query)


@pytest.mark.parametrize(
    "query",
    ["SHOW OPEN TABLES", "SHOW OPEN TABLES WHERE `Database` = 'secret'"],
)
def test_show_open_tables_requires_a_database(query):
    with pytest.raises(ReadOnlyViolation, match="SHOW OPEN TABLES FROM"):
        validate_database_access(
            query,
            selected_database="app",
            allowed_databases=("app",),
            allow_system_databases=False,
        )


def test_show_open_tables_from_allowed_database_is_scoped():
    for query in ("SHOW OPEN TABLES FROM app", "SHOW OPEN TABLES FROM app LIKE 'o%'"):
        assert validate_database_access(
            query,
            selected_database="app",
            allowed_databases=("app",),
            allow_system_databases=False,
        ) == {"app"}
    with pytest.raises(ReadOnlyViolation, match="outside allowed_databases"):
        validate_database_access(
            "SHOW OPEN TABLES FROM secret",
            selected_database="app",
            allowed_databases=("app",),
            allow_system_databases=False,
        )


@pytest.mark.parametrize(
    ("query", "name"),
    [
        ("SELECT app.now()", "NOW"),
        ("SELECT app.uuid() FROM users", "UUID"),
        ("SELECT `app`.`current_timestamp`()", "CURRENT_TIMESTAMP"),
        ("SELECT id FROM users WHERE id = app.connection_id()", "CONNECTION_ID"),
    ],
)
def test_schema_qualified_calls_do_not_inherit_built_in_safety(query, name):
    # app.now() runs a stored function named now, not the built-in NOW().
    with pytest.raises(ReadOnlyViolation, match=name):
        validate_function_safety(query)


def test_schema_qualified_calls_need_an_explicitly_reviewed_function():
    assert validate_function_safety(
        "SELECT app.reviewed_metric(id), NOW() FROM users",
        allowed_functions=("REVIEWED_METRIC",),
    ) == {"REVIEWED_METRIC", "NOW"}
    assert validate_function_safety(
        "SELECT app.now()", allowed_functions=("NOW",)
    ) == {"NOW"}


def test_checksum_over_built_in_pure_functions_needs_no_allowlist():
    query = "SELECT BIT_XOR(CRC32(CONCAT_WS('|', a, b))), SUM(CRC32(a)) FROM t"

    # Older sqlglot releases also report BIT_XOR as an unmodeled call.
    assert {"CRC32"} <= validate_function_safety(validate_read_only_query(query))
    assert validate_function_safety(
        "SELECT CONV(a, 10, 16), ORD(a), FIND_IN_SET(a, b) FROM t"
    ) == {"CONV", "ORD", "FIND_IN_SET"}


def test_built_in_names_do_not_make_unknown_or_qualified_calls_safe():
    with pytest.raises(ReadOnlyViolation, match="CUSTOM_CRC"):
        validate_function_safety("SELECT crc32(a), custom_crc(a) FROM t")
    with pytest.raises(ReadOnlyViolation, match="CRC32"):
        validate_function_safety("SELECT app.crc32(a) FROM t")
    assert validate_function_safety(
        "SELECT app.crc32(a) FROM t", allowed_functions=("CRC32",)
    ) == {"CRC32"}


def test_replace_function_is_allowed_but_replace_statements_are_not():
    assert validate_read_only_query("SELECT REPLACE(a, 'x', 'y') FROM t")
    for query in (
        "REPLACE INTO t VALUES (1)",
        "EXPLAIN REPLACE INTO t VALUES (1)",
        "WITH x AS (SELECT 1) REPLACE INTO t SELECT * FROM x",
    ):
        with pytest.raises(ReadOnlyViolation):
            validate_read_only_query(query)


# Calls whose arity is not the default single argument.
_SAMPLE_ARGUMENTS = {
    "ATAN2": "a, b",
    "ELT": "a, b",
    "PI": "",
    "REGEXP_INSTR": "a, b",
    "REGEXP_SUBSTR": "a, b",
    "REPLACE": "a, b, c",
    "SUBSTRING_INDEX": "a, b, 1",
    "VERSION": "",
}


@pytest.mark.parametrize("name", sorted(BUILTIN_PURE_FUNCTIONS))
def test_built_in_pure_functions_pass_without_an_allowlist(name):
    # Whether sqlglot models a name depends on its release; the call passes
    # either way, and no listed name may be a blocked function.
    assert name not in BLOCKED_FUNCTIONS
    arguments = _SAMPLE_ARGUMENTS.get(name, "a")
    query = validate_read_only_query(f"SELECT {name}({arguments}) FROM t")
    validate_function_safety(query)


@pytest.mark.parametrize(
    "name",
    ["SLEEP", "BENCHMARK", "GET_LOCK", "LOAD_FILE", "RELEASE_LOCK", "SYS_EXEC"],
)
def test_side_effect_functions_stay_blocked(name):
    assert name not in BUILTIN_PURE_FUNCTIONS
    with pytest.raises(ReadOnlyViolation, match=name):
        validate_read_only_query(f"SELECT {name}(1)")


def test_unrecognized_function_error_names_the_config_keys():
    with pytest.raises(ReadOnlyViolation) as exc:
        validate_function_safety("SELECT custom_func(secret_literal) FROM t")

    message = str(exc.value)
    assert "CUSTOM_FUNC" in message
    assert "allowed_functions" in message
    assert "MYSQL_ALLOWED_FUNCTIONS" in message
