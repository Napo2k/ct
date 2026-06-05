"""Decision JSON schema validation."""

from __future__ import annotations

import re
from typing import Any

VALID_ACTIONS = {"ENTER", "EXIT", "MODIFY", "HOLD", "SUSPEND"}
VALID_DIRECTIONS = {"LONG", "SHORT", None}
VALID_ORDER_TYPES = {"BUY_LIMIT", "SELL_LIMIT", "BUY", "SELL"}
VALID_CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}

REASONING_SECTIONS = [
    "VETO CHECK",
    "REGIME CLASSIFICATION",
    "SIGNAL EVALUATION",
    "RISK CALCULATION",
    "DECISION",
]

# HOLD/SUSPEND are system or no-trade outcomes — short reasoning is allowed.
RELAXED_REASONING_ACTIONS = {"HOLD", "SUSPEND"}


class DecisionValidationError(Exception):
    pass


def _require_fields(decision: dict[str, Any], fields: list[str]) -> None:
    missing = [field for field in fields if field not in decision]
    if missing:
        raise DecisionValidationError(f"Missing required fields: {', '.join(missing)}")


def validate_decision(decision: dict[str, Any], *, cycle_id: str) -> dict[str, Any]:
    """Validate and normalize a Claude decision payload."""
    if not isinstance(decision, dict):
        raise DecisionValidationError("Decision must be a JSON object")

    _require_fields(
        decision,
        [
            "action",
            "pair",
            "direction",
            "order_type",
            "lot_size",
            "entry_price",
            "entry_window",
            "stop_loss",
            "take_profit",
            "reasoning",
            "confidence",
        ],
    )

    action = str(decision["action"]).upper()
    if action not in VALID_ACTIONS:
        raise DecisionValidationError(f"Invalid action: {action}")

    pair = str(decision["pair"]).upper()
    if not re.fullmatch(r"[A-Z0-9._#-]{3,12}", pair):
        raise DecisionValidationError(f"Invalid pair: {pair}")

    direction = decision["direction"]
    if direction is not None:
        direction = str(direction).upper()
        if direction not in {"LONG", "SHORT"}:
            raise DecisionValidationError(f"Invalid direction: {direction}")

    order_type = str(decision["order_type"]).upper()
    if order_type not in VALID_ORDER_TYPES:
        raise DecisionValidationError(f"Invalid order_type: {order_type}")

    lot_size = float(decision["lot_size"])
    if lot_size < 0:
        raise DecisionValidationError("lot_size must be >= 0")

    confidence = str(decision["confidence"]).upper()
    if confidence not in VALID_CONFIDENCE:
        raise DecisionValidationError(f"Invalid confidence: {confidence}")

    entry_window = decision["entry_window"]
    if entry_window is not None:
        if not isinstance(entry_window, list) or len(entry_window) != 2:
            raise DecisionValidationError("entry_window must be [min, max] or null")
        entry_window = [float(entry_window[0]), float(entry_window[1])]

    entry_price = decision["entry_price"]
    if entry_price is not None:
        entry_price = float(entry_price)
        if entry_price <= 0:
            raise DecisionValidationError("entry_price must be positive when set")

    stop_loss = decision["stop_loss"]
    if stop_loss is not None:
        stop_loss = float(stop_loss)

    take_profit = decision["take_profit"]
    if take_profit is not None:
        take_profit = float(take_profit)

    reasoning = str(decision["reasoning"]).strip()
    if action not in RELAXED_REASONING_ACTIONS and len(reasoning) < 50:
        raise DecisionValidationError(
            "reasoning too short — ENTER/EXIT/MODIFY require ≥50 characters"
        )

    if action not in RELAXED_REASONING_ACTIONS:
        upper_reasoning = reasoning.upper()
        for section in REASONING_SECTIONS:
            if section not in upper_reasoning:
                raise DecisionValidationError(f"reasoning missing section: {section}")

    if action == "ENTER":
        if direction is None:
            raise DecisionValidationError("ENTER requires direction")
        if lot_size <= 0:
            raise DecisionValidationError("ENTER requires positive lot_size")
        if stop_loss is None or take_profit is None:
            raise DecisionValidationError("ENTER requires stop_loss and take_profit")
        _validate_rr(entry_price, stop_loss, take_profit, direction)

    normalized = {
        "action": action,
        "pair": pair,
        "direction": direction,
        "order_type": order_type,
        "lot_size": lot_size,
        "entry_price": entry_price,
        "entry_window": entry_window,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "reasoning": reasoning,
        "confidence": confidence,
        "cycle_id": decision.get("cycle_id", cycle_id),
    }
    return normalized


def _validate_rr(
    entry_price: float | None,
    stop_loss: float,
    take_profit: float,
    direction: str | None,
) -> None:
    if entry_price is None or direction is None:
        return

    if direction == "LONG":
        risk = entry_price - stop_loss
        reward = take_profit - entry_price
    else:
        risk = stop_loss - entry_price
        reward = entry_price - take_profit

    if risk <= 0 or reward <= 0:
        raise DecisionValidationError("Invalid SL/TP geometry for direction")

    ratio = reward / risk
    if ratio < 1.5:
        raise DecisionValidationError(f"R:R ratio {ratio:.2f} below 1.5 minimum")


def hold_decision(pair: str, cycle_id: str, reason: str) -> dict[str, Any]:
    return {
        "action": "HOLD",
        "pair": pair,
        "direction": None,
        "order_type": "BUY_LIMIT",
        "lot_size": 0.0,
        "entry_price": None,
        "entry_window": None,
        "stop_loss": None,
        "take_profit": None,
        "reasoning": reason,
        "confidence": "LOW",
        "cycle_id": cycle_id,
    }


def suspend_decision(cycle_id: str, reason: str, pair: str = "EURUSD") -> dict[str, Any]:
    return {
        "action": "SUSPEND",
        "pair": pair,
        "direction": None,
        "order_type": "BUY_LIMIT",
        "lot_size": 0.0,
        "entry_price": None,
        "entry_window": None,
        "stop_loss": None,
        "take_profit": None,
        "reasoning": reason,
        "confidence": "LOW",
        "cycle_id": cycle_id,
    }
