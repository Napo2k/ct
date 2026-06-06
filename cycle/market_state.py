"""Assemble market_state snapshot from MT5 + Massive MCP feeds."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from cycle.indicators import compute_indicator_bundle
from cycle.mcp_client import MCPClient, MCPClientError
from cycle.regime import classify_regime

logger = logging.getLogger(__name__)

_FALLBACK_BAR_COUNTS = {"H4": 250, "H1": 120, "M15": 60}


async def fetch_market_state(
    pairs: list[str],
    mt5: MCPClient | None,
    massive: MCPClient | None,
) -> dict[str, Any]:
    """Build the full market_state object for a cycle."""
    state: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pairs": pairs,
        "account": None,
        "positions": [],
        "pending_orders": [],
        "ticks": {},
        "indicators": {},
        "news": [],
        "errors": [],
    }

    if mt5 is not None:
        await _fetch_mt5_data(state, pairs, mt5)
    else:
        state["errors"].append("MT5 MCP client not available")

    if massive is not None:
        await _fetch_massive_data(state, pairs, massive)
    else:
        state["errors"].append("Massive MCP client not available — using MT5 indicator fallback")

    if mt5 is not None:
        await _apply_mt5_indicator_fallback(state, pairs, mt5)

    for pair in pairs:
        indicators = state["indicators"].setdefault(pair, {})
        h4 = indicators.get("H4", {})
        if h4:
            indicators["regime"] = classify_regime(
                {
                    "adx": h4.get("adx"),
                    "ema50": h4.get("ema50"),
                    "ema200": h4.get("ema200"),
                    "price": h4.get("price") or state.get("ticks", {}).get(pair, {}).get("bid"),
                }
            )

    return state


async def _fetch_mt5_data(state: dict[str, Any], pairs: list[str], mt5: MCPClient) -> None:
    try:
        account = await mt5.call_tool("get_account_info")
        if isinstance(account, dict) and account.get("success"):
            state["account"] = account.get("summary") or account.get("account")
    except MCPClientError as exc:
        state["errors"].append(f"get_account_info: {exc}")

    try:
        positions = await mt5.call_tool("get_open_positions")
        if isinstance(positions, dict) and positions.get("success"):
            state["positions"] = positions.get("positions", [])
    except MCPClientError as exc:
        state["errors"].append(f"get_open_positions: {exc}")

    try:
        orders = await mt5.call_tool("get_pending_orders")
        if isinstance(orders, dict) and orders.get("success"):
            state["pending_orders"] = orders.get("orders", [])
    except MCPClientError as exc:
        state["errors"].append(f"get_pending_orders: {exc}")

    for pair in pairs:
        try:
            tick = await mt5.call_tool("get_tick", {"symbol": pair})
            if isinstance(tick, dict) and tick.get("success"):
                state["ticks"][pair] = tick
        except MCPClientError as exc:
            state["errors"].append(f"get_tick({pair}): {exc}")

        try:
            info = await mt5.call_tool("get_symbol_info", {"symbol": pair})
            if isinstance(info, dict) and info.get("success"):
                point = info.get("info", {}).get("point")
                if pair in state["ticks"] and point:
                    state["ticks"][pair]["point"] = point
        except MCPClientError as exc:
            state["errors"].append(f"get_symbol_info({pair}): {exc}")

        for tf in ("H4", "H1", "M15"):
            try:
                rates = await mt5.call_tool(
                    "get_rates",
                    {"symbol": pair, "timeframe": tf, "count": 3},
                )
                if isinstance(rates, dict) and rates.get("success") and rates.get("bars"):
                    bars = rates["bars"]
                    latest = bars[-1]
                    prev = bars[-2] if len(bars) > 1 else {}
                    bucket = state["indicators"].setdefault(pair, {}).setdefault(tf, {})
                    bucket["price"] = latest.get("close")
                    bucket["open"] = latest.get("open")
                    bucket["high"] = latest.get("high")
                    bucket["low"] = latest.get("low")
                    if tf == "H1":
                        state["indicators"][pair].setdefault("H1_prev", {})["price"] = prev.get("close")
            except MCPClientError as exc:
                state["errors"].append(f"get_rates({pair},{tf}): {exc}")


async def _apply_mt5_indicator_fallback(
    state: dict[str, Any],
    pairs: list[str],
    mt5: MCPClient,
) -> None:
    """Fill missing indicator fields from MT5 OHLCV when Massive MCP is unavailable."""
    for pair in pairs:
        for tf in ("H4", "H1", "M15"):
            bucket = state["indicators"].setdefault(pair, {}).setdefault(tf, {})
            if not _needs_indicator_fallback(bucket, tf):
                continue

            count = _FALLBACK_BAR_COUNTS[tf]
            try:
                rates = await mt5.call_tool(
                    "get_rates",
                    {"symbol": pair, "timeframe": tf, "count": count},
                )
            except MCPClientError as exc:
                state["errors"].append(f"indicator_fallback get_rates({pair},{tf}): {exc}")
                continue

            if not isinstance(rates, dict) or not rates.get("success") or not rates.get("bars"):
                continue

            bars = rates["bars"]
            computed = compute_indicator_bundle(bars)
            _merge_indicators(bucket, computed)

            if tf == "H1" and len(bars) >= 2:
                prev = compute_indicator_bundle(bars[:-1])
                if prev:
                    state["indicators"][pair]["H1_prev"] = {
                        k: prev[k]
                        for k in ("rsi", "macd", "macd_signal", "macd_histogram", "price")
                        if k in prev
                    }
            if tf == "H4" and len(bars) >= 2:
                prev = compute_indicator_bundle(bars[:-1])
                if prev and prev.get("adx") is not None:
                    state["indicators"][pair]["H4_prev"] = {"adx": prev["adx"]}


def _needs_indicator_fallback(bucket: dict[str, Any], timeframe: str) -> bool:
    if bucket.get("data_source") == "massive":
        return False
    if timeframe == "H4":
        return not all(bucket.get(key) is not None for key in ("adx", "ema50", "ema200", "price"))
    if timeframe == "H1":
        return not all(bucket.get(key) is not None for key in ("rsi", "macd_histogram", "atr"))
    return bucket.get("price") is None


def _merge_indicators(target: dict[str, Any], computed: dict[str, Any]) -> None:
    for key, value in computed.items():
        if key == "data_source":
            if target.get("data_source") != "massive":
                target["data_source"] = value
        elif target.get(key) is None:
            target[key] = value


async def _fetch_massive_data(state: dict[str, Any], pairs: list[str], massive: MCPClient) -> None:
    for pair in pairs:
        for tf in ("H4", "H1", "M15"):
            try:
                data = await massive.call_tool(
                    "get_indicators",
                    {"symbol": pair, "timeframe": tf},
                )
                if isinstance(data, dict):
                    bucket = state["indicators"].setdefault(pair, {}).setdefault(tf, {})
                    bucket.update(_normalize_indicators(data))
                    bucket["data_source"] = "massive"
            except MCPClientError:
                try:
                    data = await massive.call_tool(
                        f"get_{tf.lower()}_indicators",
                        {"symbol": pair},
                    )
                    if isinstance(data, dict):
                        bucket = state["indicators"].setdefault(pair, {}).setdefault(tf, {})
                        bucket.update(_normalize_indicators(data))
                        bucket["data_source"] = "massive"
                except MCPClientError as exc:
                    logger.debug("Massive indicators unavailable for %s %s: %s", pair, tf, exc)

        try:
            prev = await massive.call_tool(
                "get_indicators",
                {"symbol": pair, "timeframe": "H1", "offset": 1},
            )
            if isinstance(prev, dict):
                state["indicators"].setdefault(pair, {})["H1_prev"] = _normalize_indicators(prev)
                h4_prev = await massive.call_tool(
                    "get_indicators",
                    {"symbol": pair, "timeframe": "H4", "offset": 1},
                )
                if isinstance(h4_prev, dict):
                    state["indicators"][pair]["H4_prev"] = _normalize_indicators(h4_prev)
        except MCPClientError:
            pass

    try:
        news = await massive.call_tool("get_economic_calendar", {"hours_ahead": 2})
        if isinstance(news, dict):
            state["news"] = news.get("events", news.get("calendar", []))
        elif isinstance(news, list):
            state["news"] = news
    except MCPClientError as exc:
        state["errors"].append(f"get_economic_calendar: {exc}")


def _normalize_indicators(data: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "rsi": data.get("rsi") or data.get("RSI"),
        "macd": data.get("macd") or data.get("MACD"),
        "macd_signal": data.get("macd_signal") or data.get("signal"),
        "macd_histogram": data.get("macd_histogram") or data.get("histogram"),
        "atr": data.get("atr") or data.get("ATR"),
        "adx": data.get("adx") or data.get("ADX"),
        "ema50": data.get("ema50") or data.get("EMA50"),
        "ema200": data.get("ema200") or data.get("EMA200"),
        "price": data.get("price") or data.get("close"),
    }

    macd = _float(mapping["macd"])
    signal = _float(mapping["macd_signal"])
    if macd is not None and signal is not None:
        mapping["macd_bullish"] = macd > signal
        mapping["macd_bearish"] = macd < signal

    return {k: v for k, v in mapping.items() if v is not None}


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
