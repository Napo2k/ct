#!/usr/bin/env python3
"""Audit Phase 0 cycle logs against success criteria."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.decision import DecisionValidationError, validate_decision  # noqa: E402
from cycle.regime import classify_regime  # noqa: E402


def _check_regime(payload: dict) -> str | None:
    """Re-classify regime from market_state; return error if mismatch."""
    market_state = payload.get("market_state", {})
    meta = payload.get("meta", {})
    logged_regime = (meta.get("regime") or {}).get("regime")
    if not logged_regime:
        return None

    eurusd = market_state.get("indicators", {}).get("EURUSD", {})
    h4 = eurusd.get("H4", {})
    if not h4:
        return None

    recomputed = classify_regime(
        {
            "adx": h4.get("adx"),
            "ema50": h4.get("ema50"),
            "ema200": h4.get("ema200"),
            "price": h4.get("price"),
        }
    )
    if recomputed["regime"] != logged_regime:
        return f"regime mismatch: logged={logged_regime}, recomputed={recomputed['regime']}"
    return None


def audit_logs(logs_dir: Path, *, min_cycles: int = 50) -> dict:
    log_files = sorted(logs_dir.rglob("*.json"))
    stats: dict = {
        "total_logs": len(log_files),
        "mock_logs": 0,
        "live_logs": 0,
        "valid_json": 0,
        "invalid_json": 0,
        "enter_decisions": 0,
        "hold_decisions": 0,
        "suspend_decisions": 0,
        "modify_decisions": 0,
        "exit_decisions": 0,
        "rr_pass": 0,
        "rr_fail": 0,
        "veto_blocked_enters": 0,
        "phantom_trades": 0,
        "regime_mismatches": 0,
        "regime_correct": 0,
        "scenarios": {},
        "action_distribution": {},
        "errors": [],
    }

    for path in log_files:
        try:
            with path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            stats["invalid_json"] += 1
            stats["errors"].append(f"{path.name}: parse error — {exc}")
            continue

        stats["valid_json"] += 1
        meta = payload.get("meta", {})
        if meta.get("mock_mode"):
            stats["mock_logs"] += 1
        else:
            stats["live_logs"] += 1

        scenario = meta.get("mock_scenario", "live")
        stats["scenarios"][scenario] = stats["scenarios"].get(scenario, 0) + 1

        decision = payload.get("decision", {})
        action = decision.get("action", "UNKNOWN")
        stats["action_distribution"][action] = stats["action_distribution"].get(action, 0) + 1

        if action == "ENTER":
            stats["enter_decisions"] += 1
        elif action == "HOLD":
            stats["hold_decisions"] += 1
        elif action == "SUSPEND":
            stats["suspend_decisions"] += 1
        elif action == "MODIFY":
            stats["modify_decisions"] += 1
        elif action == "EXIT":
            stats["exit_decisions"] += 1

        cycle_id = decision.get("cycle_id", "unknown")
        try:
            validate_decision(decision, cycle_id=cycle_id)
        except DecisionValidationError as exc:
            stats["errors"].append(f"{path.name}: schema — {exc}")

        regime_err = _check_regime(payload)
        if regime_err:
            stats["regime_mismatches"] += 1
            stats["errors"].append(f"{path.name}: {regime_err}")
        elif meta.get("regime"):
            stats["regime_correct"] += 1

        veto = meta.get("veto") or payload.get("market_state", {}).get("veto")
        if action == "ENTER" and veto and veto.get("blocked"):
            stats["veto_blocked_enters"] += 1
            stats["errors"].append(f"{path.name}: ENTER despite veto block")

        execution = payload.get("execution_result") or {}
        meta_phase = meta.get("phase", 0)
        is_mock_exec = execution.get("mock_execution") or meta.get("mock_mode")
        if (
            execution.get("executed")
            and not execution.get("simulated")
            and not is_mock_exec
            and meta_phase < 1
        ):
            stats["phantom_trades"] += 1
            stats["errors"].append(f"{path.name}: phantom trade — executed in Phase 0")

        if action == "ENTER":
            entry = decision.get("entry_price")
            sl = decision.get("stop_loss")
            tp = decision.get("take_profit")
            direction = decision.get("direction")
            if entry and sl and tp and direction:
                if direction == "LONG":
                    risk, reward = entry - sl, tp - entry
                else:
                    risk, reward = sl - entry, entry - tp
                if risk > 0 and reward / risk >= 1.5:
                    stats["rr_pass"] += 1
                else:
                    stats["rr_fail"] += 1
                    stats["errors"].append(f"{path.name}: R:R below 1.5")

    regime_total = stats["regime_correct"] + stats["regime_mismatches"]
    stats["regime_accuracy_pct"] = (
        round(100.0 * stats["regime_correct"] / regime_total, 1) if regime_total else None
    )
    stats["meets_min_cycles"] = stats["total_logs"] >= min_cycles
    stats["phase0_ready"] = (
        stats["meets_min_cycles"]
        and stats["invalid_json"] == 0
        and stats["veto_blocked_enters"] == 0
        and stats["phantom_trades"] == 0
        and len(stats["errors"]) == 0
        and (stats["rr_fail"] == 0 or stats["enter_decisions"] == 0)
        and (stats["regime_accuracy_pct"] is None or stats["regime_accuracy_pct"] >= 95.0)
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Phase 0 cycle logs")
    parser.add_argument("--logs-dir", default="logs", help="Logs directory to scan")
    parser.add_argument("--min-cycles", type=int, default=50)
    args = parser.parse_args()

    logs_dir = Path(args.logs_dir)
    if not logs_dir.is_absolute():
        logs_dir = ROOT / logs_dir

    stats = audit_logs(logs_dir, min_cycles=args.min_cycles)
    print(json.dumps(stats, indent=2))

    return 0 if stats["phase0_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
