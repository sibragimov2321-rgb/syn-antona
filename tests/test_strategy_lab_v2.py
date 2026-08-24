from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.core import BacktestEngine, Candle
from app.domain.models import RiskProfile
from app.strategy_lab.ai_provider import FrozenResearchAIProvider
from app.strategy_lab.execution import ResearchVariant, action_for, action_from_vote
from app.strategy_lab.features import AuxiliaryMarketSeries, LabFeature, LabFeatureBuilder
from app.strategy_lab.framework import FinalHoldoutVault
from app.strategy_lab.strategies import (
    LabDirection,
    MeanReversionStrategy,
    StrategyEnsemble,
    StrategyVote,
    TrendFollowingStrategy,
    WAIT,
    strategy_families,
)


def _feature(**overrides) -> LabFeature:
    values = {
        "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        "price": 105.0,
        "rsi": 60.0,
        "adx": 30.0,
        "atr": 2.0,
        "atr_pct": 0.019,
        "atr_percentile": 60.0,
        "relative_volume": 1.4,
        "vwap": 102.0,
        "bollinger_width": 0.02,
        "bollinger_z": 1.0,
        "ema_20": 103.0,
        "ema_50": 101.0,
        "ema_200": 95.0,
        "trend_strength": 3.0,
        "momentum": 0.01,
        "momentum_acceleration": 0.003,
        "distance_from_ema": 2.0,
        "volatility_regime": "NORMAL_VOLATILITY",
        "trend_regime": "BULL",
        "breakout": 1,
        "mtf_alignment": 4,
        "funding_rate": None,
        "open_interest": None,
        "open_interest_change": None,
    }
    values.update(overrides)
    return LabFeature(**values)


def _candles(start: datetime, count: int, minutes: int) -> list[Candle]:
    candles = []
    price = Decimal("100")
    for index in range(count):
        close = price + Decimal("0.1")
        candles.append(Candle(start + timedelta(minutes=minutes * index), price, close + 1, price - 1, close, Decimal("100") + index))
        price = close
    return candles


def test_all_seven_independent_strategies_plus_ensemble_exist():
    names = [strategy.name for strategy in strategy_families()]
    assert names == [
        "TREND_FOLLOWING",
        "BREAKOUT",
        "MEAN_REVERSION",
        "MOMENTUM",
        "VOLATILITY_EXPANSION",
        "REGIME_ADAPTIVE",
        "MULTI_TIMEFRAME_TREND",
        "STRATEGY_ENSEMBLE",
    ]


def test_trend_long_short_and_mean_reversion_wait():
    strategy = TrendFollowingStrategy()
    assert strategy.evaluate(_feature()).direction is LabDirection.LONG
    short = _feature(price=90, ema_20=92, ema_50=95, ema_200=100, trend_regime="BEAR", momentum=-0.01, mtf_alignment=-4)
    assert strategy.evaluate(short).direction is LabDirection.SHORT
    assert MeanReversionStrategy().evaluate(_feature()).direction is LabDirection.WAIT


def test_mean_reversion_long_and_short():
    strategy = MeanReversionStrategy()
    long = _feature(rsi=24, adx=15, atr_percentile=30, bollinger_z=-2.2, distance_from_ema=-2, trend_regime="SIDEWAYS")
    short = _feature(rsi=76, adx=15, atr_percentile=30, bollinger_z=2.2, distance_from_ema=2, trend_regime="SIDEWAYS")
    assert strategy.evaluate(long).direction is LabDirection.LONG
    assert strategy.evaluate(short).direction is LabDirection.SHORT


class _FixedStrategy:
    def __init__(self, direction: LabDirection):
        self.direction = direction

    def evaluate(self, _feature):
        return WAIT if self.direction is LabDirection.WAIT else StrategyVote(self.direction, 80, 0.03, 1.5, 2, ("fixed",))


def test_ensemble_requires_agreement_and_rejects_conflict():
    agreeing = StrategyEnsemble((_FixedStrategy(LabDirection.LONG), _FixedStrategy(LabDirection.LONG)))
    conflict = StrategyEnsemble((_FixedStrategy(LabDirection.LONG), _FixedStrategy(LabDirection.SHORT)))
    assert agreeing.evaluate(_feature()).direction is LabDirection.LONG
    short = StrategyEnsemble((_FixedStrategy(LabDirection.SHORT), _FixedStrategy(LabDirection.SHORT)))
    assert short.evaluate(_feature()).direction is LabDirection.SHORT
    assert conflict.evaluate(_feature()).direction is LabDirection.WAIT


