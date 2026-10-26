import base64
import json
import time
from datetime import datetime
from decimal import Decimal

import pytest

from mysql_mcp_server.config import DEFAULT_MASK_COLUMNS
from mysql_mcp_server.results import (
    MASKED_VALUE,
    QueryResult,
    mask_result_rows,
    serialize_value,
)

# An explicit policy for masking tests, independent of the unmasked defaults.
CONFIGURED_MASK_COLUMNS = (
    "password",
    "*pwd",
    "mobile",
    "*secret",
    "appsec",
    "token",
    "token_value",
)


def test_default_policy_preserves_fields_aliases_and_nested_json():
    columns = ["password", "access_token", "email", "harmless", "config"]
    values = [
        "synthetic-password",
        "synthetic-token",
        "user@example.test",
        "synthetic-secret",
        '{"password":"synthetic-password","nested":[{"token":"synthetic-token"}]}',
    ]
    rows, masked = mask_result_rows(
        "SELECT password, access_token, email, clientSecret AS harmless, config FROM users",
        columns,
        [values],
        DEFAULT_MASK_COLUMNS,
    )
    assert DEFAULT_MASK_COLUMNS == ()
    assert rows == [values]
    assert masked == []


def test_json_result_preserves_types_and_paging_metadata():
    result = QueryResult(
        connection="test",
        database="app",
        columns=["id", "amount", "created_at", "payload"],
        rows=[
            [
                1,
                serialize_value(Decimal("12.30"), 100),
                serialize_value(datetime(2026, 7, 29, 12, 30), 100),
                serialize_value(b"abc", 100),
            ]
        ],
        offset=10,
        truncated=True,
        duration_ms=5,
        query_id="abc123",
    )

    payload = json.loads(result.render("json"))

    assert payload["rows"][0][1] == "12.30"
    assert payload["rows"][0][2] == "2026-07-29T12:30:00"
    assert payload["rows"][0][3] == "base64:" + base64.b64encode(b"abc").decode()
    assert payload["next_offset"] == 11
    assert payload["returned_rows"] == 1
    assert payload["row_count"] == 1


def test_csv_uses_real_escaping_and_explicit_null():
    result = QueryResult(
        connection="dev",
        database="app",
        columns=["text", "empty", "null"],
        rows=[["hello,\nworld", "", None]],
        offset=0,
        truncated=False,
        duration_ms=1,
        query_id="q",
    )

    rendered = result.render("csv")

    assert '"hello,\nworld"' in rendered
    assert ",,NULL" in rendered


def test_render_is_cached_per_instance_and_not_shared_by_replace():
    from dataclasses import replace

    result = QueryResult(
        connection="dev",
        database="app",
        columns=["id"],
        rows=[[1], [2]],
        offset=0,
        truncated=False,
        duration_ms=1,
        query_id="q",
        result_format="json",
    )
    fitted = result.fit_response("json", 4096)
    assert fitted is result
    assert result.render("json") is result.render("json")

    shorter = replace(result, rows=[[1]], truncated=True)
    assert shorter.result_format == "json"
    assert json.loads(shorter.render("json"))["rows"] == [[1]]
    assert json.loads(result.render("json"))["rows"] == [[1], [2]]


def test_large_cell_is_truncated_deterministically():
    assert serialize_value("abcdefgh", 4) == "abcd…[truncated]"


def test_fast_paths_match_the_general_serialization_rules():
    class Label(str):
        pass

    class Flag(int):
        pass

    assert serialize_value("abcd", 4) == "abcd"
    assert serialize_value("abcdefgh", None) == "abcdefgh"
    assert serialize_value(Label("abcdefgh"), 4) == "abcd…[truncated]"
    assert type(serialize_value(Label("ab"), 4)) is str
    assert serialize_value(True, 1) is True
    assert serialize_value(Flag(7), 1) == 7
    assert serialize_value(None, 1) is None


@pytest.mark.parametrize(
    "query",
    [
        "WITH source AS (SELECT password AS value FROM users) "
        "SELECT value AS safe FROM source",
        "WITH source AS (SELECT password AS value, status FROM users) "
        "SELECT value AS safe, status FROM source",
        "WITH source AS (SELECT password AS value, status FROM users), "
        "wrapped AS (SELECT value AS harmless, status FROM source) "
        "SELECT harmless AS safe, status FROM wrapped",
        "WITH source AS (SELECT password AS value, status FROM users) "
        "SELECT value AS safe, status FROM source "
        "UNION ALL SELECT value AS safe, status FROM source",
    ],
)
def test_sensitive_results_are_masked_across_aliases_and_ctes(query):
    rows, masked = mask_result_rows(
        query,
        ["safe", "status"],
        [["secret", "active"]],
        ("password",),
    )

    assert rows == [[MASKED_VALUE, "active"]]
    assert masked == ["safe"]


