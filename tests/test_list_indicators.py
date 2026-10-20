"""list_indicators reads the API's public indicator catalog (P3 Task 15).

GET /indicators/catalog needs no account, so the tool sends no token: it
works before `authenticate`, and a caller's key never rides on a public read.
"""
import asyncio
import json

import pytest

import server

CATALOG = {"version": "abc123def456", "indicators": [
    {"name": "rsi", "family": "momentum", "label_template": "RSI({length})", "params": [], "outputs": []},
    {"name": "stoch", "family": "momentum", "label_template": "Stochastic({length}, {smooth_k}, {smooth_d})",
     "params": [], "outputs": []},
    {"name": "sma", "family": "moving_average", "label_template": "SMA({length})", "params": [], "outputs": []},
]}


class _Response:
    status_code = 200
    text = "{}"

    def json(self):
        return CATALOG


class _AsyncClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        _AsyncClient.calls.append((url, kwargs))
        return _Response()


@pytest.fixture
def http(monkeypatch):
    _AsyncClient.calls = []
    monkeypatch.setattr(server.httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(server, "get_client", lambda: pytest.fail("list_indicators must not need an account"))
    return _AsyncClient.calls


def test_it_reads_the_public_catalog_without_a_token(http):
    out = json.loads(asyncio.run(server.list_indicators.fn()))
    assert [e["name"] for e in out["indicators"]] == ["rsi", "stoch", "sma"]
    assert out["version"] == "abc123def456"
    (url, kwargs), = http
    assert url == f"{server.API_BASE_URL}/indicators/catalog"
    assert "Authorization" not in (kwargs.get("headers") or {})


def test_family_filters(http):
    out = json.loads(asyncio.run(server.list_indicators.fn(family="Momentum")))
    assert [e["name"] for e in out["indicators"]] == ["rsi", "stoch"]


def test_an_unknown_family_names_the_real_ones(http):
    out = json.loads(asyncio.run(server.list_indicators.fn(family="candles")))
    assert out["indicators"] == [] and out["families"] == ["momentum", "moving_average"]
