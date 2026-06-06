"""Deterministic position-management exit signals (safety net independent of the LLM)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

HARD = "HARD"
SOFT = "SOFT"

DEFAULT_MAX_AGE_HOURS = 48.0
ADX_TREND_FLOOR = 20.0
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0


@dataclass
class ExitSignal:
    name: str
    severity: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "severity": self.severity, "detail": self.detail}


def _position_side(position: dict[str, Any]) -> str | None:
    """Return LONG/SHORT from MT5 position 'type' (0=buy, 1=sell)."""
    raw = position.get("type")
    if raw in (0, "0", "BUY", "POSITION_TYPE_BUY"):
        return "LONG"
    if raw in (1, "1", "SELL", "POSITION_TYPE_SELL"):
        return "SHORT"
    return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_unix_time(value: Any) -> int | None:
    if value is None:
        return None
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    if ts > 1_000_000_000_000:
        ts //= 1000
    return ts if ts > 0 else None


def evaluate_position_exit(
    position: dict[str, Any],
    indicators: dict[str, Any],
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
) -> list[ExitSignal]:
    """Compute protective exit signals for a single open position."""
    signals: list[ExitSignal] = []
    side = _position_side(position)
    if side is None:
        return signals

    h4 = indicators.get("H4", {})
    h4_prev = indicators.get("H4_prev", {})
    h1 = indicators.get("H1", {})

    ema50 = _float(h4.get("ema50"))
    ema200 = _float(h4.get("ema200"))
    prev_ema50 = _float(h4_prev.get("ema50"))
    prev_ema200 = _float(h4_prev.get("ema200"))

    if ema50 is not None and ema200 is not None:
        reversed_now = (side == "LONG" and ema50 < ema200) or (
            side == "SHORT" and ema50 > ema200
        )
        crossed = False
        if prev_ema50 is not None and prev_ema200 is not None:
            aligned_before = (side == "LONG" and prev_ema50 >= prev_ema200) or (
                side == "SHORT" and prev_ema50 <= prev_ema200
            )
            crossed = aligned_before and reversed_now
        if crossed or reversed_now:
            signals.append(
                ExitSignal(
                    "trend_reversal",
                    HARD,
                    f"{side} position but H4 EMA50 {ema50} vs EMA200 {ema200} reversed",
                )
            )

    adx = _float(h4.get("adx"))
    if adx is not None and adx < ADX_TREND_FLOOR:
        signals.append(
            ExitSignal(
                "adx_collapse",
                SOFT,
                f"H4 ADX {adx:.1f} < {ADX_TREND_FLOOR} — trend exhausted",
            )
        )

    rsi = _float(h1.get("rsi"))
    if rsi is not None:
        if side == "LONG" and rsi >= RSI_OVERBOUGHT:
            signals.append(
                ExitSignal("rsi_extreme", SOFT, f"H1 RSI {rsi:.1f} overbought for LONG")
            )
        elif side == "SHORT" and rsi <= RSI_OVERSOLD:
            signals.append(
                ExitSignal("rsi_extreme", SOFT, f"H1 RSI {rsi:.1f} oversold for SHORT")
            )

    open_ts = _parse_unix_time(position.get("time") or position.get("time_setup"))
    tp = _float(position.get("tp") or position.get("take_profit"))
    if open_ts is not None and (tp is None or tp <= 0):
        current = now or datetime.now(timezone.utc)
        age_hours = (int(current.timestamp()) - open_ts) / 3600
        if age_hours > max_age_hours:
            signals.append(
                ExitSignal(
                    "stale_age",
                    HARD,
                    f"open {age_hours:.1f}h > {max_age_hours}h without take-profit",
                )
            )

    return signals


def evaluate_exit_signals(
    market_state: dict[str, Any],
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Evaluate exit signals for all open positions, keyed by symbol."""
    result: dict[str, dict[str, Any]] = {}
    indicators_by_pair = market_state.get("indicators", {})

    for position in market_state.get("positions") or []:
        symbol = position.get("symbol")
        if not symbol:
            continue
        signals = evaluate_position_exit(
            position,
            indicators_by_pair.get(symbol, {}),
            max_age_hours=max_age_hours,
            now=now,
        )
        if not signals:
            continue
        result[symbol] = {
            "signals": [s.to_dict() for s in signals],
            "force_exit": any(s.severity == HARD for s in signals),
        }

    return result


def first_forced_exit(exit_signals: dict[str, dict[str, Any]]) -> str | None:
    """Return the first symbol with a HARD force_exit signal, if any."""
    for symbol, info in exit_signals.items():
        if info.get("force_exit"):
            return symbol
    return None
