"""create_bot and refine_bot show what was built; list_bots can be narrowed (2026-11-13).

A Claude connector test asked for a two-leg KO/PEP pair with a z-score exit.
create_bot answered only {"id", "name"} and refine_bot only id/name/status/
version, so the agent could not see that the builder had dropped the PEP leg,
nor the API's own note that KO and PEP had drifted apart. Both tools now read
the saved strategy back and return its rules, the bot's mode and status, the
builder's warnings, and every change it made to the request
(validation_adjustments), with a note telling the agent to tell the user.

list_bots returned all 289 of the owner's bots in API order, mostly paused
test sweeps, with only a paper/live filter. It now filters by status and name,
lists the newest first, and says how many matched.
"""
import asyncio
import json

import pytest

import server


class _Api:
    def __init__(self, routes, fail=()):
        self.routes, self.fail, self.calls = routes, set(fail), []

    async def _do(self, method, endpoint, data=None):
        self.calls.append((method, endpoint, data))
        if endpoint in self.fail:
            raise server.ApiError(503, "unavailable")
        return self.routes[endpoint.split("?")[0]]

    async def get(self, endpoint):
        return await self._do("GET", endpoint)

    async def post(self, endpoint, data=None):
        return await self._do("POST", endpoint, data)


def _run(monkeypatch, tool, api, **kwargs):
    monkeypatch.setattr(server, "get_client", lambda: api)
    return json.loads(asyncio.run(tool.fn(**kwargs)))


BOT = {"id": "q1", "name": "KO-PEP ratio mean reversion", "status": "paused",
       "trading_mode": "paper", "strategy_version": 1, "current_strategy_id": "s1"}

PAIR_RULE = {"type": "indicator", "indicator": "pair_spread", "params": {"other": "PEP", "length": 20},
             "output": "zscore", "operator": "less_than", "value": -2, "timeframe": "1d", "closed_bar": True}

STRATEGY = {
    "id": "s1", "bot_id": "q1", "version": 1,
    "strategy_json": {"symbols": ["KO"], "direction": "long", "mode": "paper", "version": "1.0",
                      "entry": {"logic": "all", "conditions": [PAIR_RULE]},
                      "exit": {"logic": "any", "conditions": [{"type": "stop_loss", "percent": 5.0}]},
                      "position_sizing": {"type": "fixed_dollar", "value": 1000.0}},
    "validation_warnings": ["KO and PEP haven't reliably moved together over the past year."],
    "validation_adjustments": ["Not built: buying PEP when the z-score is above +2."],
    "generated_code": "# ...",
    "explanation": "Buys KO when ...",
}


def test_create_bot_returns_the_rules_mode_status_and_changes(monkeypatch):
    api = _Api({"/bots/": BOT, "/strategy/bots/q1/strategy": STRATEGY})
    out = _run(monkeypatch, server.create_bot, api, prompt="pair bot on KO and PEP")

    assert out["bot"]["id"] == "q1"
    assert out["bot"]["status"] == "paused" and out["bot"]["trading_mode"] == "paper"
    assert out["bot"]["symbols"] == ["KO"]
    assert out["rules"]["entry"]["conditions"][0]["params"]["other"] == "PEP"
    assert "mode" not in out["rules"] and "version" not in out["rules"]   # schema fields, not the bot's
    assert out["changes_to_your_request"] == STRATEGY["validation_adjustments"]
    assert out["warnings"] == STRATEGY["validation_warnings"]
    assert "tell the user" in out["note"].lower()


def test_create_bot_without_changes_says_nothing_was_changed(monkeypatch):
    clean = {**STRATEGY, "validation_adjustments": [], "validation_warnings": []}
    api = _Api({"/bots/": BOT, "/strategy/bots/q1/strategy": clean})
    out = _run(monkeypatch, server.create_bot, api, prompt="buy KO when cheap vs PEP")
    assert out["changes_to_your_request"] == []
    assert "note" not in out or "tell the user" not in out["note"].lower()


def test_create_bot_still_answers_when_the_strategy_read_fails(monkeypatch):
    api = _Api({"/bots/": BOT}, fail={"/strategy/bots/q1/strategy"})
    out = _run(monkeypatch, server.create_bot, api, prompt="x")
    assert out["bot"]["id"] == "q1"          # the bot exists: never report a failed create
    assert "get_bot_strategy" in out["note"]


