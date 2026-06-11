"""Main cycle orchestrator."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from cycle.alerts import alert_cycle_events
from cycle.config import CycleConfig, load_config
from cycle.decision import (
    DecisionValidationError,
    hold_decision,
    protective_exit_decision,
    suspend_decision,
    validate_decision,
)
from cycle.safety import (
    kill_switch_engaged,
    kill_switch_reason,
    stale_ticks,
    write_heartbeat,
)
from cycle.exits import evaluate_exit_signals, first_forced_exit
from cycle.executor import emergency_close_all, execute_decision
from cycle.gitea_logger import write_cycle_log
from cycle.llm import LLMError, build_user_prompt, invoke_claude, load_playbook
from cycle.market_state import fetch_market_state
from cycle.mock_data import build_mock_market_state, mock_llm_decision
from cycle.mock_mt5 import MockMT5Client
from cycle.mcp_client import MCPClient, MCPClientError, mcp_session
from cycle.maintenance import run_maintenance
from cycle.manage import manage_positions
from cycle.prefilter import filter_pairs
from cycle.prefilter_state import (
    enrich_with_previous,
    load_prefilter_state,
    update_prefilter_state,
)
from cycle.session_state import (
    apply_lot_multiplier,
    begin_cycle,
    end_cycle,
    load_session,
    save_session,
)
from cycle.router import (
    build_triage_summary,
    decide_with_routing,
    load_provider_state,
    route_decision,
    save_provider_state,
)
from cycle.store import db_path_for, record_cycle
from cycle.verifier import verify_entry
from cycle.veto import check_vetoes

logger = logging.getLogger(__name__)


async def run_cycle(config: CycleConfig | None = None) -> dict[str, Any]:
    """Execute one ClaudeTrader evaluation cycle."""
    cfg = config or load_config()
    cycle_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary: dict[str, Any] = {
        "cycle_id": cycle_id,
        "phase": cfg.phase,
        "execution_mode": cfg.execution_mode,
        "mock_mode": cfg.mock_mode,
        "skipped_llm": False,
        "errors": [],
    }

    if cfg.live_mode:
        logger.warning("LIVE MODE ACTIVE — orders will execute with real money")
        summary["live_mode"] = True

    if cfg.mock_mode:
        logger.info("Running in mock_mode — using fixture data")
        market_state = build_mock_market_state(cfg.pairs, cycle_id)
        mt5: MCPClient | MockMT5Client | None = (
            MockMT5Client(market_state) if cfg.execution_mode else None
        )
        summary.update(
            await _evaluate_and_log(cfg, cycle_id, market_state, mt5, summary, mock_meta=True)
        )
        return await _finalize_cycle(cfg, summary)

    mt5_cfg = cfg.mt5_mcp
    massive_cfg = cfg.massive_mcp

    market_state: dict[str, Any] = {"timestamp": cycle_id, "pairs": cfg.pairs}

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
        log_path = write_cycle_log(
            cfg,
            decision,
            market_state,
            meta={"connection_error": True, "phase": cfg.phase},
        )
        summary["log_path"] = str(log_path)
        summary["decision"] = decision

    return await _finalize_cycle(cfg, summary)


async def _finalize_cycle(cfg: CycleConfig, summary: dict[str, Any]) -> dict[str, Any]:
    """Heartbeat + alerts + history store on every cycle exit path. Never raises."""
    try:
        record_cycle(db_path_for(cfg.session_state_dir), summary)
    except Exception as exc:  # noqa: BLE001 — the store is best-effort
        logger.warning("Cycle store write failed: %s", exc)
    write_heartbeat(
        cfg.heartbeat_path,
        {
            "cycle_id": summary.get("cycle_id"),
            "phase": cfg.phase,
            "live_mode": cfg.live_mode,
            "action": (summary.get("decision") or {}).get("action"),
            "errors": len(summary.get("errors") or []),
        },
    )
    try:
        await alert_cycle_events(cfg.alerts, summary)
    except Exception as exc:  # noqa: BLE001 — alerting must never break the cycle
        logger.warning("Alert dispatch failed: %s", exc)
    return summary


async def _evaluate_and_log(
    cfg: CycleConfig,
    cycle_id: str,
    market_state: dict[str, Any],
    mt5: MCPClient | MockMT5Client | None,
    summary: dict[str, Any],
    *,
    mock_meta: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {"errors": []}
    mock_execution = cfg.mock_mode and cfg.execution_mode
    maintenance_result: dict[str, Any] | None = None

    now = (
        datetime(2026, 6, 3, 10, 0, tzinfo=timezone.utc)
        if cfg.mock_mode
        else datetime.now(timezone.utc)
    )

    if kill_switch_engaged(cfg.kill_switch_path):
        reason = kill_switch_reason(cfg.kill_switch_path)
        logger.warning("Kill switch engaged — suspending: %s", reason)
        decision = suspend_decision(cycle_id, f"Kill switch: {reason}")
        execution_result = None
        if cfg.execution_mode and mt5 is not None:
            execution_result = await emergency_close_all(mt5, reason=f"kill switch: {reason}")
        log_path = write_cycle_log(
            cfg,
            decision,
            market_state,
            execution_result=execution_result,
            meta=_log_meta(cfg, mock_meta, market_state, kill_switch=True),
        )
        result.update({
            "decision": decision,
            "execution_result": execution_result,
            "log_path": str(log_path),
            "skipped_llm": True,
            "kill_switch": True,
            "kill_switch_reason": reason,
        })
        return result

    session = load_session(cfg.session_state_dir)
    session = begin_cycle(
        session,
        now=now,
        timezone=cfg.timezone,
        account=market_state.get("account"),
        cycle_id=cycle_id,
    )
    result["session"] = session.to_dict()

    prior_indicators = load_prefilter_state(cfg.session_state_dir)
    prefilter_history = enrich_with_previous(market_state, prior_indicators)
    if prefilter_history:
        result["prefilter_history_pairs"] = prefilter_history

    if cfg.execution_mode and mt5 is not None and cfg.maintenance.get("enabled", True):
        maintenance_result = await run_maintenance(
            mt5,
            market_state,
            max_pending_hours=float(cfg.maintenance.get("max_pending_hours", 48)),
            max_position_hours_without_tp=float(
                cfg.maintenance.get("max_position_hours_without_tp", 48)
            ),
            now=now,
        )
        result["maintenance"] = maintenance_result

    # Protective management (BE moves, trailing, partial closes) runs even
    # when vetoes block new entries — these actions only ever reduce risk.
    if cfg.execution_mode and mt5 is not None and cfg.manage.get("enabled", True):
        manage_result = await manage_positions(
            mt5,
            market_state,
            manage_config=cfg.manage,
            state_dir=cfg.session_state_dir,
        )
        if manage_result["actions"]:
            result["position_management"] = manage_result

    stale_ages: dict[str, float] = {}
    if not cfg.mock_mode:
        stale_ages = stale_ticks(
            market_state.get("ticks"),
            now=now,
            max_age_seconds=float(cfg.safety.get("max_tick_age_seconds", 120)),
        )

    state_errors = market_state.get("errors") or []
    news_feed_available = not any(
        "economic_calendar" in str(e) or "Massive MCP" in str(e) for e in state_errors
    )

    veto = check_vetoes(
        now,
        timezone=cfg.timezone,
        account=market_state.get("account"),
        ticks=market_state.get("ticks"),
        spread_limits_pips=cfg.spread_limits_pips,
        news_events=market_state.get("news"),
        max_daily_drawdown_pct=cfg.max_daily_drawdown_pct,
        max_intraday_drawdown_pct=cfg.max_intraday_drawdown_pct,
        daily_start_balance=session.daily_start_balance or None,
        session_peak_equity=session.session_peak_equity or None,
        live_mode=cfg.live_mode,
        pairs=cfg.pairs,
        stale_tick_ages=stale_ages,
        news_feed_available=news_feed_available,
    )
    veto_dict = {
        "blocked": veto.blocked,
        "emergency_close": veto.emergency_close,
        "suspend": veto.suspend,
        "checks": veto.checks,
    }
    result["veto"] = veto_dict

    exit_signals = evaluate_exit_signals(
        market_state,
        max_age_hours=float(cfg.maintenance.get("max_position_hours_without_tp", 48)),
        now=now,
    )
    if exit_signals:
        result["exit_signals"] = exit_signals

    emergency_result = None
    if veto.emergency_close and cfg.execution_mode and mt5 is not None:
        emergency_result = await emergency_close_all(mt5, reason="veto emergency")
        result["emergency_close"] = emergency_result

    if veto.suspend:
        decision = suspend_decision(cycle_id, "Veto: daily drawdown limit reached")
        execution_result = None
        if cfg.execution_mode and mt5 is not None:
            execution_result = await execute_decision(
                decision,
                mt5,
                execution_mode=True,
                cycle_id=cycle_id,
                market_state=market_state,
                max_positions=cfg.max_positions,
                base_lot_size=cfg.base_lot_size,
                mock_execution=mock_execution,
                consecutive_losses=session.consecutive_losses,
                risk_config=cfg.risk,
                trades_today=session.trades_today,
            )
        session = end_cycle(
            session,
            account=market_state.get("account"),
            decision=decision,
            execution_result=execution_result,
        )
        save_session(session, cfg.session_state_dir)
        log_path = write_cycle_log(
            cfg,
            decision,
            market_state,
            execution_result=execution_result,
            meta=_log_meta(cfg, mock_meta, market_state, session=session.to_dict(), veto_suspend=True),
        )
        result.update({
            "decision": decision,
            "execution_result": execution_result,
            "log_path": str(log_path),
            "skipped_llm": True,
            "session": session.to_dict(),
        })
        update_prefilter_state(market_state, cfg.session_state_dir)
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
            meta=_log_meta(cfg, mock_meta, market_state, prefilter_skip=True, warm_reasons={}),
        )
        result.update({"decision": decision, "log_path": str(log_path), "skipped_llm": True})
        update_prefilter_state(market_state, cfg.session_state_dir)
        return result

    pairs_to_eval = active_pairs or cfg.pairs
    result["active_pairs"] = pairs_to_eval
    result["warm_reasons"] = warm_reasons

    try:
        if cfg.mock_mode and cfg.mock_llm:
            raw_decision = mock_llm_decision(cycle_id, market_state)
            logger.info("Using mock_llm decision (no Anthropic API call)")
        else:
            playbook = load_playbook(cfg.playbook_file, lessons_path=cfg.lessons_file)
            user_prompt = build_user_prompt(
                cycle_id,
                pairs_to_eval,
                market_state,
                veto_dict,
                warm_reasons,
                cfg.execution_mode,
                session=session.to_dict(),
                exit_signals=exit_signals,
                live_mode=cfg.live_mode,
            )
            if cfg.llm_router.get("enabled", False):
                provider_state = load_provider_state(cfg.session_state_dir)
                triage = build_triage_summary(
                    market_state, veto_dict, warm_reasons, exit_signals,
                    live_mode=cfg.live_mode,
                )
                routing = await route_decision(
                    triage,
                    router_config=cfg.llm_router,
                    provider_state=provider_state,
                    live_mode=cfg.live_mode,
                )
                raw_decision, routing_meta = await decide_with_routing(
                    playbook=playbook,
                    user_prompt=user_prompt,
                    routing=routing,
                    router_config=cfg.llm_router,
                    anthropic_config=cfg.anthropic,
                    provider_state=provider_state,
                    mt5=mt5,
                )
                save_provider_state(provider_state, cfg.session_state_dir)
                result["llm_routing"] = routing_meta
                logger.info(
                    "Decision routed to %s (%s): %s",
                    routing_meta.get("decided_by"),
                    routing_meta.get("complexity"),
                    routing_meta.get("reason"),
                )
            else:
                raw_decision = await invoke_claude(
                    playbook=playbook,
                    user_prompt=user_prompt,
                    model=cfg.anthropic.get("model", "claude-opus-4-8"),
                    max_tokens=int(cfg.anthropic.get("max_tokens", 8192)),
                    max_retries=int(cfg.anthropic.get("max_retries", 3)),
                    timeout_seconds=float(cfg.anthropic.get("timeout_seconds", 60.0)),
                    retry_base_delay=float(cfg.anthropic.get("retry_base_delay", 1.0)),
                    mt5=mt5,
                    enable_tools=bool(cfg.anthropic.get("enable_tools", False)),
                    max_tool_rounds=int(cfg.anthropic.get("max_tool_rounds", 5)),
                    use_structured_output=bool(cfg.anthropic.get("structured_output", True)),
                    cache_playbook=bool(cfg.anthropic.get("cache_playbook", True)),
                )
        decision = validate_decision(raw_decision, cycle_id=cycle_id)
    except (LLMError, DecisionValidationError, RuntimeError) as exc:
        logger.error("Decision generation failed: %s", exc)
        result["errors"].append(str(exc))
        decision = hold_decision("EURUSD", cycle_id, f"Decision error: {exc}")

    if veto.blocked and decision.get("action") == "ENTER":
        decision = hold_decision(
            decision.get("pair", "EURUSD"),
            cycle_id,
            "Veto conditions block new entries — overriding ENTER to HOLD",
        )

    if cfg.live_mode and decision.get("action") == "ENTER":
        min_confidence = str(cfg.risk.get("live_min_confidence", "HIGH")).upper()
        if _confidence_rank(decision.get("confidence")) < _confidence_rank(min_confidence):
            decision = hold_decision(
                decision.get("pair", "EURUSD"),
                cycle_id,
                f"Live mode requires {min_confidence} confidence — "
                f"got {decision.get('confidence')}, overriding ENTER to HOLD",
            )
            result["confidence_gate"] = True

    # Adversarial verification: a second, independent model call tries to refute
    # the entry. Defaults on in live mode. A verification *error* blocks the
    # entry in live mode (fail closed) but only warns in paper mode.
    verifier_enabled = cfg.verifier.get("enabled", cfg.live_mode)
    if (
        decision.get("action") == "ENTER"
        and verifier_enabled
        and not cfg.mock_mode
        and not cfg.mock_llm
    ):
        verdict = await verify_entry(
            decision,
            market_state,
            veto_dict,
            verifier_config=cfg.verifier,
        )
        result["verifier"] = verdict
        block_entry = verdict["refuted"] or (cfg.live_mode and not verdict["approved"])
        if block_entry:
            reason = verdict["reason"] or verdict.get("error") or "verification unavailable"
            decision = hold_decision(
                decision.get("pair", "EURUSD"),
                cycle_id,
                f"Entry refused by adversarial verifier: {reason}",
            )
            logger.warning("Verifier blocked entry: %s", reason)

    if decision.get("action") == "ENTER" and session.lot_multiplier < 1.0:
        decision = apply_lot_multiplier(decision, session.lot_multiplier)

    forced_pair = first_forced_exit(exit_signals)
    if (
        cfg.execution_mode
        and forced_pair
        and not (decision.get("action") == "EXIT" and decision.get("pair") == forced_pair)
        and decision.get("action") != "SUSPEND"
    ):
        reasons = "; ".join(
            s["name"] for s in exit_signals[forced_pair]["signals"] if s["severity"] == "HARD"
        )
        decision = protective_exit_decision(forced_pair, cycle_id, reasons)
        result["exit_override"] = {"pair": forced_pair, "reasons": reasons}
        logger.warning("Protective exit override for %s: %s", forced_pair, reasons)

    execution_result = await execute_decision(
        decision,
        mt5,
        execution_mode=cfg.execution_mode,
        cycle_id=cycle_id,
        market_state=market_state,
        max_positions=cfg.max_positions,
        base_lot_size=cfg.base_lot_size,
        mock_execution=mock_execution,
        consecutive_losses=session.consecutive_losses,
        risk_config=cfg.risk,
        trades_today=session.trades_today,
    )

    session = end_cycle(
        session,
        account=market_state.get("account"),
        decision=decision,
        execution_result=execution_result,
    )
    save_session(session, cfg.session_state_dir)

    log_path = write_cycle_log(
        cfg,
        decision,
        market_state,
        execution_result=execution_result,
            meta=_log_meta(
            cfg,
            mock_meta,
            market_state,
            session=session.to_dict(),
            warm_reasons=warm_reasons,
            veto=veto_dict,
            skipped_llm=False,
            emergency_close=emergency_result,
            maintenance=maintenance_result,
            exit_signals=exit_signals or None,
            exit_override=result.get("exit_override"),
        ),
    )

    result.update({
        "decision": decision,
        "execution_result": execution_result,
        "log_path": str(log_path),
        "skipped_llm": False,
        "session": session.to_dict(),
    })
    update_prefilter_state(market_state, cfg.session_state_dir)
    return result


def _confidence_rank(confidence: Any) -> int:
    return {"LOW": 0, "MEDIUM": 1, "HIGH": 2}.get(str(confidence or "").upper(), 0)


def _log_meta(
    cfg: CycleConfig,
    mock_meta: bool,
    market_state: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    meta: dict[str, Any] = {"phase": cfg.phase, **extra}
    if mock_meta or cfg.mock_mode:
        meta["mock_mode"] = True
        meta["mock_llm"] = cfg.mock_llm
    if cfg.execution_mode:
        meta["execution_mode"] = True
    if market_state:
        if scenario := market_state.get("mock_scenario"):
            meta["mock_scenario"] = scenario
        eurusd = market_state.get("indicators", {}).get("EURUSD", {})
        if regime := eurusd.get("regime"):
            meta["regime"] = regime
    return meta
