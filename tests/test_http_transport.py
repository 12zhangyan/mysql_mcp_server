from unittest.mock import AsyncMock, patch

import pytest

from mysql_mcp_server.server import (
    _validate_http_exposure,
    _with_bearer_auth,
    main,
)


def test_streamable_http_rejects_unauthenticated_public_bind():
    with pytest.raises(ValueError, match="unauthenticated public Streamable HTTP"):
        _validate_http_exposure("0.0.0.0", None, False)


def test_streamable_http_accepts_loopback_without_authentication():
    _validate_http_exposure("127.0.0.1", None, False)


def test_streamable_http_rejects_short_bearer_token():
    with pytest.raises(ValueError, match="at least 32 characters"):
        _validate_http_exposure("127.0.0.1", "short", False)


@pytest.mark.asyncio
async def test_main_accepts_streamable_http_alias(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "streamable_http")
    with (
        patch(
            "mysql_mcp_server.server._run_streamable_http_server",
            new=AsyncMock(),
        ) as run_http,
        patch("mysql_mcp_server.server.close_runtime_resources"),
    ):
        await main()

    run_http.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_bearer_auth_protects_mcp_route_but_not_health_route():
    calls = []

    async def downstream(scope, receive, send):
        calls.append(scope["path"])

    class FakeResponse:
        def __init__(self, body, status_code, headers):
            self.status_code = status_code
            self.headers = headers

        async def __call__(self, scope, receive, send):
            calls.append(self.status_code)

    middleware = _with_bearer_auth(downstream, "x" * 32, FakeResponse)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        return None

    await middleware(
        {"type": "http", "path": "/", "headers": []}, receive, send
    )
    await middleware(
        {"type": "http", "path": "/mcp", "headers": []}, receive, send
    )
    await middleware(
        {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"authorization", f"Bearer {'x' * 32}".encode())],
        },
        receive,
        send,
    )

    assert calls == ["/", 401, "/mcp"]
