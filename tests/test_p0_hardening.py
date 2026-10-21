"""P0 hardening from the 2026-10-20 review.

- A failed call must reach the agent as a tool error (isError), not as a
  successful result that says {"success": false} and reads like a win.
- An API error page that isn't JSON (a Cloud Run 502) must still say its status.
- place_order sends a deterministic client_order_id, so an agent's retry after
  a timeout can't place a second real order (the API answers 409 duplicate).
- Every tool says whether it only reads, and which ones can spend money or
  delete things, so clients can auto-approve reads and confirm the rest.
- get_market_status hard-coded UTC-5: an hour wrong for half the year.
- Session tokens set by `authenticate` expire with the session.
"""
import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastmcp.exceptions import ToolError

import server


class _FailingClient:
    """Every API call fails the way VibeTraderClient raises."""

    def __init__(self, status=404, detail="Bot not found: b1"):
        self.error = server.ApiError(status, detail)

    async def get(self, endpoint):
        raise self.error

    async def post(self, endpoint, data=None):
        raise self.error

    async def patch(self, endpoint, data):
        raise self.error

    async def delete(self, endpoint):
        raise self.error


class _RecordingClient:
    def __init__(self, responses=None, fail_on=()):
        self.calls = []
        self.responses = responses or {}
        self.fail_on = set(fail_on)

    async def _do(self, method, endpoint, data=None):
        self.calls.append((method, endpoint, data))
        if endpoint in self.fail_on:
            raise server.ApiError(409, "Pause this bot first")
        return self.responses.get(endpoint, {})

    async def get(self, endpoint):
        return await self._do("GET", endpoint)

    async def post(self, endpoint, data=None):
        return await self._do("POST", endpoint, data)

    async def patch(self, endpoint, data):
        return await self._do("PATCH", endpoint, data)

    async def delete(self, endpoint):
        return await self._do("DELETE", endpoint)


def _use(monkeypatch, client):
    monkeypatch.setattr(server, "get_client", lambda: client)
    return client


# --- failures are tool errors --------------------------------------------------

FAILING_CALLS = [
    (server.get_bot_strategy, {"bot_id": "b1"}),
    (server.explain_bot_strategy, {"bot_id": "b1"}),
    (server.refine_bot, {"bot_id": "b1", "prompt": "add a 5% stop"}),
    (server.update_bot, {"bot_id": "b1", "name": "x"}),
    (server.get_positions, {}),
    (server.close_position, {"symbol": "AAPL"}),
    (server.place_order, {"symbol": "AAPL", "side": "buy", "qty": 1}),
    (server.run_backtest, {"bot_id": "b1"}),
    (server.list_backtests, {"bot_id": "b1"}),
    (server.get_backtest, {"bot_id": "b1", "backtest_id": "bt1"}),
    (server.quick_covered_call_backtest, {"symbol": "AAPL"}),
]


@pytest.mark.parametrize("tool,kwargs", FAILING_CALLS, ids=[t.name for t, _ in FAILING_CALLS])
def test_a_failed_api_call_is_a_tool_error(tool, kwargs, monkeypatch):
    _use(monkeypatch, _FailingClient())
    with pytest.raises(ToolError, match="Bot not found"):
        asyncio.run(tool.fn(**kwargs))


def test_place_order_without_a_size_is_a_tool_error(monkeypatch):
    _use(monkeypatch, _RecordingClient())
    with pytest.raises(ToolError, match="qty or notional"):
        asyncio.run(server.place_order.fn(symbol="AAPL", side="buy"))


def test_update_bot_with_nothing_to_change_is_a_tool_error(monkeypatch):
    _use(monkeypatch, _RecordingClient())
    with pytest.raises(ToolError, match="name or status"):
        asyncio.run(server.update_bot.fn(bot_id="b1"))


def test_failed_authentication_is_a_tool_error(monkeypatch):
    async def invalid(api_key):
        return False, "Invalid API key"
    monkeypatch.setattr(server, "validate_token", invalid)
    with pytest.raises(ToolError, match="Invalid API key"):
        asyncio.run(server.authenticate.fn(api_key="vt_nope"))


# --- the API client reads every error shape ------------------------------------

def _client_answering(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(server.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handler), **kw))
    return server.VibeTraderClient("vt_test")


def test_an_html_error_page_still_reports_its_status(monkeypatch):
    client = _client_answering(monkeypatch, lambda req: httpx.Response(
        502, text="<html><body>Bad Gateway</body></html>", headers={"content-type": "text/html"}))
    with pytest.raises(server.ApiError) as err:
        asyncio.run(client.get("/bots/"))
    assert err.value.status == 502
    assert "502" in str(err.value)


def test_a_structured_detail_reports_its_message(monkeypatch):
    # The signal routes answer {"detail": {"code": ..., "message": ...}}.
    client = _client_answering(monkeypatch, lambda req: httpx.Response(
        409, json={"detail": {"code": "bot_running", "message": "Pause this bot first, then switch it to signals."}}))
    with pytest.raises(server.ApiError, match="Pause this bot first"):
        asyncio.run(client.post("/signals/bots/b1/mode", {}))


