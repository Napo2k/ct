"""Tests for the SQLite cycle/trade store."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.store import db_path_for, record_cycle, record_trades, recent_cycles, trade_stats


def _summary(cycle_id="2026-06-11T10:00:00Z", action="HOLD", executed=False):
    return {
        "cycle_id": cycle_id,
        "phase": 1,
        "live_mode": False,
        "skipped_llm": False,
        "errors": [],
        "decision": {"action": action, "pair": "EURUSD", "confidence": "LOW"},
        "execution_result": {"executed": executed},
        "session": {"last_equity": 10_000.0},
    }


def test_record_and_query_cycles(tmp_path):
    db = db_path_for(tmp_path)
    assert record_cycle(db, _summary())
    assert record_cycle(db, _summary(action="ENTER", executed=True))
    rows = recent_cycles(db, limit=10)
    assert len(rows) == 2
    assert rows[0]["action"] == "ENTER"  # newest first
    assert rows[0]["executed"] == 1
    assert rows[1]["equity"] == 10_000.0


def test_record_trades_dedups(tmp_path):
    db = db_path_for(tmp_path)
    deals = [
        {"ticket": 1, "symbol": "EURUSD", "profit": 12.0, "volume": 0.01, "time": "1718000000"},
        {"ticket": 2, "symbol": "GBPUSD", "profit": -7.0, "volume": 0.01, "time": "1718001000"},
    ]
    assert record_trades(db, deals) == 2
    assert record_trades(db, deals) == 0  # duplicates ignored

    stats = trade_stats(db)
    assert stats["trades"] == 2
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["total_profit"] == 5.0
    assert stats["win_rate"] == 0.5


def test_store_failure_is_silent(tmp_path):
    bogus = tmp_path / "not_a_dir" / "x.db"
    # parent doesn't exist → sqlite error → False/0, never raises
    assert record_cycle(bogus, _summary()) is False
    assert record_trades(bogus, [{"ticket": 1}]) == 0
    assert recent_cycles(bogus) == []
    assert trade_stats(bogus)["trades"] == 0
