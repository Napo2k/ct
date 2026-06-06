"""Execution layer — MT5 write tools (Phase 1 paper execution)."""

from __future__ import annotations

import logging
from typing import Any

from cycle.decision import DecisionValidationError, MARKET_ORDER_TYPES, validate_decision
from cycle.mcp_client import MCPClient, MCPClientError
from cycle.risk import check_enter_risk

logger = logging.getLogger(__name__)


async def execute_decision(
    decision: dict[str, Any],
    mt5: MCPClient | Any | None,
    *,
    execution_mode: bool,
    cycle_id: str,
    market_state: dict[str, Any] | None = None,
    max_positions: int = 3,
    base_lot_size: float = 0.01,
    mock_execution: bool = False,
    consecutive_losses: int = 0,
) -> dict[str, Any]:
    """
    Execute a validated decision against MT5 MCP write tools.

    Phase 0 (execution_mode=False): simulated result, no writes.
    Phase 1 (execution_mode=True): live or mock MT5 writes with risk guards.
    """
    validated = validate_decision(decision, cycle_id=cycle_id)
    action = validated["action"]
    state = market_state or {}

    if not execution_mode:
        return {
            "executed": False,
            "simulated": True,
            "phase": 0,
            "action": action,
            "message": "EXECUTION_MODE=false — decision logged only, no MT5 writes",
            "would_execute": _describe_action(validated),
        }

    if mt5 is None:
        return {"executed": False, "phase": 1, "error": "MT5 MCP client not available"}

    if action == "SUSPEND":
        close_result = await emergency_close_all(mt5, reason="SUSPEND decision")
        return {
            "executed": close_result.get("closed", 0) > 0,
            "phase": 1,
            "action": "SUSPEND",
            "message": "Closed all positions on SUSPEND",
            "close_result": close_result,
            "mock_execution": mock_execution,
        }

    if action in {"HOLD"}:
        return {
            "executed": False,
            "phase": 1,
            "action": action,
            "message": "No execution required",
            "mock_execution": mock_execution,
        }

    try:
        if action == "ENTER":
            return await _place_entry(
                mt5,
                validated,
                state,
                max_positions=max_positions,
                base_lot_size=base_lot_size,
                mock_execution=mock_execution,
                consecutive_losses=consecutive_losses,
            )
        if action == "EXIT":
            return await _close_positions(mt5, validated, mock_execution=mock_execution)
        if action == "MODIFY":
            return await _modify(mt5, validated, mock_execution=mock_execution)
        return {"executed": False, "phase": 1, "error": f"Unsupported action: {action}"}
    except (MCPClientError, DecisionValidationError) as exc:
        return {"executed": False, "phase": 1, "error": str(exc), "mock_execution": mock_execution}


async def emergency_close_all(
    mt5: MCPClient | Any,
    *,
    reason: str = "emergency",
) -> dict[str, Any]:
    """Close all open positions (veto emergency, Friday close, news, etc.)."""
    positions_result = await mt5.call_tool("get_open_positions")
    if not isinstance(positions_result, dict):
        return {"closed": 0, "error": "Failed to fetch positions", "reason": reason}

    positions = positions_result.get("positions", [])
    if not positions:
        return {"closed": 0, "message": "No positions to close", "reason": reason}

    results = []
    for pos in positions:
        ticket = pos.get("ticket")
        if ticket:
            close_result = await mt5.call_tool("close_position", {"ticket": ticket})
            results.append(close_result)

    logger.warning("Emergency close: %d positions (%s)", len(results), reason)
    return {"closed": len(results), "reason": reason, "results": results}


def _describe_action(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "pair": decision["pair"],
        "action": decision["action"],
        "order_type": decision["order_type"],
        "lot_size": decision["lot_size"],
        "entry_price": decision["entry_price"],
        "stop_loss": decision["stop_loss"],
        "take_profit": decision["take_profit"],
    }


