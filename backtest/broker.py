"""Simulated broker exposing the MT5 MCP tool interface over historical bars.

Implements the same `call_tool` names and response shapes as the real MT5
gateway, so `fetch_market_state` and `execute_decision` run against it
unmodified. M15 data is approximated by serving H1 bars (only the M15 close
is consumed by the pipeline); H4 is resampled from H1.

Fill model (deliberately conservative):
- Limit orders fill when the bar range crosses the limit price.
- SL and TP are checked on every bar; if both fall inside one bar's range the
  STOP is assumed to hit first (worst case).
- Fills and exits execute at the order's price (no favorable slippage).
"""

from __future__ import annotations

from typing import Any

from backtest.data import resample_h1_to_h4

STANDARD_CONTRACT_SIZE = 100_000
DEFAULT_SPREAD_PIPS = 1.0
PENDING_EXPIRY_BARS = 48  # H1 bars ≈ 48h, mirrors maintenance.max_pending_hours


class SimulatedBroker:
    def __init__(
        self,
        bars_by_symbol: dict[str, list[dict[str, Any]]],
        *,
        start_balance: float = 10_000.0,
        spread_pips: float = DEFAULT_SPREAD_PIPS,
    ) -> None:
        self.h1 = bars_by_symbol
        self.h4 = {sym: resample_h1_to_h4(bars) for sym, bars in bars_by_symbol.items()}
        self.balance = start_balance
        self.spread_pips = spread_pips
        self.index = 0  # current H1 bar index (same timeline for all symbols)
        self.positions: list[dict[str, Any]] = []
        self.pending_orders: list[dict[str, Any]] = []
        self.closed_trades: list[dict[str, Any]] = []
        self._ticket_seq = 1000

    # ------------------------------------------------------------------
    # Time control
    # ------------------------------------------------------------------

    def advance_to(self, index: int) -> None:
        """Move the clock forward, processing fills/SL/TP on every bar passed."""
        while self.index < index:
            self.index += 1
            self._process_bar()

    def current_bar(self, symbol: str) -> dict[str, Any]:
        return self.h1[symbol][self.index]

    def current_time(self, symbol: str | None = None) -> int:
        sym = symbol or next(iter(self.h1))
        return int(self.h1[sym][self.index]["time"])

    # ------------------------------------------------------------------
    # Simulation core
    # ------------------------------------------------------------------

    def _pip(self, symbol: str) -> float:
        return 0.01 if symbol.endswith("JPY") else 0.0001

    def _spread(self, symbol: str) -> float:
        return self.spread_pips * self._pip(symbol)

    def _next_ticket(self) -> int:
        self._ticket_seq += 1
        return self._ticket_seq

    def _process_bar(self) -> None:
        for symbol in self.h1:
            if self.index >= len(self.h1[symbol]):
                continue
            bar = self.h1[symbol][self.index]
            self._fill_pending(symbol, bar)
            self._check_exits(symbol, bar)
        self._expire_pending()

    def _fill_pending(self, symbol: str, bar: dict[str, Any]) -> None:
        remaining = []
        for order in self.pending_orders:
            if order["symbol"] != symbol:
                remaining.append(order)
                continue
            price = order["price_open"]
            kind = order["type"]
            filled = False
            if kind == "BUY_LIMIT" and bar["low"] <= price:
                filled = True
            elif kind == "SELL_LIMIT" and bar["high"] >= price:
                filled = True
            elif kind == "BUY_STOP" and bar["high"] >= price:
                filled = True
            elif kind == "SELL_STOP" and bar["low"] <= price:
                filled = True

            if filled:
                side = 0 if kind.startswith("BUY") else 1
                self.positions.append({
                    "ticket": order["ticket"],
                    "symbol": symbol,
                    "type": side,
                    "volume": order["volume_current"],
                    "price_open": price,
                    "sl": order.get("sl", 0),
                    "tp": order.get("tp", 0),
                    "time": int(bar["time"]),
                    "comment": order.get("comment", ""),
                })
            else:
                remaining.append(order)
        self.pending_orders = remaining

    def _check_exits(self, symbol: str, bar: dict[str, Any]) -> None:
        remaining = []
        for pos in self.positions:
            if pos["symbol"] != symbol:
                remaining.append(pos)
                continue
            sl = pos.get("sl") or 0
            tp = pos.get("tp") or 0
            long = pos["type"] == 0
            exit_price = None
            exit_reason = None

            sl_hit = sl > 0 and (bar["low"] <= sl if long else bar["high"] >= sl)
            tp_hit = tp > 0 and (bar["high"] >= tp if long else bar["low"] <= tp)
            if sl_hit:  # conservative: stop first when both hit in one bar
                exit_price, exit_reason = sl, "sl"
            elif tp_hit:
                exit_price, exit_reason = tp, "tp"

            if exit_price is None:
                remaining.append(pos)
            else:
                self._close(pos, exit_price, int(bar["time"]), exit_reason)
        self.positions = remaining

    def _expire_pending(self) -> None:
        now = self.current_time()
        cutoff = now - PENDING_EXPIRY_BARS * 3600
        self.pending_orders = [
            o for o in self.pending_orders if o.get("time_setup", now) > cutoff
        ]

    def _close(
        self,
        pos: dict[str, Any],
        exit_price: float,
        exit_time: int,
        reason: str,
        volume: float | None = None,
    ) -> dict[str, Any]:
        vol = volume if volume is not None else pos["volume"]
        side = 1 if pos["type"] == 0 else -1
        distance = (exit_price - pos["price_open"]) * side
        profit_quote = distance * vol * STANDARD_CONTRACT_SIZE
        profit = profit_quote / exit_price if pos["symbol"].endswith("JPY") else profit_quote
        self.balance += profit

        sl_distance = abs(pos["price_open"] - pos["sl"]) if pos.get("sl") else None
        r_multiple = (distance / sl_distance) if sl_distance else None

        trade = {
            "ticket": pos["ticket"],
            "symbol": pos["symbol"],
            "direction": "LONG" if pos["type"] == 0 else "SHORT",
            "volume": vol,
            "price_open": pos["price_open"],
            "price_close": exit_price,
            "open_time": pos.get("time"),
            "close_time": exit_time,
            "profit": round(profit, 2),
            "r_multiple": round(r_multiple, 3) if r_multiple is not None else None,
            "exit_reason": reason,
            "comment": pos.get("comment", ""),
        }
        self.closed_trades.append(trade)
        return trade

    def close_all_at_market(self) -> None:
        """End-of-backtest cleanup: close everything at the last close price."""
        for pos in list(self.positions):
            bar = self.h1[pos["symbol"]][min(self.index, len(self.h1[pos["symbol"]]) - 1)]
            self._close(pos, bar["close"], int(bar["time"]), "end_of_test")
        self.positions = []
        self.pending_orders = []

    # ------------------------------------------------------------------
    # Equity
    # ------------------------------------------------------------------

    def equity(self) -> float:
        floating = 0.0
        for pos in self.positions:
            bar = self.h1[pos["symbol"]][self.index]
            side = 1 if pos["type"] == 0 else -1
            distance = (bar["close"] - pos["price_open"]) * side
            profit_quote = distance * pos["volume"] * STANDARD_CONTRACT_SIZE
            floating += (
                profit_quote / bar["close"] if pos["symbol"].endswith("JPY") else profit_quote
            )
        return self.balance + floating

    # ------------------------------------------------------------------
    # MT5 MCP tool interface
    # ------------------------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        args = arguments or {}
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            return {"success": False, "error": f"SimulatedBroker: unsupported tool {tool_name}"}
        return handler(args)

    def _tool_get_account_info(self, args: dict[str, Any]) -> dict[str, Any]:
        equity = self.equity()
        return {
            "success": True,
            "summary": {
                "balance": round(self.balance, 2),
                "equity": round(equity, 2),
                "margin": 0.0,
                "free_margin": round(equity, 2),
            },
        }

    def _tool_get_rates(self, args: dict[str, Any]) -> dict[str, Any]:
        symbol = str(args.get("symbol", "")).upper()
        timeframe = str(args.get("timeframe", "H1")).upper()
        count = int(args.get("count", 100))
        if symbol not in self.h1:
            return {"success": False, "error": f"No data for {symbol}"}

        if timeframe == "H4":
            series = self.h4[symbol]
            end = (self.index + 1) // 4
        else:  # H1 and M15 both serve H1 bars (M15 only contributes a close price)
            series = self.h1[symbol]
            end = self.index + 1

        window = series[max(0, end - count):end]
        return {"success": True, "count": len(window), "bars": window}

    def _tool_get_tick(self, args: dict[str, Any]) -> dict[str, Any]:
        symbol = str(args.get("symbol", "")).upper()
        if symbol not in self.h1:
            return {"success": False, "error": f"No data for {symbol}"}
        bar = self.current_bar(symbol)
        half_spread = self._spread(symbol) / 2
        mid = bar["close"]
        return {
            "success": True,
            "tick": {
                "bid": round(mid - half_spread, 5),
                "ask": round(mid + half_spread, 5),
                "time": int(bar["time"]),
            },
        }

    def _tool_get_symbol_info(self, args: dict[str, Any]) -> dict[str, Any]:
        symbol = str(args.get("symbol", "")).upper()
        point = 0.001 if symbol.endswith("JPY") else 0.00001
        return {"success": True, "info": {"point": point, "digits": 3 if symbol.endswith("JPY") else 5}}

    def _tool_get_open_positions(self, args: dict[str, Any]) -> dict[str, Any]:
        symbol = args.get("symbol")
        items = [p for p in self.positions if not symbol or p["symbol"] == symbol]
        return {"success": True, "count": len(items), "positions": items}

    def _tool_get_position_by_symbol(self, args: dict[str, Any]) -> dict[str, Any]:
        symbol = args.get("symbol")
        items = [p for p in self.positions if p.get("symbol") == symbol]
        return {"success": True, "symbol": symbol, "position": items[0] if items else None}

    def _tool_get_pending_orders(self, args: dict[str, Any]) -> dict[str, Any]:
        symbol = args.get("symbol")
        items = [o for o in self.pending_orders if not symbol or o["symbol"] == symbol]
        return {"success": True, "count": len(items), "orders": items}

    def _tool_place_order(self, args: dict[str, Any]) -> dict[str, Any]:
        symbol = str(args.get("symbol", "")).upper()
        order_type = str(args.get("order_type", "")).upper()
        if symbol not in self.h1:
            return {"success": False, "error": f"No data for {symbol}"}
        ticket = self._next_ticket()
        bar = self.current_bar(symbol)

        if order_type in {"BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"}:
            self.pending_orders.append({
                "ticket": ticket,
                "symbol": symbol,
                "type": order_type,
                "volume_current": float(args["lot_size"]),
                "price_open": float(args["price"]),
                "sl": float(args.get("stop_loss") or 0),
                "tp": float(args.get("take_profit") or 0),
                "time_setup": int(bar["time"]),
                "comment": args.get("comment", ""),
            })
            return {"success": True, "result": {"retcode": 10009, "order": ticket}}

        if order_type in {"BUY", "SELL"}:
            half_spread = self._spread(symbol) / 2
            fill = bar["close"] + half_spread if order_type == "BUY" else bar["close"] - half_spread
            window = args.get("entry_window")
            if window and len(window) == 2:
                low, high = sorted(window)
                if not (low <= fill <= high):
                    return {
                        "success": False,
                        "error": "Market price outside entry_window",
                        "current_price": fill,
                    }
            self.positions.append({
                "ticket": ticket,
                "symbol": symbol,
                "type": 0 if order_type == "BUY" else 1,
                "volume": float(args["lot_size"]),
                "price_open": round(fill, 5),
                "sl": float(args.get("stop_loss") or 0),
                "tp": float(args.get("take_profit") or 0),
                "time": int(bar["time"]),
                "comment": args.get("comment", ""),
            })
            return {"success": True, "result": {"retcode": 10009, "deal": ticket}}

        return {"success": False, "error": f"Unsupported order_type {order_type}"}

    def _tool_modify_position(self, args: dict[str, Any]) -> dict[str, Any]:
        ticket = args.get("ticket")
        for pos in self.positions:
            if pos["ticket"] == ticket:
                if args.get("stop_loss") is not None:
                    pos["sl"] = float(args["stop_loss"])
                if args.get("take_profit") is not None:
                    pos["tp"] = float(args["take_profit"])
                return {"success": True, "result": {"retcode": 10009, "order": ticket}}
        return {"success": False, "error": f"No position for ticket {ticket}"}

    def _tool_close_position(self, args: dict[str, Any]) -> dict[str, Any]:
        ticket = args.get("ticket")
        partial = args.get("lot_size")
        for pos in list(self.positions):
            if pos["ticket"] != ticket:
                continue
            bar = self.current_bar(pos["symbol"])
            half_spread = self._spread(pos["symbol"]) / 2
            exit_price = bar["close"] - half_spread if pos["type"] == 0 else bar["close"] + half_spread
            if partial is not None and float(partial) < pos["volume"]:
                self._close(pos, round(exit_price, 5), int(bar["time"]), "partial", float(partial))
                pos["volume"] = round(pos["volume"] - float(partial), 2)
            else:
                self._close(pos, round(exit_price, 5), int(bar["time"]), "manual")
                self.positions.remove(pos)
            return {"success": True, "result": {"retcode": 10009, "order": ticket}}
        return {"success": False, "error": f"No position for ticket {ticket}"}

    def _tool_cancel_order(self, args: dict[str, Any]) -> dict[str, Any]:
        ticket = args.get("ticket")
        before = len(self.pending_orders)
        self.pending_orders = [o for o in self.pending_orders if o["ticket"] != ticket]
        ok = len(self.pending_orders) < before
        if ok:
            return {"success": True, "result": {"retcode": 10009, "order": ticket}}
        return {"success": False, "error": f"No pending order for ticket {ticket}"}
