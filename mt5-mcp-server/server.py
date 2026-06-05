"""ClaudeTrader MT5 MCP server — FastMCP stdio transport."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from handlers import account, history, orders, positions, symbols
from mt5client import MT5Error, ensure_initialized, is_suspended

mcp = FastMCP(
    name="ClaudeTrader MT5",
    instructions=(
        "MetaTrader 5 execution gateway for ClaudeTrader (demo/paper only). "
        "Read tools provide live broker data; write tools place/modify/cancel orders. "
        "On timeout the server enters SUSPEND state — do not retry writes until cleared."
    ),
)


def _tool_response(payload: dict[str, Any]) -> dict[str, Any]:
    if is_suspended():
        payload["suspended"] = True
        payload["action"] = "SUSPEND"
    return payload


def _handle_tool(handler, *args, **kwargs) -> dict[str, Any]:
    try:
        ensure_initialized()
        return _tool_response(handler(*args, **kwargs))
    except MT5Error as exc:
        response: dict[str, Any] = {
            "success": False,
            "error": str(exc),
        }
        if exc.suspend or is_suspended():
            response["suspended"] = True
            response["action"] = "SUSPEND"
        return response
    except Exception as exc:  # noqa: BLE001 — surface unexpected failures to MCP client
        return {"success": False, "error": f"Unexpected error: {exc}"}


@mcp.tool
def get_account_info() -> dict[str, Any]:
    """Return live account balance, equity, margin, and trading permissions."""
    return _handle_tool(account.get_account_info)


@mcp.tool
def get_rates(
    symbol: str,
    timeframe: str,
    count: int = 100,
    start_pos: int = 0,
) -> dict[str, Any]:
    """Return OHLCV bars for a symbol and timeframe (fallback data feed)."""
    return _handle_tool(symbols.get_rates, symbol, timeframe, count, start_pos)


@mcp.tool
def get_tick(symbol: str) -> dict[str, Any]:
    """Return live bid/ask/spread from the broker-native feed."""
    return _handle_tool(symbols.get_tick, symbol)


@mcp.tool
def get_symbol_info(symbol: str) -> dict[str, Any]:
    """Return broker-native symbol specifications (digits, lot limits, spread)."""
    return _handle_tool(symbols.get_symbol_info, symbol)


@mcp.tool
def get_symbols(group: str | None = None) -> dict[str, Any]:
    """List tradable symbols, optionally filtered by MT5 group pattern."""
    return _handle_tool(symbols.get_symbols, group)


@mcp.tool
def get_open_positions(symbol: str | None = None) -> dict[str, Any]:
    """List all open positions, optionally filtered by symbol."""
    return _handle_tool(positions.get_open_positions, symbol)


@mcp.tool
def get_position_by_symbol(symbol: str) -> dict[str, Any]:
    """Return the open position for a symbol, if any."""
    return _handle_tool(positions.get_position_by_symbol, symbol)


@mcp.tool
def close_position(ticket: int, lot_size: float | None = None) -> dict[str, Any]:
    """Close an open position fully or partially at market."""
    return _handle_tool(positions.close_position, ticket, lot_size)


@mcp.tool
def get_pending_orders(symbol: str | None = None) -> dict[str, Any]:
    """List all pending orders, optionally filtered by symbol."""
    return _handle_tool(orders.get_pending_orders, symbol)


@mcp.tool
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
    """Place a market or pending order with optional entry_window guard for market entries."""
    return _handle_tool(
        orders.place_order,
        symbol,
        order_type,
        lot_size,
        price,
        stop_loss,
        take_profit,
        entry_window,
        deviation,
        filling,
        time_type,
        comment,
    )


@mcp.tool
def modify_order(
    ticket: int,
    price: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    lot_size: float | None = None,
) -> dict[str, Any]:
    """Modify price, volume, SL, or TP on a pending order."""
    return _handle_tool(orders.modify_order, ticket, price, stop_loss, take_profit, lot_size)


@mcp.tool
def cancel_order(ticket: int) -> dict[str, Any]:
    """Cancel a pending order by ticket."""
    return _handle_tool(orders.cancel_order, ticket)


@mcp.tool
def modify_position(
    ticket: int,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> dict[str, Any]:
    """Modify stop loss and/or take profit on an open position."""
    return _handle_tool(positions.modify_position, ticket, stop_loss, take_profit)


@mcp.tool
def get_history(
    from_date: str,
    to_date: str,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Return closed deals and orders between two ISO-8601 UTC timestamps."""
    return _handle_tool(history.get_history, from_date, to_date, symbol)


if __name__ == "__main__":
    mcp.run()
