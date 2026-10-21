"""Signal mode over MCP: the companion loop for brokerages VibeTrader can't trade.

A bot in signal mode watches the market and decides; the user places each
trade wherever their money is (Robinhood through its own MCP, say) and tells
the bot what filled. The API has served this since the 2026-10-14 signal-mode
spec (/signals/bots/...); these tools put it in an agent's hands.
"""
import asyncio
import json

import pytest
from fastmcp.exceptions import ToolError

import server


class _RecordingClient:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    async def _do(self, method, endpoint, data=None):
        self.calls.append((method, endpoint, data))
        return self.responses.get(endpoint, {})

    async def get(self, endpoint):
        return await self._do("GET", endpoint)

    async def post(self, endpoint, data=None):
        return await self._do("POST", endpoint, data)


def _run(monkeypatch, tool, responses=None, **kwargs):
    client = _RecordingClient(responses)
    monkeypatch.setattr(server, "get_client", lambda: client)
    out = asyncio.run(tool.fn(**kwargs))
    return client.calls, out


OPEN_BUY = {"decision_id": "AAPL-1760965200-in", "side": "buy", "symbol": "AAPL", "status": "open",
            "signal_price": 181.0, "suggested_qty": 5, "reason_text": "RSI(14) crossed below 30",
            "planned_exits": ["Take profit at +5%"], "created_at": "2026-10-20T14:00:00Z",
            "delivered": {"push": True}, "delivery_attempts": 1}
PLACING = {**OPEN_BUY, "decision_id": "MSFT-1760965200-in", "symbol": "MSFT",
           "auto": {"state": "submitted"}}
DONE = {**OPEN_BUY, "decision_id": "NVDA-1760960000-in", "symbol": "NVDA", "status": "bought"}
STATE = {"bot_id": "b1", "entry_mode": "alert", "exit_mode": "alert", "follow_account": "manual",
         "status": "running", "signals": [OPEN_BUY, PLACING, DONE], "book": [], "results": {"trades": 3}}


# --- set_signal_mode ------------------------------------------------------------

def test_set_signal_mode_defaults_to_alerts_the_user_places_anywhere(monkeypatch):
    # A bot that isn't a signal bot yet has no account: "manual" is any brokerage.
    calls, _ = _run(monkeypatch, server.set_signal_mode, {"/signals/bots/b1": {"follow_account": None}}, bot_id="b1")
    assert calls[-1] == ("POST", "/signals/bots/b1/mode",
                         {"entry_mode": "alert", "exit_mode": "alert", "follow_account": "manual"})


def test_set_signal_mode_passes_capital_and_account(monkeypatch):
    calls, _ = _run(monkeypatch, server.set_signal_mode, bot_id="b1", entry_mode="alert", exit_mode="auto",
                    follow_account="alpaca_paper", signal_capital=5000)
    assert calls == [("POST", "/signals/bots/b1/mode", {"entry_mode": "alert", "exit_mode": "auto",
                                                         "follow_account": "alpaca_paper", "signal_capital": 5000})]


def test_changing_one_side_keeps_the_account_and_capital(monkeypatch):
    # The mode route replaces the whole setup: a field left out is written as
    # missing. Changing one side must not repoint a bot that follows Alpaca.
    state = {"follow_account": "alpaca_paper", "signal_capital": 5000.0}
    calls, _ = _run(monkeypatch, server.set_signal_mode, {"/signals/bots/b1": state}, bot_id="b1", exit_mode="auto")
    assert calls[-1][2] == {"entry_mode": "alert", "exit_mode": "auto", "follow_account": "alpaca_paper",
                            "signal_capital": 5000.0}


# --- list_open_signals ----------------------------------------------------------

def test_list_open_signals_for_one_bot_shows_only_what_the_user_can_act_on(monkeypatch):
    calls, out = _run(monkeypatch, server.list_open_signals, {"/signals/bots/b1": STATE}, bot_id="b1")
    out = json.loads(out)
    assert calls == [("GET", "/signals/bots/b1", None)]
    assert [s["decision_id"] for s in out["open_signals"]] == ["AAPL-1760965200-in"]
    signal = out["open_signals"][0]
    assert signal["bot_id"] == "b1"
    assert signal["suggested_qty"] == 5 and signal["signal_price"] == 181.0
    assert "delivered" not in signal and "delivery_attempts" not in signal
    assert out["being_placed_by_bot"] == 1


