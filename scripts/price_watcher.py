#!/usr/bin/env python3
"""
Event-driven cycle trigger — fires an evaluation when price moves sharply.

    python scripts/price_watcher.py
    python scripts/price_watcher.py --threshold-pips EURUSD=8 --poll 20

The 15-minute cron leaves up to 15 minutes of blindness during violent moves.
This watcher polls ticks via the MT5 MCP gateway and POSTs to the local
http_trigger /cycle endpoint whenever price moves more than the per-pair
threshold since the last trigger (reference resets on every trigger and on
every regular interval so cron cycles and event cycles stay coherent).

Requires scripts/http_trigger.py running. Stops cleanly on Ctrl-C.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.config import load_config  # noqa: E402
from cycle.mcp_client import mcp_session  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_PIPS = 10.0
DEFAULT_POLL_SECONDS = 20.0
REFERENCE_RESET_SECONDS = 900  # re-anchor every 15 min to match the cron cadence


def _pip(symbol: str) -> float:
    return 0.01 if symbol.endswith("JPY") else 0.0001


def _trigger_cycle(base_url: str, timeout: float = 120.0) -> dict:
    request = urllib.request.Request(f"{base_url}/cycle", method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


async def watch(config, thresholds: dict[str, float], poll_seconds: float, base_url: str) -> None:
    references: dict[str, tuple[float, float]] = {}  # pair -> (price, anchored_at)

    async with mcp_session("mt5", config.mt5_mcp) as mt5:
        logger.info("Watching %s (thresholds: %s)", config.pairs, thresholds)
        while True:
            now = asyncio.get_event_loop().time()
            for pair in config.pairs:
                try:
                    tick = await mt5.call_tool("get_tick", {"symbol": pair})
                except Exception as exc:  # noqa: BLE001 — keep watching through blips
                    logger.warning("get_tick(%s) failed: %s", pair, exc)
                    continue
                if not isinstance(tick, dict) or not tick.get("success"):
                    continue
                data = tick.get("tick", tick)
                bid = data.get("bid")
                if bid is None:
                    continue
                bid = float(bid)

                ref = references.get(pair)
                if ref is None or now - ref[1] > REFERENCE_RESET_SECONDS:
                    references[pair] = (bid, now)
                    continue

                moved_pips = abs(bid - ref[0]) / _pip(pair)
                threshold = thresholds.get(pair, DEFAULT_THRESHOLD_PIPS)
                if moved_pips >= threshold:
                    logger.warning(
                        "%s moved %.1f pips (>= %.1f) — triggering cycle",
                        pair, moved_pips, threshold,
                    )
                    try:
                        summary = await asyncio.to_thread(_trigger_cycle, base_url)
                        logger.info(
                            "Triggered cycle: %s",
                            (summary.get("decision") or {}).get("action"),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Cycle trigger failed: %s", exc)
                    references = {}  # re-anchor all pairs after a triggered cycle
                    break

            await asyncio.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="ClaudeTrader price watcher")
    parser.add_argument("--config", default="config/cycle.json")
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument(
        "--threshold-pips", action="append", default=[],
        help="PAIR=pips override (repeatable); default 10 pips",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    config = load_config(args.config)

    thresholds: dict[str, float] = {}
    for spec in args.threshold_pips:
        pair, _, value = spec.partition("=")
        if value:
            thresholds[pair.upper()] = float(value)

    trigger = config.http_trigger
    base_url = f"http://{trigger.get('host', '127.0.0.1')}:{trigger.get('port', 8787)}"

    try:
        asyncio.run(watch(config, thresholds, args.poll, base_url))
    except KeyboardInterrupt:
        logger.info("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
