"""
VibeTrader MCP Server

Based on official FastMCP documentation for HTTP deployment.
https://gofastmcp.com/deployment/http
"""

import os
import json
import logging
from typing import Optional
from datetime import datetime

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("vibetrader-mcp")

# VibeTrader API configuration
API_BASE_URL = os.getenv("VIBETRADER_API_URL", "https://api.vibetrader.markets")

# Initialize FastMCP
mcp = FastMCP(
    "VibeTrader",
    instructions="Create and manage AI-powered trading bots. Use 'authenticate' first with your API key from vibetrader.markets/settings"
)


# =============================================================================
# API Client
# =============================================================================

class VibeTraderClient:
    def __init__(self, api_key: str):
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
    
    async def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        async with httpx.AsyncClient() as client:
            url = f"{API_BASE_URL}{endpoint}"
            response = await client.request(method, url, headers=self.headers, timeout=30.0, **kwargs)
            if response.status_code >= 400:
                error = response.json().get("detail", response.text) if response.text else "Unknown error"
                raise Exception(f"API Error ({response.status_code}): {error}")
            return response.json()
    
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
#   1. The request's own `Authorization: Bearer vt_...` header (stateless
#      clients that send the key on every call — the most secure path).
#   2. A token stored against THIS MCP session id by the `authenticate` tool
#      (preserves the "authenticate once" UX; isolated per session).
#   3. A single-process fallback for local stdio use (no HTTP context, one
#      user) — never reachable when running under HTTP.
#
# get_http_headers() is request-scoped via contextvars, so each concurrent
# task sees only its own request — that is what fixes the cross-user bleed.

# session_id -> validated vt_ key. Only used under HTTP (multi-user).
_tokens_by_session: dict[str, str] = {}
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


def _session_id() -> Optional[str]:
    return _http_headers().get("mcp-session-id")


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
    # 2. Per-session token set via the authenticate tool (HTTP, multi-user).
    sid = _session_id()
    if sid and sid in _tokens_by_session:
        return _tokens_by_session[sid]
    # 3. Local stdio single-user fallback (no HTTP context).
    if not _is_http_context():
        return _stdio_token
    return None


def get_client() -> VibeTraderClient:
    token = _resolve_token()
    if not token:
        raise Exception(
            "Not authenticated! Use the 'authenticate' tool with your API key, "
            "or send it as an 'Authorization: Bearer vt_...' header."
        )
    return VibeTraderClient(token)


# =============================================================================
# Core Tools
# =============================================================================

@mcp.tool()
async def authenticate(api_key: str) -> str:
    """Authenticate with your API key from vibetrader.markets/settings"""
    success, result = await validate_token(api_key)
    if not success:
        return f"❌ Authentication failed: {result}"

    # Store the validated key scoped to THIS caller only — never a shared
    # global (see the per-request auth section above for why).
    sid = _session_id()
    if sid:
        _tokens_by_session[sid] = api_key
        return f"✅ Authenticated as {result}. You can now use all tools!"
    if not _is_http_context():
        # Local stdio: single user, safe to hold in-process.
        global _stdio_token
        _stdio_token = api_key
        return f"✅ Authenticated as {result}. You can now use all tools!"
    # HTTP request with no session id (unusual): can't isolate safely, so
    # don't store a shared token. Direct the client to header auth instead.
    return (
        f"✅ Key valid for {result}, but this client didn't supply an MCP "
        f"session. Configure your MCP client to send "
        f"'Authorization: Bearer {api_key[:6]}...' on each request."
    )


@mcp.tool()
async def list_bots(mode: Optional[str] = None) -> str:
    """List all your trading bots.
    
    Args:
        mode: Filter by trading mode - "paper", "live", or None for all bots
    """
    client = get_client()
    bots = await client.get("/bots/")
    if mode:
        bots = [b for b in bots if b.get("trading_mode") == mode]
    if not bots:
        return json.dumps({"message": f"No {mode or ''} bots found", "bots": []})
    summary = [{"id": b.get("id"), "name": b.get("name"), "status": b.get("status"), 
                "trading_mode": b.get("trading_mode"), "total_pnl": round(b.get("total_pnl", 0), 2)} 
               for b in bots[:10]]
    return json.dumps({"total": len(bots), "bots": summary}, indent=2)


@mcp.tool()
async def get_bot(bot_id: str) -> str:
    """Get details of a specific bot"""
    client = get_client()
    return json.dumps(await client.get(f"/bots/{bot_id}"), indent=2)


