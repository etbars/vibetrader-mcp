"""get_bot_strategy shows the rules the API stores (2026-11-07).

The tool re-mapped strategy_json field by field under names the API never
used (entry_rules, exit_rules, indicators, risk_management), so every bot's
rules came back empty. A claude.ai Connect test showed it on a fresh bot whose
saved strategy was RSI(14) on daily bars below 30 -> buy $100 of SPY -> +3%.
"""
import asyncio
import json

import server

# The shape GET /strategy/bots/{id}/strategy answers (app.models.strategy.Strategy).
STORED = {
    "id": "s1",
    "bot_id": "b1",
    "version": 2,
    "strategy_json": {
        "version": "1.0",
        "name": "SPY RSI Oversold Buyer",
        "description": "Buys $100 of SPY when the 14-period daily RSI drops below 30.",
        "symbols": ["SPY"],
        "direction": "long",
        "short_entry": None,
        "entry": {
            "conditions": [{"type": "indicator", "indicator": "rsi", "params": {"length": 14},
                            "operator": "less_than", "value": 30, "timeframe": "1d"}],
            "logic": "all",
        },
        "exit": {
            "conditions": [{"type": "take_profit", "percent": 3.0},
                           {"type": "stop_loss", "percent": 5.0}],
            "logic": "any",
        },
        "position_sizing": {"type": "fixed_dollar", "value": 100},
        "risk_controls": {"max_trades_per_day": 3},
        "schedule": {"check_interval_minutes": 15},
        "mode": "paper",
    },
    "generated_code": "if rsi(14) < 30: buy(100)",
    "created_at": "2026-11-07T10:57:00Z",
}


class _Client:
    async def get(self, endpoint):
        assert endpoint == "/strategy/bots/b1/strategy"
        return STORED


def _strategy(monkeypatch):
    monkeypatch.setattr(server, "get_client", lambda: _Client())
    return json.loads(asyncio.run(server.get_bot_strategy.fn(bot_id="b1")))


def test_entry_and_exit_rules_come_through_with_their_timeframe(monkeypatch):
    s = _strategy(monkeypatch)["strategy"]
    entry = s["entry"]["conditions"][0]
    assert (entry["indicator"], entry["operator"], entry["value"], entry["timeframe"]) == ("rsi", "less_than", 30, "1d")
    assert [c["type"] for c in s["exit"]["conditions"]] == ["take_profit", "stop_loss"]
    assert s["risk_controls"] == {"max_trades_per_day": 3}
    assert s["position_sizing"]["value"] == 100
    assert s["symbols"] == ["SPY"]


def test_no_invented_empty_fields(monkeypatch):
    s = _strategy(monkeypatch)["strategy"]
    for name in ("entry_rules", "exit_rules", "indicators", "risk_management", "short_entry"):
        assert name not in s, name


def test_paper_only_schema_default_is_not_shown_as_the_bot_mode(monkeypatch):
    # StrategyJSON.mode is a Literal["paper"] schema default, not the bot's
    # trading mode, so passing it through would tell Claude a live bot is paper.
    assert "mode" not in _strategy(monkeypatch)["strategy"]


def test_strategy_version_and_code_kept(monkeypatch):
    out = _strategy(monkeypatch)
    assert out["version"] == 2
    assert out["generated_code"] == "if rsi(14) < 30: buy(100)"
