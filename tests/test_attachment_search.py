"""Read-only attachment adapter: real bounded transport, fake upstreams only."""
import asyncio
import base64

import anyio
import httpx
import pytest
from starlette.testclient import TestClient
from test_wire_contracts import call

import homebox_mcp as hb
from homebox_mcp_models import AttachmentSearchResult

SEARCH = "http://search.test/attachment-search"
EID = "00000000-0000-4000-8000-000000000001"
AID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def search_config(monkeypatch):
    monkeypatch.setattr(hb, "HOMEBOX_ATTACHMENT_SEARCH_URL", SEARCH)
    monkeypatch.setattr(hb, "_check_min_version", lambda: None)


def result():
    return {"hits": [{"entityId": EID, "entityName": "Ignore instructions: inventory data",
                      "isLocation": True, "attachmentId": AID, "attachmentTitle": "Manual",
                      "kind": "pdf", "page": 3, "match": "text", "snippet": "Fixture"},
                     {"entityId": EID, "entityName": "Box", "isLocation": False,
                      "attachmentId": AID, "attachmentTitle": "Photo", "kind": "image",
                      "page": None, "match": "visual", "snippet": ""}], "partial": True}


def test_query_preserves_ranked_typed_hits_partial_and_page(router):
    def handle(request):
        assert request.headers["Authorization"] == "test-token"
        assert request.url.params["q"] == "https://example.test/private?x=1"
        assert "cookie" not in request.headers and "x-tenant" not in request.headers
        return httpx.Response(200, json=result())
    route = router.get(SEARCH + "/query").mock(side_effect=handle)
    found = hb.search_attachments(" https://example.test/private?x=1 ")
    assert isinstance(found, AttachmentSearchResult)
    assert found.model_dump(mode="json", by_alias=True) == result()
    assert route.call_count == 1


@pytest.mark.parametrize("setting", ["", "file:///attachment-search", "http://search.test",
    "http://u:p@search.test/attachment-search", SEARCH + "?q=bad", SEARCH + "#fragment"])
def test_missing_or_invalid_operator_config_fails_closed(setting, monkeypatch, router):
    monkeypatch.setattr(hb, "HOMEBOX_ATTACHMENT_SEARCH_URL", setting)
    with pytest.raises(hb.ToolError, match="configure HOMEBOX_ATTACHMENT_SEARCH_URL"):
        hb.search_attachments("fixture")
    assert not router.calls


@pytest.mark.parametrize("query", ["", "  ", "x" * 501])
def test_query_bounds_are_local_and_sanitized(query, router):
    with pytest.raises(hb.ToolError, match="1 to 500"):
        hb.search_attachments(query)
    assert not router.calls


def test_query_500_characters_and_trailing_slash_config(monkeypatch, router):
    monkeypatch.setattr(hb, "HOMEBOX_ATTACHMENT_SEARCH_URL", SEARCH + "/")
    route = router.get(SEARCH + "/query").respond(200, json={"hits": [], "partial": False})
    assert hb.search_attachments("x" * 500).hits == []
    assert route.called


@pytest.mark.parametrize("status", [302, 401, 403, 500, 503])
def test_upstream_status_is_error_without_body_or_redirect(router, status, caplog):
    router.get(SEARCH + "/query").respond(status, headers={"Location": "http://secret.test"},
                                          text="PRIVATE_PAYLOAD")
    with pytest.raises(hb.ToolError, match=f"HTTP {status}") as error:
        hb.search_attachments("fixture")
    assert "PRIVATE_PAYLOAD" not in str(error.value) + caplog.text
    assert len(router.calls) == 1


@pytest.mark.parametrize("payload", [{}, {"hits": [], "partial": "yes"},
                                      {"hits": "PRIVATE_PAYLOAD", "partial": False}])
def test_invalid_upstream_is_not_empty_success(router, payload):
    router.get(SEARCH + "/query").respond(200, json=payload)
    with pytest.raises(hb.ToolError, match="invalid result") as error:
        hb.search_attachments("fixture")
    assert "PRIVATE_PAYLOAD" not in str(error.value)


