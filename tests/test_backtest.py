"""Tests for the backtest harness (broker simulation, fills, engine, metrics)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.broker import SimulatedBroker
from backtest.data import generate_synthetic_bars, load_bars_csv, resample_h1_to_h4, save_bars_csv
from backtest.engine import playbook_rule_decision, run_backtest
from backtest.metrics import compute_metrics


def _flat_bars(count, price=1.0800, start=1704067200):
    return [
        {"time": start + i * 3600, "open": price, "high": price + 0.0005,
         "low": price - 0.0005, "close": price, "tick_volume": 100}
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

def test_csv_roundtrip(tmp_path):
    bars = generate_synthetic_bars(count=50, seed=7)
    path = tmp_path / "EURUSD_H1.csv"
    save_bars_csv(bars, path)
    loaded = load_bars_csv(path)
    assert len(loaded) == 50
    assert loaded[0]["close"] == bars[0]["close"]
    assert loaded[10]["time"] == bars[10]["time"]


def test_synthetic_deterministic_per_seed():
    a = generate_synthetic_bars(count=100, seed=1)
    b = generate_synthetic_bars(count=100, seed=1)
    c = generate_synthetic_bars(count=100, seed=2)
    assert a == b
    assert a != c


def test_resample_h4():
    bars = _flat_bars(8)
    bars[1]["high"] = 1.0900
    bars[2]["low"] = 1.0700
    h4 = resample_h1_to_h4(bars)
    assert len(h4) == 2
    assert h4[0]["high"] == 1.0900
    assert h4[0]["low"] == 1.0700
    assert h4[0]["open"] == bars[0]["open"]
    assert h4[0]["close"] == bars[3]["close"]


# ---------------------------------------------------------------------------
# Broker simulation
# ---------------------------------------------------------------------------

def _broker(bars=None):
    return SimulatedBroker({"EURUSD": bars or _flat_bars(100)}, start_balance=10_000)


def test_limit_order_fills_when_price_crosses():
    bars = _flat_bars(100)
    # Bar 11 dips to the limit price
    bars[11]["low"] = 1.0780
    broker = _broker(bars)
    broker.advance_to(10)
    result = asyncio.run(broker.call_tool("place_order", {
        "symbol": "EURUSD", "order_type": "BUY_LIMIT", "lot_size": 0.01,
        "price": 1.0785, "stop_loss": 1.0755, "take_profit": 1.0835,
    }))
    assert result["success"]
    broker.advance_to(11)
    assert len(broker.positions) == 1
    assert broker.positions[0]["price_open"] == 1.0785
    assert not broker.pending_orders


def test_sl_hit_conservative_when_both_in_range():
    bars = _flat_bars(100)
    bars[12]["low"] = 1.0740   # SL hit
    bars[12]["high"] = 1.0860  # TP also hit — stop must win
    broker = _broker(bars)
    broker.advance_to(10)
    asyncio.run(broker.call_tool("place_order", {
        "symbol": "EURUSD", "order_type": "BUY", "lot_size": 0.01,
        "stop_loss": 1.0750, "take_profit": 1.0850,
        "entry_window": [1.0700, 1.0900],
    }))
    broker.advance_to(12)
    assert not broker.positions
    assert broker.closed_trades[0]["exit_reason"] == "sl"
    assert broker.closed_trades[0]["profit"] < 0


def test_tp_exit_profit_and_r_multiple():
    bars = _flat_bars(100)
    bars[12]["high"] = 1.0860
    broker = _broker(bars)
    broker.advance_to(10)
    asyncio.run(broker.call_tool("place_order", {
        "symbol": "EURUSD", "order_type": "BUY", "lot_size": 0.01,
        "stop_loss": 1.0770, "take_profit": 1.0850,
        "entry_window": [1.0700, 1.0900],
    }))
    entry = broker.positions[0]["price_open"]
    broker.advance_to(12)
    trade = broker.closed_trades[0]
    assert trade["exit_reason"] == "tp"
    assert trade["profit"] == pytest.approx((1.0850 - entry) * 0.01 * 100_000, abs=0.01)
    assert trade["r_multiple"] == pytest.approx((1.0850 - entry) / (entry - 1.0770), abs=0.01)


def test_equity_includes_floating_pnl():
    bars = _flat_bars(100)
    for i in range(11, 100):
        bars[i] = dict(bars[i], open=1.0900, high=1.0905, low=1.0895, close=1.0900)
    broker = _broker(bars)
    broker.advance_to(10)
    asyncio.run(broker.call_tool("place_order", {
        "symbol": "EURUSD", "order_type": "BUY", "lot_size": 0.01,
        "stop_loss": 1.0700, "take_profit": 1.1100,
        "entry_window": [1.0700, 1.0900],
    }))
    broker.advance_to(11)
    # ~100 pips on 0.01 lots (micro lot, $0.10/pip) ≈ $10 floating, less half-spread
    assert broker.equity() == pytest.approx(10_000 + 9.95, abs=0.5)
    assert broker.balance == 10_000  # unrealized


def test_partial_close_keeps_remainder():
    broker = _broker()
    broker.advance_to(10)
    asyncio.run(broker.call_tool("place_order", {
        "symbol": "EURUSD", "order_type": "BUY", "lot_size": 0.02,
        "stop_loss": 1.0700, "take_profit": 1.1100,
        "entry_window": [1.0700, 1.0900],
    }))
    ticket = broker.positions[0]["ticket"]
    result = asyncio.run(broker.call_tool("close_position", {"ticket": ticket, "lot_size": 0.01}))
    assert result["success"]
    assert len(broker.positions) == 1
    assert broker.positions[0]["volume"] == 0.01
    assert broker.closed_trades[0]["exit_reason"] == "partial"


def test_get_rates_serves_history_window():
    broker = _broker(_flat_bars(600))
    broker.advance_to(500)
    h1 = asyncio.run(broker.call_tool("get_rates", {"symbol": "EURUSD", "timeframe": "H1", "count": 120}))
    assert h1["success"] and len(h1["bars"]) == 120
    assert h1["bars"][-1]["time"] == broker.current_time()
    h4 = asyncio.run(broker.call_tool("get_rates", {"symbol": "EURUSD", "timeframe": "H4", "count": 100}))
    assert h4["success"] and len(h4["bars"]) == 100


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def test_rule_decision_holds_without_setup():
    state = {"indicators": {"EURUSD": {"H1": {}, "regime": {"regime": "RANGING"}}}}
    decision = playbook_rule_decision(state, "cid", ["EURUSD"])
    assert decision["action"] == "HOLD"


def test_rule_decision_enters_on_bullish_setup():
    state = {"indicators": {"EURUSD": {
        "H1": {"rsi": 55.0, "atr": 0.0010, "price": 1.0850, "ema50": 1.0830,
               "macd_histogram": 0.0002},
        "regime": {"regime": "TRENDING_BULLISH"},
    }}}
    decision = playbook_rule_decision(state, "cid", ["EURUSD"])
    assert decision["action"] == "ENTER"
    assert decision["direction"] == "LONG"
    assert decision["stop_loss"] < decision["entry_price"] < decision["take_profit"]


def test_run_backtest_synthetic_end_to_end():
    bars = generate_synthetic_bars(count=2200, seed=11)
    result = asyncio.run(run_backtest(
        {"EURUSD": bars},
        warmup_bars=1000,
        cycle_every=8,
        enable_manage=False,
    ))
    report = result["report"]
    assert report["cycles"] > 100
    assert report["bars"] == 2200
    assert report["final_equity"] > 0
    # Equity curve is consistent with trade P&L
    net = sum(t["profit"] for t in result["trades"])
    assert report["final_equity"] == pytest.approx(10_000 + net, abs=0.05)


def test_run_backtest_requires_enough_bars():
    with pytest.raises(ValueError):
        asyncio.run(run_backtest({"EURUSD": _flat_bars(100)}, warmup_bars=1000))


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def test_metrics_computation():
    trades = [
        {"profit": 100.0, "r_multiple": 1.67, "open_time": 0, "close_time": 7200, "exit_reason": "tp"},
        {"profit": -60.0, "r_multiple": -1.0, "open_time": 0, "close_time": 3600, "exit_reason": "sl"},
        {"profit": -40.0, "r_multiple": -1.0, "open_time": 0, "close_time": 3600, "exit_reason": "sl"},
    ]
    curve = [{"time": 0, "equity": 10_000}, {"time": 1, "equity": 9_900},
             {"time": 2, "equity": 10_000}]
    report = compute_metrics(trades, curve, 10_000)
    assert report["trades"] == 3
    assert report["win_rate"] == pytest.approx(0.333, abs=0.001)
    assert report["profit_factor"] == 1.0
    assert report["expectancy_r"] == pytest.approx(-0.11, abs=0.01)
    assert report["max_drawdown_pct"] == pytest.approx(1.0, abs=0.01)
    assert report["exit_reasons"] == {"tp": 1, "sl": 2}
