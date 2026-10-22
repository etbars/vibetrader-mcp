"""Hardening pass, 2026-10-22 (the sign-in review's deferred items).

- A session belongs to whoever opened it: the gate names the signed-in user to
  the MCP SDK, which then answers anyone else's request on that session as if
  the session didn't exist (404). A refreshed token is the same user; each API
  key is its own owner.
- A header that isn't UTF-8 is a 401, not a crash.
- A retried close_position never sells twice: like place_order it sends a
  client_order_id that a retry reuses, and the API answers a repeat with 409.
"""
import asyncio
import hashlib
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp.exceptions import ToolError
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, authorization_context
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import server
from oauth_gate import AUTH_SERVER, JWKSVerifier, OAuthGate, VibeTraderTokenVerifier, jwk_for

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
BRANDED = "mcp.vibetrader.markets"
AUD = f"https://{BRANDED}/mcp"


def token(sub="u1", jti="a", client_id="https://claude.ai/oauth/mcp-oauth-client-metadata"):
    now = int(time.time())
    return jwt.encode({"iss": AUTH_SERVER, "sub": sub, "aud": AUD, "scope": "read bots", "client_id": client_id,
                       "gid": "g1", "iat": now, "exp": now + 3600, "jti": jti},
                      KEY, algorithm="RS256", headers={"typ": "at+jwt", "kid": "k1"})


def real_verifier():
    async def fetch():
        return {"keys": [jwk_for(KEY.public_key(), "k1")]}
    return VibeTraderTokenVerifier(JWKSVerifier(fetch, issuer=AUTH_SERVER, audience=[AUD]))


# --- the gate names the user ------------------------------------------------------

def _owner_seen(bearer):
    seen = {}

    async def record(request):
        seen["user"] = request.scope.get("user")
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/mcp", record, methods=["POST"])])
    r = TestClient(OAuthGate(inner, real_verifier()), base_url=f"https://{BRANDED}").post(
        "/mcp", headers={"authorization": f"Bearer {bearer}"})
    assert r.status_code == 200, r.text
    user = seen["user"]
    assert isinstance(user, AuthenticatedUser)
    return authorization_context(user)


def test_a_signed_in_user_owns_their_sessions_across_refreshes():
    first = _owner_seen(token(jti="a"))
    assert first["subject"] == "u1" and first["issuer"] == AUTH_SERVER
    assert _owner_seen(token(jti="b")) == first            # the hourly refresh: same owner
    assert _owner_seen(token(sub="u2")) != first           # another person
    assert _owner_seen(token(client_id="https://chatgpt.com/oauth/client.json")) != first   # another app


def test_each_api_key_owns_its_own_sessions():
    a = _owner_seen("vt_aaaa")
    assert a == _owner_seen("vt_aaaa")
    assert a != _owner_seen("vt_bbbb")
    assert a["subject"] == "key:" + hashlib.sha256(b"vt_aaaa").hexdigest()


ACCEPT = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def test_another_user_cannot_use_a_session_on_the_real_server():
    with TestClient(OAuthGate(server._app, real_verifier()), base_url=f"https://{BRANDED}") as c:
        opened = c.post("/mcp", json=INIT, headers={**ACCEPT, "authorization": f"Bearer {token()}"})
        assert opened.status_code == 200, opened.text
        sid = opened.headers["mcp-session-id"]
        ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}

        theirs = c.post("/mcp", json=ping, headers={**ACCEPT, "mcp-session-id": sid,
                                                    "authorization": f"Bearer {token(sub='u2')}"})
        assert theirs.status_code == 404
        a_key = c.post("/mcp", json=ping, headers={**ACCEPT, "mcp-session-id": sid,
                                                   "authorization": "Bearer vt_someone"})
        assert a_key.status_code == 404
        mine = c.post("/mcp", json=ping, headers={**ACCEPT, "mcp-session-id": sid,
                                                  "authorization": f"Bearer {token(jti='refreshed')}"})
        assert mine.status_code == 200, mine.text


