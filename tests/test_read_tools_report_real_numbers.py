"""The read tools report the numbers the app shows (2026-10-22 audit 15-F8, 05-F17).

- get_daily_trade_stats read `trade_count` / `total_pnl`; the API sends
  `total_trades` / `realized_pnl`, so every summary said 0 trades and $0.00.
- get_bot_trades read snake_case keys from a camelCase response (entry, exit,
  P&L % and time were all null), divided wins by every row (buys included,
  about half the true win rate) and labelled the last `limit` rows' P&L as
  the bot's total.
- list_bots / get_bot_stats called realized P&L "total_pnl" and crashed on a
  null; get_trade_history read a price key the API doesn't send.
- Backtest results dropped the buy-and-hold comparison and the rules the
  backtest couldn't model; docstrings promised a 30-day limit that isn't there.

The API's keys are pinned to both shapes: main as of f2d6ce0a, and main after
the P&L fix (T13) adds realized_pnl / unrealized_pnl / all_time_pnl.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import server


class _Api:
    """Answers each GET by its path (the query string is recorded, not matched)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def get(self, endpoint):
        self.calls.append(("GET", endpoint))
        return self.routes[endpoint.split("?")[0]]

    async def post(self, endpoint, data=None):
        self.calls.append(("POST", endpoint))
        return self.routes[endpoint.split("?")[0]]


def _run(monkeypatch, tool, routes, **kwargs):
    api = _Api(routes)
    monkeypatch.setattr(server, "get_client", lambda: api)
    return api, json.loads(asyncio.run(tool.fn(**kwargs)))


def _day(n):
    return (datetime.now(timezone.utc).date() - timedelta(days=n)).isoformat()


# The bot as GET /bots/{id} answers on main today, and after the P&L fix.
BOT_MAIN = {"id": "b1", "name": "Dip", "total_pnl": 120.5, "today_pnl": 3.0, "total_trades": 9,
            "winning_trades": 3, "losing_trades": 1, "win_rate": 75.0}
BOT_T13 = {**BOT_MAIN, "realized_pnl": 120.5, "unrealized_pnl": -20.25, "all_time_pnl": 100.25,
           "open_positions": 1, "unpriced_symbols": []}


# --- get_daily_trade_stats ------------------------------------------------------

DAILY = [  # newest first, as the API sorts
    {"date": _day(0), "total_trades": 2, "buy_trades": 1, "sell_trades": 1, "total_volume": 2000.0,
     "realized_pnl": 50.0, "winning_trades": 1, "losing_trades": 0},
    {"date": _day(3), "total_trades": 3, "buy_trades": 1, "sell_trades": 2, "total_volume": 3000.0,
     "realized_pnl": -30.0, "winning_trades": 1, "losing_trades": 1},
    {"date": _day(40), "total_trades": 4, "buy_trades": 2, "sell_trades": 2, "total_volume": 4000.0,
     "realized_pnl": 999.0, "winning_trades": 2, "losing_trades": 0},
]


def test_daily_stats_read_total_trades_and_realized_pnl(monkeypatch):
    api, out = _run(monkeypatch, server.get_daily_trade_stats,
                    {"/trading/bots/b1/trades/stats/daily": DAILY}, bot_id="b1", days=30)
    s = out["summary"]
    assert s["total_trades"] == 5            # the 40-day-old row is outside 30 days
    assert s["realized_pnl"] == 20.0
    assert (s["winning_trades"], s["losing_trades"], s["closed_trades"]) == (2, 1, 3)
    assert s["win_rate"] == 66.7
    assert s["best_day"] == {"date": _day(0), "realized_pnl": 50.0}
    assert s["worst_day"] == {"date": _day(3), "realized_pnl": -30.0}
    assert len(out["daily_stats"]) == 2
    # the server is asked for the window too (honoured once the P&L fix lands)
    assert f"start_date={_day(29)}" in api.calls[0][1]


def test_daily_stats_with_no_closed_trades_has_no_win_rate(monkeypatch):
    rows = [{**DAILY[0], "realized_pnl": 0.0, "winning_trades": 0, "losing_trades": 0}]
    _, out = _run(monkeypatch, server.get_daily_trade_stats,
                  {"/trading/bots/b1/trades/stats/daily": rows}, bot_id="b1")
    assert out["summary"]["win_rate"] is None


