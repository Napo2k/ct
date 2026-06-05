# ClaudeTrader MT5 MCP Server

Windows-native MetaTrader 5 gateway for ClaudeTrader. Built with FastMCP (stdio) and the official `MetaTrader5` Python package.

## Requirements

- Windows 10/11 (native — not WSL2)
- Python 3.10+
- MetaTrader 5 terminal installed and logged into OANDA demo
- Node.js (for MCP Inspector)

## Setup

```powershell
cd mt5-mcp-server
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Populate `config.json` with your OANDA demo credentials:

```json
{
  "mt5_path": "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
  "login": 12345678,
  "password": "your-demo-password",
  "server": "OANDA-Demo-1",
  "timeout_ms": 60000,
  "default_timeout_sec": 60,
  "health_timeout_sec": 15
}
```

## Run

```powershell
python server.py
```

## MCP Inspector

```powershell
npx @modelcontextprotocol/inspector python server.py
```

## Gateway Tests (7)

With MT5 running and `config.json` populated:

```powershell
python tests/gateway_tests.py
```

| # | Test | Tool |
|---|------|------|
| 1 | Health check | `ensure_initialized` (15s timeout) |
| 2 | Account info | `get_account_info` |
| 3 | Symbol list | `get_symbols` |
| 4 | Symbol specs | `get_symbol_info` |
| 5 | OHLCV bars | `get_rates` |
| 6 | Live tick | `get_tick` |
| 7 | Read positions/orders | `get_open_positions` + `get_pending_orders` |

## Tools (14)

| Tool | Type |
|------|------|
| `get_account_info` | Read |
| `get_rates` | Read |
| `get_tick` | Read |
| `get_symbol_info` | Read |
| `get_symbols` | Read |
| `get_open_positions` | Read |
| `get_position_by_symbol` | Read |
| `get_pending_orders` | Read |
| `get_history` | Read |
| `place_order` | Write |
| `modify_order` | Write |
| `cancel_order` | Write |
| `close_position` | Write |
| `modify_position` | Write |

## Architecture

```
server.py          → FastMCP tool registration
mt5client.py       → ensure_initialized(), _run_with_timeout(), validation
constants.py       → TIMEFRAME_MAP, ORDER_TYPE_MAP, FILLING_MAP, TIME_MAP
handlers/          → account, symbols, positions, orders, history
config.json        → credentials (not committed with real values)
```
