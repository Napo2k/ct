"""Main cycle orchestrator."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from cycle.config import CycleConfig, load_config
from cycle.decision import (
    DecisionValidationError,
    hold_decision,
    suspend_decision,
    validate_decision,
)
from cycle.executor import execute_decision
from cycle.gitea_logger import write_cycle_log
from cycle.llm import LLMError, build_user_prompt, invoke_claude, load_playbook
from cycle.market_state import fetch_market_state
from cycle.mcp_client import MCPClient, MCPClientError, mcp_session
from cycle.prefilter import filter_pairs
from cycle.veto import check_vetoes

logger = logging.getLogger(__name__)


async def run_cycle(config: CycleConfig | None = None) -> dict[str, Any]:
    """Execute one ClaudeTrader evaluation cycle."""
    cfg = config or load_config()
    cycle_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary: dict[str, Any] = {
        "cycle_id": cycle_id,
        "execution_mode": cfg.execution_mode,
        "skipped_llm": False,
        "errors": [],
    }

    mt5_cfg = cfg.mt5_mcp
    massive_cfg = cfg.massive_mcp

    market_state: dict[str, Any] = {"timestamp": cycle_id, "pairs": cfg.pairs}
    decision: dict[str, Any] | None = None
    execution_result: dict[str, Any] | None = None

    try:
        async with mcp_session("mt5", mt5_cfg) as mt5:
            massive = None
            try:
                async with mcp_session("massive", massive_cfg) as massive_client:
                    massive = massive_client
                    market_state = await fetch_market_state(cfg.pairs, mt5, massive)
                    summary.update(await _evaluate_and_log(
                        cfg, cycle_id, market_state, mt5, summary
                    ))
            except MCPClientError:
                logger.warning("Massive MCP unavailable — continuing with MT5 only")
                market_state = await fetch_market_state(cfg.pairs, mt5, None)
                result = await _evaluate_and_log(cfg, cycle_id, market_state, mt5, summary)
                summary.update(result)

    except MCPClientError as exc:
        logger.error("MT5 MCP connection failed: %s", exc)
        summary["errors"].append(str(exc))
        decision = suspend_decision(cycle_id, f"MT5 MCP unavailable: {exc}")
        market_state["errors"] = summary["errors"]
        log_path = write_cycle_log(cfg, decision, market_state, meta={"connection_error": True})
        summary["log_path"] = str(log_path)
        summary["decision"] = decision

    return summary


async def _evaluate_and_log(
    cfg: CycleConfig,
    cycle_id: str,
    market_state: dict[str, Any],
    mt5: MCPClient,
    summary: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {"errors": []}

    veto = check_vetoes(
        datetime.now(timezone.utc),
        timezone=cfg.timezone,
        account=market_state.get("account"),
        ticks=market_state.get("ticks"),
        spread_limits_pips=cfg.spread_limits_pips,
        news_events=market_state.get("news"),
        max_daily_drawdown_pct=cfg.max_daily_drawdown_pct,
    )
    veto_dict = {
        "blocked": veto.blocked,
        "emergency_close": veto.emergency_close,
        "suspend": veto.suspend,
        "checks": veto.checks,
    }
    result["veto"] = veto_dict

    if veto.suspend:
        decision = suspend_decision(cycle_id, "Veto: daily drawdown limit reached")
        log_path = write_cycle_log(cfg, decision, market_state, meta={"veto_suspend": True})
        result.update({"decision": decision, "log_path": str(log_path), "skipped_llm": True})
        return result

    active_pairs, warm_reasons = filter_pairs(
        cfg.pairs,
        market_state,
        prefilter_config=cfg.prefilter,
    )

    prefilter_enabled = cfg.prefilter.get("enabled", True)
    if prefilter_enabled and not active_pairs:
        decision = hold_decision(
            "EURUSD",
            cycle_id,
            "Pre-filter: no warm signals across monitored pairs",
        )
        log_path = write_cycle_log(
            cfg,
            decision,
            market_state,
            meta={"prefilter_skip": True, "warm_reasons": {}},
        )
        result.update({"decision": decision, "log_path": str(log_path), "skipped_llm": True})
        return result

    pairs_to_eval = active_pairs or cfg.pairs
    result["active_pairs"] = pairs_to_eval
    result["warm_reasons"] = warm_reasons

    try:
        playbook = load_playbook(cfg.playbook_file)
        user_prompt = build_user_prompt(
            cycle_id,
            pairs_to_eval,
            market_state,
            veto_dict,
            warm_reasons,
            cfg.execution_mode,
        )
        raw_decision = await invoke_claude(
            playbook=playbook,
            user_prompt=user_prompt,
            model=cfg.anthropic.get("model", "claude-sonnet-4-20250514"),
            max_tokens=int(cfg.anthropic.get("max_tokens", 4096)),
        )
        decision = validate_decision(raw_decision, cycle_id=cycle_id)
    except (LLMError, DecisionValidationError) as exc:
        logger.error("Decision generation failed: %s", exc)
        result["errors"].append(str(exc))
        decision = hold_decision("EURUSD", cycle_id, f"Decision error: {exc}")

    if veto.blocked and decision.get("action") == "ENTER":
        decision = hold_decision(
            decision.get("pair", "EURUSD"),
            cycle_id,
            "Veto conditions block new entries — overriding ENTER to HOLD",
        )

    execution_result = await execute_decision(
        decision,
        mt5,
        execution_mode=cfg.execution_mode,
        cycle_id=cycle_id,
    )

    log_path = write_cycle_log(
        cfg,
        decision,
        market_state,
        execution_result=execution_result,
        meta={
            "warm_reasons": warm_reasons,
            "veto": veto_dict,
            "skipped_llm": False,
        },
    )

    result.update({
        "decision": decision,
        "execution_result": execution_result,
        "log_path": str(log_path),
        "skipped_llm": False,
    })
    return result