async def _place_entry(
    mt5: MCPClient | Any,
    decision: dict[str, Any],
    market_state: dict[str, Any],
    *,
    max_positions: int,
    base_lot_size: float,
    mock_execution: bool,
    consecutive_losses: int = 0,
) -> dict[str, Any]:
    risk = check_enter_risk(
        decision,
        market_state,
        max_positions=max_positions,
        base_lot_size=base_lot_size,
        consecutive_losses=consecutive_losses,
    )
    if not risk.allowed:
        failed = [c for c in risk.checks if not c["pass"]]
        return {
            "executed": False,
            "phase": 1,
            "action": "ENTER",
            "error": "Risk check failed",
            "risk_checks": risk.checks,
            "blocked_by": failed[0]["name"] if failed else "unknown",
            "mock_execution": mock_execution,
        }

    args: dict[str, Any] = {
        "symbol": decision["pair"],
        "order_type": decision["order_type"],
        "lot_size": decision["lot_size"],
        "stop_loss": decision["stop_loss"],
        "take_profit": decision["take_profit"],
        "comment": "ClaudeTrader",
    }
    if decision["entry_price"] is not None:
        args["price"] = decision["entry_price"]
    if decision["entry_window"] is not None:
        args["entry_window"] = decision["entry_window"]

    order_type = str(decision.get("order_type", "")).upper()
    if order_type in MARKET_ORDER_TYPES:
        atr_err = await _validate_market_atr(mt5, decision, market_state)
        if atr_err:
            return {
                "executed": False,
                "phase": 1,
                "action": "ENTER",
                "error": atr_err,
                "risk_checks": risk.checks,
                "mock_execution": mock_execution,
            }

    result = await mt5.call_tool("place_order", args)
    success = isinstance(result, dict) and result.get("success", False)
    return {
        "executed": success,
        "phase": 1,
        "tool": "place_order",
        "result": result,
        "risk_checks": risk.checks,
        "mock_execution": mock_execution,
    }


async def _close_positions(
    mt5: MCPClient | Any,
    decision: dict[str, Any],
    *,
    mock_execution: bool,
) -> dict[str, Any]:
    positions = await mt5.call_tool("get_open_positions", {"symbol": decision["pair"]})
    if not isinstance(positions, dict) or not positions.get("positions"):
        return {
            "executed": False,
            "phase": 1,
            "message": "No positions to close",
            "mock_execution": mock_execution,
        }

    results = []
    for pos in positions["positions"]:
        ticket = pos.get("ticket")
        if ticket:
            close_result = await mt5.call_tool("close_position", {"ticket": ticket})
            results.append(close_result)

    return {
        "executed": True,
        "phase": 1,
        "tool": "close_position",
        "results": results,
        "mock_execution": mock_execution,
    }


async def _validate_market_atr(
    mt5: MCPClient | Any,
    decision: dict[str, Any],
    market_state: dict[str, Any],
) -> str | None:
    """Reject market order if price moved > 0.5×ATR since decision reference."""
    pair = decision["pair"]
    order_type = str(decision.get("order_type", "")).upper()

    indicators = market_state.get("indicators", {}).get(pair, {})
    h1 = indicators.get("H1", {})
    atr = h1.get("atr")
    if atr is None:
        return None

    reference = decision.get("entry_price")
    window = decision.get("entry_window")
    if reference is None and window and len(window) == 2:
        reference = (float(window[0]) + float(window[1])) / 2
    if reference is None:
        return None

    tick = await mt5.call_tool("get_tick", {"symbol": pair})
    if not isinstance(tick, dict) or not tick.get("success"):
        return "Market order rejected — tick unavailable for ATR validation"

    tick_data = tick.get("tick", tick)
    current = float(tick_data["ask"] if order_type == "BUY" else tick_data["bid"])
    threshold = 0.5 * float(atr)

    if abs(current - float(reference)) > threshold:
        return (
            f"Market order rejected — price moved {abs(current - float(reference)):.5f} "
            f"> 0.5×ATR ({threshold:.5f})"
        )
    return None


async def _modify(
    mt5: MCPClient | Any,
    decision: dict[str, Any],
    *,
    mock_execution: bool,
) -> dict[str, Any]:
    position = await mt5.call_tool("get_position_by_symbol", {"symbol": decision["pair"]})
    if not isinstance(position, dict) or not position.get("position"):
        return {
            "executed": False,
            "phase": 1,
            "message": "No position to modify",
            "mock_execution": mock_execution,
        }

    ticket = position["position"]["ticket"]
    args: dict[str, Any] = {"ticket": ticket}
    if decision["stop_loss"] is not None:
        args["stop_loss"] = decision["stop_loss"]
    if decision["take_profit"] is not None:
        args["take_profit"] = decision["take_profit"]

    result = await mt5.call_tool("modify_position", args)
    success = isinstance(result, dict) and result.get("success", False)
    return {
        "executed": success,
        "phase": 1,
        "tool": "modify_position",
        "result": result,
        "mock_execution": mock_execution,
    }
