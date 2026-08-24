from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.backtest.audit import reference_market_trade, synthetic_audit
from app.backtest.costs import BYBIT_SPOT_NON_VIP
from app.backtest.core import BacktestEngine, Candle, StrategyAction, monte_carlo
from app.backtest.orchestrator import MultiTimeframeAligner
from app.backtest.persistence import BacktestRepository
from app.db import Base
from app.domain.models import RiskProfile, Side
from app.strategy_lab.features import LabFeatureBuilder


def _flat(start: datetime, count: int, minutes: int, price: Decimal = Decimal("100")) -> list[Candle]:
    return [
        Candle(start + timedelta(minutes=index * minutes), price, price + 1, price - 1, price, Decimal("10"))
        for index in range(count)
    ]


def test_independent_long_short_fee_slippage_and_random_controls() -> None:
    checks = synthetic_audit()
    assert checks
    assert all(check.status == "PASS" for check in checks), [check for check in checks if check.status != "PASS"]


def test_reference_formula_does_not_double_charge_costs() -> None:
    result = reference_market_trade(Side.LONG, Decimal("100"), Decimal("110"), Decimal("2"), Decimal("0.001"), Decimal("0.002"))
    assert result.entry == Decimal("100.200")
    assert result.exit == Decimal("109.780")
    assert result.fees == (result.entry + result.exit) * Decimal("2") * Decimal("0.001")
    assert result.net == result.gross_after_execution - result.fees


def test_bybit_spot_market_orders_use_spot_taker_not_derivatives_fee() -> None:
    assert BYBIT_SPOT_NON_VIP.market_type == "spot"
    assert BYBIT_SPOT_NON_VIP.maker_fee == Decimal("0.001")
    assert BYBIT_SPOT_NON_VIP.taker_fee == Decimal("0.001")


def test_spot_rejects_funding_and_perpetual_funding_is_directional() -> None:
    with pytest.raises(ValueError, match="Spot datasets"):
        BacktestEngine(funding_rate=Decimal("0.0001"))

    start = datetime(2025, 1, 1, 7, 55, tzinfo=UTC)
    candles = _flat(start, 98, 5)

    def run(side: Side):
        used = False

        def strategy(_history):
            nonlocal used
            if used:
                return None
            used = True
            return StrategyAction(side, Decimal("50") if side is Side.LONG else Decimal("149"), Decimal("200") if side is Side.LONG else Decimal("1"))

        return BacktestEngine(
            fee_rate=Decimal(),
            slippage=Decimal(),
            funding_rate=Decimal("0.001"),
            market_type="perpetual",
        ).run(candles, strategy)

    long = run(Side.LONG).trades[0]
    short = run(Side.SHORT).trades[0]
    expected = long.entry * long.quantity * Decimal("0.001")
    assert long.funding == expected
    assert short.funding == -(short.entry * short.quantity * Decimal("0.001"))
    assert long.pnl == -expected
    assert short.pnl == -short.funding


def test_position_sizing_uses_fill_risk_equity_and_leverage_cap() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    candles = _flat(start, 2, 5)
    profile = RiskProfile(
        risk_per_trade_pct=Decimal("1"),
        max_position_notional=Decimal("10000"),
        max_leverage=Decimal("2"),
        max_daily_loss_pct=Decimal("1"),
    )

    def run(leverage: Decimal):
        used = False

        def strategy(_history):
            nonlocal used
            if used:
                return None
            used = True
            return StrategyAction(Side.LONG, Decimal("50"), Decimal("200"))

        return BacktestEngine(fee_rate=Decimal(), slippage=Decimal()).run(
            candles,
            strategy,
            Decimal("100"),
            risk_profile=profile,
            leverage=leverage,
        ).trades[0]

    assert run(Decimal("1")).quantity == Decimal("1.000000")
    assert run(Decimal("2")).quantity == Decimal("2.000000")


def test_equity_includes_entry_fee_before_close() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    candles = _flat(start, 3, 5)
    used = False

    def strategy(_history):
        nonlocal used
        if used:
            return None
        used = True
        return StrategyAction(Side.LONG, Decimal("50"), Decimal("200"))

    result = BacktestEngine(fee_rate=Decimal("0.001"), slippage=Decimal()).run(candles, strategy)
    trade = result.trades[0]
    expected_entry_fee = trade.entry * trade.quantity * Decimal("0.001")
    assert result.equity_points[0].equity == result.starting_balance - expected_entry_fee


