"""
VibeTrader MCP Server

Based on official FastMCP documentation for HTTP deployment.
https://gofastmcp.com/deployment/http
"""

import os
import json
import asyncio
import hashlib
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal, Optional
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
import jwt
import pytz
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers

from oauth_gate import OAuthGate, default_verifier
from fastmcp.server.http import RequestContextMiddleware
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("vibetrader-mcp")

# VibeTrader API configuration
API_BASE_URL = os.getenv("VIBETRADER_API_URL", "https://api.vibetrader.markets")

# Initialize FastMCP
mcp = FastMCP(
    "VibeTrader",
    instructions=(
        "VibeTrader runs rule-based trading bots around the clock. Create a bot in plain English, "
        "backtest it, and run it on paper, on the user's live Alpaca account, or as a signal bot that "
        "tells the user what to trade at any brokerage. Hosted clients connect by signing in to "
        "VibeTrader; clients without sign-in send 'Authorization: Bearer vt_...' with every request "
        "(keys are at vibetrader.markets/settings). Each connection has permissions the user chose: see, "
        "manage bots and signals, and place orders and run live bots; a call outside them fails with a "
        "message saying how to allow it. place_order and close_position reach only the user's VibeTrader "
        "Alpaca account. A signal bot (set_signal_mode) doesn't trade; its signals are listed by "
        "list_open_signals, and once the user has traded one wherever they hold the account, "
        "report_signal records the fill so the bot tracks the position and sends its exits."
    ),
)

# Tool annotations (MCP spec). Clients may run read-only tools without asking
# and should confirm destructive ones. Here "destructive" means the call can
# spend money, start trading or delete something; "open world" means it reaches
# past VibeTrader to a brokerage (an order, or a bot that will place them).
# Every hint is explicit: both connector directories require it.
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
SAFE_WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
CONSEQUENTIAL = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}
CONSEQUENTIAL_EXTERNAL = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}


def _tool(title: str, hints: dict, **kwargs):
    """@mcp.tool with a title on the tool and in its annotations."""
    return mcp.tool(title=title, annotations={**hints, "title": title}, **kwargs)


# =============================================================================
# API Client
# =============================================================================

class ApiError(ToolError):
    """The VibeTrader API answered with an error status. A ToolError, so the
    agent sees a failed call (isError), never a result that reads like a win."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"API Error ({status}): {detail}")


class ApiUnreachable(ToolError):
    """The API never answered (a timeout or a dropped connection). For a write,
    that means it may or may not have happened."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"No answer from the VibeTrader API ({detail}). Try again.")


def _error_detail(response: httpx.Response) -> str:
    """The useful part of an error body, whatever its shape."""
    try:
        body = response.json()
    except ValueError:
        text = (response.text or "").strip()
        # A proxy's HTML error page (Cloud Run's 502) says nothing an agent can use.
        if not text or text.startswith("<"):
            return response.reason_phrase or "Unknown error"
        return text[:300]
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, dict):          # the signal routes: {"code": ..., "message": ...}
        return str(detail.get("message") or detail)
    if isinstance(detail, list):          # FastAPI validation errors
        return "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    return str(detail)


class VibeTraderClient:
    def __init__(self, api_key: str):
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

    async def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        try:
            async with httpx.AsyncClient() as client:
                url = f"{API_BASE_URL}{endpoint}"
                response = await client.request(method, url, headers=self.headers, timeout=30.0, **kwargs)
        except httpx.TransportError as e:
            raise ApiUnreachable(type(e).__name__) from e
        if response.status_code >= 400:
            raise ApiError(response.status_code, _error_detail(response))
        return response.json() if response.content else {}
    
    async def get(self, endpoint: str) -> dict:
        return await self._request("GET", endpoint)
    
    async def post(self, endpoint: str, data: dict = None) -> dict:
        return await self._request("POST", endpoint, json=data or {})
    
    async def patch(self, endpoint: str, data: dict) -> dict:
        return await self._request("PATCH", endpoint, json=data)
    
    async def delete(self, endpoint: str) -> dict:
        return await self._request("DELETE", endpoint)


# =============================================================================
# Per-request auth (multi-tenant safe)
# =============================================================================
# This server is deployed over HTTP (uvicorn) as a SHARED single process: one
# Python process handles every connected user. A module-global "current token"
# is therefore last-writer-wins across users — whoever authenticated most
# recently silently owns every other session's tool calls, exposing and
# mutating strangers' real-money accounts.
#
# Tokens are now resolved PER REQUEST, in priority order:
#   1. The request's own `Authorization: Bearer ...` header: a sign-in token
#      issued to this AI app, or a vt_ key. Over HTTP nothing else is accepted
#      (oauth_gate.py answers a request without one with 401 — "Connect").
#   2. A single-process fallback for local stdio use (no HTTP context, one
#      user, set by `authenticate`) — never reachable when running under HTTP.
#
# get_http_headers() is request-scoped via contextvars, so each concurrent
# task sees only its own request — that is what fixes the cross-user bleed.

# Local stdio single-user fallback (no HTTP request context exists).
_stdio_token: Optional[str] = None


def _http_headers() -> dict:
    """Current request's headers (lowercased), or {} when not under HTTP."""
    try:
        return get_http_headers() or {}
    except Exception:
        return {}


def _is_http_context() -> bool:
    return bool(_http_headers())


def _request_bearer() -> Optional[str]:
    auth = _http_headers().get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return token or None
    return None


async def validate_token(api_key: str) -> tuple[bool, str]:
    """Validate a key against the API. Pure check — stores nothing global."""
    if not api_key.startswith("vt_"):
        return False, "Invalid format. Keys start with 'vt_'"

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{API_BASE_URL}/auth/validate-api-key",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30.0
            )
            if response.status_code == 200:
                return True, response.json().get("email", "user")
            return False, "Invalid API key"
        except Exception as e:
            return False, str(e)


def _resolve_token() -> Optional[str]:
    # 1. Explicit per-request Authorization header.
    token = _request_bearer()
    if token:
        return token
    # 2. Local stdio single-user fallback (no HTTP context).
    if not _is_http_context():
        return _stdio_token
    return None


def get_client() -> VibeTraderClient:
    token = _resolve_token()
    if not token:
        raise ToolError(
            "Not signed in. Connect VibeTrader in your AI app (it will ask you to sign in), or send "
            "'Authorization: Bearer vt_...' with a key from vibetrader.markets/settings."
        )
    return VibeTraderClient(token)


# =============================================================================
# Core Tools
# =============================================================================

@_tool("Sign in with an API key", SAFE_WRITE, enabled=False)
async def authenticate(api_key: str) -> str:
    """Authenticate with your API key from vibetrader.markets/settings"""
    success, result = await validate_token(api_key)
    if not success:
        raise ToolError(f"Authentication failed: {result}")
    if _is_http_context():
        # Unreachable through the gate in practice (no token, no request), and
        # never stored: over HTTP the key belongs in a header, per request.
        return (f"✅ Key valid for {result}. Send it as 'Authorization: Bearer {api_key[:6]}...' "
                "with each request instead.")
    # Local stdio: single user, safe to hold in-process.
    global _stdio_token
    _stdio_token = api_key
    return f"✅ Authenticated as {result}. You can now use all tools!"


def _enable_stdio_tools() -> None:
    """Over local stdio there is no sign-in; the key comes through authenticate."""
    authenticate.enable()


def _is_signal_bot(bot: dict) -> bool:
    return "alert" in (bot.get("entry_mode"), bot.get("exit_mode"))


# =============================================================================
# Reading the API's numbers
# =============================================================================
# The read tools report what the app shows (2026-10-22 audit 15-F8). P&L on a
# bot, as GET /bots/ and /bots/{id} send it:
#   realized_pnl   closed trades. Sent by the API since the P&L fix; before it,
#                  total_pnl is that same realized counter, so it's the fallback.
#   unrealized_pnl the bot's open lots at the current price; all_time_pnl is
#                  realized + open. Null until the API sends them, or while an
#                  open lot has no price (unpriced_symbols names it).
# Win rate is wins / (wins + losses): a breakeven or an opening buy is neither.

