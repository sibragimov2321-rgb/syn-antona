"""Independent arithmetic controls for the Phase 4D execution audit."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import random

from app.backtest.core import BacktestEngine, BacktestResult, Candle, StrategyAction
from app.domain.models import RiskProfile, Side

MONEY_TOLERANCE = Decimal("0.00000001")
RATIO_TOLERANCE = Decimal("0.0000000001")


@dataclass(frozen=True)
class ReferenceFill:
    entry: Decimal
    exit: Decimal
    gross_after_execution: Decimal
    fees: Decimal
    slippage: Decimal
    spread: Decimal
    funding: Decimal
    net: Decimal


@dataclass(frozen=True)
class AuditCheck:
    check: str
    expected: object
    backtester: object
    difference: object
    tolerance: object
    status: str

    def to_dict(self) -> dict:
        return asdict(self)


def reference_market_trade(
    side: Side,
    entry_reference: Decimal,
    exit_reference: Decimal,
    quantity: Decimal,
    fee_rate: Decimal,
    slippage: Decimal,
    *,
    spread: Decimal = Decimal(),
    funding: Decimal = Decimal(),
) -> ReferenceFill:
    """Calculate a trade without calling or importing any engine internals."""
    adverse = slippage + spread / Decimal("2")
    if side is Side.LONG:
        entry = entry_reference * (Decimal("1") + adverse)
        exit_price = exit_reference * (Decimal("1") - adverse)
        gross = (exit_price - entry) * quantity
    else:
        entry = entry_reference * (Decimal("1") - adverse)
        exit_price = exit_reference * (Decimal("1") + adverse)
        gross = (entry - exit_price) * quantity
    fees = (entry + exit_price) * quantity * fee_rate
    slippage_cost = (entry_reference + exit_reference) * quantity * slippage
    spread_cost = (entry_reference + exit_reference) * quantity * spread / Decimal("2")
    return ReferenceFill(
        entry,
        exit_price,
        gross,
        fees,
        slippage_cost,
        spread_cost,
        funding,
        gross - fees - funding,
    )


def compare(check: str, expected: Decimal, actual: Decimal, tolerance: Decimal = MONEY_TOLERANCE) -> AuditCheck:
    difference = abs(expected - actual)
    return AuditCheck(check, expected, actual, difference, tolerance, "PASS" if difference <= tolerance else "FAIL")


def compare_exact(check: str, expected: object, actual: object) -> AuditCheck:
    return AuditCheck(check, expected, actual, 0 if expected == actual else 1, 0, "PASS" if expected == actual else "FAIL")


def one_trade_control(
    side: Side,
    entry_reference: Decimal,
    exit_reference: Decimal,
    *,
    fee_rate: Decimal = Decimal("0.0006"),
    slippage: Decimal = Decimal("0.0002"),
    spread: Decimal = Decimal(),
) -> tuple[BacktestResult, ReferenceFill]:
    started = datetime(2025, 1, 1, tzinfo=UTC)
    upward = max(entry_reference, exit_reference)
    downward = min(entry_reference, exit_reference)
    candles = [
        Candle(started, entry_reference, entry_reference, entry_reference, entry_reference, Decimal("1")),
        Candle(started + timedelta(minutes=5), entry_reference, upward, downward, exit_reference, Decimal("1")),
    ]
    used = False

    def strategy(_history):
        nonlocal used
        if used:
            return None
        used = True
        if side is Side.LONG:
            return StrategyAction(side, entry_reference / Decimal("2"), entry_reference * Decimal("3"))
        return StrategyAction(side, entry_reference * Decimal("1.49"), entry_reference / Decimal("100"))

    profile = RiskProfile(
        risk_per_trade_pct=Decimal("1"),
        max_position_notional=Decimal("1000000"),
        max_leverage=Decimal("2"),
        max_daily_loss_pct=Decimal("1"),
        min_risk_reward=Decimal("2"),
    )
    result = BacktestEngine(fee_rate=fee_rate, slippage=slippage, spread=spread).run(
        candles,
        strategy,
        Decimal("100000"),
        risk_profile=profile,
        retain_equity=True,
    )
    trade = result.trades[0]
    reference = reference_market_trade(side, entry_reference, exit_reference, trade.quantity, fee_rate, slippage, spread=spread)
    return result, reference


def buy_and_hold_control(candles: list[Candle], fee_rate: Decimal = Decimal("0.0006"), slippage: Decimal = Decimal("0.0002")) -> tuple[list[AuditCheck], BacktestResult]:
    if len(candles) < 2:
        raise ValueError("BUY & HOLD control requires at least two candles")
    first = candles[0].close
    used = False

    def strategy(_history):
        nonlocal used
        if used:
            return None
        used = True
        return StrategyAction(Side.LONG, first / Decimal("100"), first * Decimal("4"))

    profile = RiskProfile(
        risk_per_trade_pct=Decimal("1"),
        max_position_notional=Decimal("1000000000"),
        max_leverage=Decimal("1"),
        max_daily_loss_pct=Decimal("1"),
        min_risk_reward=Decimal("2"),
    )
    result = BacktestEngine(fee_rate=fee_rate, slippage=slippage).run(candles, strategy, Decimal("1000000"), risk_profile=profile, retain_equity=False)
    trade = result.trades[0]
    reference = reference_market_trade(Side.LONG, first, candles[-1].close, trade.quantity, fee_rate, slippage)
    gross_return = (candles[-1].close - first) / first
    engine_gross_return = trade.pnl_before_costs / (first * trade.quantity)
    checks = [
        compare("CONTROL 1 BUY & HOLD gross return", gross_return, engine_gross_return, RATIO_TOLERANCE),
        compare("CONTROL 1 BUY & HOLD net PnL", reference.net, trade.pnl),
    ]
    return checks, result


def synthetic_audit() -> list[AuditCheck]:
    checks: list[AuditCheck] = []
    for label, side, entry, exit_price in (
        ("CONTROL 2 ALWAYS LONG", Side.LONG, Decimal("100"), Decimal("110")),
        ("CONTROL 3 ALWAYS SHORT", Side.SHORT, Decimal("100"), Decimal("90")),
    ):
        result, reference = one_trade_control(side, entry, exit_price)
        trade = result.trades[0]
        checks.extend(
            [
                compare(f"{label} entry fill", reference.entry, trade.entry),
                compare(f"{label} exit fill", reference.exit, trade.exit),
                compare(f"{label} PnL", reference.net, trade.pnl),
                compare(f"{label} taker fees once per leg", reference.fees, trade.fees),
                compare(f"{label} slippage once per leg", reference.slippage, trade.slippage_cost),
                compare(f"{label} final equity", result.starting_balance + reference.net, result.final_equity),
            ]
        )

    random_side = random.Random(42).choice((Side.LONG, Side.SHORT))
    first, reference = one_trade_control(random_side, Decimal("100"), Decimal("103"))
    second, _ = one_trade_control(random.Random(42).choice((Side.LONG, Side.SHORT)), Decimal("100"), Decimal("103"))
    checks.append(compare_exact("CONTROL 4 RANDOM seed=42 side", random_side, first.trades[0].side))
    checks.append(compare("CONTROL 4 RANDOM reproducible PnL", first.trades[0].pnl, second.trades[0].pnl))
    checks.append(compare("CONTROL 4 RANDOM reference PnL", reference.net, first.trades[0].pnl))
    return checks