def test_sl_tp_same_candle_is_conservative_and_close_is_single_trade() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    candles = [
        Candle(start, Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("1")),
        Candle(start + timedelta(minutes=5), Decimal("100"), Decimal("106"), Decimal("94"), Decimal("100"), Decimal("1")),
    ]
    used = False

    def strategy(_history):
        nonlocal used
        if used:
            return None
        used = True
        return StrategyAction(Side.LONG, Decimal("95"), Decimal("105"))

    result = BacktestEngine(fee_rate=Decimal(), slippage=Decimal(), risk_manager=None).run(
        candles,
        strategy,
        risk_profile=RiskProfile(min_risk_reward=Decimal("1")),
    )
    assert len(result.trades) == 1
    assert result.trades[0].reason == "STOP_LOSS"


def test_trailing_stop_moves_only_toward_long_position() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    candles = [
        Candle(start, Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("1")),
        Candle(start + timedelta(minutes=5), Decimal("100"), Decimal("106"), Decimal("99"), Decimal("105"), Decimal("1")),
        Candle(start + timedelta(minutes=10), Decimal("105"), Decimal("105"), Decimal("103"), Decimal("104"), Decimal("1")),
    ]
    used = False

    def strategy(_history):
        nonlocal used
        if used:
            return None
        used = True
        return StrategyAction(Side.LONG, Decimal("95"), Decimal("120"), trailing_distance=Decimal("2"))

    trade = BacktestEngine(fee_rate=Decimal(), slippage=Decimal()).run(candles, strategy).trades[0]
    assert trade.reason == "STOP_LOSS"
    assert trade.exit == Decimal("104")


def test_decision_and_trade_timestamps_are_candle_close_times_without_lookahead() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    candles = _flat(start, 2, 5)
    seen = []
    used = False

    def strategy(history):
        nonlocal used
        seen.append((len(history), history[-1].timestamp))
        with pytest.raises(IndexError):
            _ = history[len(history)]
        if used:
            return None
        used = True
        return StrategyAction(Side.LONG, Decimal("50"), Decimal("200"))

    trade = BacktestEngine(fee_rate=Decimal(), slippage=Decimal()).run(candles, strategy).trades[0]
    assert seen[0] == (1, start)
    assert trade.entry_time == start + timedelta(minutes=5)
    assert trade.exit_time == start + timedelta(minutes=10)


def test_all_higher_timeframes_require_fully_closed_candles() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    histories = {
        "15m": _flat(start, 2, 15),
        "1h": _flat(start, 2, 60),
        "4h": _flat(start, 2, 240),
    }
    aligner = MultiTimeframeAligner(histories)
    for timeframe, minutes in (("15m", 15), ("1h", 60), ("4h", 240)):
        assert aligner.closed_history(timeframe, start + timedelta(minutes=minutes) - timedelta(seconds=1)) == []
        assert aligner.closed_history(timeframe, start + timedelta(minutes=minutes)) == [histories[timeframe][0]]


def test_lab_warmup_emits_no_feature_before_ema200() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    histories = {
        "5m": _flat(start, 199, 5),
        "15m": _flat(start, 199, 15),
        "1h": _flat(start, 199, 60),
        "4h": _flat(start, 199, 240),
    }
    end = start + timedelta(days=100)
    assert LabFeatureBuilder().build(histories, start, end) == {}


def test_separate_spread_and_funding_fields_survive_persistence(tmp_path) -> None:
    database = create_engine(f"sqlite:///{tmp_path / 'phase4d.db'}")
    Base.metadata.create_all(database)
    sessions = sessionmaker(bind=database, expire_on_commit=False)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    candles = _flat(start, 2, 5)
    used = False

    def strategy(_history):
        nonlocal used
        if used:
            return None
        used = True
        return StrategyAction(Side.LONG, Decimal("50"), Decimal("200"))

    result = BacktestEngine(spread=Decimal("0.002")).run(candles, strategy)
    simulation = monte_carlo(result.trades, Decimal("1000"), 1000)
    repository = BacktestRepository(sessions)
    repository.save(result, "bybit", "BTC/USDT", "LOW", "AUDIT", {}, simulation)
    loaded = repository.load_result(result.run_id)
    assert loaded is not None
    assert loaded.trades[0].spread_cost == result.trades[0].spread_cost
    assert loaded.trades[0].funding == result.trades[0].funding
