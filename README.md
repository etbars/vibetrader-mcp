# VibeTrader MCP Server

<p align="center">
  <img src="https://vibetrader.markets/icons/VibeTrader%20-%20Logo%20-%20Icon.svg" width="80" alt="VibeTrader Logo">
</p>

<p align="center">
  <strong>Trade stocks directly from your AI assistant</strong><br>
  Create bots, manage portfolios, and execute trades with natural language
</p>

<p align="center">
  <a href="https://vibetrader.markets">Website</a> •
  <a href="https://vibetrader.markets/blog/mcp-server">Documentation</a> •
  <a href="https://vibetrader.markets/settings">Get API Key</a>
</p>

---

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that connects AI assistants like **Claude Desktop**, **Cursor**, and **Windsurf** to VibeTrader's trading platform.

## Features

- **Create Bots**: Create AI-powered trading bots using natural language
- **Manage Bots**: Start, pause, delete, and monitor your bots
- **Portfolio**: View positions and account balance
- **Market Data**: Get quotes and the market clock
- **Signal Mode**: Bots that tell you what to trade at any brokerage, including ones VibeTrader
  can't trade such as Robinhood, and track the fills you report back
- **Backtesting**: Test strategies on historical data

## Available Tools

All 38 are available over either endpoint below.

| Tool | Description |
|------|-------------|
| `authenticate` | Authenticate with your API key from vibetrader.markets/settings |
| `create_bot` | Create a new trading bot using natural language |
| `list_bots` | List all your trading bots |
| `search_bots` | Search for bots by name |
| `get_bot` | Get details of a specific bot |
| `update_bot` | Update a bot's name or status |
| `refine_bot` | Refine a bot's strategy using natural language |
| `start_bot` | Start a paused bot |
| `pause_bot` | Pause a running bot |
| `delete_bot` | Delete a bot |
| `start_all_bots` | Start all paused bots at once |
| `pause_all_bots` | Pause all running bots at once |
| `delete_all_bots` | Delete ALL bots (requires `confirm='DELETE ALL'`) |
| `get_bot_strategy` | Get the full strategy configuration for a bot |
| `explain_bot_strategy` | Get a plain English explanation of a bot's strategy |
| `get_bot_stats` | Get aggregate statistics across your bots |
| `get_daily_trade_stats` | Get daily trading statistics for a bot |
| `get_portfolio` | Get portfolio positions and balance |
| `get_positions` | Get current open positions with unrealized P&L |
| `get_account_summary` | Get account balance, buying power, and key metrics |
| `close_position` | Close a position by symbol (Alpaca account) |
| `place_order` | Place a buy or sell order (Alpaca account; a retry is never placed twice) |
| `get_trade_history` | Get recent trade history across all bots |
| `get_bot_trades` | Get trade history for a specific bot |
| `get_quote` | Get current quote for a stock |
| `list_indicators` | List every technical indicator bots can use, with parameters and outputs (no API key needed) |
| `get_market_status` | Check if US stock markets are currently open |
| `preview_bot` | Quick 30-day backtest of a bot, not saved |
| `run_backtest` | Run a historical backtest for a bot's current strategy |
| `list_backtests` | List all backtests for a bot |
| `get_backtest` | Get detailed results for a specific backtest |
| `quick_covered_call_backtest` | Run a quick covered call income strategy backtest |
| `set_signal_mode` | Switch a bot between placing its own trades and sending signals |
| `list_open_signals` | Signals waiting for you to place, across your signal bots |
| `report_signal` | Record what you did with a stock or crypto signal (bought, sold, skip, keep, undo) |
| `report_option_signal` | Record what you did with an option signal |
| `report_short_signal` | Record what you did with a short signal |
| `get_signal_state` | A signal bot's modes, books, results and recent signals |

## Endpoints

The hosted server speaks both MCP HTTP transports:

