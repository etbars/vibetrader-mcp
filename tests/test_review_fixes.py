"""Fixes from the 2026-10-21 MCP sign-in review.

- A retry after the client refreshed its sign-in token (every hour) must keep
  the order's id: the caller is the connection (sub + grant), not the token.
- SSE clients' 401s on /messages/ point at the /sse metadata (their resource).
- The signing keys are fetched resiliently: cached keys keep working when a
  refresh fails, unknown key ids can't trigger a fetch per request, and with no
  keys at all the answer is 503, not "your token is bad".
- No tool description tells the model to use another server's tools.
"""
import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp import Client
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import oauth_gate
import server
from oauth_gate import AUTH_SERVER, JWKSVerifier, OAuthGate, VibeTraderTokenVerifier, jwk_for

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
AUD = "https://mcp.vibetrader.markets/mcp"


def token(sub="u1", gid="g1", jti="a", kid="k1", key=KEY, exp_in=3600, aud=AUD):
    now = int(time.time())
    return jwt.encode({"iss": AUTH_SERVER, "sub": sub, "aud": aud, "scope": "read bots trade", "client_id": "c1",
                       "gid": gid, "iat": now, "exp": now + exp_in, "jti": jti},
                      key, algorithm="RS256", headers={"typ": "at+jwt", "kid": kid})


# --- order ids survive a token refresh -----------------------------------------

def _order_id(monkeypatch, bearer):
    monkeypatch.setattr(server, "_resolve_token", lambda: bearer)
    return server._mcp_order_id({"symbol": "AAPL", "side": "buy", "qty": 1}, "paper", None)


def test_a_refreshed_token_keeps_the_order_id(monkeypatch):
    server._recent_order_ids.clear()
    monkeypatch.setattr(server, "_wall_time", lambda: 1_000.0)
    first = _order_id(monkeypatch, token(jti="before-refresh"))
    assert _order_id(monkeypatch, token(jti="after-refresh")) == first
    assert _order_id(monkeypatch, token(gid="another-connection")) != first
    assert _order_id(monkeypatch, "vt_key_one") == _order_id(monkeypatch, "vt_key_one") != first


# --- /messages/ belongs to /sse -------------------------------------------------

def test_messages_points_at_the_sse_metadata():
    assert oauth_gate.resource_for("mcp.vibetrader.markets", "/messages/") == "https://mcp.vibetrader.markets/sse"
    inner = Starlette(routes=[Route("/messages/", lambda r: PlainTextResponse("x"), methods=["POST"])])
    gate = OAuthGate(inner, VibeTraderTokenVerifier(JWKSVerifier(lambda: _jwks(), issuer=AUTH_SERVER, audience=[AUD])))
    r = TestClient(gate, base_url="https://mcp.vibetrader.markets").post("/messages/?session_id=s")
    assert r.status_code == 401
    assert 'resource_metadata="https://mcp.vibetrader.markets/.well-known/oauth-protected-resource/sse"' in r.headers["www-authenticate"]


# --- resilient signing keys -----------------------------------------------------

async def _jwks(key=KEY, kid="k1"):
    return {"keys": [jwk_for(key.public_key(), kid)]}


class Fetcher:
    def __init__(self, result=None, fail=False, delay=0.0):
        self.calls, self.result, self.fail, self.delay = 0, result, fail, delay

    async def __call__(self):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("jwks down")
        return self.result or await _jwks()


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


def verifier(fetch, clock=None):
    return JWKSVerifier(fetch, issuer=AUTH_SERVER, audience=[AUD], clock=clock or Clock())


def check(v, t):
    return asyncio.run(v.verify_token(t))


def test_valid_wrong_audience_and_expired():
    v = verifier(Fetcher(), clock=time.time)   # expiry against the real clock
    ok = check(v, token())
    assert ok is not None and ok.client_id == "c1" and ok.scopes == ["read", "bots", "trade"]
    assert check(v, token(aud="https://evil.example/mcp")) is None
    assert check(v, token(exp_in=-3600)) is None
    assert check(v, "not-a-jwt") is None


def test_unknown_key_ids_cannot_cause_a_fetch_per_request():
    fetch, clock = Fetcher(), Clock()
    v = verifier(fetch, clock)
    check(v, token())                                # the first fetch
    for i in range(20):
        assert check(v, token(kid=f"forged{i}")) is None
    assert fetch.calls == 1                          # at most one fetch per 30 s
    clock.t += 31
    check(v, token(kid="forged-again"))
    assert fetch.calls == 2


def test_cached_keys_survive_a_failed_refresh():
    fetch, clock = Fetcher(), Clock()
    v = verifier(fetch, clock)
    assert check(v, token()) is not None
    fetch.fail = True
    clock.t += 7200                                  # cache is stale; the refresh fails
    assert check(v, token()) is not None


def test_one_fetch_for_many_concurrent_first_requests():
    fetch = Fetcher(delay=0.05)
    v = verifier(fetch)

    async def many():
        return await asyncio.gather(*(v.verify_token(token()) for _ in range(10)))
    assert all(asyncio.run(many())) and fetch.calls == 1


def test_no_keys_at_all_is_a_503_not_a_bad_token():
    v = verifier(Fetcher(fail=True))
    inner = Starlette(routes=[Route("/mcp", lambda r: PlainTextResponse("x"), methods=["POST"])])
    r = TestClient(OAuthGate(inner, VibeTraderTokenVerifier(v)), base_url="https://mcp.vibetrader.markets").post(
        "/mcp", headers={"authorization": f"Bearer {token()}"})
    assert r.status_code == 503 and "invalid_token" not in r.headers.get("www-authenticate", "")


# --- descriptions describe ------------------------------------------------------

def test_no_tool_description_directs_the_model_to_other_servers():
    async def go():
        async with Client(server.mcp) as c:
            return await c.list_tools()
    for tool in asyncio.run(go()):
        text = tool.description or ""
        assert "its MCP" not in text and "own tools" not in text, tool.name