# --- headers that aren't UTF-8 ------------------------------------------------------
# Called at the ASGI level: the test client re-encodes header bytes as UTF-8,
# so it can't send what a raw HTTP client can.

def _raw_post(headers):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner"})

    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "method": "POST", "path": "/mcp", "query_string": b"",
             "headers": [(b"host", BRANDED.encode()), *headers]}
    asyncio.run(OAuthGate(inner, real_verifier())(scope, receive, send))
    start = messages[0]
    return start["status"], {k.decode(): v.decode("latin-1") for k, v in start["headers"]}


def test_a_non_utf8_token_is_a_401_not_a_crash():
    status, headers = _raw_post([(b"authorization", b"Bearer \xff\xfe")])
    assert status == 401 and 'error="invalid_token"' in headers["www-authenticate"]


def test_a_non_utf8_header_elsewhere_doesnt_matter():
    status, _ = _raw_post([(b"authorization", b"Bearer vt_abc"), (b"x-junk", b"\xff")])
    assert status == 200


# --- close_position is retry-safe ------------------------------------------------

class _Recording:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    async def post(self, endpoint, data=None):
        self.calls.append((endpoint, data))
        if self.error:
            raise self.error
        return {"symbol": "AAPL", "closed_qty": 5}


@pytest.fixture(autouse=True)
def _fresh_order_memory():
    server._recent_order_ids.clear()
    yield
    server._recent_order_ids.clear()


def _close_cid(monkeypatch, at, **kwargs):
    client = _Recording()
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_wall_time", lambda: at)
    asyncio.run(server.close_position.fn(**{"symbol": "AAPL", "qty": 5, **kwargs}))
    (_, data), = client.calls
    return data["client_order_id"]


def test_close_sends_an_mcp_client_order_id(monkeypatch):
    cid = _close_cid(monkeypatch, 1_000_000.0)
    assert cid.startswith("vt_ticket_mcp_") and len(cid) <= 128


def test_a_retried_close_reuses_its_id(monkeypatch):
    assert _close_cid(monkeypatch, 1_000_000.0) == _close_cid(monkeypatch, 1_000_030.0)


def test_a_different_close_gets_a_different_id(monkeypatch):
    first = _close_cid(monkeypatch, 1_000_000.0)
    assert first != _close_cid(monkeypatch, 1_000_000.0, qty=6)
    assert first != _close_cid(monkeypatch, 1_000_000.0, percentage=50, qty=None)
    assert first != _close_cid(monkeypatch, 1_000_000.0, symbol="MSFT")
    assert first != _close_cid(monkeypatch, 1_000_000.0, mode="live")
    assert first != _close_cid(monkeypatch, 1_000_000.0, idempotency_key="trim-2")


def test_a_close_never_shares_an_id_with_the_same_sized_sell_order(monkeypatch):
    close = _close_cid(monkeypatch, 1_000_000.0)
    client = _Recording()
    monkeypatch.setattr(server, "get_client", lambda: client)
    asyncio.run(server.place_order.fn(symbol="AAPL", side="sell", qty=5))
    assert client.calls[0][1]["client_order_id"] != close


def test_a_repeated_close_says_it_was_already_sent(monkeypatch):
    client = _Recording(server.ApiError(409, "This close was already sent. Check your positions before closing again."))
    monkeypatch.setattr(server, "get_client", lambda: client)
    with pytest.raises(ToolError, match="nothing more was sold"):
        asyncio.run(server.close_position.fn(symbol="AAPL", qty=5))


def test_a_close_timeout_says_it_may_have_happened(monkeypatch):
    client = _Recording(server.ApiUnreachable("ReadTimeout"))
    monkeypatch.setattr(server, "get_client", lambda: client)
    with pytest.raises(ToolError, match="may or may not have"):
        asyncio.run(server.close_position.fn(symbol="AAPL", qty=5))
