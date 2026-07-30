import base64
import json
from datetime import datetime
from decimal import Decimal

from mysql_mcp_server.results import QueryResult, serialize_value


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
