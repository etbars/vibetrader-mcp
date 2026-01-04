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
- **Market Data**: Get quotes and options chains
- **Backtesting**: Test strategies on historical data

## Available Tools

| Tool | Description |
|------|-------------|
| `authenticate` | Connect with your VibeTrader API token |
| `create_bot` | Create a trading bot from natural language |
| `list_bots` | List all your bots |
| `get_bot` | Get detailed bot info |
| `start_bot` | Start a paused bot |
| `pause_bot` | Pause a running bot |
| `delete_bot` | Delete a bot |
| `set_trading_mode` | Switch between paper/live trading |
| `get_portfolio` | View your positions |
| `get_trade_history` | See recent trades |
| `get_quote` | Get stock/ETF quotes |
| `get_options_chain` | Get options data |
| `backtest_strategy` | Backtest a strategy |
| `list_strategy_templates` | See available templates |

## Setup for Claude Desktop

1. Get your API token from https://vibetrader.markets/settings

2. Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "vibetrader": {
      "url": "https://vibetrader-mcp-289016366682.us-central1.run.app/sse"
    }
  }
}
```

3. Restart Claude Desktop

4. In a conversation, first authenticate:
   > "Use the vibetrader authenticate tool with my token: [YOUR_TOKEN]"

5. Then create bots:
   > "Create a trading bot that buys AAPL when RSI goes below 30"

## Example Conversations

**Create a momentum bot:**
> "Create a trading bot that buys AAPL when RSI crosses below 30 and sells when it crosses above 70"

**Check your bots:**
> "Show me all my trading bots and their performance"

**Backtest a strategy:**
> "Backtest a moving average crossover strategy on SPY for the last 6 months"

**Options trading:**
> "Create an iron condor bot for SPY that enters when IV rank is above 50"

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (HTTP mode)
python server.py

# Run in STDIO mode (for Claude Desktop)
python server.py stdio
```

## Deployment

Deployed on Google Cloud Run at `mcp.vibetrader.markets`

```bash
gcloud run deploy vibetrader-mcp --source . --region us-central1
```

## Security

- Your API token is required for authentication
- All requests are made over HTTPS
- Tokens are not stored on the server (stateless)
- Paper trading mode is default for safety
