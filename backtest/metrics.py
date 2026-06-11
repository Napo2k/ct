"""Backtest performance metrics."""

from __future__ import annotations

from typing import Any


def compute_metrics(
    trades: list[dict[str, Any]],
    equity_curve: list[dict[str, Any]],
    start_balance: float,
) -> dict[str, Any]:
    wins = [t for t in trades if t["profit"] > 0]
    losses = [t for t in trades if t["profit"] < 0]
    gross_win = sum(t["profit"] for t in wins)
    gross_loss = abs(sum(t["profit"] for t in losses))

    r_values = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]

    final_equity = equity_curve[-1]["equity"] if equity_curve else start_balance
    max_dd = _max_drawdown_pct(equity_curve)

    durations = [
        (t["close_time"] - t["open_time"]) / 3600
        for t in trades
        if t.get("open_time") and t.get("close_time")
    ]

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades), 3) if trades else None,
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "net_profit": round(gross_win - gross_loss, 2),
        "expectancy_r": round(sum(r_values) / len(r_values), 3) if r_values else None,
        "total_return_pct": round((final_equity - start_balance) / start_balance * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "avg_hold_hours": round(sum(durations) / len(durations), 1) if durations else None,
        "exit_reasons": _count_by(trades, "exit_reason"),
        "final_equity": round(final_equity, 2),
    }


def _max_drawdown_pct(equity_curve: list[dict[str, Any]]) -> float:
    peak = float("-inf")
    max_dd = 0.0
    for point in equity_curve:
        equity = point["equity"]
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100)
    return max_dd


def _count_by(trades: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trade in trades:
        value = str(trade.get(key, "unknown"))
        counts[value] = counts.get(value, 0) + 1
    return counts
