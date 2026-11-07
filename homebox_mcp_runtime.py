"""Startup-only tool policy and authenticated, bounded private HTTP transport.

Bearer credentials identify capabilities of one owner, not separate Homebox users.
TLS and a trusted reverse proxy are prerequisites outside loopback. OAuth verifies
external issuer JWTs with PyJWT and official SDK resource-server middleware; this
module does not implement an issuer. No tool registry changes occur on import.
"""

from __future__ import annotations

import argparse
import contextvars
import logging
import math
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import anyio
from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
from mcp.server.auth.routes import build_resource_metadata_url, create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse

from homebox_mcp_oauth import EDIT_SCOPE, SCOPES, PublicKeyTokenVerifier, validate_oauth_urls

READ_TOOLS = frozenset({
    "search_items", "get_item", "list_locations", "location_contents",
    "list_tags", "warranties_expiring", "inventory_stats", "list_custom_fields",
    "list_attachments", "field_index", "list_maintenance", "get_entity", "list_entities",
})
EDIT_TOOLS = frozenset({
    "set_item", "move_item", "set_warranty", "set_identity", "set_fields",
    "set_tags", "set_tag", "set_location", "mark_sold", "set_maintenance",
    "rename_attachment", "update_entity",
})
MODES = ("read", "read-edit", "legacy")
# None is the normal stdio environment/process trust boundary. HTTP always sets it.
_capability: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "homebox_mcp_capability", default=None
)
_audit = logging.getLogger("homebox_mcp.audit")


class _SDKPrivacyFilter(logging.Filter):
    def filter(self, record):
        # SDK errors can contain inventory identifiers/values in exception text.
        # Keep level/logger/time for operations, never their original payload.
        record.msg = "MCP SDK event; private details suppressed (see safe tool audit)"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


def _configure_private_logging():
    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("mcp.server.mcpserver.server", "mcp.server.lowlevel.server"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, _SDKPrivacyFilter) for f in logger.filters):
            logger.addFilter(_SDKPrivacyFilter())


def _execution_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


class _ToolPolicy:
    def __init__(self, allowed: frozenset[str], deadline: float = 60.0):
        self.allowed = allowed
        self.deadline = deadline

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        if ctx.method == "tools/list":
            result = await call_next(ctx)
            if _capability.get() == "read":
                if isinstance(result, dict):
                    result = {**result, "tools": [
                        tool for tool in result.get("tools", [])
                        if tool["name"] in READ_TOOLS
                    ]}
                else:
                    result = result.model_copy(update={
                        "tools": [tool for tool in result.tools if tool.name in READ_TOOLS]
                    })
            return result
        if ctx.method != "tools/call":
            return await call_next(ctx)
        name = (ctx.params or {}).get("name")
        # Never log caller-controlled unknown tool names, arguments or exception text.
        safe_name = name if isinstance(name, str) and name in self.allowed else "unknown"
        started = time.monotonic()
        outcome = "error"
        try:
            if safe_name == "unknown" or (
                _capability.get() == "read" and name not in READ_TOOLS
            ):
                outcome = "denied"
                return _execution_error("Tool is not permitted for this credential or mode")
            try:
                with anyio.fail_after(self.deadline):
                    result = await call_next(ctx)
            except TimeoutError:
                outcome = "timeout"
                return _execution_error(
                    "Tool deadline exceeded; a write may have completed. Read back before retrying."
                )
            is_error = result.get("isError", False) if isinstance(result, dict) else getattr(
                result, "is_error", False
            )
            outcome = "error" if is_error else "success"
            return result
        finally:
            _audit.info("tool=%s outcome=%s duration_ms=%d", safe_name, outcome,
                        (time.monotonic() - started) * 1000)


