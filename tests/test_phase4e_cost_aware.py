from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.core import BacktestEngine, Candle, StrategyAction
from app.backtest.costs import PHASE4E_SPOT_PROFILES
from app.domain.models import Side
from app.strategy_lab.features import LabFeature
from app.strategy_lab.phase4e import (
    CostAwareConfig,
    CostAwareMeanReversion,
    ExecutionMode,
    PendingLimit,
    frozen_candidate_grid,
)
from app.strategy_lab.phase4e_framework import FinalHoldout4E


def _feature(timestamp: datetime, *, price: float = 100, z: float = -2.5, rsi: float = 25) -> LabFeature:
    return LabFeature(timestamp, price, rsi, 10, 1, 0.01, 30, 1, 102, 0.04, z, 103, 102, 102, 0.3, -0.01, 0.01, -2, "NORMAL_VOLATILITY", "SIDEWAYS", 0, 0, None, None, None)


def _config(mode: ExecutionMode = ExecutionMode.TAKER_ONLY, buffer: str = "1.5") -> CostAwareConfig:
    return CostAwareConfig("TEST", "5m", Decimal(buffer), Decimal("0.003"), execution=mode)


def test_candidate_grid_is_frozen_across_three_timeframes() -> None:
    grid = frozen_candidate_grid()
    assert len(grid) == 24
    assert {item.timeframe for item in grid} == {"5m", "15m", "1h"}
    assert all(item.execution is ExecutionMode.TAKER_ONLY for item in grid)


def test_cost_aware_gate_records_full_cost_decomposition() -> None:
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    strategy = CostAwareMeanReversion("BTC/USDT", _config(), {timestamp: _feature(timestamp)}, PHASE4E_SPOT_PROFILES["bybit"])
    candle = Candle(timestamp, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("10"))
    action = strategy([candle])
    assert action is not None
    assert action.side is Side.LONG
    assert {"expected_move", "estimated_entry_fee", "estimated_exit_fee", "estimated_spread", "estimated_slippage", "estimated_total_cost", "expected_net_edge"} <= action.context.keys()
    assert action.context["expected_net_edge"] > 0


def test_excessive_cost_buffer_rejects_trade() -> None:
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    strategy = CostAwareMeanReversion("BTC/USDT", _config(buffer="20"), {timestamp: _feature(timestamp)}, PHASE4E_SPOT_PROFILES["bybit"])
    candle = Candle(timestamp, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("10"))
    assert strategy([candle]) is None


def test_maker_limit_touch_is_not_a_fill_and_probability_is_deterministic() -> None:
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    strategy = CostAwareMeanReversion("BTC/USDT", _config(ExecutionMode.MAKER_PREFERRED), {}, PHASE4E_SPOT_PROFILES["bybit"])
    action = StrategyAction(Side.LONG, Decimal("95"), Decimal("110"))
    pending = PendingLimit(action, Decimal("100"), timestamp, 2, 1.0)
    touch = Candle(timestamp + timedelta(minutes=5), Decimal("101"), Decimal("102"), Decimal("100"), Decimal("101"), Decimal("10"))
    through = Candle(timestamp + timedelta(minutes=5), Decimal("101"), Decimal("102"), Decimal("99"), Decimal("101"), Decimal("10"))
    assert not strategy._limit_filled(touch, pending)
    assert strategy._limit_filled(through, pending) == strategy._limit_filled(through, pending)


def test_unfilled_maker_limit_expires_as_no_trade() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    candles = [Candle(start + timedelta(minutes=5 * index), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("10")) for index in range(4)]
    features = {candle.timestamp: _feature(candle.timestamp) for candle in candles}
    strategy = CostAwareMeanReversion("BTC/USDT", _config(ExecutionMode.MAKER_PREFERRED), features, PHASE4E_SPOT_PROFILES["bybit"])
    result = BacktestEngine(fee_rate=Decimal("0.001"), slippage=Decimal("0.0002"), spread=Decimal("0.0001")).run(candles, strategy)
    assert result.trades == []
    assert strategy.unfilled_limits >= 1


def test_maker_entry_override_uses_maker_fee_and_no_entry_slippage() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    candles = [
        Candle(start, Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("10")),
        Candle(start + timedelta(minutes=5), Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("10")),
    ]
    used = False

    def strategy(_history):
        nonlocal used
        if used:
            return None
        used = True
        return StrategyAction(Side.LONG, Decimal("95"), Decimal("110"), entry_price_override=Decimal("99.9"), entry_fee_rate=Decimal("0.0008"))

    trade = BacktestEngine(fee_rate=Decimal("0.001"), slippage=Decimal("0.0002"), spread=Decimal("0.0001")).run(candles, strategy).trades[0]
    expected_fees = trade.entry * trade.quantity * Decimal("0.0008") + trade.exit * trade.quantity * Decimal("0.001")
    assert trade.entry == Decimal("99.9")
    assert trade.fees == expected_fees
    assert trade.slippage_cost == candles[-1].close * trade.quantity * Decimal("0.0002")


def test_final_holdout_can_open_exactly_once_for_locked_candidate() -> None:
    vault = FinalHoldout4E()
    vault.lock("candidate")
    vault.open_once("candidate")
    with pytest.raises(RuntimeError):
        vault.open_once("candidate")
    assert vault.opened


def test_exchange_profiles_are_spot_and_do_not_fake_discounted_fees() -> None:
    assert set(PHASE4E_SPOT_PROFILES) == {"bybit", "binance", "okx", "bitget"}
    assert all(profile.market_type == "spot" for profile in PHASE4E_SPOT_PROFILES.values())
    assert PHASE4E_SPOT_PROFILES["bybit"].taker_fee == Decimal("0.001")
    assert PHASE4E_SPOT_PROFILES["okx"].maker_fee == Decimal("0.0008")
