"""Deterministic position management — break-even moves, trailing stops, partial closes.

These rules are protective (they only ever reduce risk on open positions), so
they run every execution cycle regardless of veto state, before the LLM.
Per-ticket action flags persist in data/position_state.json so one-time
actions (break-even move, partial close) never repeat.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_BREAKEVEN_R = 1.0
DEFAULT_TRAIL_R = 1.5
DEFAULT_TRAIL_ATR_MULT = 1.0
DEFAULT_PARTIAL_FRACTION = 0.5
DEFAULT_MIN_PARTIAL_VOLUME = 0.02
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0


def _state_path(state_dir: Path | str) -> Path:
    base = Path(state_dir)
    if not base.is_absolute():
        base = ROOT / base
    base.mkdir(parents=True, exist_ok=True)
    return base / "position_state.json"


def load_position_state(state_dir: Path | str) -> dict[str, dict[str, Any]]:
    path = _state_path(state_dir)
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_position_state(state: dict[str, dict[str, Any]], state_dir: Path | str) -> None:
    path = _state_path(state_dir)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".position_state.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _side(position: dict[str, Any]) -> int:
    raw = position.get("type")
    if raw in (0, "0", "BUY", "POSITION_TYPE_BUY"):
        return 1
    if raw in (1, "1", "SELL", "POSITION_TYPE_SELL"):
        return -1
    return 0


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _current_price(market_state: dict[str, Any], symbol: str, side: int) -> float | None:
    """Price at which the position would close: bid for longs, ask for shorts."""
    tick_payload = market_state.get("ticks", {}).get(symbol, {})
    tick = tick_payload.get("tick", tick_payload)
    if side > 0:
        return _float(tick.get("bid"))
    return _float(tick.get("ask") or tick.get("bid"))


async def manage_positions(
    mt5: Any,
    market_state: dict[str, Any],
    *,
    manage_config: dict[str, Any] | None = None,
    state_dir: Path | str = "data",
) -> dict[str, Any]:
    """Apply protective management rules to all open positions."""
    cfg = manage_config or {}
    breakeven_r = float(cfg.get("breakeven_r", DEFAULT_BREAKEVEN_R))
    trail_r = float(cfg.get("trail_r", DEFAULT_TRAIL_R))
    trail_atr_mult = float(cfg.get("trail_atr_mult", DEFAULT_TRAIL_ATR_MULT))
    partial_fraction = float(cfg.get("partial_close_fraction", DEFAULT_PARTIAL_FRACTION))
    min_partial_volume = float(cfg.get("min_partial_volume", DEFAULT_MIN_PARTIAL_VOLUME))

    state = load_position_state(state_dir)
    positions = market_state.get("positions") or []
    actions: list[dict[str, Any]] = []

    open_tickets = set()
    for position in positions:
        ticket = position.get("ticket")
        symbol = str(position.get("symbol", "")).upper()
        side = _side(position)
        if not ticket or not symbol or side == 0:
            continue
        key = str(ticket)
        open_tickets.add(key)
        flags = state.setdefault(key, {})

        entry = _float(position.get("price_open"))
        sl = _float(position.get("sl") or position.get("stop_loss"))
        volume = _float(position.get("volume") or position.get("volume_current")) or 0.0
        current = _current_price(market_state, symbol, side)
        indicators = market_state.get("indicators", {}).get(symbol, {})
        h1 = indicators.get("H1", {})
        atr = _float(h1.get("atr"))
        rsi = _float(h1.get("rsi"))

        if entry is None or current is None:
            continue

        profit_distance = (current - entry) * side
        r_distance = abs(entry - sl) if sl and sl > 0 else None

        # --- One-time 50% partial close on RSI extreme against the position ---
        rsi_extreme = rsi is not None and (
            (side > 0 and rsi >= RSI_OVERBOUGHT) or (side < 0 and rsi <= RSI_OVERSOLD)
        )
        if (
            rsi_extreme
            and not flags.get("partial_closed")
            and volume >= min_partial_volume
            and profit_distance > 0
        ):
            close_volume = round(volume * partial_fraction, 2)
            if close_volume > 0:
                result = await mt5.call_tool(
                    "close_position", {"ticket": ticket, "lot_size": close_volume}
                )
                ok = isinstance(result, dict) and result.get("success", False)
                if ok:
                    flags["partial_closed"] = True
                    volume = round(volume - close_volume, 2)
                actions.append({
                    "ticket": ticket,
                    "symbol": symbol,
                    "action": "partial_close",
                    "volume": close_volume,
                    "reason": f"RSI {rsi:.1f} extreme vs {('LONG' if side > 0 else 'SHORT')}",
                    "success": ok,
                })

        if r_distance is None or r_distance <= 0:
            continue

        # --- Break-even move at >= breakeven_r ---
        sl_at_or_past_entry = sl is not None and (sl - entry) * side >= 0
        if (
            profit_distance >= breakeven_r * r_distance
            and not flags.get("be_moved")
            and not sl_at_or_past_entry
        ):
            result = await mt5.call_tool(
                "modify_position", {"ticket": ticket, "stop_loss": entry}
            )
            ok = isinstance(result, dict) and result.get("success", False)
            if ok:
                flags["be_moved"] = True
                sl = entry
            actions.append({
                "ticket": ticket,
                "symbol": symbol,
                "action": "breakeven_move",
                "new_sl": entry,
                "reason": f"profit {profit_distance:.5f} >= {breakeven_r}R ({r_distance:.5f})",
                "success": ok,
            })

        # --- ATR trailing stop at >= trail_r; only ever tightens ---
        if profit_distance >= trail_r * r_distance and atr is not None and atr > 0:
            candidate = current - side * trail_atr_mult * atr
            improves = sl is None or (candidate - sl) * side > 0
            if improves:
                result = await mt5.call_tool(
                    "modify_position", {"ticket": ticket, "stop_loss": candidate}
                )
                ok = isinstance(result, dict) and result.get("success", False)
                actions.append({
                    "ticket": ticket,
                    "symbol": symbol,
                    "action": "trail_stop",
                    "new_sl": candidate,
                    "reason": f"trailing at {trail_atr_mult}xATR ({atr:.5f})",
                    "success": ok,
                })

    # Prune flags for tickets no longer open so the file can't grow forever.
    stale = [key for key in state if key not in open_tickets]
    for key in stale:
        del state[key]

    save_position_state(state, state_dir)
    return {"ran": True, "actions": actions}
