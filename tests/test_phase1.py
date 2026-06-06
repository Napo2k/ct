"""Phase 1 execution tests — mock MT5, no live broker."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.config import CycleConfig
from cycle.decision import validate_decision
from cycle.executor import emergency_close_all, execute_decision
from cycle.mock_data import build_mock_market_state, mock_llm_decision, reset_scenario_rotation
from cycle.mock_mt5 import MockMT5Client
from cycle.risk import check_enter_risk
from cycle.maintenance import run_maintenance
from cycle.prefilter import has_warm_signal
from cycle.prefilter_state import (
    enrich_with_previous,
    load_prefilter_state,
    save_prefilter_state,
    snapshot_indicators,
    update_prefilter_state,
)
from cycle.runner import run_cycle
from cycle.session_state import (
    SessionState,
    apply_lot_multiplier,
    begin_cycle,
    load_session,
    lot_multiplier_for_losses,
    save_session,
)


def test_enter_risk_blocks_duplicate_pair():
    state = build_mock_market_state(["EURUSD"], "t", scenario="open_position")
    decision = mock_llm_decision("t", state)
    decision["action"] = "ENTER"
    decision["order_type"] = "BUY_LIMIT"
    decision["entry_price"] = 1.08420
    risk = check_enter_risk(decision, state, max_positions=3)
    assert risk.allowed is False


def test_enter_risk_blocks_market_without_entry_window():
    state = build_mock_market_state(["EURUSD"], "t", scenario="trending_bullish")
    decision = {
        "action": "ENTER",
        "pair": "EURUSD",
        "direction": "LONG",
        "order_type": "BUY",
        "lot_size": 0.01,
        "entry_price": None,
        "entry_window": None,
        "stop_loss": 1.08180,
        "take_profit": 1.08900,
    }
    risk = check_enter_risk(decision, state)
    assert risk.allowed is False


def test_mock_mt5_places_pending_order():
    state = build_mock_market_state(["EURUSD"], "t", scenario="trending_bullish")
    client = MockMT5Client(state)
    decision = mock_llm_decision("t", state)
    validated = validate_decision(decision, cycle_id="t")

    result = asyncio.run(
        execute_decision(
            validated,
            client,
            execution_mode=True,
            cycle_id="t",
            market_state=state,
            mock_execution=True,
        )
    )
    assert result["executed"] is True
    assert result["phase"] == 1
    assert len(client.pending_orders) == 1
    assert client.pending_orders[0]["symbol"] == "EURUSD"


def test_emergency_close_all_positions():
    state = build_mock_market_state(["EURUSD"], "t", scenario="open_position")
    client = MockMT5Client(state)
    assert len(client.positions) == 1

    result = asyncio.run(emergency_close_all(client, reason="test"))
    assert result["closed"] == 1
    assert len(client.positions) == 0


def test_run_cycle_phase1_mock_execution(tmp_path):
    reset_scenario_rotation()
    cfg = CycleConfig(
        phase=1,
        execution_mode=True,
        mock_mode=True,
        mock_llm=True,
        prefilter={"enabled": True},
        playbook_path="playbook/algo_trading_skill.md",
        gitea={"repo_path": str(tmp_path), "logs_dir": "logs", "auto_commit": False},
    )

    async def run() -> dict:
        return await run_cycle(cfg)

    summary = asyncio.run(run())
    assert summary["phase"] == 1
    assert summary["execution_mode"] is True
    exec_result = summary.get("execution_result", {})
    assert exec_result.get("phase") == 1


def test_lot_multiplier_after_three_losses():
    assert lot_multiplier_for_losses(2) == 1.0
    assert lot_multiplier_for_losses(3) == 0.5
    decision = {"action": "ENTER", "lot_size": 0.02}
    adjusted = apply_lot_multiplier(decision, 0.5)
    assert adjusted["lot_size"] == 0.01


def test_session_state_persists(tmp_path):
    state = begin_cycle(
        load_session(tmp_path),
        now=__import__("datetime").datetime(2026, 6, 3, 10, 0, tzinfo=__import__("datetime").timezone.utc),
        timezone="Europe/Berlin",
        account={"balance": 10000.0, "equity": 10025.0},
        cycle_id="test-cycle",
    )
    save_session(state, tmp_path)
    loaded = load_session(tmp_path)
    assert loaded.daily_start_balance == 10000.0
    assert loaded.cycles_today == 1


def test_enter_risk_blocks_low_confidence_after_loss_streak():
    state = build_mock_market_state(["EURUSD"], "t", scenario="trending_bullish")
    from cycle.risk import check_enter_risk

    decision = mock_llm_decision("t", state)
    decision["confidence"] = "MEDIUM"
    risk = check_enter_risk(decision, state, consecutive_losses=3)
    assert risk.allowed is False


def test_prefilter_state_detects_macd_cross_on_second_cycle(tmp_path):
    state = build_mock_market_state(["EURUSD"], "t1", scenario="cold_market")
    indicators = state["indicators"]["EURUSD"]
    indicators["H1"]["macd_histogram"] = 0.0002
    save_prefilter_state(
        {"EURUSD": {"H1": {"macd_histogram": -0.0001}, "H4": {"adx": 18.0}}},
        tmp_path,
    )
    enrich_with_previous(state, load_prefilter_state(tmp_path))
    warm, reasons = has_warm_signal("EURUSD", indicators)
    assert warm is True
    assert any("MACD histogram sign flip" in r for r in reasons)


def test_maintenance_cancels_stale_pending_order():
    now = __import__("datetime").datetime(2026, 6, 5, 12, 0, tzinfo=__import__("datetime").timezone.utc)
    stale_ts = int(now.timestamp()) - (50 * 3600)
    state = build_mock_market_state(["EURUSD"], "t", scenario="trending_bullish")
    state["pending_orders"] = [
        {
            "ticket": 9001,
            "symbol": "EURUSD",
            "time_setup": stale_ts,
            "volume_current": 0.01,
        }
    ]
    client = MockMT5Client(state)

    result = asyncio.run(
        run_maintenance(client, state, max_pending_hours=48, now=now)
    )
    assert len(result["cancelled_orders"]) == 1
    assert result["cancelled_orders"][0]["ticket"] == 9001
    assert len(client.pending_orders) == 0


def test_maintenance_closes_aged_position_without_tp():
    now = __import__("datetime").datetime(2026, 6, 5, 12, 0, tzinfo=__import__("datetime").timezone.utc)
    stale_ts = int(now.timestamp()) - (50 * 3600)
    state = build_mock_market_state(["EURUSD"], "t", scenario="open_position")
    state["positions"][0]["time"] = stale_ts
    state["positions"][0]["tp"] = 0
    client = MockMT5Client(state)

    result = asyncio.run(
        run_maintenance(
            client,
            state,
            max_position_hours_without_tp=48,
            now=now,
        )
    )
    assert len(result["closed_positions"]) == 1
    assert len(client.positions) == 0


def test_session_peak_equity_tracks_high_water_mark(tmp_path):
    now = __import__("datetime").datetime(2026, 6, 3, 10, 0, tzinfo=__import__("datetime").timezone.utc)
    state = begin_cycle(
        load_session(tmp_path),
        now=now,
        timezone="Europe/Berlin",
        account={"balance": 10000.0, "equity": 10050.0},
        cycle_id="peak-1",
    )
    state = begin_cycle(
        state,
        now=now,
        timezone="Europe/Berlin",
        account={"balance": 10000.0, "equity": 9980.0},
        cycle_id="peak-2",
    )
    assert state.session_peak_equity == 10050.0


def test_intraday_drawdown_suspend_in_mock_cycle(tmp_path):
    save_session(
        SessionState(
            session_date="2026-06-03",
            daily_start_balance=10000.0,
            session_peak_equity=10000.0,
            last_equity=10000.0,
        ),
        tmp_path,
    )
    state = build_mock_market_state(
        ["EURUSD"], "2026-06-03T10:00:00Z", scenario="intraday_drawdown"
    )
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
    state["positions"] = [
        {
            "ticket": 10001,
            "symbol": "EURUSD",
            "type": 0,
            "volume": 0.01,
            "price_open": 1.08350,
            "sl": 1.08200,
            "tp": 1.08600,
        }
    ]
    client.positions = list(state["positions"])

    async def run() -> dict:
        from cycle.runner import _evaluate_and_log

        return await _evaluate_and_log(
            cfg,
            "2026-06-03T10:00:00Z",
            state,
            client,
            {"errors": []},
            mock_meta=True,
        )

    result = asyncio.run(run())
    assert result["veto"]["suspend"] is True
    assert result["veto"]["emergency_close"] is True
    assert result["decision"]["action"] == "SUSPEND"
    assert result.get("emergency_close", {}).get("closed", 0) == 1


def test_snapshot_indicators_round_trip(tmp_path):
    state = build_mock_market_state(["EURUSD"], "t", scenario="trending_bullish")
    update_prefilter_state(state, tmp_path)
    saved = load_prefilter_state(tmp_path)
    assert "EURUSD" in saved
    assert "rsi" in saved["EURUSD"]["H1"]


def test_phase0_simulation_when_execution_disabled():
    state = build_mock_market_state(["EURUSD"], "t", scenario="trending_bullish")
    decision = mock_llm_decision("t", state)

    async def run() -> dict:
        return await execute_decision(
            decision,
            None,
            execution_mode=False,
            cycle_id="t",
            market_state=state,
        )

    result = asyncio.run(run())
    assert result["simulated"] is True
    assert result["phase"] == 0
