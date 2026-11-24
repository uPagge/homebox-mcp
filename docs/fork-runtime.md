# Fork runtime, authentication, and data contracts

This fork retains upstream history and tools, using the official Python MCP SDK
2.3+ within 2.x. Install from this repository/tag; the similarly named PyPI
package is upstream's distribution. No PyPI/MCP registry publication is performed
by the fork's workflows. Dependencies resolve through `uv.lock`; deployment uses
hash-verified `requirements.lock`. Review updates and rerun preservation/auth
regressions before updating either lock.

## Tool exposure

Normal entrypoints (`homebox-mcp`, `python homebox_mcp.py`, compatibility
`server.py`) apply policy before serving:

| Mode | Exposure |
| --- | --- |
| `--tool-mode read` | 13 inventory read tools |
| `--tool-mode read-edit` (default) | 13 reads + 12 editors |
| `--tool-mode legacy` | All retained upstream tools, including create/delete, arbitrary URL/file uploads and filesystem output |

The new tools are `get_entity`, `update_entity` and `list_entities`. Existing tool
names are retained. A Python import registers source tools but does not apply
runtime policy; embedded servers must call `await configure_tools(module, mode)`
BEFORE creating their app. Do not expose `module.mcp.run()` directly and assume
that CLI policy/authentication applies. `configure_tools` cannot restore tools
previously removed from a live object; restart when changing modes.

Overwrite editors are explicitly potentially destructive, idempotent and not
read-only. Hints do not grant permission or remove the need for client approval.
Tag editors can create unknown tags, even in read-edit mode. `legacy` is an
explicit unsafe-capability opt-in, not an authenticated safe-file API: do not
expose it to untrusted callers. URL/file/CSV/photo workflows require independent
SSRF, path, size and data-safety review before broader use.

## Local stdio

The client starts the process, supplies `HOMEBOX_URL`/`HOMEBOX_TOKEN` in its
protected environment and owns the normal process trust boundary. No extra MCP
bearer key is required for stdio. stdout remains protocol-only. A sibling `.env`
is accepted for clone installations; protect it with mode 0600 and never commit
it. The template contains placeholders only.

## Private single-owner HTTP

```sh
# Provision these distinct values in a protected environment, not command history:
# HOMEBOX_TOKEN: backend Homebox API key
# HOMEBOX_MCP_TOKEN: independently generated random MCP edit credential (>=32 ASCII chars)
# HOMEBOX_MCP_READ_TOKEN: optional different random read-only credential
homebox-mcp --transport streamable-http --tool-mode read-edit
```

The stateless MCP endpoint serves requests over POST. An authenticated standalone
GET to `/mcp` returns 405, allowing clients to proceed without an idle SSE stream.

Default bind is loopback, port 8000. HTTP fails closed without a valid MCP key.
The backend token cannot be reused as an MCP credential. Every request is
authenticated, and the read capability is checked at actual tool invocation,
not only discovery. A read key cannot invoke an editor guessed by name.

These keys authorize capabilities of **one configured Homebox service identity**;
they do not map separate users to separate Homebox accounts. Revoke/rotate by
changing the key and restarting. Never log or paste generated keys. Use a TLS
reverse proxy or authenticated tunnel for non-local access. No public proxy,
tunnel, certificate or identity provider is installed by this package.

Set `HOMEBOX_MCP_ALLOWED_HOSTS` to an explicit comma-separated list, e.g.
`localhost:*,127.0.0.1:*` for local access, or your deliberately configured proxy
host. `HOMEBOX_MCP_ALLOWED_ORIGINS` is an explicit comma-separated browser-origin
allowlist; default is empty. Invalid origins are rejected, not wildcard-allowed.
Container bind `--host 0.0.0.0` does not authorize public host publication. Binding
only the host loopback port still leaves peers on a shared container network;
HTTP authentication is required independently of that network boundary.

## OAuth resource-server mode

Use an existing authorization server, not the Homebox API key or a homemade
issuer. Configure:

```text
HOMEBOX_MCP_OAUTH_ISSUER=https://issuer.example
HOMEBOX_MCP_OAUTH_RESOURCE=https://mcp.example/mcp
HOMEBOX_MCP_OAUTH_PUBLIC_KEY_FILE=/configured/path/public.pem
```

Then start with `--transport streamable-http --auth-mode oauth`. Both URLs must
be HTTPS. The PEM is the issuer's **public RSA verification key**, not its
private signing key. The file is trusted administrator configuration; token
headers cannot choose a key, URL or algorithm. Rotation requires updating this
file and restarting. This intentionally avoids untrusted JWKS/discovery URL
fetching, but does not provide automatic signing-key rotation.

PyJWT validates RS256 signature, issuer, audience and mandatory `exp`, `iss`,
`aud`, `sub` claims. Invalid/expired/nonfinite-expiry tokens fail closed. The
space-separated `scope` claim must include `homebox:read` or `homebox:edit`;
edit permits reads too. SDK TokenVerifier/AccessToken/AuthSettings provide
standard bearer authorization and protected-resource metadata. With resource
`https://mcp.example/mcp`, metadata is available at
`/.well-known/oauth-protected-resource/mcp`; challenges identify it. That exact
metadata route is public, not the tool endpoint. Issuer login, client registration,
consent, PKCE and token issuance remain responsibilities of the external issuer
and client. No issuer/token endpoints are created here.