# --- get_bot_trades -------------------------------------------------------------

TRADES = [  # TradeSummary rows: camelCase, newest first
    {"id": "t3", "symbol": "AAPL", "side": "sell", "qty": 2, "filledPrice": 110.0, "entryPrice": 100.0,
     "pnl": 20.0, "pnlPercent": 10.0, "status": "filled", "createdAt": "2026-10-20T15:00:00Z",
     "triggerReason": "take profit"},
    {"id": "t2", "symbol": "AAPL", "side": "buy", "qty": 2, "filledPrice": 100.0, "entryPrice": None,
     "pnl": None, "pnlPercent": None, "status": "filled", "createdAt": "2026-10-19T15:00:00Z"},
    {"id": "t1", "symbol": "MSFT", "side": "sell", "qty": 1, "filledPrice": 95.0, "entryPrice": 100.0,
     "pnl": -5.0, "pnlPercent": -5.0, "status": "filled", "createdAt": "2026-10-18T15:00:00Z"},
]


@pytest.mark.parametrize("bot", [BOT_MAIN, BOT_T13], ids=["main", "after-pnl-fix"])
def test_bot_trades_read_the_camelcase_rows(monkeypatch, bot):
    _, out = _run(monkeypatch, server.get_bot_trades,
                  {"/trading/bots/b1/trades": TRADES, "/bots/b1": bot}, bot_id="b1")
    sell, buy = out["trades"][0], out["trades"][1]
    assert (sell["entry_price"], sell["exit_price"], sell["pnl_percent"]) == (100.0, 110.0, 10.0)
    assert sell["time"] == "2026-10-20T15:00:00Z"
    assert sell["reason"] == "take profit"
    assert buy["price"] == 100.0 and buy["pnl"] is None and "exit_price" not in buy


@pytest.mark.parametrize("bot", [BOT_MAIN, BOT_T13], ids=["main", "after-pnl-fix"])
def test_bot_trades_totals_come_from_the_bot_not_the_page(monkeypatch, bot):
    _, out = _run(monkeypatch, server.get_bot_trades,
                  {"/trading/bots/b1/trades": TRADES, "/bots/b1": bot}, bot_id="b1")
    s = out["summary"]
    assert s["realized_pnl"] == 120.5        # the bot's counter, not the 3 rows' +15
    assert s["total_trades"] == 9
    assert (s["winning_trades"], s["losing_trades"], s["closed_trades"]) == (3, 1, 4)
    assert s["win_rate"] == 75.0             # wins / closed, not wins / every row
    page = out["shown"]
    assert page == {"rows": 3, "closed": 2, "realized_pnl": 15.0}


def test_bot_trades_carry_open_pnl_once_the_api_sends_it(monkeypatch):
    _, out = _run(monkeypatch, server.get_bot_trades,
                  {"/trading/bots/b1/trades": TRADES, "/bots/b1": BOT_T13}, bot_id="b1")
    assert out["summary"]["unrealized_pnl"] == -20.25
    assert out["summary"]["all_time_pnl"] == 100.25


def test_bot_trades_win_rate_falls_back_to_wins_over_closed(monkeypatch):
    bot = {k: v for k, v in BOT_MAIN.items() if k != "win_rate"}
    _, out = _run(monkeypatch, server.get_bot_trades,
                  {"/trading/bots/b1/trades": TRADES, "/bots/b1": bot}, bot_id="b1")
    assert out["summary"]["win_rate"] == 75.0


# --- list_bots / get_bot_stats ---------------------------------------------------