def _money(value) -> Optional[float]:
    """A dollar (or percent) figure rounded to cents; None when the API sent none."""
    try:
        return None if value is None else round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _pick(row: dict, *keys):
    """The first of `keys` the row carries a value for (camelCase or snake_case)."""
    for key in keys:
        if row.get(key) is not None:
            return row[key]
    return None


def _win_rate(wins, losses) -> Optional[float]:
    wins, losses = int(wins or 0), int(losses or 0)
    return round(wins / (wins + losses) * 100, 1) if wins + losses else None


def _bot_pnl(bot: dict) -> dict:
    """realized_pnl (never None), unrealized_pnl and all_time_pnl (None = unknown)."""
    return {
        "realized_pnl": _money(_pick(bot, "realized_pnl", "total_pnl")) or 0.0,
        "unrealized_pnl": _money(bot.get("unrealized_pnl")),
        "all_time_pnl": _money(bot.get("all_time_pnl")),
    }


def _bot_win_rate(bot: dict) -> Optional[float]:
    wins, losses = bot.get("winning_trades"), bot.get("losing_trades")
    if not int(wins or 0) + int(losses or 0):
        return None
    sent = _money(bot.get("win_rate"))
    return round(sent, 1) if sent is not None else _win_rate(wins, losses)


@_tool("List bots", READ_ONLY)
async def list_bots(mode: Optional[str] = None, limit: int = 100) -> str:
    """List all your trading bots, with each bot's realized P&L (closed trades),
    its open P&L and all-time P&L when the API reports them, and its win rate
    (wins / closed trades).

    Args:
        mode: Filter by trading mode - "paper", "live", or None for all bots
        limit: Most bots to return (default 100)
    """
    client = get_client()
    bots = await client.get("/bots/")
    if mode:
        bots = [b for b in bots if b.get("trading_mode") == mode]
    if not bots:
        return json.dumps({"message": f"No {mode or ''} bots found", "bots": []})
    summary = []
    for b in bots[:max(1, limit)]:
        pnl = _bot_pnl(b)
        row = {"id": b.get("id"), "name": b.get("name"), "status": b.get("status"),
               "trading_mode": b.get("trading_mode"), "realized_pnl": pnl["realized_pnl"]}
        for key in ("unrealized_pnl", "all_time_pnl"):
            if pnl[key] is not None:
                row[key] = pnl[key]
        row["win_rate"] = _bot_win_rate(b)
        if _is_signal_bot(b):
            row["signal_bot"] = True
        summary.append(row)
    out = {"total": len(bots), "bots": summary}
    if len(summary) < len(bots):
        out["note"] = f"Showing {len(summary)} of {len(bots)}. Raise limit, or use search_bots."
    return json.dumps(out, indent=2)


@_tool("Get bot", READ_ONLY)
async def get_bot(bot_id: str) -> str:
    """Get details of a specific bot.

    P&L fields: total_pnl and realized_pnl are realized (closed trades only);
    unrealized_pnl is the bot's open positions at the current price and
    all_time_pnl is realized + open (null while an open position has no price,
    or on an API that doesn't send them yet). total_trades counts every buy and
    sell; winning_trades + losing_trades counts closed trades.
    """
    client = get_client()
    return json.dumps(await client.get(f"/bots/{bot_id}"), indent=2)


@_tool("Create bot", SAFE_WRITE)
async def create_bot(prompt: str, name: Optional[str] = None) -> str:
    """Create a new trading bot using natural language"""
    client = get_client()
    data = {"prompt": prompt}
    if name:
        data["name"] = name
    bot = await client.post("/bots/", data)
    return json.dumps({"status": "success", "bot": {"id": bot.get("id"), "name": bot.get("name")}}, indent=2)


@_tool("Start bot", CONSEQUENTIAL_EXTERNAL)
async def start_bot(bot_id: str) -> str:
    """Start a paused bot"""
    client = get_client()
    await client.post(f"/bots/{bot_id}/start")
    return "✅ Bot started"


@_tool("Pause bot", SAFE_WRITE)
async def pause_bot(bot_id: str) -> str:
    """Pause a running bot"""
    client = get_client()
    await client.post(f"/bots/{bot_id}/pause")
    return "✅ Bot paused"


@_tool("Delete bot", CONSEQUENTIAL)
async def delete_bot(bot_id: str) -> str:
    """Delete a bot"""
    client = get_client()
    await client.delete(f"/bots/{bot_id}")
    return "✅ Bot deleted"


@_tool("Get portfolio", READ_ONLY)
async def get_portfolio(mode: str = "paper") -> str:
    """Get portfolio positions and balance.
    
    Args:
        mode: Account mode - "paper" (default) or "live" for real money
    """
    client = get_client()
    account = await client.get(f"/trading/portfolio/account?mode={mode}")
    positions = await client.get(f"/trading/portfolio/alpaca-positions?mode={mode}")
    return json.dumps({"mode": mode, "account": account, "positions": positions}, indent=2)


@_tool("Get quote", READ_ONLY)
async def get_quote(symbol: str) -> str:
    """Get current quote for a stock"""
    client = get_client()
    return json.dumps(await client.get(f"/research/quote/{symbol}"), indent=2)


@_tool("List indicators", READ_ONLY)
async def list_indicators(family: str = "") -> str:
    """List every technical indicator VibeTrader bots can use, with each one's
    parameters (defaults and bounds) and outputs. Indicator math matches
    TradingView. Optionally filter by family: moving_average, momentum, trend,
    volatility, volume or price. Needs no API key."""
    # The catalog is public: no token, so it works before `authenticate`.
    async with httpx.AsyncClient() as http:
        response = await http.get(f"{API_BASE_URL}/indicators/catalog", timeout=30.0)
    if response.status_code >= 400:
        raise Exception(f"API Error ({response.status_code}): {response.text or 'Unknown error'}")
    body = response.json()
    entries = body.get("indicators", [])
    wanted = family.strip().lower()
    if not wanted:
        return json.dumps(body, indent=2)
    out = {"version": body.get("version"), "indicators": [e for e in entries if e.get("family") == wanted]}
    if not out["indicators"]:
        out["families"] = sorted({e.get("family") for e in entries if e.get("family")})
    return json.dumps(out, indent=2)


# =============================================================================
# Bulk Actions
# =============================================================================

async def _for_each_bot(bots: list, act, done_verb: str, verb: str, mode: Optional[str]) -> str:
    """Run `act` on every bot and say which ones failed, and why: a panic
    button that reports "2 of 3" without naming the third is no use."""
    label = f"{mode} bots" if mode else "bots"
    failed = []
    for bot in bots:
        try:
            await act(bot)
        except Exception as e:
            failed.append(f"'{bot.get('name') or bot.get('id')}' ({e})")
    done = len(bots) - len(failed)
    if failed and not done:
        raise ToolError(f"Couldn't {verb} any of {len(bots)} {label}: " + "; ".join(failed))
    message = f"✅ {done_verb} {done} of {len(bots)} {label}"
    if failed:
        message += f". Couldn't {verb}: " + "; ".join(failed)
    return message


@_tool("Pause all bots", SAFE_WRITE)
async def pause_all_bots(mode: Optional[str] = None) -> str:
    """Pause all running bots at once.

    Args:
        mode: Filter by trading mode - "paper", "live", or None for all bots
    """
    client = get_client()
    bots = await client.get("/bots/")
    running = [b for b in bots if b.get("status") == "running"]
    if mode:
        running = [b for b in running if b.get("trading_mode") == mode]

    if not running:
        return f"No running {mode or ''} bots to pause"

    return await _for_each_bot(running, lambda b: client.post(f"/bots/{b['id']}/pause"),
                               "Paused", "pause", mode)


@_tool("Start all bots", CONSEQUENTIAL_EXTERNAL)
async def start_all_bots(mode: Optional[str] = None) -> str:
    """Start all paused bots at once.

    Args:
        mode: Filter by trading mode - "paper", "live", or None for all bots
    """
    client = get_client()
    bots = await client.get("/bots/")
    paused = [b for b in bots if b.get("status") == "paused"]
    if mode:
        paused = [b for b in paused if b.get("trading_mode") == mode]

    if not paused:
        return f"No paused {mode or ''} bots to start"

    return await _for_each_bot(paused, lambda b: client.post(f"/bots/{b['id']}/start"),
                               "Started", "start", mode)


