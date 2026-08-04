import asyncio
import sys

from mysql_mcp_server.credentials_cli import credentials_main
from mysql_mcp_server.server import main

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "credentials":
        raise SystemExit(credentials_main(sys.argv[2:]))
    asyncio.run(main())