def test_a_validation_error_reports_what_was_wrong(monkeypatch):
    client = _client_answering(monkeypatch, lambda req: httpx.Response(
        422, json={"detail": [{"loc": ["body", "price"], "msg": "Input should be greater than 0"}]}))
    with pytest.raises(server.ApiError, match="greater than 0"):
        asyncio.run(client.post("/x", {}))


def test_an_empty_success_body_is_an_empty_result(monkeypatch):
    client = _client_answering(monkeypatch, lambda req: httpx.Response(204))
    assert asyncio.run(client.delete("/bots/b1")) == {}


# --- place_order can't double-fill on a retry ----------------------------------

@pytest.fixture(autouse=True)
def _fresh_order_memory():
    server._recent_order_ids.clear()
    yield
    server._recent_order_ids.clear()


def _order_cid(monkeypatch, at, **kwargs):
    client = _use(monkeypatch, _RecordingClient())
    monkeypatch.setattr(server, "_wall_time", lambda: at)
    asyncio.run(server.place_order.fn(**{"symbol": "AAPL", "side": "buy", "qty": 1, **kwargs}))
    (_, endpoint, data), = client.calls
    return data["client_order_id"]


def test_place_order_sends_an_mcp_client_order_id(monkeypatch):
    cid = _order_cid(monkeypatch, 1_000_000.0)
    assert cid.startswith("vt_ticket_mcp_")
    assert len(cid) <= 128


def test_a_retry_of_the_same_order_reuses_its_id(monkeypatch):
    assert _order_cid(monkeypatch, 1_000_000.0) == _order_cid(monkeypatch, 1_000_030.0)


def test_a_different_order_gets_a_different_id(monkeypatch):
    assert _order_cid(monkeypatch, 1_000_000.0) != _order_cid(monkeypatch, 1_000_000.0, qty=2)
    assert _order_cid(monkeypatch, 1_000_000.0) != _order_cid(monkeypatch, 1_000_000.0, mode="live")


def test_an_idempotency_key_holds_the_id_past_the_window(monkeypatch):
    first = _order_cid(monkeypatch, 1_000_000.0, idempotency_key="rebalance-1")
    assert first == _order_cid(monkeypatch, 1_009_999.0, idempotency_key="rebalance-1")
    assert first != _order_cid(monkeypatch, 1_000_000.0, idempotency_key="rebalance-2")


def test_a_retry_across_a_clock_boundary_reuses_its_id(monkeypatch):
    # Review 2026-10-21: fixed two-minute buckets split a call at 1_000_079 and
    # its retry at 1_000_085 into two ids, so both orders went through.
    assert _order_cid(monkeypatch, 1_000_079.0) == _order_cid(monkeypatch, 1_000_085.0)


def test_retries_keep_the_window_open(monkeypatch):
    first = _order_cid(monkeypatch, 1_000_000.0)
    assert _order_cid(monkeypatch, 1_000_110.0) == first
    assert _order_cid(monkeypatch, 1_000_220.0) == first


def test_the_same_order_after_a_quiet_window_is_a_new_order(monkeypatch):
    first = _order_cid(monkeypatch, 1_000_000.0)
    assert _order_cid(monkeypatch, 1_000_000.0 + server.ORDER_ID_WINDOW_SECONDS + 1) != first


def test_a_timeout_says_the_order_may_have_been_placed(monkeypatch):
    class _TimingOut(_RecordingClient):
        async def post(self, endpoint, data=None):
            raise server.ApiUnreachable("ReadTimeout")
    _use(monkeypatch, _TimingOut())
    with pytest.raises(ToolError, match="may or may not have been placed"):
        asyncio.run(server.place_order.fn(symbol="AAPL", side="buy", qty=1))


def test_an_unreachable_api_is_a_tool_error(monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)
    client = _client_answering(monkeypatch, timeout)
    with pytest.raises(server.ApiUnreachable, match="No answer from the VibeTrader API"):
        asyncio.run(client.get("/bots/"))


def test_a_duplicate_order_says_how_to_place_another_on_purpose(monkeypatch):
    _use(monkeypatch, _FailingClient(409, "This order was already sent. Check your orders before placing it again."))
    with pytest.raises(ToolError, match="idempotency_key"):
        asyncio.run(server.place_order.fn(symbol="AAPL", side="buy", qty=1))


# --- tool annotations -------------------------------------------------------------

# Tools that can spend money, start trading, or delete: clients should confirm.
CONSEQUENTIAL = {
    "place_order", "close_position", "delete_bot", "delete_all_bots",
    "start_bot", "start_all_bots", "update_bot", "set_signal_mode",
}
READ_ONLY = {
    "list_bots", "get_bot", "search_bots", "get_bot_stats", "get_bot_strategy",
    "explain_bot_strategy", "get_daily_trade_stats", "get_portfolio", "get_positions",
    "get_account_summary", "get_trade_history", "get_bot_trades", "get_quote",
    "list_indicators", "get_market_status", "list_backtests", "get_backtest",
    "quick_covered_call_backtest", "list_open_signals", "get_signal_state", "preview_bot",
}


