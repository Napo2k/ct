"""Operational alerting — webhook notifications for events a human must see.

Alerts are best-effort: a failed webhook must never break the trading cycle,
so every public function swallows its own errors and only logs them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 5.0

# Events worth waking a human for.
SEVERITY_CRITICAL = "CRITICAL"  # emergency close, suspend, kill switch
SEVERITY_WARNING = "WARNING"    # exit override, blocked entry, errors
SEVERITY_INFO = "INFO"          # executed trades


async def send_alert(
    alerts_config: dict[str, Any] | None,
    *,
    severity: str,
    event: str,
    detail: str,
    payload: dict[str, Any] | None = None,
) -> bool:
    """POST a JSON alert to the configured webhook. Returns False on any failure."""
    cfg = alerts_config or {}
    url = cfg.get("webhook_url")
    if not url:
        return False

    min_severity = str(cfg.get("min_severity", SEVERITY_WARNING)).upper()
    if _rank(severity) < _rank(min_severity):
        return False

    body = {
        "source": "claudetrader",
        "severity": severity,
        "event": event,
        "detail": detail,
        "payload": payload or {},
    }
    timeout = float(cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))

    try:
        await asyncio.to_thread(_post_json, url, body, timeout)
        return True
    except Exception as exc:  # noqa: BLE001 — alerting must never break the cycle
        logger.warning("Alert webhook failed (%s): %s", event, exc)
        return False


def _rank(severity: str) -> int:
    return {SEVERITY_INFO: 0, SEVERITY_WARNING: 1, SEVERITY_CRITICAL: 2}.get(severity, 1)


def _post_json(url: str, body: dict[str, Any], timeout: float) -> None:
    data = json.dumps(body, default=str).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


async def alert_cycle_events(
    alerts_config: dict[str, Any] | None,
    cycle_summary: dict[str, Any],
) -> None:
    """Inspect a finished cycle and emit alerts for anything actionable."""
    cfg = alerts_config or {}
    if not cfg.get("webhook_url"):
        return

    decision = cycle_summary.get("decision") or {}
    action = decision.get("action")

    if cycle_summary.get("kill_switch"):
        await send_alert(
            cfg,
            severity=SEVERITY_CRITICAL,
            event="kill_switch",
            detail=str(cycle_summary.get("kill_switch_reason", "kill switch engaged")),
        )

    if cycle_summary.get("emergency_close"):
        await send_alert(
            cfg,
            severity=SEVERITY_CRITICAL,
            event="emergency_close",
            detail=str(cycle_summary["emergency_close"].get("reason", "")),
            payload=cycle_summary["emergency_close"],
        )

    if action == "SUSPEND":
        await send_alert(
            cfg,
            severity=SEVERITY_CRITICAL,
            event="suspend",
            detail=str(decision.get("reasoning", ""))[:300],
        )

    if cycle_summary.get("exit_override"):
        await send_alert(
            cfg,
            severity=SEVERITY_WARNING,
            event="protective_exit_override",
            detail=str(cycle_summary["exit_override"].get("reasons", "")),
            payload=cycle_summary["exit_override"],
        )

    errors = cycle_summary.get("errors") or []
    if errors:
        await send_alert(
            cfg,
            severity=SEVERITY_WARNING,
            event="cycle_errors",
            detail="; ".join(str(e) for e in errors[:5]),
        )

    execution = cycle_summary.get("execution_result") or {}
    if execution.get("executed") and action == "ENTER":
        await send_alert(
            cfg,
            severity=SEVERITY_INFO,
            event="trade_entered",
            detail=(
                f"{decision.get('pair')} {decision.get('direction')} "
                f"{decision.get('lot_size')} lots @ {decision.get('entry_price')}"
            ),
            payload={
                "pair": decision.get("pair"),
                "order_type": decision.get("order_type"),
                "stop_loss": decision.get("stop_loss"),
                "take_profit": decision.get("take_profit"),
                "confidence": decision.get("confidence"),
            },
        )
