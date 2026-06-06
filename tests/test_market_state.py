"""Market state assembly tests — MT5 indicator fallback."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.market_state import fetch_market_state
from tests.test_indicators import _trending_bars


class FakeMT5Client:
    def __init__(self, bars_by_tf: dict[str, list[dict]]) -> None:
        self.bars_by_tf = bars_by_tf

    async def call_tool(self, tool_name: str, arguments: dict | None = None) -> dict:
        args = arguments or {}
        if tool_name == "get_account_info":
            return {
                "success": True,
                "summary": {"balance": 10000.0, "equity": 10000.0, "currency": "USD"},
            }
        if tool_name == "get_open_positions":
            return {"success": True, "positions": []}
        if tool_name == "get_pending_orders":
            return {"success": True, "orders": []}
        if tool_name == "get_tick":
            return {
                "success": True,
                "symbol": args["symbol"],
                "tick": {"bid": 1.08410, "ask": 1.08424},
                "spread": 0.00014,
                "point": 0.00001,
            }
        if tool_name == "get_symbol_info":
            return {"success": True, "info": {"point": 0.00001}}
        if tool_name == "get_rates":
            tf = args["timeframe"]
            bars = self.bars_by_tf.get(tf, [])
            return {"success": True, "symbol": args["symbol"], "timeframe": tf, "bars": bars}
        return {"success": False, "error": f"unsupported tool {tool_name}"}


def test_fetch_market_state_mt5_indicator_fallback():
    bars = {
        "H4": _trending_bars(250, start=1.07, step=0.001),
        "H1": _trending_bars(120, start=1.08, step=0.0005),
        "M15": _trending_bars(60, start=1.081, step=0.0002),
    }
    client = FakeMT5Client(bars)

    async def run() -> dict:
        return await fetch_market_state(["EURUSD"], client, None)

    state = asyncio.run(run())
    h4 = state["indicators"]["EURUSD"]["H4"]
    h1 = state["indicators"]["EURUSD"]["H1"]

    assert h4["data_source"] == "mt5_fallback"
    assert h4["adx"] is not None
    assert h4["ema50"] is not None
    assert h4["ema200"] is not None
    assert state["indicators"]["EURUSD"]["regime"]["regime"] in {
        "TRENDING_BULLISH",
        "TRANSITIONAL",
        "OVEREXTENDED",
    }
    assert h1["rsi"] is not None
    assert h1["atr"] is not None
    assert "H1_prev" in state["indicators"]["EURUSD"]
