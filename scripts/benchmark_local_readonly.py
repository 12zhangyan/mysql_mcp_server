"""Opt-in localhost-only benchmark using synthetic SELECT/CTE rows.

Set MYSQL_MCP_LOCAL_TESTS=1 and supply MYSQL_USER/MYSQL_PASSWORD in the process
only. Output contains timings and dependency versions, never credentials or rows.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from importlib.metadata import version


def configure_local(mask_sensitive_field=False):
    if os.getenv("MYSQL_MCP_LOCAL_TESTS") != "1":
        raise ValueError("Set MYSQL_MCP_LOCAL_TESTS=1 to opt in to localhost queries")
    credentials = {name: os.getenv(name) for name in ("MYSQL_USER", "MYSQL_PASSWORD")}
    if not all(credentials.values()):
        raise ValueError(
            "Supply MYSQL_USER and MYSQL_PASSWORD in the process environment"
        )
    # Ignore inherited production profiles, SSH settings and credential providers.
    for name in list(os.environ):
        if name.startswith("MYSQL_"):
            del os.environ[name]
    os.environ.update(credentials)
    os.environ.update(
        MYSQL_HOST="127.0.0.1",
        MYSQL_PORT="3306",
        MYSQL_SSL_MODE="DISABLED",
        MYSQL_SSH_ENABLE="false",
        MYSQL_POOL_SIZE="0",
        MYSQL_AUDIT_ENABLED="false",
    )

    if mask_sensitive_field:
        os.environ["MYSQL_MASK_COLUMNS"] = "password"


def synthetic_case(width):
    columns = ["harmless", *(f"value_{index}" for index in range(1, width))]
    source = "'synthetic-sensitive-value' AS password," + ",".join(
        f"{index} AS value_{index}" for index in range(1, width)
    )
    projection = "password AS harmless," + ",".join(columns[1:])
    query = f"WITH fixture AS (SELECT {source}) SELECT {projection} FROM fixture"
    return query, columns, [["synthetic-sensitive-value", *range(1, width)]]


async def measure(widths, iterations):
    from mysql_mcp_server.config import load_connection_registry
    from mysql_mcp_server.results import MASKED_VALUE, mask_result_rows
    from mysql_mcp_server.runtime import close_runtime_resources
    from mysql_mcp_server.server import execute_query

    patterns = load_connection_registry().get().mask_columns
    samples = []
    try:
        probe = await execute_query(
            "SELECT VERSION(), @@transaction_read_only, @@autocommit",
            result_format="json",
        )
        mysql_version, read_only, autocommit = probe.rows[0]
        if (read_only, autocommit) != (1, 0):
            raise RuntimeError("Read-only transaction preflight failed")
        for width in widths:
            query, columns, rows = synthetic_case(width)
            mask_ms, query_ms = [], []
            for _ in range(iterations):
                start = time.perf_counter()
                masked, _ = mask_result_rows(query, columns, rows, patterns)
                mask_ms.append((time.perf_counter() - start) * 1000)
                expected = [[MASKED_VALUE, *range(1, width)]] if patterns else rows
                if masked != expected:
                    raise RuntimeError("Masking verification failed")
                start = time.perf_counter()
                result = await execute_query(query, result_format="json")
                query_ms.append((time.perf_counter() - start) * 1000)
                if result.rows != expected:
                    raise RuntimeError("Query verification failed")
            samples.append(
                {
                    "columns": width,
                    "iterations": iterations,
                    "mask_median_ms": round(statistics.median(mask_ms), 3),
                    "query_median_ms": round(statistics.median(query_ms), 3),
                }
            )
        return {
            "mysql": mysql_version,
            "sqlglot": version("sqlglot"),
            "python": sys.version.split()[0],
            "transaction_read_only": read_only,
            "autocommit": autocommit,
            "mask_columns": list(patterns),
            "samples": samples,
        }
    finally:
        close_runtime_resources()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=3, choices=range(1, 11))
    parser.add_argument("--columns", type=int, nargs="+", default=[25, 100, 200])
    parser.add_argument(
        "--mask-sensitive-field",
        action="store_true",
        help="Explicitly mask the synthetic password field; defaults to unmasked",
    )
    args = parser.parse_args()
    if not all(2 <= width <= 200 for width in args.columns):
        parser.error("--columns values must be between 2 and 200")
    try:
        configure_local(args.mask_sensitive_field)
        report = asyncio.run(measure(args.columns, args.iterations))
    except Exception as exc:
        # Connector and parser exception messages can include connection or SQL data.
        print(json.dumps({"error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
