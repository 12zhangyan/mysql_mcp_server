import asyncio
import sys
from importlib.metadata import PackageNotFoundError, version

from . import server

try:
    __version__ = version("mysql-mcp-server")
except PackageNotFoundError:
    # Supports direct source-tree imports before the package is installed.
    __version__ = "0.8.0"


def main():
    """Main entry point for the package."""
    if len(sys.argv) > 1 and sys.argv[1] == "credentials":
        from .credentials_cli import credentials_main

        raise SystemExit(credentials_main(sys.argv[2:]))
    asyncio.run(server.main())


# Expose important items at package level
__all__ = ["__version__", "main", "server"]
