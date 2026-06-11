"""Backtest engine — replays the production decision pipeline over history.

Reuses the real modules (fetch_market_state, check_vetoes, filter_pairs,
validate_decision, execute_decision, manage_positions) against a
SimulatedBroker, so what you measure is what would actually run — including
risk guards, vetoes, prefilter skips, and protective position management.
"""

from __future__ import annotations

import inspect
import logging
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from backtest.broker import SimulatedBroker
from backtest.metrics import compute_metrics
from cycle.decision import DecisionValidationError, hold_decision, validate_decision
from cycle.executor import emergency_close_all, execute_decision
from cycle.manage import manage_positions
from cycle.market_state import fetch_market_state
from cycle.prefilter import filter_pairs
from cycle.session_state import lot_multiplier_for_losses
from cycle.veto import check_vetoes

logger = logging.getLogger(__name__)

DEFAULT_WARMUP_BARS = 1000  # 1000 H1 bars → 250 H4 bars for EMA200/ADX warmup
DEFAULT_CYCLE_EVERY = 4     # evaluate once per 4 H1 bars (~ every 4 hours)

DecideFn = Callable[..., Any]  # (market_state, cycle_id, pairs) -> decision dict


def playbook_rule_decision(
    market_state: dict[str, Any],
    cycle_id: str,
    pairs: list[str],
    *,
    base_lot: float = 0.01,
) -> dict[str, Any]:
    """Deterministic implementation of the playbook trend checklist.

    Lets backtests measure the strategy without LLM cost; the LLM path can be
    plugged in via decide_fn for comparison runs.
    """
    for pair in pairs:
        indicators = market_state.get("indicators", {}).get(pair, {})
        h1 = indicators.get("H1", {})
        regime = (indicators.get("regime") or {}).get("regime")
        rsi = h1.get("rsi")
        atr = h1.get("atr")
        price = h1.get("price")
        ema50 = h1.get("ema50")
        macd_hist = h1.get("macd_histogram")
        if None in (rsi, atr, price, ema50, macd_hist) or atr <= 0:
            continue

        direction = None
        if regime == "TRENDING_BULLISH" and 40 <= rsi <= 65 and macd_hist > 0 and price > ema50:
            direction = "LONG"
        elif regime == "TRENDING_BEARISH" and 35 <= rsi <= 60 and macd_hist < 0 and price < ema50:
            direction = "SHORT"
        if direction is None:
            continue

        side = 1 if direction == "LONG" else -1
        # Limit slightly inside current price so normal noise fills it.
        entry = round(price - side * 0.1 * atr, 5)
        sl = round(entry - side * 1.5 * atr, 5)
        tp = round(entry + side * 2.5 * atr, 5)
        return {
            "action": "ENTER",
            "pair": pair,
            "direction": direction,
            "order_type": "BUY_LIMIT" if direction == "LONG" else "SELL_LIMIT",
            "lot_size": base_lot,
            "entry_price": entry,
            "entry_window": None,
            "stop_loss": sl,
            "take_profit": tp,
            "reasoning": (
                "1. VETO CHECK: handled by engine\n"
                f"2. REGIME CLASSIFICATION: {regime}\n"
                f"3. SIGNAL EVALUATION: RSI {rsi:.1f}, MACD hist {macd_hist:+.5f}, "
                f"price vs EMA50 {price - ema50:+.5f}\n"
                f"4. RISK CALCULATION: ATR {atr:.5f}, SL 1.5xATR, TP 2.5xATR\n"
                f"5. DECISION: ENTER {direction}"
            ),
            "confidence": "HIGH",
            "cycle_id": cycle_id,
        }

    return hold_decision(pairs[0] if pairs else "EURUSD", cycle_id, "No checklist setup")


