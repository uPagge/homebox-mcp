"""External-issuer resource-server tests with ephemeral RSA keys; no IdP/network."""

import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.mcpserver import MCPServer
from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY
from starlette.testclient import TestClient

import homebox_mcp_runtime as runtime
from homebox_mcp_oauth import EDIT_SCOPE, READ_SCOPE, PublicKeyTokenVerifier

ISSUER = "https://issuer.example.test"
RESOURCE = "https://inventory.example.test/mcp"
METADATA = "/.well-known/oauth-protected-resource/mcp"
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Host": "localhost:8000",
    "Origin": "https://client.example.test",
    "MCP-Protocol-Version": "2025-11-25",
}


@pytest.fixture(scope="module")
def keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, public


@pytest.fixture
def oauth(keypair, tmp_path):
    private, public = keypair
    path = tmp_path / "trusted-public.pem"
    path.write_bytes(public)
    server = MCPServer("oauth-resource-test")
    hb = SimpleNamespace(mcp=server, calls=[], HOMEBOX_TOKEN="backend-secret")

    async def tool(value: str = "") -> dict:
        hb.calls.append(value)
        return {"ok": True}

    for name in runtime.READ_TOOLS | runtime.EDIT_TOOLS | {"delete_item"}:
        server.add_tool(tool, name=name)
    anyio.run(runtime.configure_tools, hb)
    config = runtime.HTTPConfig(
        auth_mode="oauth", oauth_issuer_url=ISSUER,
        oauth_resource_url=RESOURCE, oauth_public_key_file=str(path),
        allowed_origins=(HEADERS["Origin"],),
    )

    def token(**changes):
        claims = {
            "iss": ISSUER, "aud": RESOURCE, "sub": "owner-private-subject",
            "exp": int(time.time()) + 300, "scope": READ_SCOPE,
        }
        claims.update(changes)
        return jwt.encode(claims, private, algorithm="RS256")

    return SimpleNamespace(hb=hb, config=config, token=token, path=path)


def rpc(client, token=None, method="tools/list", params=None, headers=None):
    actual = HEADERS | {"MCP-Method": method}
    if token is not None:
        actual["Authorization"] = f"Bearer {token}"
    if params and "name" in params:
        actual["MCP-Name"] = params["name"]
    return client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
    }, headers=actual | (headers or {}))


def test_sdk_public_metadata_challenge_and_no_issuer_routes(oauth):
    with TestClient(runtime.create_http_app(oauth.hb, config=oauth.config)) as client:
        response = client.get(METADATA, headers=HEADERS)
        assert response.status_code == 200
        metadata = response.json()
        assert metadata["resource"] == RESOURCE
        assert metadata["authorization_servers"] == [ISSUER]
        assert set(metadata["scopes_supported"]) == {READ_SCOPE, EDIT_SCOPE}
        denied = rpc(client)
        assert denied.status_code == 401
        assert f'resource_metadata="https://inventory.example.test{METADATA}"' in denied.headers[
            "www-authenticate"
        ]
        # Public discovery is not a route alias to tools, even if POSTed JSON-RPC.
        assert client.post(METADATA, json={"method": "tools/call"}, headers=HEADERS).status_code == 405
        for path in ("/token", "/authorize", "/register", "/.well-known/oauth-authorization-server"):
            assert client.get(path, headers=HEADERS).status_code != 200
        assert oauth.hb.calls == []


def test_read_edit_scope_enforced_on_each_call(oauth):
    with TestClient(runtime.create_http_app(oauth.hb, config=oauth.config)) as client:
        read, edit = oauth.token(), oauth.token(scope=EDIT_SCOPE)
        tools = rpc(client, read).json()["result"]["tools"]
        assert {tool["name"] for tool in tools} == runtime.READ_TOOLS
        assert rpc(client, read, "tools/call", {"name": "list_entities"}).json()["result"][
            "isError"
        ] is False
        denied = rpc(client, read, "tools/call", {"name": "update_entity"}).json()
        assert denied["result"]["isError"] is True
        assert len(oauth.hb.calls) == 1
        assert rpc(client, edit, "tools/call", {"name": "update_entity"}).json()["result"][
            "isError"
        ] is False
        # An edit-only scope includes inventory reads, never vice versa.
        assert rpc(client, edit, "tools/call", {"name": "get_entity"}).json()["result"][
            "isError"
        ] is False
        assert rpc(client, read, "tools/call", {"name": "set_item"}).json()["result"][
            "isError"
        ] is True
        assert rpc(client, method="tools/call", params={"name": "get_entity"}).status_code == 401
        assert len(oauth.hb.calls) == 3


