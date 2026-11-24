"""Exercise the installed SDK wire conversion, with all backend I/O mocked."""
import copy
from unittest.mock import patch

import anyio
import httpx
import pytest
from starlette.testclient import TestClient
from test_fork_contracts import ID, entity

import homebox_mcp as hb

HEADERS = {
    "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28", "Host": "localhost:8000",
    "Mcp-Method": "tools/call",
}


def call(client, name, arguments):
    return client.post("/mcp", headers=HEADERS | {"Mcp-Name": name}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": name, "arguments": arguments, "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {"name": "contract-test", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    })


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(hb, "_check_min_version", lambda: None)
    with TestClient(hb.mcp.streamable_http_app(json_response=True, stateless_http=True),
                    base_url="http://localhost:8000") as client:
        yield client


def test_domain_error_and_success_are_distinguished(client, monkeypatch):
    monkeypatch.setattr(hb, "_resolve_exact", lambda _: (None, "ambiguous identifier"))
    response = call(client, "set_item", {"identifier": "fixture", "new_name": "Test"})
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    assert "ambiguous" in response.json()["result"]["content"][0]["text"]


def test_entity_result_is_structured_with_false_zero_and_ids(client, monkeypatch):
    full = entity()
    monkeypatch.setattr(hb, "_get", lambda *args, **kwargs: copy.deepcopy(full))
    response = call(client, "get_entity", {"entity_id": ID})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError")
    data = result["structuredContent"]
    assert data["insured"] is False
    assert data["purchasePrice"] == 0
    assert data["locationId"] == full["locationId"]
    assert data["fields"][0]["id"] == full["fields"][0]["id"]
    assert result["content"][0]["type"] == "text"


def test_unknown_partial_input_is_rejected_without_mutation(client):
    with patch.object(hb, "_put") as put:
        response = call(client, "update_entity", {"entity_id": ID, "changes": {"typo": 1}})
    assert response.json()["result"]["isError"] is True
    put.assert_not_called()


def test_attachment_search_returns_pdf_page_on_the_wire(client, router, monkeypatch):
    service = "http://attachment-search.test/attachment-search"
    monkeypatch.setattr(hb, "HOMEBOX_ATTACHMENT_SEARCH_URL", service)
    hit = {
        "entityId": ID, "entityName": "Drawer", "attachmentId": ID,
        "attachmentTitle": "Receipt", "isLocation": False, "kind": "pdf",
        "page": 2, "match": "text", "snippet": "Invoice 4321",
    }
    expected = {"hits": [hit], "partial": True}
    router.get(service + "/query", params={"q": "4321"}).respond(200, json=expected)
    result = call(client, "search_attachments", {"query": "4321"}).json()["result"]
    assert not result["isError"]
    assert result["structuredContent"] == expected


def test_backend_request_budget_fails_instead_of_partial_success(monkeypatch):
    monkeypatch.setattr(hb, "_check_min_version", lambda: None)
    monkeypatch.setattr(hb, "MAX_BACKEND_REQUESTS", 0)
    with patch.object(hb._client, "request") as request:
        with pytest.raises(hb.ToolError, match="budget"):
            hb.list_tags()
    request.assert_not_called()


def test_trickling_response_is_cancelled_by_absolute_deadline(monkeypatch):
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in [b'["', *([b'x'] * 100), b'"]']:
                await anyio.sleep(0.01)
                yield chunk

    factory = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowStream()))
    monkeypatch.setattr(hb.httpx, "AsyncClient", lambda **kwargs: factory(
        transport=transport, **kwargs))
    monkeypatch.setattr(hb, "_check_min_version", lambda: None)
    monkeypatch.setattr(hb, "OPERATION_TIMEOUT", 0.05)
    with pytest.raises(hb.ToolError, match="deadline"):
        hb.list_tags()


def test_response_budget_fails_instead_of_truncating(monkeypatch):
    monkeypatch.setattr(hb, "_check_min_version", lambda: None)
    monkeypatch.setattr(hb, "MAX_RESPONSE_BYTES", 10)
    monkeypatch.setattr(hb, "_get", lambda *args, **kwargs: [{"name": "x" * 20}])
    with pytest.raises(hb.ToolError, match="response budget"):
        hb.list_tags()