| URL | Speaks |
|-----|--------|
| `https://vibetrader-mcp-289016366682.us-central1.run.app/mcp` | Streamable HTTP — the current MCP transport. **Use this one.** It is also what the in-app setup at [vibetrader.markets/settings](https://vibetrader.markets/settings) gives you. |
| `https://vibetrader-mcp-289016366682.us-central1.run.app/sse` | Either transport. A client that opens an SSE stream gets one (with its companion `/messages/` POST route, which the stream advertises — you never configure it by hand); a client that POSTs JSON-RPC gets streamable HTTP, the same session space as `/mcp`. |

Both URLs expose the same 38 tools and the same authentication, so an existing
`/sse` setup keeps working as-is. Prefer `/mcp` for anything new: SSE is
deprecated upstream, and `/sse` is kept working for setups copied from older
versions of this README.

## Setup for Claude Desktop

1. Get your API token from https://vibetrader.markets/settings

2. Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "vibetrader": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://vibetrader-mcp-289016366682.us-central1.run.app/mcp"]
    }
  }
}
```

   Clients that connect to a remote MCP URL directly (Cursor, Windsurf, and
   Claude Desktop's own remote-server support) can use the URL on its own:

```json
{
  "mcpServers": {
    "vibetrader": {
      "url": "https://vibetrader-mcp-289016366682.us-central1.run.app/mcp"
    }
  }
}
```

   `/sse` works too — see [Endpoints](#endpoints) — but `/mcp` is the one to
   use for a new setup.

3. Send your token as a header. **Against the hosted server this is how you
   authenticate** — see the note below:

```json
{
  "mcpServers": {
    "vibetrader": {
      "url": "https://vibetrader-mcp-289016366682.us-central1.run.app/mcp",
      "headers": { "Authorization": "Bearer vt_YOUR_TOKEN" }
    }
  }
}
```

4. Restart Claude Desktop

5. Create bots:
   > "Create a trading bot that buys AAPL when RSI goes below 30"

### A note on the `authenticate` tool

There is an `authenticate` tool, and over a **local stdio** setup
(`python server.py stdio`) it does what you would expect: hand it your token
once and the rest of the session is authenticated.

Over **HTTP** — which is every hosted setup above — use the `Authorization`
header instead. The header is resolved per request and stored nowhere, so it is
also the safer of the two: the server never holds your token at all.

## Example Conversations

**Create a momentum bot:**
> "Create a trading bot that buys AAPL when RSI crosses below 30 and sells when it crosses above 70"

**Check your bots:**
> "Show me all my trading bots and their performance"

**Backtest a strategy:**
> "Create a moving average crossover bot for SPY and backtest it"

**Covered calls:**
> "How much would covered calls on NVDA have paid over the last 12 months?"

**Trade signals at another brokerage:**
> "Switch my RSI bot to signals. I'll place the trades on Robinhood."
>
> "Any signals waiting? Place them on Robinhood and record the fills."

## Signal mode: trade at any brokerage

`place_order` and `close_position` only reach your VibeTrader Alpaca account. For
money anywhere else, put a bot in signal mode. It keeps watching the market and
deciding, but sends you each entry and exit instead of trading:

1. `set_signal_mode(bot_id, follow_account="manual")` (pause the bot first)
2. `list_open_signals()`: what to buy or sell, how much, and near what price
3. Place the order with that brokerage's own tools. With Robinhood's MCP
   connected in the same assistant, the assistant can do this step too.
4. `report_signal(bot_id, decision_id, "bought", qty, price)` with the real fill,
   so the bot tracks your actual shares and results and sends the exits.

Signals also arrive by push, email or webhook, set up in the app.

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (HTTP mode — serves both /mcp and /sse on :8080,
# the same app the deployment runs)
python server.py

# ...or exactly as the container does
uvicorn server:app --host 0.0.0.0 --port 8080

# Run in STDIO mode (for a local Claude Desktop install)
python server.py stdio
```

Then point a client at `http://localhost:8080/mcp` or `http://localhost:8080/sse`.

## Deployment

Deployed on Google Cloud Run at `mcp.vibetrader.markets`

```bash
gcloud run deploy vibetrader-mcp --source . --region us-central1
```

## Security

- Your API token is required for authentication
- All requests are made over HTTPS
- Tokens are resolved per request and never shared between users: over HTTP the
  `Authorization: Bearer vt_...` header is read from your own request and stored
  nowhere at all
- Paper trading mode is default for safety
- Every tool declares MCP annotations, so your client can run read-only tools
  without asking and confirm the ones that trade, start bots or delete
- `place_order` gives the same order the same order id while retries keep
  coming within two minutes of the last try, so retrying a call (say, after a
  timeout) never places a second order. Pass `idempotency_key` to hold the id
  for as long as you like
