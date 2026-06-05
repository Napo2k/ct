"""Phase 0 unit tests — no MT5 or API required."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.config import CycleConfig
from cycle.decision import DecisionValidationError, hold_decision, suspend_decision, validate_decision
from cycle.executor import execute_decision
from cycle.llm import LLMError
from cycle.prefilter import has_warm_signal
from cycle.regime import classify_regime, evaluate_entry_checklist
from cycle.mock_data import build_mock_market_state, mock_llm_decision
from cycle.runner import _evaluate_and_log, run_cycle
from cycle.veto import check_vetoes


def test_classify_trending_bullish():
    result = classify_regime({"adx": 28, "ema50": 1.09, "ema200": 1.08, "price": 1.095})
    assert result["regime"] == "TRENDING_BULLISH"


def test_classify_ranging():
    result = classify_regime({"adx": 15, "ema50": 1.09, "ema200": 1.08, "price": 1.095})
    assert result["regime"] == "RANGING"


def test_classify_overextended():
    result = classify_regime({"adx": 65, "ema50": 1.09, "ema200": 1.08, "price": 1.095})
    assert result["regime"] == "OVEREXTENDED"


def test_entry_checklist_high_confidence():
    regime = {"regime": "TRENDING_BULLISH"}
    h1 = {
        "rsi": 55,
        "macd": 0.001,
        "macd_signal": 0.0005,
        "macd_bullish": True,
        "price": 1.095,
        "ema50": 1.09,
        "veto_active": False,
    }
    result = evaluate_entry_checklist(regime, h1, "LONG")
    assert result["passed"] == 5
    assert result["confidence"] == "HIGH"


def test_warm_signal_rsi_zone():
    warm, reasons = has_warm_signal(
        "EURUSD",
        {"H1": {"rsi": 45}},
        prefilter_config={"rsi_long_zone": [40, 65], "rsi_short_zone": [35, 60]},
    )
    assert warm is True
    assert any("RSI" in r for r in reasons)


def test_warm_signal_open_position():
    warm, reasons = has_warm_signal("EURUSD", {}, open_position={"ticket": 1})
    assert warm is True


def test_veto_outside_hours():
    # Saturday
    now = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
    result = check_vetoes(now, timezone="Europe/Berlin")
    assert result.blocked is True


def test_validate_enter_decision():
    decision = {
        "action": "ENTER",
        "pair": "EURUSD",
        "direction": "LONG",
        "order_type": "BUY_LIMIT",
        "lot_size": 0.01,
        "entry_price": 1.08420,
        "entry_window": [1.0835, 1.0850],
        "stop_loss": 1.08180,
        "take_profit": 1.08900,
        "reasoning": (
            "1. VETO CHECK: all PASS\n"
            "2. REGIME CLASSIFICATION: TRENDING BULLISH\n"
            "3. SIGNAL EVALUATION: 5/5 PASS\n"
            "4. RISK CALCULATION: R:R 1.67\n"
            "5. DECISION: ENTER HIGH"
        ),
        "confidence": "HIGH",
        "cycle_id": "2026-06-05T14:30:00Z",
    }
    validated = validate_decision(decision, cycle_id="2026-06-05T14:30:00Z")
    assert validated["action"] == "ENTER"


@pytest.mark.parametrize("order_type", ["BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"])
def test_enter_pending_order_requires_entry_price(order_type: str):
    decision = {
        "action": "ENTER",
        "pair": "EURUSD",
        "direction": "LONG",
        "order_type": order_type,
        "lot_size": 0.01,
        "entry_price": None,
        "entry_window": None,
        "stop_loss": 1.08180,
        "take_profit": 1.08900,
        "reasoning": (
            "1. VETO CHECK: all PASS\n"
            "2. REGIME CLASSIFICATION: TRENDING BULLISH\n"
            "3. SIGNAL EVALUATION: 5/5 PASS\n"
            "4. RISK CALCULATION: R:R 1.67\n"
            "5. DECISION: ENTER HIGH"
        ),
        "confidence": "HIGH",
    }
    with pytest.raises(DecisionValidationError, match="requires entry_price"):
        validate_decision(decision, cycle_id="test")


def test_enter_market_order_allows_null_entry_price():
    decision = {
        "action": "ENTER",
        "pair": "EURUSD",
        "direction": "LONG",
        "order_type": "BUY",
        "lot_size": 0.01,
        "entry_price": None,
        "entry_window": [1.0835, 1.0850],
        "stop_loss": 1.08180,
        "take_profit": 1.08900,
        "reasoning": (
            "1. VETO CHECK: all PASS\n"
            "2. REGIME CLASSIFICATION: TRENDING BULLISH\n"
            "3. SIGNAL EVALUATION: 5/5 PASS\n"
            "4. RISK CALCULATION: R:R 1.67\n"
            "5. DECISION: ENTER HIGH"
        ),
        "confidence": "HIGH",
    }
    validated = validate_decision(decision, cycle_id="test")
    assert validated["order_type"] == "BUY"
    assert validated["entry_price"] is None


def test_validate_rr_rejection():
    decision = {
        "action": "ENTER",
        "pair": "EURUSD",
        "direction": "LONG",
        "order_type": "BUY_LIMIT",
        "lot_size": 0.01,
        "entry_price": 1.08420,
        "entry_window": None,
        "stop_loss": 1.08300,
        "take_profit": 1.08450,
        "reasoning": (
            "1. VETO CHECK: PASS\n"
            "2. REGIME CLASSIFICATION: BULLISH\n"
            "3. SIGNAL EVALUATION: PASS\n"
            "4. RISK CALCULATION: bad R:R\n"
            "5. DECISION: ENTER"
        ),
        "confidence": "LOW",
    }
    with pytest.raises(DecisionValidationError):
        validate_decision(decision, cycle_id="test")


def test_hold_decision_schema():
    hold = hold_decision("EURUSD", "2026-06-05T12:00:00Z", "No signal")
    validated = validate_decision(hold, cycle_id="2026-06-05T12:00:00Z")
    assert validated["action"] == "HOLD"


def test_suspend_decision_schema():
    suspend = suspend_decision("2026-06-05T12:00:00Z", "Veto: daily drawdown limit reached")
    validated = validate_decision(suspend, cycle_id="2026-06-05T12:00:00Z")
    assert validated["action"] == "SUSPEND"


def test_execute_decision_accepts_suspend_helper():
    """Regression: system SUSPEND must pass validation inside execute_decision."""
    suspend = suspend_decision("2026-06-05T12:00:00Z", "MT5 MCP unavailable")

    async def run() -> dict:
        return await execute_decision(
            suspend,
            mt5=None,
            execution_mode=False,
            cycle_id="2026-06-05T12:00:00Z",
        )

    result = asyncio.run(run())
    assert result["action"] == "SUSPEND"
    assert result["simulated"] is True


def test_mock_market_state_has_warm_eurusd_signal():
    state = build_mock_market_state(["EURUSD", "GBPUSD"], "2026-06-05T12:00:00Z")
    assert state["mock_mode"] is True
    assert state["indicators"]["EURUSD"]["regime"]["regime"] == "TRENDING_BULLISH"
    warm, reasons = has_warm_signal("EURUSD", state["indicators"]["EURUSD"])
    assert warm is True


def test_mock_llm_decision_validates():
    state = build_mock_market_state(["EURUSD"], "2026-06-05T12:00:00Z")
    decision = mock_llm_decision("2026-06-05T12:00:00Z", state)
    validated = validate_decision(decision, cycle_id="2026-06-05T12:00:00Z")
    assert validated["action"] == "HOLD"


def test_run_cycle_mock_mode(tmp_path):
    cfg = CycleConfig(
        mock_mode=True,
        mock_llm=True,
        prefilter={"enabled": True},
        playbook_path="playbook/algo_trading_skill.md",
        gitea={"repo_path": str(tmp_path), "logs_dir": "logs", "auto_commit": False},
    )

    async def run() -> dict:
        return await run_cycle(cfg)

    summary = asyncio.run(run())
    assert summary["mock_mode"] is True
    assert summary.get("log_path")
    assert Path(summary["log_path"]).exists()


def test_evaluate_and_log_appends_llm_error_without_keyerror():
    """Regression: result['errors'] must exist before append on LLM failure."""
    cfg = CycleConfig(
        prefilter={"enabled": True},
        playbook_path="playbook/algo_trading_skill.md",
    )
    cycle_id = "2026-06-05T14:30:00Z"
    market_state = {"pairs": ["EURUSD"], "ticks": {}, "indicators": {}}
    summary = {"errors": []}
    mt5 = MagicMock()

    async def run() -> dict:
        with (
            patch("cycle.runner.filter_pairs", return_value=(["EURUSD"], {"EURUSD": ["warm"]})),
            patch("cycle.runner.load_playbook", return_value="playbook"),
            patch("cycle.runner.invoke_claude", side_effect=LLMError("API unavailable")),
            patch("cycle.runner.execute_decision", new_callable=AsyncMock, return_value={"simulated": True}),
            patch("cycle.runner.write_cycle_log", return_value=Path("logs/test.json")),
        ):
            return await _evaluate_and_log(cfg, cycle_id, market_state, mt5, summary)

    result = asyncio.run(run())
    assert result["errors"] == ["API unavailable"]
    assert result["decision"]["action"] == "HOLD"