@_tool("Delete all bots", CONSEQUENTIAL)
async def delete_all_bots(confirm: str, mode: Optional[str] = None) -> str:
    """Delete ALL bots. Requires confirm='DELETE ALL' to proceed.

    Args:
        confirm: Must be 'DELETE ALL' to confirm deletion
        mode: Filter by trading mode - "paper", "live", or None for all bots
    """
    if confirm != "DELETE ALL":
        raise ToolError("To delete all bots, pass confirm='DELETE ALL'")

    client = get_client()
    bots = await client.get("/bots/")
    if mode:
        bots = [b for b in bots if b.get("trading_mode") == mode]

    if not bots:
        return f"No {mode or ''} bots to delete"

    return await _for_each_bot(bots, lambda b: client.delete(f"/bots/{b['id']}"),
                               "Deleted", "delete", mode)


# =============================================================================
# Utility Tools
# =============================================================================

@_tool("Get bot stats", READ_ONLY)
async def get_bot_stats(mode: Optional[str] = None) -> str:
    """Get aggregate statistics across your bots: realized P&L (closed trades),
    open and all-time P&L when every bot's open positions are priced, the win
    rate over all closed trades, and the best and worst bot.

    Args:
        mode: Filter by trading mode - "paper", "live", or None for all bots
    """
    client = get_client()
    bots = await client.get("/bots/")
    if mode:
        bots = [b for b in bots if b.get("trading_mode") == mode]

    if not bots:
        return json.dumps({"message": f"No {mode or ''} bots found"})

    pnls = [_bot_pnl(b) for b in bots]

    def fleet(key: str) -> Optional[float]:
        values = [p[key] for p in pnls]
        return round(sum(values), 2) if all(v is not None for v in values) else None

    all_time = fleet("all_time_pnl")
    basis = "all_time_pnl" if all_time is not None else "realized_pnl"
    stats = {
        "mode_filter": mode or "all",
        "total_bots": len(bots),
        "running": len([b for b in bots if b.get("status") == "running"]),
        "paused": len([b for b in bots if b.get("status") == "paused"]),
        "paper_trading": len([b for b in bots if b.get("trading_mode") == "paper"]),
        "live_trading": len([b for b in bots if b.get("trading_mode") == "live"]),
        "realized_pnl": round(sum(p["realized_pnl"] for p in pnls), 2),
        "unrealized_pnl": fleet("unrealized_pnl"),
        "all_time_pnl": all_time,
        "win_rate": _win_rate(sum(int(b.get("winning_trades") or 0) for b in bots),
                              sum(int(b.get("losing_trades") or 0) for b in bots)),
        "ranked_by": basis,
        "profitable_bots": len([p for p in pnls if p[basis] > 0]),
    }
    ranked = sorted(zip(bots, pnls), key=lambda bp: bp[1][basis], reverse=True)
    stats["top_performer"] = {"name": ranked[0][0].get("name"), "pnl": ranked[0][1][basis]}
    stats["worst_performer"] = {"name": ranked[-1][0].get("name"), "pnl": ranked[-1][1][basis]}

    unpriced = sorted({s for b in bots for s in (b.get("unpriced_symbols") or [])})
    if unpriced:
        stats["note"] = (f"Open P&L is unknown while {', '.join(unpriced)} has no current price, so only "
                         "realized P&L is totalled.")
    elif all_time is None:
        stats["note"] = "Open P&L isn't reported for these bots; the P&L here is realized (closed trades) only."
    return json.dumps(stats, indent=2)


_EASTERN = pytz.timezone("America/New_York")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@_tool("Get market status", READ_ONLY)
async def get_market_status() -> str:
    """Check whether the US stock market is open now, with its next open and
    close. Reads the exchange clock (holidays and early closes included)."""
    try:
        clock = await get_client().get("/research/market/status")
    except Exception:
        clock = None
    if isinstance(clock, dict) and "isOpen" in clock:
        return json.dumps({
            "is_open": bool(clock.get("isOpen")),
            "next_open": clock.get("nextOpen"),
            "next_close": clock.get("nextClose"),
            "as_of": clock.get("timestamp"),
        }, indent=2)

    # No key, or the clock is unreachable: regular hours in real Eastern time.
    now_et = _utc_now().astimezone(_EASTERN)
    minute = now_et.hour * 60 + now_et.minute
    return json.dumps({
        "is_open": now_et.weekday() < 5 and 9 * 60 + 30 <= minute < 16 * 60,
        "current_time_et": now_et.strftime("%Y-%m-%d %H:%M %Z"),
        "note": ("Regular hours only (9:30 AM-4:00 PM ET, Monday-Friday): this doesn't know market "
                 "holidays or early closes. Authenticate to read the exchange clock."),
    }, indent=2)


@_tool("Get account summary", READ_ONLY)
async def get_account_summary(mode: str = "paper") -> str:
    """Get the Alpaca account's cash, buying power, value and today's P&L.
    For open positions and their P&L, use get_positions.

    Args:
        mode: Account mode - "paper" (default) or "live" for real money
    """
    client = get_client()
    account = await client.get(f"/trading/portfolio/account?mode={mode}")

    # The keys GET /trading/portfolio/account sends (it used to read
    # cash_available / total_value, which it never sent: always 0).
    return json.dumps({
        "mode": mode,
        "cash": _money(account.get("cash")),
        "buying_power": _money(account.get("buying_power")),
        "portfolio_value": _money(_pick(account, "portfolio_value", "equity")),
        "equity": _money(account.get("equity")),
        "day_pnl": _money(account.get("day_pnl")),
        "day_pnl_percent": _money(account.get("day_pnl_percent")),
        "status": account.get("status"),
        "trading_blocked": account.get("trading_blocked"),
    }, indent=2)


@_tool("Search bots", READ_ONLY)
async def search_bots(query: str) -> str:
    """Search for bots by name"""
    client = get_client()
    bots = await client.get("/bots/")
    
    matches = [b for b in bots if query.lower() in b.get("name", "").lower()]
    
    if not matches:
        return f"No bots found matching '{query}'"
    
    results = [{"id": b.get("id"), "name": b.get("name"), "status": b.get("status")} for b in matches]
    return json.dumps({"matches": len(results), "bots": results}, indent=2)


# =============================================================================
# Trade History Tools
# =============================================================================

@_tool("Get trade history", READ_ONLY)
async def get_trade_history(mode: str = "paper", limit: int = 20) -> str:
    """Get recent trade history across all bots.
    
    Args:
        mode: Trading mode - "paper" (default) or "live"
        limit: Number of trades to return (default 20, max 100)
    """
    client = get_client()
    limit = min(limit, 100)
    trades = await client.get(f"/bots/trades/recent?mode={mode}&limit={limit}")
    
    if not trades:
        return json.dumps({"message": f"No {mode} trades found", "trades": []})
    
    # Format trades for readability. The route's rows carry filled_price (the
    # fill) and price (the decision price); there is no filled_avg_price.
    formatted = []
    for t in trades:
        formatted.append({
            "bot_name": t.get("bot_name"),
            "symbol": t.get("symbol"),
            "side": t.get("side"),
            "qty": t.get("qty"),
            "price": _pick(t, "filled_price", "price"),
            "pnl": _money(t.get("pnl")),    # null on an opening trade; 0.0 is a breakeven close
            "timestamp": t.get("timestamp"),
        })
    
    return json.dumps({
        "mode": mode,
        "total": len(formatted),
        "trades": formatted
    }, indent=2)


