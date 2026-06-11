"""Persistent session state for drawdown tracking and loss streaks."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = ROOT / "data"


@dataclass
class SessionState:
    session_date: str = ""
    daily_start_balance: float = 0.0
    consecutive_losses: int = 0
    cycles_today: int = 0
    trades_today: int = 0
    realized_pnl_today: float = 0.0
    last_equity: float = 0.0
    session_peak_equity: float = 0.0
    last_cycle_id: str = ""
    lot_multiplier: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionState:
        return cls(
            session_date=str(data.get("session_date", "")),
            daily_start_balance=float(data.get("daily_start_balance", 0)),
            consecutive_losses=int(data.get("consecutive_losses", 0)),
            cycles_today=int(data.get("cycles_today", 0)),
            trades_today=int(data.get("trades_today", 0)),
            realized_pnl_today=float(data.get("realized_pnl_today", 0)),
            last_equity=float(data.get("last_equity", 0)),
            session_peak_equity=float(data.get("session_peak_equity", 0)),
            last_cycle_id=str(data.get("last_cycle_id", "")),
            lot_multiplier=float(data.get("lot_multiplier", 1.0)),
        )


def _state_path(state_dir: Path | str | None = None) -> Path:
    base = Path(state_dir) if state_dir else DEFAULT_STATE_DIR
    if not base.is_absolute():
        base = ROOT / base
    base.mkdir(parents=True, exist_ok=True)
    return base / "session_state.json"


def _session_date(now: datetime, timezone: str) -> str:
    return now.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d")


def load_session(state_dir: Path | str | None = None) -> SessionState:
    path = _state_path(state_dir)
    if not path.exists():
        return SessionState()
    with path.open(encoding="utf-8") as handle:
        return SessionState.from_dict(json.load(handle))


def save_session(state: SessionState, state_dir: Path | str | None = None) -> Path:
    path = _state_path(state_dir)
    # Atomic write: a crash mid-save must never leave a truncated state file,
    # because drawdown limits are computed from daily_start_balance on restart.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".session_state.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state.to_dict(), handle, indent=2)
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def begin_cycle(
    state: SessionState,
    *,
    now: datetime,
    timezone: str,
    account: dict[str, Any] | None,
    cycle_id: str,
) -> SessionState:
    """Roll session on new day and capture daily start balance."""
    today = _session_date(now, timezone)
    equity = float((account or {}).get("equity", (account or {}).get("balance", 0)))

    if state.session_date != today:
        balance = float((account or {}).get("balance", equity))
        peak = max(balance, equity) if equity > 0 else balance
        state = SessionState(
            session_date=today,
            daily_start_balance=balance if balance > 0 else equity,
            consecutive_losses=0,
            cycles_today=0,
            last_equity=equity,
            session_peak_equity=peak,
            lot_multiplier=1.0,
        )
        logger.info("New session %s — start balance %.2f", today, state.daily_start_balance)

    state.cycles_today += 1
    state.last_cycle_id = cycle_id
    if equity > 0:
        state.last_equity = equity
        state.session_peak_equity = max(state.session_peak_equity, equity)
    state.lot_multiplier = lot_multiplier_for_losses(state.consecutive_losses)
    return state


def end_cycle(
    state: SessionState,
    *,
    account: dict[str, Any] | None,
    decision: dict[str, Any],
    execution_result: dict[str, Any] | None,
) -> SessionState:
    """Update loss streak from equity change after EXIT/SUSPEND closes."""
    equity = float((account or {}).get("equity", 0))
    action = decision.get("action", "")

    if equity > 0 and state.last_equity > 0 and action in {"EXIT", "SUSPEND"}:
        delta = equity - state.last_equity
        if delta < -0.01:
            state.consecutive_losses += 1
        elif delta > 0.01:
            state.consecutive_losses = 0
        state.realized_pnl_today += delta
        state.lot_multiplier = lot_multiplier_for_losses(state.consecutive_losses)

    if equity > 0:
        state.last_equity = equity
        state.session_peak_equity = max(state.session_peak_equity, equity)

    if execution_result and execution_result.get("executed") and action == "ENTER":
        state.trades_today += 1

    return state


def lot_multiplier_for_losses(consecutive_losses: int) -> float:
    """3+ consecutive losses → 50% lot per playbook."""
    if consecutive_losses >= 3:
        return 0.5
    return 1.0


def apply_lot_multiplier(decision: dict[str, Any], multiplier: float) -> dict[str, Any]:
    """Scale ENTER lot_size by session multiplier."""
    if multiplier >= 1.0 or decision.get("action") != "ENTER":
        return decision
    adjusted = dict(decision)
    adjusted["lot_size"] = round(float(decision.get("lot_size", 0)) * multiplier, 2)
    return adjusted
