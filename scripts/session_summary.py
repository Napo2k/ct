#!/usr/bin/env python3
"""End-of-session P&L and activity report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.session_state import load_session  # noqa: E402


def _log_session_date(payload: dict, path: Path) -> str | None:
    meta = payload.get("meta", {})
    session = meta.get("session", {})
    if isinstance(session, dict) and session.get("session_date"):
        return str(session["session_date"])

    decision = payload.get("decision", {})
    cycle_id = decision.get("cycle_id") or payload.get("cycle_id")
    if isinstance(cycle_id, str) and len(cycle_id) >= 10:
        return cycle_id[:10]

    parent = path.parent.name
    if len(parent) == 10 and parent[4] == "-" and parent[7] == "-":
        return parent
    return None


def build_summary(logs_dir: Path, session_date: str | None = None) -> dict:
    log_files = sorted(logs_dir.rglob("*.json"))
    session = load_session()

    if not session_date:
        session_date = session.session_date or "all"

    actions: dict[str, int] = {}
    executed = 0
    equities: list[float] = []
    balances: list[float] = []
    cycles = 0

    for path in log_files:
        try:
            with path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
        except (json.JSONDecodeError, OSError):
            continue

        if session_date != "all":
            log_date = _log_session_date(payload, path)
            if log_date != session_date and session_date not in str(path):
                continue

        cycles += 1
        decision = payload.get("decision", {})
        action = decision.get("action", "UNKNOWN")
        actions[action] = actions.get(action, 0) + 1

        execution = payload.get("execution_result") or {}
        if execution.get("executed"):
            executed += 1

        account = payload.get("market_state", {}).get("account", {})
        if account.get("equity"):
            equities.append(float(account["equity"]))
        if account.get("balance"):
            balances.append(float(account["balance"]))

    start_balance = session.daily_start_balance or (balances[0] if balances else 0)
    end_equity = equities[-1] if equities else session.last_equity
    pnl = end_equity - start_balance if start_balance and end_equity else 0
    pnl_pct = (pnl / start_balance * 100) if start_balance else 0

    return {
        "session_date": session_date,
        "cycles_logged": cycles,
        "actions": actions,
        "executions": executed,
        "daily_start_balance": start_balance,
        "end_equity": end_equity,
        "session_pnl": round(pnl, 2),
        "session_pnl_pct": round(pnl_pct, 2),
        "consecutive_losses": session.consecutive_losses,
        "lot_multiplier": session.lot_multiplier,
        "report": (
            f"Session {session_date}: {cycles} cycles, P&L {pnl:+.2f} ({pnl_pct:+.2f}%), "
            f"{executed} executions, loss streak {session.consecutive_losses}"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="ClaudeTrader session summary")
    parser.add_argument("--logs-dir", default="logs")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today from session)")
    args = parser.parse_args()

    logs_dir = Path(args.logs_dir)
    if not logs_dir.is_absolute():
        logs_dir = ROOT / logs_dir

    summary = build_summary(logs_dir, session_date=args.date)
    print(json.dumps(summary, indent=2))
    print()
    print(summary["report"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
