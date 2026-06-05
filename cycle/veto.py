"""Programmatic veto condition checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


@dataclass
class VetoResult:
    blocked: bool = False
    emergency_close: bool = False
    suspend: bool = False
    checks: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "", *, emergency: bool = False) -> None:
        self.checks.append({"name": name, "pass": passed, "detail": detail})
        if not passed:
            self.blocked = True
            if emergency:
                self.emergency_close = True


def check_vetoes(
    now: datetime,
    *,
    timezone: str = "Europe/Berlin",
    account: dict[str, Any] | None = None,
    ticks: dict[str, dict[str, Any]] | None = None,
    spread_limits_pips: dict[str, float] | None = None,
    news_events: list[dict[str, Any]] | None = None,
    session_start: time = time(7, 0),
    session_end: time = time(21, 0),
    max_daily_drawdown_pct: float = 2.0,
    daily_start_balance: float | None = None,
) -> VetoResult:
    """Evaluate all veto conditions. Returns structured PASS/FAIL per check."""
    tz = ZoneInfo(timezone)
    local = now.astimezone(tz)
    result = VetoResult()

    weekday = local.weekday()  # Mon=0
    local_time = local.time()

    in_session = weekday < 5 and session_start <= local_time < session_end
    result.add("Trading hours Mon-Fri 07:00-21:00 CET", in_session, local.strftime("%a %H:%M %Z"))

    london_open_block = time(7, 0) <= local_time < time(7, 15)
    result.add("Outside London open buffer 07:00-07:15", not london_open_block)

    ny_close_block = time(20, 30) <= local_time < time(21, 0)
    result.add("Outside NY close buffer 20:30-21:00", not ny_close_block)

    friday_late = weekday == 4 and local_time >= time(18, 0)
    result.add("Friday before 18:00 CET", not friday_late, emergency=friday_late)

    if news_events:
        high_60 = _high_impact_within(news_events, local, minutes=60)
        result.add("No HIGH impact news in 60 min", not high_60, _format_news(high_60))

        high_30 = _high_impact_within(news_events, local, minutes=30)
        result.add(
            "No HIGH impact news in 30 min (emergency)",
            not high_30,
            _format_news(high_30),
            emergency=bool(high_30),
        )

    if ticks and spread_limits_pips:
        for symbol, tick_data in ticks.items():
            limit = spread_limits_pips.get(symbol, 3.0)
            spread_pips = _spread_to_pips(symbol, tick_data)
            if spread_pips is not None:
                passed = spread_pips <= limit
                result.add(
                    f"Spread {symbol} <= {limit} pips",
                    passed,
                    f"spread={spread_pips:.2f} pips",
                )

    if account and daily_start_balance and daily_start_balance > 0:
        equity = float(account.get("equity", account.get("balance", 0)))
        drawdown_pct = ((daily_start_balance - equity) / daily_start_balance) * 100
        result.add(
            "Daily drawdown < 1%",
            drawdown_pct < 1.0,
            f"drawdown={drawdown_pct:.2f}%",
        )
        suspend_dd = drawdown_pct >= max_daily_drawdown_pct
        result.add(
            f"Daily drawdown < {max_daily_drawdown_pct}%",
            not suspend_dd,
            f"drawdown={drawdown_pct:.2f}%",
        )
        if suspend_dd:
            result.suspend = True

    return result


def _high_impact_within(
    events: list[dict[str, Any]],
    now: datetime,
    *,
    minutes: int,
) -> list[dict[str, Any]]:
    window_end = now + timedelta(minutes=minutes)
    matches = []
    for event in events:
        impact = str(event.get("impact", "")).upper()
        if impact != "HIGH":
            continue
        event_time = _parse_event_time(event.get("time") or event.get("datetime"))
        if event_time is None:
            continue
        if now <= event_time <= window_end:
            matches.append(event)
    return matches


def _parse_event_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _format_news(events: list[dict[str, Any]]) -> str:
    if not events:
        return ""
    names = [e.get("title") or e.get("event") or "HIGH impact event" for e in events[:3]]
    return "; ".join(names)


def _spread_to_pips(symbol: str, tick_data: dict[str, Any]) -> float | None:
    spread = tick_data.get("spread")
    if spread is not None:
        point = tick_data.get("point", 0.00001)
        if "JPY" in symbol:
            return float(spread) / (point * 10) if point else float(spread) * 100
        return float(spread) / (point * 10) if point else float(spread) * 10000

    tick = tick_data.get("tick", tick_data)
    bid = tick.get("bid")
    ask = tick.get("ask")
    if bid is None or ask is None:
        return None

    raw = float(ask) - float(bid)
    if "JPY" in symbol:
        return raw * 100
    return raw * 10000
