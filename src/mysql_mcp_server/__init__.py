import asyncio
from importlib.metadata import PackageNotFoundError, version

from . import server

try:
    __version__ = version("mysql-mcp-server")
except PackageNotFoundError:
    # Supports direct source-tree imports before the package is installed.
    __version__ = "0.7.0"


def main():
    """Main entry point for the package."""
    asyncio.run(server.main())


# Expose important items at package level
__all__ = ["__version__", "main", "server"]