async def configure_tools(hb: Any, mode: str = "read-edit") -> None:
    """Apply an explicit startup policy; call before serving, never during requests.

    Legacy is an explicit opt-in to *all* registered upstream tools, including
    create/delete, URL/file and export operations, with their original annotations.
    Reconfiguration can narrow but cannot restore already removed tools.
    """
    if mode not in MODES:
        raise ValueError("tool mode must be read, read-edit or legacy")
    tools = await hb.mcp.list_tools()
    found = frozenset(tool.name for tool in tools)
    allowed = found if mode == "legacy" else READ_TOOLS | (
        EDIT_TOOLS if mode == "read-edit" else frozenset()
    )
    if not allowed <= found:
        raise RuntimeError("Required inventory tools are missing; check fork/SDK compatibility")
    for tool in tools:
        if tool.name not in allowed:
            hb.mcp.remove_tool(tool.name)
        elif tool.name in READ_TOOLS | EDIT_TOOLS:
            # SDK list_tools returns wire-model copies. This localized SDK access
            # updates registered annotations without rebuilding schemas/functions.
            registered = hb.mcp._tool_manager.get_tool(tool.name)
            registered.annotations = ToolAnnotations(
                read_only_hint=tool.name in READ_TOOLS,
                destructive_hint=tool.name in EDIT_TOOLS,
                idempotent_hint=True,
                open_world_hint=False,
            )
    # SDK 2.x request middleware covers actual calls, not merely discovery.
    previous = [mw for mw in hb.mcp.middleware if isinstance(mw, _ToolPolicy)]
    for middleware in previous:
        hb.mcp.middleware.remove(middleware)
    hb.mcp.middleware.append(_ToolPolicy(allowed))


