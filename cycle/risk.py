"""Hard risk limits enforced before Phase 1 execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cycle.decision import MARKET_ORDER_TYPES, PENDING_ORDER_TYPES


@dataclass
class RiskCheckResult:
    allowed: bool = True
    checks: list[dict[str, Any]] = field(default_factory=list)

    def deny(self, name: str, detail: str = "") -> None:
        self.checks.append({"name": name, "pass": False, "detail": detail})
        self.allowed = False

    def allow(self, name: str, detail: str = "") -> None:
        self.checks.append({"name": name, "pass": True, "detail": detail})


def check_enter_risk(
    decision: dict[str, Any],
    market_state: dict[str, Any],
    *,
    max_positions: int = 3,
    min_free_margin_pct: float = 200.0,
    base_lot_size: float = 0.01,
    consecutive_losses: int = 0,
) -> RiskCheckResult:
    """Enforce hard risk limits before placing a new entry."""
    result = RiskCheckResult()
    pair = decision.get("pair", "")
    order_type = str(decision.get("order_type", "")).upper()
    lot_size = float(decision.get("lot_size", 0))

    positions = market_state.get("positions") or []
    pending = market_state.get("pending_orders") or []

    if len(positions) >= max_positions:
        result.deny("Max concurrent positions", f"{len(positions)} >= {max_positions}")

    pair_positions = [p for p in positions if p.get("symbol") == pair]
    if pair_positions:
        result.deny("One position per pair", f"{pair} already open")

    pair_pending = [o for o in pending if o.get("symbol") == pair]
    if pair_pending:
        result.deny("One pending order per pair", f"{pair} already has pending order")

    account = market_state.get("account") or {}
    margin = float(account.get("margin", 0))
    free_margin = float(account.get("free_margin", account.get("margin_free", 0)))
    if margin > 0:
        margin_level = (free_margin / margin) * 100
        if margin_level < min_free_margin_pct:
            result.deny(
                "Free margin >= 200% required",
                f"margin_level={margin_level:.1f}%",
            )
    else:
        result.allow("Free margin check", "no margin in use")

    if lot_size > base_lot_size * 2:
        result.deny("Lot size within limits", f"lot {lot_size} > 2x base {base_lot_size}")
    elif lot_size > 0:
        result.allow("Lot size within limits", str(lot_size))

    if order_type in MARKET_ORDER_TYPES and decision.get("entry_window") is None:
        result.deny(
            "Market order requires entry_window",
            "BUY/SELL must include entry_window guard",
        )
    elif order_type in PENDING_ORDER_TYPES and decision.get("entry_price") is None:
        result.deny("Pending order requires entry_price", order_type)
    else:
        result.allow("Order type valid", order_type)

    if consecutive_losses >= 3 and str(decision.get("confidence", "")).upper() != "HIGH":
        result.deny(
            "3-loss streak requires HIGH confidence",
            f"consecutive_losses={consecutive_losses}",
        )

    if result.allowed:
        result.allow("Max concurrent positions", f"{len(positions)}/{max_positions}")
        result.allow("One position per pair", f"{pair} clear")

    return result