@mcp.tool()
async def create_bot(prompt: str, name: Optional[str] = None) -> str:
    """Create a new trading bot using natural language"""
    client = get_client()
    data = {"prompt": prompt}
    if name:
        data["name"] = name
    bot = await client.post("/bots/", data)
    return json.dumps({"status": "success", "bot": {"id": bot.get("id"), "name": bot.get("name")}}, indent=2)


@mcp.tool()
async def start_bot(bot_id: str) -> str:
    """Start a paused bot"""
    client = get_client()
    await client.post(f"/bots/{bot_id}/start")
    return "✅ Bot started"


@mcp.tool()
async def pause_bot(bot_id: str) -> str:
    """Pause a running bot"""
    client = get_client()
    await client.post(f"/bots/{bot_id}/pause")
    return "✅ Bot paused"


@mcp.tool()
async def delete_bot(bot_id: str) -> str:
    """Delete a bot"""
    client = get_client()
    await client.delete(f"/bots/{bot_id}")
    return "✅ Bot deleted"


@mcp.tool()
async def get_portfolio(mode: str = "paper") -> str:
    """Get portfolio positions and balance.
    
    Args:
        mode: Account mode - "paper" (default) or "live" for real money
    """
    client = get_client()
    account = await client.get(f"/trading/portfolio/account?mode={mode}")
    positions = await client.get(f"/trading/portfolio/alpaca-positions?mode={mode}")
    return json.dumps({"mode": mode, "account": account, "positions": positions}, indent=2)


@mcp.tool()
async def get_quote(symbol: str) -> str:
    """Get current quote for a stock"""
    client = get_client()
    return json.dumps(await client.get(f"/trading/quote/{symbol}"), indent=2)


# =============================================================================
# Bulk Actions
# =============================================================================

@mcp.tool()
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
    
    paused = 0
    for bot in running:
        try:
            await client.post(f"/bots/{bot['id']}/pause")
            paused += 1
        except:
            pass
    
    return f"✅ Paused {paused} of {len(running)} {mode or ''} bots"


@mcp.tool()
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
    
    started = 0
    for bot in paused:
        try:
            await client.post(f"/bots/{bot['id']}/start")
            started += 1
        except:
            pass
    
    return f"✅ Started {started} of {len(paused)} {mode or ''} bots"


@mcp.tool()
async def delete_all_bots(confirm: str, mode: Optional[str] = None) -> str:
    """Delete ALL bots. Requires confirm='DELETE ALL' to proceed.
    
    Args:
        confirm: Must be 'DELETE ALL' to confirm deletion
        mode: Filter by trading mode - "paper", "live", or None for all bots
    """
    if confirm != "DELETE ALL":
        return "❌ To delete all bots, pass confirm='DELETE ALL'"
    
    client = get_client()
    bots = await client.get("/bots/")
    if mode:
        bots = [b for b in bots if b.get("trading_mode") == mode]
    
    if not bots:
        return f"No {mode or ''} bots to delete"
    
    deleted = 0
    for bot in bots:
        try:
            await client.delete(f"/bots/{bot['id']}")
            deleted += 1
        except:
            pass
    
    return f"✅ Deleted {deleted} of {len(bots)} {mode or ''} bots"


# =============================================================================
# Utility Tools
# =============================================================================

@mcp.tool()
async def get_bot_stats(mode: Optional[str] = None) -> str:
    """Get aggregate statistics across your bots.
    
    Args:
        mode: Filter by trading mode - "paper", "live", or None for all bots
    """
    client = get_client()
    bots = await client.get("/bots/")
    if mode:
        bots = [b for b in bots if b.get("trading_mode") == mode]
    
    if not bots:
        return json.dumps({"message": f"No {mode or ''} bots found"})
    
    stats = {
        "mode_filter": mode or "all",
        "total_bots": len(bots),
        "running": len([b for b in bots if b.get("status") == "running"]),
        "paused": len([b for b in bots if b.get("status") == "paused"]),
        "paper_trading": len([b for b in bots if b.get("trading_mode") == "paper"]),
        "live_trading": len([b for b in bots if b.get("trading_mode") == "live"]),
        "total_pnl": round(sum(b.get("total_pnl", 0) for b in bots), 2),
        "profitable_bots": len([b for b in bots if b.get("total_pnl", 0) > 0])
    }
    
    sorted_by_pnl = sorted(bots, key=lambda x: x.get("total_pnl", 0), reverse=True)
    stats["top_performer"] = {"name": sorted_by_pnl[0].get("name"), "pnl": sorted_by_pnl[0].get("total_pnl", 0)}
    stats["worst_performer"] = {"name": sorted_by_pnl[-1].get("name"), "pnl": sorted_by_pnl[-1].get("total_pnl", 0)}
    
    return json.dumps(stats, indent=2)