@pytest.mark.parametrize("changes", [
    {"iss": "https://wrong.example.test"}, {"aud": "https://wrong.example.test/mcp"},
    {"exp": int(time.time()) - 3600}, {"exp": None}, {"exp": float("inf")},
    {"exp": float("nan")}, {"exp": "999999999999"}, {"exp": True},
    {"sub": None}, {"sub": ""}, {"scope": ""}, {"scope": "unrelated:read"},
    {"scope": [READ_SCOPE]}, {"client_id": []},
])
def test_invalid_claims_fail_closed(oauth, changes):
    with TestClient(runtime.create_http_app(oauth.hb, config=oauth.config)) as client:
        response = rpc(client, oauth.token(**changes), "tools/call", {"name": "update_entity"})
        assert response.status_code == 401, response.text
        assert "resource_metadata=" in response.headers["www-authenticate"]
        assert oauth.hb.calls == []


@pytest.mark.parametrize("claim", ["exp", "iss", "aud", "sub", "scope"])
def test_required_claims(oauth, keypair, claim):
    claims = jwt.decode(oauth.token(), options={"verify_signature": False})
    del claims[claim]
    token = jwt.encode(claims, keypair[0], algorithm="RS256")
    with TestClient(runtime.create_http_app(oauth.hb, config=oauth.config)) as client:
        assert rpc(client, token).status_code == 401


def test_signature_and_algorithm_cannot_be_selected_by_token(oauth):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = jwt.decode(oauth.token(), options={"verify_signature": False})
    bad_tokens = [
        jwt.encode(claims, other_key, algorithm="RS256"),
        jwt.encode(claims, "attacker-secret" * 4, algorithm="HS256"),
        jwt.encode(claims, None, algorithm="none"),
        "malformed.jwt", "", "x" * 16385,
    ]
    with TestClient(runtime.create_http_app(oauth.hb, config=oauth.config)) as client:
        for token in bad_tokens:
            assert rpc(client, token).status_code == 401
        # A token-controlled jku is ignored; signature must use the pinned key.
        token = jwt.encode(claims, other_key, algorithm="RS256", headers={
            "jku": "https://attacker.invalid/keys", "kid": "attacker-key"
        })
        assert rpc(client, token).status_code == 401
        assert oauth.hb.calls == []


def test_backend_token_is_never_an_mcp_access_token(oauth, monkeypatch):
    valid = oauth.token(scope=EDIT_SCOPE)
    oauth.hb.HOMEBOX_TOKEN = valid
    app = runtime.create_http_app(oauth.hb, config=oauth.config)
    with TestClient(app) as client:
        assert rpc(client, valid).status_code == 401
        other_valid = oauth.token(scope=f"{READ_SCOPE} {EDIT_SCOPE}")
        monkeypatch.setenv("HOMEBOX_TOKEN", other_valid)
        assert rpc(client, other_valid).status_code == 401
        assert oauth.hb.calls == []


def test_malformed_duplicate_headers_and_bearer_keys_rejected(oauth):
    with TestClient(runtime.create_http_app(oauth.hb, config=oauth.config)) as client:
        for auth in ("Basic token", "Bearer", "Bearer bad token", "Bearer " + "e" * 40):
            assert rpc(client, headers={"Authorization": auth}).status_code == 401
        response = client.post("/mcp", headers=list(HEADERS.items()) + [
            ("Authorization", f"Bearer {oauth.token()}"),
            ("Authorization", f"Bearer {oauth.token(scope=EDIT_SCOPE)}"),
        ], json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]


def test_current_protocol_and_oauth_audit_are_sanitized(oauth, caplog):
    token = oauth.token(scope=EDIT_SCOPE)
    meta = {PROTOCOL_VERSION_META_KEY: "2026-07-28", CLIENT_CAPABILITIES_META_KEY: {}}
    with caplog.at_level("INFO", logger="homebox_mcp.audit"):
        with TestClient(runtime.create_http_app(oauth.hb, config=oauth.config)) as client:
            response = rpc(client, token, "tools/call", {
                "name": "update_entity", "arguments": {"value": "PRIVATE_INVENTORY_VALUE"},
                "_meta": meta,
            }, {"MCP-Protocol-Version": "2026-07-28"})
            assert response.status_code == 200, response.text
            assert response.json()["result"]["isError"] is False
    audit = "\n".join(record.getMessage() for record in caplog.records
                      if record.name == "homebox_mcp.audit")
    assert "tool=update_entity outcome=success duration_ms=" in audit
    for private in (token, "PRIVATE_INVENTORY_VALUE", "owner-private-subject"):
        assert private not in audit


