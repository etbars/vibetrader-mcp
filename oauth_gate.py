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
import asyncio
import base64
import json
import logging
import time
from typing import Awaitable, Callable, Optional

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm
from fastmcp.server.auth import AccessToken, TokenVerifier

logger = logging.getLogger(__name__)

AUTH_SERVER = "https://api.vibetrader.markets"
JWKS_URI = AUTH_SERVER + "/oauth/jwks.json"
BRANDED_HOST = "mcp.vibetrader.markets"
RESOURCE_HOSTS = (BRANDED_HOST, "vibetrader-mcp-289016366682.us-central1.run.app")
SCOPES = ["read", "bots", "trade"]
_PROTECTED = ("/mcp", "/sse", "/messages")
_METADATA = "/.well-known/oauth-protected-resource"


class KeysUnavailable(Exception):
    """No signing keys could be loaded: the server can't tell a good token from
    a bad one, so it says so (503) rather than calling every token bad."""


def jwk_for(public_key, kid: str) -> dict:
    """A public RSA key as a JWK (what the API's JWKS publishes)."""
    numbers = public_key.public_numbers()

    def b64(n: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()
    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid, "n": b64(numbers.n), "e": b64(numbers.e)}


class JWKSVerifier(TokenVerifier):
    """Verifies the API's RS256 access tokens against its JWKS.

    The keys are cached and refreshed hourly. A failed refresh keeps the keys
    already held; a token with a key id we don't know triggers at most one
    fetch per MIN_REFETCH seconds (so forged tokens can't make us hammer the
    API); concurrent first requests share one fetch.
    """

    REFRESH_AFTER = 3600
    MIN_REFETCH = 30
    LEEWAY = 60

    def __init__(self, fetch: Callable[[], Awaitable[dict]], *, issuer: str, audience: list[str],
                 clock: Callable[[], float] = time.time):
        super().__init__(required_scopes=None)
        self._fetch, self.issuer, self.audience, self._clock = fetch, issuer, list(audience), clock
        self._keys: dict = {}
        self._fetched_at: Optional[float] = None
        self._last_attempt: Optional[float] = None
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        async with self._lock:
            now = self._clock()
            if self._last_attempt is not None and now - self._last_attempt < self.MIN_REFETCH:
                return
            self._last_attempt = now
            try:
                document = await self._fetch()
                keys = {k["kid"]: RSAAlgorithm.from_jwk(k) for k in document.get("keys", [])
                        if k.get("kty") == "RSA" and k.get("kid")}
            except Exception as e:  # noqa: BLE001 - any failure keeps the keys we have
                logger.warning(f"JWKS refresh failed; keeping {len(self._keys)} cached key(s): {e}")
                return
            if keys:
                self._keys, self._fetched_at = keys, now

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            return None
        now = self._clock()
        if self._fetched_at is None or now - self._fetched_at > self.REFRESH_AFTER or kid not in self._keys:
            await self._refresh()
        if not self._keys:
            raise KeysUnavailable()
        key = self._keys.get(kid)
        if key is None:
            return None
        try:
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.audience, issuer=self.issuer,
                                options={"verify_exp": False, "verify_iat": False,
                                         "require": ["exp", "iat", "sub", "aud", "iss"]})
        except jwt.PyJWTError:
            return None
        if now > claims["exp"] + self.LEEWAY:
            return None
        return AccessToken(token=token, client_id=str(claims.get("client_id", "")),
                           scopes=str(claims.get("scope", "")).split(), expires_at=int(claims["exp"]),
                           subject=claims["sub"], claims=claims)


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


async def _fetch_jwks() -> dict:
    async with httpx.AsyncClient(timeout=3.0) as http:
        response = await http.get(JWKS_URI)
        response.raise_for_status()
        return response.json()


def default_verifier() -> VibeTraderTokenVerifier:
    return VibeTraderTokenVerifier(JWKSVerifier(
        _fetch_jwks, issuer=AUTH_SERVER,
        audience=[f"https://{h}{p}" for h in RESOURCE_HOSTS for p in ("/mcp", "/sse")],
    ))


def _host(raw: str) -> str:
    host = (raw or "").split(":")[0].strip().lower()
    return host if host in RESOURCE_HOSTS else BRANDED_HOST


def _suffix(path: str) -> str:
    """Which endpoint a path belongs to. /messages/ is the SSE transport's POST
    route, so its clients' resource is /sse."""
    p = path.rstrip("/")
    return "/sse" if p == "/sse" or p == "/messages" or p.startswith("/messages/") else "/mcp"


def resource_for(host: str, path: str) -> str:
    """The resource a client at this host and path entered."""
    return f"https://{_host(host)}{_suffix(path)}"


def _metadata_url(host: str, path: str) -> str:
    return f"https://{_host(host)}{_METADATA}{_suffix(path)}"


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
        try:
            verified = bool(token) and await self.verifier.verify_token(token)
        except KeysUnavailable:
            return await self._json(send, 503, {
                "error": "temporarily_unavailable",
                "error_description": "Sign-in can't be checked right now. Try again shortly.",
            }, {"retry-after": "30"})
        if verified:
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
