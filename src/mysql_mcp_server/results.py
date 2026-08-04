"""Stable CSV/JSON result serialization for MySQL values."""

from __future__ import annotations

import base64
import csv
import io
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from fnmatch import fnmatchcase
from typing import Any

import sqlglot
from sqlglot import exp

TRUNCATION_SUFFIX = "…[truncated]"
MASKED_VALUE = "[REDACTED]"


def serialize_value(value: Any, max_length: int) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        value = f"base64:{encoded}"
    elif isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        value = str(value)

    if len(value) > max_length:
        return value[:max_length] + TRUNCATION_SUFFIX
    return value


def _column_candidates(column: exp.Column) -> set[str]:
    parts = [
        value.lower()
        for value in (column.catalog, column.db, column.table, column.name)
        if value
    ]
    return {".".join(parts[index:]) for index in range(len(parts))}


def mask_result_rows(
    query: str,
    columns: list[str],
    rows: list[list[Any]],
    patterns: tuple[str, ...],
) -> tuple[list[list[Any]], list[str]]:
    """Mask configured columns, failing conservatively across aliases and CTEs."""
    normalized_patterns = tuple(pattern.lower() for pattern in patterns if pattern)
    if not normalized_patterns or not rows:
        return rows, []

    def matches(candidates: set[str]) -> bool:
        return any(
            fnmatchcase(candidate, pattern)
            for candidate in candidates
            for pattern in normalized_patterns
        )

    masked_indexes = {
        index for index, name in enumerate(columns) if matches({str(name).lower()})
    }

    # If a sensitive source column appears anywhere in the statement, mask the
    # complete result. This intentionally favors confidentiality over precision:
    # aliases, CTEs and expressions must not turn `password AS value` into a
    # masking bypass.
    statement = sqlglot.parse_one(query, read="mysql")
    if any(
        matches(_column_candidates(column)) for column in statement.find_all(exp.Column)
    ):
        masked_indexes = set(range(len(columns)))

    if not masked_indexes:
        return rows, []

    masked_rows = [
        [
            MASKED_VALUE if index in masked_indexes and value is not None else value
            for index, value in enumerate(row)
        ]
        for row in rows
    ]
    return masked_rows, [columns[index] for index in sorted(masked_indexes)]


@dataclass(frozen=True)
class QueryResult:
    connection: str
    database: str | None
    columns: list[str]
    rows: list[list[Any]]
    offset: int
    truncated: bool
    duration_ms: int
    query_id: str
    masked_columns: list[str] = field(default_factory=list)
    retry_count: int = 0
    requested_connection: str | None = None
    requested_database: str | None = None
    route_applied: bool = False

    @property
    def next_offset(self) -> int | None:
        return self.offset + len(self.rows) if self.truncated else None

    def to_payload(self) -> dict[str, Any]:
        return {
            "connection": self.connection,
            "database": self.database,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": len(self.rows),
            "offset": self.offset,
            "truncated": self.truncated,
            "next_offset": self.next_offset,
            "duration_ms": self.duration_ms,
            "query_id": self.query_id,
            "masked_columns": self.masked_columns,
            "retry_count": self.retry_count,
            "requested_connection": self.requested_connection,
            "requested_database": self.requested_database,
            "route_applied": self.route_applied,
        }

    def render(self, result_format: str) -> str:
        if result_format == "json":
            return json.dumps(
                self.to_payload(),
                ensure_ascii=False,
                separators=(",", ":"),
            )

        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(self.columns)
        writer.writerows(
            ["NULL" if value is None else value for value in row] for row in self.rows
        )
        if not self.rows:
            writer.writerow(["No results returned."])
        if self.truncated:
            writer.writerow(
                [
                    f"[truncated: next_offset={self.next_offset}, "
                    f"rows={len(self.rows)}]"
                ]
            )
        return output.getvalue().rstrip("\n")