The SDK has no public post-construction token-verifier setter. The startup-only
assignment is localized in `create_http_app`, covered by SDK-version/auth tests;
review it on SDK upgrades. The OAuth mode was tested with synthetic signed tokens,
not certified against an arbitrary identity provider or a logged-in ChatGPT account.

## Complete reads and partial updates

`get_entity(entity_id)` accepts a stable UUID and returns a typed backend DTO,
including parent, resolved physical location, nullable explicit `locationId`,
entity type, false/zero/null values, system metadata, attachment metadata and
custom-field IDs/types. It does not download attachment file content.

`get_item(identifier)` remains a convenient exact/fuzzy lookup. Its `fields`
member is the legacy name/value dictionary; `custom_fields` retains the complete
list, including duplicate names and IDs. `tags` remains a legacy name list and
`tag_details` preserves tag objects. `location_path` is containment ancestry;
use `location` for resolved physical location. `list_entities` returns one bounded
backend summary page with total/next page; locations come from `list_locations`.

Example `update_entity` arguments:

```json
{
  "entity_id": "11111111-1111-4111-8111-111111111111",
  "changes": {
    "quantity": 1.25,
    "insured": false,
    "purchasePrice": 0,
    "purchaseDate": null,
    "locationId": null
  }
}
```

- Omitted members are unchanged. null clears dates or parent/location links;
  empty strings clear text; false clears booleans; `[]` clears field/tag lists.
  Null is rejected for nonnullable editable scalars. Unknown keys are errors.
- `fields` is deliberate full-list replacement with validated complete nested
  DTOs. Keep existing IDs to update fields without recreating them. New fields
  have no ID. IDs belonging to another entity, duplicate IDs and unsupported time
  edits are rejected before mutation. `remove_field_ids` surgically deletes
  existing field IDs and cannot be combined with list replacement.
- All user-editable entity DTO fields are available through `EntityChanges`;
  system timestamps, resolved relationships and attachments are not arbitrary
  scalar assignments. Attachment title/type/primary and tag/maintenance metadata
  retain their focused upstream editors. Creation/deletion/upload are not implied.
- Effective containment after backend parent/location normalization is checked
  against self/descendant moves. A location cannot be placed under an item.
- Where the requested combination is fully supported, PATCH avoids overwriting
  unrelated concurrent edits. Null relationship clearing and broader scalar
  changes require full PUT. Those operations cannot atomically prevent writes
  racing with another UI/client; read back and avoid concurrent editing.
- Existing PUT/upsert editors preserve field IDs, explicit/inherited locations
  and the child sync flag. Full reads remain subject to the configured backend
  version's actual API contract; an MCP client cannot invent missing API data.

Typed entity tools provide `outputSchema`, `structuredContent` and text fallback.
Legacy tools retain their return shapes for compatibility; not every legacy list
is wrapped in a new envelope. Domain/API/validation failures become execution
errors with `isError: true`; raw backend error bodies are not echoed.

## Execution and audit bounds

Current defaults:

- HTTP body 1 MiB, at most four active requests, 60 admitted requests/minute per
  process (including invalid-auth attempts), tool deadline 60 s and request
  deadline 65 s. One process-global bucket bounds memory; it is not a multiworker
  distributed rate limiter and can be exhausted by a caller reaching the port.
- Core operation wall-clock budget 30 s, at most 2000 backend calls, at most
  4 MiB per backend response and 1 MiB tool output. Async HTTPX cancellation
  enforces the wall-clock budget even on trickling responses. Backend requests
  are made without inherited proxy settings or automatic redirects. No automatic
  write retry; after an ambiguous timeout, read back before deciding to repeat.
- Search limit 1–200, bounded explicit page size 1–200, recursive contents maximum
  1000, input collections maximum 200 entries and strings maximum 10000 chars.
  Full scans above 10000 entities fail explicitly. A dedupe index is never silently
  returned as complete after truncation/budget failure.

HTTPConfig supports explicit process-local bounds for embedded apps. Core budgets
are constants kept together in the source. Limits reject work rather than promise
arbitrary-size exports; use narrower/page-oriented calls. A slow synchronous
legacy file operation is not made cancellable by HTTP middleware. Those tools
remain outside normal mode.

Audit events contain known tool name, outcome and duration, never arguments,
credentials or full result values. HTTP client URL INFO logging is disabled by
normal CLI startup. Do not enable verbose SDK/network debugging on private data.

## Verification and release

```sh
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen ruff check .
uv build
```

The mocked suite does not use real inventory. The separate
[disposable-backend harness](integration-tests.md) performs real mutations and
must only target throwaway local storage. Inspector/target-client checks are
additional acceptance, not a substitute for API preservation tests. No successful
ChatGPT account/tunnel connection is implied by a healthy HTTP service.

Upstream integration: retain history, use small commits, propose generic fixes
upstream when separately authorized, keep local deployment secrets/configuration
outside this repository and rerun the exact preservation/auth regressions on every
upstream/SDK update. Dependency auto-merge is disabled because upstream branch
protection is not inherited by a fork. GitHub release builds never publish to
upstream's PyPI identity.
