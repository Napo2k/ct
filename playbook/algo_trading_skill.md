# ClaudeTrader Algo Trading Skill

Version: 1.0.0  
Phase: 0 (evaluation + logging only)

## Pairs & Timeframes

- **Pairs:** EURUSD (primary), GBPUSD, USDJPY
- **Timeframes:** H4 (trend/regime), H1 (entry signals), M15 (timing)

## Regime Classification (run first — gates everything)

| Condition | Regime |
|-----------|--------|
| ADX H4 < 20 | RANGING — range rules apply, no trend entries |
| ADX H4 ≥ 20 + EMA50 > EMA200 + price > EMA50 | TRENDING BULLISH |
| ADX H4 ≥ 20 + EMA50 < EMA200 + price < EMA50 | TRENDING BEARISH |
| Mixed EMA alignment | TRANSITIONAL — no new entries, tighten SLs to BE+0.5×ATR |
| ADX > 60 | OVEREXTENDED — no new entries, manage open only |

## Volatility Modifier

| Condition | Action |
|-----------|--------|
| ATR > 1.5× 20-day avg | HIGH VOL: lot 50%, widen SL/TP 1.3× |
| ATR < 0.7× 20-day avg | LOW VOL: lot 75%, standard or tighter TP |

## Trend Entry Checklist (all 5 required for HIGH confidence)

1. **Regime:** TRENDING (correct direction)
2. **RSI H1:** 40–65 (long) or 35–60 (short) — not overbought/oversold
3. **MACD H1:** signal line crossover correct direction OR histogram expanding
4. **Price:** > EMA50 H1 (long) or < EMA50 H1 (short)
5. **No veto conditions active**

Scoring: 5/5 = HIGH (full lot) | 3–4/5 = MEDIUM (75%) | <3 = HOLD

## Indicators

- **EMA 50/200 on H4:** macro trend filter, permitted direction
- **ADX 14 on H4+H1:** trend strength (<20 ranging, 25–40 trending, >60 overextended)
- **RSI 14 on H1+M15:** momentum + divergence detection
- **MACD 12/26/9 on H1:** momentum confirmation (never sole trigger)
- **ATR 14 on H1:** SL = 1.5×ATR, TP = 2.5×ATR (R:R = 1:1.67 minimum)

## Risk Rules (hard limits — override all signals)

- SL: 1.5× ATR from entry | TP: 2.5× ATR from entry
- Max concurrent positions: 3 total, 1 per pair
- Max daily drawdown: 2% → SUSPEND session
- Max intraday drawdown: 1.5% → close all, SUSPEND
- 3 consecutive losses → reduce to 50% lot, HIGH confidence entries only
- Free margin < 200% required margin → no new entries

## Veto Conditions (checked first — block all new entries)

- Outside 07:00–21:00 CET Mon–Fri
- First 15 min after London open (07:00–07:15)
- Last 30 min before NY close (20:30–21:00)
- Friday 18:00+ → close all open positions
- HIGH impact news in next 60 min → no entries
- HIGH impact news in next 30 min → close all positions (emergency)
- Spread > 2.0 pips EURUSD (or > 1.5× average for other pairs)
- Daily drawdown ≥ 1% → no new entries for session
- Daily drawdown ≥ 2% → SUSPEND

## Exit Rules

- TP/SL hit: MT5 handles automatically (set at entry)
- Position open > 48h without TP → close at market on next cycle
- Trend reversal (EMA50 crosses EMA200 opposite to trade) → close immediately
- ADX collapses < 20 while in trend trade → close (trend exhausted)
- RSI divergence against position → close 50%, tighten SL to BE
- Regime → TRANSITIONAL → tighten SL to BE+0.5×ATR

## Execution Strategy

- **Default entry:** BUY_LIMIT / SELL_LIMIT (pending orders)
- **Market entries:** only with `entry_window` guard; reject if price outside window
- **Never** place market orders without fresh tick validation

## Required Reasoning Format (every non-HOLD decision)

```
1. VETO CHECK: list each veto condition and PASS/FAIL
2. REGIME CLASSIFICATION: ADX value, EMA alignment, price position
3. SIGNAL EVALUATION: each checklist item with value and PASS/FAIL
4. RISK CALCULATION: ATR, SL pips, TP pips, R:R ratio, lot size
5. DECISION + CONFIDENCE
```

## Decision Output Schema

Respond with a single JSON object (no markdown fences):

```json
{
  "action": "ENTER" | "EXIT" | "MODIFY" | "HOLD" | "SUSPEND",
  "pair": "EURUSD",
  "direction": "LONG" | "SHORT" | null,
  "order_type": "BUY_LIMIT" | "SELL_LIMIT" | "BUY" | "SELL",
  "lot_size": 0.01,
  "entry_price": 1.08420,
  "entry_window": [1.0835, 1.0850],
  "stop_loss": 1.08180,
  "take_profit": 1.08900,
  "reasoning": "...",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "cycle_id": "2026-06-05T14:30:00Z"
}
```
