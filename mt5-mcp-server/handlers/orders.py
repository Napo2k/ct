"""Pending order and trade execution handlers."""

from __future__ import annotations

from typing import Any

import MetaTrader5 as mt5

from mt5client import (
    is_pending_order_type,
    named_tuple_list_to_dicts,
    named_tuple_to_dict,
    resolve_filling_mode,
    run_mt5,
    validate_lot_size,
    validate_order_type,
    validate_price,
    validate_symbol,
    validate_ticket,
    validate_time_type,
)


def get_pending_orders(symbol: str | None = None) -> dict[str, Any]:
    """List all pending orders, optionally filtered by symbol."""
    if symbol:
        normalized = validate_symbol(symbol)
        orders = run_mt5(mt5.orders_get, symbol=normalized)
    else:
        orders = run_mt5(mt5.orders_get)

    items = named_tuple_list_to_dicts(orders)
    return {"success": True, "count": len(items), "orders": items}


def place_order(
    symbol: str,
    order_type: str,
    lot_size: float,
    price: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    entry_window: list[float] | None = None,
    deviation: int = 20,
    filling: str | None = None,
    time_type: str | None = None,
    comment: str = "ClaudeTrader",
) -> dict[str, Any]:
    """
    Place a market or pending order.

    For market orders (BUY/SELL), validates the current tick against entry_window
    when provided and rejects if price moved outside the window.
    """
    normalized = validate_symbol(symbol)
    type_name, mt5_order_type = validate_order_type(order_type)
    volume = validate_lot_size(lot_size)
    run_mt5(mt5.symbol_select, normalized, True)

    tick = run_mt5(mt5.symbol_info_tick, normalized)
    if tick is None:
        return {"success": False, "error": f"Tick unavailable for {normalized}"}

    if is_pending_order_type(type_name):
        if price is None:
            return {"success": False, "error": "price is required for pending orders"}
        order_price = validate_price(price, field="price")
        action = mt5.TRADE_ACTION_PENDING
    else:
        action = mt5.TRADE_ACTION_DEAL
        if type_name == "BUY":
            order_price = tick.ask
        else:
            order_price = tick.bid

        if entry_window is not None:
            if len(entry_window) != 2:
                return {"success": False, "error": "entry_window must contain [min, max]"}
            low, high = sorted(entry_window)
            if order_price < low or order_price > high:
                return {
                    "success": False,
                    "error": "Market price outside entry_window",
                    "current_price": order_price,
                    "entry_window": [low, high],
                }

    sl = validate_price(stop_loss, field="stop_loss") if stop_loss is not None else 0.0
    tp = validate_price(take_profit, field="take_profit") if take_profit is not None else 0.0

    request: dict[str, Any] = {
        "action": action,
        "symbol": normalized,
        "volume": volume,
        "type": mt5_order_type,
        "price": order_price,
        "sl": sl,
        "tp": tp,
        "deviation": deviation,
        "magic": 260605,
        "comment": comment[:31],
        "type_time": validate_time_type(time_type),
        "type_filling": resolve_filling_mode(normalized, filling),
    }

    result = run_mt5(mt5.order_send, request)
    if result is None:
        return {"success": False, "error": "order_send returned None"}

    data = named_tuple_to_dict(result)
    success = data.get("retcode") == mt5.TRADE_RETCODE_DONE
    return {"success": success, "result": data, "request": request}


def modify_order(
    ticket: int,
    price: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    lot_size: float | None = None,
) -> dict[str, Any]:
    """Modify price, volume, SL, or TP on a pending order."""
    validated_ticket = validate_ticket(ticket)
    orders = run_mt5(mt5.orders_get, ticket=validated_ticket)
    items = named_tuple_list_to_dicts(orders)
    if not items:
        return {"success": False, "error": f"No pending order for ticket {validated_ticket}"}

    order = items[0]
    request: dict[str, Any] = {
        "action": mt5.TRADE_ACTION_MODIFY,
        "order": validated_ticket,
        "symbol": order["symbol"],
        "price": validate_price(price, field="price") if price is not None else order["price_open"],
        "sl": validate_price(stop_loss, field="stop_loss") if stop_loss is not None else order["sl"],
        "tp": validate_price(take_profit, field="take_profit") if take_profit is not None else order["tp"],
        "type_time": order["type_time"],
        "type_filling": resolve_filling_mode(order["symbol"]),
    }

    if lot_size is not None:
        request["volume"] = validate_lot_size(lot_size)
    else:
        request["volume"] = order["volume_current"]

    result = run_mt5(mt5.order_send, request)
    if result is None:
        return {"success": False, "error": "order_send returned None"}

    data = named_tuple_to_dict(result)
    success = data.get("retcode") == mt5.TRADE_RETCODE_DONE
    return {"success": success, "result": data, "request": request}


def cancel_order(ticket: int) -> dict[str, Any]:
    """Cancel a pending order by ticket."""
    validated_ticket = validate_ticket(ticket)
    orders = run_mt5(mt5.orders_get, ticket=validated_ticket)
    items = named_tuple_list_to_dicts(orders)
    if not items:
        return {"success": False, "error": f"No pending order for ticket {validated_ticket}"}

    order = items[0]
    request = {
        "action": mt5.TRADE_ACTION_REMOVE,
        "order": validated_ticket,
        "symbol": order["symbol"],
    }

    result = run_mt5(mt5.order_send, request)
    if result is None:
        return {"success": False, "error": "order_send returned None"}

    data = named_tuple_to_dict(result)
    success = data.get("retcode") == mt5.TRADE_RETCODE_DONE
    return {"success": success, "result": data, "request": request}
