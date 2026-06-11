"""Tests for ops tooling (reconciliation)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "scripts"))
from reconcile import reconcile  # noqa: E402

from cycle.postmortem import load_decision_logs  # noqa: E402


def _write_log(logs_dir: Path, cycle_id: str, pair="EURUSD", executed=True):
    day = logs_dir / "2026-06-10"
    day.mkdir(parents=True, exist_ok=True)
    name = f"10-00-00-{abs(hash(cycle_id)) % 1000:03d}_{pair}_ENTER.json"
    payload = {
        "decision": {"action": "ENTER", "pair": pair, "cycle_id": cycle_id},
        "market_state": {},
        "execution_result": {"executed": executed},
        "meta": {"logged_at": "2026-06-10T10:00:00+00:00"},
    }
    (day / name).write_text(json.dumps(payload))


def test_reconcile_clean(tmp_path):
    _write_log(tmp_path, "2026-06-10T10:00:00Z")
    logs = load_decision_logs(tmp_path)
    deals = [{"ticket": 1, "symbol": "EURUSD", "comment": "CT -10T10:00:00Z", "profit": 5.0}]
    result = reconcile(deals, logs)
    assert result["clean"]
    assert not result["phantom_deals"]
    assert not result["unmatched_logs"]


def test_reconcile_flags_phantom_deal(tmp_path):
    logs = load_decision_logs(tmp_path)  # no logs at all
    deals = [{"ticket": 9, "symbol": "AUDCAD", "comment": "manual", "profit": -20.0}]
    result = reconcile(deals, logs)
    assert not result["clean"]
    assert len(result["phantom_deals"]) == 1


def test_reconcile_flags_unmatched_log(tmp_path):
    _write_log(tmp_path, "2026-06-10T10:00:00Z")
    logs = load_decision_logs(tmp_path)
    result = reconcile([], logs)
    assert not result["clean"]
    assert len(result["unmatched_logs"]) == 1
    assert result["unmatched_logs"][0]["pair"] == "EURUSD"


def test_reconcile_ignores_unexecuted_logs(tmp_path):
    _write_log(tmp_path, "2026-06-10T11:00:00Z", executed=False)
    logs = load_decision_logs(tmp_path)
    result = reconcile([], logs)
    assert result["clean"]
