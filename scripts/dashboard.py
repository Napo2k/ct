#!/usr/bin/env python3
"""
ClaudeTrader dashboard — read-only Streamlit view over the SQLite store + logs.

    pip install streamlit
    streamlit run scripts/dashboard.py

Shows: equity curve, decision/action mix, veto + prefilter rates, trade stats,
recent decisions, and lessons learned. Reads only — never touches broker state.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import streamlit as st
except ImportError:  # pragma: no cover - optional dependency
    print("Streamlit is not installed. Run: pip install streamlit", file=sys.stderr)
    raise SystemExit(1)

from cycle.config import load_config  # noqa: E402
from cycle.postmortem import load_lessons  # noqa: E402
from cycle.safety import heartbeat_age_seconds, kill_switch_engaged  # noqa: E402
from cycle.store import db_path_for, recent_cycles, trade_stats  # noqa: E402

st.set_page_config(page_title="ClaudeTrader", layout="wide")

config = load_config()
db_path = db_path_for(config.session_state_dir)

st.title("ClaudeTrader")

# --- Status row -------------------------------------------------------------
col1, col2, col3, col4 = st.columns(4)
engaged = kill_switch_engaged(config.kill_switch_path)
heartbeat_age = heartbeat_age_seconds(config.heartbeat_path)
col1.metric("Mode", f"Phase {config.phase}" + (" LIVE" if config.live_mode else ""))
col2.metric("Kill switch", "ENGAGED" if engaged else "clear")
col3.metric(
    "Heartbeat age",
    f"{heartbeat_age:.0f}s" if heartbeat_age is not None else "never",
)

stats = trade_stats(db_path)
col4.metric(
    "Trades (win rate)",
    f"{stats['trades']} ({stats['win_rate']:.0%})" if stats["win_rate"] is not None else str(stats["trades"]),
    delta=f"{stats['total_profit']:+.2f}",
)

# --- Equity curve -----------------------------------------------------------
cycles = recent_cycles(db_path, limit=2000)
if cycles:
    points = [
        {"recorded_at": c["recorded_at"], "equity": c["equity"]}
        for c in reversed(cycles)
        if c["equity"]
    ]
    if points:
        st.subheader("Equity")
        st.line_chart(points, x="recorded_at", y="equity")

    # --- Action mix and gate rates -------------------------------------------
    left, right = st.columns(2)
    with left:
        st.subheader("Decision mix")
        mix: dict[str, int] = {}
        for c in cycles:
            mix[c["action"] or "?"] = mix.get(c["action"] or "?", 0) + 1
        st.bar_chart(mix)
    with right:
        st.subheader("Pipeline rates")
        n = len(cycles)
        skipped = sum(1 for c in cycles if c["skipped_llm"])
        executed = sum(1 for c in cycles if c["executed"])
        errors = sum(1 for c in cycles if c["errors"])
        st.write({
            "cycles": n,
            "llm_skipped_pct": round(skipped / n * 100, 1),
            "executed_pct": round(executed / n * 100, 1),
            "cycles_with_errors": errors,
        })

    st.subheader("Recent decisions")
    st.dataframe(cycles[:50])
else:
    st.info("No cycles recorded yet — run a cycle to populate the store.")

# --- Lessons ------------------------------------------------------------------
lessons = load_lessons(config.lessons_file)
if lessons:
    st.subheader("Lessons learned")
    st.markdown(lessons)