@dataclass(frozen=True)
class HTTPConfig:
    """Private single-owner access; tokens belong in environment, never CLI args.

    Limits are per process (not shared across multiple workers). Rate is a global
    admission bucket, intentionally bounding memory without an attacker-keyed map.
    """

    bearer_token: str = field(default="", repr=False)
    read_token: str | None = field(default=None, repr=False)
    host: str = "127.0.0.1"
    port: int = 8000
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:*", "localhost:*")
    allowed_origins: tuple[str, ...] = ()
    max_body_bytes: int = 1024 * 1024
    max_concurrent: int = 4
    requests_per_minute: int = 60
    tool_timeout_seconds: float = 60.0
    request_timeout_seconds: float = 65.0
    auth_mode: str = "bearer"
    oauth_issuer_url: str | None = None
    oauth_resource_url: str | None = None
    oauth_public_key_file: str | None = field(default=None, repr=False)

    def validate(self, hb: Any) -> None:
        if self.auth_mode not in ("bearer", "oauth"):
            raise ValueError("HTTP auth mode must be bearer or oauth")
        if self.auth_mode == "oauth" and not all((
            self.oauth_issuer_url, self.oauth_resource_url, self.oauth_public_key_file
        )):
            raise ValueError(
                "OAuth requires an external issuer, audience-validating SDK TokenVerifier "
                "and protected-resource metadata integration; not configured. "
                "Use private bearer access, not an unauthenticated endpoint."
            )
        if self.auth_mode == "oauth":
            validate_oauth_urls(self.oauth_issuer_url, self.oauth_resource_url)
        backend_tokens = (os.environ.get("HOMEBOX_TOKEN"), getattr(hb, "HOMEBOX_TOKEN", None))
        credentials = [self.bearer_token] if self.auth_mode == "bearer" else []
        if self.read_token is not None and self.auth_mode == "bearer":
            credentials.append(self.read_token)
        for token in credentials:
            if not isinstance(token, str) or len(token) < 32 or not token.isascii() or any(
                char.isspace() for char in token
            ):
                raise ValueError("HTTP MCP credentials must be distinct ASCII keys of >=32 characters")
            if any(backend_token and token == backend_token for backend_token in backend_tokens):
                raise ValueError("MCP credentials must not reuse HOMEBOX_TOKEN")
        if self.auth_mode == "bearer" and self.read_token and secrets.compare_digest(
            self.bearer_token, self.read_token
        ):
            raise ValueError("Read and edit MCP credentials must be distinct")
        for value in (self.max_body_bytes, self.max_concurrent, self.requests_per_minute):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("HTTP bounds must be positive integers")
        if self.max_body_bytes > 4 * 1024 * 1024:
            raise ValueError("HTTP body bound must not exceed the SDK 4 MiB default")
        for value in (self.tool_timeout_seconds, self.request_timeout_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("HTTP deadlines must be finite and positive")
        if self.request_timeout_seconds <= self.tool_timeout_seconds:
            raise ValueError("HTTP request deadline must exceed the tool deadline")
        if not self.allowed_hosts:
            raise ValueError("Explicit allowed HTTP hosts are required")


class _PrivateHTTP:
    def __init__(self, app: Any, config: HTTPConfig,
                 oauth_verifier: PublicKeyTokenVerifier | None = None):
        self.app = app
        self.config = config
        self.oauth_verifier = oauth_verifier
        self.metadata_path = None
        self.oauth_reject = None
        if oauth_verifier is not None:
            metadata_url = build_resource_metadata_url(AuthSettings(
                issuer_url=config.oauth_issuer_url, resource_server_url=config.oauth_resource_url,
                validate_token_resource=True,
            ).resource_server_url)
            self.metadata_path = metadata_url.path
            # Reuse the SDK's challenge generation for malformed/duplicate/invalid
            # credentials too. A cleared user guarantees this guard never calls app.
            self.oauth_reject = RequireAuthMiddleware(app, [], resource_metadata_url=metadata_url)
        self.active = 0
        self.tokens = float(config.requests_per_minute)
        self.refilled = time.monotonic()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        public_metadata = bool(self.metadata_path and scope.get("path") == self.metadata_path)
        # Admission precedes JWT verification: invalid credentials must not get
        # an unbounded RSA verification budget. This is intentionally one global
        # process bucket, including unauthenticated attempts (no attacker-key map).
        now = time.monotonic()
        self.tokens = min(float(self.config.requests_per_minute), self.tokens + (
            now - self.refilled
        ) * self.config.requests_per_minute / 60)
        self.refilled = now
        if self.tokens < 1:
            await JSONResponse({"error": "Rate limit exceeded"}, 429,
                               headers={"Retry-After": "60"})(scope, receive, send)
            return
        self.tokens -= 1
        headers = {}
        for key, value in scope.get("headers", []):
            # Duplicate Authorization is ambiguous: fail closed.
            if key.lower() == b"authorization" and key.lower() in headers:
                if self.oauth_reject:
                    await self.oauth_reject({**scope, "user": None}, receive, send)
                else:
                    await JSONResponse({"error": "Invalid authorization"}, 401)(scope, receive, send)
                return
            headers[key.lower()] = value
        auth = headers.get(b"authorization", b"")
        capability = None
        if public_metadata:
            # Public discovery shares admission/body/deadline bounds but should
            # not cause RSA work merely because the caller supplied a token.
            scope = {**scope, "headers": [(key, value) for key, value in scope.get("headers", [])
                                          if key.lower() != b"authorization"]}
        elif self.oauth_verifier:
            try:
                scheme, token = auth.decode("ascii").split(" ", 1)
                access = await self.oauth_verifier.verify_token(token) if (
                    scheme.lower() == "bearer"
                ) else None
            except (UnicodeDecodeError, ValueError):
                access = None
            if access:
                capability = "edit" if EDIT_SCOPE in access.scopes else "read"
        else:
            for token, role in ((self.config.bearer_token, "edit"), (self.config.read_token, "read")):
                if token and secrets.compare_digest(auth, b"Bearer " + token.encode("ascii")):
                    capability = role
        if capability is None and not public_metadata:
            if self.oauth_reject:
                await self.oauth_reject({**scope, "user": None}, receive, send)
            else:
                await JSONResponse({"error": "MCP bearer authentication required"}, 401,
                                   headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            return
        if self.active >= self.config.max_concurrent:
            await JSONResponse({"error": "HTTP concurrency limit exceeded"}, 503,
                               headers={"Retry-After": "1"})(scope, receive, send)
            return
        self.active += 1
        context_token = _capability.set(capability)
        started = False

        async def tracked_send(message: Any) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            with anyio.fail_after(self.config.request_timeout_seconds):
                # Bound actual bytes even if Content-Length is absent or dishonest.
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.config.max_body_bytes:
                        await JSONResponse({"error": "Request body too large"}, 413)(
                            scope, receive, tracked_send
                        )
                        return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
                delivered = False

                async def replay() -> Any:
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                await self.app(scope, replay, tracked_send)
        except TimeoutError:
            if not started:
                await JSONResponse({"error": "HTTP deadline exceeded; verify writes before retry"},
                                   504)(scope, receive, send)
        finally:
            _capability.reset(context_token)
            self.active -= 1


def create_http_app(hb: Any, *, config: HTTPConfig) -> Any:
    """Build official SDK stateless HTTP after configure_tools; fail closed on auth.

    Awaited operations are cancellable. Blocking sync tool I/O cannot be forcibly
    stopped here: backend timeouts/request budgets remain necessary. No retries.
    """
    config.validate(hb)
    _configure_private_logging()
    policies = [mw for mw in hb.mcp.middleware if isinstance(mw, _ToolPolicy)]
    if len(policies) != 1:
        raise RuntimeError("Call configure_tools before creating the HTTP app")
    policies[0].deadline = config.tool_timeout_seconds
    verifier = None
    if config.auth_mode == "oauth":
        if hb.mcp._auth_server_provider is not None:
            raise ValueError("OAuth resource mode must not configure an authorization-server provider")
        verifier = PublicKeyTokenVerifier(
            issuer_url=config.oauth_issuer_url,
            resource_url=config.oauth_resource_url,
            public_key_file=config.oauth_public_key_file,
            forbidden_tokens=(os.environ.get("HOMEBOX_TOKEN"), getattr(hb, "HOMEBOX_TOKEN", None)),
        )
        hb.mcp.settings.auth = AuthSettings(
            issuer_url=config.oauth_issuer_url,
            resource_server_url=config.oauth_resource_url,
            required_scopes=None,  # Verifier requires read OR edit, not both.
            validate_token_resource=True,
        )
        # SDK 2.x has no public post-construction verifier setter. Keep this
        # startup-only assignment localized; SDK auth still validates every HTTP
        # request independently of our capability gate (no token/session cache).
        hb.mcp._token_verifier = verifier
    elif hb.mcp.settings.auth is not None:
        raise ValueError("Bearer mode requires a server without preconfigured SDK OAuth")
    app = hb.mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        max_request_body_size=config.max_body_bytes,
        host=config.host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(config.allowed_hosts),
            allowed_origins=list(config.allowed_origins),
        ),
    )
    if verifier:
        # SDK uses required_scopes for metadata too, which cannot express read OR
        # edit. Replace only its public metadata route with the official factory
        # advertising both supported scopes, without adding any issuer routes.
        metadata_routes = create_protected_resource_routes(
            resource_url=hb.mcp.settings.auth.resource_server_url,
            authorization_servers=[hb.mcp.settings.auth.issuer_url],
            scopes_supported=SCOPES,
        )
        paths = {route.path for route in metadata_routes}
        app.routes[:] = [route for route in app.routes if getattr(route, "path", None) not in paths]
        app.routes.extend(metadata_routes)
    return _PrivateHTTP(app, config, oauth_verifier=verifier)


