"""Open position handlers."""

from __future__ import annotations

from typing import Any

import MetaTrader5 as mt5

from mt5client import (
    named_tuple_list_to_dicts,
    named_tuple_to_dict,
    resolve_filling_mode,
    run_mt5,
    validate_lot_size,
    validate_price,
    validate_symbol,
    validate_ticket,
)


def get_open_positions(symbol: str | None = None) -> dict[str, Any]:
    """List all open positions, optionally filtered by symbol."""
    if symbol:
        normalized = validate_symbol(symbol)
        positions = run_mt5(mt5.positions_get, symbol=normalized)
    else:
        positions = run_mt5(mt5.positions_get)

    items = named_tuple_list_to_dicts(positions)
    return {"success": True, "count": len(items), "positions": items}


def get_position_by_symbol(symbol: str) -> dict[str, Any]:
    """Return the open position for a symbol, if any."""
    normalized = validate_symbol(symbol)
    positions = run_mt5(mt5.positions_get, symbol=normalized)
    items = named_tuple_list_to_dicts(positions)

    if not items:
        return {"success": True, "symbol": normalized, "position": None}

    if len(items) > 1:
        return {
            "success": True,
            "symbol": normalized,
            "position": items[0],
            "warning": f"Multiple positions ({len(items)}) found; returning first",
            "all_positions": items,
        }

    return {"success": True, "symbol": normalized, "position": items[0]}


def close_position(ticket: int, lot_size: float | None = None) -> dict[str, Any]:
    """Close an open position fully or partially at market."""
    validated_ticket = validate_ticket(ticket)
    positions = run_mt5(mt5.positions_get, ticket=validated_ticket)
    items = named_tuple_list_to_dicts(positions)
    if not items:
        return {"success": False, "error": f"No open position for ticket {validated_ticket}"}

    position = items[0]
    symbol = position["symbol"]
    volume = validate_lot_size(lot_size) if lot_size is not None else float(position["volume"])

    tick = run_mt5(mt5.symbol_info_tick, symbol)
    if tick is None:
        return {"success": False, "error": f"Tick unavailable for {symbol}"}

    if position["type"] == mt5.POSITION_TYPE_BUY:
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    else:
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "position": validated_ticket,
        "price": price,
        "deviation": 20,
        "magic": position.get("magic", 0),
        "comment": "ClaudeTrader close_position",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": resolve_filling_mode(symbol),
    }

    result = run_mt5(mt5.order_send, request)
    if result is None:
        return {"success": False, "error": "order_send returned None"}

    data = named_tuple_to_dict(result)
    success = data.get("retcode") == mt5.TRADE_RETCODE_DONE
    return {"success": success, "result": data, "request": request}


def modify_position(
    ticket: int,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> dict[str, Any]:
    """Modify stop loss and/or take profit on an open position."""
    validated_ticket = validate_ticket(ticket)
    positions = run_mt5(mt5.positions_get, ticket=validated_ticket)
    items = named_tuple_list_to_dicts(positions)
    if not items:
        return {"success": False, "error": f"No open position for ticket {validated_ticket}"}

    position = items[0]
    sl = validate_price(stop_loss, field="stop_loss") if stop_loss is not None else position["sl"]
    tp = validate_price(take_profit, field="take_profit") if take_profit is not None else position["tp"]

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": position["symbol"],
        "position": validated_ticket,
        "sl": sl,
        "tp": tp,
    }

    result = run_mt5(mt5.order_send, request)
    if result is None:
        return {"success": False, "error": "order_send returned None"}

    data = named_tuple_to_dict(result)
    success = data.get("retcode") == mt5.TRADE_RETCODE_DONE
    return {"success": success, "result": data, "request": request}