@mcp.tool()
async def get_market_status() -> str:
    """Check if US stock markets are currently open"""
    now = datetime.utcnow()
    # Convert to ET (UTC-5 or UTC-4 depending on DST)
    et_hour = (now.hour - 5) % 24
    
    is_weekday = now.weekday() < 5
    is_market_hours = (et_hour > 9 or (et_hour == 9 and now.minute >= 30)) and et_hour < 16
    
    return json.dumps({
        "is_open": is_weekday and is_market_hours,
        "current_time_utc": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "note": "Markets open 9:30 AM - 4:00 PM ET, Monday-Friday"
    }, indent=2)


@mcp.tool()
async def get_account_summary(mode: str = "paper") -> str:
    """Get account balance, buying power, and key metrics.
    
    Args:
        mode: Account mode - "paper" (default) or "live" for real money
    """
    client = get_client()
    portfolio = await client.get(f"/trading/portfolio/account?mode={mode}")
    
    return json.dumps({
        "cash_available": portfolio.get("cash_available", 0),
        "buying_power": portfolio.get("buying_power", 0),
        "portfolio_value": portfolio.get("total_value", 0),
        "unrealized_pnl": portfolio.get("total_unrealized_pnl", 0),
        "position_count": portfolio.get("position_count", 0)
    }, indent=2)


@mcp.tool()
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

@mcp.tool()
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
    
    # Format trades for readability
    formatted = []
    for t in trades:
        formatted.append({
            "bot_name": t.get("bot_name"),
            "symbol": t.get("symbol"),
            "side": t.get("side"),
            "qty": t.get("qty"),
            "price": t.get("filled_avg_price"),
            "pnl": round(t.get("pnl", 0), 2) if t.get("pnl") else None,
            "timestamp": t.get("timestamp"),
        })
    
    return json.dumps({
        "mode": mode,
        "total": len(formatted),
        "trades": formatted
    }, indent=2)


@mcp.tool()
async def get_bot_trades(bot_id: str, limit: int = 20) -> str:
    """Get trade history for a specific bot.
    
    Args:
        bot_id: The bot ID to get trades for
        limit: Number of trades to return (default 20, max 200)
    """
    client = get_client()
    limit = min(limit, 200)
    trades = await client.get(f"/trading/bots/{bot_id}/trades?limit={limit}")
    
    if not trades:
        return json.dumps({"message": "No trades found for this bot", "trades": []})
    
    # Calculate summary stats
    total_pnl = sum(t.get("pnl", 0) or 0 for t in trades)
    winning = len([t for t in trades if (t.get("pnl") or 0) > 0])
    losing = len([t for t in trades if (t.get("pnl") or 0) < 0])
    
    formatted = []
    for t in trades[:limit]:
        formatted.append({
            "symbol": t.get("symbol"),
            "side": t.get("side"),
            "qty": t.get("qty"),
            "entry_price": t.get("entry_price"),
            "exit_price": t.get("exit_price"),
            "pnl": round(t.get("pnl", 0), 2) if t.get("pnl") else None,
            "pnl_percent": round(t.get("pnl_percent", 0), 2) if t.get("pnl_percent") else None,
            "timestamp": t.get("timestamp"),
        })
    
    return json.dumps({
        "bot_id": bot_id,
        "summary": {
            "total_trades": len(trades),
            "total_pnl": round(total_pnl, 2),
            "winning_trades": winning,
            "losing_trades": losing,
            "win_rate": round(winning / len(trades) * 100, 1) if trades else 0
        },
        "trades": formatted
    }, indent=2)


