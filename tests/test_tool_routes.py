"""Tools must call API routes that exist (2026-10-11 review, P1-6).

get_quote, run_backtest, list_backtests and get_backtest called paths the API
never served — /trading/quote/{s} and /bots/{id}/backtest(s) — so every call
404'd (43 failures in 14 days). The real routes are /research/quote/{s} and,
under the backtest router's /backtest prefix, /backtest/bots/{id}/...
"""
import asyncio
import json

import server


class _RecordingClient:
    def __init__(self):
        self.calls = []

    async def get(self, endpoint):
        self.calls.append(("GET", endpoint))
        return []

    async def post(self, endpoint, data=None):
        self.calls.append(("POST", endpoint))
        return {}


def _call(tool, monkeypatch, **kwargs):
    client = _RecordingClient()
    monkeypatch.setattr(server, "get_client", lambda: client)
    out = asyncio.run(tool.fn(**kwargs))
    return client.calls, out


def test_get_quote_uses_the_research_route(monkeypatch):
    calls, _ = _call(server.get_quote, monkeypatch, symbol="AAPL")
    assert calls == [("GET", "/research/quote/AAPL")]


def test_run_backtest_uses_the_backtest_router(monkeypatch):
    calls, out = _call(server.run_backtest, monkeypatch, bot_id="b1")
    assert calls == [("POST", "/backtest/bots/b1/backtest")]
    assert json.loads(out)["success"] is True


def test_list_backtests_uses_the_backtest_router(monkeypatch):
    calls, _ = _call(server.list_backtests, monkeypatch, bot_id="b1")
    assert calls == [("GET", "/backtest/bots/b1/backtests")]


def test_get_backtest_uses_the_backtest_router(monkeypatch):
    calls, _ = _call(server.get_backtest, monkeypatch, bot_id="b1", backtest_id="bt1")
    assert calls == [("GET", "/backtest/bots/b1/backtests/bt1")]


def test_quick_covered_call_backtest_uses_the_backtest_router(monkeypatch):
    # Found by auditing every tool path against the API's route table — the
    # review's three broken tools were not the only ones.
    calls, _ = _call(server.quick_covered_call_backtest, monkeypatch, symbol="AAPL")
    assert calls[0][0] == "GET"
    assert calls[0][1].startswith("/backtest/covered-call/quick?symbol=AAPL")
