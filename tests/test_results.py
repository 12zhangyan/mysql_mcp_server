import base64
import json
from datetime import datetime
from decimal import Decimal

from mysql_mcp_server.config import DEFAULT_MASK_COLUMNS
from mysql_mcp_server.results import (
    MASKED_VALUE,
    QueryResult,
    mask_result_rows,
    serialize_value,
)


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


def test_large_cell_is_truncated_deterministically():
    assert serialize_value("abcdefgh", 4) == "abcd…[truncated]"


def test_sensitive_results_are_masked_across_aliases_and_ctes():
    rows, masked = mask_result_rows(
        "WITH source AS (SELECT password AS value FROM users) SELECT value AS safe FROM source",
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
        DEFAULT_MASK_COLUMNS,
    )

    assert rows == [[MASKED_VALUE, MASKED_VALUE, "Alice"]]
    assert masked == ["harmless", "Mobile"]


def test_sensitive_alias_does_not_mask_a_safe_source_column():
    rows, masked = mask_result_rows(
        "SELECT DisplayName AS secret_col FROM user",
        ["secret_col"],
        [["Alice"]],
        DEFAULT_MASK_COLUMNS,
    )

    assert rows == [["Alice"]]
    assert masked == []


def test_json_secret_keys_are_masked_without_hiding_the_config_column():
    rows, masked = mask_result_rows(
        "SELECT AccountConfig FROM opentityintegratechannelinfo",
        ["AccountConfig"],
        [['{"clientId":"visible","clientSecret":"secret","nested":{"AppSec":"key"}}']],
        DEFAULT_MASK_COLUMNS,
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
        DEFAULT_MASK_COLUMNS,
    )

    assert rows == [["channel-a", "{}", 12]]
    assert masked == []
