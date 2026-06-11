#!/usr/bin/env python3
"""
Reconcile broker history against decision logs.

    python scripts/reconcile.py --days 7
    python scripts/reconcile.py --deals-file deals.json   # offline

Flags two failure classes that audits of logs alone can't see:
- PHANTOM: a broker deal with no matching executed-ENTER decision log
  (something traded that the system didn't decide).
- UNFILLED/UNLOGGED: an executed ENTER log with no broker deal
  (an order the system believes it placed but the broker never saw,
  or a pending order that expired — listed for review either way).

Also records all closed deals into the SQLite store for the dashboard.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.config import load_config  # noqa: E402
from cycle.mcp_client import mcp_session  # noqa: E402
from cycle.postmortem import load_decision_logs, match_deal_to_log  # noqa: E402
from cycle.store import db_path_for, record_trades  # noqa: E402

logger = logging.getLogger(__name__)


def reconcile(deals: list[dict], logs: list[dict]) -> dict:
    """Match deals ↔ executed ENTER logs; return discrepancies."""
    executed_logs = [
        log for log in logs
        if (log.get("execution_result") or {}).get("executed")
    ]

    phantom_deals = []
    matched_log_paths = set()
    for deal in deals:
        log = match_deal_to_log(deal, executed_logs)
        comment = str(deal.get("comment", ""))
        if log is None and not comment.startswith("CT "):
            # Not ours by comment AND unmatchable — manual trade or other system
            phantom_deals.append(deal)
        elif log is None:
            phantom_deals.append(deal)
        else:
            matched_log_paths.add(log.get("_path"))

    unmatched_logs = [
        {
            "path": log.get("_path"),
            "pair": (log.get("decision") or {}).get("pair"),
            "cycle_id": (log.get("decision") or {}).get("cycle_id"),
        }
        for log in executed_logs
        if log.get("_path") not in matched_log_paths
    ]

    return {
        "deals": len(deals),
        "executed_enter_logs": len(executed_logs),
        "phantom_deals": phantom_deals,
        "unmatched_logs": unmatched_logs,
        "clean": not phantom_deals and not unmatched_logs,
    }


async def fetch_deals(config, days: float) -> list[dict]:
    to_date = datetime.now(timezone.utc)
    from_date = to_date - timedelta(days=days)
    async with mcp_session("mt5", config.mt5_mcp) as mt5:
        result = await mt5.call_tool("get_history", {
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
        })
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError(f"get_history failed: {result}")
    return result.get("deals", [])


def main() -> int:
    parser = argparse.ArgumentParser(description="ClaudeTrader broker reconciliation")
    parser.add_argument("--config", default="config/cycle.json")
    parser.add_argument("--days", type=float, default=7.0)
    parser.add_argument("--deals-file", help="JSON deals file (offline mode)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    config = load_config(args.config)

    if args.deals_file:
        with open(args.deals_file, encoding="utf-8") as handle:
            deals = json.load(handle)
    else:
        deals = asyncio.run(fetch_deals(config, args.days))

    closing_deals = [d for d in deals if d.get("profit") not in (None, 0, 0.0)]
    record_trades(db_path_for(config.session_state_dir), closing_deals)

    logs = load_decision_logs(config.logs_dir)
    result = reconcile(closing_deals, logs)

    print(json.dumps(result, indent=2, default=str))
    if not result["clean"]:
        print("\nDISCREPANCIES FOUND — investigate before next session.", file=sys.stderr)
        return 1
    print("\nReconciliation clean.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