def run(hb: Any, argv: list[str] | None = None) -> None:
    """CLI entry point used by homebox_mcp.main()."""
    parser = argparse.ArgumentParser(description="Bounded Homebox MCP server")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--tool-mode", choices=MODES, default="read-edit")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--auth-mode", choices=("bearer", "oauth"), default="bearer")
    args = parser.parse_args(argv)
    # HTTP clients log full request URLs (including search queries/entity IDs) at
    # INFO. Keep operational warnings, never enable wire/debug logging here.
    _configure_private_logging()
    config = HTTPConfig(
        bearer_token=os.environ.get("HOMEBOX_MCP_TOKEN", ""),
        read_token=os.environ.get("HOMEBOX_MCP_READ_TOKEN"),
        host=args.host, port=args.port, auth_mode=args.auth_mode,
        oauth_issuer_url=os.environ.get("HOMEBOX_MCP_OAUTH_ISSUER"),
        oauth_resource_url=os.environ.get("HOMEBOX_MCP_OAUTH_RESOURCE"),
        oauth_public_key_file=os.environ.get("HOMEBOX_MCP_OAUTH_PUBLIC_KEY_FILE"),
        allowed_hosts=tuple(os.environ.get(
            "HOMEBOX_MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*"
        ).split(",")),
        allowed_origins=tuple(filter(None, os.environ.get(
            "HOMEBOX_MCP_ALLOWED_ORIGINS", ""
        ).split(","))),
    )
    if args.transport == "streamable-http":
        try:
            config.validate(hb)
        except ValueError as exc:
            parser.error(str(exc))
    anyio.run(configure_tools, hb, args.tool_mode)
    if args.transport == "stdio":
        hb.mcp.run(transport="stdio")
    else:
        import uvicorn

        uvicorn.run(create_http_app(hb, config=config), host=args.host, port=args.port,
                    access_log=False)
