"""Unit tests for MT5 OHLCV indicator calculations."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.indicators import adx, atr, compute_indicator_bundle, ema_last, macd, rsi


def _trending_bars(count: int, start: float = 1.08, step: float = 0.0005) -> list[dict]:
    bars = []
    price = start
    for _ in range(count):
        bars.append({
            "open": price,
            "high": price + 0.0004,
            "low": price - 0.0002,
            "close": price + 0.0003,
        })
        price += step
    return bars


def test_rsi_rising_market_above_midline():
    closes = [1.0 + i * 0.01 for i in range(30)]
    value = rsi(closes, 14)
    assert value is not None
    assert value > 50


def test_ema_last_follows_trend():
    closes = [1.0 + i * 0.01 for i in range(60)]
    value = ema_last(closes, 50)
    assert value is not None
    assert value > closes[0]


def test_macd_bullish_on_uptrend():
    closes = [1.0 + i * 0.005 for i in range(80)]
    line, signal, hist = macd(closes)
    assert line is not None
    assert signal is not None
    assert hist is not None
    assert line >= signal


def test_atr_positive_on_volatile_bars():
    bars = _trending_bars(40)
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    value = atr(highs, lows, closes, 14)
    assert value is not None
    assert value > 0


def test_adx_trending_series():
    bars = _trending_bars(80, step=0.001)
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    value = adx(highs, lows, closes, 14)
    assert value is not None
    assert value > 0


def test_compute_indicator_bundle_populates_core_fields():
    bundle = compute_indicator_bundle(_trending_bars(250, step=0.001))
    assert bundle["data_source"] == "mt5_fallback"
    assert bundle["rsi"] is not None
    assert bundle["adx"] is not None
    assert bundle["ema50"] is not None
    assert bundle["ema200"] is not None
    assert bundle["atr"] is not None
    assert bundle["macd_histogram"] is not None
