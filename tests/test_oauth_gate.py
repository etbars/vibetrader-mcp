"""The server is an OAuth protected resource (spec 2026-10-21 §7): a request
without a usable token gets 401 pointing at VibeTrader sign-in, the metadata
names the exact URL the user entered, and keys sent as a header still work."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from oauth_gate import AUTH_SERVER, OAuthGate, VibeTraderTokenVerifier, resource_for

BRANDED = "mcp.vibetrader.markets"
RUN_APP = "vibetrader-mcp-289016366682.us-central1.run.app"
AUDIENCES = [f"https://{h}{p}" for h in (BRANDED, RUN_APP) for p in ("/mcp", "/sse")]

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_PEM = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def token(aud=f"https://{BRANDED}/mcp", exp_in=3600, key=KEY):
    now = int(time.time())
    claims = {"iss": AUTH_SERVER, "sub": "u1", "aud": aud, "scope": "read bots", "client_id": "c1",
              "gid": "g1", "iat": now, "exp": now + exp_in, "jti": "j"}
    return jwt.encode(claims, key, algorithm="RS256", headers={"typ": "at+jwt", "kid": "k1"})


async def echo(request):
    return PlainTextResponse(f"inner {request.url.path}")


@pytest.fixture
def client():
    inner = Starlette(routes=[Route(p, echo, methods=["GET", "POST", "OPTIONS"])
                              for p in ("/mcp", "/sse", "/messages/", "/health")])
    verifier = VibeTraderTokenVerifier(JWTVerifier(public_key=PUBLIC_PEM, issuer=AUTH_SERVER,
                                                   audience=AUDIENCES, algorithm="RS256"))
    return TestClient(OAuthGate(inner, verifier), base_url=f"https://{BRANDED}")


def challenge(r):
    assert r.status_code == 401
    return r.headers["www-authenticate"]


def test_no_token_is_a_401_naming_the_metadata(client):
    header = challenge(client.post("/mcp"))
    assert header == (f'Bearer resource_metadata="https://{BRANDED}/.well-known/oauth-protected-resource/mcp", '
                      'scope="read bots trade"')


def test_the_challenge_follows_the_host_and_path(client):
    header = challenge(client.post("/sse", headers={"host": RUN_APP}))
    assert f'resource_metadata="https://{RUN_APP}/.well-known/oauth-protected-resource/sse"' in header
    header = challenge(client.post("/mcp", headers={"host": "MCP.VibeTrader.Markets:443"}))
    assert f'resource_metadata="https://{BRANDED}/.well-known/oauth-protected-resource/mcp"' in header


def test_messages_route_is_protected_too(client):
    challenge(client.post("/messages/?session_id=x"))


@pytest.mark.parametrize("host", [BRANDED, RUN_APP])
@pytest.mark.parametrize("path", ["/mcp", "/sse"])
def test_protected_resource_metadata(client, host, path):
    r = client.get(f"/.well-known/oauth-protected-resource{path}", headers={"host": host})
    assert r.status_code == 200
    assert r.json() == {"resource": f"https://{host}{path}", "authorization_servers": [AUTH_SERVER],
                        "scopes_supported": ["read", "bots", "trade"], "bearer_methods_supported": ["header"],
                        "resource_name": "VibeTrader"}


def test_root_metadata_names_the_mcp_endpoint(client):
    assert client.get("/.well-known/oauth-protected-resource").json()["resource"] == f"https://{BRANDED}/mcp"


def test_an_api_key_header_passes(client):
    r = client.post("/mcp", headers={"authorization": "Bearer vt_abc"})
    assert r.status_code == 200 and r.text == "inner /mcp"


def test_a_valid_token_passes(client):
    assert client.post("/mcp", headers={"authorization": f"Bearer {token()}"}).text == "inner /mcp"
    run_app = token(aud=f"https://{RUN_APP}/sse")
    assert client.post("/sse", headers={"authorization": f"Bearer {run_app}", "host": RUN_APP}).status_code == 200


@pytest.mark.parametrize("bad", [
    lambda: token(aud="https://evil.example/mcp"),
    lambda: token(exp_in=-3600),
    lambda: token(key=rsa.generate_private_key(public_exponent=65537, key_size=2048)),
    lambda: "not-a-token",
])
def test_a_bad_token_is_invalid_token(client, bad):
    header = challenge(client.post("/mcp", headers={"authorization": f"Bearer {bad()}"}))
    assert 'error="invalid_token"' in header


def test_preflight_and_other_paths_pass(client):
    assert client.options("/mcp").status_code == 200
    assert client.get("/health").text == "inner /health"


def test_resource_for():
    assert resource_for("mcp.vibetrader.markets", "/sse") == f"https://{BRANDED}/sse"
    assert resource_for("elsewhere.example", "/mcp") == f"https://{BRANDED}/mcp"
    assert resource_for(f"{RUN_APP}:8080", "/messages/") == f"https://{RUN_APP}/sse"   # the SSE transport's route


def test_the_real_server_requires_a_token():
    import server
    r = TestClient(server.app, base_url=f"https://{BRANDED}").post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"accept": "application/json, text/event-stream"})
    assert r.status_code == 401
