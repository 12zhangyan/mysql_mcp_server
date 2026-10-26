"""Stable CSV/JSON result serialization for MySQL values."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal
from fnmatch import fnmatchcase
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.jsonpath import parse as parse_json_path
from sqlglot.optimizer.scope import Scope, ScopeType, build_scope

from .errors import QueryFailure

TRUNCATION_SUFFIX = "…[truncated]"
MASKED_VALUE = "[REDACTED]"


_PASSTHROUGH_TYPES = frozenset({type(None), bool, int, float})


def serialize_value(value: Any, max_length: int | None) -> Any:
    # Exact-type fast paths for the common driver values; this runs per cell.
    value_type = type(value)
    if value_type in _PASSTHROUGH_TYPES:
        return value
    if value_type is str:
        if max_length is not None and len(value) > max_length:
            return value[:max_length] + TRUNCATION_SUFFIX
        return value
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


# Projection names are recorded before COUNT is rewritten for analysis.
_KEY_META = "mcp_output_key"


def _output_key(name: str) -> str:
    """Compare a result column name with SQL text despite re-spacing and quoting."""
    return re.sub(r"[\s`]", "", name).lower()


def _projection_key(projection: exp.Expression) -> str:
    """The name MySQL gives a projection: its alias, column name or SQL text."""
    recorded = projection.meta.get(_KEY_META)
    if recorded is not None:
        return recorded
    # Only an Alias node names a column; a window's `alias` is its WINDOW name.
    if isinstance(projection, exp.Alias):
        return _output_key(projection.alias)
    if isinstance(projection, exp.Column):
        return _output_key(projection.name)
    if isinstance(projection, exp.Literal) and projection.is_string:
        return _output_key(projection.this)
    return _output_key(projection.sql(dialect="mysql"))


def _is_star(projection: exp.Expression) -> bool:
    return isinstance(projection, exp.Star) or (
        isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
    )


def _is_table_function(table: exp.Table) -> bool:
    """JSON_TABLE and other table functions produce rows from expressions."""
    return not isinstance(table.this, exp.Identifier)


# JSON functions that return the value stored at a path in a document.
_JSON_EXTRACTIONS = tuple(
    getattr(exp, name)
    for name in (
        "JSONExtract",
        "JSONExtractScalar",
        "JSONValue",
        "JSONExtractArray",
        "JSONExtractQuote",
        "JSONBExtract",
        "JSONBExtractScalar",
    )
    if hasattr(exp, name)
)


def _json_path_keys(path: Any) -> list[str] | None:
    """Object keys a JSON path reads, or None when they cannot be listed.

    Wildcards, recursive descent and computed (non-literal) paths may read
    any key, so callers treat None as possibly sensitive.
    """
    if isinstance(path, exp.Literal) and path.is_string:
        try:
            path = parse_json_path(path.this)
        except Exception:
            return None
    if not isinstance(path, exp.JSONPath):
        return None
    keys = []
    for part in path.expressions:
        if isinstance(part, (exp.JSONPathWildcard, exp.JSONPathRecursive)):
            return None
        if isinstance(part, exp.JSONPathKey):
            if not isinstance(part.this, str):
                return None
            keys.append(part.this)
    return keys


def _json_extraction_paths(node: exp.Expr) -> list[Any]:
    """Every path argument of a JSON extraction, e.g. JSON_EXTRACT(d, p1, p2)."""
    paths = [node.args.get(name) for name in ("expression", "path")]
    paths.extend(node.args.get("expressions") or ())
    found = [path for path in paths if path is not None]
    # No recognisable path argument: the keys read cannot be listed.
    return found or [None]


def _json_table_paths(table: exp.JSONTable, key: str) -> list[list[Any]] | None:
    """For each JSON_TABLE column named `key`: root, NESTED and column paths."""
    chains = []
    for definition in table.find_all(exp.JSONColumnDef):
        if not definition.name or _output_key(definition.name) != key:
            continue
        chain = [definition.args.get("path")]
        node = definition.parent
        while node is not None and node is not table:
            nested_path = node.args.get("path")
            if isinstance(node, exp.JSONColumnDef) and nested_path is not None:
                chain.append(nested_path)
            node = node.parent
        chain.append(table.args.get("path"))
        chains.append([path for path in chain if path is not None])
    return chains or None


def _table_function_columns(table: exp.Table) -> set[str] | None:
    """Columns a JSON_TABLE declares, or None when they cannot be listed."""
    if not isinstance(table.this, exp.JSONTable):
        return None
    names = {
        _output_key(definition.name)
        for definition in table.this.find_all(exp.JSONColumnDef)
        if definition.name
    }
    return names or None


def _column_list(expression: exp.Expr) -> list[str] | None:
    """Output names declared by a CTE or derived table column list, e.g. x(p)."""
    parent = expression.parent
    if isinstance(parent, (exp.Subquery, exp.CTE)):
        alias = parent.args.get("alias")
        if alias is not None and alias.columns:
            return [_output_key(column.name) for column in alias.columns]
    return None


def _contains(ancestor: exp.Expr, node: exp.Expr) -> bool:
    current = node.parent
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def _is_filter_subquery(select: exp.Select, root: exp.Expr) -> bool:
    """EXISTS and IN subqueries inside a projection yield only a boolean."""
    node = select.parent
    while node is not None and node is not root:
        if isinstance(node, exp.Exists) or (
            isinstance(node, exp.In) and node.args.get("query") is not None
        ):
            return True
        node = node.parent
    return isinstance(root, exp.Exists)


# Scopes whose unqualified names MySQL may resolve in the enclosing query.
_CORRELATING_SCOPES = {ScopeType.SUBQUERY, ScopeType.UDTF}


def _reaches_parent(scope: Scope) -> bool:
    """Subqueries, LATERAL, and anything nested inside them see outer sources."""
    current: Scope | None = scope
    while current is not None:
        if current.scope_type in _CORRELATING_SCOPES:
            return True
        current = current.parent
    return False

_TABLE_STATEMENT_CONTEXT = re.compile(
    r"(?:^|\(|\b(?:UNION|INTERSECT|EXCEPT|ALL|DISTINCT))$", re.IGNORECASE
)


def _table_statements_as_select(query: str) -> str:
    """Rewrite `TABLE t` query blocks as `SELECT * FROM t` for analysis only."""
    from .sql_guard import _sanitize_sql

    try:
        sanitized, _ = _sanitize_sql(query)
    except ValueError:
        return query
    pieces: list[str] = []
    position = 0
    for match in re.finditer(r"\bTABLE\b", sanitized, re.IGNORECASE):
        if _TABLE_STATEMENT_CONTEXT.search(sanitized[: match.start()].rstrip()):
            pieces.append(query[position : match.start()])
            pieces.append("SELECT * FROM")
            position = match.end()
    pieces.append(query[position:])
    return "".join(pieces)


class _SensitivityResolver:
    """Decide fail-closed whether each result column may carry a masked source.

    Every output is traced through its scope to base-table columns: aliases,
    expressions, scalar subqueries, derived tables, CTEs, set operations,
    LATERAL, table functions and stars. A column whose source cannot be
    determined is treated as sensitive. Filter subqueries (WHERE, HAVING,
    EXISTS, IN) are never visited because their values cannot reach the
    result. Results are memoized per scope so shared CTEs are analysed once.
    """

    def __init__(
        self,
        root: Scope,
        matches: Callable[[set[str]], bool],
        patterns: tuple[str, ...],
        database: str | None,
        output_keys: set[str],
    ) -> None:
        self.matches = matches
        self.patterns = patterns
        # JSON keys, like returned JSON text, are matched by bare patterns.
        self.matches_json_keys = any("." not in pattern for pattern in patterns)
        self.database = (database or "").lower()
        self.output_keys = output_keys
        self.scopes = {id(scope.expression): scope for scope in root.traverse()}
        # Projections other than stars and plain column references that
        # produce each name; a name produced twice vouches for neither.
        self.producers: dict[str, int] = {}
        for scope in self.scopes.values():
            for key in _column_list(scope.expression) or ():
                self.producers[key] = self.producers.get(key, 0) + 1
            if isinstance(scope.expression, exp.Select):
                for projection in scope.expression.expressions:
                    if not _is_star(projection) and not isinstance(
                        projection, exp.Column
                    ):
                        key = _projection_key(projection)
                        self.producers[key] = self.producers.get(key, 0) + 1
        self.cache: dict[tuple[str, int, str], bool] = {}

    def memo(self, key: tuple[str, int, str], compute: Callable[[], bool]) -> bool:
        if key not in self.cache:
            # A recursive CTE reaches itself while being analysed; fail closed.
            self.cache[key] = True
            self.cache[key] = compute()
        return self.cache[key]

    def table_column(self, table: exp.Table, name: str, alias: str = "") -> bool:
        column = name.lower()
        table_name = table.name.lower()
        database = (table.db or self.database).lower()
        candidates = {column, f"{table_name}.{column}"}
        if database:
            candidates.add(f"{database}.{table_name}.{column}")
        if alias:
            candidates.add(f"{alias.lower()}.{column}")
        return self.matches(candidates)

    def table_may_hold_match(self, table: exp.Table) -> bool:
        """A star over a base table exposes columns the parser cannot list."""
        table_name = table.name.lower()
        database = (table.db or self.database).lower()
        for pattern in self.patterns:
            parts = pattern.split(".")
            if len(parts) == 1:
                return True
            if fnmatchcase(table_name, parts[-2]) and (
                len(parts) == 2 or not database or fnmatchcase(database, parts[-3])
            ):
                return True
        return False

    def lookup(
        self, scope: Scope, alias: str, database: str = ""
    ) -> list[tuple[Any, Scope]]:
        """Find the sources a qualifier may name, in this or an enclosing scope.

        sqlglot renames a second table of the same name (users -> users_2), so
        a qualifier matches by alias or by the name of an unaliased base table
        (an aliased table cannot be referenced by its name). With a database
        (app.users.x) only that database's table matches; without one, every
        same-named table is a candidate.
        """
        alias = alias.lower()
        current: Scope | None = scope
        while current is not None:
            found: list[tuple[Any, Scope]] = []
            for key, source in current.sources.items():
                table_name = (
                    source.name.lower()
                    if isinstance(source, exp.Table) and not source.alias
                    else None
                )
                if database:
                    if (
                        isinstance(source, exp.Table)
                        and not source.alias
                        and table_name == alias
                        and (source.db or self.database).lower() == database.lower()
                    ):
                        found.append((source, current))
                elif key.lower() == alias or table_name == alias:
                    found.append((source, current))
            if found:
                return found
            current = current.parent
        return []

    def source_output(
        self, source: Any, name: str, alias: str, owner: Scope | None
    ) -> bool:
        if isinstance(source, exp.Table):
            if _is_table_function(source):
                declared = _table_function_columns(source)
                if declared is not None and _output_key(name) not in declared:
                    return False
                if owner is None:
                    return True
                # Each declared column is extracted from the function's input,
                # at a path whose keys may themselves be sensitive.
                return self.table_function_output(
                    source.this, _output_key(name), owner
                )
            return self.table_column(source, name, alias)
        if isinstance(source, Scope):
            # A derived table or CTE column is itself a named source column,
            # whatever expression produces it; only final aliases are exempt.
            column = name.lower()
            candidates = {column, f"{alias.lower()}.{column}"} if alias else {column}
            return self.matches(candidates) or self.scope_output(source, name)
        return True

    def column(self, column: exp.Column, scope: Scope) -> bool:
        return self.memo(
            ("column", id(scope), column.sql(dialect="mysql").lower()),
            lambda: self._column(column, scope),
        )

    def _column(self, column: exp.Column, scope: Scope) -> bool:
        name = column.name
        if column.table:
            sources = self.lookup(scope, column.table, column.db)
            return not sources or any(
                self.source_output(source, name, column.table, owner)
                for source, owner in sources
            )
        # MySQL resolves an unqualified name to whichever source has it, and a
        # subquery may resolve it in the enclosing query, including that
        # query's select-list aliases.
        current: Scope | None = scope
        found_source = False
        while current is not None:
            for alias, (_node, source) in current.selected_sources.items():
                found_source = True
                if self.source_output(source, name, alias, current):
                    return True
            if current is not scope and isinstance(current.expression, exp.Select):
                key = _output_key(name)
                for projection in current.expression.expressions:
                    if (
                        isinstance(projection, exp.Alias)
                        and _projection_key(projection) == key
                        and not _contains(projection, column)
                        and self.expression(projection.this, current)
                    ):
                        return True
            if not _reaches_parent(current):
                break
            current = current.parent
        return not found_source

    def branches(self, scope: Scope) -> list[Scope]:
        if not isinstance(scope.expression, exp.SetOperation):
            return [scope]
        # sqlglot renamed union_scopes to set_operation_scopes in 30.x.
        children: list[Scope] = getattr(
            scope, "set_operation_scopes", None
        ) or getattr(scope, "union_scopes", [])
        return [branch for child in children for branch in self.branches(child)]

    def scope_output(self, scope: Scope, name: str) -> bool:
        key = _output_key(name)
        return self.memo(
            ("output", id(scope), key), lambda: self._scope_output(scope, key)
        )

    def _scope_output(self, scope: Scope, key: str) -> bool:
        expression = scope.expression
        declared = _column_list(expression)
        if declared is not None:
            indexes = [index for index, name in enumerate(declared) if name == key]
            if not indexes:
                return True
            return any(self.projection_at(scope, index) for index in indexes)
        if isinstance(expression, exp.SetOperation):
            branches = self.branches(scope)
            first = branches[0].expression
            projections = first.expressions if isinstance(first, exp.Select) else []
            if not projections or any(_is_star(p) for p in projections):
                # Later branches line up by a position the star hides.
                return self.scope_output(branches[0], key) or any(
                    self.any_projection(branch) for branch in branches[1:]
                )
            indexes = [
                index
                for index, projection in enumerate(projections)
                if _projection_key(projection) == key
            ]
            if not indexes:
                return any(self.any_projection(branch) for branch in branches)
            return any(
                self.projection_at(branch, index)
                for branch in branches
                for index in indexes
            )
        if isinstance(expression, exp.Values):
            return self.expression(expression, scope)
        if not isinstance(expression, exp.Select):
            # LATERAL and table functions wrap the scopes they read from.
            inner = self.inner_scopes(scope)
            return not inner or any(self.scope_output(source, key) for source in inner)

        projections = expression.expressions
        named = [
            projection
            for projection in projections
            if not _is_star(projection) and _projection_key(projection) == key
        ]
        stars = [projection for projection in projections if _is_star(projection)]
        # Equally named outputs from explicit projections and stars are
        # indistinguishable by name, so every candidate is considered.
        if any(self.expression(projection, scope) for projection in named):
            return True
        if any(self.star_output(star, scope, key) for star in stars):
            return True
        if not named and not stars and self.matches({key}):
            # The output maps to no projection; judge it by its name.
            return True
        # An unaliased expression whose generated name matches no result column
        # may be surfacing under a name the parser cannot reproduce.
        return (not named or bool(stars)) and any(
            self.expression(projection, scope)
            for projection in projections
            if not _is_star(projection)
            and not isinstance(projection, (exp.Alias, exp.Column))
            and (
                _projection_key(projection) not in self.output_keys
                or self.producers.get(_projection_key(projection), 0) > 1
            )
        )

    def inner_scopes(self, scope: Scope) -> list[Scope]:
        return [
            source
            for source in scope.sources.values()
            if isinstance(source, Scope)
            and source.expression.find_ancestor(type(scope.expression))
            is scope.expression
        ]

    def star_sources(
        self, star: exp.Expression, scope: Scope
    ) -> list[tuple[str, Any, Scope | None]]:
        if isinstance(star, exp.Column) and star.table:
            sources = self.lookup(scope, star.table, star.db)
            if not sources:
                return [(star.table, None, None)]
            return [(star.table, source, owner) for source, owner in sources]
        return [
            (alias, source, scope)
            for alias, (_node, source) in scope.selected_sources.items()
        ]

    def star_output(self, star: exp.Expression, scope: Scope, name: str) -> bool:
        return any(
            source is None or self.source_output(source, name, alias, owner)
            for alias, source, owner in self.star_sources(star, scope)
        )

    def star_any(self, star: exp.Expression, scope: Scope) -> bool:
        for _alias, source, owner in self.star_sources(star, scope):
            if isinstance(source, exp.Table):
                if _is_table_function(source):
                    if owner is None or self.table_function_output(
                        source.this, None, owner
                    ):
                        return True
                elif self.table_may_hold_match(source):
                    return True
            elif not isinstance(source, Scope) or self.any_projection(source):
                return True
        return False

    def projection_at(self, scope: Scope, index: int) -> bool:
        expression = scope.expression
        if isinstance(expression, exp.SetOperation):
            return any(
                self.projection_at(branch, index) for branch in self.branches(scope)
            )
        if not isinstance(expression, exp.Select):
            return self.any_projection(scope)
        projections = expression.expressions
        if index >= len(projections) or any(
            _is_star(projection) for projection in projections[: index + 1]
        ):
            # A star shifts positions by a column count the parser cannot know.
            return self.any_projection(scope)
        return self.expression(projections[index], scope)

    def any_projection(self, scope: Scope) -> bool:
        return self.memo(
            ("any", id(scope), ""), lambda: self._any_projection(scope)
        )

    def _any_projection(self, scope: Scope) -> bool:
        expression = scope.expression
        if isinstance(expression, exp.SetOperation):
            return any(self.any_projection(branch) for branch in self.branches(scope))
        if isinstance(expression, exp.Values):
            return self.expression(expression, scope)
        if not isinstance(expression, exp.Select):
            inner = self.inner_scopes(scope)
            return not inner or any(self.any_projection(source) for source in inner)
        return any(
            self.star_any(projection, scope)
            if _is_star(projection)
            else self.expression(projection, scope)
            for projection in expression.expressions
        )

    def json_path(self, path: Any) -> bool:
        keys = _json_path_keys(path)
        if keys is None:
            return self.matches_json_keys
        return any(self.matches({key.lower()}) for key in keys)

    def table_function_output(
        self, function: exp.Expr, key: str | None, owner: Scope
    ) -> bool:
        """A JSON_TABLE column: its document, or a sensitive key on its path.

        ``key`` names one output column; None means any of them.
        """
        if not isinstance(function, exp.JSONTable):
            return self.expression(function, owner)
        if self.expression(function.this, owner):
            return True
        if key is None:
            # Any column: the root path and every declared column path.
            paths = [function.args.get("path")] + [
                definition.args.get("path")
                for definition in function.find_all(exp.JSONColumnDef)
            ]
            return any(self.json_path(path) for path in paths if path is not None)
        chains = _json_table_paths(function, key) or []
        return any(self.json_path(path) for chain in chains for path in chain)

    def expression(self, expression: exp.Expr, scope: Scope) -> bool:
        for node in expression.walk(prune=lambda node: isinstance(node, exp.Select)):
            if isinstance(node, exp.Select):
                if _is_filter_subquery(node, expression):
                    continue
                inner = self.scopes.get(id(node))
                if inner is None or self.any_projection(inner):
                    return True
            elif isinstance(node, exp.Column) and not _is_star(node):
                if self.column(node, scope):
                    return True
            elif isinstance(node, _JSON_EXTRACTIONS):
                # The value stored under a sensitive JSON key is sensitive,
                # whichever column holds the document.
                if any(self.json_path(path) for path in _json_extraction_paths(node)):
                    return True
        return False


def _sensitive_outputs(
    query: str,
    columns: list[str],
    matches: Callable[[set[str]], bool],
    patterns: tuple[str, ...],
    database: str | None,
) -> list[bool]:
    """Return, per result column, whether it may carry a masked source value."""
    statement = sqlglot.parse_one(_table_statements_as_select(query), read="mysql")
    if not isinstance(statement, exp.Query):
        # SHOW, DESCRIBE and EXPLAIN report metadata under fixed column names.
        return [matches({str(column).lower()}) for column in columns]

    # MySQL names columns after the SQL as written; record those names before
    # the analysis-only COUNT rewrite below changes the expressions.
    for select in statement.find_all(exp.Select):
        for projection in select.expressions:
            projection.meta[_KEY_META] = _projection_key(projection)

    # COUNT reveals cardinality, not its input values. Remove its source
    # references only in this masking AST; the executed SQL is unchanged.
    for aggregate in list(statement.find_all(exp.Count)):
        replacement = exp.Literal.number(0)
        if _KEY_META in aggregate.meta:
            replacement.meta[_KEY_META] = aggregate.meta[_KEY_META]
        aggregate.replace(replacement)

    root = build_scope(statement)
    if root is None:
        raise ValueError("query scope could not be analysed")
    resolver = _SensitivityResolver(
        root,
        matches,
        patterns,
        database,
        {_output_key(str(column)) for column in columns},
    )
    if all(
        isinstance(branch.expression, exp.Select)
        and len(branch.expression.expressions) == len(columns)
        and not any(_is_star(projection) for projection in branch.expression.expressions)
        for branch in resolver.branches(root)
    ):
        # Without a star, result columns line up with projections by position.
        return [resolver.projection_at(root, index) for index in range(len(columns))]
    return [resolver.scope_output(root, str(column)) for column in columns]


def _references_match(
    expression: exp.Expr,
    matches: Callable[[set[str]], bool],
    matches_json_keys: bool,
) -> bool:
    for column in expression.find_all(exp.Column):
        parts = [
            part.lower()
            for part in (column.catalog, column.db, column.table, column.name)
            if part
        ]
        if matches({".".join(parts[index:]) for index in range(len(parts))}):
            return True
    for extraction in expression.find_all(*_JSON_EXTRACTIONS):
        for path in _json_extraction_paths(extraction):
            keys = _json_path_keys(path)
            if (keys is None and matches_json_keys) or any(
                matches({key.lower()}) for key in keys or ()
            ):
                return True
    return False


def _fallback_sensitive_outputs(
    query: str,
    columns: list[str],
    matches: Callable[[set[str]], bool],
    patterns: tuple[str, ...],
) -> list[bool]:
    """Fail closed when scope analysis fails.

    A single SELECT without subqueries keeps per-column decisions from its
    direct references; otherwise any matching reference or star masks every
    column, because sources nested elsewhere cannot be attributed.
    """
    by_name = [matches({str(column).lower()}) for column in columns]
    json_keys = any("." not in pattern for pattern in patterns)
    try:
        statement = sqlglot.parse_one(query, read="mysql")
    except Exception:
        return by_name
    flat = isinstance(statement, exp.Select) and not any(
        select is not statement for select in statement.find_all(exp.Select)
    )
    projections = statement.expressions if flat else []
    if len(projections) == len(columns) and not any(
        _is_star(projection) for projection in projections
    ):
        return [
            named or _references_match(projection, matches, json_keys)
            for named, projection in zip(by_name, projections)
        ]
    # A star exposes columns the fallback cannot name; fail closed.
    if _references_match(statement, matches, json_keys) or any(
        isinstance(node, exp.Star) for node in statement.find_all(exp.Star)
    ):
        return [True] * len(columns)
    return by_name


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
    rows: Sequence[Sequence[Any]],
    patterns: tuple[str, ...],
    database: str | None = None,
) -> tuple[Sequence[Sequence[Any]], list[str]]:
    """Mask sensitive source columns and sensitive keys nested in JSON values.

    ``database`` is the session's selected schema, which applies
    database.table.column patterns to tables named without a database.
    """
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
        sensitive = _sensitive_outputs(
            query, columns, matches, normalized_patterns, database
        )
    except Exception:
        sensitive = _fallback_sensitive_outputs(
            query, columns, matches, normalized_patterns
        )

    masked_indexes = {index for index, flag in enumerate(sensitive) if flag}

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
    # Format the response budget was fitted for; not part of the payload.
    result_format: str | None = field(default=None, compare=False)
    # Rendered text per format. Instances are never mutated after creation,
    # and replace() starts a fresh cache because this field is not an init arg.
    _rendered: dict[str, str] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

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
        rendered = self._rendered.get(result_format)
        if rendered is None:
            rendered = self._rendered[result_format] = self._render(result_format)
        return rendered

    def _render(self, result_format: str) -> str:
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
