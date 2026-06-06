"""Persist indicator snapshots between cycles for warm-signal delta detection."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = ROOT / "data"
STATE_FILENAME = "prefilter_state.json"


def _state_path(state_dir: Path | str | None = None) -> Path:
    base = Path(state_dir) if state_dir else DEFAULT_STATE_DIR
    if not base.is_absolute():
        base = ROOT / base
    base.mkdir(parents=True, exist_ok=True)
    return base / STATE_FILENAME


def load_prefilter_state(state_dir: Path | str | None = None) -> dict[str, dict[str, Any]]:
    path = _state_path(state_dir)
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load prefilter state: %s", exc)
        return {}
    pairs = data.get("pairs", data)
    return pairs if isinstance(pairs, dict) else {}


def save_prefilter_state(
    snapshot: dict[str, dict[str, Any]],
    state_dir: Path | str | None = None,
) -> Path:
    path = _state_path(state_dir)
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"pairs": snapshot}, handle, indent=2)
    return path


def snapshot_indicators(market_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract prefilter-relevant fields from the current market state."""
    result: dict[str, dict[str, Any]] = {}
    for pair, indicators in (market_state.get("indicators") or {}).items():
        h1 = indicators.get("H1", {})
        h4 = indicators.get("H4", {})
        result[pair] = {
            "H1": {
                k: h1[k]
                for k in ("rsi", "macd", "macd_signal", "macd_histogram")
                if k in h1
            },
            "H4": {k: h4[k] for k in ("adx",) if k in h4},
        }
    return result


def enrich_with_previous(
    market_state: dict[str, Any],
    previous: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    """Attach H1_prev/H4_prev to indicators; return pairs that received history."""
    enriched: list[str] = []
    indicators = market_state.setdefault("indicators", {})
    for pair, prior in previous.items():
        if pair not in indicators:
            continue
        pair_data = indicators[pair]
        if prior.get("H1"):
            pair_data["H1_prev"] = prior["H1"]
        if prior.get("H4"):
            pair_data["H4_prev"] = prior["H4"]
        enriched.append(pair)
    return enriched


def update_prefilter_state(
    market_state: dict[str, Any],
    state_dir: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """Save current indicators as the next cycle's previous snapshot."""
    snapshot = snapshot_indicators(market_state)
    if snapshot:
        save_prefilter_state(snapshot, state_dir)
    return snapshot
