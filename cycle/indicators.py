"""Technical indicators computed from OHLCV bars (MT5 fallback when Massive MCP is down)."""

from __future__ import annotations

from typing import Any


def compute_indicator_bundle(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute RSI, MACD, ATR, ADX, and EMAs from MT5-style bar dicts."""
    if len(bars) < 30:
        return {}

    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    latest = bars[-1]

    rsi_val = rsi(closes, 14)
    macd_line, macd_sig, macd_hist = macd(closes)
    atr_val = atr(highs, lows, closes, 14)
    adx_val = adx(highs, lows, closes, 14)
    ema50_val = ema_last(closes, 50)
    ema200_val = ema_last(closes, 200) if len(closes) >= 200 else None

    result: dict[str, Any] = {
        "price": closes[-1],
        "open": float(latest["open"]),
        "high": float(latest["high"]),
        "low": float(latest["low"]),
        "rsi": rsi_val,
        "macd": macd_line,
        "macd_signal": macd_sig,
        "macd_histogram": macd_hist,
        "atr": atr_val,
        "adx": adx_val,
        "ema50": ema50_val,
        "ema200": ema200_val,
        "data_source": "mt5_fallback",
    }

    if macd_line is not None and macd_sig is not None:
        result["macd_bullish"] = macd_line > macd_sig
        result["macd_bearish"] = macd_line < macd_sig

    return {k: v for k, v in result.items() if v is not None}


def ema_series(values: list[float], period: int) -> list[float | None]:
    if len(values) < period:
        return [None] * len(values)

    multiplier = 2 / (period + 1)
    seed = sum(values[:period]) / period
    series: list[float | None] = [None] * (period - 1) + [seed]
    ema = seed
    for value in values[period:]:
        ema = (value - ema) * multiplier + ema
        series.append(ema)
    return series


def ema_last(values: list[float], period: int) -> float | None:
    series = ema_series(values, period)
    return series[-1] if series else None


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None

    gains: list[float] = []
    losses: list[float] = []
    for idx in range(1, len(closes)):
        delta = closes[idx] - closes[idx - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for idx in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[idx]) / period
        avg_loss = (avg_loss * (period - 1) + losses[idx]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[float | None, float | None, float | None]:
    if len(closes) < slow + signal:
        return None, None, None

    fast_ema = ema_series(closes, fast)
    slow_ema = ema_series(closes, slow)
    macd_line: list[float | None] = []
    for fast_val, slow_val in zip(fast_ema, slow_ema):
        if fast_val is None or slow_val is None:
            macd_line.append(None)
        else:
            macd_line.append(fast_val - slow_val)

    valid = [value for value in macd_line if value is not None]
    if len(valid) < signal:
        return None, None, None

    signal_series = ema_series(valid, signal)
    line = valid[-1]
    sig = signal_series[-1]
    if sig is None:
        return line, None, None
    return line, sig, line - sig


def atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float | None:
    if len(closes) < period + 1:
        return None

    true_ranges: list[float] = []
    for idx in range(1, len(closes)):
        high_low = highs[idx] - lows[idx]
        high_close = abs(highs[idx] - closes[idx - 1])
        low_close = abs(lows[idx] - closes[idx - 1])
        true_ranges.append(max(high_low, high_close, low_close))

    if len(true_ranges) < period:
        return None

    atr_val = sum(true_ranges[:period]) / period
    for idx in range(period, len(true_ranges)):
        atr_val = (atr_val * (period - 1) + true_ranges[idx]) / period
    return atr_val


def adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float | None:
    if len(closes) < period * 2:
        return None

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    true_ranges: list[float] = []

    for idx in range(1, len(closes)):
        up_move = highs[idx] - highs[idx - 1]
        down_move = lows[idx - 1] - lows[idx]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        high_low = highs[idx] - lows[idx]
        high_close = abs(highs[idx] - closes[idx - 1])
        low_close = abs(lows[idx] - closes[idx - 1])
        true_ranges.append(max(high_low, high_close, low_close))

    if len(true_ranges) < period:
        return None

    tr_smooth = sum(true_ranges[:period])
    plus_smooth = sum(plus_dm[:period])
    minus_smooth = sum(minus_dm[:period])

    dx_values: list[float] = []
    for idx in range(period, len(true_ranges)):
        tr_smooth = tr_smooth - (tr_smooth / period) + true_ranges[idx]
        plus_smooth = plus_smooth - (plus_smooth / period) + plus_dm[idx]
        minus_smooth = minus_smooth - (minus_smooth / period) + minus_dm[idx]

        if tr_smooth == 0:
            continue

        plus_di = 100 * plus_smooth / tr_smooth
        minus_di = 100 * minus_smooth / tr_smooth
        di_sum = plus_di + minus_di
        if di_sum == 0:
            continue
        dx_values.append(100 * abs(plus_di - minus_di) / di_sum)

    if len(dx_values) < period:
        return None

    adx_val = sum(dx_values[:period]) / period
    for idx in range(period, len(dx_values)):
        adx_val = (adx_val * (period - 1) + dx_values[idx]) / period
    return adx_val
