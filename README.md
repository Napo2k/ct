# ClaudeTrader

Autonomous AI trading system for MetaTrader 5.

## Architecture

| Layer | Component |
|-------|-----------|
| Data | Massive MCP (indicators, calendar) + MT5 MCP (ticks, account, OHLCV indicator fallback) |
| Reasoning | Claude (`claude-opus-4-8`, adaptive thinking) + `playbook/algo_trading_skill.md` |
| Execution | MT5 MCP server (16 tools, real-account write guard, orphan-write tracking) |
| Safety | Kill switch, heartbeat, fail-closed vetoes, equity-risk sizing, webhook alerts |
| Orchestration | n8n cron → Python cycle → Gitea logs |

## Phase 1 (current)

`EXECUTION_MODE=true` — paper execution on OANDA demo via MT5 MCP write tools.

- Default entries: `BUY_LIMIT` / `SELL_LIMIT` (pending orders)
- Market entries require `entry_window` guard
- Risk guards: max 3 positions, 1/pair, free margin ≥ 200%
- Emergency close on veto (Friday 18:00+, news < 30 min, intraday drawdown ≥ 1.5%)
- `SUSPEND` closes all positions

## Phase 2 (live trading)

Live execution requires **all three** of the following — anything less silently runs as Phase 1:

1. `"phase": 2` in `config/cycle.json`
2. `"live_confirmation": "I-UNDERSTAND-THIS-TRADES-REAL-MONEY"` in config
3. `LIVE_TRADING=true` environment variable

Plus the MT5 gateway refuses writes on non-demo accounts unless `allow_real_account: true`
is set in `mt5-mcp-server/config.json` (fourth, independent gate at the broker boundary).
`mock_mode`/`mock_llm` combined with phase 2 raise a `ConfigError` instead of degrading.

Live-mode hardening (on top of Phase 1 guards):

- **Fail-closed vetoes** — missing ticks, unreadable spreads, stale ticks (> `safety.max_tick_age_seconds`), or an unavailable news feed block new entries
- **Equity risk sizing** — entries denied if the stop-loss risks more than `risk.risk_per_trade_pct` of equity; absolute `risk.max_lot_size` cap; `risk.max_trades_per_day` limit
- **Confidence gate** — ENTER below `risk.live_min_confidence` (default HIGH) is downgraded to HOLD
- **Kill switch** — create `data/KILL_SWITCH` (or `POST /killswitch`) to close all positions and halt until the file is manually removed
- **Heartbeat** — `data/heartbeat.json` written every cycle; `/health` reports its age for watchdogs
- **Alerts** — set `alerts.webhook_url` to get JSON webhooks on SUSPEND, emergency close, protective exits, errors, and executed entries
- **Pre-entry exposure refresh** — positions/pending orders re-fetched from the broker immediately before placing an order
- Cycle ID embedded in MT5 order comments for broker-side audit trails

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
mt5-mcp-server/     # Windows-native MT5 MCP gateway (16 tools, real-account guard)
cycle/              # Cycle orchestration (prefilter, veto, regime, LLM, risk,
                    #   manage, verifier, postmortem, safety, alerts, store)
backtest/           # Historical replay harness (simulated broker + metrics)
playbook/           # Algo trading skill + auto-generated lessons.md
config/             # cycle.json configuration
scripts/            # run_cycle, http_trigger, run_backtest, review_trades,
                    #   reconcile, price_watcher, dashboard
