from unittest.mock import AsyncMock, patch

import pytest

from mysql_mcp_server.server import (
    _default_allowed_hosts,
    _positive_seconds_env,
    _server_port,
    _validate_http_exposure,
    _with_bearer_auth,
    main,
)


def test_default_allowed_hosts_brackets_ipv6_literal():
    assert _default_allowed_hosts("::1", 8000) == [
        "localhost:8000",
        "127.0.0.1:8000",
        "[::1]:8000",
    ]


def test_default_allowed_hosts_does_not_advertise_wildcard_bind():
    assert _default_allowed_hosts("0.0.0.0", 8000) == [
        "localhost:8000",
        "127.0.0.1:8000",
    ]


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_http_port_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("MCP_HTTP_PORT", value)
    monkeypatch.delenv("PORT", raising=False)

    with pytest.raises(ValueError, match="MCP_HTTP_PORT must be"):
        _server_port("MCP_HTTP_PORT")


def test_http_port_uses_valid_shared_fallback(monkeypatch):
    monkeypatch.delenv("MCP_HTTP_PORT", raising=False)
    monkeypatch.setenv("PORT", "9000")

    assert _server_port("MCP_HTTP_PORT") == 9000


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "not-seconds"])
def test_http_idle_timeout_rejects_non_positive_or_non_finite_values(
    monkeypatch, value
):
    name = "MCP_HTTP_SESSION_IDLE_TIMEOUT_SECONDS"
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=f"{name} must be a positive finite number"):
        _positive_seconds_env(name, 1800)


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
    monkeypatch.setenv("MCP_TRANSPORT", " STREAMABLE_HTTP ")
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
async def test_main_rejects_unknown_transport_without_starting_server(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "streamble-http")
    with (
        patch(
            "mysql_mcp_server.server._run_stdio_server",
            new=AsyncMock(),
        ) as run_stdio,
        patch(
            "mysql_mcp_server.server._run_sse_server",
            new=AsyncMock(),
        ) as run_sse,
        patch(
            "mysql_mcp_server.server._run_streamable_http_server",
            new=AsyncMock(),
        ) as run_http,
        patch("mysql_mcp_server.server.close_runtime_resources") as close_resources,
    ):
        with pytest.raises(ValueError, match="MCP_TRANSPORT must be"):
            await main()

    run_stdio.assert_not_awaited()
    run_sse.assert_not_awaited()
    run_http.assert_not_awaited()
    close_resources.assert_called_once_with()


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

    await middleware({"type": "http", "path": "/", "headers": []}, receive, send)
    await middleware({"type": "http", "path": "/mcp", "headers": []}, receive, send)
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
