#!/usr/bin/env python3
"""
Trade post-mortem review — turn closed trades into playbook lessons.

    python scripts/review_trades.py --days 1            # live broker history
    python scripts/review_trades.py --deals-file f.json # offline (no MT5)
    python scripts/review_trades.py --days 7 --mock     # no Anthropic API calls

Fetches closed deals from MT5 history, matches them to the decision logs that
opened them, asks Claude for a post-mortem of each, and appends distilled
lessons to playbook/lessons.md (which feeds back into the system prompt).
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
from cycle.postmortem import review_closed_trades  # noqa: E402

logger = logging.getLogger(__name__)


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
    deals = result.get("deals", [])
    # Only closing deals carry realized P&L worth reviewing.
    return [d for d in deals if d.get("profit") not in (None, 0, 0.0)]


def main() -> int:
    parser = argparse.ArgumentParser(description="ClaudeTrader trade post-mortems")
    parser.add_argument("--config", default="config/cycle.json")
    parser.add_argument("--days", type=float, default=1.0, help="History window in days")
    parser.add_argument("--deals-file", help="JSON file with deals (offline mode, skips MT5)")
    parser.add_argument("--mock", action="store_true", help="Deterministic lessons, no API calls")
    parser.add_argument("--max-lessons", type=int, default=20)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    config = load_config(args.config)

    if args.deals_file:
        with open(args.deals_file, encoding="utf-8") as handle:
            deals = json.load(handle)
    else:
        deals = asyncio.run(fetch_deals(config, args.days))

    if not deals:
        print("No closed trades to review.")
        return 0

    reviews = asyncio.run(review_closed_trades(
        deals,
        config.logs_dir,
        config.lessons_file,
        mock=args.mock,
        model=config.anthropic.get("model", "claude-opus-4-8"),
        max_lessons=args.max_lessons,
    ))

    print(f"Reviewed {len(reviews)} closed trade(s):")
    for review in reviews:
        print(f"  {review['symbol']} {review['profit']:+.2f} [{review['verdict']}] {review['lesson']}")
    print(f"Lessons file: {config.lessons_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
