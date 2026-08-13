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
from sqlglot.lineage import lineage

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


def _name_candidates(name: str) -> set[str]:
    parts = [part.strip('`"').lower() for part in name.split(".") if part]
    return {".".join(parts[index:]) for index in range(len(parts))}


def _result_source_candidates(query: str, columns: list[str]) -> list[set[str] | None]:
    """Resolve each SELECT output to its source columns, including CTE aliases.

    ``None`` means the projection contains a star or could not be mapped safely;
    callers then use the connector's output name as the source-column fallback.
    """
    statement = sqlglot.parse_one(query, read="mysql")
    if not isinstance(statement, exp.Query):
        return [None] * len(columns)

    if not isinstance(statement, exp.Select):
        query_resolved: list[set[str] | None] = []
        query_columns = {
            candidate
            for column in statement.find_all(exp.Column)
            for candidate in _column_candidates(column)
        }
        for column_name in columns:
            query_candidates: set[str] = set()
            try:
                node = lineage(column_name, statement, dialect="mysql")
                for lineage_node in node.walk():
                    query_candidates.update(_name_candidates(str(lineage_node.name)))
            except Exception:
                query_resolved.append(query_columns or None)
                continue
            query_resolved.append(query_candidates or None)
        return query_resolved

    projections = list(statement.expressions)
    if len(projections) != len(columns):
        mismatched_resolved: list[set[str] | None] = []
        for column_name in columns:
            mismatched_candidates: set[str] = set()
            try:
                node = lineage(column_name, statement, dialect="mysql")
                for lineage_node in node.walk():
                    mismatched_candidates.update(
                        _name_candidates(str(lineage_node.name))
                    )
            except Exception:
                mismatched_resolved.append(None)
                continue
            mismatched_resolved.append(mismatched_candidates or None)
        return mismatched_resolved
    if any(projection.find(exp.Star) is not None for projection in projections):
        return [None] * len(columns)

    traced_statement = statement.copy()
    traced_projections = list(traced_statement.expressions)
    aliases = [f"__mcp_output_{index}" for index in range(len(columns))]
    traced_statement.set(
        "expressions",
        [
            projection.as_(alias, copy=False)
            for projection, alias in zip(traced_projections, aliases)
        ],
    )

    resolved: list[set[str] | None] = []
    for projection, alias in zip(projections, aliases):
        candidates = {
            candidate
            for column in projection.find_all(exp.Column)
            for candidate in _column_candidates(column)
        }
        try:
            node = lineage(alias, traced_statement, dialect="mysql")
            for lineage_node in node.walk():
                candidates.update(_name_candidates(str(lineage_node.name)))
        except Exception:
            # Direct projection columns still give a safe result for ordinary
            # SELECTs. When lineage cannot resolve an indirect source, fall back
            # to the connector output name rather than silently trusting it.
            if not candidates:
                resolved.append(None)
                continue
        resolved.append(candidates)
    return resolved


def _mask_json_keys(value: Any, matches) -> tuple[Any, bool]:
    """Redact sensitive keys inside serialized JSON without hiding safe siblings."""
    parsed = value
    serialized = isinstance(value, str)
    if serialized:
        stripped = value.lstrip()
        if not stripped.startswith(("{", "[")):
            return value, False
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value, False

    def visit(item: Any) -> tuple[Any, bool]:
        if isinstance(item, dict):
            changed = False
            output = {}
            for key, nested in item.items():
                if matches({str(key).lower()}):
                    output[key] = MASKED_VALUE if nested is not None else None
                    changed = changed or nested is not None
                else:
                    output[key], nested_changed = visit(nested)
                    changed = changed or nested_changed
            return output, changed
        if isinstance(item, list):
            list_output = []
            changed = False
            for nested in item:
                masked, nested_changed = visit(nested)
                list_output.append(masked)
                changed = changed or nested_changed
            return list_output, changed
        return item, False

    masked, changed = visit(parsed)
    if serialized:
        if changed:
            return json.dumps(masked, ensure_ascii=False, separators=(",", ":")), True
        return value, False
    return masked, changed


def mask_result_rows(
    query: str,
    columns: list[str],
    rows: list[list[Any]],
    patterns: tuple[str, ...],
) -> tuple[list[list[Any]], list[str]]:
    """Mask sensitive source columns and sensitive keys nested in JSON values."""
    normalized_patterns = tuple(pattern.lower() for pattern in patterns if pattern)
    if not normalized_patterns or not rows:
        return rows, []

    def matches(candidates: set[str]) -> bool:
        return any(
            fnmatchcase(candidate, pattern)
            for candidate in candidates
            for pattern in normalized_patterns
        )

    try:
        source_candidates = _result_source_candidates(query, columns)
    except Exception:
        source_candidates = [None] * len(columns)

    masked_indexes = {
        index
        for index, (name, candidates) in enumerate(zip(columns, source_candidates))
        if (
            matches(candidates)
            if candidates is not None
            else matches({str(name).lower()})
        )
    }

    changed_indexes = set(masked_indexes)
    masked_rows: list[list[Any]] = []
    for row in rows:
        masked_row = []
        for index, value in enumerate(row):
            if index in masked_indexes and value is not None:
                masked_row.append(MASKED_VALUE)
                continue
            masked_value, changed = _mask_json_keys(value, matches)
            masked_row.append(masked_value)
            if changed:
                changed_indexes.add(index)
        masked_rows.append(masked_row)

    return masked_rows, [columns[index] for index in sorted(changed_indexes)]


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
