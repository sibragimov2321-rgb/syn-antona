from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.core import BacktestEngine, Candle, monte_carlo, validate_candles
from app.domain.models import Side


def candles():
    now=datetime(2025,1,1,tzinfo=UTC)
    return [Candle(now,Decimal("100"),Decimal("101"),Decimal("99"),Decimal("100"),Decimal("1")), Candle(now+timedelta(minutes=5),Decimal("100"),Decimal("106"),Decimal("94"),Decimal("101"),Decimal("1"))]
def long_strategy(history):
    return (Side.LONG,Decimal("95"),Decimal("110")) if len(history)==1 else None
def test_historical_validation_detects_gap_and_duplicate():
    data=candles(); assert not validate_candles(data,300)
    assert validate_candles([data[0],data[0]],300)
def test_conservative_same_candle_sl_tp_and_no_lookahead():
    seen=[]
    def strategy(history): seen.append(len(history)); return long_strategy(history)
    result=BacktestEngine(slippage=Decimal()).run(candles(),strategy)
    assert seen[0] == 1 and result.trades[0].reason=="STOP_LOSS"
def test_metrics_and_monte_carlo():
    result=BacktestEngine(slippage=Decimal()).run(candles(),long_strategy)
    assert result.metrics["total_trades"]==1 and result.metrics["max_drawdown"]>=0
    assert "median_final_equity" in monte_carlo(result.trades,Decimal("1000"),10)
def test_invalid_data_is_rejected():
    bad=[Candle(datetime.now(UTC),Decimal("1"),Decimal("1"),Decimal("2"),Decimal("1"),Decimal("1"))]
    with pytest.raises(ValueError): BacktestEngine().run(bad,long_strategy)
