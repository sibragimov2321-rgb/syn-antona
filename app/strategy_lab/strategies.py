from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from app.strategy_lab.features import LabFeature


class LabDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"


@dataclass(frozen=True, slots=True)
class StrategyVote:
    direction: LabDirection
    edge_score: float
    expected_move_pct: float
    stop_atr: float
    reward_r: float
    reasons: tuple[str, ...]


WAIT = StrategyVote(LabDirection.WAIT, 0.0, 0.0, 0.0, 0.0, ())


class LabStrategy(ABC):
    name: str

    @abstractmethod
    def evaluate(self, feature: LabFeature) -> StrategyVote: ...


class TrendFollowingStrategy(LabStrategy):
    name = "TREND_FOLLOWING"

    def evaluate(self, feature: LabFeature) -> StrategyVote:
        if feature.adx < 20 or feature.trend_strength < 1.0:
            return WAIT
        if feature.trend_regime == "BULL" and feature.price > feature.ema_20 and feature.momentum > 0:
            score = min(95.0, 58 + feature.adx * 0.55 + min(feature.trend_strength, 5) * 4)
            return _vote(LabDirection.LONG, score, feature, 1.8, 2.5, "bull trend with ADX")
        if feature.trend_regime == "BEAR" and feature.price < feature.ema_20 and feature.momentum < 0:
            score = min(95.0, 58 + feature.adx * 0.55 + min(feature.trend_strength, 5) * 4)
            return _vote(LabDirection.SHORT, score, feature, 1.8, 2.5, "bear trend with ADX")
        return WAIT


class BreakoutStrategy(LabStrategy):
    name = "BREAKOUT"

    def evaluate(self, feature: LabFeature) -> StrategyVote:
        if not feature.breakout or feature.relative_volume < 1.1 or feature.atr_percentile < 45:
            return WAIT
        direction = LabDirection.LONG if feature.breakout > 0 else LabDirection.SHORT
        score = min(96.0, 62 + min(feature.relative_volume, 3) * 8 + max(feature.atr_percentile - 50, 0) * 0.18)
        return _vote(direction, score, feature, 1.6, 2.8, "range breakout with relative volume")


class MeanReversionStrategy(LabStrategy):
    name = "MEAN_REVERSION"

    def evaluate(self, feature: LabFeature) -> StrategyVote:
        if feature.trend_regime != "SIDEWAYS" or feature.adx > 24 or feature.atr_percentile > 70:
            return WAIT
        if feature.rsi <= 30 and feature.bollinger_z <= -1.8 and feature.distance_from_ema <= -1.2:
            score = min(92.0, 62 + (30 - feature.rsi) * 0.8 + abs(feature.bollinger_z) * 5)
            return _vote(LabDirection.LONG, score, feature, 1.3, 2.0, "oversold deviation in sideways regime")
        if feature.rsi >= 70 and feature.bollinger_z >= 1.8 and feature.distance_from_ema >= 1.2:
            score = min(92.0, 62 + (feature.rsi - 70) * 0.8 + abs(feature.bollinger_z) * 5)
            return _vote(LabDirection.SHORT, score, feature, 1.3, 2.0, "overbought deviation in sideways regime")
        return WAIT


class MomentumStrategy(LabStrategy):
    name = "MOMENTUM"

    def evaluate(self, feature: LabFeature) -> StrategyVote:
        if feature.relative_volume < 1.0 or feature.adx < 16:
            return WAIT
        if feature.momentum > 0.003 and feature.momentum_acceleration > 0 and 52 <= feature.rsi <= 78:
            score = min(94.0, 62 + feature.momentum * 1800 + feature.momentum_acceleration * 1200 + feature.relative_volume * 4)
            return _vote(LabDirection.LONG, score, feature, 1.7, 2.4, "positive momentum acceleration")
        if feature.momentum < -0.003 and feature.momentum_acceleration < 0 and 22 <= feature.rsi <= 48:
            score = min(94.0, 62 + abs(feature.momentum) * 1800 + abs(feature.momentum_acceleration) * 1200 + feature.relative_volume * 4)
            return _vote(LabDirection.SHORT, score, feature, 1.7, 2.4, "negative momentum acceleration")
        return WAIT