def _tools():
    return asyncio.run(server.mcp.get_tools())


def test_every_tool_says_whether_it_only_reads():
    for name, tool in _tools().items():
        assert tool.annotations is not None, name
        assert tool.annotations.readOnlyHint is (name in READ_ONLY), name


def test_consequential_tools_are_marked_destructive():
    for name, tool in _tools().items():
        if tool.annotations.readOnlyHint:
            continue
        assert tool.annotations.destructiveHint is (name in CONSEQUENTIAL), name


# --- market status -------------------------------------------------------------

def _not_authenticated():
    raise ToolError("Not authenticated!")


def _status_at(monkeypatch, utc):
    monkeypatch.setattr(server, "get_client", _not_authenticated)
    monkeypatch.setattr(server, "_utc_now", lambda: utc)
    return json.loads(asyncio.run(server.get_market_status.fn()))


def test_market_status_reads_the_exchange_clock(monkeypatch):
    client = _use(monkeypatch, _RecordingClient({"/research/market/status": {
        "isOpen": False, "nextOpen": "2026-11-27T09:30:00-05:00", "nextClose": "2026-11-27T13:00:00-05:00"}}))
    out = json.loads(asyncio.run(server.get_market_status.fn()))
    assert client.calls[0][1] == "/research/market/status"
    assert out["is_open"] is False
    assert out["next_open"] == "2026-11-27T09:30:00-05:00"


def test_market_status_fallback_knows_daylight_time(monkeypatch):
    # Tue 2026-10-20 13:45 UTC is 9:45 AM EDT: open. UTC-5 called it 8:45, closed.
    assert _status_at(monkeypatch, datetime(2026, 10, 20, 13, 45, tzinfo=timezone.utc))["is_open"] is True
    # 20:30 UTC is 4:30 PM EDT: closed. UTC-5 called it 3:30, open.
    assert _status_at(monkeypatch, datetime(2026, 10, 20, 20, 30, tzinfo=timezone.utc))["is_open"] is False


def test_market_status_fallback_knows_standard_time_and_weekends(monkeypatch):
    # Mon 2026-12-07 14:45 UTC is 9:45 AM EST: open.
    assert _status_at(monkeypatch, datetime(2026, 12, 7, 14, 45, tzinfo=timezone.utc))["is_open"] is True
    # Sat 2026-10-24 15:00 UTC: closed.
    assert _status_at(monkeypatch, datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc))["is_open"] is False


# --- session tokens ------------------------------------------------------------

def test_a_session_token_expires_once_the_session_is_idle(monkeypatch):
    server._tokens_by_session.clear()
    clock = [0.0]
    monkeypatch.setattr(server, "_monotonic", lambda: clock[0])
    server._remember_session_token("s1", "vt_a")
    clock[0] = server.SESSION_TOKEN_IDLE_SECONDS - 1
    assert server._session_token("s1") == "vt_a"      # a read keeps it alive
    clock[0] += server.SESSION_TOKEN_IDLE_SECONDS - 1
    assert server._session_token("s1") == "vt_a"
    clock[0] += server.SESSION_TOKEN_IDLE_SECONDS + 1
    assert server._session_token("s1") is None
    assert "s1" not in server._tokens_by_session


def test_storing_a_token_sweeps_out_idle_sessions(monkeypatch):
    server._tokens_by_session.clear()
    clock = [0.0]
    monkeypatch.setattr(server, "_monotonic", lambda: clock[0])
    server._remember_session_token("old", "vt_a")
    clock[0] = server.SESSION_TOKEN_IDLE_SECONDS + 1
    server._remember_session_token("new", "vt_b")
    assert set(server._tokens_by_session) == {"new"}


# --- bulk actions, list_bots, instructions -------------------------------------

BOTS = [{"id": f"b{i}", "name": f"Bot {i}", "status": "running", "trading_mode": "paper", "total_pnl": 0}
        for i in range(3)]


def test_pause_all_bots_names_the_bots_it_could_not_pause(monkeypatch):
    _use(monkeypatch, _RecordingClient({"/bots/": BOTS}, fail_on={"/bots/b1/pause"}))
    out = asyncio.run(server.pause_all_bots.fn())
    assert "2 of 3" in out
    assert "Bot 1" in out and "Pause this bot first" in out


def test_list_bots_shows_more_than_ten(monkeypatch):
    many = [{**BOTS[0], "id": f"b{i}", "name": f"Bot {i}"} for i in range(25)]
    _use(monkeypatch, _RecordingClient({"/bots/": many}))
    out = json.loads(asyncio.run(server.list_bots.fn()))
    assert out["total"] == 25
    assert len(out["bots"]) == 25


def test_instructions_tell_hosted_clients_to_send_the_key_as_a_header():
    text = server.mcp.instructions
    assert "Authorization: Bearer" in text
    assert "Use 'authenticate' first" not in text
