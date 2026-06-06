"""Operational tooling tests — session summary filtering, HTTP handler helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.session_summary import _log_session_date, build_summary  # noqa: E402


def test_log_session_date_from_meta_session(tmp_path):
    payload = {
        "meta": {"session": {"session_date": "2026-06-03"}},
        "decision": {"cycle_id": "2026-06-05T10:00:00Z"},
    }
    path = tmp_path / "2026-06-05" / "log.json"
    path.parent.mkdir(parents=True)
    assert _log_session_date(payload, path) == "2026-06-03"


def test_build_summary_filters_by_meta_session_date(tmp_path):
    logs_dir = tmp_path / "logs"
    day_a = logs_dir / "2026-06-03"
    day_b = logs_dir / "2026-06-05"
    day_a.mkdir(parents=True)
    day_b.mkdir(parents=True)

    for folder, session_date in ((day_a, "2026-06-03"), (day_b, "2026-06-05")):
        payload = {
            "decision": {"action": "HOLD", "cycle_id": f"{session_date}T10:00:00Z"},
            "meta": {"session": {"session_date": session_date}},
            "market_state": {"account": {"equity": 10000.0, "balance": 10000.0}},
        }
        with (folder / "cycle.json").open("w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    summary = build_summary(logs_dir, session_date="2026-06-03")
    assert summary["cycles_logged"] == 1
