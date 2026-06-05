"""Execution layer — MT5 write tools (disabled in Phase 0)."""

from __future__ import annotations

import logging
from typing import Any

from cycle.decision import DecisionValidationError, validate_decision
from cycle.mcp_client import MCPClient, MCPClientError

logger = logging.getLogger(__name__)


async def execute_decision(
    decision: dict[str, Any],
    mt5: MCPClient | None,
    *,
    execution_mode: bool,
    cycle_id: str,
) -> dict[str, Any]:
    """
    Execute a validated decision against MT5 MCP write tools.

    Phase 0 (execution_mode=False): returns simulated result, no writes.
    """
    validated = validate_decision(decision, cycle_id=cycle_id)
    action = validated["action"]

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
        return {"executed": False, "error": "MT5 MCP client not available"}

    if action in {"HOLD", "SUSPEND"}:
        return {"executed": False, "action": action, "message": "No execution required"}

    try:
        if action == "ENTER":
            return await _place_entry(mt5, validated)
        if action == "EXIT":
            return await _close_positions(mt5, validated)
        if action == "MODIFY":
            return await _modify(mt5, validated)
        return {"executed": False, "error": f"Unsupported action: {action}"}
    except (MCPClientError, DecisionValidationError) as exc:
        return {"executed": False, "error": str(exc)}


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


async def _place_entry(mt5: MCPClient, decision: dict[str, Any]) -> dict[str, Any]:
    args = {
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

    result = await mt5.call_tool("place_order", args)
    return {"executed": True, "tool": "place_order", "result": result}


async def _close_positions(mt5: MCPClient, decision: dict[str, Any]) -> dict[str, Any]:
    positions = await mt5.call_tool("get_open_positions", {"symbol": decision["pair"]})
    if not isinstance(positions, dict) or not positions.get("positions"):
        return {"executed": False, "message": "No positions to close"}

    results = []
    for pos in positions["positions"]:
        ticket = pos.get("ticket")
        if ticket:
            close_result = await mt5.call_tool("close_position", {"ticket": ticket})
            results.append(close_result)

    return {"executed": True, "tool": "close_position", "results": results}


async def _modify(mt5: MCPClient, decision: dict[str, Any]) -> dict[str, Any]:
    position = await mt5.call_tool("get_position_by_symbol", {"symbol": decision["pair"]})
    if not isinstance(position, dict) or not position.get("position"):
        return {"executed": False, "message": "No position to modify"}

    ticket = position["position"]["ticket"]
    args = {"ticket": ticket}
    if decision["stop_loss"] is not None:
        args["stop_loss"] = decision["stop_loss"]
    if decision["take_profit"] is not None:
        args["take_profit"] = decision["take_profit"]

    result = await mt5.call_tool("modify_position", args)
    return {"executed": True, "tool": "modify_position", "result": result}