# Each star projects exactly as many outputs as there are projections, the
# shape that previously skipped lineage and matched output names only.
@pytest.mark.parametrize(
    ("query", "columns"),
    [
        ("SELECT * FROM (SELECT password AS p FROM users) t", ["p"]),
        ("WITH x AS (SELECT password AS p FROM users) SELECT * FROM x", ["p"]),
        (
            "WITH x AS (SELECT password AS p FROM users) "
            "SELECT x.*, status FROM x JOIN accounts a ON a.id = 1",
            ["p", "status"],
        ),
        ("SELECT * FROM (SELECT * FROM (SELECT password AS p FROM users) a) b", ["p"]),
        (
            "SELECT * FROM orders o "
            "JOIN (SELECT id, password AS p FROM users) u ON u.id = o.user_id",
            ["id", "p"],
        ),
    ],
)
def test_star_over_aliased_sensitive_columns_is_masked(query, columns):
    rows, masked = mask_result_rows(
        query, columns, [["value"] * len(columns)], ("password",)
    )

    assert "p" in masked
    assert rows[0][columns.index("p")] == MASKED_VALUE


@pytest.mark.parametrize(
    "query",
    [
        "WITH x AS (SELECT id, name FROM users) SELECT * FROM x",
        "WITH x AS (SELECT COUNT(password) AS id, name FROM users) SELECT * FROM x",
        # Filter subqueries never reach the result, aliased or not.
        "SELECT * FROM orders WHERE name IN (SELECT password AS q FROM users)",
        "SELECT * FROM orders WHERE name IN (SELECT UPPER(password) FROM users)",
        "SELECT o.* FROM orders o "
        "JOIN (SELECT id, password AS p FROM users) u ON u.id = o.user_id",
    ],
)
def test_star_over_safe_columns_stays_unmasked(query):
    rows, masked = mask_result_rows(query, ["id", "name"], [[1, "a"]], ("password",))

    assert masked == []
    assert rows == [[1, "a"]]


# Each case needs tracing and nested-name resolution together; dropping either
# one, or using nested names only when tracing fails, leaks at least one case.
@pytest.mark.parametrize(
    ("query", "columns", "sensitive"),
    [
        (
            "SELECT * FROM (SELECT name AS p FROM accounts) b "
            "JOIN (SELECT password AS p FROM users) a ON 1 = 1",
            ["p", "p"],
            1,
        ),
        ("SELECT * FROM (SELECT p AS q FROM (SELECT password AS p FROM users) a) b", ["q"], 0),
        ("WITH x(p) AS (SELECT password FROM users) SELECT * FROM x", ["p"], 0),
        ("SELECT * FROM (SELECT password FROM users) AS t(p)", ["p"], 0),
        (
            "SELECT * FROM (SELECT name AS p FROM people "
            "UNION ALL SELECT password FROM users) t",
            ["p"],
            0,
        ),
    ],
)
def test_star_combines_tracing_with_nested_projection_names(query, columns, sensitive):
    rows, _masked = mask_result_rows(
        query, columns, [["value"] * len(columns)], ("password",)
    )

    assert rows[0][sensitive] == MASKED_VALUE


# MySQL names an unaliased derived column after its expression text, re-spaced
# (CONCAT(password, '')), or in forms the parser does not reproduce exactly.
@pytest.mark.parametrize(
    ("query", "column"),
    [
        ("SELECT * FROM (SELECT CONCAT(password,'') FROM users) t", "CONCAT(password, '')"),
        ("WITH x AS (SELECT UPPER(password) FROM users) SELECT * FROM x", "UPPER(password)"),
        ("SELECT * FROM (SELECT IFNULL(password,'') FROM users) t", "ifnull(`password`,'')"),
        (
            "SELECT t.`CONCAT(password,'')` FROM (SELECT CONCAT(password,'') FROM users) t",
            "CONCAT(password,'')",
        ),
    ],
)
def test_unaliased_nested_expressions_are_masked(query, column):
    rows, masked = mask_result_rows(query, [column], [["value"]], ("password",))

    assert rows == [[MASKED_VALUE]]
    assert masked == [column]


@pytest.mark.parametrize(
    ("query", "columns", "pattern", "sensitive"),
    [
        ("SELECT * FROM users", ["id", "password"], "users.password", 1),
        ("SELECT * FROM `users` LIMIT 100", ["id", "password"], "users.password", 1),
        ("SELECT * FROM app.users", ["id", "password"], "app.users.password", 1),
        ("SELECT u.password FROM users u", ["password"], "users.password", 0),
        ("SELECT u.* FROM users u", ["id", "password"], "users.password", 1),
        (
            "SELECT p FROM (SELECT password AS p FROM app.users) t",
            ["p"],
            "app.users.password",
            0,
        ),
        ("SELECT p FROM (SELECT u.password AS p FROM users u) t", ["p"], "users.password", 0),
        (
            "SELECT o.id, u.phone FROM orders o JOIN users u ON u.id = o.user_id",
            ["id", "phone"],
            "users.phone",
            1,
        ),
    ],
)
def test_table_qualified_patterns_follow_aliases_and_stars(
    query, columns, pattern, sensitive
):
    rows, masked = mask_result_rows(
        query, columns, [["value"] * len(columns)], (pattern,)
    )

    assert rows[0][sensitive] == MASKED_VALUE
    assert masked == [columns[sensitive]]