@_tool("Get bot trades", READ_ONLY)
async def get_bot_trades(bot_id: str, limit: int = 20) -> str:
    """Get a bot's recent trades, newest first, with its whole record.

    `summary` is the bot's whole record (from the bot itself, not this page):
    realized P&L, open and all-time P&L when reported, total_trades (every buy
    and sell), closed_trades (round trips), wins, losses and the win rate
    (wins / closed trades). `shown` covers only the rows returned. A closing
    trade (a sell of a long, or the buy that covers a short) has entry_price,
    exit_price and its P&L; an opening trade has price and no P&L.

    Args:
        bot_id: The bot ID to get trades for
        limit: Number of trades to return (default 20, max 200)
    """
    client = get_client()
    limit = max(1, min(limit, 200))
    trades, bot = await asyncio.gather(
        client.get(f"/trading/bots/{_seg(bot_id)}/trades?limit={limit}"),
        client.get(f"/bots/{_seg(bot_id)}"),
    )
    trades = trades or []

    # TradeSummary rows are camelCase (filledPrice, entryPrice, pnlPercent,
    # createdAt); snake_case is read too in case the serializer changes.
    formatted, shown_pnl, shown_closed = [], 0.0, 0
    for t in trades[:limit]:
        pnl = _money(t.get("pnl"))
        filled = _pick(t, "filledPrice", "filled_price")
        row = {"symbol": t.get("symbol"), "side": t.get("side"), "qty": t.get("qty"),
               "status": t.get("status"), "time": _pick(t, "createdAt", "created_at"), "pnl": pnl}
        if pnl is not None:
            shown_closed += 1
            shown_pnl += pnl
            row["entry_price"] = _pick(t, "entryPrice", "entry_price")
            row["exit_price"] = filled
            row["pnl_percent"] = _money(_pick(t, "pnlPercent", "pnl_percent"))
        else:
            row["price"] = filled
        for out_key, keys in (("reason", ("triggerReason", "trigger_reason")),
                              ("option_action", ("optionAction", "option_action"))):
            value = _pick(t, *keys)
            if value:
                row[out_key] = value
        formatted.append(row)

    wins, losses = int(bot.get("winning_trades") or 0), int(bot.get("losing_trades") or 0)
    out = {
        "bot_id": bot_id,
        "summary": {
            **_bot_pnl(bot),
            "total_trades": int(bot.get("total_trades") or 0),
            "closed_trades": wins + losses,
            "winning_trades": wins,
            "losing_trades": losses,
            "win_rate": _bot_win_rate(bot),
        },
        "shown": {"rows": len(formatted), "closed": shown_closed, "realized_pnl": round(shown_pnl, 2)},
        "trades": formatted,
    }
    if not formatted:
        out["message"] = "No trades found for this bot"
    return json.dumps(out, indent=2)


@_tool("Get daily trade stats", READ_ONLY)
async def get_daily_trade_stats(bot_id: str, days: int = 30) -> str:
    """Get a bot's trading day by day over the last `days` calendar days:
    trades (every buy and sell), closed trades, wins, losses and realized P&L
    (closed trades only; open positions are in get_bot), with the best and
    worst day.

    Args:
        bot_id: The bot ID to get stats for
        days: Number of calendar days to look back, today included (default 30, max 365)
    """
    client = get_client()
    days = max(1, min(int(days), 365))
    start = (_utc_now().date() - timedelta(days=days - 1)).isoformat()
    stats = await client.get(f"/trading/bots/{_seg(bot_id)}/trades/stats/daily?start_date={start}")

    # DailyTradeStats rows: date, total_trades, buy_trades, sell_trades,
    # total_volume, realized_pnl, winning_trades, losing_trades, newest first.
    # (This read trade_count / total_pnl, which the API never sent: every
    # summary said 0 trades and $0.00.) Filtered here as well, for an API
    # that doesn't apply start_date yet.
    rows = [s for s in (stats or []) if str(s.get("date") or "")[:10] >= start]
    if not rows:
        return json.dumps({"bot_id": bot_id, "period_days": days,
                           "message": f"No trades in the last {days} days", "daily_stats": []})

    def pnl(row) -> float:
        return float(row.get("realized_pnl") or 0)

    realized = sum(pnl(s) for s in rows)
    wins = sum(int(s.get("winning_trades") or 0) for s in rows)
    losses = sum(int(s.get("losing_trades") or 0) for s in rows)
    best, worst = max(rows, key=pnl), min(rows, key=pnl)
    return json.dumps({
        "bot_id": bot_id,
        "period_days": days,
        "since": start,
        "summary": {
            "total_trades": sum(int(s.get("total_trades") or 0) for s in rows),
            "closed_trades": wins + losses,
            "winning_trades": wins,
            "losing_trades": losses,
            "win_rate": _win_rate(wins, losses),
            "realized_pnl": round(realized, 2),
            "trading_days": len(rows),
            "avg_realized_pnl_per_trading_day": round(realized / len(rows), 2),
            "best_day": {"date": best.get("date"), "realized_pnl": _money(pnl(best))},
            "worst_day": {"date": worst.get("date"), "realized_pnl": _money(pnl(worst))},
        },
        "daily_stats": rows,
    }, indent=2)


# =============================================================================
# Strategy Tools
# =============================================================================

@_tool("Get bot strategy", READ_ONLY)
async def get_bot_strategy(bot_id: str) -> str:
    """Get the full strategy configuration for a bot.

    Returns entry rules, exit rules, indicators, risk settings, and symbols.

    Args:
        bot_id: The bot ID to get strategy for
    """
    client = get_client()
    strategy = await client.get(f"/strategy/bots/{bot_id}/strategy")

    # Extract key info from strategy_json
    strat_json = strategy.get("strategy_json", {})

    return json.dumps({
        "bot_id": bot_id,
        "version": strategy.get("version", 1),
        "strategy": {
            "symbols": strat_json.get("symbols", []),
            "entry_rules": strat_json.get("entry_rules", []),
            "exit_rules": strat_json.get("exit_rules", []),
            "indicators": strat_json.get("indicators", []),
            "position_sizing": strat_json.get("position_sizing", {}),
            "risk_management": strat_json.get("risk_management", {}),
            "strategy_type": strat_json.get("strategy_type", "standard"),
        },
        "generated_code": strategy.get("generated_code", ""),
        "created_at": strategy.get("created_at"),
    }, indent=2)


@_tool("Explain bot strategy", READ_ONLY)
async def explain_bot_strategy(bot_id: str) -> str:
    """Get a plain English explanation of a bot's strategy.

    Explains entry rules, exit rules, and risk settings in simple terms.

    Args:
        bot_id: The bot ID to explain
    """
    client = get_client()
    explanation = await client.get(f"/strategy/bots/{bot_id}/strategy/explain")
    return json.dumps({
        "bot_id": bot_id,
        "explanation": explanation.get("explanation", ""),
        "entry_rules": explanation.get("entry_rules", ""),
        "exit_rules": explanation.get("exit_rules", ""),
        "risk_settings": explanation.get("risk_settings", "")
    }, indent=2)


@_tool("Refine bot", SAFE_WRITE)
async def refine_bot(bot_id: str, prompt: str) -> str:
    """Refine a bot's strategy using natural language.

    Describe changes you want to make to the strategy. The bot will be paused
    during refinement and a new strategy version will be created.

    Args:
        bot_id: The bot ID to refine
        prompt: Natural language description of changes (e.g., "add a trailing stop of 5%")
    """
    client = get_client()
    bot = await client.post(f"/bots/{bot_id}/refine", {"prompt": prompt})
    return json.dumps({
        "success": True,
        "message": f"Strategy refined for '{bot.get('name')}'",
        "bot": {
            "id": bot.get("id"),
            "name": bot.get("name"),
            "status": bot.get("status"),
            "strategy_version": bot.get("strategy_version")
        }
    }, indent=2)


# Open world: status "running" starts the bot, which may trade a real account.
@_tool("Update bot", CONSEQUENTIAL_EXTERNAL)
async def update_bot(bot_id: str, name: str = None, status: str = None) -> str:
    """Update a bot's name or status.

    Args:
        bot_id: The bot ID to update
        name: New name for the bot (optional)
        status: New status - "running" or "paused" (optional)
    """
    client = get_client()

    data = {}
    if name:
        data["name"] = name
    if status:
        data["status"] = status

    if not data:
        raise ToolError("Provide at least one field to update (name or status)")

    bot = await client.patch(f"/bots/{bot_id}", data)
    return json.dumps({
        "success": True,
        "bot": {
            "id": bot.get("id"),
            "name": bot.get("name"),
            "status": bot.get("status")
        }
    }, indent=2)


# =============================================================================
# Trading Tools
# =============================================================================

