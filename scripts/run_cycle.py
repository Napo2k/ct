#!/usr/bin/env python3
"""ClaudeTrader cycle entry point."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.config import load_config  # noqa: E402
from cycle.mcp_client import run_async  # noqa: E402
from cycle.runner import run_cycle  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one ClaudeTrader evaluation cycle")
    parser.add_argument(
        "--config",
        default="config/cycle.json",
        help="Path to cycle config JSON",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load config and print settings without running cycle",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)

    if args.dry_run:
        print(json.dumps({
            "execution_mode": config.execution_mode,
            "pairs": config.pairs,
            "playbook": str(config.playbook_file),
            "logs_dir": str(config.logs_dir),
        }, indent=2))
        return 0

    summary = run_async(run_cycle(config))
    print(json.dumps(summary, indent=2, default=str))

    if summary.get("errors"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
