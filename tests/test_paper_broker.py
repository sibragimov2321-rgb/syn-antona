from decimal import Decimal

import pytest

from app.demo import DemoAutotrader
from app.domain.models import BotState, RiskProfile, Side, TradeIntent
from app.trading.paper import DuplicateTradeError
from app.trading.paper_broker import PaperBroker
from app.trading.positions import PositionManager


def intent(identifier: str = "paper-1", **kwargs) -> TradeIntent:
    values = dict(
        trade_id=identifier, user_id=1, symbol="BTCUSDT", side=Side.LONG,
        entry=Decimal("100"), stop_loss=Decimal("95"), take_profit=Decimal("110"),
        equity=Decimal("10000"), daily_realized_pnl=Decimal("0"), open_positions=0,
        consecutive_losses=0,
    )
    values.update(kwargs)
    return TradeIntent(**values)


def test_commission_slippage_and_pnl_are_applied() -> None:
    broker = PaperBroker(fee_rate=Decimal("0.001"), slippage_rate=Decimal("0.01"))
    position = broker.open_market(intent(), RiskProfile())
    assert position.entry_price == Decimal("101.00")
    assert position.entry_fee > 0
    closed = broker.close_market(position.trade_id, Decimal("110"), "MANUAL")
    assert closed.exit_price == Decimal("108.90")
    assert closed.realized_pnl == Decimal("38.46")
    assert broker.account().available_balance >= 0


def test_stop_loss_take_profit_and_duplicate_protection() -> None:
    broker = PaperBroker()
    broker.open_market(intent(), RiskProfile())
    closed = PositionManager().on_price(broker, "BTCUSDT", Decimal("94"))
    assert closed[0].reason == "STOP_LOSS"
    with pytest.raises(DuplicateTradeError):
        broker.open_market(intent(), RiskProfile())
    target = broker.open_market(intent("paper-2"), RiskProfile())
    assert PositionManager().on_price(broker, "BTCUSDT", Decimal("111"))[0].reason == "TAKE_PROFIT"
    assert target.trade_id == "paper-2"


def test_emergency_stop_prevents_new_entries() -> None:
    demo = DemoAutotrader()
    demo.start()
    demo.emergency_stop()
    assert demo.state is BotState.EMERGENCY_STOP
    assert demo.emergency_stop(close_positions=True) == []
