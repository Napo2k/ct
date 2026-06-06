# ClaudeTrader

Autonomous AI trading system for MetaTrader 5 (demo/paper only).

## Architecture

| Layer | Component |
|-------|-----------|
| Data | Massive MCP (indicators, calendar) + MT5 MCP (ticks, account, OHLCV fallback) |
| Reasoning | Claude + `playbook/algo_trading_skill.md` |
| Execution | MT5 MCP server (14 tools) |
| Orchestration | n8n cron → Python cycle → Gitea logs |

## Phase 1 (current)

`EXECUTION_MODE=true` — paper execution on OANDA demo via MT5 MCP write tools.

- Default entries: `BUY_LIMIT` / `SELL_LIMIT` (pending orders)
- Market entries require `entry_window` guard
- Risk guards: max 3 positions, 1/pair, free margin ≥ 200%
- Emergency close on veto (Friday 18:00+, news < 30 min)
- `SUSPEND` closes all positions

**Without live broker:** use mock execution to test the full Phase 1 path:

```bash
python scripts/run_cycle.py --mock -v          # MockMT5 records place_order/modify/close
python scripts/run_batch.py --count 20         # Batch with execution_mode=true from config
python scripts/audit_phase1.py                 # Audit Phase 1 logs
```

Set `EXECUTION_MODE=false` or `phase: 0` in config to revert to evaluation-only mode.

### Phase 0 criteria (completed via mock)

- ≥ 50 cycles logged · regime accuracy · veto compliance · valid JSON · R:R ≥ 1.5

## Project layout

```
mt5-mcp-server/     # Windows-native MT5 MCP gateway (14 tools)
cycle/              # Cycle orchestration (prefilter, veto, regime, LLM, logging)
playbook/           # Algo trading skill (versioned with logs)
config/             # cycle.json configuration
scripts/            # run_cycle.py, http_trigger.py
n8n/                # Workflow definitions for import
logs/               # Cycle logs → Gitea
tests/              # Unit tests
```

## Quick start

### 1. MT5 MCP server (Windows)

```powershell
cd mt5-mcp-server
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
# Populate config.json with OANDA demo credentials
python tests/gateway_tests.py
npx @modelcontextprotocol/inspector python server.py
```

### 2. Cycle runner (offline — no MT5 required)

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Offline mock cycle (fixture data + mock LLM, no API key needed)
python scripts/run_cycle.py --mock -v

# Mock data + real Claude reasoning (needs ANTHROPIC_API_KEY)
export ANTHROPIC_API_KEY=sk-ant-...
python scripts/run_cycle.py --mock --live-llm -v

# Live broker feeds (requires MT5 gateway on Windows)
python scripts/run_cycle.py -v
```

### 2b. Batch mock cycles (toward 50-cycle goal)

```bash
python scripts/run_batch.py --count 50
```

Rotates through 6 scenarios: `trending_bullish`, `ranging`, `overextended`, `high_spread`, `open_position`, `cold_market`.

### 2d. Session summary (21:15 CET report)

```bash
python scripts/session_summary.py
python scripts/session_summary.py --date 2026-06-06
```

Session state (`data/session_state.json`) tracks daily start balance, consecutive losses, and lot multiplier.

### 2c. Phase 0 audit

```bash
python scripts/audit_phase0.py --logs-dir logs --min-cycles 50
```

Checks: valid JSON, veto compliance, phantom trades, R:R ≥ 1.5, regime accuracy ≥ 95%.

### 3. HTTP trigger (n8n → Windows)

```powershell
# Enable in config/cycle.json: "http_trigger": {"enabled": true, ...}
python scripts/http_trigger.py --force
```

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/cycle` | POST | Run one evaluation cycle (`?mock=1` for offline test) |
| `/health` | GET | Liveness + phase/session snapshot |
| `/summary` | GET | End-of-session P&L (`?date=YYYY-MM-DD` optional) |

n8n workflows in `n8n/` call `POST /cycle` every 15 min and `GET /summary` at 21:15 CET.

### 4. n8n import

Import `n8n/trading_cycle.json` (every 15 min Mon–Fri) and `n8n/session_summary.json` (21:15 CET).

## Configuration

| File | Purpose |
|------|---------|
| `config/cycle.json` | Pairs, execution mode, MCP commands, Gitea settings |
| `mt5-mcp-server/config.json` | OANDA demo credentials (placeholder) |
| `ANTHROPIC_API_KEY` | Claude API key (env var, not committed) |
| `phase` | `1` = paper execution, `0` = evaluation only |
| `EXECUTION_MODE` | Env override: `true`/`false` |
| `mock_mode` / `MOCK_MODE` | Offline fixture data, skip MCP connections |
| `mock_llm` / `MOCK_LLM` | Use deterministic HOLD instead of Anthropic API |

## Cycle flow

```
n8n cron (15 min)
  → HTTP POST /cycle (Windows)
    → fetch market_state (MT5 + Massive MCP)
    → veto checks (programmatic)
    → pre-filter (skip LLM if no warm signals)
    → Claude + playbook (if active)
    → validate decision JSON
    → execute (skipped in Phase 0)
    → write log → Gitea commit
```

## Logs

```
logs/YYYY-MM-DD/HH-MM-SS_{PAIR}_{ACTION}.json
```

Each log contains: decision JSON, market_state snapshot, execution result, playbook version.

## Tests

```bash
pip install pytest
pytest tests/ -v
```
