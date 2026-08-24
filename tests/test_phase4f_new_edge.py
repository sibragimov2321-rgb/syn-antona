from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.core import Candle
from app.backtest.costs import PHASE4E_SPOT_PROFILES
from app.strategy_lab.features import LabFeature
from app.strategy_lab.phase4f import (
    EconomicHypothesisStrategy,
    Hypothesis,
    Regime4F,
    build_aligned_features,
    classify_regime,
    frozen_hypotheses,
    make_boundaries,
    multiple_testing_adjustment,
)
from app.strategy_lab.phase4f_framework import FinalHoldout4F


def _feature(timestamp: datetime, **changes) -> LabFeature:
    values = dict(timestamp=timestamp, price=106, rsi=60, adx=30, atr=1, atr_pct=0.01, atr_percentile=50, relative_volume=1.3, vwap=103, bollinger_width=0.01, bollinger_z=1, ema_20=105, ema_50=100, ema_200=95, trend_strength=5, momentum=0.01, momentum_acceleration=0.003, distance_from_ema=6, volatility_regime="NORMAL_VOLATILITY", trend_regime="BULL", breakout=0, mtf_alignment=4, funding_rate=None, open_interest=None, open_interest_change=None)
    values.update(changes)
    return LabFeature(**values)


def _candles(start: datetime, count: int, minutes: int) -> list[Candle]:
    return [Candle(start + timedelta(minutes=index * minutes), Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("10")) for index in range(count)]


def test_search_space_is_frozen_and_economically_documented() -> None:
    hypotheses = frozen_hypotheses()
    assert len(hypotheses) == 36
    assert {item.timeframe for item in hypotheses} == {"5m", "15m", "1h", "4h"}
    assert len({item.family for item in hypotheses}) == 9
    assert all(item.rationale for item in hypotheses)


def test_purge_and_embargo_boundaries_do_not_overlap() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    boundaries = make_boundaries(start, start + timedelta(days=730))
    assert boundaries.train_end + timedelta(days=7) == boundaries.validation_start
    assert boundaries.validation_end + timedelta(days=7) == boundaries.walk_forward_start
    assert boundaries.walk_forward_end + timedelta(days=7) == boundaries.holdout_start
    assert boundaries.train_end < boundaries.validation_start < boundaries.validation_end < boundaries.walk_forward_start < boundaries.walk_forward_end < boundaries.holdout_start < boundaries.end


def test_regime_engine_classifies_all_primary_mechanisms() -> None:
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    assert classify_regime(_feature(timestamp)) is Regime4F.TREND_UP
    assert classify_regime(_feature(timestamp, ema_20=95, ema_50=100, momentum=-0.01)) is Regime4F.TREND_DOWN
    assert classify_regime(_feature(timestamp, adx=10, momentum=0)) is Regime4F.RANGE
    assert classify_regime(_feature(timestamp, atr_percentile=90, adx=10)) is Regime4F.HIGH_VOLATILITY
    assert classify_regime(_feature(timestamp, atr_percentile=10, adx=10)) is Regime4F.LOW_VOLATILITY
    assert classify_regime(_feature(timestamp, breakout=1, atr_percentile=70)) is Regime4F.BREAKOUT


def test_cost_aware_trend_signal_includes_impact_and_safety_margin() -> None:
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    hypothesis = Hypothesis("TREND_FOLLOWING", "5m", "economic rationale")
    strategy = EconomicHypothesisStrategy(hypothesis, {timestamp: _feature(timestamp)}, PHASE4E_SPOT_PROFILES["binance"])
    candle = Candle(timestamp, Decimal("106"), Decimal("107"), Decimal("105"), Decimal("106"), Decimal("10"))
    action = strategy([candle])
    assert action is not None
    assert action.context["expected_edge"] > action.context["estimated_round_trip_cost"] + action.context["safety_margin"]
    assert action.context["estimated_market_impact"] > 0
    assert action.context["estimated_funding"] == 0


def test_no_suitable_regime_means_no_trade() -> None:
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    hypothesis = Hypothesis("TREND_FOLLOWING", "5m", "economic rationale")
    strategy = EconomicHypothesisStrategy(hypothesis, {timestamp: _feature(timestamp, adx=10, momentum=0)}, PHASE4E_SPOT_PROFILES["binance"])
    candle = Candle(timestamp, Decimal("106"), Decimal("107"), Decimal("105"), Decimal("106"), Decimal("10"))
    assert strategy([candle]) is None


def test_mtf_alignment_uses_only_closed_higher_candles() -> None:
    history_start = datetime(2024, 1, 1, tzinfo=UTC)
    research_start = history_start + timedelta(days=60)
    histories = {
        "5m": _candles(history_start, 60 * 24 * 12 + 60, 5),
        "15m": _candles(history_start, 60 * 24 * 4 + 20, 15),
        "1h": _candles(history_start, 60 * 24 + 10, 60),
        "4h": _candles(history_start, 60 * 6 + 5, 240),
    }
    features = build_aligned_features(histories, research_start, research_start + timedelta(hours=5))
    assert research_start + timedelta(hours=3, minutes=50) not in features["5m"]
    assert research_start + timedelta(hours=3, minutes=55) in features["5m"]


def test_multiple_testing_correction_penalizes_larger_search() -> None:
    summary = {"sharpe": Decimal("2")}
    small = multiple_testing_adjustment(summary, 2)
    large = multiple_testing_adjustment(summary, 36)
    assert large["bonferroni_p"] >= small["bonferroni_p"]
    assert large["deflated_sharpe"] < small["deflated_sharpe"]


def test_phase4f_holdout_opens_once_only_after_lock() -> None:
    vault = FinalHoldout4F()
    with pytest.raises(RuntimeError):
        vault.open_once("candidate")
    vault.lock("candidate")
    vault.open_once("candidate")
    with pytest.raises(RuntimeError):
        vault.open_once("candidate")
