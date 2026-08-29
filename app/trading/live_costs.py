"""Deterministic cost-aware gates shared by AI preview and exchange guard."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.exchanges.models import OrderSide


MINIMUM_NET_RR = Decimal("1.5")
EXPECTED_MOVE_COST_BUFFER = Decimal("1.25")
MAX_REASONABLE_TAKER_FEE_RATE = Decimal("0.01")


@dataclass(frozen=True)
class LiveCostEstimate:
    taker_fee_rate: Decimal
    entry_fee: Decimal
    target_exit_fee: Decimal
    stop_exit_fee: Decimal
    spread_cost: Decimal
    target_slippage: Decimal
    stop_slippage: Decimal
    gross_reward: Decimal
    gross_risk: Decimal
    expected_total_cost: Decimal
    maximum_loss_cost: Decimal
    net_reward: Decimal
    net_risk: Decimal
    net_rr: Decimal


def validate_taker_fee_rate(value: Decimal) -> Decimal:
    rate = Decimal(value)
    if rate <= 0 or rate > MAX_REASONABLE_TAKER_FEE_RATE:
        raise ValueError("Bybit taker fee rate is invalid")
    return rate


def estimate_live_costs(
    *,
    side: OrderSide,
    quantity: Decimal,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    bid: Decimal,
    ask: Decimal,
    taker_fee_rate: Decimal,
    slippage_per_leg: Decimal,
) -> LiveCostEstimate:
    """Return conservative round-trip costs and NET reward/risk.

    Entry is already a fresh executable ask for LONG or bid for SHORT.  The
    explicit spread allowance covers the second marketable leg without
    pretending that the future exit book is known.
    """

    rate = validate_taker_fee_rate(taker_fee_rate)
    values = (quantity, entry, stop, target, bid, ask)
    if any(value <= 0 for value in values) or ask < bid or slippage_per_leg < 0:
        raise ValueError("Live cost inputs are invalid")
    if side is OrderSide.BUY:
        gross_risk = quantity * (entry - stop)
        gross_reward = quantity * (target - entry)
    else:
        gross_risk = quantity * (stop - entry)
        gross_reward = quantity * (entry - target)
    if gross_risk <= 0 or gross_reward <= 0:
        raise ValueError("SL/TP ordering is invalid")

    entry_fee = quantity * entry * rate
    target_exit_fee = quantity * target * rate
    stop_exit_fee = quantity * stop * rate
    spread_cost = quantity * (ask - bid)
    target_slippage = quantity * (entry + target) * slippage_per_leg
    stop_slippage = quantity * (entry + stop) * slippage_per_leg
    expected_total_cost = entry_fee + target_exit_fee + spread_cost + target_slippage
    maximum_loss_cost = entry_fee + stop_exit_fee + spread_cost + stop_slippage
    net_reward = gross_reward - expected_total_cost
    net_risk = gross_risk + maximum_loss_cost
    net_rr = net_reward / net_risk if net_reward > 0 and net_risk > 0 else Decimal()
    return LiveCostEstimate(
        rate,
        entry_fee,
        target_exit_fee,
        stop_exit_fee,
        spread_cost,
        target_slippage,
        stop_slippage,
        gross_reward,
        gross_risk,
        expected_total_cost,
        maximum_loss_cost,
        net_reward,
        net_risk,
        net_rr,
    )


def require_cost_aware_edge(costs: LiveCostEstimate) -> None:
    if costs.gross_reward < costs.expected_total_cost * EXPECTED_MOVE_COST_BUFFER:
        raise ValueError("Expected move does not clear round-trip costs plus safety buffer")
    if costs.net_rr < MINIMUM_NET_RR:
        raise ValueError(f"NET R/R {costs.net_rr:.4f} is below 1.5")