@pytest.mark.parametrize(
    ("query", "column", "pattern"),
    [
        ("SELECT o.phone FROM orders o", "phone", "users.phone"),
        ("SELECT u.name FROM users u", "name", "users.password"),
    ],
)
def test_table_qualified_patterns_keep_their_table_scope(query, column, pattern):
    rows, masked = mask_result_rows(query, [column], [["value"]], (pattern,))

    assert masked == []
    assert rows == [["value"]]


def test_output_column_patterns_mask_only_matching_columns():
    rows, masked = mask_result_rows(
        "SELECT 1",
        ["id", "email_address"],
        [[1, "user@example.test"]],
        ("*email*",),
    )

    assert rows == [[1, MASKED_VALUE]]
    assert masked == ["email_address"]


def test_real_world_sensitive_names_mask_the_source_not_safe_siblings():
    rows, masked = mask_result_rows(
        "SELECT UserPWD AS harmless, Mobile, DisplayName FROM user",
        ["harmless", "Mobile", "DisplayName"],
        [["pwd", "13800000000", "Alice"]],
        CONFIGURED_MASK_COLUMNS,
    )

    assert rows == [[MASKED_VALUE, MASKED_VALUE, "Alice"]]
    assert masked == ["harmless", "Mobile"]


def test_sensitive_alias_does_not_mask_a_safe_source_column():
    rows, masked = mask_result_rows(
        "SELECT DisplayName AS secret_col FROM user",
        ["secret_col"],
        [["Alice"]],
        CONFIGURED_MASK_COLUMNS,
    )

    assert rows == [["Alice"]]
    assert masked == []


def test_json_secret_keys_are_masked_without_hiding_the_config_column():
    rows, masked = mask_result_rows(
        "SELECT AccountConfig FROM opentityintegratechannelinfo",
        ["AccountConfig"],
        [['{"clientId":"visible","clientSecret":"secret","nested":{"AppSec":"key"}}']],
        CONFIGURED_MASK_COLUMNS,
    )

    assert rows == [
        [
            '{"clientId":"visible","clientSecret":"[REDACTED]",'
            '"nested":{"AppSec":"[REDACTED]"}}'
        ]
    ]
    assert masked == ["AccountConfig"]


def test_token_business_fields_are_not_masked_by_substring_alone():
    rows, masked = mask_result_rows(
        "SELECT IntegrateChannelCode, TokenCallConfig, TokenEffectiveDuration "
        "FROM integratechannel",
        ["IntegrateChannelCode", "TokenCallConfig", "TokenEffectiveDuration"],
        [["channel-a", "{}", 12]],
        CONFIGURED_MASK_COLUMNS,
    )

    assert rows == [["channel-a", "{}", 12]]
    assert masked == []


@pytest.mark.parametrize("union", [False, True])
@pytest.mark.parametrize("width", [2, 80])
def test_wide_nested_ctes_keep_sensitive_and_public_sources_separate(width, union):
    public = [f"public_{index}" for index in range(width)]
    public_sql = ", ".join(public)
    query = (
        f"WITH source AS (SELECT password AS hidden, {public_sql} FROM users), "
        f"wrapped AS (SELECT hidden AS value, {public_sql} FROM source) "
        f"SELECT value AS harmless, {public_sql} FROM wrapped"
    )
    if union:
        query += f" UNION ALL SELECT value, {public_sql} FROM wrapped"
    values = ["synthetic-sensitive-value", *range(width)]

    rows, masked = mask_result_rows(
        query, ["harmless", *public], [values], CONFIGURED_MASK_COLUMNS
    )

    assert rows == [[MASKED_VALUE, *range(width)]]
    assert masked == ["harmless"]


def test_duplicate_output_names_are_masked_by_position():
    rows, masked = mask_result_rows(
        "WITH source AS (SELECT password AS hidden, DisplayName FROM users) "
        "SELECT hidden AS value, DisplayName AS value FROM source",
        ["value", "value"],
        [["synthetic-sensitive-value", "visible"]],
        CONFIGURED_MASK_COLUMNS,
    )

    assert rows == [[MASKED_VALUE, "visible"]]
    assert masked == ["value"]


@pytest.mark.parametrize("failure_point", ["build_scope", "_sensitive_outputs"])
def test_scope_failure_preserves_direct_column_and_json_masking(
    monkeypatch, failure_point
):
    import mysql_mcp_server.results as results

    def fail(*args, **kwargs):
        raise ValueError("synthetic analysis failure")

    monkeypatch.setattr(results, failure_point, fail)
    rows, masked = mask_result_rows(
        "SELECT password AS harmless, DisplayName, AccountConfig FROM users",
        ["harmless", "DisplayName", "AccountConfig"],
        [["synthetic-sensitive-value", "visible", '{"token":"sensitive","id":7}']],
        CONFIGURED_MASK_COLUMNS,
    )

    assert rows == [[MASKED_VALUE, "visible", '{"token":"[REDACTED]","id":7}']]
    assert masked == ["harmless", "AccountConfig"]


