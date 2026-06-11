"""Cycle configuration loader."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cycle.env import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "cycle.json"

# Phase 2 (live trading) requires this exact string in config.live_confirmation
# AND the LIVE_TRADING=true env var. Anything less downgrades to Phase 1 paper.
LIVE_CONFIRMATION_PHRASE = "I-UNDERSTAND-THIS-TRADES-REAL-MONEY"


class ConfigError(Exception):
    """Raised when the cycle configuration is invalid or unsafe."""


@dataclass
class CycleConfig:
    phase: int = 1
    execution_mode: bool = True
    mock_mode: bool = False
    mock_llm: bool = True
    live_mode: bool = False
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
    playbook_lessons_path: str = "playbook/lessons.md"
    prefilter: dict[str, Any] = field(default_factory=dict)
    http_trigger: dict[str, Any] = field(default_factory=dict)
    session_state_dir: str = "data"
    maintenance: dict[str, Any] = field(default_factory=dict)
    manage: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)
    alerts: dict[str, Any] = field(default_factory=dict)
    verifier: dict[str, Any] = field(default_factory=dict)
    llm_router: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def playbook_file(self) -> Path:
        path = Path(self.playbook_path)
        return path if path.is_absolute() else ROOT / path

    @property
    def lessons_file(self) -> Path:
        path = Path(self.playbook_lessons_path)
        return path if path.is_absolute() else ROOT / path

    @property
    def kill_switch_path(self) -> Path:
        name = self.safety.get("kill_switch_file", "KILL_SWITCH")
        base = Path(self.session_state_dir)
        if not base.is_absolute():
            base = ROOT / base
        return base / name

    @property
    def heartbeat_path(self) -> Path:
        base = Path(self.session_state_dir)
        if not base.is_absolute():
            base = ROOT / base
        return base / "heartbeat.json"

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
    # Pick up .env credentials (ANTHROPIC_API_KEY, OANDA_*, LIVE_TRADING, ...)
    # before reading env overrides below. Real environment always wins.
    load_dotenv()

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

    phase = int(data.get("phase", 1))
    live_mode = _resolve_live_mode(phase, data, mock_mode=mock_mode, mock_llm=mock_llm)
    if phase >= 2 and not live_mode:
        phase = 1

    config = CycleConfig(
        phase=phase,
        execution_mode=execution_mode,
        mock_mode=mock_mode,
        mock_llm=mock_llm,
        live_mode=live_mode,
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
        playbook_lessons_path=data.get("playbook_lessons_path", "playbook/lessons.md"),
        prefilter=data.get("prefilter", {}),
        http_trigger=data.get("http_trigger", {}),
        session_state_dir=str(data.get("session_state_dir", "data")),
        maintenance=data.get("maintenance", {}),
        manage=data.get("manage", {}),
        risk=data.get("risk", {}),
        safety=data.get("safety", {}),
        alerts=data.get("alerts", {}),
        verifier=data.get("verifier", {}),
        llm_router=data.get("llm_router", {}),
        raw=data,
    )
    validate_config(config)
    return config


def _resolve_live_mode(
    phase: int,
    data: dict[str, Any],
    *,
    mock_mode: bool,
    mock_llm: bool,
) -> bool:
    """
    Live trading requires ALL of:
    - phase >= 2 in config
    - live_confirmation in config matching LIVE_CONFIRMATION_PHRASE exactly
    - LIVE_TRADING=true environment variable

    Mock flags conflict with live trading and raise instead of silently degrading.
    """
    if phase < 2:
        return False

    confirmation = str(data.get("live_confirmation", ""))
    env_live = os.environ.get("LIVE_TRADING", "").lower() in {"true", "1", "yes"}

    if confirmation != LIVE_CONFIRMATION_PHRASE or not env_live:
        return False

    if mock_mode:
        raise ConfigError("live trading (phase 2) cannot run with mock_mode enabled")
    if mock_llm:
        raise ConfigError("live trading (phase 2) cannot run with mock_llm enabled")
    return True


def validate_config(config: CycleConfig) -> None:
    """Reject configurations that are unsafe to trade with."""
    errors: list[str] = []

    if not config.pairs:
        errors.append("pairs must not be empty")
    if not 0 < config.base_lot_size <= 1.0:
        errors.append(f"base_lot_size {config.base_lot_size} outside (0, 1.0]")
    if not 1 <= config.max_positions <= 10:
        errors.append(f"max_positions {config.max_positions} outside [1, 10]")
    if not 0 < config.max_daily_drawdown_pct <= 10:
        errors.append(f"max_daily_drawdown_pct {config.max_daily_drawdown_pct} outside (0, 10]")
    if not 0 < config.max_intraday_drawdown_pct <= 10:
        errors.append(
            f"max_intraday_drawdown_pct {config.max_intraday_drawdown_pct} outside (0, 10]"
        )
    if config.max_intraday_drawdown_pct > config.max_daily_drawdown_pct:
        errors.append("max_intraday_drawdown_pct must not exceed max_daily_drawdown_pct")
    for pair, limit in config.spread_limits_pips.items():
        if float(limit) <= 0:
            errors.append(f"spread limit for {pair} must be positive")

    risk_per_trade = float(config.risk.get("risk_per_trade_pct", 1.0))
    if not 0 < risk_per_trade <= 5.0:
        errors.append(f"risk.risk_per_trade_pct {risk_per_trade} outside (0, 5.0]")
    max_lot = float(config.risk.get("max_lot_size", 1.0))
    if not config.base_lot_size <= max_lot <= 10.0:
        errors.append(f"risk.max_lot_size {max_lot} outside [base_lot_size, 10.0]")
    max_trades = int(config.risk.get("max_trades_per_day", 10))
    if not 1 <= max_trades <= 50:
        errors.append(f"risk.max_trades_per_day {max_trades} outside [1, 50]")

    max_tick_age = float(config.safety.get("max_tick_age_seconds", 120))
    if max_tick_age <= 0:
        errors.append("safety.max_tick_age_seconds must be positive")

    if config.live_mode:
        for pair in config.pairs:
            if pair not in config.spread_limits_pips:
                errors.append(f"live mode requires an explicit spread limit for {pair}")

    if errors:
        raise ConfigError("; ".join(errors))
