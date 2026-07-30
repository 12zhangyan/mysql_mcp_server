"""Stable CSV/JSON result serialization for MySQL values."""

from __future__ import annotations

import base64
import csv
import io
import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

TRUNCATION_SUFFIX = "…[truncated]"


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