def test_one_unresolved_output_does_not_stop_other_column_traces():
    # `ghost` names no source, so its column fails closed on its own while
    # the traceable columns keep their individual decisions.
    rows, masked = mask_result_rows(
        "WITH source AS (SELECT password AS hidden, DisplayName FROM users) "
        "SELECT ghost.anything AS unknown, DisplayName, hidden AS harmless FROM source",
        ["unknown", "DisplayName", "harmless"],
        [["unresolved", "visible", "synthetic-sensitive-value"]],
        CONFIGURED_MASK_COLUMNS,
    )

    assert rows == [[MASKED_VALUE, "visible", MASKED_VALUE]]
    assert masked == ["unknown", "harmless"]


@pytest.mark.parametrize(
    "table", ["mobile_devices", "secret_settings", "app.email_jobs"]
)
def test_sensitive_table_names_do_not_mask_ordinary_columns(table):
    rows, masked = mask_result_rows(
        f"SELECT id, status FROM {table}",
        ["id", "status"],
        [[7, "active"]],
        CONFIGURED_MASK_COLUMNS,
    )

    assert rows == [[7, "active"]]
    assert masked == []


def test_configured_masks_leave_business_metadata_visible():
    columns = [
        "token_count",
        "PasswordUpdatedAt",
        "EmailEnabled",
        "PhoneCount",
        "TokenCallConfig",
        "AccessTokenExpiresAt",
        "ClientSecretVersion",
        "private_key_algorithm",
        "mobile_device_type",
    ]
    values = [5, "2026-10-14", 1, 3, "/token", "2026-11-01", 2, "RSA", "tablet"]
    rows, masked = mask_result_rows(
        "SELECT " + ", ".join(columns) + " FROM settings",
        columns,
        [values],
        CONFIGURED_MASK_COLUMNS,
    )

    assert rows == [values]
    assert masked == []


@pytest.mark.parametrize(
    "column",
    [
        "Password",
        "UserPWD",
        "PasswordHash",
        "password_hash",
        "PasswordValue",
        "ClientSecret",
        "secret_key",
        "AccessToken",
        "refresh_token",
        "token_value",
        "TokenHash",
        "AppSec",
        "ApiKey",
        "api_key",
        "PrivateKey",
        "private_key_pem",
        "Mobile",
        "phone_number",
        "EmailAddress",
        "id_card",
        "SSN",
    ],
)
def test_configured_field_names_are_masked_through_aliases(column):
    rows, masked = mask_result_rows(
        f"WITH source AS (SELECT {column} AS value FROM users) "
        "SELECT value AS harmless FROM source",
        ["harmless"],
        [["synthetic-sensitive-value"]],
        (column.lower(),),
    )

    assert rows == [[MASKED_VALUE]]
    assert masked == ["harmless"]


def test_explicit_broad_and_qualified_rules_keep_their_intended_scope():
    rows, masked = mask_result_rows(
        "SELECT config.token_count, devices.id FROM config JOIN devices ON 1=1",
        ["token_count", "id"],
        [[5, 7]],
        ("token_*", "devices.id"),
    )

    assert rows == [[MASKED_VALUE, MASKED_VALUE]]
    assert masked == ["token_count", "id"]
    rows, masked = mask_result_rows(
        "SELECT orders.id, devices.status FROM orders JOIN devices ON 1=1",
        ["id", "status"],
        [[7, "active"]],
        ("devices.id",),
    )
    assert rows == [[7, "active"]]
    assert masked == []


@pytest.mark.parametrize(
    "query",
    [
        "SELECT COUNT(password) AS n FROM users",
        "SELECT COUNT(DISTINCT password) AS n FROM users",
        "SELECT COUNT(password) OVER () AS n FROM users",
        "WITH counts AS (SELECT COUNT(password) AS n FROM users) SELECT n FROM counts",
        "SELECT COUNT(password) AS n FROM users UNION ALL SELECT COUNT(token) FROM users",
    ],
)
def test_count_statistics_remain_visible(query):
    rows, masked = mask_result_rows(query, ["n"], [[12]], CONFIGURED_MASK_COLUMNS)

    assert rows == [[12]]
    assert masked == []


def test_count_does_not_unmask_sensitive_siblings_or_combined_values():
    rows, masked = mask_result_rows(
        "SELECT password AS harmless, COUNT(*) AS n, "
        "CONCAT(password, COUNT(password)) AS combined FROM users GROUP BY password",
        ["harmless", "n", "combined"],
        [["synthetic-sensitive-value", 12, "synthetic-sensitive-value12"]],
        CONFIGURED_MASK_COLUMNS,
    )

    assert rows == [[MASKED_VALUE, 12, MASKED_VALUE]]
    assert masked == ["harmless", "combined"]


def test_json_business_metadata_is_preserved_while_secret_values_are_masked():
    value = {
        "token_count": 5,
        "token_url": "/token",
        "email_enabled": True,
        "clientSecret": "synthetic-sensitive-value",
        "nested": [
            {"token_value": "synthetic-sensitive-value", "token_expires_in": 60}
        ],
    }
    rows, masked = mask_result_rows(
        "SELECT AccountConfig FROM channels",
        ["AccountConfig"],
        [[json.dumps(value)]],
        CONFIGURED_MASK_COLUMNS,
    )

    expected = {
        **value,
        "clientSecret": MASKED_VALUE,
        "nested": [{"token_value": MASKED_VALUE, "token_expires_in": 60}],
    }
    assert json.loads(rows[0][0]) == expected
    assert masked == ["AccountConfig"]


