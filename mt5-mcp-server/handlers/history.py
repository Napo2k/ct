"""Trade history handlers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import MetaTrader5 as mt5

from mt5client import named_tuple_list_to_dicts, run_mt5, validate_symbol


def _parse_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Invalid datetime: {value!r}") from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_history(
    from_date: str,
    to_date: str,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Return closed deals and orders between two ISO-8601 UTC timestamps."""
    start = _parse_datetime(from_date)
    end = _parse_datetime(to_date)
    if end < start:
        return {"success": False, "error": "to_date must be >= from_date"}

    if symbol:
        normalized = validate_symbol(symbol)
        deals = run_mt5(mt5.history_deals_get, start, end, group=f"*{normalized}*")
        orders = run_mt5(mt5.history_orders_get, start, end, group=f"*{normalized}*")
    else:
        deals = run_mt5(mt5.history_deals_get, start, end)
        orders = run_mt5(mt5.history_orders_get, start, end)

    deal_items = named_tuple_list_to_dicts(deals)
    order_items = named_tuple_list_to_dicts(orders)

    return {
        "success": True,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "symbol": symbol.upper() if symbol else None,
        "deals_count": len(deal_items),
        "orders_count": len(order_items),
        "deals": deal_items,
        "orders": order_items,
    }
