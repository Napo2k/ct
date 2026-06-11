"""Phase 2 (live trading) readiness tests — config gating, safety, risk sizing."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.config import (
    LIVE_CONFIRMATION_PHRASE,
    ConfigError,
    CycleConfig,
    load_config,
    validate_config,
)
from cycle.risk import check_enter_risk
from cycle.safety import (
    engage_kill_switch,
    heartbeat_age_seconds,
    kill_switch_engaged,
    kill_switch_reason,
    stale_ticks,
    write_heartbeat,
)
from cycle.session_state import SessionState, end_cycle, save_session, load_session
from cycle.veto import check_vetoes

TUESDAY_10AM = datetime(2026, 6, 9, 8, 0, tzinfo=timezone.utc)  # 10:00 CEST


def _write_config(tmp_path: Path, overrides: dict) -> Path:
    base = {
        "phase": 1,
        "execution_mode": True,
        "mock_mode": True,
        "mock_llm": True,
        "pairs": ["EURUSD"],
        "spread_limits_pips": {"EURUSD": 2.0},
    }
    base.update(overrides)
    path = tmp_path / "cycle.json"
    path.write_text(json.dumps(base))
    return path


# ---------------------------------------------------------------------------
# Config: live-mode gating
# ---------------------------------------------------------------------------

def test_phase2_without_confirmation_downgrades_to_phase1(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "true")
    path = _write_config(tmp_path, {"phase": 2, "mock_mode": False, "mock_llm": False})
    cfg = load_config(path)
    assert cfg.phase == 1
    assert cfg.live_mode is False


def test_phase2_without_env_var_downgrades(tmp_path, monkeypatch):
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    path = _write_config(tmp_path, {
        "phase": 2,
        "mock_mode": False,
        "mock_llm": False,
        "live_confirmation": LIVE_CONFIRMATION_PHRASE,
    })
    cfg = load_config(path)
    assert cfg.phase == 1
    assert cfg.live_mode is False


def test_phase2_full_opt_in_enables_live_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "true")
    path = _write_config(tmp_path, {
        "phase": 2,
        "mock_mode": False,
        "mock_llm": False,
        "live_confirmation": LIVE_CONFIRMATION_PHRASE,
    })
    cfg = load_config(path)
    assert cfg.phase == 2
    assert cfg.live_mode is True


def test_phase2_with_mock_llm_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "true")
    path = _write_config(tmp_path, {
        "phase": 2,
        "mock_mode": False,
        "mock_llm": True,
        "live_confirmation": LIVE_CONFIRMATION_PHRASE,
    })
    with pytest.raises(ConfigError):
        load_config(path)


def test_phase2_with_mock_mode_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "true")
    path = _write_config(tmp_path, {
        "phase": 2,
        "mock_mode": True,
        "mock_llm": False,
        "live_confirmation": LIVE_CONFIRMATION_PHRASE,
    })
    with pytest.raises(ConfigError):
        load_config(path)


# ---------------------------------------------------------------------------
# Config: validation bounds
# ---------------------------------------------------------------------------

def test_validate_config_rejects_insane_lot_size():
    cfg = CycleConfig(base_lot_size=5.0)
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_validate_config_rejects_drawdown_inversion():
    cfg = CycleConfig(max_intraday_drawdown_pct=3.0, max_daily_drawdown_pct=2.0)
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_validate_config_live_requires_spread_limits():
    cfg = CycleConfig(live_mode=True, pairs=["EURUSD"], spread_limits_pips={})
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_validate_config_accepts_defaults():
    validate_config(CycleConfig(spread_limits_pips={"EURUSD": 2.0}))


# ---------------------------------------------------------------------------
# Safety: kill switch + heartbeat + stale ticks
# ---------------------------------------------------------------------------

def test_kill_switch_roundtrip(tmp_path):
    path = tmp_path / "KILL_SWITCH"
    assert not kill_switch_engaged(path)
    engage_kill_switch(path, "manual halt for broker incident")
    assert kill_switch_engaged(path)
    assert "manual halt" in kill_switch_reason(path)


def test_heartbeat_age(tmp_path):
    path = tmp_path / "heartbeat.json"
    assert heartbeat_age_seconds(path) is None
    write_heartbeat(path, {"cycle_id": "test"})
    age = heartbeat_age_seconds(path)
    assert age is not None and age < 5


def test_stale_ticks_flags_old_and_unknown():
    now = datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc)
    fresh = int(now.timestamp()) - 30
    old = int(now.timestamp()) - 600
    ticks = {
        "EURUSD": {"tick": {"time": fresh}},
        "GBPUSD": {"tick": {"time": old}},
        "USDJPY": {"tick": {"bid": 155.0}},  # no timestamp
    }
    result = stale_ticks(ticks, now=now, max_age_seconds=120)
    assert "EURUSD" not in result
    assert result["GBPUSD"] > 500
    assert result["USDJPY"] == -1.0


def test_kill_switch_suspends_cycle(tmp_path):
    from cycle.runner import run_cycle

    cfg = CycleConfig(
        phase=1,
        execution_mode=True,
        mock_mode=True,
        mock_llm=True,
        pairs=["EURUSD"],
        spread_limits_pips={"EURUSD": 2.0},
        session_state_dir=str(tmp_path),
        gitea={"auto_commit": False, "repo_path": str(tmp_path), "logs_dir": "logs"},
    )
    engage_kill_switch(cfg.kill_switch_path, "test halt")
    summary = asyncio.run(run_cycle(cfg))
    assert summary["kill_switch"] is True
    assert summary["decision"]["action"] == "SUSPEND"
    assert summary["skipped_llm"] is True


# ---------------------------------------------------------------------------
# Veto: fail-closed behavior in live mode
# ---------------------------------------------------------------------------

def test_live_mode_blocks_on_missing_news_feed():
    result = check_vetoes(
        TUESDAY_10AM,
        live_mode=True,
        news_events=None,
        news_feed_available=False,
    )
    assert result.blocked
    assert any("News feed" in c["name"] and not c["pass"] for c in result.checks)


def test_live_mode_blocks_on_missing_tick():
    result = check_vetoes(
        TUESDAY_10AM,
        live_mode=True,
        pairs=["EURUSD", "GBPUSD"],
        ticks={"EURUSD": {"tick": {"bid": 1.08, "ask": 1.0801}}},
        spread_limits_pips={"EURUSD": 2.0, "GBPUSD": 3.0},
    )
    assert any("Tick data" in c["name"] and not c["pass"] for c in result.checks)


def test_live_mode_blocks_on_unreadable_spread():
    result = check_vetoes(
        TUESDAY_10AM,
        live_mode=True,
        ticks={"EURUSD": {"tick": {}}},
        spread_limits_pips={"EURUSD": 2.0},
    )
    assert any("Spread EURUSD" in c["name"] and not c["pass"] for c in result.checks)


def test_paper_mode_does_not_fail_closed():
    result = check_vetoes(
        TUESDAY_10AM,
        live_mode=False,
        ticks={"EURUSD": {"tick": {}}},
        spread_limits_pips={"EURUSD": 2.0},
        news_feed_available=False,
    )
    assert not result.blocked


def test_stale_tick_ages_block():
    result = check_vetoes(TUESDAY_10AM, stale_tick_ages={"EURUSD": 400.0})
    assert result.blocked
    assert any("Fresh tick EURUSD" in c["name"] for c in result.checks)


# ---------------------------------------------------------------------------
# Risk: equity-based sizing, lot cap, daily trade limit
# ---------------------------------------------------------------------------

def _enter_decision(lot=0.01, entry=1.0850, sl=1.0820, tp=1.0925):
    return {
        "action": "ENTER",
        "pair": "EURUSD",
        "direction": "LONG",
        "order_type": "BUY_LIMIT",
        "lot_size": lot,
        "entry_price": entry,
        "entry_window": None,
        "stop_loss": sl,
        "take_profit": tp,
        "reasoning": "x" * 60,
        "confidence": "HIGH",
    }


def _state(equity=10_000.0):
    return {
        "account": {"equity": equity, "balance": equity, "margin": 0, "free_margin": equity},
        "positions": [],
        "pending_orders": [],
        "ticks": {},
    }


def test_risk_allows_within_equity_budget():
    # 0.01 lot, 30 pip SL → ~$3 risk on $10k equity at 1% budget ($100)
    result = check_enter_risk(
        _enter_decision(),
        _state(),
        risk_config={"risk_per_trade_pct": 1.0, "max_lot_size": 0.05},
    )
    assert result.allowed


def test_risk_blocks_oversized_trade():
    # 0.02 lot, 500 pip SL → $100 risk > 0.5% of $10k ($50)
    decision = _enter_decision(lot=0.02, entry=1.0850, sl=1.0350, tp=1.16)
    result = check_enter_risk(
        decision,
        _state(),
        base_lot_size=0.01,
        risk_config={"risk_per_trade_pct": 0.5, "max_lot_size": 0.05},
    )
    assert not result.allowed
    assert any("Trade risk" in c["name"] and not c["pass"] for c in result.checks)


def test_risk_blocks_absolute_lot_cap():
    decision = _enter_decision(lot=0.10)
    result = check_enter_risk(
        decision,
        _state(),
        base_lot_size=0.05,
        risk_config={"max_lot_size": 0.05},
    )
    assert not result.allowed
    assert any("Absolute lot cap" in c["name"] for c in result.checks)


def test_risk_blocks_max_trades_per_day():
    result = check_enter_risk(
        _enter_decision(),
        _state(),
        risk_config={"max_trades_per_day": 5},
        trades_today=5,
    )
    assert not result.allowed
    assert any("Max trades per day" in c["name"] for c in result.checks)


def test_risk_blocks_unknown_equity():
    state = _state()
    state["account"] = {"margin": 0, "free_margin": 0}
    result = check_enter_risk(_enter_decision(), state)
    assert not result.allowed
    assert any("Equity known" in c["name"] for c in result.checks)


def test_risk_jpy_pair_conversion():
    # USDJPY: 0.01 lot, 50 pip SL (0.50 JPY) → 500 JPY ≈ $3.2 at 155 — allowed
    decision = _enter_decision(lot=0.01, entry=155.00, sl=154.50, tp=156.00)
    decision["pair"] = "USDJPY"
    result = check_enter_risk(
        decision,
        _state(),
        risk_config={"risk_per_trade_pct": 1.0, "max_lot_size": 0.05},
    )
    assert result.allowed


# ---------------------------------------------------------------------------
# Session state: trade counting and atomic persistence
# ---------------------------------------------------------------------------

def test_end_cycle_counts_executed_entries():
    state = SessionState(session_date="2026-06-09", last_equity=10_000, daily_start_balance=10_000)
    decision = {"action": "ENTER"}
    state = end_cycle(
        state,
        account={"equity": 10_000},
        decision=decision,
        execution_result={"executed": True},
    )
    assert state.trades_today == 1

    state = end_cycle(
        state,
        account={"equity": 10_000},
        decision=decision,
        execution_result={"executed": False},
    )
    assert state.trades_today == 1


def test_realized_pnl_tracked_on_exit():
    state = SessionState(session_date="2026-06-09", last_equity=10_000, daily_start_balance=10_000)
    state = end_cycle(
        state,
        account={"equity": 9_950},
        decision={"action": "EXIT"},
        execution_result={"executed": True},
    )
    assert state.realized_pnl_today == pytest.approx(-50)
    assert state.consecutive_losses == 1


def test_session_roundtrip_with_new_fields(tmp_path):
    state = SessionState(session_date="2026-06-09", trades_today=3, realized_pnl_today=-12.5)
    save_session(state, tmp_path)
    loaded = load_session(tmp_path)
    assert loaded.trades_today == 3
    assert loaded.realized_pnl_today == -12.5


# ---------------------------------------------------------------------------
# Runner: live confidence gate
# ---------------------------------------------------------------------------

def test_live_confidence_gate_downgrades_medium_enter(tmp_path, monkeypatch):
    import cycle.runner as runner_mod

    def medium_enter(cycle_id, market_state):
        return {
            "action": "ENTER",
            "pair": "EURUSD",
            "direction": "LONG",
            "order_type": "BUY_LIMIT",
            "lot_size": 0.01,
            "entry_price": 1.0850,
            "entry_window": None,
            "stop_loss": 1.0820,
            "take_profit": 1.0925,
            "reasoning": (
                "1. VETO CHECK: pass\n2. REGIME CLASSIFICATION: trending\n"
                "3. SIGNAL EVALUATION: 4/5\n4. RISK CALCULATION: ok\n5. DECISION: ENTER"
            ),
            "confidence": "MEDIUM",
            "cycle_id": cycle_id,
        }

    monkeypatch.setattr(runner_mod, "mock_llm_decision", medium_enter)

    cfg = CycleConfig(
        phase=2,
        execution_mode=True,
        mock_mode=True,  # dataclass constructed directly — bypasses load_config gating
        mock_llm=True,
        live_mode=True,
        pairs=["EURUSD"],
        spread_limits_pips={"EURUSD": 2.0},
        session_state_dir=str(tmp_path),
        gitea={"auto_commit": False, "repo_path": str(tmp_path), "logs_dir": "logs"},
    )
    summary = asyncio.run(runner_mod.run_cycle(cfg))
    decision = summary["decision"]
    assert decision["action"] == "HOLD"
    assert "Live mode requires HIGH confidence" in decision["reasoning"]


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def test_send_alert_noop_without_webhook():
    from cycle.alerts import send_alert

    assert asyncio.run(send_alert(None, severity="CRITICAL", event="x", detail="y")) is False
    assert asyncio.run(send_alert({}, severity="CRITICAL", event="x", detail="y")) is False


def test_send_alert_respects_min_severity(monkeypatch):
    import cycle.alerts as alerts_mod

    posted = []
    monkeypatch.setattr(alerts_mod, "_post_json", lambda url, body, timeout: posted.append(body))

    cfg = {"webhook_url": "http://localhost:9/hook", "min_severity": "CRITICAL"}
    assert asyncio.run(alerts_mod.send_alert(cfg, severity="WARNING", event="w", detail="d")) is False
    assert asyncio.run(alerts_mod.send_alert(cfg, severity="CRITICAL", event="c", detail="d")) is True
    assert len(posted) == 1
    assert posted[0]["event"] == "c"


def test_alert_webhook_failure_never_raises(monkeypatch):
    import cycle.alerts as alerts_mod

    def boom(url, body, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(alerts_mod, "_post_json", boom)
    cfg = {"webhook_url": "http://localhost:9/hook"}
    assert asyncio.run(alerts_mod.send_alert(cfg, severity="CRITICAL", event="x", detail="y")) is False


# ---------------------------------------------------------------------------
# Portfolio risk: correlation + net currency exposure
# ---------------------------------------------------------------------------

def _open_position(symbol="GBPUSD", direction=0, volume=0.01):
    return {"symbol": symbol, "type": direction, "volume": volume, "ticket": 111}


def test_correlated_same_direction_blocked():
    decision = _enter_decision()  # EURUSD LONG
    state = _state()
    state["positions"] = [_open_position("GBPUSD", direction=0)]  # GBPUSD long
    result = check_enter_risk(decision, state)
    assert not result.allowed
    assert any("Correlated exposure" in c["name"] and not c["pass"] for c in result.checks)


def test_correlated_opposite_direction_allowed():
    decision = _enter_decision()  # EURUSD LONG
    state = _state()
    state["positions"] = [_open_position("GBPUSD", direction=1)]  # GBPUSD short
    result = check_enter_risk(decision, state)
    assert not any("Correlated exposure" in c["name"] and not c["pass"] for c in result.checks)


def test_negative_correlation_opposite_direction_blocked():
    # EURUSD long + USDJPY short are both short-USD: corr -0.30 * (+1)(-1) = +0.30,
    # below default 0.7 threshold — so use an override to verify the mechanics.
    decision = _enter_decision()
    state = _state()
    state["positions"] = [_open_position("USDJPY", direction=1)]  # USDJPY short
    result = check_enter_risk(
        decision,
        state,
        risk_config={"correlations": {"EURUSD/USDJPY": -0.9}},
    )
    assert not result.allowed
    assert any("Correlated exposure" in c["name"] and not c["pass"] for c in result.checks)


def test_net_currency_exposure_blocked():
    # Two open short-USD positions + a third short-USD entry breaches a 0.02 cap.
    decision = _enter_decision()  # EURUSD LONG → -0.01 USD
    state = _state()
    state["positions"] = [
        _open_position("USDJPY", direction=1, volume=0.01),  # short USDJPY → -0.01 USD
        _open_position("USDCHF", direction=1, volume=0.01),  # short USDCHF → -0.01 USD
    ]
    result = check_enter_risk(
        decision,
        state,
        risk_config={"max_net_currency_lots": 0.02},
    )
    assert not result.allowed
    assert any("Net currency exposure" in c["name"] and not c["pass"] for c in result.checks)


def test_net_currency_exposure_allows_balanced_book():
    decision = _enter_decision()
    state = _state()
    state["positions"] = [_open_position("USDJPY", direction=0, volume=0.01)]  # long USD
    result = check_enter_risk(
        decision,
        state,
        risk_config={"max_net_currency_lots": 0.02},
    )
    assert not any("Net currency exposure" in c["name"] and not c["pass"] for c in result.checks)