# Second independent review: conversions that sqlglot reports under the inner
# column's name, stars mixed with expressions, and qualified patterns behind
# same-named nested columns.
@pytest.mark.parametrize(
    ("query", "columns", "pattern", "sensitive"),
    [
        (
            "SELECT * FROM (SELECT CAST(password AS CHAR) FROM users) t",
            ["CAST(password AS CHAR)"],
            "password",
            [0],
        ),
        (
            "SELECT * FROM (SELECT CONVERT(password USING utf8mb4) FROM users) t",
            ["CAST(password AS CHAR CHARACTER SET utf8mb4)"],
            "password",
            [0],
        ),
        (
            "WITH x AS (SELECT JSON_UNQUOTE(password) FROM users) SELECT * FROM x",
            ["JSON_UNQUOTE(password)"],
            "password",
            [0],
        ),
        (
            "SELECT t.`CAST(password AS CHAR)` "
            "FROM (SELECT CAST(password AS CHAR) FROM users) t",
            ["CAST(password AS CHAR)"],
            "password",
            [0],
        ),
        (
            "SELECT *, UPPER(password) FROM users",
            ["id", "password", "UPPER(password)"],
            "password",
            [1, 2],
        ),
        (
            "SELECT *, (SELECT password FROM users LIMIT 1) FROM orders",
            ["id", "(SELECT password FROM users LIMIT 1)"],
            "password",
            [1],
        ),
        (
            "SELECT * FROM users WHERE phone IN (SELECT phone FROM blacklist)",
            ["id", "phone"],
            "users.phone",
            [1],
        ),
        (
            "WITH x AS (SELECT * FROM users WHERE active = 1) SELECT name, phone FROM x",
            ["name", "phone"],
            "users.phone",
            [1],
        ),
        (
            "SELECT o.id, phone FROM orders o JOIN users u ON u.id = o.user_id",
            ["id", "phone"],
            "users.phone",
            [1],
        ),
        (
            "SELECT name FROM people UNION ALL SELECT password FROM users",
            ["name"],
            "password",
            [0],
        ),
        (
            "SELECT u.id, l.p FROM users u JOIN LATERAL (SELECT u.password AS p) l ON TRUE",
            ["id", "p"],
            "password",
            [1],
        ),
    ],
)
def test_fail_closed_resolution_masks_every_traceable_source(
    query, columns, pattern, sensitive
):
    rows, masked = mask_result_rows(
        query, columns, [["value"] * len(columns)], (pattern,)
    )

    assert masked == [columns[index] for index in sensitive]
    assert all(rows[0][index] == MASKED_VALUE for index in sensitive)


@pytest.mark.parametrize(
    ("query", "columns", "masked_columns"),
    [
        (
            "SELECT o.*, (SELECT MAX(LENGTH(password)) FROM users) AS maxlen FROM orders o",
            ["id", "amount", "maxlen"],
            ["maxlen"],
        ),
        (
            "SELECT * FROM (SELECT name AS p FROM users) t "
            "WHERE EXISTS (SELECT password AS p FROM users)",
            ["p"],
            [],
        ),
        (
            "SELECT name, EXISTS(SELECT password FROM users) AS e FROM people",
            ["name", "e"],
            [],
        ),
        (
            "SELECT *, UPPER(password) FROM users",
            ["id", "name", "password", "UPPER(password)"],
            ["password", "UPPER(password)"],
        ),
    ],
)
def test_fail_closed_resolution_leaves_unrelated_columns_visible(
    query, columns, masked_columns
):
    _rows, masked = mask_result_rows(
        query, columns, [["value"] * len(columns)], ("password",)
    )

    assert masked == masked_columns


@pytest.mark.parametrize(("database", "masked"), [("app", ["phone"]), ("crm", [])])
def test_database_qualified_patterns_use_the_selected_database(database, masked):
    _rows, masked_columns = mask_result_rows(
        "SELECT * FROM `users` LIMIT 100",
        ["id", "phone"],
        [[1, "13800000000"]],
        ("app.users.phone",),
        database=database,
    )

    assert masked_columns == masked


