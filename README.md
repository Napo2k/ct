# ClaudeTrader

Autonomous AI trading system for MetaTrader 5 (demo/paper only).

## Architecture

| Layer | Component |
|-------|-----------|
| Data | Massive MCP (indicators, calendar) + MT5 MCP (ticks, account, OHLCV fallback) |
| Reasoning | Claude + `playbook/algo_trading_skill.md` |
| Execution | MT5 MCP server (14 tools) |
| Orchestration | n8n cron → Python cycle → Gitea logs |

## Phase 0 (current)

`EXECUTION_MODE=false` — Claude evaluates and logs decisions. No MT5 write tools called.

Success criteria:
- ≥ 50 cycles logged
- ≥ 95% correct regime classification
- 100% veto compliance
- 100% valid decision JSON
- 0 phantom trades
- Simulated R:R ≥ 1.5:1 on ENTER decisions

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

### 2. Cycle runner

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python scripts/run_cycle.py --dry-run
python scripts/run_cycle.py -v
```

### 3. HTTP trigger (n8n → Windows)

```powershell
# Enable in config/cycle.json: "http_trigger": {"enabled": true, ...}
python scripts/http_trigger.py
```

n8n workflows in `n8n/` call `POST http://host.docker.internal:8787/cycle`.

### 4. n8n import

Import `n8n/trading_cycle.json` (every 15 min Mon–Fri) and `n8n/session_summary.json` (21:15 CET).

## Configuration

| File | Purpose |
|------|---------|
| `config/cycle.json` | Pairs, execution mode, MCP commands, Gitea settings |
| `mt5-mcp-server/config.json` | OANDA demo credentials (placeholder) |
| `ANTHROPIC_API_KEY` | Claude API key (env var, not committed) |
| `EXECUTION_MODE` | Env override: `true`/`false` |

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