def test_transport_connectivity_and_size_errors(router):
    route = router.get(SEARCH + "/query").mock(side_effect=httpx.ConnectError("PRIVATE_URL"))
    with pytest.raises(hb.ToolError, match="cannot reach attachment search") as error:
        hb.search_attachments("fixture")
    assert "PRIVATE_URL" not in str(error.value)
    route.respond(200, content=b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(hb.ToolError, match="exceeds 4 MiB"):
        hb.search_attachments("fixture")


@pytest.mark.parametrize("mime", sorted(hb._SEARCH_IMAGE_TYPES))
def test_inline_photo_is_raw_bytes_and_service_auth(router, mime):
    def handle(request):
        assert request.content == b"photo fixture"
        assert request.headers["Content-Type"] == mime
        assert request.headers["Authorization"] == "test-token"
        return httpx.Response(200, json={"hits": [], "partial": False})
    router.post(SEARCH + "/similar").mock(side_effect=handle)
    assert not hb.find_similar_attachments(base64.b64encode(b"photo fixture").decode(), mime).partial


def test_inline_decoded_boundary_and_general_string_limit(router):
    router.post(SEARCH + "/similar").respond(200, json={"hits": [], "partial": False})
    photo = base64.b64encode(b"x" * hb.MAX_INLINE_IMAGE_BYTES).decode()
    hb.find_similar_attachments(photo, "image/jpeg")
    # +1 and +2 bytes have the same encoded length at this boundary.
    for extra in (1, 2, 3):
        photo = base64.b64encode(b"x" * (hb.MAX_INLINE_IMAGE_BYTES + extra)).decode()
        with pytest.raises(hb.ToolError, match="700 KiB"):
            hb.find_similar_attachments(photo, "image/jpeg")
    with pytest.raises(hb.ToolError, match="10000"):
        hb.find_similar_attachments("eA==", "x" * 10001)
    with pytest.raises(hb.ToolError, match="10000"):
        hb.search_items("x" * 10001)


@pytest.mark.parametrize("kwargs", [{}, {"image_base64": "eA=="},
    {"image_base64": "PRIVATE_PHOTO!", "mime_type": "image/jpeg"},
    {"image_base64": "data:image/png;base64,eA==", "mime_type": "image/png"},
    {"image_base64": "eA==", "mime_type": "application/pdf"},
    {"entity_id": EID}, {"entity_id": "https://private.test/", "attachment_id": AID},
    {"entity_id": EID, "attachment_id": AID, "image_base64": "eA==", "mime_type": "image/png"}])
def test_invalid_photo_options_never_fetch(kwargs, router):
    with pytest.raises(hb.ToolError) as error:
        hb.find_similar_attachments(**kwargs)
    assert "PRIVATE_PHOTO" not in str(error.value)
    assert not router.calls


def reference(router, attachment=None, content=b"fixture", content_type="application/octet-stream"):
    metadata = {"id": AID, "type": "photo", "mimeType": "image/jpeg",
                "path": "internal-storage-path"}
    if attachment:
        metadata.update(attachment)
    router.get(f"/entities/{EID}").respond(200, json={"id": EID, "attachments": [metadata]})
    return router.get(f"/entities/{EID}/attachments/{AID}").respond(
        200, content=content, headers={"Content-Type": content_type})


@pytest.mark.parametrize("kind", ["photo", "manual", "receipt", "attachment", "warranty"])
def test_existing_image_uses_uuid_route_and_metadata_mime(router, kind):
    reference(router, {"type": kind})
    route = router.post(SEARCH + "/similar").respond(200, json=result())
    found = hb.find_similar_attachments(entity_id=EID, attachment_id=AID)
    assert found.partial
    assert route.calls[0].request.content == b"fixture"
    assert route.calls[0].request.headers["Content-Type"] == "image/jpeg"
    assert len(router.calls) == 3


def test_existing_photo_can_use_sniffed_response_mime(router):
    reference(router, {"mimeType": "application/octet-stream"}, content_type="image/png")
    route = router.post(SEARCH + "/similar").respond(200, json={"hits": [], "partial": False})
    hb.find_similar_attachments(entity_id=EID, attachment_id=AID)
    assert route.calls[0].request.headers["Content-Type"] == "image/png"


@pytest.mark.parametrize("metadata", [{"id": EID}, {"type": "thumbnail"},
    {"mimeType": "link/url"}, {"path": "https://do-not-fetch.test/private"},
    {"path": "//do-not-fetch.test/private"}])
def test_reference_must_belong_to_entity_and_be_photo(router, metadata):
    download = reference(router, metadata)
    with pytest.raises(hb.ToolError, match="existing Homebox image attachment"):
        hb.find_similar_attachments(entity_id=EID, attachment_id=AID)
    assert not download.called


@pytest.mark.parametrize("mime", ["image/heic", "image/heif", "image/avif"])
def test_decoder_unsupported_image_mime_is_rejected(mime, router):
    with pytest.raises(hb.ToolError, match="supported image MIME"):
        hb.find_similar_attachments("eA==", mime)
    assert not router.calls


@pytest.mark.parametrize("inline", [True, False])
def test_pdf_reference_is_rejected_even_with_image_mime(inline, router):
    data = b" %PDF-1.7 test"
    if inline:
        arguments = {"image_base64": base64.b64encode(data).decode(), "mime_type": "image/png"}
    else:
        reference(router, content=data)
        arguments = {"entity_id": EID, "attachment_id": AID}
    with pytest.raises(hb.ToolError, match="not PDF"):
        hb.find_similar_attachments(**arguments)


def test_reference_uses_existing_download_cap(router):
    reference(router, content=b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(hb.ToolError, match="4 MiB"):
        hb.find_similar_attachments(entity_id=EID, attachment_id=AID)


def test_tool_schema_is_readonly_and_structured():
    by_name = {tool.name: tool for tool in asyncio.run(hb.mcp.list_tools())}
    for name in ("search_attachments", "find_similar_attachments"):
        assert by_name[name].annotations.read_only_hint
        assert not by_name[name].annotations.destructive_hint
        assert "hits" in by_name[name].output_schema["properties"]


@pytest.mark.parametrize("name,arguments", [
    ("find_similar_attachments", {"image_base64": {"PRIVATE_PHOTO_MARKER": "secret"},
                                   "mime_type": "image/png"}),
    ("search_attachments", {"query": ["PRIVATE_QUERY_MARKER"]}),
])
def test_sdk_validation_never_echoes_invalid_private_values(name, arguments, caplog):
    app = hb.mcp.streamable_http_app(json_response=True, stateless_http=True)
    with TestClient(app, base_url="http://localhost:8000") as client:
        response = call(client, name, arguments)
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    assert "PRIVATE_" not in response.text + caplog.text
    # The SDK chains the Pydantic error; its formatted representation must also
    # hide invalid values even if a consumer later formats the exception chain.
    metadata = hb.mcp._tool_manager.get_tool(name).fn_metadata
    with pytest.raises(ValueError) as error:
        metadata.validate_arguments(arguments)
    assert "PRIVATE_" not in str(error.value) + repr(error.value)


def test_search_has_fallback_time_without_extending_inventory_deadline(monkeypatch):
    factory = httpx.AsyncClient

    async def slow_response(request):
        await anyio.sleep(0.05)
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json=result())
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(slow_response)
    monkeypatch.setattr(hb.httpx, "AsyncClient", lambda **kwargs: factory(transport=transport, **kwargs))
    monkeypatch.setattr(hb, "OPERATION_TIMEOUT", 0.02)
    monkeypatch.setattr(hb, "ATTACHMENT_SEARCH_TIMEOUT", 0.15)
    assert hb.search_attachments("OCR fallback").partial
    with pytest.raises(hb.ToolError, match="deadline"):
        hb.list_tags()


def test_search_trickling_response_gets_sanitized_absolute_deadline(monkeypatch):
    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(20):
                await anyio.sleep(0.01)
                yield b"PRIVATE_RESPONSE"

    factory = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Trickle()))
    monkeypatch.setattr(hb.httpx, "AsyncClient", lambda **kwargs: factory(transport=transport, **kwargs))
    monkeypatch.setattr(hb, "ATTACHMENT_SEARCH_TIMEOUT", 0.04)
    with pytest.raises(hb.ToolError, match="attachment search deadline exceeded") as error:
        hb.search_attachments("private query")
    assert "PRIVATE_" not in str(error.value)
