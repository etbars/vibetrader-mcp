"""A covered-call or wheel backtest says what its share stop did (2026-11-17).

Since 2026-11-17 the API's backtest result carries `share_stop` for a bot with a
stop-loss, take-profit or trailing stop on its shares: the rules, whether one
was reached in the run, the event, the words for it, and one more sentence when
the bot's route doesn't run the rule. run_backtest and get_backtest re-map the
result field by field and dropped the block, so an AI app reported the return
of a run that ended in a stop-out and never said so.

The block is passed through as the API stores it (the 2026-11-07 lesson: a
re-mapped field drifts silently). It is absent for every other backtest, and
list_backtests has none to pass: the API's list rows don't carry it.
"""
import asyncio
import json

import server
from tests.test_read_tools_report_real_numbers import BACKTEST, _run

STOPPED_OUT = {
    "rules": {"stop_loss_percent": 10.0, "take_profit_percent": None, "trailing_stop_percent": None,
              "trailing_activation_percent": None},
    "stopped_out": True,
    "event": {"date": "2026-08-20", "reason": "stop_loss", "share_price": 13.5, "buy_back_cost": 42.0},
    "headline": "The stop loss was reached on Aug 20, 2026 and closed the position.",
    "message": ("The stop loss was reached on Aug 20, 2026 and closed the position. It bought back the call for "
                "$42.00 and sold 100 shares at $13.50."),
    "basis": "The backtest checks the stop on daily bars.",
    "bars_checked": 61,
    "route": "runs",
}
NOT_REACHED = {**STOPPED_OUT, "stopped_out": False, "event": None, "headline": None,
               "message": "The 10% stop loss was not reached in this window.", "bars_checked": 124}
ROUTE_NOTE = {**NOT_REACHED, "route": "signal",
              "message": ("The 10% stop loss was not reached in this window. Signal bots don't run the share stop: "
                          "CC Bot will not act on it.")}


def _ran(monkeypatch, share_stop):
    bt = {**BACKTEST, **({} if share_stop == "absent" else {"share_stop": share_stop})}
    _, out = _run(monkeypatch, server.run_backtest, {"/backtest/bots/b1/backtest": bt}, bot_id="b1")
    return out


def _got(monkeypatch, share_stop):
    bt = {**BACKTEST, **({} if share_stop == "absent" else {"share_stop": share_stop})}
    _, out = _run(monkeypatch, server.get_backtest, {"/backtest/bots/b1/backtests/bt1": bt},
                  bot_id="b1", backtest_id="bt1")
    return out


def test_run_backtest_says_the_share_stop_closed_the_position(monkeypatch):
    out = _ran(monkeypatch, STOPPED_OUT)
    assert out["share_stop"] == STOPPED_OUT
    assert out["total_return_pct"] == 1.4                       # the numbers are as they were


def test_get_backtest_says_the_share_stop_closed_the_position(monkeypatch):
    assert _got(monkeypatch, STOPPED_OUT)["share_stop"] == STOPPED_OUT


def test_a_stop_that_was_not_reached_says_so(monkeypatch):
    assert _ran(monkeypatch, NOT_REACHED)["share_stop"] == NOT_REACHED
    assert _got(monkeypatch, NOT_REACHED)["share_stop"] == NOT_REACHED


def test_the_sentence_for_a_route_that_does_not_run_the_stop_comes_through(monkeypatch):
    assert _ran(monkeypatch, ROUTE_NOTE)["share_stop"]["message"].endswith("CC Bot will not act on it.")
    assert _got(monkeypatch, ROUTE_NOTE)["share_stop"]["route"] == "signal"


def test_a_backtest_without_a_share_stop_has_no_such_field(monkeypatch):
    """Every other backtest: the API sends null, or (an older API) nothing."""
    for nothing in (None, "absent", {}, "", []):
        assert "share_stop" not in _ran(monkeypatch, nothing), nothing
        assert "share_stop" not in _got(monkeypatch, nothing), nothing


def test_something_that_is_not_the_block_is_not_passed_on(monkeypatch):
    for junk in ("stopped out", 1, ["stopped_out"]):
        assert "share_stop" not in _ran(monkeypatch, junk), junk
        assert "share_stop" not in _got(monkeypatch, junk), junk


def test_the_tools_tell_the_model_to_report_it():
    for tool in (server.run_backtest, server.get_backtest):
        text = " ".join(tool.description.split())
        assert "share_stop" in text and "stopped_out" in text, tool.name
        assert "report its message with the return" in text, tool.name
