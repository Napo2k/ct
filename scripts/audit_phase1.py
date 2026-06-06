#!/usr/bin/env python3
"""Audit Phase 1 execution logs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.audit_phase0 import audit_logs  # noqa: E402


def audit_phase1(logs_dir: Path, *, min_cycles: int = 10) -> dict:
    stats = audit_logs(logs_dir, min_cycles=min_cycles)
    stats["phase1_checks"] = {
        "execution_enabled_logs": 0,
        "mock_execution_logs": 0,
        "live_execution_logs": 0,
        "successful_executions": 0,
        "failed_executions": 0,
        "risk_blocked": 0,
    }
    p1 = stats["phase1_checks"]

    for path in sorted(logs_dir.rglob("*.json")):
        try:
            with path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
        except (json.JSONDecodeError, OSError):
            continue

        meta = payload.get("meta", {})
        if meta.get("phase") != 1 and not meta.get("execution_mode"):
            continue

        p1["execution_enabled_logs"] += 1
        execution = payload.get("execution_result") or {}

        if execution.get("mock_execution"):
            p1["mock_execution_logs"] += 1
        elif execution.get("executed"):
            p1["live_execution_logs"] += 1

        if execution.get("executed"):
            p1["successful_executions"] += 1
        elif execution.get("error") or execution.get("blocked_by"):
            if execution.get("blocked_by"):
                p1["risk_blocked"] += 1
            else:
                p1["failed_executions"] += 1

    stats["phase1_ready"] = (
        stats.get("phase0_ready", False)
        and p1["execution_enabled_logs"] > 0
        and p1["live_execution_logs"] == 0  # paper/mock only until broker validated
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Phase 1 execution logs")
    parser.add_argument("--logs-dir", default="logs")
    parser.add_argument("--min-cycles", type=int, default=10)
    args = parser.parse_args()

    logs_dir = Path(args.logs_dir)
    if not logs_dir.is_absolute():
        logs_dir = ROOT / logs_dir

    stats = audit_phase1(logs_dir, min_cycles=args.min_cycles)
    print(json.dumps(stats, indent=2))
    return 0 if stats.get("phase1_ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
