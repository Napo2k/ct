"""SQLite store for cycle summaries and closed trades.

Additive and best-effort: the JSON session/state files remain the source of
truth for the trading loop; this store exists so the dashboard, reconciler,
and post-mortem tooling can query history without parsing thousands of log
files. A store failure must never break a trading cycle.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    phase INTEGER,
    live_mode INTEGER,
    action TEXT,
    pair TEXT,
    confidence TEXT,
    executed INTEGER,
    skipped_llm INTEGER,
    equity REAL,
    errors INTEGER,
    summary_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_cycles_cycle_id ON cycles(cycle_id);
CREATE INDEX IF NOT EXISTS idx_cycles_recorded_at ON cycles(recorded_at);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket INTEGER NOT NULL,
    symbol TEXT,
    profit REAL,
    volume REAL,
    close_time TEXT,
    comment TEXT,
    cycle_id TEXT,
    raw_json TEXT,
    UNIQUE(ticket, close_time)
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
"""


def db_path_for(state_dir: Path | str) -> Path:
    base = Path(state_dir)
    if not base.is_absolute():
        base = ROOT / base
    base.mkdir(parents=True, exist_ok=True)
    return base / "claudetrader.db"


def _connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    return conn


def record_cycle(db_path: Path | str, summary: dict[str, Any]) -> bool:
    """Insert one cycle summary. Returns False (never raises) on failure."""
    decision = summary.get("decision") or {}
    session = summary.get("session") or {}
    execution = summary.get("execution_result") or {}
    try:
        with _connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO cycles (
                    cycle_id, recorded_at, phase, live_mode, action, pair,
                    confidence, executed, skipped_llm, equity, errors, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(summary.get("cycle_id", "")),
                    datetime.now(timezone.utc).isoformat(),
                    int(summary.get("phase", 0)),
                    1 if summary.get("live_mode") else 0,
                    decision.get("action"),
                    decision.get("pair"),
                    decision.get("confidence"),
                    1 if execution.get("executed") else 0,
                    1 if summary.get("skipped_llm") else 0,
                    float(session.get("last_equity", 0) or 0),
                    len(summary.get("errors") or []),
                    json.dumps(summary, default=str)[:100_000],
                ),
            )
        return True
    except sqlite3.Error as exc:
        logger.warning("Cycle store insert failed: %s", exc)
        return False


def record_trades(db_path: Path | str, deals: list[dict[str, Any]]) -> int:
    """Insert closed deals, ignoring duplicates. Returns rows inserted."""
    inserted = 0
    try:
        with _connect(db_path) as conn:
            for deal in deals:
                ticket = deal.get("ticket")
                if ticket is None:
                    continue
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO trades (
                        ticket, symbol, profit, volume, close_time, comment,
                        cycle_id, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(ticket),
                        str(deal.get("symbol", "")),
                        float(deal.get("profit", 0) or 0),
                        float(deal.get("volume", 0) or 0),
                        str(deal.get("time", "")),
                        str(deal.get("comment", "")),
                        str(deal.get("cycle_id", "")),
                        json.dumps(deal, default=str)[:20_000],
                    ),
                )
                inserted += cursor.rowcount
    except sqlite3.Error as exc:
        logger.warning("Trade store insert failed: %s", exc)
    return inserted


def recent_cycles(db_path: Path | str, limit: int = 100) -> list[dict[str, Any]]:
    try:
        with _connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT cycle_id, recorded_at, phase, live_mode, action, pair,
                       confidence, executed, skipped_llm, equity, errors
                FROM cycles ORDER BY id DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        logger.warning("Cycle store query failed: %s", exc)
        return []


def trade_stats(db_path: Path | str) -> dict[str, Any]:
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS trades,
                       COALESCE(SUM(profit), 0) AS total_profit,
                       COALESCE(SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END), 0) AS wins,
                       COALESCE(SUM(CASE WHEN profit < 0 THEN 1 ELSE 0 END), 0) AS losses
                FROM trades
                """
            ).fetchone()
        trades, total_profit, wins, losses = row
        return {
            "trades": trades,
            "total_profit": round(total_profit, 2),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / trades, 3) if trades else None,
        }
    except sqlite3.Error as exc:
        logger.warning("Trade stats query failed: %s", exc)
        return {"trades": 0, "total_profit": 0, "wins": 0, "losses": 0, "win_rate": None}