def test_trusted_key_loaded_only_at_startup_and_audience_list(oauth):
    app = runtime.create_http_app(oauth.hb, config=oauth.config)
    oauth.path.unlink()
    with TestClient(app) as client:
        assert rpc(client, oauth.token(aud=["https://other.example.test", RESOURCE])).status_code == 200


@pytest.mark.parametrize("changes", [
    {"oauth_issuer_url": "http://issuer.example.test"},
    {"oauth_resource_url": "http://inventory.example.test"},
    {"oauth_issuer_url": "https://user:password@issuer.example.test"},
    {"oauth_resource_url": "https://inventory.example.test/mcp#fragment"},
    {"oauth_resource_url": "https://inventory.example.test/mcp?query=x"},
    {"oauth_issuer_url": "https://issuer.example.test:bad"},
])
def test_explicit_https_config_required(oauth, changes):
    with pytest.raises(ValueError, match="HTTPS"):
        runtime.create_http_app(oauth.hb, config=replace(oauth.config, **changes))


@pytest.mark.parametrize("kind", ["missing", "garbage", "private-key"])
def test_missing_or_invalid_public_pem_fails_startup(oauth, keypair, kind):
    if kind == "missing":
        oauth.path.unlink()
    elif kind == "garbage":
        oauth.path.write_bytes(b"not a public key")
    else:
        oauth.path.write_bytes(keypair[0].private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
    with pytest.raises(ValueError, match="trusted RSA public PEM"):
        runtime.create_http_app(oauth.hb, config=oauth.config)


def test_oauth_keeps_http_bounds_and_host_origin_guards(oauth):
    config = replace(oauth.config, max_body_bytes=4000, requests_per_minute=4)
    with TestClient(runtime.create_http_app(oauth.hb, config=config)) as client:
        assert rpc(client, oauth.token(), headers={"Host": "attacker.invalid"}).status_code == 421
        assert rpc(client, oauth.token(), headers={"Origin": "https://attacker.invalid"}).status_code == 403
        response = client.post("/mcp", content=b"x" * 4001, headers=HEADERS | {
            "Authorization": f"Bearer {oauth.token()}"
        })
        assert response.status_code == 413
        assert rpc(client, oauth.token()).status_code == 200
        assert rpc(client, oauth.token()).status_code == 429


def test_resource_server_rejects_preconfigured_issuer(oauth):
    oauth.hb.mcp._auth_server_provider = object()
    with pytest.raises(ValueError, match="must not configure.*authorization-server"):
        runtime.create_http_app(oauth.hb, config=oauth.config)


def test_bearer_mode_does_not_publish_oauth_metadata(oauth):
    config = runtime.HTTPConfig(bearer_token="e" * 40)
    with TestClient(runtime.create_http_app(oauth.hb, config=config)) as client:
        assert client.get(METADATA, headers=HEADERS).status_code == 401
        assert client.get(METADATA, headers=HEADERS | {
            "Authorization": "Bearer " + "e" * 40
        }).status_code == 404


def test_invalid_credentials_share_bounded_verification_budget(oauth):
    config = replace(oauth.config, requests_per_minute=1)
    with TestClient(runtime.create_http_app(oauth.hb, config=config)) as client:
        assert rpc(client, "invalid.jwt").status_code == 401
        assert rpc(client, "another.invalid.jwt").status_code == 429
        assert oauth.hb.calls == []


def test_public_metadata_does_not_bypass_admission_or_verify_jwt(oauth, monkeypatch):
    config = replace(oauth.config, requests_per_minute=1)
    app = runtime.create_http_app(oauth.hb, config=config)
    verifier = AsyncMock(wraps=app.oauth_verifier.verify_token)
    monkeypatch.setattr(app.oauth_verifier, "verify_token", verifier)
    foreign_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    invalid_signature = jwt.encode({"iss": ISSUER, "aud": RESOURCE, "sub": "test",
                                    "exp": int(time.time()) + 300, "scope": READ_SCOPE},
                                   foreign_key, algorithm="RS256")
    with TestClient(app) as client:
        headers = HEADERS | {"Authorization": f"Bearer {invalid_signature}"}
        assert client.get(METADATA, headers=headers).status_code == 200
        assert client.get(METADATA, headers=headers).status_code == 429
    verifier.assert_not_awaited()


def test_standalone_verifier_implements_sdk_contract(oauth):
    verifier = PublicKeyTokenVerifier(
        issuer_url=ISSUER, resource_url=RESOURCE, public_key_file=str(oauth.path)
    )
    access = anyio.run(verifier.verify_token, oauth.token())
    assert access.subject == "owner-private-subject"
    assert access.resource == RESOURCE
    assert access.scopes == [READ_SCOPE]
    assert access.claims == {"iss": ISSUER}