@_tool("Get positions", READ_ONLY)
async def get_positions(mode: str = "paper") -> str:
    """Get current open positions with unrealized P&L.

    Args:
        mode: Account mode - "paper" (default) or "live" for real money
    """
    client = get_client()
    positions = await client.get(f"/trading/portfolio/alpaca-positions?mode={mode}")

    if not positions:
        return json.dumps({"message": f"No open {mode} positions", "positions": []})

    # Format positions for readability
    formatted = []
    total_value = 0
    total_pnl = 0

    for p in positions:
        # The route sends unrealized_pnl and unrealized_pnl_percent (already a
        # percent). This read Alpaca's raw unrealized_pl / unrealized_plpc,
        # which it never sends: every position showed $0.00 open P&L.
        market_value = float(p.get("market_value") or 0)
        pct = _pick(p, "unrealized_pnl_percent")
        if pct is None and p.get("unrealized_plpc") is not None:
            pct = float(p["unrealized_plpc"]) * 100
        unrealized_pl = float(_pick(p, "unrealized_pnl", "unrealized_pl") or 0)
        total_value += market_value
        total_pnl += unrealized_pl

        formatted.append({
            "symbol": p.get("symbol"),
            "qty": float(p.get("qty") or 0),
            "avg_entry_price": float(p.get("avg_entry_price") or 0),
            "current_price": float(p.get("current_price") or 0),
            "market_value": round(market_value, 2),
            "unrealized_pnl": round(unrealized_pl, 2),
            "unrealized_pnl_percent": _money(pct),
            "side": p.get("side", "long")
        })

    return json.dumps({
        "mode": mode,
        "total_positions": len(formatted),
        "total_market_value": round(total_value, 2),
        "total_unrealized_pnl": round(total_pnl, 2),
        "positions": formatted
    }, indent=2)


@_tool("Close position", CONSEQUENTIAL_EXTERNAL)
async def close_position(
    symbol: str,
    mode: str = "paper",
    qty: float = None,
    percentage: float = None,
    idempotency_key: Optional[str] = None,
) -> str:
    """Close a position by symbol in the user's VibeTrader Alpaca account.

    This only reaches the Alpaca account (paper or live), the same one
    get_positions shows. It cannot sell holdings at other brokerages the user
    linked (Robinhood, Webull, Fidelity, Coinbase and so on); if they ask for
    that, tell them to close it in that brokerage's app. Never use this to
    "sell my Robinhood shares": it would sell the Alpaca position instead.

    Retrying is safe: the same close sent again within two minutes of the
    last try, or with the same idempotency_key, is never sold twice.

    Args:
        symbol: Stock/crypto symbol to close (e.g., "AAPL", "BTC/USD")
        mode: Account mode - "paper" (default) or "live"
        qty: Specific quantity to close (optional, closes all if not specified)
        percentage: Percentage of position to close, 0-100 (optional)
        idempotency_key: Optional. Any string naming this one close. Pass the
            same key when retrying it; pass a new key to close more on purpose.
    """
    client = get_client()

    data = {}
    if qty is not None:
        data["qty"] = qty
    if percentage is not None:
        data["percentage"] = percentage
    # A partial close sent twice sells twice, so it carries an order id the
    # way place_order does (the "close" key keeps it apart from a sell order).
    data["client_order_id"] = _mcp_order_id({"close": symbol.upper(), **data}, mode, idempotency_key)

    # broker is explicit: the API falls back to "alpaca" when it is
    # missing, and an implicit default is how the wrong account got sold.
    try:
        result = await client.post(f"/trading/portfolio/close/{symbol}?mode={mode}&broker=alpaca", data)
    except ApiUnreachable as e:
        raise ToolError(
            f"The close request got no answer ({e.detail}), so the position may or may not have been closed. "
            "Retry the same call within two minutes (or with the same idempotency_key): a retry never sells "
            "twice. Or check get_positions first."
        ) from e
    except ApiError as e:
        if e.status == 409 and "already sent" in e.detail:
            raise ToolError(
                "This close was already sent (the same close in the last two minutes, or one with this "
                "idempotency_key), so nothing more was sold. Check get_positions. To close more on purpose, "
                "call close_position with a new idempotency_key."
            ) from e
        raise

    return json.dumps({
        "success": True,
        "message": f"Position {symbol} closed",
        "details": result
    }, indent=2)


# An agent that times out retries the call, and a retry with a fresh order id
# is a second real order (or close). So the same order gets the same client_order_id,
# and the API's order log answers the repeat with 409 "already sent".
#   - With an idempotency_key, the id is a hash of the order and the key: the
#     same for as long as the caller reuses the key.
#   - Without one, the id is remembered per caller and order, and reused while
#     tries keep coming within ORDER_ID_WINDOW_SECONDS of the last one. Sliding,
#     not fixed buckets: a bucket edge split a call from its retry (review
#     2026-10-21). The memory is this process's; the service runs as one
#     instance (--max-instances=1), and a restart forgets it.
# The prefix is vt_ticket_ because that's the only client id the order route
# keeps; "mcp" marks where the order came from.
ORDER_ID_WINDOW_SECONDS = 120
# fingerprint (caller, order, mode) -> (client_order_id, last try)
_recent_order_ids: dict[str, tuple[str, float]] = {}


def _wall_time() -> float:
    return time.time()


def _caller_identity() -> str:
    """Who is placing the order, stable across sign-in token refreshes.

    A sign-in token is replaced every hour, so the raw bearer would give a
    retry after a refresh a new order id — and a second order. The connection
    (user + grant) is the caller instead; the gate already verified this
    request's token, so reading its claims here is safe. Keys are their hash.
    """
    token = _resolve_token() or ""
    if not token.startswith("vt_"):
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
            if claims.get("sub") and claims.get("gid"):
                return f"grant:{claims['sub']}:{claims['gid']}"
        except jwt.PyJWTError:
            pass
    return "token:" + hashlib.sha256(token.encode()).hexdigest()


def _mcp_order_id(order: dict, mode: str, idempotency_key: Optional[str]) -> str:
    if idempotency_key:
        seed = json.dumps({"order": order, "mode": mode, "key": idempotency_key}, sort_keys=True)
        return f"vt_ticket_mcp_{hashlib.sha256(seed.encode()).hexdigest()[:32]}"
    now = _wall_time()
    for stale in [k for k, (_, at) in _recent_order_ids.items() if now - at > ORDER_ID_WINDOW_SECONDS]:
        del _recent_order_ids[stale]
    fingerprint = json.dumps({"caller": _caller_identity(), "order": order, "mode": mode}, sort_keys=True)
    remembered = _recent_order_ids.get(fingerprint)
    cid = remembered[0] if remembered else f"vt_ticket_mcp_{uuid.uuid4().hex}"
    _recent_order_ids[fingerprint] = (cid, now)
    return cid


@_tool("Place order", CONSEQUENTIAL_EXTERNAL)
async def place_order(
    symbol: str,
    side: str,
    qty: float = None,
    notional: float = None,
    order_type: str = "market",
    limit_price: float = None,
    mode: str = "paper",
    idempotency_key: Optional[str] = None,
) -> str:
    """Place a buy or sell order in the user's VibeTrader Alpaca account.

    This only reaches the Alpaca account (paper or live). It cannot trade at
    other brokerages the user linked (Robinhood, Webull, Fidelity, Coinbase
    and so on); if they ask for that, tell them it isn't supported here.

    Retrying is safe: the same order sent again within two minutes of the
    last try, or with the same idempotency_key, is never placed twice.

    Args:
        symbol: Stock/crypto symbol (e.g., "AAPL", "BTC/USD")
        side: "buy" or "sell"
        qty: Number of shares/units to trade (optional if using notional)
        notional: Dollar amount to trade (optional, for market orders only)
        order_type: "market" (default) or "limit"
        limit_price: Required for limit orders
        mode: Account mode - "paper" (default) or "live"
        idempotency_key: Optional. Any string naming this one order. Pass the
            same key when retrying it; pass a new key to place a second
            identical order on purpose.
    """
    client = get_client()

    if not qty and not notional:
        raise ToolError("Either qty or notional (dollar amount) required")

    if order_type == "limit" and not limit_price:
        raise ToolError("limit_price required for limit orders")

    data = {
        "symbol": symbol.upper(),
        "side": side.lower(),
        "order_type": order_type,
        "time_in_force": "day"
    }

    if qty:
        data["qty"] = qty
    if notional:
        data["notional"] = notional
    if limit_price:
        data["limit_price"] = limit_price
    data["client_order_id"] = _mcp_order_id(dict(data), mode, idempotency_key)

    try:
        result = await client.post(f"/trading/portfolio/order?mode={mode}&broker=alpaca", data)
    except ApiUnreachable as e:
        raise ToolError(
            f"The order request got no answer ({e.detail}), so the order may or may not have been placed. "
            "Retry the same call within two minutes (or with the same idempotency_key): a retry is never "
            "placed twice. Or check get_trade_history first."
        ) from e
    except ApiError as e:
        if e.status == 409 and "already sent" in e.detail:
            raise ToolError(
                "This order was already placed (the same order in the last two minutes, or one with this "
                "idempotency_key), so it wasn't sent again. Check get_trade_history or get_positions. "
                "To place a second identical order on purpose, call place_order with a new idempotency_key."
            ) from e
        raise

    return json.dumps({
        "success": True,
        "message": f"{side.upper()} order placed for {symbol}",
        "order": result
    }, indent=2)


