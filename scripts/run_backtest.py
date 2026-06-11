#!/usr/bin/env python3
"""
Backtest the ClaudeTrader pipeline over historical or synthetic data.

    # Synthetic smoke run (no data files needed)
    python scripts/run_backtest.py --synthetic 4000

    # Real data: CSVs with columns time,open,high,low,close[,volume] (H1 bars)
    python scripts/run_backtest.py --csv EURUSD=data/history/EURUSD_H1.csv

    # Compare playbook rule strategy vs the live LLM (costs API tokens!)
    python scripts/run_backtest.py --csv EURUSD=... --live-llm

Replays the exact production pipeline: indicators → prefilter → veto →
decision → risk guards → simulated execution with conservative fills.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data import generate_synthetic_bars, load_bars_csv  # noqa: E402
from backtest.engine import run_backtest  # noqa: E402
from cycle.config import load_config  # noqa: E402

logger = logging.getLogger(__name__)


def _llm_decide_fn(config):
    """Adapter that routes backtest decisions through the real Claude reasoning layer."""
    from cycle.llm import build_user_prompt, invoke_claude, load_playbook

    playbook = load_playbook(config.playbook_file, lessons_path=config.lessons_file)

    async def decide(market_state, cycle_id, pairs):
        prompt = build_user_prompt(
            cycle_id, pairs, market_state, {"blocked": False, "checks": []}, {}, True,
        )
        return await invoke_claude(
            playbook=playbook,
            user_prompt=prompt,
            model=config.anthropic.get("model", "claude-opus-4-8"),
            max_tokens=int(config.anthropic.get("max_tokens", 8192)),
        )

    return decide


def main() -> int:
    parser = argparse.ArgumentParser(description="ClaudeTrader backtest")
    parser.add_argument("--csv", action="append", default=[],
                        help="PAIR=path.csv (repeatable, H1 bars)")
    parser.add_argument("--synthetic", type=int, default=0,
                        help="Generate N synthetic H1 bars for EURUSD instead of CSVs")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--start-balance", type=float, default=10_000.0)
    parser.add_argument("--cycle-every", type=int, default=4, help="H1 bars per evaluation")
    parser.add_argument("--warmup", type=int, default=1000)
    parser.add_argument("--base-lot", type=float, default=0.01)
    parser.add_argument("--no-manage", action="store_true",
                        help="Disable BE/trailing/partial-close management")
    parser.add_argument("--live-llm", action="store_true",
                        help="Use the real Claude reasoning layer (costs API tokens)")
    parser.add_argument("--config", default="config/cycle.json")
    parser.add_argument("--out", help="Write full result JSON (report + trades + curve)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    bars_by_symbol = {}
    for spec in args.csv:
        pair, _, path = spec.partition("=")
        if not path:
            parser.error(f"--csv expects PAIR=path, got {spec!r}")
        bars_by_symbol[pair.upper()] = load_bars_csv(path)
    if args.synthetic:
        bars_by_symbol.setdefault(
            "EURUSD",
            generate_synthetic_bars(count=args.synthetic, seed=args.seed),
        )
    if not bars_by_symbol:
        parser.error("Provide --csv PAIR=path or --synthetic N")

    config = load_config(args.config)
    decide_fn = _llm_decide_fn(config) if args.live_llm else None

    result = asyncio.run(run_backtest(
        bars_by_symbol,
        start_balance=args.start_balance,
        warmup_bars=args.warmup,
        cycle_every=args.cycle_every,
        decide_fn=decide_fn,
        base_lot_size=args.base_lot,
        risk_config=config.risk,
        prefilter_config=config.prefilter,
        manage_config=config.manage,
        enable_manage=not args.no_manage,
    ))

    report = result["report"]
    print(json.dumps(report, indent=2))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, default=str)
        print(f"\nFull results written to {args.out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
