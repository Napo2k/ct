"""Cycle log writer — logs/YYYY-MM-DD/HH-MM-SS_{PAIR}_{ACTION}.json + optional git commit."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cycle.config import CycleConfig

logger = logging.getLogger(__name__)


def _safe_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", value.upper())
    return cleaned or "UNKNOWN"


def build_log_payload(
    decision: dict[str, Any],
    market_state: dict[str, Any],
    *,
    execution_result: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    playbook_version: str | None = None,
) -> dict[str, Any]:
    return {
        "decision": decision,
        "market_state": market_state,
        "execution_result": execution_result,
        "meta": {
            "playbook": playbook_version or "playbook/algo_trading_skill.md",
            "logged_at": datetime.now(timezone.utc).isoformat(),
            **(meta or {}),
        },
    }


def write_cycle_log(
    config: CycleConfig,
    decision: dict[str, Any],
    market_state: dict[str, Any],
    *,
    execution_result: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> Path:
    """Write cycle log to logs/YYYY-MM-DD/HH-MM-SS_{PAIR}_{ACTION}.json."""
    now = datetime.now(timezone.utc)
    date_dir = config.logs_dir / now.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)

    pair = _safe_filename_component(str(decision.get("pair", "EURUSD")))
    action = _safe_filename_component(str(decision.get("action", "HOLD")))
    base = f"{now.strftime('%H-%M-%S')}-{now.microsecond // 1000:03d}_{pair}_{action}"
    log_path = date_dir / f"{base}.json"
    suffix = 1
    while log_path.exists():
        log_path = date_dir / f"{base}_{suffix}.json"
        suffix += 1

    payload = build_log_payload(
        decision,
        market_state,
        execution_result=execution_result,
        meta=meta,
        playbook_version=str(config.playbook_path),
    )

    with log_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)

    logger.info("Wrote cycle log: %s", log_path)

    if config.gitea.get("auto_commit", True):
        _git_commit(config, log_path)

    return log_path


def _git_commit(config: CycleConfig, log_path: Path) -> None:
    repo = config.repo_path
    if not (repo / ".git").exists():
        logger.debug("No git repo at %s — skipping commit", repo)
        return

    rel_path = log_path.relative_to(repo) if log_path.is_relative_to(repo) else log_path

    try:
        # -f: cycle logs are matched by .gitignore (logs/**/*.json) so local
        # runs stay clean, but the audit trail must still reach the repo.
        subprocess.run(
            ["git", "add", "-f", str(rel_path)],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "commit",
                "-m",
                f"cycle log: {log_path.name}",
            ],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info("Committed %s", rel_path)

        if config.gitea.get("auto_push", False):
            remote = config.gitea.get("remote", "origin")
            branch = config.gitea.get("branch", "main")
            subprocess.run(
                ["git", "push", remote, branch],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info("Pushed to %s/%s", remote, branch)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        if "nothing to commit" in stderr.lower():
            logger.debug("Nothing to commit")
        else:
            logger.warning("Git commit failed: %s", stderr.strip())
