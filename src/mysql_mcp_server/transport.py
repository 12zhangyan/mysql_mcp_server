"""HTTP/stdio transport startup, exposure checks and bearer authentication."""

import hmac
import logging
import math
import os

from mcp.server import Server

logger = logging.getLogger("mysql_mcp_server")


def _server_port(primary_env: str) -> int:
    """Read an HTTP server port with a shared PORT fallback and clear errors."""
    primary_value = os.getenv(primary_env)
    fallback_value = os.getenv("PORT")
    value = primary_value or fallback_value or "8000"
    source = primary_env if primary_value else "PORT" if fallback_value else primary_env
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError(f"{source} must be an integer between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{source} must be between 1 and 65535")
    return port


def _positive_seconds_env(name: str, default: float) -> float:
    """Read a positive, finite duration from an environment variable."""
    value = os.getenv(name, str(default))
    try:
        seconds = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return seconds


def _default_allowed_hosts(host: str, port: int) -> list[str]:
    """Build exact safe Host header values, including bracketed IPv6 forms."""
    allowed = [f"localhost:{port}", f"127.0.0.1:{port}"]
    if host in {"0.0.0.0", "::"}:
        return allowed
    authority = f"[{host}]" if ":" in host else host
    host_header = f"{authority}:{port}"
    if host_header not in allowed:
        allowed.append(host_header)
    return allowed


async def run_stdio(app: Server) -> None:
    """Runs the server using standard input/output streams."""
    from mcp.server.stdio import stdio_server

    logger.info("Starting MySQL MCP server (STDIO)...")
    async with stdio_server() as (read_stream, write_stream):
        try:
            await app.run(
                read_stream, write_stream, app.create_initialization_options()
            )
        except Exception as e:
            logger.error(f"Server error: {str(e)}", exc_info=True)
            raise


def _validate_exposure(
    host: str,
    bearer_token: str | None,
    trust_proxy_auth: bool,
    *,
    env_prefix: str,
    label: str,
) -> None:
    """Fail closed when an HTTP transport would be exposed without authentication."""
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    public_bind = host not in loopback_hosts
    if bearer_token is not None and len(bearer_token) < 32:
        raise ValueError(f"{env_prefix}_BEARER_TOKEN must be at least 32 characters")
    if public_bind and not bearer_token and not trust_proxy_auth:
        raise ValueError(
            f"Refusing unauthenticated public {label} bind. Configure "
            f"{env_prefix}_BEARER_TOKEN, bind to 127.0.0.1, or explicitly set "
            f"{env_prefix}_TRUST_PROXY_AUTH=true when an authenticated reverse proxy "
            "is the only network entry point."
        )


def _validate_sse_exposure(
    host: str, bearer_token: str | None, trust_proxy_auth: bool
) -> None:
    _validate_exposure(
        host, bearer_token, trust_proxy_auth, env_prefix="MCP_SSE", label="SSE"
    )


def _validate_http_exposure(
    host: str, bearer_token: str | None, trust_proxy_auth: bool
) -> None:
    _validate_exposure(
        host,
        bearer_token,
        trust_proxy_auth,
        env_prefix="MCP_HTTP",
        label="Streamable HTTP",
    )


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _allowed_hosts(env_name: str, host: str, port: int) -> list[str]:
    """Read a comma-separated Host allowlist, defaulting to the bind address."""
    configured = os.getenv(env_name, "")
    if configured:
        return [value.strip() for value in configured.split(",") if value.strip()]
    return _default_allowed_hosts(host, port)


