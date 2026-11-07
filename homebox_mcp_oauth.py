"""OAuth resource-server verification only; the issuer and client flows are external.

Trust is pinned at startup to an administrator-provided RSA public PEM. PyJWT
performs signature/issuer/audience/time validation; token headers never select a
key, algorithm or URL. Rotation requires replacing the trusted file and restart.
"""

from __future__ import annotations

import math
import os
from urllib.parse import urlsplit

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

READ_SCOPE = "homebox:read"
EDIT_SCOPE = "homebox:edit"
SCOPES = [READ_SCOPE, EDIT_SCOPE]


def validate_oauth_urls(issuer_url: str | None, resource_url: str | None) -> None:
    for value in (issuer_url, resource_url):
        try:
            parsed = urlsplit(value or "")
            valid = (
                isinstance(value, str) and parsed.scheme == "https" and parsed.hostname
                and not parsed.username and not parsed.password and not parsed.query
                and not parsed.fragment and not any(ord(char) <= 32 for char in value)
            )
            parsed.port  # Reject malformed ports without reporting the configured value.
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("OAuth issuer and resource URLs must be explicit HTTPS URLs")


class PublicKeyTokenVerifier(TokenVerifier):
    """Validate RS256 access JWTs for one configured issuer and MCP resource.

    Scope claim uses the OAuth space-separated `scope` string. An edit scope
    grants inventory reads too; the runtime independently enforces the edit gate
    on each tools/call. This is one backend identity, not multi-user tenancy.
    """

    def __init__(self, *, issuer_url: str, resource_url: str, public_key_file: str,
                 forbidden_tokens: tuple[str | None, ...] = ()):
        validate_oauth_urls(issuer_url, resource_url)
        self.issuer_url = issuer_url
        self.resource_url = resource_url
        self.forbidden_tokens = tuple(token for token in forbidden_tokens if token)
        try:
            with open(public_key_file, "rb") as source:
                pem = source.read(65537)
            if len(pem) > 65536 or not pem.startswith((
                b"-----BEGIN PUBLIC KEY-----", b"-----BEGIN RSA PUBLIC KEY-----"
            )):
                raise ValueError("Expected RSA public PEM")
            self.public_key = jwt.algorithms.get_default_algorithms()["RS256"].prepare_key(pem)
        except (OSError, ValueError, TypeError, jwt.PyJWTError) as exc:
            raise ValueError("OAuth requires a readable trusted RSA public PEM file") from exc

    async def verify_token(self, token: str) -> AccessToken | None:
        # Never admit the downstream credential, including import-time and current
        # environment values. No token, claims, subject or failure detail is logged.
        if not isinstance(token, str) or not token or len(token) > 16384 or token in (
            *self.forbidden_tokens, os.environ.get("HOMEBOX_TOKEN")
        ):
            return None
        try:
            claims = jwt.decode(
                token,
                self.public_key,
                algorithms=["RS256"],
                issuer=self.issuer_url,
                audience=self.resource_url,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
            expiration = claims["exp"]
            if isinstance(expiration, bool) or not isinstance(expiration, (int, float)) or not (
                math.isfinite(expiration)
            ):
                return None
            subject = claims["sub"]
            scope = claims.get("scope")
            if not isinstance(subject, str) or not subject or not isinstance(scope, str):
                return None
            scopes = set(scope.split()) & set(SCOPES)
            if not scopes:
                return None
            # client_id is optional for external issuers; sub remains a required,
            # verified principal. Never persist arbitrary other JWT claims.
            client_id = claims.get("client_id", subject)
            if not isinstance(client_id, str) or not client_id:
                return None
            return AccessToken(
                token=token, client_id=client_id, subject=subject,
                scopes=sorted(scopes), expires_at=int(expiration),
                resource=self.resource_url, claims={"iss": self.issuer_url},
            )
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
            return None