def test_list_bots_labels_pnl_as_realized(monkeypatch):
    bots = [{**BOT_MAIN, "status": "running", "trading_mode": "paper"},
            {**BOT_T13, "id": "b2", "status": "running", "trading_mode": "live"},
            {**BOT_MAIN, "id": "b3", "total_pnl": None, "status": "paused", "trading_mode": "paper"}]
    _, out = _run(monkeypatch, server.list_bots, {"/bots/": bots})
    rows = {r["id"]: r for r in out["bots"]}
    assert rows["b1"]["realized_pnl"] == 120.5 and "total_pnl" not in rows["b1"]
    assert "all_time_pnl" not in rows["b1"]                  # main: not sent, not invented
    assert rows["b2"]["all_time_pnl"] == 100.25 and rows["b2"]["unrealized_pnl"] == -20.25
    assert rows["b3"]["realized_pnl"] == 0
    assert rows["b1"]["win_rate"] == 75.0


def test_bot_stats_sums_realized_and_open_pnl(monkeypatch):
    bots = [{**BOT_T13, "status": "running", "trading_mode": "paper"},
            {**BOT_T13, "id": "b2", "name": "Loser", "realized_pnl": -10.0, "total_pnl": -10.0,
             "unrealized_pnl": 0.0, "all_time_pnl": -10.0, "winning_trades": 0, "losing_trades": 2,
             "status": "paused", "trading_mode": "live"}]
    _, out = _run(monkeypatch, server.get_bot_stats, {"/bots/": bots})
    assert out["realized_pnl"] == 110.5
    assert out["unrealized_pnl"] == -20.25
    assert out["all_time_pnl"] == 90.25
    assert out["win_rate"] == 50.0           # 3 wins / 6 closed across bots
    assert out["top_performer"]["name"] == "Dip"
    assert out["worst_performer"]["name"] == "Loser"
    assert "total_pnl" not in out


def test_bot_stats_on_main_and_with_a_null_pnl(monkeypatch):
    bots = [{**BOT_MAIN, "status": "running", "trading_mode": "paper"},
            {**BOT_MAIN, "id": "b2", "name": "New", "total_pnl": None, "winning_trades": 0,
             "losing_trades": 0, "status": "running", "trading_mode": "paper"}]
    _, out = _run(monkeypatch, server.get_bot_stats, {"/bots/": bots})
    assert out["realized_pnl"] == 120.5
    assert out["unrealized_pnl"] is None and out["all_time_pnl"] is None   # the API doesn't send it yet
    assert out["profitable_bots"] == 1


def test_bot_stats_says_when_some_open_lots_are_unpriced(monkeypatch):
    bots = [{**BOT_T13, "status": "running", "trading_mode": "paper"},
            {**BOT_T13, "id": "b2", "unrealized_pnl": None, "all_time_pnl": None,
             "unpriced_symbols": ["XYZ"], "status": "running", "trading_mode": "paper"}]
    _, out = _run(monkeypatch, server.get_bot_stats, {"/bots/": bots})
    assert out["all_time_pnl"] is None
    assert "XYZ" in out["note"]


# --- get_trade_history -----------------------------------------------------------

def test_trade_history_reads_the_fill_price_and_keeps_a_zero_pnl(monkeypatch):
    rows = [{"id": "t1", "bot_id": "b1", "bot_name": "Dip", "symbol": "AAPL", "side": "sell", "qty": 1,
             "price": 101.0, "filled_price": 100.5, "pnl": 0.0, "timestamp": "2026-10-20T15:00:00Z"},
            {"id": "t0", "bot_id": "b1", "bot_name": "Dip", "symbol": "AAPL", "side": "buy", "qty": 1,
             "price": 100.5, "filled_price": None, "pnl": None, "timestamp": "2026-10-19T15:00:00Z"}]
    _, out = _run(monkeypatch, server.get_trade_history, {"/bots/trades/recent": rows})
    assert out["trades"][0]["price"] == 100.5
    assert out["trades"][0]["pnl"] == 0.0
    assert out["trades"][1]["price"] == 100.5 and out["trades"][1]["pnl"] is None


# --- backtests (05-F17) ----------------------------------------------------------

BACKTEST = {"id": "bt1", "start_date": "2026-01-01", "end_date": "2026-10-01", "initial_capital": 10000,
            "final_value": 10140.0, "total_return": 1.4, "buy_hold_return_pct": 19.2, "total_pnl": 140.0,
            "max_drawdown_percent": 2.0, "total_trades": 2, "winning_trades": 2, "losing_trades": 0,
            "win_rate": 100.0, "avg_trade_pnl": 70.0, "avg_win": 70.0, "avg_loss": 0.0, "best_trade": 90.0,
            "worst_trade": 50.0, "trades": [], "equity_curve": [], "ignored_conditions": ["vix_level"]}