async def run_backtest(
    bars_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    start_balance: float = 10_000.0,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    cycle_every: int = DEFAULT_CYCLE_EVERY,
    decide_fn: DecideFn | None = None,
    base_lot_size: float = 0.01,
    max_positions: int = 3,
    risk_config: dict[str, Any] | None = None,
    prefilter_config: dict[str, Any] | None = None,
    spread_limits_pips: dict[str, float] | None = None,
    manage_config: dict[str, Any] | None = None,
    enable_manage: bool = True,
    timezone_name: str = "Europe/Berlin",
    max_daily_drawdown_pct: float = 2.0,
    max_intraday_drawdown_pct: float = 1.5,
) -> dict[str, Any]:
    pairs = sorted(bars_by_symbol)
    broker = SimulatedBroker(bars_by_symbol, start_balance=start_balance)
    decide = decide_fn or playbook_rule_decision
    spread_limits = spread_limits_pips or {p: 3.0 for p in pairs}
    manage_state_dir = tempfile.mkdtemp(prefix="ct_backtest_")
    tz = ZoneInfo(timezone_name)

    total_bars = min(len(bars) for bars in bars_by_symbol.values())
    if total_bars <= warmup_bars:
        raise ValueError(
            f"Not enough bars: {total_bars} <= warmup {warmup_bars}"
        )

    equity_curve: list[dict[str, Any]] = []
    cycles = 0
    skipped_by_prefilter = 0
    blocked_by_veto = 0
    consecutive_losses = 0
    seen_closed = 0
    session_date = None
    daily_start_balance = start_balance
    session_peak_equity = start_balance
    trades_today = 0
    suspended_until_date = None

    for index in range(warmup_bars, total_bars, cycle_every):
        broker.advance_to(index)
        now = datetime.fromtimestamp(broker.current_time(), tz=timezone.utc)
        cycle_id = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        cycles += 1

        # Loss streak from newly closed trades (drives the lot multiplier)
        for trade in broker.closed_trades[seen_closed:]:
            if trade["profit"] < -0.01:
                consecutive_losses += 1
            elif trade["profit"] > 0.01:
                consecutive_losses = 0
        seen_closed = len(broker.closed_trades)

        local_date = now.astimezone(tz).date()
        if local_date != session_date:
            session_date = local_date
            daily_start_balance = broker.equity()
            session_peak_equity = daily_start_balance
            trades_today = 0
        equity = broker.equity()
        session_peak_equity = max(session_peak_equity, equity)
        equity_curve.append({"time": broker.current_time(), "equity": equity})

        if suspended_until_date and local_date <= suspended_until_date:
            continue

        market_state = await fetch_market_state(pairs, broker, None)

        veto = check_vetoes(
            now,
            timezone=timezone_name,
            account=market_state.get("account"),
            ticks=market_state.get("ticks"),
            spread_limits_pips=spread_limits,
            news_events=None,
            max_daily_drawdown_pct=max_daily_drawdown_pct,
            max_intraday_drawdown_pct=max_intraday_drawdown_pct,
            daily_start_balance=daily_start_balance or None,
            session_peak_equity=session_peak_equity or None,
        )

        if veto.emergency_close or veto.suspend:
            await emergency_close_all(broker, reason="backtest veto")
            if veto.suspend:
                suspended_until_date = local_date
            continue

        if enable_manage:
            await manage_positions(
                broker,
                market_state,
                manage_config=manage_config,
                state_dir=manage_state_dir,
            )

        active_pairs, _warm = filter_pairs(
            pairs, market_state, prefilter_config=prefilter_config
        )
        if not active_pairs:
            skipped_by_prefilter += 1
            continue

        raw = decide(market_state, cycle_id, active_pairs)
        if inspect.isawaitable(raw):
            raw = await raw
        try:
            decision = validate_decision(raw, cycle_id=cycle_id)
        except DecisionValidationError as exc:
            logger.debug("Invalid decision at %s: %s", cycle_id, exc)
            continue

        if decision["action"] == "ENTER":
            if veto.blocked:
                blocked_by_veto += 1
                continue
            multiplier = lot_multiplier_for_losses(consecutive_losses)
            if multiplier < 1.0:
                decision["lot_size"] = round(decision["lot_size"] * multiplier, 2)
                if decision["lot_size"] <= 0:
                    continue

        execution = await execute_decision(
            decision,
            broker,
            execution_mode=True,
            cycle_id=cycle_id,
            market_state=market_state,
            max_positions=max_positions,
            base_lot_size=base_lot_size,
            consecutive_losses=consecutive_losses,
            risk_config=risk_config,
            trades_today=trades_today,
        )
        if decision["action"] == "ENTER" and execution.get("executed"):
            trades_today += 1

    broker.close_all_at_market()
    final_equity = broker.balance
    equity_curve.append({"time": broker.current_time(), "equity": final_equity})

    report = compute_metrics(broker.closed_trades, equity_curve, start_balance)
    report.update({
        "cycles": cycles,
        "skipped_by_prefilter": skipped_by_prefilter,
        "blocked_by_veto": blocked_by_veto,
        "bars": total_bars,
        "pairs": pairs,
        "start_balance": start_balance,
    })
    return {
        "report": report,
        "trades": broker.closed_trades,
        "equity_curve": equity_curve,
    }
