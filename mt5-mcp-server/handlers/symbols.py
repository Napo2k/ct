"""Symbol and market data handlers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import MetaTrader5 as mt5

from constants import DEFAULT_SYMBOLS
from mt5client import (
    named_tuple_list_to_dicts,
    named_tuple_to_dict,
    run_mt5,
    validate_symbol,
    validate_timeframe,
)


def get_symbols(group: str | None = None) -> dict[str, Any]:
    """List tradable symbols, optionally filtered by MT5 group pattern."""
    if group:
        symbols = run_mt5(mt5.symbols_get, group)
    else:
        symbols = run_mt5(mt5.symbols_get)

    items = named_tuple_list_to_dicts(symbols)
    if not items:
        for symbol in DEFAULT_SYMBOLS:
            run_mt5(mt5.symbol_select, symbol, True)
        symbols = run_mt5(mt5.symbols_get)
        items = named_tuple_list_to_dicts(symbols)

    return {
        "success": True,
        "count": len(items),
        "symbols": [
            {
                "name": item["name"],
                "description": item.get("description"),
                "path": item.get("path"),
                "visible": item.get("visible"),
                "trade_mode": item.get("trade_mode"),
            }
            for item in items
        ],
    }


def get_symbol_info(symbol: str) -> dict[str, Any]:
    """Return broker-native symbol specifications for a pair."""
    normalized = validate_symbol(symbol)
    run_mt5(mt5.symbol_select, normalized, True)
    info = run_mt5(mt5.symbol_info, normalized)
    if info is None:
        return {"success": False, "error": f"Symbol not found: {normalized}"}

    data = named_tuple_to_dict(info)
    return {"success": True, "symbol": normalized, "info": data}


def get_tick(symbol: str) -> dict[str, Any]:
    """Return live bid/ask/spread from the broker feed."""
    normalized = validate_symbol(symbol)
    run_mt5(mt5.symbol_select, normalized, True)
    tick = run_mt5(mt5.symbol_info_tick, normalized)
    if tick is None:
        return {"success": False, "error": f"Tick unavailable for {normalized}"}

    data = named_tuple_to_dict(tick)
    spread = data["ask"] - data["bid"]
    info = run_mt5(mt5.symbol_info, normalized)
    point = getattr(info, "point", None) if info is not None else None
    spread_points = spread / point if point else None

    return {
        "success": True,
        "symbol": normalized,
        "tick": data,
        "spread": spread,
        "spread_points": spread_points,
        "time_utc": datetime.fromtimestamp(data["time"], tz=timezone.utc).isoformat(),
    }


def get_rates(
    symbol: str,
    timeframe: str,
    count: int = 100,
    start_pos: int = 0,
) -> dict[str, Any]:
    """Return OHLCV bars as fallback when Massive MCP is unavailable."""
    normalized = validate_symbol(symbol)
    tf = validate_timeframe(timeframe)

    if count < 1 or count > 5000:
        return {"success": False, "error": "count must be between 1 and 5000"}
    if start_pos < 0:
        return {"success": False, "error": "start_pos must be >= 0"}

    run_mt5(mt5.symbol_select, normalized, True)
    rates = run_mt5(mt5.copy_rates_from_pos, normalized, tf, start_pos, count)
    if rates is None:
        return {"success": False, "error": f"Rates unavailable for {normalized} {timeframe}"}

    bars = [
        {
            "time": int(bar["time"]),
            "time_utc": datetime.fromtimestamp(int(bar["time"]), tz=timezone.utc).isoformat(),
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "tick_volume": int(bar["tick_volume"]),
            "spread": int(bar["spread"]),
            "real_volume": int(bar["real_volume"]),
        }
        for bar in rates
    ]

    return {
        "success": True,
        "symbol": normalized,
        "timeframe": timeframe.upper(),
        "count": len(bars),
        "bars": bars,
    }
