#!/usr/bin/env python3
"""
Download historical H1 candles from the OANDA v20 REST API for backtesting.

    export OANDA_API_TOKEN=...      # personal access token (account portal → Manage API Access)
    export OANDA_ACCOUNT_ID=...     # e.g. 101-004-1234567-001
    python scripts/fetch_oanda_history.py --pairs EURUSD,GBPUSD,USDJPY --years 3

    # Then backtest on it:
    python scripts/run_backtest.py --csv EURUSD=data/history/EURUSD_H1.csv

Uses the practice environment (api-fxpractice.oanda.com) by default — the same
environment as the OANDA demo account. Writes CSVs compatible with
backtest/data.load_bars_csv. Only completed candles are kept.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data import save_bars_csv  # noqa: E402
from cycle.env import load_dotenv  # noqa: E402

logger = logging.getLogger(__name__)

HOSTS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}
CANDLES_PER_REQUEST = 5000
MAX_REQUESTS_PER_PAIR = 200  # runaway guard: 200 * 5000 H1 bars ≈ 114 years
RETRY_DELAY_SECONDS = 5.0
MAX_RETRIES = 3

GRANULARITY_SECONDS = {"M15": 900, "H1": 3600, "H4": 14400, "D": 86400}


def oanda_instrument(pair: str) -> str:
    """EURUSD -> EUR_USD."""
    pair = pair.strip().upper()
    if "_" in pair:
        return pair
    if len(pair) != 6:
        raise ValueError(f"Cannot derive OANDA instrument from {pair!r}")
    return f"{pair[:3]}_{pair[3:]}"


def _http_get(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept-Datetime-Format": "UNIX",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def _candles_to_bars(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bars = []
    for candle in candles:
        if not candle.get("complete", False):
            continue
        mid = candle.get("mid") or {}
        bars.append({
            "time": int(float(candle["time"])),
            "open": float(mid["o"]),
            "high": float(mid["h"]),
            "low": float(mid["l"]),
            "close": float(mid["c"]),
            "tick_volume": int(candle.get("volume", 0)),
        })
    return bars


def fetch_pair_history(
    pair: str,
    *,
    account_id: str,
    token: str,
    host: str,
    granularity: str = "H1",
    from_time: datetime,
    to_time: datetime,
    fetch_fn: Callable[[str, str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Paginate through the candles endpoint, oldest first, completed candles only."""
    fetch = fetch_fn or _http_get
    instrument = oanda_instrument(pair)
    bar_seconds = GRANULARITY_SECONDS.get(granularity, 3600)

    bars: list[dict[str, Any]] = []
    cursor = from_time.timestamp()
    end_ts = to_time.timestamp()
    requests_made = 0

    while cursor < end_ts and requests_made < MAX_REQUESTS_PER_PAIR:
        params = urllib.parse.urlencode({
            "from": f"{cursor:.0f}",
            "count": CANDLES_PER_REQUEST,
            "granularity": granularity,
            "price": "M",
        })
        url = f"{host}/v3/accounts/{account_id}/instruments/{instrument}/candles?{params}"

        payload = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                payload = fetch(url, token)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < MAX_RETRIES:
                    logger.warning("Rate limited — sleeping %.0fs", RETRY_DELAY_SECONDS)
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                raise
        requests_made += 1

        candles = (payload or {}).get("candles", [])
        batch = _candles_to_bars(candles)
        new = [b for b in batch if b["time"] <= end_ts and (not bars or b["time"] > bars[-1]["time"])]
        bars.extend(new)

        logger.info(
            "%s: +%d bars (total %d, cursor %s)",
            pair, len(new), len(bars),
            datetime.fromtimestamp(cursor, tz=timezone.utc).date(),
        )

        if not batch:
            break
        last_time = batch[-1]["time"]
        if last_time <= cursor:
            break  # no forward progress (end of available history)
        cursor = last_time + bar_seconds

        if len(candles) < CANDLES_PER_REQUEST:
            # Reached the present (or the end of available data)
            break

    return bars


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch OANDA historical candles")
    parser.add_argument("--pairs", default="EURUSD,GBPUSD,USDJPY",
                        help="Comma-separated pairs (default: EURUSD,GBPUSD,USDJPY)")
    parser.add_argument("--granularity", default="H1", choices=sorted(GRANULARITY_SECONDS))
    parser.add_argument("--years", type=float, default=3.0,
                        help="Years of history to fetch (ignored if --from given)")
    parser.add_argument("--from", dest="from_date", help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", help="End date YYYY-MM-DD (default: now)")
    parser.add_argument("--env", choices=["practice", "live"], default="practice")
    parser.add_argument("--account-id", default="",
                        help="Defaults to OANDA_ACCOUNT_ID (env or .env)")
    parser.add_argument("--out-dir", default="data/history")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    load_dotenv()

    token = os.environ.get("OANDA_API_TOKEN", "")
    if not token:
        print("OANDA_API_TOKEN not set (environment or .env).", file=sys.stderr)
        print("Generate a token: OANDA account portal → Manage API Access.", file=sys.stderr)
        return 1
    account_id = args.account_id or os.environ.get("OANDA_ACCOUNT_ID", "")
    if not account_id:
        print("Set OANDA_ACCOUNT_ID (environment or .env) or pass --account-id.", file=sys.stderr)
        return 1

    to_time = (
        datetime.strptime(args.to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if args.to_date else datetime.now(timezone.utc)
    )
    from_time = (
        datetime.strptime(args.from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if args.from_date else to_time - timedelta(days=args.years * 365.25)
    )

    host = HOSTS[args.env]
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir

    for pair in [p.strip().upper() for p in args.pairs.split(",") if p.strip()]:
        bars = fetch_pair_history(
            pair,
            account_id=account_id,
            token=token,
            host=host,
            granularity=args.granularity,
            from_time=from_time,
            to_time=to_time,
        )
        if not bars:
            print(f"{pair}: no candles returned", file=sys.stderr)
            continue
        out_path = out_dir / f"{pair}_{args.granularity}.csv"
        save_bars_csv(bars, out_path)
        first = datetime.fromtimestamp(bars[0]["time"], tz=timezone.utc).date()
        last = datetime.fromtimestamp(bars[-1]["time"], tz=timezone.utc).date()
        print(f"{pair}: {len(bars)} bars ({first} → {last}) → {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