def test_edge_must_survive_fees_and_slippage():
    vote = StrategyVote(LabDirection.LONG, 80, 0.001, 1.5, 2, ("weak after costs",))
    assert action_from_vote(vote, _feature(), Decimal("0.0006"), Decimal("0.0002")) is None
    strong = StrategyVote(LabDirection.LONG, 80, 0.02, 1.5, 2, ("edge",))
    assert action_from_vote(strong, _feature(), Decimal("0.0006"), Decimal("0.0002")) is not None


def test_ai_provider_is_analysis_only_and_exact_output_shape():
    provider = FrozenResearchAIProvider()
    result = provider.analyze(_feature())
    assert set(result.__dataclass_fields__) == {"direction", "confidence", "market_regime", "risk_flags", "reasoning_summary"}
    assert not hasattr(provider, "create_order")
    assert result.direction is LabDirection.LONG


def test_ai_signal_cannot_bypass_deterministic_risk_manager():
    feature = _feature(atr=10, atr_pct=0.10)
    provider = FrozenResearchAIProvider()
    action = action_for(TrendFollowingStrategy(), ResearchVariant.AI_SIGNAL_RISK, feature, provider, Decimal("0.0006"), Decimal("0.0002"))
    assert action is not None
    start = feature.timestamp
    candles = [
        Candle(start, Decimal("105"), Decimal("106"), Decimal("104"), Decimal("105"), Decimal("10")),
        Candle(start + timedelta(minutes=5), Decimal("105"), Decimal("106"), Decimal("104"), Decimal("105"), Decimal("10")),
    ]
    result = BacktestEngine().run(candles, lambda _history: action, risk_profile=RiskProfile(max_volatility_pct=Decimal("0.04")))
    assert result.trades == []


def test_feature_warmup_and_optional_derivatives_alignment():
    anchor = datetime(2025, 2, 15, tzinfo=UTC)
    histories = {
        "4h": _candles(anchor - timedelta(hours=4 * 210), 210, 240),
        "1h": _candles(anchor - timedelta(hours=210), 210, 60),
        "15m": _candles(anchor - timedelta(minutes=15 * 210), 210, 15),
        "5m": _candles(anchor - timedelta(minutes=5 * 210), 210, 5),
    }
    funding_time = anchor - timedelta(minutes=30)
    features = LabFeatureBuilder().build(histories, histories["5m"][0].timestamp, anchor, AuxiliaryMarketSeries({funding_time: 0.0001}, {funding_time: 1000.0}))
    assert histories["5m"][198].timestamp not in features
    last = features[histories["5m"][-1].timestamp]
    assert last.adx >= 0
    assert last.relative_volume > 0
    assert last.funding_rate == 0.0001
    assert last.open_interest == 1000.0


def test_mtf_alignment_does_not_use_unclosed_higher_candle():
    anchor = datetime(2025, 2, 15, tzinfo=UTC)
    histories = {
        "4h": _candles(anchor - timedelta(hours=4 * 210), 210, 240),
        "1h": _candles(anchor - timedelta(hours=210), 210, 60),
        "15m": _candles(anchor - timedelta(minutes=15 * 210), 210, 15),
        "5m": _candles(anchor - timedelta(minutes=5 * 210), 210, 5),
    }
    altered = {timeframe: list(candles) for timeframe, candles in histories.items()}
    original = altered["15m"][-1]
    altered["15m"][-1] = Candle(original.timestamp, original.open, original.high + 100, Decimal("1"), Decimal("1"), original.volume)
    builder = LabFeatureBuilder()
    first = builder.build(histories, histories["5m"][-3].timestamp, anchor)
    second = builder.build(altered, histories["5m"][-3].timestamp, anchor)
    before_close = histories["5m"][-2].timestamp
    assert first[before_close].mtf_alignment == second[before_close].mtf_alignment


def test_final_holdout_batch_can_open_only_once_and_only_when_unchanged():
    vault = FinalHoldoutVault()
    frozen = ("trend:A", "breakout:B")
    vault.lock(frozen)
    with pytest.raises(RuntimeError, match="changed"):
        vault.open_once(("trend:C",))
    vault.open_once(frozen)
    with pytest.raises(RuntimeError, match="only once"):
        vault.open_once(frozen)
