from decimal import Decimal

import pytest

from app.domain.models import RiskProfile, Side, TradeIntent
from app.risk.manager import RiskManager
from app.trading.paper import DuplicateTradeError, PaperExecutionEngine


def test_paper_engine_never_executes_twice() -> None:
    engine = PaperExecutionEngine(RiskManager())
    trade = TradeIntent(
        trade_id="same",
        user_id=1,
        symbol="ETHUSDT",
        side=Side.LONG,
        entry=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        equity=Decimal("10000"),
        daily_realized_pnl=Decimal("0"),
        open_positions=0,
        consecutive_losses=0,
    )
    fill = engine.execute(trade, RiskProfile())
    assert fill.fee > 0
    with pytest.raises(DuplicateTradeError):
        engine.execute(trade, RiskProfile())
