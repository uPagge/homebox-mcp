"""Private runtime tests: only in-memory SDK tools, no Homebox or network I/O."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import anyio
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY
from starlette.testclient import TestClient

import homebox_mcp_runtime as runtime

EDIT_KEY = "e" * 40
READ_KEY = "r" * 40
HEADERS = {
    "Authorization": f"Bearer {EDIT_KEY}",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-11-25",
    "Origin": "http://localhost",
    "Host": "localhost:8000",
}


@pytest.fixture
def hb():
    server = MCPServer("runtime-test")
    box = SimpleNamespace(mcp=server, calls=[])

    async def inventory(value: str = "") -> dict:
        box.calls.append(value)
        return {"ok": True}

    for name in runtime.READ_TOOLS | runtime.EDIT_TOOLS | {"delete_item"}:
        server.add_tool(inventory, name=name)
    return box


def configured(hb, mode="read-edit", **kwargs):
    anyio.run(runtime.configure_tools, hb, mode)
    config = runtime.HTTPConfig(
        bearer_token=EDIT_KEY, read_token=READ_KEY,
        allowed_origins=("http://localhost",), **kwargs
    )
    return runtime.create_http_app(hb, config=config)


def rpc(client, method="tools/list", params=None, headers=None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    return client.post("/mcp", json=payload, headers=HEADERS | {
        "MCP-Method": method,
    } | ({"MCP-Name": params["name"]} if params and "name" in params else {}) | (headers or {}))


@pytest.mark.parametrize("mode,expected", [
    ("read", runtime.READ_TOOLS),
    ("read-edit", runtime.READ_TOOLS | runtime.EDIT_TOOLS),
    ("legacy", runtime.READ_TOOLS | runtime.EDIT_TOOLS | {"delete_item"}),
])
def test_explicit_startup_policy_and_annotations(hb, mode, expected):
    # Import has not removed dangerous tools.
    assert "delete_item" in {tool.name for tool in anyio.run(hb.mcp.list_tools)}
    anyio.run(runtime.configure_tools, hb, mode)
    tools = anyio.run(hb.mcp.list_tools)
    assert {tool.name for tool in tools} == expected
    for tool in tools:
        if tool.name in runtime.READ_TOOLS | runtime.EDIT_TOOLS:
            assert tool.annotations.read_only_hint == (tool.name in runtime.READ_TOOLS)
            assert tool.annotations.destructive_hint == (tool.name in runtime.EDIT_TOOLS)
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.open_world_hint is False


def test_invalid_or_missing_tool_policy_fails_without_partial_removal(hb):
    with pytest.raises(ValueError):
        anyio.run(runtime.configure_tools, hb, "automatic")
    hb.mcp.remove_tool("update_entity")
    with pytest.raises(RuntimeError, match="missing"):
        anyio.run(runtime.configure_tools, hb)
    assert "delete_item" in {tool.name for tool in anyio.run(hb.mcp.list_tools)}


@pytest.mark.parametrize("key", [None, "", "short", "x" * 31, " " * 40, "é" * 40])
def test_http_rejects_weak_or_missing_key(hb, key):
    with pytest.raises(ValueError, match="credentials"):
        runtime.HTTPConfig(bearer_token=key).validate(hb)


def test_tokens_must_be_separate(hb, monkeypatch):
    monkeypatch.setenv("HOMEBOX_TOKEN", EDIT_KEY)
    with pytest.raises(ValueError, match="HOMEBOX_TOKEN"):
        runtime.HTTPConfig(bearer_token=EDIT_KEY).validate(hb)
    monkeypatch.setenv("HOMEBOX_TOKEN", "other-key" * 5)
    hb.HOMEBOX_TOKEN = EDIT_KEY  # Actual client may retain import-time configuration.
    with pytest.raises(ValueError, match="HOMEBOX_TOKEN"):
        runtime.HTTPConfig(bearer_token=EDIT_KEY).validate(hb)
    del hb.HOMEBOX_TOKEN
    monkeypatch.delenv("HOMEBOX_TOKEN")
    with pytest.raises(ValueError, match="distinct"):
        runtime.HTTPConfig(bearer_token=EDIT_KEY, read_token=EDIT_KEY).validate(hb)


def test_oauth_fails_closed_with_explicit_prerequisite(hb):
    with pytest.raises(ValueError, match="external issuer.*TokenVerifier"):
        runtime.create_http_app(hb, config=runtime.HTTPConfig(
            bearer_token=EDIT_KEY, auth_mode="oauth"
        ))


def test_http_requires_configured_registry(hb):
    with pytest.raises(RuntimeError, match="configure_tools"):
        runtime.create_http_app(hb, config=runtime.HTTPConfig(bearer_token=EDIT_KEY))


def test_auth_every_request_and_scoped_invocation(hb):
    with TestClient(configured(hb), base_url="http://localhost") as client:
        for key in ("", "wrong", EDIT_KEY + "x"):
            response = rpc(client, headers={"Authorization": f"Bearer {key}"})
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
        read_headers = {"Authorization": f"Bearer {READ_KEY}"}
        response = rpc(client, headers=read_headers)
        assert response.status_code == 200, response.text
        assert {tool["name"] for tool in response.json()["result"]["tools"]} == runtime.READ_TOOLS
        # Direct call must be denied even if a client cached edit discovery.
        response = rpc(client, "tools/call", {"name": "set_item", "arguments": {}}, read_headers)
        assert response.status_code == 200, response.text
        assert response.json()["result"]["isError"] is True
        assert hb.calls == []
        assert rpc(client, "tools/call", {"name": "get_entity"}, read_headers).json()[
            "result"
        ]["isError"] is False
        assert rpc(client, "tools/call", {"name": "update_entity"}).json()["result"][
            "isError"
        ] is False
        assert len(hb.calls) == 2
        # Authentication is not an HTTP session: the next request cannot reuse it.
        assert rpc(client, "tools/call", {"name": "get_item"}, {
            "Authorization": ""
        }).status_code == 401


def test_stateless_mcp_does_not_open_a_notification_stream(hb):
    app = configured(hb, tool_timeout_seconds=0.1, request_timeout_seconds=0.5)
    with TestClient(app, base_url="http://localhost", raise_server_exceptions=False) as client:
        headers = HEADERS | {"Accept": "text/event-stream", "Authorization": f"Bearer {READ_KEY}"}
        assert client.get("/mcp", headers=headers | {"Authorization": ""}).status_code == 401
        response = client.get("/mcp", headers=headers)
        assert response.status_code == 405
        assert "POST" in response.headers["allow"]
        assert rpc(client, headers={"Authorization": f"Bearer {READ_KEY}"}).status_code == 200


def test_read_mode_rejects_edit_even_with_edit_credential(hb):
    with TestClient(configured(hb, "read"), base_url="http://localhost") as client:
        result = rpc(client, "tools/call", {"name": "update_entity"}).json()
        assert result["result"]["isError"] is True
        assert hb.calls == []


def test_legacy_read_credential_cannot_invoke_dangerous_tool(hb):
    with TestClient(configured(hb, "legacy"), base_url="http://localhost") as client:
        result = rpc(client, "tools/call", {"name": "delete_item"}, {
            "Authorization": f"Bearer {READ_KEY}"
        }).json()
        assert result["result"]["isError"] is True
        assert hb.calls == []
        assert rpc(client, "tools/call", {"name": "delete_item"}).json()["result"][
            "isError"
        ] is False


def test_current_protocol_scopes_and_invocation(hb):
    meta = {PROTOCOL_VERSION_META_KEY: "2026-07-28", CLIENT_CAPABILITIES_META_KEY: {}}
    headers = {"MCP-Protocol-Version": "2026-07-28"}
    with TestClient(configured(hb), base_url="http://localhost") as client:
        response = rpc(client, params={"_meta": meta}, headers=headers)
        assert response.status_code == 200, response.text
        assert len(response.json()["result"]["tools"]) == 25
        response = rpc(client, "tools/call", {"name": "get_entity", "_meta": meta}, headers)
        assert response.json()["result"]["isError"] is False
        response = rpc(client, "tools/call", {"name": "set_item", "_meta": meta}, headers | {
            "Authorization": f"Bearer {READ_KEY}"
        })
        assert response.json()["result"]["isError"] is True
        assert len(hb.calls) == 1


def test_host_origin_checks(hb):
    with TestClient(configured(hb), base_url="http://localhost") as client:
        assert rpc(client, headers={"Host": "attacker.invalid"}).status_code == 421
        assert rpc(client, headers={"Origin": "https://attacker.invalid"}).status_code == 403
        assert rpc(client).status_code == 200


def test_body_size_and_rate(hb):
    with TestClient(configured(hb, max_body_bytes=100), base_url="http://localhost") as client:
        response = client.post("/mcp", content=b"x" * 101, headers=HEADERS)
        assert response.status_code == 413
        assert hb.calls == []
    with TestClient(configured(hb, requests_per_minute=1), base_url="http://localhost") as client:
        assert rpc(client).status_code == 200
        assert rpc(client).status_code == 429


def test_concurrent_admission_and_release():
    async def check():
        entered, release = anyio.Event(), anyio.Event()

        async def slow_app(scope, receive, send):
            entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        app = runtime._PrivateHTTP(slow_app, runtime.HTTPConfig(
            bearer_token=EDIT_KEY, max_concurrent=1
        ))
        scope = {"type": "http", "headers": [(b"authorization", f"Bearer {EDIT_KEY}".encode())]}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            pass

        async with anyio.create_task_group() as group:
            group.start_soon(app, scope, receive, send)
            await entered.wait()
            responses = []

            async def capture(message):
                responses.append(message)

            await app(scope, receive, capture)
            assert responses[0]["status"] == 503
            assert app.active == 1
            release.set()
        assert app.active == 0
    anyio.run(check)


def test_http_concurrency_with_sdk_tool(hb):
    entered, release = threading.Event(), threading.Event()

    async def slow() -> dict:
        entered.set()
        while not release.is_set():
            await anyio.sleep(0.005)
        return {"ok": True}

    hb.mcp.remove_tool("get_item")
    hb.mcp.add_tool(slow, name="get_item")
    with TestClient(configured(hb, max_concurrent=1), base_url="http://localhost") as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(rpc, client, "tools/call", {"name": "get_item"})
            try:
                assert entered.wait(timeout=2)
                assert rpc(client).status_code == 503
            finally:
                release.set()
            assert pending.result(timeout=2).json()["result"]["isError"] is False
        assert rpc(client).status_code == 200


@pytest.mark.parametrize("scenario,expected", [("chunks", 413), ("slow", 504), ("duplicate", 401)])
def test_http_streaming_input_boundaries(scenario, expected):
    async def check():
        invoked = []

        async def inner(scope, receive, send):
            invoked.append(True)

        app = runtime._PrivateHTTP(inner, runtime.HTTPConfig(
            bearer_token=EDIT_KEY, max_body_bytes=10,
            tool_timeout_seconds=0.001, request_timeout_seconds=0.01,
        ))
        scope = {"type": "http", "headers": [(b"authorization", f"Bearer {EDIT_KEY}".encode())]}
        if scenario == "duplicate":
            scope["headers"].append((b"authorization", f"Bearer {READ_KEY}".encode()))
        chunks = iter([b"x" * 6, b"x" * 5])

        async def receive():
            if scenario == "slow":
                await anyio.sleep(1)
            return {"type": "http.request", "body": next(chunks), "more_body": True}

        responses = []

        async def capture(message):
            responses.append(message)

        await app(scope, receive, capture)
        assert responses[0]["status"] == expected
        assert invoked == []
        assert app.active == 0
    anyio.run(check)


def test_deadline_and_sanitized_audit(hb, caplog):
    async def slow(value: str = "") -> dict:
        await anyio.sleep(1)
        return {"private": value}

    hb.mcp.remove_tool("get_item")
    hb.mcp.add_tool(slow, name="get_item")
    with caplog.at_level(logging.INFO, logger="homebox_mcp.audit"):
        with TestClient(configured(hb, tool_timeout_seconds=0.01),
                        base_url="http://localhost") as client:
            response = rpc(client, "tools/call", {
                "name": "get_item", "arguments": {"value": "PRIVATE_VALUE"}
            })
            assert response.status_code == 200, response.text
            assert response.json()["result"]["isError"] is True
            assert "deadline" in response.text
            rpc(client, "tools/call", {"name": "PRIVATE_NAME"})
    audit = "\n".join(record.getMessage() for record in caplog.records
                      if record.name == "homebox_mcp.audit")
    assert "tool=get_item outcome=timeout duration_ms=" in audit
    assert "tool=unknown outcome=denied" in audit
    assert "PRIVATE" not in audit
    assert EDIT_KEY not in audit
    assert READ_KEY not in audit


def test_error_and_success_audit(hb, caplog):
    async def fail() -> dict:
        raise ToolError("PRIVATE_BACKEND_ERROR")

    hb.mcp.remove_tool("get_item")
    hb.mcp.add_tool(fail, name="get_item")
    with caplog.at_level(logging.INFO):
        with TestClient(configured(hb), base_url="http://localhost") as client:
            rpc(client, "tools/call", {"name": "get_item"})
            rpc(client, "tools/call", {"name": "set_item"})
    audit = "\n".join(record.getMessage() for record in caplog.records
                      if record.name == "homebox_mcp.audit")
    assert "tool=get_item outcome=error" in audit
    assert "tool=set_item outcome=success" in audit
    assert "PRIVATE_BACKEND_ERROR" not in audit
    assert "PRIVATE_BACKEND_ERROR" not in caplog.text


def test_stdio_cli_uses_environment_trust(hb, monkeypatch, caplog):
    monkeypatch.delenv("HOMEBOX_MCP_TOKEN", raising=False)
    hb.mcp.run = Mock()
    for name in ("httpx", "httpx2", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.DEBUG)
    audit_level = logging.getLogger("homebox_mcp.audit").level
    runtime.run(hb, ["--transport", "stdio", "--tool-mode", "read"])
    hb.mcp.run.assert_called_once_with(transport="stdio")
    for name in ("httpx", "httpx2", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING
    assert logging.getLogger("homebox_mcp.audit").level == audit_level
    with caplog.at_level(logging.INFO):
        for name in ("httpx", "httpx2", "httpcore"):
            logging.getLogger(name).info("PRIVATE_QUERY_ENTITY_ID")
            logging.getLogger(name).warning("sanitized operational warning")
        logging.getLogger("homebox_mcp.audit").info("tool=get_item outcome=success duration_ms=1")
    assert "PRIVATE_QUERY_ENTITY_ID" not in caplog.text
    assert "sanitized operational warning" in caplog.text
    assert "tool=get_item outcome=success duration_ms=1" in caplog.text
    assert {tool.name for tool in anyio.run(hb.mcp.list_tools)} == runtime.READ_TOOLS


def test_http_cli_refuses_missing_auth_before_start(hb, monkeypatch):
    monkeypatch.delenv("HOMEBOX_MCP_TOKEN", raising=False)
    hb.mcp.run = Mock()
    with pytest.raises(SystemExit) as error:
        runtime.run(hb, ["--transport", "streamable-http"])
    assert error.value.code == 2
    hb.mcp.run.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"max_body_bytes": 4 * 1024 * 1024 + 1}, {"max_concurrent": 0},
    {"requests_per_minute": -1}, {"tool_timeout_seconds": float("nan")},
    {"request_timeout_seconds": 1}, {"allowed_hosts": ()},
])
def test_invalid_bounds(hb, changes):
    with pytest.raises(ValueError):
        replace(runtime.HTTPConfig(bearer_token=EDIT_KEY), **changes).validate(hb)