def test_list_open_signals_across_bots_reads_only_signal_bots(monkeypatch):
    bots = [{"id": "b1", "name": "Dip buyer", "entry_mode": "alert", "exit_mode": "alert"},
            {"id": "b2", "name": "Plain bot", "entry_mode": "auto", "exit_mode": "auto"},
            {"id": "b3", "name": "Exit alerts", "entry_mode": "auto", "exit_mode": "alert"}]
    calls, out = _run(monkeypatch, server.list_open_signals,
                      {"/bots/": bots, "/signals/bots/b1": STATE, "/signals/bots/b3": {"signals": []}})
    read = sorted(endpoint for _, endpoint, _ in calls)
    assert read == ["/bots/", "/signals/bots/b1", "/signals/bots/b3"]
    out = json.loads(out)
    assert out["open_signals"][0]["bot_name"] == "Dip buyer"
    assert out["count"] == 1


def test_list_open_signals_tells_the_agent_to_report_back(monkeypatch):
    assert "report_signal" in server.list_open_signals.description


# --- report_signal --------------------------------------------------------------

def test_report_signal_bought_records_the_fill(monkeypatch):
    calls, _ = _run(monkeypatch, server.report_signal, bot_id="b1", decision_id="AAPL-1760965200-in",
                    action="bought", qty=5, price=180.95)
    assert calls == [("POST", "/signals/bots/b1/signals/AAPL-1760965200-in/bought", {"qty": 5, "price": 180.95})]


def test_report_signal_sold_records_the_fill(monkeypatch):
    calls, _ = _run(monkeypatch, server.report_signal, bot_id="b1", decision_id="AAPL-1760965200-out-all",
                    action="sold", qty=5, price=190.1)
    assert calls[0][1].endswith("/AAPL-1760965200-out-all/sold")


@pytest.mark.parametrize("action", ["skip", "keep", "undo"])
def test_report_signal_actions_without_a_fill(monkeypatch, action):
    calls, _ = _run(monkeypatch, server.report_signal, bot_id="b1", decision_id="d1", action=action)
    assert calls == [("POST", f"/signals/bots/b1/signals/d1/{action}", None)]


@pytest.mark.parametrize("missing", [{"qty": 5}, {"price": 180.0}, {}])
def test_a_fill_needs_both_qty_and_price(monkeypatch, missing):
    with pytest.raises(ToolError, match="qty and price"):
        _run(monkeypatch, server.report_signal, bot_id="b1", decision_id="d1", action="bought", **missing)


def test_report_signal_rejects_an_unknown_action(monkeypatch):
    with pytest.raises(ToolError, match="bought, sold, skip, keep or undo"):
        _run(monkeypatch, server.report_signal, bot_id="b1", decision_id="d1", action="filled")


def test_ids_are_escaped_in_the_path(monkeypatch):
    calls, _ = _run(monkeypatch, server.report_signal, bot_id="b1", decision_id="a/b", action="skip")
    assert calls[0][1] == "/signals/bots/b1/signals/a%2Fb/skip"


# --- options and shorts ---------------------------------------------------------

def test_report_option_signal(monkeypatch):
    calls, _ = _run(monkeypatch, server.report_option_signal, bot_id="b1", decision_id="d1",
                    action="sold", qty=1, price=2.35)
    assert calls == [("POST", "/signals/bots/b1/signals/d1/option", {"action": "sold", "qty": 1, "price": 2.35})]


def test_report_option_signal_without_a_fill(monkeypatch):
    calls, _ = _run(monkeypatch, server.report_option_signal, bot_id="b1", decision_id="d1", action="expired")
    assert calls[0][2] == {"action": "expired"}


def test_report_short_signal(monkeypatch):
    calls, _ = _run(monkeypatch, server.report_short_signal, bot_id="b1", decision_id="d1",
                    action="covered", qty=10, price=40.5)
    assert calls == [("POST", "/signals/bots/b1/signals/d1/short", {"action": "covered", "qty": 10, "price": 40.5})]


def test_report_short_signal_rejects_an_unknown_action(monkeypatch):
    with pytest.raises(ToolError, match="shorted, skip, covered or keep"):
        _run(monkeypatch, server.report_short_signal, bot_id="b1", decision_id="d1", action="sold")


# --- state and preview ----------------------------------------------------------

def test_get_signal_state_summarizes_the_bot(monkeypatch):
    calls, out = _run(monkeypatch, server.get_signal_state, {"/signals/bots/b1": STATE}, bot_id="b1")
    out = json.loads(out)
    assert calls == [("GET", "/signals/bots/b1", None)]
    assert out["entry_mode"] == "alert" and out["follow_account"] == "manual"
    assert out["open_signal_count"] == 2
    assert "signals" not in out
    assert [s["decision_id"] for s in out["recent_signals"]][0] == "AAPL-1760965200-in"


def test_preview_bot_reads_the_quick_backtest(monkeypatch):
    calls, _ = _run(monkeypatch, server.preview_bot, bot_id="b1")
    assert calls == [("GET", "/backtest/bots/b1/preview", None)]