# =============================================================================
# Backtest Tools
# =============================================================================

@_tool("Run backtest", SAFE_WRITE)
async def run_backtest(
    bot_id: str,
    start_date: str = None,
    end_date: str = None,
    initial_capital: float = 10000.0
) -> str:
    """
    Run a historical backtest for a bot's current strategy.

    Simulates how the strategy would have performed on historical data.
    Returns the return, what simply holding the same symbols over the same
    window returned (buy_hold_return_pct, and vs_buy_and_hold_pct = strategy
    minus holding), max drawdown, win rate and trade count. ignored_conditions
    lists rules the backtest can't model; the live bot still applies them, so
    results can differ. Report the buy-and-hold comparison with the return.

    Args:
        bot_id: The bot ID to backtest
        start_date: Start date (YYYY-MM-DD). Default: 30 days before end_date
            (DCA bots: 6 months; covered-call and wheel bots: 1 year)
        end_date: End date (YYYY-MM-DD). Default: today
        initial_capital: Starting capital for simulation (default: $10,000)

    Note: a standard or DCA backtest covers at most 365 days; a longer range
    is trimmed to its last 365 days.
    """
    client = get_client()

    data = {"initial_capital": initial_capital}
    if start_date:
        data["start_date"] = start_date
    if end_date:
        data["end_date"] = end_date

    result = await client.post(f"/backtest/bots/{bot_id}/backtest", data)

    # Format key metrics
    summary = {
        "success": True,
        "bot_id": bot_id,
        "period": f"{result.get('start_date')} to {result.get('end_date')}",
        "initial_capital": result.get("initial_capital"),
        "final_value": round(result.get("final_value", 0), 2),
        "total_return_pct": round(result.get("total_return", 0), 2),
        "total_pnl": round(result.get("total_pnl", 0), 2),
        "max_drawdown_pct": round(result.get("max_drawdown_percent", 0), 2),
        "total_trades": result.get("total_trades", 0),
        "win_rate_pct": round(result.get("win_rate", 0), 2),
        "avg_trade_pnl": round(result.get("avg_trade_pnl", 0), 2),
        "best_trade": round(result.get("best_trade", 0), 2),
        "worst_trade": round(result.get("worst_trade", 0), 2),
        **_vs_holding(result),
        "backtest_id": result.get("id")
    }

    return json.dumps(summary, indent=2)


def _vs_holding(result: dict) -> dict:
    """The comparison the web app shows with every backtest (2026-10-22 audit
    05-F17): holding the same symbols over the same window, and the rules the
    backtest couldn't model. buy_hold_return_pct is null when the API has no
    benchmark for the run."""
    hold = _money(result.get("buy_hold_return_pct"))
    strategy = _money(result.get("total_return"))
    return {
        "buy_hold_return_pct": hold,
        "vs_buy_and_hold_pct": round(strategy - hold, 2) if hold is not None and strategy is not None else None,
        "ignored_conditions": list(result.get("ignored_conditions") or []),
    }


@_tool("List backtests", READ_ONLY)
async def list_backtests(bot_id: str) -> str:
    """
    List all backtests for a bot.

    Args:
        bot_id: The bot ID to list backtests for

    Returns summary of each backtest including date range, return, and win rate.
    """
    client = get_client()
    backtests = await client.get(f"/backtest/bots/{bot_id}/backtests")

    if not backtests:
        return json.dumps({"message": "No backtests found for this bot", "backtests": []})

    formatted = []
    for bt in backtests:
        formatted.append({
            "id": bt.get("id"),
            "period": f"{bt.get('start_date')} to {bt.get('end_date')}",
            "return_pct": round(bt.get("total_return", 0), 2),
            "buy_hold_return_pct": _money(bt.get("buy_hold_return_pct")),
            "trades": bt.get("total_trades", 0),
            "win_rate_pct": round(bt.get("win_rate", 0), 2),
            "max_drawdown_pct": round(bt.get("max_drawdown_percent", 0), 2)
        })

    return json.dumps({
        "bot_id": bot_id,
        "total_backtests": len(formatted),
        "backtests": formatted
    }, indent=2)


@_tool("Get backtest", READ_ONLY)
async def get_backtest(bot_id: str, backtest_id: str) -> str:
    """
    Get detailed results for a specific backtest.

    Args:
        bot_id: The bot ID
        backtest_id: The backtest ID to retrieve

    Returns full backtest details including trades and equity curve.
    """
    client = get_client()
    result = await client.get(f"/backtest/bots/{bot_id}/backtests/{backtest_id}")

    return json.dumps({
        "success": True,
        "backtest_id": result.get("id"),
        "bot_id": bot_id,
        "period": f"{result.get('start_date')} to {result.get('end_date')}",
        "initial_capital": result.get("initial_capital"),
        "final_value": round(result.get("final_value", 0), 2),
        "total_return_pct": round(result.get("total_return", 0), 2),
        "total_pnl": round(result.get("total_pnl", 0), 2),
        "max_drawdown_pct": round(result.get("max_drawdown_percent", 0), 2),
        "total_trades": result.get("total_trades", 0),
        "winning_trades": result.get("winning_trades", 0),
        "losing_trades": result.get("losing_trades", 0),
        "win_rate_pct": round(result.get("win_rate", 0), 2),
        "avg_trade_pnl": round(result.get("avg_trade_pnl", 0), 2),
        "avg_win": round(result.get("avg_win", 0), 2),
        "avg_loss": round(result.get("avg_loss", 0), 2),
        "best_trade": round(result.get("best_trade", 0), 2),
        "worst_trade": round(result.get("worst_trade", 0), 2),
        "trade_count": len(result.get("trades", [])),
        "equity_curve_points": len(result.get("equity_curve", [])),
        **_vs_holding(result),
    }, indent=2)


@_tool("Quick covered-call backtest", READ_ONLY)
async def quick_covered_call_backtest(
    symbol: str = "NVDA",
    months: int = 12
) -> str:
    """
    Run a quick covered call income strategy backtest.

    Simulates the covered call strategy:
    1. Buy 100 shares of the underlying
    2. Sell monthly covered calls (~30 DTE, 5% OTM)
    3. Collect premium and track assignments

    Args:
        symbol: Stock symbol (default: NVDA)
        months: Number of months to backtest (3-36, default: 12)

    Returns estimated monthly income, annual yield, and assignment frequency.
    """
    client = get_client()
    result = await client.get(f"/backtest/covered-call/quick?symbol={symbol}&months={months}")

    if not result.get("success"):
        raise ToolError(result.get("error") or "Backtest failed")

    return json.dumps({
        "success": True,
        "symbol": result.get("symbol"),
        "period_months": result.get("period_months"),
        "total_return_pct": round(result.get("total_return_pct", 0), 2),
        "total_premium_collected": round(result.get("total_premium", 0), 2),
        "avg_monthly_income": round(result.get("avg_monthly_income", 0), 2),
        "estimated_annual_yield_pct": round(result.get("annual_yield_pct", 0), 2),
        "max_drawdown_pct": round(result.get("max_drawdown_pct", 0), 2),
        "total_assignments": result.get("assignments", 0),
        "calls_sold": result.get("calls_sold", 0)
    }, indent=2)