# Third independent review (R3-*): shapes that leaked after the scope-based
# rewrite, plus cases that pin each resolver rule against single-point mutation.
@pytest.mark.parametrize(
    ("query", "columns", "pattern", "masked_columns"),
    [
        # R3-1: equally named outputs from a star and an explicit projection.
        (
            "SELECT *, 1 AS password FROM users",
            ["id", "password", "password"],
            "password",
            ["password", "password"],
        ),
        (
            "SELECT u.*, o.phone FROM users u JOIN orders o ON o.user_id = u.id",
            ["id", "phone", "phone"],
            "users.phone",
            ["phone", "phone"],
        ),
        # R3-2: a star in the first set-operation branch hides positions.
        (
            "SELECT * FROM orders UNION ALL SELECT id, password FROM users",
            ["id", "amount"],
            "password",
            ["id", "amount"],
        ),
        # R3-4: a named window is not a column alias.
        (
            "SELECT *, LEAD(password) OVER w FROM users WINDOW w AS (ORDER BY id)",
            ["id", "password", "LEAD(password) OVER w"],
            "password",
            ["password", "LEAD(password) OVER w"],
        ),
        # R3-5: JSON_TABLE columns are extracted from its input expression.
        (
            "SELECT * FROM users u, "
            "JSON_TABLE(u.profile, '$' COLUMNS (phone VARCHAR(20) PATH '$.phone')) jt",
            ["id", "profile", "phone"],
            "profile",
            ["profile", "phone"],
        ),
        # R3-6: TABLE statements are SELECT * over the table.
        ("TABLE users", ["id", "phone"], "users.phone", ["phone"]),
        (
            "WITH x(a, b) AS (TABLE users) SELECT * FROM x",
            ["a", "b"],
            "password",
            ["a", "b"],
        ),
        # R3-7: unqualified names in subqueries may resolve in the outer query,
        # including its select-list aliases.
        (
            "SELECT id, (SELECT phone FROM orders o WHERE o.user_id = users.id LIMIT 1) "
            "AS x FROM users",
            ["id", "x"],
            "users.phone",
            ["x"],
        ),
        (
            "SELECT password AS a, (SELECT a FROM orders LIMIT 1) AS b FROM users",
            ["a", "b"],
            "password",
            ["a", "b"],
        ),
        # R3-8: an explicit alias cannot vouch for an unaliased expression's name.
        (
            "SELECT t.`LCASE(password)` AS a, 1 AS `LOWER(password)` "
            "FROM (SELECT LCASE(password) FROM users) t",
            ["a", "LOWER(password)"],
            "password",
            ["a"],
        ),
        # Derived and CTE column names are sources even for literals.
        (
            "WITH x AS (SELECT 'v' AS password) SELECT password AS a FROM x",
            ["a"],
            "password",
            ["a"],
        ),
        # Alias-qualified patterns keep matching through table aliases.
        ("SELECT u.password AS p FROM users u", ["p"], "u.password", ["p"]),
        # A star over a base table inside a scalar subquery may expose a match.
        (
            "SELECT name, (SELECT * FROM secrets LIMIT 1) AS s FROM users",
            ["name", "s"],
            "password",
            ["s"],
        ),
        # Correlated qualified references resolve in the enclosing scope.
        (
            "SELECT id, (SELECT u.password FROM orders o LIMIT 1) AS x FROM users u",
            ["id", "x"],
            "password",
            ["x"],
        ),
        # Every branch of a longer set operation is considered.
        (
            "SELECT * FROM (SELECT a AS p FROM x UNION ALL SELECT b FROM y "
            "UNION ALL SELECT password FROM users) t",
            ["p"],
            "password",
            ["p"],
        ),
    ],
)
def test_fail_closed_resolution_after_third_review(
    query, columns, pattern, masked_columns
):
    _rows, masked = mask_result_rows(
        query, columns, [["value"] * len(columns)], (pattern,)
    )

    assert masked == masked_columns


@pytest.mark.parametrize(
    ("query", "columns"),
    [
        (
            "SELECT * FROM users u, "
            "JSON_TABLE(u.tags, '$[*]' COLUMNS (tag VARCHAR(20) PATH '$')) jt",
            ["id", "tags", "tag"],
        ),
        ("SELECT * FROM (VALUES ROW(1, 2)) AS t(a, b)", ["a", "b"]),
        (
            "SELECT l.x FROM users u, LATERAL (SELECT u.name AS x) l",
            ["x"],
        ),
    ],
)
def test_fail_closed_resolution_keeps_traceable_safe_columns_visible(query, columns):
    _rows, masked = mask_result_rows(
        query, columns, [["value"] * len(columns)], ("password",)
    )

    assert masked == []


def test_shared_cte_references_are_resolved_in_linear_time():
    # Each level references the previous CTE twice; without memoization the
    # analysis doubled per level (R3-3) and could pin a worker thread. At 19
    # levels an exponential regression takes seconds, so it fails, not hangs.
    ctes = ["c0 AS (SELECT id AS x FROM orders)"] + [
        f"c{i} AS (SELECT a.x + b.x AS x FROM c{i - 1} a JOIN c{i - 1} b ON a.x = b.x)"
        for i in range(1, 20)
    ]
    query = "WITH " + ", ".join(ctes) + " SELECT x FROM c19"

    started = time.perf_counter()
    _rows, masked = mask_result_rows(query, ["x"], [[1]], ("password",))

    assert masked == []
    assert time.perf_counter() - started < 1


