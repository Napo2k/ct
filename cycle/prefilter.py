"""Pre-filter: skip LLM unless warm signals detected."""

from __future__ import annotations

from typing import Any


def has_warm_signal(
    pair: str,
    indicators: dict[str, Any],
    *,
    prefilter_config: dict[str, Any] | None = None,
    open_position: dict[str, Any] | None = None,
    pending_orders: list[dict[str, Any]] | None = None,
) -> tuple[bool, list[str]]:
    """
    Return (should_invoke_llm, reasons).

    Warm signals:
    - RSI H1 entered long/short zone
    - MACD H1 histogram sign flip or signal crossover
    - ADX H4 crossed threshold (prev vs current)
    - Open position or pending order exists (position management)
    """
    reasons: list[str] = []
    cfg = prefilter_config or {}
    rsi_long = cfg.get("rsi_long_zone", [40, 65])
    rsi_short = cfg.get("rsi_short_zone", [35, 60])
    adx_threshold = float(cfg.get("adx_threshold", 20))

    if open_position:
        reasons.append(f"{pair}: open position requires management")
        return True, reasons

    if pending_orders:
        reasons.append(f"{pair}: {len(pending_orders)} pending order(s)")
        return True, reasons

    h1 = indicators.get("H1", {})
    h4 = indicators.get("H4", {})
    h1_prev = indicators.get("H1_prev", {})

    rsi = _float(h1.get("rsi"))
    if rsi is not None:
        if rsi_long[0] <= rsi <= rsi_long[1]:
            reasons.append(f"{pair}: RSI H1 {rsi:.1f} in long zone {rsi_long}")
        if rsi_short[0] <= rsi <= rsi_short[1]:
            reasons.append(f"{pair}: RSI H1 {rsi:.1f} in short zone {rsi_short}")

    macd_hist = _float(h1.get("macd_histogram"))
    macd_hist_prev = _float(h1_prev.get("macd_histogram"))
    if macd_hist is not None and macd_hist_prev is not None:
        if (macd_hist_prev <= 0 < macd_hist) or (macd_hist_prev >= 0 > macd_hist):
            reasons.append(f"{pair}: MACD histogram sign flip ({macd_hist_prev:.5f} → {macd_hist:.5f})")

    macd = _float(h1.get("macd"))
    macd_signal = _float(h1.get("macd_signal"))
    macd_prev = _float(h1_prev.get("macd"))
    signal_prev = _float(h1_prev.get("macd_signal"))
    if all(v is not None for v in (macd, macd_signal, macd_prev, signal_prev)):
        bullish_cross = macd_prev <= signal_prev and macd > macd_signal
        bearish_cross = macd_prev >= signal_prev and macd < macd_signal
        if bullish_cross or bearish_cross:
            reasons.append(f"{pair}: MACD signal crossover")

    adx = _float(h4.get("adx"))
    adx_prev = _float(h4.get("adx_prev") or indicators.get("H4_prev", {}).get("adx"))
    if adx is not None and adx_prev is not None:
        if (adx_prev < adx_threshold <= adx) or (adx_prev >= adx_threshold > adx):
            reasons.append(f"{pair}: ADX H4 crossed {adx_threshold} ({adx_prev:.1f} → {adx:.1f})")

    regime = indicators.get("regime", {})
    if regime.get("regime") in {"TRENDING_BULLISH", "TRENDING_BEARISH", "OVEREXTENDED"}:
        reasons.append(f"{pair}: active regime {regime['regime']}")

    return len(reasons) > 0, reasons


def filter_pairs(
    pairs: list[str],
    market_state: dict[str, Any],
    *,
    prefilter_config: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, list[str]]]:
    """Return pairs that should invoke the LLM and per-pair warm signal reasons."""
    active: list[str] = []
    reasons_by_pair: dict[str, list[str]] = {}

    positions = {p["symbol"]: p for p in market_state.get("positions", []) if p.get("symbol")}
    orders_by_symbol: dict[str, list] = {}
    for order in market_state.get("pending_orders", []):
        sym = order.get("symbol")
        if sym:
            orders_by_symbol.setdefault(sym, []).append(order)

    for pair in pairs:
        indicators = market_state.get("indicators", {}).get(pair, {})
        warm, reasons = has_warm_signal(
            pair,
            indicators,
            prefilter_config=prefilter_config,
            open_position=positions.get(pair),
            pending_orders=orders_by_symbol.get(pair),
        )
        if warm:
            active.append(pair)
            reasons_by_pair[pair] = reasons

    return active, reasons_by_pair


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
