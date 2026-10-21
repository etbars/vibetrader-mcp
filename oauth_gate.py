"""
OAuth gate — the MCP server as a protected resource
====================================================
VibeTrader's API is the sign-in server (docs/superpowers/specs/2026-10-21-
mcp-oauth-design.md in the main repo). This server only has to:

  * answer a request that carries no usable token with 401 and a
    WWW-Authenticate header pointing at its protected-resource metadata —
    that 401 is what makes Claude and ChatGPT show "Connect";
  * serve that metadata (RFC 9728) with `resource` equal to the exact URL the
    user entered: two hosts (the branded domain and the run.app URL) times two
    paths (/mcp and /sse);
  * let through tokens the API issued for one of those URLs, and `vt_` keys
    sent as a header — the API checks keys on every call, as it always has.

The bearer is then forwarded to the API unchanged, which enforces what the
connection may do. Written as plain ASGI around the whole app because the app
is assembled by hand from two FastMCP sub-apps (see server.py), so FastMCP's
own auth wiring would not reach /sse or /messages/.
"""
import json
from typing import Optional

from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier

AUTH_SERVER = "https://api.vibetrader.markets"
JWKS_URI = AUTH_SERVER + "/oauth/jwks.json"
BRANDED_HOST = "mcp.vibetrader.markets"
RESOURCE_HOSTS = (BRANDED_HOST, "vibetrader-mcp-289016366682.us-central1.run.app")
SCOPES = ["read", "bots", "trade"]
_PROTECTED = ("/mcp", "/sse", "/messages")
_METADATA = "/.well-known/oauth-protected-resource"


class VibeTraderTokenVerifier(TokenVerifier):
    """API keys pass (the API validates them); anything else must be one of the
    API's access tokens for this server."""

    def __init__(self, jwt_verifier: TokenVerifier):
        super().__init__(required_scopes=None)
        self.jwt_verifier = jwt_verifier

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        if token.startswith("vt_"):
            return AccessToken(token=token, client_id="api-key", scopes=[])
        return await self.jwt_verifier.verify_token(token)


def default_verifier() -> VibeTraderTokenVerifier:
    return VibeTraderTokenVerifier(JWTVerifier(
        jwks_uri=JWKS_URI, issuer=AUTH_SERVER, algorithm="RS256",
        audience=[f"https://{h}{p}" for h in RESOURCE_HOSTS for p in ("/mcp", "/sse")],
    ))


def _host(raw: str) -> str:
    host = (raw or "").split(":")[0].strip().lower()
    return host if host in RESOURCE_HOSTS else BRANDED_HOST


def resource_for(host: str, path: str) -> str:
    """The resource a client at this host and path entered."""
    return f"https://{_host(host)}{'/sse' if path.rstrip('/') == '/sse' else '/mcp'}"


def _metadata_url(host: str, path: str) -> str:
    suffix = "/sse" if path.rstrip("/") == "/sse" else "/mcp"
    return f"https://{_host(host)}{_METADATA}{suffix}"


class OAuthGate:
    def __init__(self, app, verifier: TokenVerifier):
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        host = headers.get("host", "")

        if path.startswith(_METADATA) and method in ("GET", "HEAD"):
            return await self._json(send, 200, {
                "resource": resource_for(host, path[len(_METADATA):] or "/mcp"),
                "authorization_servers": [AUTH_SERVER],
                "scopes_supported": SCOPES,
                "bearer_methods_supported": ["header"],
                "resource_name": "VibeTrader",
            })

        if method == "OPTIONS" or not any(path == p or path.startswith(p + "/") for p in _PROTECTED):
            return await self.app(scope, receive, send)

        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if token and await self.verifier.verify_token(token):
            return await self.app(scope, receive, send)

        challenge = f'Bearer resource_metadata="{_metadata_url(host, path)}", scope="{" ".join(SCOPES)}"'
        if token:
            challenge += ', error="invalid_token"'
        return await self._json(send, 401, {
            "error": "invalid_token" if token else "unauthorized",
            "error_description": "Sign in to VibeTrader to use this server." if not token
            else "The token is invalid or expired. Connect again.",
        }, {"www-authenticate": challenge})

    @staticmethod
    async def _json(send, status: int, body: dict, extra: Optional[dict] = None):
        payload = json.dumps(body).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()),
                   (b"cache-control", b"no-store")]
        headers += [(k.encode(), v.encode()) for k, v in (extra or {}).items()]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})
