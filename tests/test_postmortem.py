"""Tests for the learning loop (post-mortems → lessons.md → playbook)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.llm import load_playbook
from cycle.postmortem import (
    append_lesson,
    load_decision_logs,
    load_lessons,
    match_deal_to_log,
    review_closed_trades,
)


def _write_log(logs_dir: Path, cycle_id: str, pair: str = "EURUSD") -> Path:
    day_dir = logs_dir / "2026-06-10"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"10-00-00-000_{pair}_ENTER.json"
    payload = {
        "decision": {
            "action": "ENTER",
            "pair": pair,
            "direction": "LONG",
            "cycle_id": cycle_id,
            "reasoning": "test",
        },
        "market_state": {"indicators": {pair: {"H1": {"rsi": 55}}}},
        "meta": {"logged_at": "2026-06-10T10:00:00+00:00"},
    }
    path.write_text(json.dumps(payload))
    return path


def test_append_lesson_caps_and_dedups(tmp_path):
    lessons = tmp_path / "lessons.md"
    for i in range(25):
        append_lesson(
            lessons,
            date="2026-06-10",
            pair="EURUSD",
            verdict="good_trade",
            category="entry_timing",
            lesson=f"Lesson number {i}",
            max_lessons=20,
        )
    content = load_lessons(lessons)
    lines = [l for l in content.splitlines() if l.startswith("- ")]
    assert len(lines) == 20
    assert "Lesson number 24" in lines[-1]
    assert "Lesson number 4" not in content  # oldest five dropped

    # Duplicate is skipped
    append_lesson(
        lessons,
        date="2026-06-11",
        pair="EURUSD",
        verdict="good_trade",
        category="entry_timing",
        lesson="Lesson number 24",
        max_lessons=20,
    )
    lines_after = [l for l in load_lessons(lessons).splitlines() if l.startswith("- ")]
    assert len(lines_after) == 20


def test_match_by_comment_suffix(tmp_path):
    _write_log(tmp_path, "2026-06-10T10:00:00Z")
    logs = load_decision_logs(tmp_path)
    deal = {"symbol": "EURUSD", "comment": "CT -10T10:00:00Z", "profit": 12.5}
    matched = match_deal_to_log(deal, logs)
    assert matched is not None
    assert matched["decision"]["cycle_id"] == "2026-06-10T10:00:00Z"


def test_match_falls_back_to_symbol(tmp_path):
    _write_log(tmp_path, "2026-06-10T10:00:00Z", pair="GBPUSD")
    logs = load_decision_logs(tmp_path)
    deal = {"symbol": "GBPUSD", "comment": "broker-generated", "profit": -8.0}
    matched = match_deal_to_log(deal, logs)
    assert matched is not None
    assert matched["decision"]["pair"] == "GBPUSD"


def test_no_match_for_unknown_symbol(tmp_path):
    _write_log(tmp_path, "2026-06-10T10:00:00Z")
    logs = load_decision_logs(tmp_path)
    assert match_deal_to_log({"symbol": "AUDCAD", "comment": ""}, logs) is None


def test_review_closed_trades_mock_appends_lessons(tmp_path):
    logs_dir = tmp_path / "logs"
    lessons = tmp_path / "lessons.md"
    _write_log(logs_dir, "2026-06-10T10:00:00Z")
    deals = [
        {"ticket": 1, "symbol": "EURUSD", "comment": "CT -10T10:00:00Z", "profit": 15.0},
        {"ticket": 2, "symbol": "EURUSD", "comment": "", "profit": -5.0},
        {"ticket": 3, "symbol": "EURUSD", "comment": "", "profit": None},  # skipped
    ]
    reviews = asyncio.run(review_closed_trades(deals, logs_dir, lessons, mock=True))
    assert len(reviews) == 2
    content = load_lessons(lessons)
    assert "+15.00" in content
    assert "-5.00" in content


def test_load_playbook_appends_lessons(tmp_path):
    playbook = tmp_path / "playbook.md"
    playbook.write_text("# Playbook\n\nRules here.\n")
    lessons = tmp_path / "lessons.md"
    append_lesson(
        lessons,
        date="2026-06-10",
        pair="EURUSD",
        verdict="bad_trade",
        category="entry_timing",
        lesson="Entered during London open buffer despite the veto note",
    )
    combined = load_playbook(playbook, lessons_path=lessons)
    assert combined.startswith("# Playbook")
    assert "London open buffer" in combined

    # Missing lessons file → playbook unchanged
    assert load_playbook(playbook, lessons_path=tmp_path / "absent.md") == playbook.read_text()
