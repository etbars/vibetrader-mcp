"""What clients see once sign-in exists (spec 2026-10-21 §7, §10):

- over HTTP there is no `authenticate` tool — a client without a token is asked
  to sign in instead (oauth_gate.py), so no key is ever pasted into chat;
  local stdio keeps it;
- every tool has a title and explicit readOnly/destructive/openWorld hints
  (both directories require them; Claude uses destructiveHint to ask first);
- the instructions describe the signal loop without telling the model to call
  another server's tools.
"""
import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import server

OPEN_WORLD = {"place_order", "close_position", "start_bot", "start_all_bots", "set_signal_mode"}


def listed():
    async def go():
        async with Client(server.mcp) as c:
            return {t.name: t for t in await c.list_tools()}
    return asyncio.run(go())


def test_http_tools_exclude_authenticate():
    tools = listed()
    assert "authenticate" not in tools
    assert len(tools) == 37


def test_stdio_enables_authenticate():
    try:
        server._enable_stdio_tools()
        assert "authenticate" in listed()
    finally:
        server.authenticate.disable()


def test_every_tool_has_a_title_and_three_boolean_hints():
    for name, tool in listed().items():
        assert tool.title, name
        a = tool.annotations
        assert a.title == tool.title, name
        for hint in (a.readOnlyHint, a.destructiveHint, a.openWorldHint):
            assert isinstance(hint, bool), (name, a)
        assert not (a.readOnlyHint and a.destructiveHint), name


def test_open_world_tools():
    assert {n for n, t in listed().items() if t.annotations.openWorldHint} == OPEN_WORLD


def test_instructions_describe_rather_than_direct():
    text = server.mcp.instructions
    assert "authenticate" not in text
    assert "Robinhood" not in text and "brokerage's own tools" not in text
    assert "report_signal" in text and "list_open_signals" in text
    assert "Authorization: Bearer" in text


def test_stdio_authenticate_sets_the_stdio_token(monkeypatch):
    async def valid(api_key):
        return True, "me@example.com"
    monkeypatch.setattr(server, "validate_token", valid)
    monkeypatch.setattr(server, "_stdio_token", None)
    out = asyncio.run(server.authenticate.fn(api_key="vt_k"))
    assert "me@example.com" in out and server._stdio_token == "vt_k"


def test_the_session_token_store_is_gone():
    for name in ("_tokens_by_session", "SESSION_TOKEN_IDLE_SECONDS", "_remember_session_token", "_session_token"):
        assert not hasattr(server, name), name


def test_without_a_token_the_error_says_how_to_connect(monkeypatch):
    monkeypatch.setattr(server, "_resolve_token", lambda: None)
    with pytest.raises(ToolError, match="Connect VibeTrader in your AI app"):
        server.get_client()