@_tool("Preview bot", READ_ONLY)
async def preview_bot(bot_id: str) -> str:
    """Quick backtest of a bot, not saved: estimated profit and return, trade
    count, win rate, and what simply holding the same symbols would have
    returned (buy_hold_return_pct); report that comparison with the return.
    The window fits the strategy's timeframe: 45 days on minute bars, 120 on
    hourly, a year on daily or slower (covered calls: 6 months); the message
    names it. ignored_conditions lists rules the preview can't model. Use it
    right after create_bot to see whether the strategy trades at all before
    running it.

    Args:
        bot_id: The bot ID to preview
    """
    client = get_client()
    return json.dumps(await client.get(f"/backtest/bots/{_seg(bot_id)}/preview"), indent=2, default=str)


# =============================================================================
# Signal Mode Tools
# =============================================================================
# A signal bot decides and the user trades, wherever their money is: that
# includes brokerages VibeTrader can't trade, such as Robinhood through its own
# MCP tools. The loop is set_signal_mode -> list_open_signals -> place the order
# there -> report_signal. The API side is the 2026-10-14 signal-mode spec.

def _seg(value: str) -> str:
    """One URL path segment."""
    return quote(str(value), safe="")


# Delivery bookkeeping on a signal; nothing an agent acts on.
_SIGNAL_INTERNAL = ("delivered", "delivery_attempts", "reminder_sent_at")
# The bot's own order for the signal is working (the API's AUTO_ACTIVE): the
# API refuses to resolve it until that order settles.
_BOT_PLACING = ("pending", "submitting", "submitted")
_STATE_READS_AT_ONCE = 8


def _bot_placing(signal: dict) -> bool:
    return (signal.get("auto") or {}).get("state") in _BOT_PLACING


def _public_signal(signal: dict, bot_id: str, bot_name: Optional[str] = None) -> dict:
    out = {k: v for k, v in signal.items() if k not in _SIGNAL_INTERNAL}
    out["bot_id"] = bot_id
    if bot_name:
        out["bot_name"] = bot_name
    return out


@_tool("Set signal mode", CONSEQUENTIAL_EXTERNAL)
async def set_signal_mode(
    bot_id: str,
    entry_mode: Literal["alert", "auto"] = "alert",
    exit_mode: Literal["alert", "auto"] = "alert",
    follow_account: Optional[str] = None,
    signal_capital: Optional[float] = None,
) -> str:
    """Switch a bot between placing its own trades and sending signals.

    On an "alert" side the bot still makes every decision, but instead of
    trading it sends a signal (push, email, webhook, and list_open_signals):
    the user places the trade and reports it with report_signal. An "auto" side
    places the order itself. Each side is set on its own, e.g. alert entries
    with auto exits; "auto" on both turns signals off.

    To switch to signals, a running bot must be paused first and hold no
    positions, and live bots can't switch: create a new bot for signals.
    Signals need a paid plan once the user's 30-day free window ends.

    Args:
        bot_id: The bot to switch
        entry_mode: "alert" (default) or "auto" for entries
        exit_mode: "alert" (default) or "auto" for exits
        follow_account: Where the user places these trades. "manual" means any
            brokerage VibeTrader can't trade, such as Robinhood: the user
            reports each fill with report_signal. Or a connected account:
            "alpaca_paper", "alpaca_live", or another connected brokerage
            account's key. Omit it to keep the bot's current account
            ("manual" for a bot that isn't a signal bot yet).
        signal_capital: Dollars the user trades this bot with. Required with
            "manual" when the bot sizes by percent of portfolio or ATR risk,
            so each signal can say how much to buy. Omit it to keep the
            bot's current amount.
    """
    for side, mode in (("entry_mode", entry_mode), ("exit_mode", exit_mode)):
        if mode not in ("alert", "auto"):
            raise ToolError(f"{side} must be 'alert' or 'auto'")
    client = get_client()
    if follow_account is None or signal_capital is None:
        # The mode route replaces the whole setup, writing a field left out as
        # missing: fill those from the bot's current setup, so changing one
        # side never repoints a bot's account or drops its capital.
        current = await client.get(f"/signals/bots/{_seg(bot_id)}")
        if follow_account is None:
            follow_account = current.get("follow_account") or "manual"
        if signal_capital is None:
            signal_capital = current.get("signal_capital")
    data = {"entry_mode": entry_mode, "exit_mode": exit_mode, "follow_account": follow_account}
    if signal_capital is not None:
        data["signal_capital"] = signal_capital
    result = await client.post(f"/signals/bots/{_seg(bot_id)}/mode", data)
    return json.dumps(result, indent=2, default=str)


@_tool("List open signals", READ_ONLY)
async def list_open_signals(bot_id: Optional[str] = None) -> str:
    """List signals waiting on the user: entries and exits a signal bot decided
    on that the user hasn't placed, skipped or reported yet.

    Each one says what to trade where the user holds the account: buy about
    suggested_qty, or sell qty, near signal_price. Once the user has traded it,
    report what really filled:
      side "buy" or "sell" -> report_signal
      side "option" -> report_option_signal
      side "short" -> report_short_signal
    Signals the bot is placing itself (auto mode) are counted in
    being_placed_by_bot and left out: there is nothing to do for them.

    Args:
        bot_id: One bot's signals. Omit it for every signal bot.
    """
    client = get_client()
    if bot_id:
        targets = [{"id": bot_id}]
    else:
        targets = [b for b in await client.get("/bots/") if _is_signal_bot(b)]

    gate = asyncio.Semaphore(_STATE_READS_AT_ONCE)

    async def read(bot: dict):
        async with gate:
            return await client.get(f"/signals/bots/{_seg(bot['id'])}")

    states = await asyncio.gather(*(read(b) for b in targets), return_exceptions=True)
    open_signals, placing, errors = [], 0, []
    for bot, state in zip(targets, states):
        if isinstance(state, Exception):
            if bot_id:
                raise state
            errors.append({"bot_id": bot["id"], "bot_name": bot.get("name"), "error": str(state)})
            continue
        for signal in state.get("signals") or []:
            if signal.get("status") != "open":
                continue
            if _bot_placing(signal):
                placing += 1
                continue
            open_signals.append(_public_signal(signal, bot["id"], bot.get("name")))

    out = {"count": len(open_signals), "open_signals": open_signals, "being_placed_by_bot": placing}
    if errors:
        out["errors"] = errors
    return json.dumps(out, indent=2, default=str)


_FILL_ACTIONS = ("bought", "sold")
_SIGNAL_ACTIONS = ("bought", "sold", "skip", "keep", "undo")


@_tool("Report a signal trade", SAFE_WRITE)
async def report_signal(
    bot_id: str,
    decision_id: str,
    action: Literal["bought", "sold", "skip", "keep", "undo"],
    qty: Optional[float] = None,
    price: Optional[float] = None,
) -> str:
    """Tell a signal bot what the user did with one of its stock or crypto
    signals (side "buy" or "sell"), so the bot tracks the user's real shares
    and results. This records what happened; it never places an order.

    Actions:
      bought: the user bought on a buy signal. qty and price are the real fill.
      sold: the user sold on a sell signal. qty and price are the real fill.
      skip: the user passed on the signal.
      keep: on a sell signal, the user keeps holding the shares.
      undo: a buy VibeTrader detected at a connected brokerage wasn't for this
        signal. Releases those shares and reopens the signal; never sells.

    Args:
        bot_id: The signal bot
        decision_id: The signal's decision_id, from list_open_signals
        action: bought, sold, skip, keep or undo
        qty: Shares or units filled (bought and sold only)
        price: Average fill price per share or unit (bought and sold only)
    """
    if action not in _SIGNAL_ACTIONS:
        raise ToolError("action must be bought, sold, skip, keep or undo")
    data = None
    if action in _FILL_ACTIONS:
        if not qty or not price or qty <= 0 or price <= 0:
            raise ToolError(f"'{action}' needs the fill's qty and price, both above 0")
        data = {"qty": qty, "price": price}
    result = await get_client().post(
        f"/signals/bots/{_seg(bot_id)}/signals/{_seg(decision_id)}/{action}", data)
    return json.dumps(result, indent=2, default=str)


def _resolution(action: str, qty: Optional[float], price: Optional[float]) -> dict:
    data = {"action": action}
    if qty is not None:
        data["qty"] = qty
    if price is not None:
        data["price"] = price
    return data


