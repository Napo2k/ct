"""Automated broker housekeeping before each evaluation cycle."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


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


async def refresh_broker_snapshot(
    market_state: dict[str, Any],
    mt5: Any,
) -> None:
    """Refresh positions and pending orders in market_state from MT5."""
    positions = await mt5.call_tool("get_open_positions")
    if isinstance(positions, dict) and positions.get("success"):
        market_state["positions"] = positions.get("positions", [])

    orders = await mt5.call_tool("get_pending_orders")
    if isinstance(orders, dict) and orders.get("success"):
        market_state["pending_orders"] = orders.get("orders", [])


async def run_maintenance(
    mt5: Any,
    market_state: dict[str, Any],
    *,
    max_pending_hours: float = 48.0,
    max_position_hours_without_tp: float = 48.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Cancel stale pending orders and close aged positions without take-profit.

    Playbook: positions open > 48h without TP → close at market on next cycle.
    Pending orders older than max_pending_hours are cancelled to avoid stale exposure.
    """
    current = now or datetime.now(timezone.utc)
    now_ts = int(current.timestamp())
    pending_cutoff = now_ts - int(max_pending_hours * 3600)
    position_cutoff = now_ts - int(max_position_hours_without_tp * 3600)

    result: dict[str, Any] = {
        "cancelled_orders": [],
        "closed_positions": [],
        "skipped": [],
    }

    for order in list(market_state.get("pending_orders") or []):
        ticket = order.get("ticket")
        setup_ts = _parse_unix_time(order.get("time_setup") or order.get("time"))
        if not ticket or setup_ts is None:
            continue
        if setup_ts > pending_cutoff:
            continue

        cancel = await mt5.call_tool("cancel_order", {"ticket": ticket})
        entry = {
            "ticket": ticket,
            "symbol": order.get("symbol"),
            "age_hours": round((now_ts - setup_ts) / 3600, 1),
            "success": isinstance(cancel, dict) and cancel.get("success", False),
        }
        result["cancelled_orders"].append(entry)
        logger.info(
            "Maintenance cancelled stale pending order %s (%s, %.1fh)",
            ticket,
            order.get("symbol"),
            entry["age_hours"],
        )

    for position in list(market_state.get("positions") or []):
        ticket = position.get("ticket")
        open_ts = _parse_unix_time(position.get("time") or position.get("time_setup"))
        tp = position.get("tp") or position.get("take_profit")
        has_tp = tp is not None and float(tp) > 0
        if not ticket or open_ts is None or has_tp:
            if ticket and open_ts and has_tp:
                result["skipped"].append(
                    {"ticket": ticket, "reason": "has_take_profit"}
                )
            continue
        if open_ts > position_cutoff:
            continue

        close = await mt5.call_tool("close_position", {"ticket": ticket})
        entry = {
            "ticket": ticket,
            "symbol": position.get("symbol"),
            "age_hours": round((now_ts - open_ts) / 3600, 1),
            "success": isinstance(close, dict) and close.get("success", False),
        }
        result["closed_positions"].append(entry)
        logger.info(
            "Maintenance closed aged position %s (%s, %.1fh, no TP)",
            ticket,
            position.get("symbol"),
            entry["age_hours"],
        )

    if result["cancelled_orders"] or result["closed_positions"]:
        await refresh_broker_snapshot(market_state, mt5)

    result["ran"] = True
    return result
