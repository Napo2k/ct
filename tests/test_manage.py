"""Tests for deterministic position management (BE moves, trailing, partial closes)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.manage import load_position_state, manage_positions, save_position_state


class FakeMT5:
    def __init__(self):
        self.calls = []

    async def call_tool(self, tool, args=None):
        self.calls.append((tool, args or {}))
        return {"success": True, "result": {"retcode": 10009}}


def _market_state(*, side=0, entry=1.0800, sl=1.0770, bid=1.0800, volume=0.02,
                  atr=0.0010, rsi=55.0, ticket=42):
    return {
        "positions": [{
            "ticket": ticket,
            "symbol": "EURUSD",
            "type": side,  # 0=long, 1=short
            "price_open": entry,
            "sl": sl,
            "tp": 1.0900,
            "volume": volume,
        }],
        "ticks": {"EURUSD": {"tick": {"bid": bid, "ask": bid + 0.0001}}},
        "indicators": {"EURUSD": {"H1": {"atr": atr, "rsi": rsi}}},
    }


def _run(mt5, state, tmp_path, config=None):
    return asyncio.run(manage_positions(
        mt5, state, manage_config=config or {}, state_dir=tmp_path,
    ))


def test_no_action_below_thresholds(tmp_path):
    mt5 = FakeMT5()
    # 10 pips profit on a 30-pip stop → 0.33R, below 1R breakeven trigger
    state = _market_state(bid=1.0810)
    result = _run(mt5, state, tmp_path)
    assert result["actions"] == []
    assert mt5.calls == []


def test_breakeven_move_at_1r(tmp_path):
    mt5 = FakeMT5()
    # 30-pip stop, 35 pips profit → > 1R
    state = _market_state(bid=1.0835)
    result = _run(mt5, state, tmp_path)
    actions = {a["action"] for a in result["actions"]}
    assert "breakeven_move" in actions
    modify = next(c for c in mt5.calls if c[0] == "modify_position")
    assert modify[1]["stop_loss"] == 1.0800

    # Second cycle: flag persisted, no repeat
    mt5_2 = FakeMT5()
    result_2 = _run(mt5_2, state, tmp_path)
    assert not any(a["action"] == "breakeven_move" for a in result_2["actions"])


def test_trailing_stop_at_1_5r_only_tightens(tmp_path):
    mt5 = FakeMT5()
    # 30-pip stop, 50 pips profit → > 1.5R; trail at 10-pip ATR → SL = 1.0840
    state = _market_state(bid=1.0850)
    result = _run(mt5, state, tmp_path)
    trail = [a for a in result["actions"] if a["action"] == "trail_stop"]
    assert trail and abs(trail[0]["new_sl"] - 1.0840) < 1e-9

    # Price retreats: candidate SL (1.0830) is worse than current (1.0840) — no action.
    # Simulate broker state where SL is already at 1.0840.
    mt5_2 = FakeMT5()
    state_2 = _market_state(bid=1.0840, sl=1.0840)
    result_2 = _run(mt5_2, state_2, tmp_path)
    assert not any(a["action"] == "trail_stop" for a in result_2["actions"])


def test_trailing_short_position(tmp_path):
    mt5 = FakeMT5()
    # Short from 1.0800, SL 1.0830 (30 pips), price at 1.0750 → 50 pips ≈ 1.67R
    state = _market_state(side=1, entry=1.0800, sl=1.0830, bid=1.0750)
    result = _run(mt5, state, tmp_path)
    trail = [a for a in result["actions"] if a["action"] == "trail_stop"]
    assert trail
    # Short closes at ask = bid + 0.0001 → 1.0751 + 0.0010 ATR
    assert abs(trail[0]["new_sl"] - 1.0761) < 1e-9


def test_partial_close_on_rsi_extreme_once(tmp_path):
    mt5 = FakeMT5()
    # Long in profit, RSI 75 → close 50% of 0.02
    state = _market_state(bid=1.0820, rsi=75.0)
    result = _run(mt5, state, tmp_path)
    partial = [a for a in result["actions"] if a["action"] == "partial_close"]
    assert partial and partial[0]["volume"] == 0.01
    close = next(c for c in mt5.calls if c[0] == "close_position")
    assert close[1]["lot_size"] == 0.01

    # Never repeats for the same ticket
    mt5_2 = FakeMT5()
    result_2 = _run(mt5_2, state, tmp_path)
    assert not any(a["action"] == "partial_close" for a in result_2["actions"])


def test_partial_close_skipped_at_min_volume(tmp_path):
    mt5 = FakeMT5()
    state = _market_state(bid=1.0820, rsi=75.0, volume=0.01)
    result = _run(mt5, state, tmp_path)
    assert not any(a["action"] == "partial_close" for a in result["actions"])


def test_partial_close_requires_profit(tmp_path):
    mt5 = FakeMT5()
    # RSI extreme but position underwater — closing half locks in a loss; skip
    state = _market_state(bid=1.0780, rsi=75.0)
    result = _run(mt5, state, tmp_path)
    assert not any(a["action"] == "partial_close" for a in result["actions"])


def test_state_pruned_for_closed_tickets(tmp_path):
    save_position_state({"999": {"be_moved": True}}, tmp_path)
    mt5 = FakeMT5()
    state = _market_state(ticket=42, bid=1.0805)
    _run(mt5, state, tmp_path)
    remaining = load_position_state(tmp_path)
    assert "999" not in remaining


def test_no_sl_skips_r_based_rules(tmp_path):
    mt5 = FakeMT5()
    state = _market_state(sl=0, bid=1.0850)
    result = _run(mt5, state, tmp_path)
    assert not any(a["action"] in {"breakeven_move", "trail_stop"} for a in result["actions"])