def test_run_backtest_reports_buy_and_hold_and_ignored_rules(monkeypatch):
    _, out = _run(monkeypatch, server.run_backtest, {"/backtest/bots/b1/backtest": BACKTEST}, bot_id="b1")
    assert out["buy_hold_return_pct"] == 19.2
    assert out["vs_buy_and_hold_pct"] == -17.8
    assert out["ignored_conditions"] == ["vix_level"]


def test_get_backtest_reports_buy_and_hold_and_ignored_rules(monkeypatch):
    _, out = _run(monkeypatch, server.get_backtest, {"/backtest/bots/b1/backtests/bt1": BACKTEST},
                  bot_id="b1", backtest_id="bt1")
    assert out["buy_hold_return_pct"] == 19.2
    assert out["ignored_conditions"] == ["vix_level"]


def test_list_backtests_reports_buy_and_hold(monkeypatch):
    rows = [{k: BACKTEST[k] for k in ("id", "start_date", "end_date", "total_return", "buy_hold_return_pct",
                                      "total_trades", "win_rate", "max_drawdown_percent")}]
    _, out = _run(monkeypatch, server.list_backtests, {"/backtest/bots/b1/backtests": rows}, bot_id="b1")
    assert out["backtests"][0]["buy_hold_return_pct"] == 19.2


def test_a_backtest_without_a_benchmark_says_so(monkeypatch):
    bt = {**BACKTEST, "buy_hold_return_pct": None, "ignored_conditions": []}
    _, out = _run(monkeypatch, server.run_backtest, {"/backtest/bots/b1/backtest": bt}, bot_id="b1")
    assert out["buy_hold_return_pct"] is None and out["vs_buy_and_hold_pct"] is None
    assert out["ignored_conditions"] == []


def test_backtest_docstrings_state_the_real_windows():
    run = server.run_backtest.description
    assert "30-day lookback" not in run and "365" in run
    preview = server.preview_bot.description
    assert "30-day" not in preview
    assert all(w in preview for w in ("45", "120", "year"))
    assert "buy-and-hold" in preview.lower() or "holding" in preview.lower()


# --- get_positions / get_account_summary (same misread, found while fixing 15-F8) --

POSITION = {"symbol": "AAPL", "qty": 2.0, "side": "long", "avg_entry_price": 100.0, "current_price": 110.0,
            "market_value": 220.0, "cost_basis": 200.0, "unrealized_pnl": 20.0, "unrealized_pnl_percent": 10.0,
            "change_today": 1.5, "asset_class": "us_equity"}


def test_positions_read_the_open_pnl_the_api_sends(monkeypatch):
    _, out = _run(monkeypatch, server.get_positions, {"/trading/portfolio/alpaca-positions": [POSITION]})
    p = out["positions"][0]
    assert (p["unrealized_pnl"], p["unrealized_pnl_percent"]) == (20.0, 10.0)
    assert out["total_unrealized_pnl"] == 20.0
    assert out["total_market_value"] == 220.0


ACCOUNT = {"equity": 10500.0, "cash": 4000.0, "buying_power": 8000.0, "portfolio_value": 10500.0,
           "last_equity": 10400.0, "day_pnl": 100.0, "day_pnl_percent": 0.96, "status": "ACTIVE",
           "currency": "USD", "trading_blocked": False}


def test_account_summary_reads_the_keys_the_api_sends(monkeypatch):
    _, out = _run(monkeypatch, server.get_account_summary, {"/trading/portfolio/account": ACCOUNT})
    assert out["cash"] == 4000.0
    assert out["portfolio_value"] == 10500.0
    assert out["buying_power"] == 8000.0
    assert (out["day_pnl"], out["day_pnl_percent"]) == (100.0, 0.96)
    assert out["trading_blocked"] is False
    # never a made-up 0 for figures the route doesn't send
    assert "position_count" not in out and "unrealized_pnl" not in out
