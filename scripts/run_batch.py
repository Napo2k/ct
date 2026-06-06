#!/usr/bin/env python3
"""Run multiple mock Phase 0 cycles to accumulate logs."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.config import load_config  # noqa: E402
from cycle.mock_data import list_mock_scenarios, reset_scenario_rotation  # noqa: E402
from cycle.mcp_client import run_async  # noqa: E402
from cycle.runner import run_cycle  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run N mock ClaudeTrader cycles")
    parser.add_argument("--count", "-n", type=int, default=10, help="Number of cycles to run")
    parser.add_argument("--config", default="config/cycle.json")
    parser.add_argument("--delay", type=float, default=0.05, help="Seconds between cycles")
    parser.add_argument("--live-llm", action="store_true", help="Use Anthropic API instead of mock_llm")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    config.mock_mode = True
    config.mock_llm = not args.live_llm

    reset_scenario_rotation()
    results: list[dict] = []
    errors = 0

    for i in range(args.count):
        summary = run_async(run_cycle(config))
        action = summary.get("decision", {}).get("action", "?")
        scenario = summary.get("decision", {})  # scenario in log meta, not summary
        log_path = summary.get("log_path", "")
        results.append({
            "cycle": i + 1,
            "action": action,
            "log_path": log_path,
            "skipped_llm": summary.get("skipped_llm"),
            "errors": summary.get("errors", []),
        })
        if summary.get("errors"):
            errors += 1
        if args.delay > 0 and i < args.count - 1:
            time.sleep(args.delay)

    report = {
        "cycles_run": args.count,
        "errors": errors,
        "scenarios": list(list_mock_scenarios()),
        "results": results,
    }
    print(json.dumps(report, indent=2, default=str))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