async def run_sse(app: Server) -> None:
    """
    Runs the server using Server-Sent Events (SSE) over HTTP.
    Requires 'starlette' and 'uvicorn' dependencies.
    """
    try:
        import uvicorn
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.responses import Response
        from starlette.routing import Mount, Route
    except ImportError:
        logger.error(
            "SSE transport requires additional dependencies. Install with: pip install mysql_mcp_server[sse]"
        )
        raise

    logger.info("Starting MySQL MCP server (SSE)...")

    host = os.getenv("MCP_SSE_HOST", "127.0.0.1")
    port = _server_port("MCP_SSE_PORT")
    bearer_token = os.getenv("MCP_SSE_BEARER_TOKEN") or None
    trust_proxy_auth = _env_flag("MCP_SSE_TRUST_PROXY_AUTH")
    _validate_sse_exposure(host, bearer_token, trust_proxy_auth)

    # Build security settings with DNS rebinding protection.
    # allowed_hosts controls which Host header values are accepted; without this
    # a DNS rebinding attack can relay requests through the victim's browser.
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        allowed_hosts = _allowed_hosts("MCP_SSE_ALLOWED_HOSTS", host, port)
        logger.info(
            "SSE DNS rebinding protection enabled. Allowed hosts: %s. "
            "Override with MCP_SSE_ALLOWED_HOSTS (comma-separated).",
            ", ".join(allowed_hosts),
        )
        security_settings = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
        )
    except ImportError:
        logger.warning(
            "mcp.server.transport_security not available (upgrade mcp>=1.9.0 for DNS rebinding protection). "
            "Running without Origin/Host validation."
        )
        security_settings = None

    sse = (
        SseServerTransport("/messages/", security_settings=security_settings)
        if security_settings is not None
        else SseServerTransport("/messages/")
    )

    async def handle_sse(request):
        """Handler for the SSE connection endpoint."""
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await app.run(streams[0], streams[1], app.create_initialization_options())
        return Response()

    async def health_check(request):
        """Simple health check endpoint."""
        return Response("MySQL MCP Server is running", media_type="text/plain")

    # Define the Starlette application with SSE routes and a health check.
    starlette_app = Starlette(
        routes=[
            Route("/", endpoint=health_check),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ]
    )

    secured_app = _with_bearer_auth(starlette_app, bearer_token, Response)

    # Configure and start the Uvicorn server.
    server_config = uvicorn.Config(secured_app, host=host, port=port, log_level="info")
    server = uvicorn.Server(server_config)
    await server.serve()


async def run_streamable_http(app: Server) -> None:
    """Run the MCP server over the standard Streamable HTTP transport."""
    try:
        import uvicorn
        from mcp.server.streamable_http_manager import \
            StreamableHTTPSessionManager
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.applications import Starlette
        from starlette.responses import Response
        from starlette.routing import Route
    except ImportError:
        logger.error(
            "Streamable HTTP requires additional dependencies. "
            "Install with: pip install mysql_mcp_server[http]"
        )
        raise

    host = os.getenv("MCP_HTTP_HOST", "127.0.0.1")
    port = _server_port("MCP_HTTP_PORT")
    path = os.getenv("MCP_HTTP_PATH", "/mcp").strip()
    bearer_token = os.getenv("MCP_HTTP_BEARER_TOKEN") or None
    trust_proxy_auth = _env_flag("MCP_HTTP_TRUST_PROXY_AUTH")
    if not path.startswith("/") or path == "/":
        raise ValueError("MCP_HTTP_PATH must start with '/' and cannot be '/'")
    _validate_http_exposure(host, bearer_token, trust_proxy_auth)

    security_settings = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts("MCP_HTTP_ALLOWED_HOSTS", host, port),
    )
    session_manager = StreamableHTTPSessionManager(
        app=app,
        security_settings=security_settings,
        session_idle_timeout=_positive_seconds_env(
            "MCP_HTTP_SESSION_IDLE_TIMEOUT_SECONDS", 1800
        ),
    )

    class StreamableHTTPASGIApp:
        async def __call__(self, scope, receive, send):
            await session_manager.handle_request(scope, receive, send)

    async def health_check(request):
        return Response("MySQL MCP Server is running", media_type="text/plain")

    starlette_app = Starlette(
        routes=[
            Route("/", endpoint=health_check),
            Route(path, endpoint=StreamableHTTPASGIApp()),
        ],
        lifespan=lambda _: session_manager.run(),
    )
    secured_app = _with_bearer_auth(starlette_app, bearer_token, Response)

    logger.info(
        "Starting MySQL MCP server (Streamable HTTP) at http://%s:%s%s",
        host,
        port,
        path,
    )
    server_config = uvicorn.Config(secured_app, host=host, port=port, log_level="info")
    server = uvicorn.Server(server_config)
    await server.serve()


def _with_bearer_auth(asgi_app, bearer_token: str | None, response_type):
    """Protect every non-health HTTP route with an optional static bearer token."""
    if not bearer_token:
        return asgi_app

    class BearerAuthMiddleware:
        def __init__(self, wrapped_app, token: str):
            self.wrapped_app = wrapped_app
            self.expected = f"Bearer {token}".encode("utf-8")

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope.get("path") != "/":
                headers = dict(scope.get("headers", []))
                # Compare raw bytes: str comparison rejects non-ASCII input
                # with TypeError, which would surface as a 500, not a 401.
                provided = headers.get(b"authorization", b"")
                if not hmac.compare_digest(provided, self.expected):
                    response = response_type(
                        "Unauthorized",
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                    await response(scope, receive, send)
                    return
            await self.wrapped_app(scope, receive, send)

    return BearerAuthMiddleware(asgi_app, bearer_token)

