# ClaudeTrader MT5 MCP Server

Windows-native MetaTrader 5 gateway for ClaudeTrader. Built with FastMCP (stdio) and the official `MetaTrader5` Python package. The only component that can touch the broker — write tools are gated, timeouts fail closed.

## Requirements

- Windows 10/11 (native — not WSL2)
- Python 3.10+
- MetaTrader 5 terminal installed and logged into the broker
- Node.js (for MCP Inspector)

## Setup

```powershell
cd mt5-mcp-server
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Credentials — never in config.json

The tracked `config.json` holds **placeholders and defaults only**. Real
credentials go in either of (highest precedence first):

1. Environment variables: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`
2. `config.local.json` (gitignored), overlaid onto `config.json`:

```json
{
  "mt5_path": "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
  "login": 12345678,
  "password": "your-password",
  "server": "OANDA-Demo-1"
}
```

Non-secret settings stay in the tracked `config.json`:

| Key | Default | Purpose |
|-----|---------|---------|
| `timeout_ms` | 60000 | MT5 initialize timeout |
| `default_timeout_sec` | 60 | Per-call timeout before SUSPEND |
| `health_timeout_sec` | 15 | Health-check timeout |
| `allow_real_account` | `false` | Write tools refuse non-demo accounts unless `true` |
| `magic` | 260605 | Magic number stamped on gateway orders |

## Run

```powershell
python server.py
```

## MCP Inspector

```powershell
npx @modelcontextprotocol/inspector python server.py
```

## Safety model

- **Real-account gate** — every write tool calls `ensure_write_allowed()`:
  writes on a non-demo account are refused unless `allow_real_account: true`.
- **Timeout → SUSPEND** — any MT5 call exceeding its timeout flips a global
  suspend flag; all subsequent calls are refused until cleared.
- **Orphaned-write marker** — a timed-out *write* may still execute at the
  broker (the worker thread is abandoned, not killed). The gateway records
  which write timed out (`get_gateway_status`) and the error says so
  explicitly. **Reconcile broker state (`scripts/reconcile.py`) before
  clearing suspend.**
- **Recovery** — `clear_suspend` clears the flag and orphan marker, re-runs
  the health check, and reports what was cleared. `get_gateway_status` and
  `clear_suspend` work *while* suspended; everything else is refused.
- SL/TP on positions can be **moved but never cleared** (deliberate: no
  stopless positions). `SPECIFIED`/`SPECIFIED_DAY` time types are rejected
  (no expiration parameter support). Partial closes are validated against
  the position's actual volume.

## Tests

Cross-platform unit tests (fake `MetaTrader5` module, no terminal needed) run
with the main repo suite: `pytest tests/test_mt5_gateway.py`.

Live gateway tests (Windows, MT5 running, credentials configured):

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

## Tools (16)

| Tool | Type |
|------|------|
| `get_gateway_status` | Read (works while suspended) |
| `clear_suspend` | Ops (works while suspended) |
| `get_account_info` | Read |
| `get_rates` | Read |
| `get_tick` | Read |
| `get_symbol_info` | Read |
| `get_symbols` | Read |
| `get_open_positions` | Read |
| `get_position_by_symbol` | Read |
| `get_pending_orders` | Read |
| `get_history` | Read |
| `place_order` | Write (gated) |
| `modify_order` | Write (gated) |
| `cancel_order` | Write (gated) |
| `close_position` | Write (gated) |
| `modify_position` | Write (gated) |

## Architecture

```
server.py          → FastMCP tool registration + write gating
mt5client.py       → init guard, timeout/suspend machinery, validation,
                     config overlay (config.json ← config.local.json ← env)
constants.py       → TIMEFRAME_MAP, ORDER_TYPE_MAP, FILLING_MAP, TIME_MAP
handlers/          → account, symbols, positions, orders, history
config.json        → tracked defaults (placeholders only — no secrets)
config.local.json  → gitignored credentials overlay
```
