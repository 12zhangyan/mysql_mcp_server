"""Stable CSV/JSON result serialization for MySQL values."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal
from fnmatch import fnmatchcase
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import build_scope

from .errors import QueryFailure

TRUNCATION_SUFFIX = "…[truncated]"
MASKED_VALUE = "[REDACTED]"


def serialize_value(value: Any, max_length: int | None) -> Any:
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

    if max_length is not None and len(value) > max_length:
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


def _lineage_source_candidates(
    statement: exp.Query, columns: list[str]
) -> list[set[str] | None]:
    """Prepare one query scope, then trace each output without copying the query."""
    try:
        qualified = qualify(
            statement.copy(),
            dialect="mysql",
            schema={},
            validate_qualify_columns=False,
            identify=False,
        )
        scope = build_scope(qualified)
    except Exception:
        return [None] * len(columns)
    if scope is None:
        return [None] * len(columns)

    resolved: list[set[str] | None] = []
    for column in columns:
        try:
            node = lineage(
                column,
                qualified,
                schema={},
                dialect="mysql",
                scope=scope,
                copy=False,
                trim_selects=False,
            )
            candidates = {
                candidate
                for lineage_node in node.walk()
                for candidate in _name_candidates(str(lineage_node.name))
            }
            resolved.append(candidates or None)
        except Exception:
            resolved.append(None)
    return resolved


def _result_source_candidates(query: str, columns: list[str]) -> list[set[str] | None]:
    """Resolve each SELECT output to its source columns, including CTE aliases.

    ``None`` means the projection contains a star or could not be mapped safely;
    callers then use the connector's output name as the source-column fallback.
    """
    statement = sqlglot.parse_one(query, read="mysql")
    if not isinstance(statement, exp.Query):
        return [None] * len(columns)

    # COUNT reveals cardinality, not its input values. Remove its source
    # references only in this masking AST; the executed SQL is unchanged.
    for aggregate in list(statement.find_all(exp.Count)):
        aggregate.replace(exp.Literal.number(0))

    if not isinstance(statement, exp.Select):
        query_columns = {
            candidate
            for column in statement.find_all(exp.Column)
            for candidate in _column_candidates(column)
        }
        return [
            candidates if candidates is not None else query_columns or None
            for candidates in _lineage_source_candidates(statement, columns)
        ]

    projections = list(statement.expressions)
    if len(projections) != len(columns):
        return _lineage_source_candidates(statement, columns)
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
    traced_candidates = _lineage_source_candidates(traced_statement, aliases)
    for projection, traced in zip(projections, traced_candidates):
        candidates = {
            candidate
            for column in projection.find_all(exp.Column)
            for candidate in _column_candidates(column)
        }
        if traced is not None:
            candidates.update(traced)
        elif not candidates:
            # Keep the output-name fallback when an indirect source cannot be
            # resolved, and preserve direct source columns on analysis failure.
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

    column_patterns = tuple(p for p in normalized_patterns if "." not in p)
    qualified_patterns = tuple(p for p in normalized_patterns if "." in p)

    def matches(candidates: set[str]) -> bool:
        # Ordinary column patterns must not match a database, table or alias.
        # Explicit table.column / database.table.column patterns retain scope.
        names = {candidate.rsplit(".", 1)[-1] for candidate in candidates}
        return any(
            fnmatchcase(name, pattern) for name in names for pattern in column_patterns
        ) or any(
            fnmatchcase(candidate, pattern)
            for candidate in candidates
            for pattern in qualified_patterns
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
    content_truncated: bool = False
    truncation_reasons: tuple[str, ...] = ()
    queue_wait_ms: int = 0
    plan: dict[str, Any] | None = None

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
            "returned_rows": len(self.rows),
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
            "content_truncated": self.content_truncated,
            "truncation_reasons": list(self.truncation_reasons),
            "queue_wait_ms": self.queue_wait_ms,
            **({"plan": self.plan} if self.plan is not None else {}),
        }

    def plan_chunks(
        self, *, max_rows: int, chunk_size: int, expected_plan_id: str | None
    ) -> QueryResult:
        """Page a masked JSON plan as lossless, stable-sized character chunks."""
        if (
            len(self.rows) != 1
            or len(self.columns) != 1
            or len(self.rows[0]) != 1
            or not isinstance(self.rows[0][0], str)
            or self.truncated
        ):
            raise QueryFailure(
                "PLAN_FORMAT_UNSUPPORTED",
                "The database did not return a single JSON execution plan.",
                "Check server support for EXPLAIN FORMAT=JSON, or retry explain_sql "
                "with plan_output=rows; do not treat this as a complete plan.",
            )
        text = self.rows[0][0]
        fingerprint = hashlib.sha256()
        fingerprint.update(
            json.dumps(
                [self.connection, self.database, self.query_id, chunk_size],
                ensure_ascii=False,
            ).encode("utf-8")
        )
        fingerprint.update(text.encode("utf-8"))
        plan_id = fingerprint.hexdigest()
        if expected_plan_id is not None and expected_plan_id != plan_id:
            raise QueryFailure(
                "PLAN_CHANGED",
                "The execution plan or its chunking context changed between pages.",
                "Discard previously collected chunks and restart at offset=0 "
                "without expected_plan_id; never combine different plan versions.",
            )
        total = max(1, (len(text) + chunk_size - 1) // chunk_size)
        end = min(total, self.offset + max_rows)
        truncated = end < total
        return replace(
            self,
            columns=["part", "text"],
            rows=[
                [index, text[index * chunk_size : (index + 1) * chunk_size]]
                for index in range(self.offset, end)
            ],
            truncated=truncated,
            truncation_reasons=("row_limit",) if truncated else (),
            content_truncated=False,
            masked_columns=["text"] if self.masked_columns else [],
            plan={
                "id": plan_id,
                "characters": len(text),
                "total_chunks": total,
                "chunk_size": chunk_size,
            },
        )

    def fit_response(self, result_format: str, max_bytes: int) -> QueryResult:
        """Keep whole rows, accounting for the exact UTF-8 rendered envelope."""
        if len(self.render(result_format).encode("utf-8")) <= max_bytes:
            return self
        reasons = tuple(dict.fromkeys((*self.truncation_reasons, "response_bytes")))

        def candidate(count: int) -> QueryResult:
            return replace(
                self, rows=self.rows[:count], truncated=True, truncation_reasons=reasons
            )

        low, high = 0, len(self.rows)
        while low < high:
            middle = (low + high + 1) // 2
            if (
                len(candidate(middle).render(result_format).encode("utf-8"))
                <= max_bytes
            ):
                low = middle
            else:
                high = middle - 1
        if not low:
            raise QueryFailure(
                "RESULT_TOO_LARGE",
                "The result header or first row exceeds the response budget.",
                "Select fewer columns or use SUBSTRING for large text values; request fewer metadata fields. "
                "Increasing max_rows will not help. No rows were delivered; retry the same offset with a narrower projection.",
                connection=self.connection,
                database=self.database,
                query_id=self.query_id,
                offset=self.offset,
                max_response_bytes=max_bytes,
            )
        return candidate(low)

    def render(self, result_format: str) -> str:
        if result_format == "compact":
            metadata = {
                key: value
                for key, value in self.to_payload().items()
                if key not in {"rows", "columns"}
            }
            return (
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
                + "\n"
                + self.render("csv")
            )
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
                    f"rows={len(self.rows)}, reasons={','.join(self.truncation_reasons) or 'row_limit'}]"
                ]
            )
        if self.content_truncated:
            writer.writerow(
                [
                    "[content_truncated: cell_length; select SUBSTRING ranges to read the remaining content]"
                ]
            )
        return output.getvalue().rstrip("\n")
