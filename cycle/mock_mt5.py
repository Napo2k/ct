"""In-memory MT5 stand-in for Phase 1 mock execution testing."""

from __future__ import annotations

import copy
from typing import Any


class MockMT5Client:
    """Records tool calls and simulates order/position state without a live broker."""

    def __init__(self, market_state: dict[str, Any]) -> None:
        self.market_state = market_state
        self.positions: list[dict[str, Any]] = copy.deepcopy(market_state.get("positions") or [])
        self.pending_orders: list[dict[str, Any]] = copy.deepcopy(
            market_state.get("pending_orders") or []
        )
        self.call_log: list[dict[str, Any]] = []
        self._ticket_seq = 20000

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        args = arguments or {}
        self.call_log.append({"tool": tool_name, "arguments": copy.deepcopy(args)})

        if tool_name == "get_open_positions":
            symbol = args.get("symbol")
            items = self.positions if not symbol else [p for p in self.positions if p["symbol"] == symbol]
            return {"success": True, "count": len(items), "positions": items}

        if tool_name == "get_position_by_symbol":
            symbol = args.get("symbol")
            items = [p for p in self.positions if p.get("symbol") == symbol]
            return {"success": True, "symbol": symbol, "position": items[0] if items else None}

        if tool_name == "get_tick":
            symbol = args.get("symbol")
            tick_data = self.market_state.get("ticks", {}).get(symbol, {})
            if not tick_data:
                return {"success": False, "error": f"No tick for {symbol}"}
            return tick_data

        if tool_name == "get_pending_orders":
            symbol = args.get("symbol")
            items = self.pending_orders if not symbol else [o for o in self.pending_orders if o["symbol"] == symbol]
            return {"success": True, "count": len(items), "orders": items}

        if tool_name == "place_order":
            return self._place_order(args)

        if tool_name == "close_position":
            return self._close_position(args)

        if tool_name == "modify_position":
            return self._modify_position(args)

        if tool_name == "cancel_order":
            return self._cancel_order(args)

        return {"success": False, "error": f"MockMT5: unsupported tool {tool_name}"}

    def _next_ticket(self) -> int:
        self._ticket_seq += 1
        return self._ticket_seq

    def _place_order(self, args: dict[str, Any]) -> dict[str, Any]:
        order_type = str(args.get("order_type", "")).upper()
        symbol = args["symbol"]
        ticket = self._next_ticket()

        if order_type in {"BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"}:
            order = {
                "ticket": ticket,
                "symbol": symbol,
                "volume_current": args["lot_size"],
                "price_open": args.get("price"),
                "sl": args.get("stop_loss", 0),
                "tp": args.get("take_profit", 0),
                "type": order_type,
            }
            self.pending_orders.append(order)
            return {
                "success": True,
                "result": {"retcode": 10009, "order": ticket, "comment": "mock pending"},
                "request": args,
            }

        position = {
            "ticket": ticket,
            "symbol": symbol,
            "volume": args["lot_size"],
            "price_open": args.get("price", 0),
            "sl": args.get("stop_loss", 0),
            "tp": args.get("take_profit", 0),
            "type": 0 if order_type == "BUY" else 1,
        }
        self.positions.append(position)
        return {
            "success": True,
            "result": {"retcode": 10009, "deal": ticket, "comment": "mock market fill"},
            "request": args,
        }

    def _close_position(self, args: dict[str, Any]) -> dict[str, Any]:
        ticket = args["ticket"]
        self.positions = [p for p in self.positions if p.get("ticket") != ticket]
        return {"success": True, "result": {"retcode": 10009, "order": ticket}}

    def _modify_position(self, args: dict[str, Any]) -> dict[str, Any]:
        ticket = args["ticket"]
        for pos in self.positions:
            if pos.get("ticket") == ticket:
                if "stop_loss" in args:
                    pos["sl"] = args["stop_loss"]
                if "take_profit" in args:
                    pos["tp"] = args["take_profit"]
                return {"success": True, "result": {"retcode": 10009, "order": ticket}}
        return {"success": False, "error": f"No position for ticket {ticket}"}

    def _cancel_order(self, args: dict[str, Any]) -> dict[str, Any]:
        ticket = args["ticket"]
        self.pending_orders = [o for o in self.pending_orders if o.get("ticket") != ticket]
        return {"success": True, "result": {"retcode": 10009, "order": ticket}}