_OPTION_SIGNAL_ACTIONS = ("sold", "skip", "expired", "assigned", "called_away", "bought")


@_tool("Report an option signal trade", SAFE_WRITE)
async def report_option_signal(
    bot_id: str,
    decision_id: str,
    action: Literal["sold", "skip", "expired", "assigned", "called_away", "bought"],
    qty: Optional[float] = None,
    price: Optional[float] = None,
) -> str:
    """Tell an options signal bot (covered calls, cash-secured puts, the wheel)
    what the user did with one of its option signals (side "option"). The
    signal's option.action says which actions apply:
      sell_to_open: sold (qty = contracts, price = premium per share) or skip
      settle: expired, assigned or called_away, as the contract ended
      buy_shares: bought (qty and price of the shares) or skip
    This records what happened; it never places an order.

    Args:
        bot_id: The signal bot
        decision_id: The signal's decision_id, from list_open_signals
        action: One of the actions above
        qty: Contracts (sold) or shares (bought), when the action has a fill
        price: Premium per share (sold) or share price (bought)
    """
    if action not in _OPTION_SIGNAL_ACTIONS:
        raise ToolError("action must be sold, skip, expired, assigned, called_away or bought")
    result = await get_client().post(
        f"/signals/bots/{_seg(bot_id)}/signals/{_seg(decision_id)}/option", _resolution(action, qty, price))
    return json.dumps(result, indent=2, default=str)


_SHORT_SIGNAL_ACTIONS = ("shorted", "skip", "covered", "keep")


@_tool("Report a short signal trade", SAFE_WRITE)
async def report_short_signal(
    bot_id: str,
    decision_id: str,
    action: Literal["shorted", "skip", "covered", "keep"],
    qty: Optional[float] = None,
    price: Optional[float] = None,
) -> str:
    """Tell a signal bot what the user did with one of its short signals
    (side "short"). The signal's short.action says which actions apply:
      open: shorted (qty and the price sold short at) or skip
      cover: covered (the price bought back at; qty if only part) or keep
    This records what happened; it never places an order.

    Args:
        bot_id: The signal bot
        decision_id: The signal's decision_id, from list_open_signals
        action: shorted, skip, covered or keep
        qty: Shares shorted or covered
        price: Price sold short at, or bought back at
    """
    if action not in _SHORT_SIGNAL_ACTIONS:
        raise ToolError("action must be shorted, skip, covered or keep")
    result = await get_client().post(
        f"/signals/bots/{_seg(bot_id)}/signals/{_seg(decision_id)}/short", _resolution(action, qty, price))
    return json.dumps(result, indent=2, default=str)


@_tool("Get signal state", READ_ONLY)
async def get_signal_state(bot_id: str) -> str:
    """A signal bot's setup and record: its entry and exit modes (and why a
    side may be alerting instead of trading), the account it follows, the
    user's book (what the user really holds for this bot), the bot's own
    virtual book, results, and its open and ten most recent signals.

    Args:
        bot_id: The signal bot
    """
    state = dict(await get_client().get(f"/signals/bots/{_seg(bot_id)}"))
    signals = state.pop("signals", None) or []
    state["open_signal_count"] = sum(1 for s in signals if s.get("status") == "open")
    state["recent_signals"] = [_public_signal(s, bot_id) for s in signals[:10]]
    return json.dumps(state, indent=2, default=str)


# =============================================================================
# ASGI App — both HTTP transports served from one process
# =============================================================================
# Two transports, one Starlette parent:
#
#   /mcp        streamable HTTP — the modern transport. Unchanged: same path,
#               same session semantics as before this file grew a second one.
#   /sse        the URL the README (and therefore every setup copied from
#               GitHub) has always advertised, which used to 404. It now
#               answers on either transport — see _SseEndpoint — and the SSE
#               half brings its companion POST route at /messages/.
#
# Both sub-apps are built by FastMCP with ABSOLUTE paths and their routes are
# lifted into the parent verbatim rather than Mount()ed under a prefix: the SSE
# transport hands the client a fixed relative "/messages/" URI that takes no
# account of a mount prefix, so those routes have to live at the app root or
# every SSE client posts its messages into a 404.
#
# RequestContextMiddleware is re-applied here because lifting the routes
# bypasses each sub-app's own middleware stack. It is what makes
# get_http_headers() request-scoped, which is what keeps the per-request auth
# above (no shared global token) correct — for BOTH transports.

_streamable_app = mcp.http_app(path="/mcp")
_sse_app = mcp.http_app(path="/sse", transport="sse")


def _handler_at(sub_app: Starlette, path: str):
    """The ASGI handler FastMCP built for `path` inside one of its sub-apps."""
    for route in sub_app.routes:
        if isinstance(route, Route) and route.path == path:
            return route.app
    raise RuntimeError(f"FastMCP registered no route at {path!r}")


_streamable_handler = _handler_at(_streamable_app, "/mcp")
_sse_handler = _handler_at(_sse_app, "/sse")


class _SseEndpoint:
    """Both transports behind the one /sse URL.

    /sse is what the README has advertised for the life of this repo, and the
    clients that followed it did not all arrive with the same transport:

      * A real SSE client opens `GET /sse` and then POSTs its JSON-RPC to the
        /messages/ URL that stream hands it.
      * A client on the current streamable-HTTP transport POSTs JSON-RPC
        straight at /sse. That is what the client stuck against this server
        since at least 2026-08-11 actually sends — Cloud Run logs show POST
        /sse and nothing else, every 1-3 seconds, from one address.

    Serving only the SSE transport here would have turned that client's 404
    into a 405, which is no more connected than before. So dispatch on the
    shape of the request instead: a GET with no session id is an SSE
    handshake; everything else is streamable HTTP, including the GET a
    streamable client opens for its server-to-client stream, which always
    carries the mcp-session-id the server issued it. Legacy SSE never sends
    that header, so the two cases never collide.

    Both paths run through the same FastMCP server and, for streamable HTTP,
    the same session manager as /mcp — /sse and /mcp are one session space,
    not two.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        has_session = any(k == b"mcp-session-id" for k, _ in scope.get("headers", []))
        if scope.get("method", "GET").upper() in ("GET", "HEAD") and not has_session:
            await _sse_handler(scope, receive, send)
        else:
            await _streamable_handler(scope, receive, send)


@asynccontextmanager
async def _lifespan(app: Starlette):
    """Run both transports' lifespans.

    The streamable-HTTP lifespan starts its StreamableHTTPSessionManager, so it
    has to be entered. FastMCP's own server lifespan is re-entrant (it no-ops
    once its result is set), so nesting the SSE lifespan inside is safe and
    starts the shared server exactly once.
    """
    async with _streamable_app.lifespan(app):
        async with _sse_app.lifespan(app):
            yield


_app = Starlette(
    routes=[
        # /mcp — streamable HTTP, exactly as before.
        *_streamable_app.routes,
        # /sse — either transport, see _SseEndpoint.
        Route(
            "/sse",
            endpoint=_SseEndpoint(),
            methods=["GET", "HEAD", "POST", "DELETE"],
        ),
        # /messages/ — the SSE transport's companion POST route. Everything
        # from the SSE sub-app except its own /sse route, which _SseEndpoint
        # now fronts.
        *(r for r in _sse_app.routes if getattr(r, "path", None) != "/sse"),
    ],
    middleware=[Middleware(RequestContextMiddleware)],
    lifespan=_lifespan,
)

# Every request on /mcp, /sse and /messages/ needs a token VibeTrader's sign-in
# issued for this server, or a vt_ key, as an Authorization header; anything
# else gets the 401 that makes Claude and ChatGPT show "Connect". See
# oauth_gate.py.
app = OAuthGate(_app, default_verifier())

if __name__ == "__main__":
    import sys
    port = int(os.getenv("PORT", 8080))
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        _enable_stdio_tools()
        mcp.run()
    else:
        import uvicorn

        # Serve the combined app, not mcp.run(transport="http"), so that
        # `python server.py` offers exactly what the deployment offers
        # (uvicorn server:app) — both /mcp and /sse — instead of only /mcp.
        uvicorn.run(app, host="0.0.0.0", port=port)