@mcp.tool()
async def get_daily_trade_stats(bot_id: str, days: int = 30) -> str:
    """Get daily trading statistics for a bot.
    
    Args:
        bot_id: The bot ID to get stats for
        days: Number of days to look back (default 30)
    """
    client = get_client()
    stats = await client.get(f"/trading/bots/{bot_id}/trades/stats/daily")
    
    if not stats:
        return json.dumps({"message": "No daily stats available", "stats": []})
    
    # Limit to requested days
    stats = stats[:days]
    
    # Calculate totals
    total_trades = sum(s.get("trade_count", 0) for s in stats)
    total_pnl = sum(s.get("total_pnl", 0) for s in stats)
    best_day = max(stats, key=lambda x: x.get("total_pnl", 0)) if stats else None
    worst_day = min(stats, key=lambda x: x.get("total_pnl", 0)) if stats else None
    
    return json.dumps({
        "bot_id": bot_id,
        "period_days": len(stats),
        "summary": {
            "total_trades": total_trades,
            "total_pnl": round(total_pnl, 2),
            "avg_daily_pnl": round(total_pnl / len(stats), 2) if stats else 0,
            "best_day": {"date": best_day.get("date"), "pnl": best_day.get("total_pnl")} if best_day else None,
            "worst_day": {"date": worst_day.get("date"), "pnl": worst_day.get("total_pnl")} if worst_day else None,
        },
        "daily_stats": stats
    }, indent=2)


# =============================================================================
# Strategy Tools
# =============================================================================

