"""Historical bar loading and synthetic data generation."""

from __future__ import annotations

import csv
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

H1_SECONDS = 3600


def load_bars_csv(path: Path | str) -> list[dict[str, Any]]:
    """Load OHLCV bars from CSV with columns: time, open, high, low, close[, volume].

    `time` may be unix seconds or ISO-8601. Bars are returned oldest-first.
    """
    bars: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            bars.append({
                "time": _parse_time(row["time"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "tick_volume": float(row.get("volume", 0) or 0),
            })
    bars.sort(key=lambda b: b["time"])
    return bars


def save_bars_csv(bars: list[dict[str, Any]], path: Path | str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "open", "high", "low", "close", "volume"])
        for bar in bars:
            writer.writerow([
                bar["time"], bar["open"], bar["high"], bar["low"], bar["close"],
                bar.get("tick_volume", 0),
            ])


def _parse_time(value: str) -> int:
    text = str(value).strip()
    try:
        return int(float(text))
    except ValueError:
        pass
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return int(datetime.fromisoformat(text).timestamp())


def generate_synthetic_bars(
    *,
    count: int,
    start_price: float = 1.0800,
    start_time: int | None = None,
    bar_seconds: int = H1_SECONDS,
    seed: int = 42,
    volatility: float = 0.0008,
    regime_length: int = 240,
) -> list[dict[str, Any]]:
    """Random-walk H1 bars with alternating trend regimes.

    Regimes flip between up-trend, down-trend, and range every ~regime_length
    bars so trend-following logic has something to find. Deterministic per seed.
    """
    rng = random.Random(seed)
    if start_time is None:
        # Monday 00:00 UTC so weekday sessions line up with veto trading hours
        start_time = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())

    bars: list[dict[str, Any]] = []
    price = start_price
    regimes = [0.00012, 0.0, -0.00012, 0.0]
    for i in range(count):
        drift = regimes[(i // regime_length) % len(regimes)]
        change = rng.gauss(drift, volatility)
        open_price = price
        close_price = max(0.01, price + change)
        wick = abs(rng.gauss(0, volatility / 2))
        high = max(open_price, close_price) + wick
        low = min(open_price, close_price) - wick
        bars.append({
            "time": start_time + i * bar_seconds,
            "open": round(open_price, 5),
            "high": round(high, 5),
            "low": round(low, 5),
            "close": round(close_price, 5),
            "tick_volume": rng.randint(100, 1000),
        })
        price = close_price
    return bars


def resample_h1_to_h4(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate H1 bars into H4 bars (groups of 4, aligned to the series start)."""
    out: list[dict[str, Any]] = []
    for i in range(0, len(bars) - len(bars) % 4, 4):
        chunk = bars[i:i + 4]
        out.append({
            "time": chunk[0]["time"],
            "open": chunk[0]["open"],
            "high": max(b["high"] for b in chunk),
            "low": min(b["low"] for b in chunk),
            "close": chunk[-1]["close"],
            "tick_volume": sum(b.get("tick_volume", 0) for b in chunk),
        })
    return out
