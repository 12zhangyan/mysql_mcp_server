import base64
import json
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


@pytest.mark.parametrize("failure_point", ["qualify", "build_scope"])
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


def test_one_unresolved_output_does_not_stop_other_column_traces(monkeypatch):
    import mysql_mcp_server.results as results

    original_lineage = results.lineage

    def partial_lineage(column, *args, **kwargs):
        if column == "__mcp_output_0":
            raise ValueError("synthetic analysis failure")
        return original_lineage(column, *args, **kwargs)

    monkeypatch.setattr(results, "lineage", partial_lineage)
    rows, masked = mask_result_rows(
        "WITH source AS (SELECT password AS hidden, DisplayName FROM users) "
        "SELECT DisplayName, hidden AS harmless FROM source",
        ["DisplayName", "harmless"],
        [["visible", "synthetic-sensitive-value"]],
        CONFIGURED_MASK_COLUMNS,
    )

    assert rows == [["visible", MASKED_VALUE]]
    assert masked == ["harmless"]


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
