"""Operational safety: kill switch, heartbeat, and market-data freshness.

These run outside the LLM and outside the strategy — they are the last line
of human/operational control over the system and must fail closed.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_TICK_AGE_SECONDS = 120.0


def kill_switch_engaged(path: Path) -> bool:
    """A kill-switch file halts all trading until manually removed."""
    return path.exists()


def kill_switch_reason(path: Path) -> str:
    """First line of the kill-switch file, if readable, for the audit log."""
    try:
        text = path.read_text(encoding="utf-8").strip()
        return text.splitlines()[0] if text else "kill switch engaged"
    except OSError:
        return "kill switch engaged"


def engage_kill_switch(path: Path, reason: str) -> None:
    """Engage the kill switch programmatically (e.g. from an ops endpoint)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, f"{reason}\nengaged_at={datetime.now(timezone.utc).isoformat()}\n")
    logger.warning("Kill switch engaged: %s", reason)


def write_heartbeat(path: Path, payload: dict[str, Any]) -> None:
    """Record cycle completion for external watchdogs. Never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        _atomic_write(path, json.dumps(body, indent=2, default=str))
    except OSError as exc:
        logger.warning("Failed to write heartbeat: %s", exc)


def read_heartbeat(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def heartbeat_age_seconds(path: Path, now: datetime | None = None) -> float | None:
    heartbeat = read_heartbeat(path)
    if not heartbeat:
        return None
    try:
        stamp = datetime.fromisoformat(str(heartbeat.get("timestamp")))
    except (TypeError, ValueError):
        return None
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - stamp).total_seconds())


def stale_ticks(
    ticks: dict[str, dict[str, Any]] | None,
    *,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_TICK_AGE_SECONDS,
) -> dict[str, float]:
    """Return {pair: age_seconds} for every tick older than max_age_seconds.

    Pairs whose tick carries no usable timestamp are reported with age -1:
    in live mode unknown freshness must block entries the same as stale data.
    """
    result: dict[str, float] = {}
    current = now or datetime.now(timezone.utc)
    now_ts = current.timestamp()

    for pair, tick_payload in (ticks or {}).items():
        tick = tick_payload.get("tick", tick_payload)
        raw = tick.get("time_msc") or tick.get("time")
        ts = _parse_tick_time(raw)
        if ts is None:
            result[pair] = -1.0
            continue
        age = now_ts - ts
        if age > max_age_seconds:
            result[pair] = round(age, 1)

    return result


def _parse_tick_time(value: Any) -> float | None:
    if value is None:
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts > 1_000_000_000_000:  # milliseconds
        ts /= 1000
    return ts if ts > 0 else None


def _atomic_write(path: Path, content: str) -> None:
    """Write via temp file + os.replace so readers never see partial content."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
