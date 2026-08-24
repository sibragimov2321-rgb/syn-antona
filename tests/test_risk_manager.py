from decimal import Decimal

from app.domain.models import RiskProfile, Side, TradeIntent
from app.risk.manager import RiskManager


def intent(**overrides) -> TradeIntent:
    values = dict(
        trade_id="trade-1",
        user_id=1,
        symbol="BTCUSDT",
        side=Side.LONG,
        entry=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        equity=Decimal("10000"),
        daily_realized_pnl=Decimal("0"),
        open_positions=0,
        consecutive_losses=0,
    )
    values.update(overrides)
    return TradeIntent(**values)


def test_risk_manager_sizes_conservative_long_position() -> None:
    decision = RiskManager().approve(intent(), RiskProfile())
    assert decision.approved
    assert decision.quantity == Decimal("5.000000")  # capped by $500 notional
    assert decision.risk_amount == Decimal("50.00")


def test_risk_manager_rejects_invalid_stop_loss() -> None:
    decision = RiskManager().approve(intent(stop_loss=Decimal("101")), RiskProfile())
    assert not decision.approved
    assert "Stop loss" in decision.reason


def test_daily_loss_limit_blocks_new_trade() -> None:
    decision = RiskManager().approve(intent(daily_realized_pnl=Decimal("-200")), RiskProfile())
    assert not decision.approved
    assert "Daily loss" in decision.reason


def test_maximum_positions_blocks_new_trade() -> None:
    decision = RiskManager().approve(intent(open_positions=2), RiskProfile())
    assert not decision.approved
    assert "Maximum simultaneous" in decision.reason


def test_risk_reward_filter_rejects_trade() -> None:
    decision = RiskManager().approve(intent(take_profit=Decimal("105")), RiskProfile())
    assert not decision.approved
    assert "risk/reward" in decision.reason


def test_spread_and_volatility_filters_reject_trade() -> None:
    decision = RiskManager().approve(intent(spread_pct=Decimal("0.01")), RiskProfile())
    assert not decision.approved
    assert "Spread" in decision.reason
    decision = RiskManager().approve(intent(volatility_pct=Decimal("0.1")), RiskProfile())
    assert not decision.approved
    assert "Volatility" in decision.reason
