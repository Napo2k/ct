"""Hard risk limits enforced before execution (Phase 1 paper / Phase 2 live)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cycle.decision import MARKET_ORDER_TYPES, PENDING_ORDER_TYPES

STANDARD_CONTRACT_SIZE = 100_000
DEFAULT_RISK_PER_TRADE_PCT = 1.0
DEFAULT_MAX_LOT_SIZE = 0.05
DEFAULT_MAX_TRADES_PER_DAY = 10

# Long-run FX pair correlations. Overridable via risk.correlations in config
# (keys "EURUSD/GBPUSD"). Sign matters: positively correlated pairs in the
# same direction stack risk, negatively correlated pairs in opposite
# directions stack risk.
DEFAULT_CORRELATIONS = {
    frozenset({"EURUSD", "GBPUSD"}): 0.85,
    frozenset({"EURUSD", "USDJPY"}): -0.30,
    frozenset({"GBPUSD", "USDJPY"}): -0.25,
}
DEFAULT_CORRELATION_THRESHOLD = 0.7
DEFAULT_MAX_NET_CURRENCY_LOTS = 0.03


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
    risk_config: dict[str, Any] | None = None,
    trades_today: int = 0,
) -> RiskCheckResult:
    """Enforce hard risk limits before placing a new entry."""
    result = RiskCheckResult()
    risk_cfg = risk_config or {}
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

    max_lot = float(risk_cfg.get("max_lot_size", DEFAULT_MAX_LOT_SIZE))
    if lot_size > max_lot:
        result.deny("Absolute lot cap", f"lot {lot_size} > max_lot_size {max_lot}")

    max_trades = int(risk_cfg.get("max_trades_per_day", DEFAULT_MAX_TRADES_PER_DAY))
    if trades_today >= max_trades:
        result.deny(
            "Max trades per day",
            f"trades_today={trades_today} >= {max_trades}",
        )

    equity = float(account.get("equity", account.get("balance", 0)))
    risk_pct = float(risk_cfg.get("risk_per_trade_pct", DEFAULT_RISK_PER_TRADE_PCT))
    risk_amount = _estimate_trade_risk(decision, market_state)
    if equity > 0 and risk_amount is not None:
        budget = equity * (risk_pct / 100.0)
        if risk_amount > budget:
            result.deny(
                f"Trade risk <= {risk_pct}% equity",
                f"risk={risk_amount:.2f} > budget={budget:.2f} (equity={equity:.2f})",
            )
        else:
            result.allow(
                f"Trade risk <= {risk_pct}% equity",
                f"risk={risk_amount:.2f} / budget={budget:.2f}",
            )
    elif equity <= 0:
        result.deny("Equity known for risk sizing", "account equity unavailable or zero")

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

    _check_correlated_exposure(result, decision, positions, risk_cfg)
    _check_currency_exposure(result, decision, positions, risk_cfg)

    if result.allowed:
        result.allow("Max concurrent positions", f"{len(positions)}/{max_positions}")
        result.allow("One position per pair", f"{pair} clear")

    return result


def _position_direction(position: dict[str, Any]) -> int:
    """+1 for long, -1 for short, 0 unknown (MT5 'type': 0=buy, 1=sell)."""
    raw = position.get("type")
    if raw in (0, "0", "BUY", "POSITION_TYPE_BUY"):
        return 1
    if raw in (1, "1", "SELL", "POSITION_TYPE_SELL"):
        return -1
    return 0


def _decision_direction(decision: dict[str, Any]) -> int:
    direction = str(decision.get("direction") or "").upper()
    if direction == "LONG":
        return 1
    if direction == "SHORT":
        return -1
    return 0


def _correlation(pair_a: str, pair_b: str, risk_cfg: dict[str, Any]) -> float:
    overrides = risk_cfg.get("correlations") or {}
    for key, value in overrides.items():
        legs = {leg.strip().upper() for leg in str(key).split("/")}
        if legs == {pair_a, pair_b}:
            return float(value)
    return DEFAULT_CORRELATIONS.get(frozenset({pair_a, pair_b}), 0.0)


def _check_correlated_exposure(
    result: RiskCheckResult,
    decision: dict[str, Any],
    positions: list[dict[str, Any]],
    risk_cfg: dict[str, Any],
) -> None:
    """Deny entries that duplicate an existing bet through a correlated pair.

    Same-direction positions in positively correlated pairs (or opposite
    directions in negatively correlated pairs) are effectively one trade —
    they breach drawdown limits together.
    """
    pair = str(decision.get("pair", "")).upper()
    direction = _decision_direction(decision)
    if not pair or direction == 0:
        return

    threshold = float(
        risk_cfg.get("correlation_threshold", DEFAULT_CORRELATION_THRESHOLD)
    )

    for position in positions:
        other = str(position.get("symbol", "")).upper()
        other_dir = _position_direction(position)
        if not other or other == pair or other_dir == 0:
            continue
        corr = _correlation(pair, other, risk_cfg)
        # Effective correlation of the two *trades* (pair correlation times
        # direction agreement): > threshold means same economic bet.
        effective = corr * direction * other_dir
        if effective >= threshold:
            result.deny(
                "Correlated exposure",
                f"{pair} {decision.get('direction')} duplicates {other} "
                f"(corr={corr:+.2f}, effective={effective:+.2f} >= {threshold})",
            )
            return

    result.allow("Correlated exposure", "no conflicting correlated positions")


def _check_currency_exposure(
    result: RiskCheckResult,
    decision: dict[str, Any],
    positions: list[dict[str, Any]],
    risk_cfg: dict[str, Any],
) -> None:
    """Cap net exposure per currency in lots across open positions + this trade.

    Long EURUSD + short USDJPY is a double-short on USD even though the pairs
    differ; summing signed lots per currency catches that.
    """
    pair = str(decision.get("pair", "")).upper()
    direction = _decision_direction(decision)
    lot = float(decision.get("lot_size", 0))
    if len(pair) != 6 or direction == 0 or lot <= 0:
        return

    max_net = float(risk_cfg.get("max_net_currency_lots", DEFAULT_MAX_NET_CURRENCY_LOTS))

    exposure: dict[str, float] = {}

    def add(symbol: str, signed_lots: float) -> None:
        if len(symbol) != 6:
            return
        base, quote = symbol[:3], symbol[3:]
        exposure[base] = exposure.get(base, 0.0) + signed_lots
        exposure[quote] = exposure.get(quote, 0.0) - signed_lots

    for position in positions:
        pos_dir = _position_direction(position)
        volume = float(position.get("volume", position.get("volume_current", 0)) or 0)
        add(str(position.get("symbol", "")).upper(), pos_dir * volume)

    add(pair, direction * lot)

    breaches = {ccy: net for ccy, net in exposure.items() if abs(net) > max_net}
    if breaches:
        detail = ", ".join(f"{ccy}={net:+.2f}" for ccy, net in sorted(breaches.items()))
        result.deny(
            f"Net currency exposure <= {max_net} lots",
            f"would breach: {detail}",
        )
    else:
        result.allow(f"Net currency exposure <= {max_net} lots", "within limits")


def _estimate_trade_risk(
    decision: dict[str, Any],
    market_state: dict[str, Any],
) -> float | None:
    """Estimate worst-case loss at the stop in account currency (assumes USD account).

    For USD-quoted pairs the quote-currency loss is the USD loss. For JPY-quoted
    pairs the JPY loss is converted at the entry price. Returns None when entry
    or stop cannot be determined — callers treat unknown risk per their mode.
    """
    pair = str(decision.get("pair", "")).upper()
    stop_loss = decision.get("stop_loss")
    lot_size = float(decision.get("lot_size", 0))
    if stop_loss is None or lot_size <= 0:
        return None

    entry = decision.get("entry_price")
    if entry is None:
        window = decision.get("entry_window")
        if window and len(window) == 2:
            entry = (float(window[0]) + float(window[1])) / 2
    if entry is None:
        tick_payload = market_state.get("ticks", {}).get(pair, {})
        tick = tick_payload.get("tick", tick_payload)
        entry = tick.get("ask") or tick.get("bid")
    if entry is None:
        return None

    entry = float(entry)
    sl_distance = abs(entry - float(stop_loss))
    if sl_distance <= 0:
        return None

    risk_quote = lot_size * STANDARD_CONTRACT_SIZE * sl_distance
    if pair.endswith("JPY") and entry > 0:
        return risk_quote / entry
    return risk_quote