def test_create_bot_carries_a_start_refusal(monkeypatch):
    refused = {**STRATEGY, "start_refusal": "This bot's stop loss has no distance the engine can read."}
    api = _Api({"/bots/": BOT, "/strategy/bots/q1/strategy": refused})
    out = _run(monkeypatch, server.create_bot, api, prompt="x")
    assert out["start_refusal"].startswith("This bot's stop loss")


def test_refine_bot_returns_the_new_rules_and_changes(monkeypatch):
    refined = {**BOT, "strategy_version": 3}
    v3 = {**STRATEGY, "version": 3}
    api = _Api({"/bots/q1/refine": refined, "/strategy/bots/q1/strategy": v3})
    out = _run(monkeypatch, server.refine_bot, api, bot_id="q1", prompt="switch to hourly bars")
    assert out["bot"]["strategy_version"] == 3
    assert out["rules"]["symbols"] == ["KO"]
    assert out["changes_to_your_request"] == STRATEGY["validation_adjustments"]
    assert ("POST", "/bots/q1/refine", {"prompt": "switch to hourly bars"}) in api.calls


# --- list_bots ----------------------------------------------------------------

def _bot(i, name, status, created, mode="paper", symbols=("SPY",)):
    return {"id": f"b{i}", "name": name, "status": status, "trading_mode": mode,
            "symbols": list(symbols), "created_at": created, "total_pnl": 0.0,
            "winning_trades": 0, "losing_trades": 0}


BOTS = [
    _bot(1, "SWEEP-rsi", "paused", "2026-11-03T10:00:00Z"),
    _bot(2, "KO-PEP ratio mean reversion", "paused", "2026-11-13T00:23:22Z", symbols=("KO",)),
    _bot(3, "SPY Grid", "running", "2026-10-01T09:00:00Z", mode="live"),
    _bot(4, "SWEEP-macd", "paused", "2026-11-03T10:05:00Z"),
    _bot(5, "Pair KO", "running", "2026-11-10T12:00:00Z", symbols=("KO", "PEP")),
]


def test_list_bots_is_newest_first_with_symbols_and_created_at(monkeypatch):
    out = _run(monkeypatch, server.list_bots, _Api({"/bots/": BOTS}))
    assert [b["id"] for b in out["bots"]] == ["b2", "b5", "b4", "b1", "b3"]
    assert out["bots"][0]["symbols"] == ["KO"]
    assert out["bots"][0]["created_at"].startswith("2026-11-13")
    assert out["total"] == 5


def test_list_bots_filters_by_status(monkeypatch):
    out = _run(monkeypatch, server.list_bots, _Api({"/bots/": BOTS}), status="running")
    assert [b["id"] for b in out["bots"]] == ["b5", "b3"]
    assert out["matched"] == 2 and out["total"] == 5


def test_list_bots_filters_by_name_case_insensitively(monkeypatch):
    out = _run(monkeypatch, server.list_bots, _Api({"/bots/": BOTS}), name_contains="sweep")
    assert {b["id"] for b in out["bots"]} == {"b1", "b4"}


def test_list_bots_combines_filters_with_mode(monkeypatch):
    out = _run(monkeypatch, server.list_bots, _Api({"/bots/": BOTS}), status="running", mode="live")
    assert [b["id"] for b in out["bots"]] == ["b3"]


def test_list_bots_with_no_match_names_the_statuses_it_has(monkeypatch):
    out = _run(monkeypatch, server.list_bots, _Api({"/bots/": BOTS}), status="error")
    assert out["bots"] == []
    assert "paused" in out["message"] and "running" in out["message"]


def test_list_bots_limit_says_how_many_more(monkeypatch):
    out = _run(monkeypatch, server.list_bots, _Api({"/bots/": BOTS}), limit=2)
    assert len(out["bots"]) == 2
    assert "2 of 5" in out["note"]


def test_list_bots_tolerates_a_missing_created_at(monkeypatch):
    bots = BOTS + [{**_bot(6, "Old", "paused", None)}]
    out = _run(monkeypatch, server.list_bots, _Api({"/bots/": bots}))
    assert out["bots"][-1]["id"] == "b6"     # undated bots sort last, never crash
