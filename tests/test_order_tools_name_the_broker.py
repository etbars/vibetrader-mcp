"""Order tools must say which account they trade (2026-10-18 SnapTrade audit).

place_order and close_position sent no `broker`, so the API fell back to its
"alpaca" default. A user who links Robinhood or Webull through SnapTrade and
asks "sell my Robinhood AAPL" could get close_position(mode="live"), which
sells their live *Alpaca* AAPL instead. These tools only ever reach the Alpaca
account, so they must send broker=alpaca explicitly and say so to the model.
"""
import asyncio

import server


class _RecordingClient:
    def __init__(self):
        self.calls = []

    async def post(self, endpoint, data=None):
        self.calls.append(("POST", endpoint))
        return {}


def _post_endpoint(tool, monkeypatch, **kwargs):
    client = _RecordingClient()
    monkeypatch.setattr(server, "get_client", lambda: client)
    asyncio.run(tool.fn(**kwargs))
    assert len(client.calls) == 1
    return client.calls[0][1]


def test_close_position_names_the_alpaca_broker(monkeypatch):
    endpoint = _post_endpoint(server.close_position, monkeypatch,
                              symbol="AAPL", mode="live")
    assert endpoint.startswith("/trading/portfolio/close/AAPL?")
    assert "broker=alpaca" in endpoint
    assert "mode=live" in endpoint


def test_place_order_names_the_alpaca_broker(monkeypatch):
    endpoint = _post_endpoint(server.place_order, monkeypatch,
                              symbol="AAPL", side="buy", qty=1, mode="live")
    assert endpoint.startswith("/trading/portfolio/order?")
    assert "broker=alpaca" in endpoint
    assert "mode=live" in endpoint


def test_order_tools_tell_the_model_they_only_trade_alpaca():
    for tool in (server.close_position, server.place_order):
        description = tool.description or ""
        assert "Alpaca account" in description, tool.name
        assert "Robinhood" in description, (
            f"{tool.name} must warn that other linked brokerages are not "
            f"reachable, or the model will assume they are"
        )