n8n/                # Workflow definitions for import
logs/               # Cycle logs → Gitea
tests/              # Unit tests
```

## Backtesting

Replays the exact production pipeline (indicators → prefilter → veto →
decision → risk guards → simulated fills) over historical H1 bars:

```bash
python scripts/run_backtest.py --synthetic 5000              # smoke run, no data needed
python scripts/run_backtest.py --csv EURUSD=data/history/EURUSD_H1.csv
python scripts/run_backtest.py --csv EURUSD=... --live-llm   # real Claude decisions ($)
```

### Fetching real history (OANDA v20 API)

```bash
cp .env.example .env          # fill in OANDA_API_TOKEN + OANDA_ACCOUNT_ID
python scripts/fetch_oanda_history.py --pairs EURUSD,GBPUSD,USDJPY --years 3
```

Credentials live in `.env` (gitignored, stdlib loader — no python-dotenv
dependency). All entry points pick it up via `load_config()`; real environment
variables always take precedence over the file.

Downloads completed H1 mid-price candles from the practice environment
(`api-fxpractice.oanda.com` — same environment as the demo account; `--env live`
for a live token) into `data/history/{PAIR}_H1.csv`, paginated 5000 candles per
request with 429 backoff. The token comes from the environment only — never a
flag, never logged.

Reports win rate, expectancy (R), profit factor, max drawdown, veto/prefilter
rates, and a full trade list (`--out results.json`). Fills are conservative:
stop-first when SL and TP share a bar, no favorable slippage. The default
strategy is a deterministic implementation of the playbook checklist, so
playbook changes can be measured before they touch the LLM prompt.

## Learning loop

```bash
python scripts/review_trades.py --days 1          # post-mortem closed trades
```

Closed trades are matched back to the decision logs that opened them
(via the cycle-id embedded in order comments), reviewed by Claude (process
over outcome), and distilled into `playbook/lessons.md` — which is appended
to the system prompt on every future cycle. Lessons are capped at 20, newest
kept, duplicates skipped.

## Reasoning layer

- **Structured outputs** — decisions are constrained to a JSON schema at the
  API level; malformed-JSON failures are gone by construction
- **Prompt caching** — the playbook system prompt is cached (`cache_playbook`)
- **Read-only tools** (`anthropic.enable_tools`) — Claude may call an
  allowlisted read-only MT5 subset (rates, ticks, positions) to investigate
  before deciding; write tools are never exposed; hard cap of 5 tool rounds
- **Adversarial verifier** (`verifier` block, auto-on in live mode) — a second
  Haiku call tries to refute every ENTER; refuted or errored verification
  blocks the entry in live mode (fail closed)

### Multi-provider routing (`llm_router`)

An agentic routing layer (off by default) decides per cycle which LLM makes
the trading decision:

1. A cheap **triage agent** (default: Groq, generous free tier) classifies the
   cycle — `routine` (flat, no signals), `standard` (warm entry signals), or
   `critical` (open positions with exit signals, drawdown pressure, live mode)
   — and proposes a provider.
2. The proposal is **sanitized**: unknown providers rejected, providers without
   API keys or in rate-limit cooldown skipped, and **live mode clamps the
   choice to `live_approved`** (default: Anthropic only). Free tiers earn
   trust in paper mode, not with real money.
3. Failures escalate through `fallback_order`, always ending at Anthropic;
   HTTP 429 puts a provider into a persisted cooldown
   (`data/provider_state.json`, 15 min default).

Supported providers (stdlib HTTP, keys via `.env` only): **Anthropic** (full
path: structured outputs + tools + caching), **Groq**, **Google Gemini**,
**Mistral**, **Cohere**, **OpenAI** (JSON-mode text, parsed by the same robust
parser). Whatever model decides, the deterministic pipeline — veto override,
confidence gate, adversarial verifier, risk checks — applies unchanged, and
routing metadata (provider, complexity, attempts) lands in every cycle log.
Model IDs live in the `llm_router.providers` registry — update them there as
providers release new models.

## Portfolio risk & position management

- Correlated-exposure veto (EURUSD/GBPUSD 0.85 default matrix, configurable)
  and net per-currency exposure caps in lots — long EURUSD + short USDJPY is
  recognized as a double-short on USD
- `cycle/manage.py` runs every execution cycle, veto or not: break-even SL
  move at ≥1R, ATR trailing stop at ≥1.5R, one-time 50% partial close on an
  RSI extreme against the position (only when in profit)

## Ops tooling

| Script | Purpose |
|--------|---------|
| `scripts/reconcile.py --days 7` | Diff broker deals vs decision logs; flags phantom and unmatched trades (exit 1 on discrepancy) |
| `scripts/price_watcher.py` | Polls ticks; POSTs `/cycle` when price moves > threshold pips between crons |
| `streamlit run scripts/dashboard.py` | Equity curve, decision mix, pipeline rates, trade stats, lessons |
| `data/claudetrader.db` | SQLite store of every cycle + closed trade (auto-populated, best-effort) |

## Quick start

### 1. MT5 MCP server (Windows)

```powershell
cd mt5-mcp-server
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
# Put credentials in config.local.json (gitignored) or MT5_* env vars
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

# MT5-only mode works without Massive MCP — indicators computed from OHLCV bars
```

When Massive MCP is unavailable, `cycle/indicators.py` computes RSI, MACD, ATR, ADX, and EMAs from MT5 `get_rates` data so regime classification and pre-filter still run.

### 2b. Batch mock cycles (toward 50-cycle goal)

```bash
python scripts/run_batch.py --count 50
```

Rotates through mock scenarios including `trending_bullish`, `intraday_drawdown`, `open_position`, and `cold_market`.

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
| `/cycle` | POST | Run one evaluation cycle (`?mock=1` for offline test; rejected in live mode) |
| `/health` | GET | Liveness + phase/session snapshot + kill-switch state + heartbeat age |
| `/summary` | GET | End-of-session P&L (`?date=YYYY-MM-DD` optional) |
| `/killswitch` | POST | Engage the kill switch (body: `{"reason": "..."}`); disengage by deleting `data/KILL_SWITCH` |

n8n workflows in `n8n/` call `POST /cycle` every 15 min and `GET /summary` at 21:15 CET.

### 4. n8n import

Import `n8n/trading_cycle.json` (every 15 min Mon–Fri) and `n8n/session_summary.json` (21:15 CET).

## Configuration

| File | Purpose |
|------|---------|
| `config/cycle.json` | Pairs, execution mode, MCP commands, Gitea settings |
| `mt5-mcp-server/config.local.json` | Broker credentials (gitignored; or MT5_* env vars) |
| `ANTHROPIC_API_KEY` | Claude API key (env var, not committed) |
| `phase` | `2` = live (requires gates below), `1` = paper execution, `0` = evaluation only |
| `live_confirmation` | Must equal `I-UNDERSTAND-THIS-TRADES-REAL-MONEY` for phase 2 |
| `LIVE_TRADING` | Env var, must be `true` for phase 2 |
| `EXECUTION_MODE` | Env override: `true`/`false` |
| `mock_mode` / `MOCK_MODE` | Offline fixture data, skip MCP connections (rejected in live mode) |
| `mock_llm` / `MOCK_LLM` | Use deterministic HOLD instead of Anthropic API (rejected in live mode) |
| `risk` | `risk_per_trade_pct`, `max_lot_size`, `max_trades_per_day`, `live_min_confidence` |
| `safety` | `max_tick_age_seconds`, `kill_switch_file` |
| `alerts` | `webhook_url`, `min_severity`, `timeout_seconds` |

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
