"""Regime classification from indicator snapshots."""

from __future__ import annotations

from typing import Any

REGIMES = {
    "RANGING",
    "TRENDING_BULLISH",
    "TRENDING_BEARISH",
    "TRANSITIONAL",
    "OVEREXTENDED",
}


def classify_regime(indicators: dict[str, Any]) -> dict[str, Any]:
    """
    Classify market regime from H4 indicator bundle.

    Expected indicators keys: adx, ema50, ema200, price
    """
    adx = _float(indicators.get("adx"))
    ema50 = _float(indicators.get("ema50"))
    ema200 = _float(indicators.get("ema200"))
    price = _float(indicators.get("price"))

    if adx is None or ema50 is None or ema200 is None or price is None:
        return {
            "regime": "TRANSITIONAL",
            "reason": "Missing H4 indicator data",
            "adx": adx,
            "ema50": ema50,
            "ema200": ema200,
            "price": price,
        }

    if adx > 60:
        regime = "OVEREXTENDED"
        reason = f"ADX {adx:.1f} > 60"
    elif adx < 20:
        regime = "RANGING"
        reason = f"ADX {adx:.1f} < 20"
    elif ema50 > ema200 and price > ema50:
        regime = "TRENDING_BULLISH"
        reason = "EMA50 > EMA200 and price > EMA50"
    elif ema50 < ema200 and price < ema50:
        regime = "TRENDING_BEARISH"
        reason = "EMA50 < EMA200 and price < EMA50"
    else:
        regime = "TRANSITIONAL"
        reason = "Mixed EMA alignment or price between EMAs"

    return {
        "regime": regime,
        "reason": reason,
        "adx": adx,
        "ema50": ema50,
        "ema200": ema200,
        "price": price,
    }


def evaluate_entry_checklist(
    regime: dict[str, Any],
    h1: dict[str, Any],
    direction: str,
) -> dict[str, Any]:
    """Score the 5-item trend entry checklist."""
    items: list[dict[str, Any]] = []

    target_regime = "TRENDING_BULLISH" if direction == "LONG" else "TRENDING_BEARISH"
    regime_pass = regime.get("regime") == target_regime
    items.append({"item": "Regime", "value": regime.get("regime"), "pass": regime_pass})

    rsi = _float(h1.get("rsi"))
    if direction == "LONG":
        rsi_pass = rsi is not None and 40 <= rsi <= 65
        rsi_range = "40-65"
    else:
        rsi_pass = rsi is not None and 35 <= rsi <= 60
        rsi_range = "35-60"
    items.append({"item": "RSI H1", "value": rsi, "range": rsi_range, "pass": rsi_pass})

    macd_pass = bool(h1.get("macd_bullish")) if direction == "LONG" else bool(h1.get("macd_bearish"))
    items.append(
        {
            "item": "MACD H1",
            "value": {
                "macd": h1.get("macd"),
                "signal": h1.get("macd_signal"),
                "histogram": h1.get("macd_histogram"),
            },
            "pass": macd_pass,
        }
    )

    price = _float(h1.get("price"))
    ema50 = _float(h1.get("ema50"))
    if direction == "LONG":
        ema_pass = price is not None and ema50 is not None and price > ema50
    else:
        ema_pass = price is not None and ema50 is not None and price < ema50
    items.append({"item": "Price vs EMA50 H1", "value": {"price": price, "ema50": ema50}, "pass": ema_pass})

    veto_active = bool(h1.get("veto_active", False))
    items.append({"item": "No veto conditions", "value": veto_active, "pass": not veto_active})

    passed = sum(1 for item in items if item["pass"])
    if passed >= 5:
        confidence = "HIGH"
    elif passed >= 3:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return {
        "direction": direction,
        "passed": passed,
        "total": len(items),
        "confidence": confidence,
        "items": items,
    }


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
