"""Offline fixture data for Phase 0 development without live MT5/Massive feeds."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from cycle.regime import classify_regime

# Representative demo prices (not live — tagged in log meta as mock_mode).
_PAIR_DEFAULTS: dict[str, dict[str, float]] = {
    "EURUSD": {"bid": 1.08412, "ask": 1.08428, "point": 0.00001},
    "GBPUSD": {"bid": 1.27140, "ask": 1.27158, "point": 0.00001},
    "USDJPY": {"bid": 156.420, "ask": 156.438, "point": 0.001},
}

MOCK_SCENARIOS = (
    "trending_bullish",
    "ranging",
    "overextended",
    "high_spread",
    "open_position",
    "cold_market",
)

_scenario_index = 0


def list_mock_scenarios() -> tuple[str, ...]:
    return MOCK_SCENARIOS


def next_mock_scenario() -> str:
    """Round-robin through scenarios for varied batch testing."""
    global _scenario_index
    scenario = MOCK_SCENARIOS[_scenario_index % len(MOCK_SCENARIOS)]
    _scenario_index += 1
    return scenario


def reset_scenario_rotation() -> None:
    global _scenario_index
    _scenario_index = 0


def build_mock_market_state(
    pairs: list[str],
    cycle_id: str,
    *,
    scenario: str | None = None,
) -> dict[str, Any]:
    """Build a market_state snapshot for the given mock scenario."""
    chosen = scenario or next_mock_scenario()
    if chosen not in MOCK_SCENARIOS:
        chosen = "trending_bullish"

    ticks: dict[str, Any] = {}
    indicators: dict[str, Any] = {}

    for pair in pairs:
        defaults = _PAIR_DEFAULTS.get(pair, {"bid": 1.0, "ask": 1.0002, "point": 0.00001})
        bid, ask, point = defaults["bid"], defaults["ask"], defaults["point"]

        if chosen == "high_spread" and pair == "EURUSD":
            ask = bid + 0.00050  # ~5 pips — exceeds 2.0 pip veto

        spread = ask - bid
        ticks[pair] = {
            "success": True,
            "symbol": pair,
            "tick": {"bid": bid, "ask": ask, "time": _unix_now()},
            "spread": spread,
            "spread_points": spread / point,
            "point": point,
        }
        indicators[pair] = _mock_indicators(pair, chosen)

    for pair in pairs:
        h4 = indicators[pair]["H4"]
        indicators[pair]["regime"] = classify_regime(
            {
                "adx": h4["adx"],
                "ema50": h4["ema50"],
                "ema200": h4["ema200"],
                "price": h4["price"],
            }
        )

    positions: list[dict[str, Any]] = []
    if chosen == "open_position":
        positions.append({
            "ticket": 10001,
            "symbol": "EURUSD",
            "type": 0,
            "volume": 0.01,
            "price_open": 1.08350,
            "sl": 1.08200,
            "tp": 1.08600,
            "profit": 6.20,
        })

    return {
        "timestamp": cycle_id,
        "pairs": pairs,
        "mock_mode": True,
        "mock_scenario": chosen,
        "account": {
            "login": 0,
            "balance": 10000.0,
            "equity": 10025.50,
            "margin": 0.0,
            "free_margin": 10025.50,
            "margin_level": 0.0,
            "profit": 25.50,
            "currency": "USD",
            "leverage": 50,
            "trade_allowed": True,
        },
        "positions": positions,
        "pending_orders": [],
        "ticks": ticks,
        "indicators": indicators,
        "news": _mock_news(chosen),
        "errors": [],
    }


def mock_llm_decision(cycle_id: str, market_state: dict[str, Any]) -> dict[str, Any]:
    """Deterministic decision for offline pipeline testing (no API key)."""
    scenario = market_state.get("mock_scenario", "trending_bullish")
    regime = market_state.get("indicators", {}).get("EURUSD", {}).get("regime", {})
    regime_name = regime.get("regime", "UNKNOWN")

    if scenario == "open_position":
        return _mock_modify_decision(cycle_id, regime_name)
    if scenario in {"trending_bullish"} and regime_name == "TRENDING_BULLISH":
        return _mock_enter_decision(cycle_id, regime_name)

    return {
        "action": "HOLD",
        "pair": "EURUSD",
        "direction": None,
        "order_type": "BUY_LIMIT",
        "lot_size": 0.0,
        "entry_price": None,
        "entry_window": None,
        "stop_loss": None,
        "take_profit": None,
        "reasoning": (
            f"[MOCK LLM] Scenario={scenario}, Regime={regime_name}. "
            "No entry criteria fully met — holding."
        ),
        "confidence": "LOW",
        "cycle_id": cycle_id,
    }


def _mock_enter_decision(cycle_id: str, regime_name: str) -> dict[str, Any]:
    entry = 1.08420
    atr = 0.0008
    sl = round(entry - 1.5 * atr, 5)
    tp = round(entry + 2.5 * atr, 5)
    return {
        "action": "ENTER",
        "pair": "EURUSD",
        "direction": "LONG",
        "order_type": "BUY_LIMIT",
        "lot_size": 0.01,
        "entry_price": entry,
        "entry_window": [round(entry - atr, 5), round(entry + atr, 5)],
        "stop_loss": sl,
        "take_profit": tp,
        "reasoning": (
            "1. VETO CHECK: all conditions PASS\n"
            f"2. REGIME CLASSIFICATION: {regime_name}, ADX 28.5, EMA50 > EMA200\n"
            "3. SIGNAL EVALUATION: RSI 52 PASS, MACD bullish PASS, price > EMA50 PASS\n"
            "4. RISK CALCULATION: ATR 0.0008, SL 12 pips, TP 20 pips, R:R 1.67, lot 0.01\n"
            "5. DECISION: ENTER LONG HIGH confidence via BUY_LIMIT"
        ),
        "confidence": "HIGH",
        "cycle_id": cycle_id,
    }


def _mock_modify_decision(cycle_id: str, regime_name: str) -> dict[str, Any]:
    return {
        "action": "MODIFY",
        "pair": "EURUSD",
        "direction": "LONG",
        "order_type": "BUY_LIMIT",
        "lot_size": 0.01,
        "entry_price": None,
        "entry_window": None,
        "stop_loss": 1.08300,
        "take_profit": 1.08650,
        "reasoning": (
            "1. VETO CHECK: all conditions PASS\n"
            f"2. REGIME CLASSIFICATION: {regime_name}, managing open position\n"
            "3. SIGNAL EVALUATION: position management — tighten SL to BE+0.5xATR\n"
            "4. RISK CALCULATION: new SL 1.08300, TP 1.08650, R:R preserved\n"
            "5. DECISION: MODIFY open EURUSD LONG position"
        ),
        "confidence": "MEDIUM",
        "cycle_id": cycle_id,
    }


def _mock_indicators(pair: str, scenario: str) -> dict[str, Any]:
    defaults = _PAIR_DEFAULTS.get(pair, {"bid": 1.0, "ask": 1.0002})
    price = defaults["bid"]

    if pair != "EURUSD":
        return _cold_pair_indicators(price)

    if scenario == "ranging":
        return {
            "H4": {"adx": 15.0, "ema50": price, "ema200": price, "price": price, "atr": 0.0006},
            "H1": {"rsi": 50.0, "macd": 0.0, "macd_signal": 0.0, "macd_histogram": 0.0,
                    "ema50": price, "price": price, "atr": 0.0005},
            "H1_prev": {"rsi": 50.0, "macd_histogram": 0.0},
            "M15": {"rsi": 50.0, "price": price},
        }

    if scenario == "overextended":
        return {
            "H4": {"adx": 65.0, "ema50": 1.0820, "ema200": 1.0780, "price": price, "atr": 0.0015},
            "H1": {"rsi": 72.0, "macd": 0.0003, "macd_signal": 0.0002, "macd_histogram": 0.0001,
                    "macd_bullish": True, "ema50": 1.0835, "price": price, "atr": 0.0010},
            "H1_prev": {"rsi": 70.0, "macd_histogram": 0.00008},
            "M15": {"rsi": 74.0, "price": price},
        }

    if scenario == "cold_market":
        return {
            "H4": {"adx": 14.0, "ema50": price, "ema200": price, "price": price, "atr": 0.0004},
            "H1": {"rsi": 28.0, "macd": -0.0002, "macd_signal": -0.0001, "macd_histogram": -0.0001,
                    "macd_bearish": True, "ema50": price + 0.002, "price": price, "atr": 0.0004},
            "H1_prev": {"rsi": 30.0, "macd_histogram": -0.00005},
            "M15": {"rsi": 25.0, "price": price},
        }

    # trending_bullish, high_spread, open_position — warm signal
    return {
        "H4": {"adx": 28.5, "ema50": 1.0820, "ema200": 1.0780, "price": price, "atr": 0.0012},
        "H1": {
            "rsi": 52.0,
            "macd": 0.00018,
            "macd_signal": 0.00012,
            "macd_histogram": 0.00006,
            "macd_bullish": True,
            "macd_bearish": False,
            "ema50": 1.0830,
            "ema200": 1.0795,
            "price": price,
            "atr": 0.0008,
        },
        "H1_prev": {
            "rsi": 48.0,
            "macd": 0.00010,
            "macd_signal": 0.00012,
            "macd_histogram": -0.00002,
            "price": price - 0.0005,
        },
        "H4_prev": {"adx": 18.5},
        "M15": {"rsi": 54.0, "price": price},
    }


def _cold_pair_indicators(price: float) -> dict[str, Any]:
    return {
        "H4": {"adx": 14.0, "ema50": price, "ema200": price, "price": price, "atr": 0.0010},
        "H1": {"rsi": 50.0, "macd": 0.0, "macd_signal": 0.0, "macd_histogram": 0.0,
                "ema50": price, "price": price, "atr": 0.0007},
        "H1_prev": {"rsi": 50.0, "macd_histogram": 0.0},
        "M15": {"rsi": 50.0, "price": price},
    }


def _mock_news(scenario: str) -> list[dict[str, Any]]:
    if scenario != "high_spread":
        return []
    soon = datetime.now(timezone.utc).isoformat()
    return [{"title": "US NFP", "impact": "HIGH", "time": soon}]


def _unix_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())
