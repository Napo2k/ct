"""Tests for the OANDA history fetcher (pagination + parsing, no network)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_oanda_history import fetch_pair_history, oanda_instrument  # noqa: E402
from backtest.data import load_bars_csv, save_bars_csv  # noqa: E402

START = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())


def _candle(ts, price=1.08, complete=True):
    return {
        "time": str(ts),
        "volume": 100,
        "complete": complete,
        "mid": {"o": str(price), "h": str(price + 0.001), "l": str(price - 0.001), "c": str(price)},
    }


def _fake_fetch_factory(total_bars, page_size=5000):
    """Simulate the candles endpoint: serves H1 candles from START, honoring from/count."""
    calls = []

    def fetch(url, token):
        calls.append(url)
        params = parse_qs(urlparse(url).query)
        cursor = int(float(params["from"][0]))
        count = int(params["count"][0])
        first_index = max(0, (cursor - START) // 3600)
        candles = [
            _candle(START + i * 3600)
            for i in range(first_index, min(first_index + count, total_bars))
        ]
        return {"candles": candles}

    fetch.calls = calls
    return fetch


def test_instrument_conversion():
    assert oanda_instrument("EURUSD") == "EUR_USD"
    assert oanda_instrument("usdjpy") == "USD_JPY"
    assert oanda_instrument("EUR_USD") == "EUR_USD"
    with pytest.raises(ValueError):
        oanda_instrument("EURUSDX")


def test_pagination_collects_all_bars():
    fetch = _fake_fetch_factory(total_bars=12_000)
    bars = fetch_pair_history(
        "EURUSD",
        account_id="acc",
        token="tok",
        host="https://example.test",
        from_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        to_time=datetime(2025, 6, 1, tzinfo=timezone.utc),
        fetch_fn=fetch,
    )
    assert len(bars) == 12_000
    assert len(fetch.calls) == 3  # 5000 + 5000 + 2000
    # Strictly increasing, no duplicates at page boundaries
    times = [b["time"] for b in bars]
    assert times == sorted(set(times))


def test_incomplete_candles_dropped():
    def fetch(url, token):
        return {"candles": [
            _candle(START),
            _candle(START + 3600, complete=False),
            _candle(START + 7200),
        ]}

    bars = fetch_pair_history(
        "EURUSD", account_id="a", token="t", host="https://example.test",
        from_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        to_time=datetime(2024, 1, 2, tzinfo=timezone.utc),
        fetch_fn=fetch,
    )
    assert len(bars) == 2
    assert all(isinstance(b["close"], float) for b in bars)


def test_to_time_bounds_result():
    fetch = _fake_fetch_factory(total_bars=5000)
    to_time = datetime.fromtimestamp(START + 99 * 3600, tz=timezone.utc)
    bars = fetch_pair_history(
        "EURUSD", account_id="a", token="t", host="https://example.test",
        from_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        to_time=to_time,
        fetch_fn=fetch,
    )
    assert len(bars) == 100  # bars at START..START+99h inclusive
    assert bars[-1]["time"] <= to_time.timestamp()


def test_output_roundtrips_through_backtest_loader(tmp_path):
    fetch = _fake_fetch_factory(total_bars=300)
    bars = fetch_pair_history(
        "EURUSD", account_id="a", token="t", host="https://example.test",
        from_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        to_time=datetime(2024, 3, 1, tzinfo=timezone.utc),
        fetch_fn=fetch,
    )
    path = tmp_path / "EURUSD_H1.csv"
    save_bars_csv(bars, path)
    loaded = load_bars_csv(path)
    assert len(loaded) == len(bars)
    assert loaded[0]["time"] == bars[0]["time"]
    assert loaded[-1]["close"] == bars[-1]["close"]
