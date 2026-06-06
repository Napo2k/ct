"""Deterministic exit-signal evaluator tests."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.config import CycleConfig
from cycle.decision import protective_exit_decision, validate_decision
from cycle.exits import (
    HARD,
    evaluate_exit_signals,
    evaluate_position_exit,
    first_forced_exit,
)
from cycle.mock_data import build_mock_market_state
from cycle.mock_mt5 import MockMT5Client


_LONG = {"ticket": 1, "symbol": "EURUSD", "type": 0, "volume": 0.01}
_SHORT = {"ticket": 2, "symbol": "EURUSD", "type": 1, "volume": 0.01}


def test_trend_reversal_long_is_hard():
    indicators = {
        "H4": {"adx": 28.0, "ema50": 1.0790, "ema200": 1.0800, "price": 1.0785},
        "H4_prev": {"ema50": 1.0810, "ema200": 1.0800},
        "H1": {"rsi": 50.0},
    }
    signals = evaluate_position_exit(_LONG, indicators)
    names = {s.name: s.severity for s in signals}
    assert names.get("trend_reversal") == HARD


def test_adx_collapse_is_soft():
    indicators = {
        "H4": {"adx": 15.0, "ema50": 1.0820, "ema200": 1.0800, "price": 1.0830},
        "H1": {"rsi": 55.0},
    }
    signals = evaluate_position_exit(_LONG, indicators)
    names = {s.name: s.severity for s in signals}
    assert names.get("adx_collapse") == "SOFT"
    assert "trend_reversal" not in names


def test_rsi_overbought_long_is_soft():
    indicators = {
        "H4": {"adx": 28.0, "ema50": 1.0820, "ema200": 1.0800, "price": 1.0830},
        "H1": {"rsi": 74.0},
    }
    signals = evaluate_position_exit(_LONG, indicators)
    names = {s.name for s in signals}
    assert "rsi_extreme" in names


def test_healthy_trending_position_no_signals():
    indicators = {
        "H4": {"adx": 28.0, "ema50": 1.0820, "ema200": 1.0800, "price": 1.0830},
        "H1": {"rsi": 55.0},
    }
    signals = evaluate_position_exit(_LONG, indicators)
    assert signals == []


def test_stale_age_without_tp_is_hard():
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    old_ts = int(now.timestamp()) - (50 * 3600)
    position = {"ticket": 9, "symbol": "EURUSD", "type": 0, "time": old_ts, "tp": 0}
    indicators = {
        "H4": {"adx": 28.0, "ema50": 1.0820, "ema200": 1.0800, "price": 1.0830},
        "H1": {"rsi": 55.0},
    }
    signals = evaluate_position_exit(position, indicators, max_age_hours=48, now=now)
    names = {s.name: s.severity for s in signals}
    assert names.get("stale_age") == HARD


def test_evaluate_exit_signals_keys_by_symbol_and_flags_force():
    state = build_mock_market_state(["EURUSD"], "t", scenario="open_position")
    state["positions"][0]["type"] = 0
    state["indicators"]["EURUSD"]["H4"] = {
        "adx": 28.0,
        "ema50": 1.0790,
        "ema200": 1.0800,
        "price": 1.0785,
    }
    state["indicators"]["EURUSD"]["H4_prev"] = {"ema50": 1.0810, "ema200": 1.0800}
    result = evaluate_exit_signals(state)
    assert "EURUSD" in result
    assert result["EURUSD"]["force_exit"] is True
    assert first_forced_exit(result) == "EURUSD"


def test_protective_exit_decision_validates():
    decision = protective_exit_decision("EURUSD", "2026-06-05T12:00:00Z", "trend_reversal")
    validated = validate_decision(decision, cycle_id="2026-06-05T12:00:00Z")
    assert validated["action"] == "EXIT"
    assert validated["pair"] == "EURUSD"


def test_runner_forces_exit_override_in_mock_execution(tmp_path):
    from cycle.runner import _evaluate_and_log

    state = build_mock_market_state(["EURUSD"], "2026-06-03T10:00:00Z", scenario="open_position")
    state["positions"][0]["type"] = 0
    state["indicators"]["EURUSD"]["H4"] = {
        "adx": 28.0,
        "ema50": 1.0790,
        "ema200": 1.0800,
        "price": 1.0785,
    }
    state["indicators"]["EURUSD"]["H4_prev"] = {"ema50": 1.0810, "ema200": 1.0800}

    cfg = CycleConfig(
        phase=1,
        execution_mode=True,
        mock_mode=True,
        mock_llm=True,
        prefilter={"enabled": False},
        session_state_dir=str(tmp_path),
        playbook_path="playbook/algo_trading_skill.md",
        gitea={"repo_path": str(tmp_path), "logs_dir": "logs", "auto_commit": False},
    )
    client = MockMT5Client(state)
    client.positions = list(state["positions"])

    async def run() -> dict:
        return await _evaluate_and_log(
            cfg,
            "2026-06-03T10:00:00Z",
            state,
            client,
            {"errors": []},
            mock_meta=True,
        )

    result = asyncio.run(run())
    assert result.get("exit_override", {}).get("pair") == "EURUSD"
    assert result["decision"]["action"] == "EXIT"