# Fourth independent review (R4-*).
@pytest.mark.parametrize(
    ("query", "columns", "pattern", "database", "masked_columns"),
    [
        # R4-2: same-named tables in two databases; sqlglot renames the second.
        (
            "SELECT crm.users.id, app.users.password "
            "FROM crm.users JOIN app.users ON crm.users.id = app.users.id",
            ["id", "password"],
            "app.users.password",
            None,
            ["password"],
        ),
        (
            "SELECT crm.users.id, users.password "
            "FROM crm.users JOIN users ON crm.users.id = users.id",
            ["id", "password"],
            "app.users.password",
            "app",
            ["password"],
        ),
        (
            "SELECT crm.users.password FROM crm.users JOIN app.users ON 1 = 1",
            ["password"],
            "app.users.password",
            None,
            [],
        ),
        # R4-3: a string literal cannot vouch for an expression's generated
        # name, including names taken before the analysis-only COUNT rewrite.
        (
            "SELECT t.`CONCAT(COUNT(*), MAX(password))` AS a, "
            "'CONCAT(0, MAX(password))' "
            "FROM (SELECT CONCAT(COUNT(*), MAX(password)) FROM users) t",
            ["a", "CONCAT(0, MAX(password))"],
            "password",
            None,
            ["a"],
        ),
        (
            "SELECT *, 'CONCAT(0, MAX(password))' "
            "FROM (SELECT CONCAT(COUNT(*), MAX(password)) FROM users) t",
            ["CONCAT(COUNT(*), MAX(password))", "CONCAT(0, MAX(password))"],
            "password",
            None,
            ["CONCAT(COUNT(*), MAX(password))"],
        ),
        (
            "SELECT t.Name_exp_1 AS a, 'UPPER(password)' "
            "FROM (SELECT UPPER(password) FROM users) t",
            ["a", "UPPER(password)"],
            "password",
            None,
            ["a"],
        ),
        # R4-4: derived tables and CTEs inside a subquery reach outer sources.
        (
            "SELECT id, (SELECT v FROM (SELECT password AS v FROM orders LIMIT 1) dt) "
            "AS x FROM users",
            ["id", "x"],
            "users.password",
            None,
            ["x"],
        ),
        (
            "SELECT id, (WITH c AS (SELECT password AS v FROM orders) "
            "SELECT v FROM c LIMIT 1) AS x FROM users",
            ["id", "x"],
            "users.password",
            None,
            ["x"],
        ),
        # A derived table at the top level cannot see sibling tables.
        (
            "SELECT u.id, t.n FROM users u JOIN (SELECT name AS n FROM orders) t ON 1 = 1",
            ["id", "n"],
            "users.name",
            None,
            [],
        ),
    ],
)
def test_fail_closed_resolution_after_fourth_review(
    query, columns, pattern, database, masked_columns
):
    _rows, masked = mask_result_rows(
        query, columns, [["value"] * len(columns)], (pattern,), database=database
    )

    assert masked == masked_columns


def test_outer_alias_resolution_is_linear_in_nesting_depth():
    # R4-1: each level's unqualified `a` also checks the enclosing select-list
    # aliases; without caching this doubled per level.
    query = "SELECT 1"
    for _ in range(19):
        query = f"SELECT a AS a, (1, 1) = ({query}) AS b FROM (SELECT 1 AS a) t"

    started = time.perf_counter()
    _rows, masked = mask_result_rows(query, ["a", "b"], [[1, 1]], ("password",))

    assert masked == []
    assert time.perf_counter() - started < 1


# Fifth review (R5-*), focused on the fourth-round fixes.
@pytest.mark.parametrize(
    ("query", "columns", "pattern", "database", "masked_columns"),
    [
        # R5-1: a column-list name cannot vouch for an expression's name.
        (
            "SELECT t.`LCASE(password)` AS a, t2.`LOWER(password)` "
            "FROM (SELECT LCASE(password) FROM users) t, "
            "(SELECT 1) AS t2(`LOWER(password)`)",
            ["a", "LOWER(password)"],
            "password",
            None,
            ["a"],
        ),
        (
            "WITH c(`LOWER(password)`) AS (SELECT 1) "
            "SELECT t.`LCASE(password)` AS a, c.`LOWER(password)` "
            "FROM (SELECT LCASE(password) FROM users) t, c",
            ["a", "LOWER(password)"],
            "password",
            None,
            ["a"],
        ),
        # R5-2: an aliased inner table cannot be referenced by its name, so
        # `users.password` names the outer table.
        (
            "SELECT (SELECT users.password FROM crm.users u LIMIT 1) AS x FROM app.users",
            ["x"],
            "app.users.password",
            None,
            ["x"],
        ),
        (
            "SELECT (SELECT users.password FROM crm.users u LIMIT 1) AS x FROM users",
            ["x"],
            "app.users.password",
            "app",
            ["x"],
        ),
        # R5-4: a select-list alias does not resolve names inside itself.
        (
            "SELECT id, (SELECT total FROM orders o WHERE o.user_id = users.id LIMIT 1) "
            "AS total FROM users",
            ["id", "total"],
            "password",
            None,
            [],
        ),
    ],
)
def test_fail_closed_resolution_after_fifth_review(
    query, columns, pattern, database, masked_columns
):
    _rows, masked = mask_result_rows(
        query, columns, [["value"] * len(columns)], (pattern,), database=database
    )

    assert masked == masked_columns