class VolatilityExpansionStrategy(LabStrategy):
    name = "VOLATILITY_EXPANSION"

    def evaluate(self, feature: LabFeature) -> StrategyVote:
        if feature.atr_percentile < 75 or feature.relative_volume < 1.15 or feature.bollinger_width < 0.008:
            return WAIT
        if feature.breakout > 0 and feature.momentum_acceleration > 0:
            score = min(96.0, 60 + feature.atr_percentile * 0.22 + feature.relative_volume * 5)
            return _vote(LabDirection.LONG, score, feature, 2.0, 3.0, "upward volatility expansion")
        if feature.breakout < 0 and feature.momentum_acceleration < 0:
            score = min(96.0, 60 + feature.atr_percentile * 0.22 + feature.relative_volume * 5)
            return _vote(LabDirection.SHORT, score, feature, 2.0, 3.0, "downward volatility expansion")
        return WAIT


class RegimeAdaptiveStrategy(LabStrategy):
    name = "REGIME_ADAPTIVE"

    def __init__(self) -> None:
        self.trend = TrendFollowingStrategy()
        self.reversion = MeanReversionStrategy()
        self.expansion = VolatilityExpansionStrategy()

    def evaluate(self, feature: LabFeature) -> StrategyVote:
        if feature.volatility_regime == "HIGH_VOLATILITY":
            return self.expansion.evaluate(feature)
        if feature.trend_regime == "SIDEWAYS":
            return self.reversion.evaluate(feature)
        return self.trend.evaluate(feature)


class MultiTimeframeTrendStrategy(LabStrategy):
    name = "MULTI_TIMEFRAME_TREND"

    def evaluate(self, feature: LabFeature) -> StrategyVote:
        if feature.adx < 18 or abs(feature.mtf_alignment) < 3:
            return WAIT
        if feature.mtf_alignment >= 3 and feature.trend_regime == "BULL" and feature.price > feature.vwap:
            score = min(96.0, 64 + feature.adx * 0.45 + feature.mtf_alignment * 3)
            return _vote(LabDirection.LONG, score, feature, 1.8, 2.7, "closed timeframes align long")
        if feature.mtf_alignment <= -3 and feature.trend_regime == "BEAR" and feature.price < feature.vwap:
            score = min(96.0, 64 + feature.adx * 0.45 + abs(feature.mtf_alignment) * 3)
            return _vote(LabDirection.SHORT, score, feature, 1.8, 2.7, "closed timeframes align short")
        return WAIT


class StrategyEnsemble(LabStrategy):
    name = "STRATEGY_ENSEMBLE"

    def __init__(self, strategies: tuple[LabStrategy, ...], minimum_votes: int = 2, minimum_edge: float = 70.0) -> None:
        self.strategies = strategies
        self.minimum_votes = minimum_votes
        self.minimum_edge = minimum_edge

    def evaluate(self, feature: LabFeature) -> StrategyVote:
        votes = [strategy.evaluate(feature) for strategy in self.strategies]
        long_votes = [vote for vote in votes if vote.direction is LabDirection.LONG]
        short_votes = [vote for vote in votes if vote.direction is LabDirection.SHORT]
        selected = long_votes if len(long_votes) > len(short_votes) else short_votes
        opposing = short_votes if selected is long_votes else long_votes
        if len(selected) < self.minimum_votes or opposing:
            return WAIT
        edge = sum(vote.edge_score for vote in selected) / len(selected)
        if edge < self.minimum_edge:
            return WAIT
        direction = selected[0].direction
        return StrategyVote(
            direction,
            edge,
            sum(vote.expected_move_pct for vote in selected) / len(selected),
            sum(vote.stop_atr for vote in selected) / len(selected),
            sum(vote.reward_r for vote in selected) / len(selected),
            tuple(f"{len(selected)} strategies agree {direction}" for _ in range(1)),
        )


def strategy_families() -> tuple[LabStrategy, ...]:
    independent: tuple[LabStrategy, ...] = (
        TrendFollowingStrategy(),
        BreakoutStrategy(),
        MeanReversionStrategy(),
        MomentumStrategy(),
        VolatilityExpansionStrategy(),
        RegimeAdaptiveStrategy(),
        MultiTimeframeTrendStrategy(),
    )
    return (*independent, StrategyEnsemble(independent))


def _vote(direction: LabDirection, score: float, feature: LabFeature, stop_atr: float, reward_r: float, reason: str) -> StrategyVote:
    expected = feature.atr_pct * stop_atr * reward_r
    return StrategyVote(direction, score, expected, stop_atr, reward_r, (reason,))
