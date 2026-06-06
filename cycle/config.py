"""Cycle configuration loader."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "cycle.json"


@dataclass
class CycleConfig:
    phase: int = 1
    execution_mode: bool = True
    mock_mode: bool = False
    mock_llm: bool = True
    pairs: list[str] = field(default_factory=lambda: ["EURUSD", "GBPUSD", "USDJPY"])
    timezone: str = "Europe/Berlin"
    base_lot_size: float = 0.01
    max_positions: int = 3
    max_daily_drawdown_pct: float = 2.0
    max_intraday_drawdown_pct: float = 1.5
    spread_limits_pips: dict[str, float] = field(default_factory=dict)
    mt5_mcp: dict[str, Any] = field(default_factory=dict)
    massive_mcp: dict[str, Any] = field(default_factory=dict)
    anthropic: dict[str, Any] = field(default_factory=dict)
    gitea: dict[str, Any] = field(default_factory=dict)
    playbook_path: str = "playbook/algo_trading_skill.md"
    prefilter: dict[str, Any] = field(default_factory=dict)
    http_trigger: dict[str, Any] = field(default_factory=dict)
    session_state_dir: str = "data"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def playbook_file(self) -> Path:
        path = Path(self.playbook_path)
        return path if path.is_absolute() else ROOT / path

    @property
    def logs_dir(self) -> Path:
        logs = self.gitea.get("logs_dir", "logs")
        repo = Path(self.gitea.get("repo_path", "."))
        if not repo.is_absolute():
            repo = ROOT / repo
        return repo / logs

    @property
    def repo_path(self) -> Path:
        repo = Path(self.gitea.get("repo_path", "."))
        return repo if repo.is_absolute() else ROOT / repo


def load_config(path: Path | str | None = None) -> CycleConfig:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.is_absolute():
        config_path = ROOT / config_path

    with config_path.open(encoding="utf-8") as handle:
        data = json.load(handle)

    execution_mode = data.get("execution_mode", False)
    env_override = os.environ.get("EXECUTION_MODE", "").lower()
    if env_override in {"true", "1", "yes"}:
        execution_mode = True
    elif env_override in {"false", "0", "no"}:
        execution_mode = False

    mock_mode = data.get("mock_mode", False)
    mock_env = os.environ.get("MOCK_MODE", "").lower()
    if mock_env in {"true", "1", "yes"}:
        mock_mode = True
    elif mock_env in {"false", "0", "no"}:
        mock_mode = False

    mock_llm = data.get("mock_llm", True)
    mock_llm_env = os.environ.get("MOCK_LLM", "").lower()
    if mock_llm_env in {"true", "1", "yes"}:
        mock_llm = True
    elif mock_llm_env in {"false", "0", "no"}:
        mock_llm = False

    return CycleConfig(
        phase=int(data.get("phase", 1)),
        execution_mode=execution_mode,
        mock_mode=mock_mode,
        mock_llm=mock_llm,
        pairs=[p.upper() for p in data.get("pairs", ["EURUSD", "GBPUSD", "USDJPY"])],
        timezone=data.get("timezone", "Europe/Berlin"),
        base_lot_size=float(data.get("base_lot_size", 0.01)),
        max_positions=int(data.get("max_positions", 3)),
        max_daily_drawdown_pct=float(data.get("max_daily_drawdown_pct", 2.0)),
        max_intraday_drawdown_pct=float(data.get("max_intraday_drawdown_pct", 1.5)),
        spread_limits_pips=data.get("spread_limits_pips", {}),
        mt5_mcp=data.get("mt5_mcp", {}),
        massive_mcp=data.get("massive_mcp", {}),
        anthropic=data.get("anthropic", {}),
        gitea=data.get("gitea", {}),
        playbook_path=data.get("playbook_path", "playbook/algo_trading_skill.md"),
        prefilter=data.get("prefilter", {}),
        http_trigger=data.get("http_trigger", {}),
        session_state_dir=str(data.get("session_state_dir", "data")),
        raw=data,
    )