def test_analysis_failure_on_nested_query_masks_every_column(monkeypatch):
    # R5-3: the fallback cannot attribute sources nested below the top level,
    # so a matching reference anywhere masks the whole result.
    import mysql_mcp_server.results as results

    def fail(*args, **kwargs):
        raise RecursionError("synthetic analysis failure")

    monkeypatch.setattr(results, "_sensitive_outputs", fail)
    rows, masked = mask_result_rows(
        "SELECT a, b FROM (SELECT password AS a, name AS b FROM users) t",
        ["a", "b"],
        [["secret", "visible"]],
        ("password",),
    )

    assert rows == [[MASKED_VALUE, MASKED_VALUE]]
    assert masked == ["a", "b"]


# JSON values read by path are masked when a path key matches a bare pattern,
# matching how keys inside returned JSON text are masked.
@pytest.mark.parametrize(
    "query",
    [
        "SELECT id, JSON_EXTRACT(profile, '$.phone') AS p FROM users",
        "SELECT id, profile->>'$.phone' AS p FROM users",
        "SELECT id, profile->'$.contact.phone' AS p FROM users",
        "SELECT id, profile->'$.phone.mobile' AS p FROM users",
        "SELECT id, JSON_EXTRACT(profile, '$.items[0].phone') AS p FROM users",
        "SELECT id, JSON_EXTRACT(profile, '$.\"phone\"') AS p FROM users",
        "SELECT id, JSON_VALUE(profile, '$.phone') AS p FROM users",
        # Every path of a multi-path extraction is checked.
        "SELECT id, JSON_EXTRACT(profile, '$.a', '$.phone') AS p FROM users",
        # Wildcards, recursive descent and computed paths may reach the key.
        "SELECT id, JSON_EXTRACT(profile, '$.*') AS p FROM users",
        "SELECT id, JSON_EXTRACT(profile, '$**.phone') AS p FROM users",
        # Recursive descent also reads keys nested under a sensitive key.
        "SELECT id, JSON_EXTRACT(profile, '$**.city') AS p FROM users",
        "SELECT id, JSON_EXTRACT(profile, CONCAT('$.', k)) AS p FROM users",
        "WITH x AS (SELECT id, profile->>'$.phone' AS p FROM users) SELECT * FROM x",
        "SELECT t.id, t.p FROM (SELECT id, profile->>'$.phone' AS p FROM users) t",
        "SELECT u.id, jt.p FROM users u, JSON_TABLE(u.profile, '$' COLUMNS "
        "(p VARCHAR(20) PATH '$.phone', city VARCHAR(9) PATH '$.city')) jt",
        "SELECT u.id, jt.p FROM users u, JSON_TABLE(u.profile, '$' COLUMNS "
        "(NESTED PATH '$.phone[*]' COLUMNS (p VARCHAR(9) PATH '$.number'))) jt",
        "SELECT u.id, jt.p FROM users u, "
        "JSON_TABLE(u.profile, '$.phone' COLUMNS (p VARCHAR(9) PATH '$')) jt",
    ],
)
def test_json_values_read_at_sensitive_keys_are_masked(query):
    rows, masked = mask_result_rows(query, ["id", "p"], [[1, "value"]], ("phone",))

    assert rows == [[1, MASKED_VALUE]]
    assert masked == ["p"]


@pytest.mark.parametrize(
    ("query", "patterns"),
    [
        ("SELECT id, profile->>'$.city' AS p FROM users", ("phone",)),
        (
            "SELECT u.id, jt.p FROM users u, JSON_TABLE(u.profile, '$' COLUMNS "
            "(phone VARCHAR(20) PATH '$.phone', p VARCHAR(9) PATH '$.city')) jt",
            ("phone",),
        ),
        # Keys are matched by bare patterns, like keys in returned JSON text.
        ("SELECT id, profile->>'$.phone' AS p FROM users", ("users.phone",)),
        ("SELECT id, JSON_EXTRACT(profile, '$.*') AS p FROM users", ("users.phone",)),
    ],
)
def test_json_values_at_other_keys_stay_visible(query, patterns):
    rows, masked = mask_result_rows(query, ["id", "p"], [[1, "value"]], patterns)

    assert rows == [[1, "value"]]
    assert masked == []


def test_json_path_filters_do_not_mask_selected_columns():
    rows, masked = mask_result_rows(
        "SELECT id FROM users WHERE profile->>'$.phone' = '1'",
        ["id"],
        [[1]],
        ("phone",),
    )

    assert rows == [[1]]
    assert masked == []


def test_analysis_failure_still_masks_json_path_extraction(monkeypatch):
    import mysql_mcp_server.results as results

    def fail(*args, **kwargs):
        raise ValueError("synthetic analysis failure")

    monkeypatch.setattr(results, "_sensitive_outputs", fail)
    rows, masked = mask_result_rows(
        "SELECT id, profile->>'$.phone' AS p FROM users",
        ["id", "p"],
        [[1, "value"]],
        ("phone",),
    )

    assert rows == [[1, MASKED_VALUE]]
    assert masked == ["p"]