@mcp.tool()
async def get_bot_strategy(bot_id: str) -> str:
    """Get the full strategy configuration for a bot.
    
    Returns entry rules, exit rules, indicators, risk settings, and symbols.
    
    Args:
        bot_id: The bot ID to get strategy for
    """
    client = get_client()
    
    try:
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
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def explain_bot_strategy(bot_id: str) -> str:
    """Get a plain English explanation of a bot's strategy.
    
    Explains entry rules, exit rules, and risk settings in simple terms.
    
    Args:
        bot_id: The bot ID to explain
    """
    client = get_client()
    
    try:
        explanation = await client.get(f"/strategy/bots/{bot_id}/strategy/explain")
        return json.dumps({
            "bot_id": bot_id,
            "explanation": explanation.get("explanation", ""),
            "entry_rules": explanation.get("entry_rules", ""),
            "exit_rules": explanation.get("exit_rules", ""),
            "risk_settings": explanation.get("risk_settings", "")
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def refine_bot(bot_id: str, prompt: str) -> str:
    """Refine a bot's strategy using natural language.
    
    Describe changes you want to make to the strategy. The bot will be paused
    during refinement and a new strategy version will be created.
    
    Args:
        bot_id: The bot ID to refine
        prompt: Natural language description of changes (e.g., "add a trailing stop of 5%")
    """
    client = get_client()
    
    try:
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
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
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
        return json.dumps({"error": "Provide at least one field to update (name or status)"})
    
    try:
        bot = await client.patch(f"/bots/{bot_id}", data)
        return json.dumps({
            "success": True,
            "bot": {
                "id": bot.get("id"),
                "name": bot.get("name"),
                "status": bot.get("status")
            }
        }, indent=2)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Trading Tools
# =============================================================================

@mcp.tool()
async def get_positions(mode: str = "paper") -> str:
    """Get current open positions with unrealized P&L.
    
    Args:
        mode: Account mode - "paper" (default) or "live" for real money
    """
    client = get_client()
    
    try:
        positions = await client.get(f"/trading/portfolio/alpaca-positions?mode={mode}")
        
        if not positions:
            return json.dumps({"message": f"No open {mode} positions", "positions": []})
        
        # Format positions for readability
        formatted = []
        total_value = 0
        total_pnl = 0
        
        for p in positions:
            market_value = float(p.get("market_value", 0))
            unrealized_pl = float(p.get("unrealized_pl", 0))
            total_value += market_value
            total_pnl += unrealized_pl
            
            formatted.append({
                "symbol": p.get("symbol"),
                "qty": float(p.get("qty", 0)),
                "avg_entry_price": float(p.get("avg_entry_price", 0)),
                "current_price": float(p.get("current_price", 0)),
                "market_value": round(market_value, 2),
                "unrealized_pnl": round(unrealized_pl, 2),
                "unrealized_pnl_percent": round(float(p.get("unrealized_plpc", 0)) * 100, 2),
                "side": p.get("side", "long")
            })
        
        return json.dumps({
            "mode": mode,
            "total_positions": len(formatted),
            "total_market_value": round(total_value, 2),
            "total_unrealized_pnl": round(total_pnl, 2),
            "positions": formatted
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def close_position(symbol: str, mode: str = "paper", qty: float = None, percentage: float = None) -> str:
    """Close a position by symbol.
    
    Args:
        symbol: Stock/crypto symbol to close (e.g., "AAPL", "BTC/USD")
        mode: Account mode - "paper" (default) or "live"
        qty: Specific quantity to close (optional, closes all if not specified)
        percentage: Percentage of position to close, 0-100 (optional)
    """
    client = get_client()
    
    try:
        data = {}
        if qty is not None:
            data["qty"] = qty
        if percentage is not None:
            data["percentage"] = percentage
        
        result = await client.post(f"/trading/portfolio/close/{symbol}?mode={mode}", data if data else None)
        
        return json.dumps({
            "success": True,
            "message": f"Position {symbol} closed",
            "details": result
        }, indent=2)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def place_order(
    symbol: str,
    side: str,
    qty: float = None,
    notional: float = None,
    order_type: str = "market",
    limit_price: float = None,
    mode: str = "paper"
) -> str:
    """Place a buy or sell order.
    
    Args:
        symbol: Stock/crypto symbol (e.g., "AAPL", "BTC/USD")
        side: "buy" or "sell"
        qty: Number of shares/units to trade (optional if using notional)
        notional: Dollar amount to trade (optional, for market orders only)
        order_type: "market" (default) or "limit"
        limit_price: Required for limit orders
        mode: Account mode - "paper" (default) or "live"
    """
    client = get_client()
    
    if not qty and not notional:
        return json.dumps({"error": "Either qty or notional (dollar amount) required"})
    
    if order_type == "limit" and not limit_price:
        return json.dumps({"error": "limit_price required for limit orders"})
    
    try:
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
        
        result = await client.post(f"/trading/portfolio/order?mode={mode}", data)
        
        return json.dumps({
            "success": True,
            "message": f"{side.upper()} order placed for {symbol}",
            "order": result
        }, indent=2)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Backtest Tools
# =============================================================================

@mcp.tool()
async def run_backtest(
    bot_id: str,
    start_date: str = None,
    end_date: str = None,
    initial_capital: float = 10000.0
) -> str:
    """
    Run a historical backtest for a bot's current strategy.
    
    Simulates how the strategy would have performed on historical data.
    Returns ROI, max drawdown, win rate, trade count, and equity curve.
    
    Args:
        bot_id: The bot ID to backtest
        start_date: Start date (YYYY-MM-DD). Default: 30 days ago
        end_date: End date (YYYY-MM-DD). Default: today
        initial_capital: Starting capital for simulation (default: $10,000)
    
    Note: Max 30-day lookback for standard bots. Covered call bots support up to 3 years.
    """
    try:
        client = get_client()
        
        data = {"initial_capital": initial_capital}
        if start_date:
            data["start_date"] = start_date
        if end_date:
            data["end_date"] = end_date
        
        result = await client.post(f"/bots/{bot_id}/backtest", data)
        
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
            "backtest_id": result.get("id")
        }
        
        return json.dumps(summary, indent=2)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def list_backtests(bot_id: str) -> str:
    """
    List all backtests for a bot.
    
    Args:
        bot_id: The bot ID to list backtests for
    
    Returns summary of each backtest including date range, return, and win rate.
    """
    try:
        client = get_client()
        backtests = await client.get(f"/bots/{bot_id}/backtests")
        
        if not backtests:
            return json.dumps({"message": "No backtests found for this bot", "backtests": []})
        
        formatted = []
        for bt in backtests:
            formatted.append({
                "id": bt.get("id"),
                "period": f"{bt.get('start_date')} to {bt.get('end_date')}",
                "return_pct": round(bt.get("total_return", 0), 2),
                "trades": bt.get("total_trades", 0),
                "win_rate_pct": round(bt.get("win_rate", 0), 2),
                "max_drawdown_pct": round(bt.get("max_drawdown_percent", 0), 2)
            })
        
        return json.dumps({
            "bot_id": bot_id,
            "total_backtests": len(formatted),
            "backtests": formatted
        }, indent=2)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def get_backtest(bot_id: str, backtest_id: str) -> str:
    """
    Get detailed results for a specific backtest.
    
    Args:
        bot_id: The bot ID
        backtest_id: The backtest ID to retrieve
    
    Returns full backtest details including trades and equity curve.
    """
    try:
        client = get_client()
        result = await client.get(f"/bots/{bot_id}/backtests/{backtest_id}")
        
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
            "equity_curve_points": len(result.get("equity_curve", []))
        }, indent=2)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
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
    try:
        client = get_client()
        result = await client.get(f"/covered-call/quick?symbol={symbol}&months={months}")
        
        if not result.get("success"):
            return json.dumps({"success": False, "error": result.get("error", "Backtest failed")})
        
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
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# ASGI App
# =============================================================================

app = mcp.http_app()

if __name__ == "__main__":
    import sys
    port = int(os.getenv("PORT", 8080))
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        mcp.run()
    else:
        mcp.run(transport="http", host="0.0.0.0", port=port)
