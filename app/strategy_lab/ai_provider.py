from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.strategy_lab.features import LabFeature
from app.strategy_lab.strategies import LabDirection, StrategyVote, WAIT


@dataclass(frozen=True, slots=True)
class AIAnalysis:
    direction: LabDirection
    confidence: int
    market_regime: str
    risk_flags: tuple[str, ...]
    reasoning_summary: str


class AIAnalysisProvider(ABC):
    """Analysis-only boundary. It has no broker, account, risk or order methods."""

    @abstractmethod
    def analyze(self, feature: LabFeature) -> AIAnalysis: ...


class FrozenResearchAIProvider(AIAnalysisProvider):
    """Deterministic offline surrogate used only for reproducible historical A/B/C comparison."""

    version = "frozen_research_ai_v1"

    def analyze(self, feature: LabFeature) -> AIAnalysis:
        flags: list[str] = []
        if feature.atr_percentile >= 95:
            flags.append("EXTREME_VOLATILITY")
        if abs(feature.funding_rate or 0) >= 0.001:
            flags.append("EXTREME_FUNDING")
        if abs(feature.open_interest_change or 0) >= 0.10:
            flags.append("OPEN_INTEREST_SHOCK")
        trend = 1 if feature.trend_regime == "BULL" else -1 if feature.trend_regime == "BEAR" else 0
        momentum = 1 if feature.momentum > 0.002 and feature.momentum_acceleration > 0 else -1 if feature.momentum < -0.002 and feature.momentum_acceleration < 0 else 0
        mean_reversion = -1 if feature.bollinger_z > 2 and feature.adx < 20 else 1 if feature.bollinger_z < -2 and feature.adx < 20 else 0
        alignment = 1 if feature.mtf_alignment >= 3 else -1 if feature.mtf_alignment <= -3 else 0
        score = trend * 2 + momentum * 2 + alignment * 2 + mean_reversion
        direction = LabDirection.LONG if score >= 4 else LabDirection.SHORT if score <= -4 else LabDirection.WAIT
        confidence = min(95, int(50 + abs(score) * 7 + min(feature.adx, 40) * 0.3)) if direction is not LabDirection.WAIT else min(60, int(35 + abs(score) * 5))
        if flags and direction is not LabDirection.WAIT:
            confidence = max(0, confidence - 20 * len(flags))
        return AIAnalysis(direction, confidence, _primary_regime(feature), tuple(flags), f"frozen score={score}; trend={trend}; momentum={momentum}; mtf={alignment}; reversion={mean_reversion}")


def strategy_with_ai_filter(vote: StrategyVote, analysis: AIAnalysis) -> StrategyVote:
    if vote.direction is LabDirection.WAIT:
        return WAIT
    if analysis.direction is not vote.direction or analysis.confidence < 65 or any(flag.startswith("EXTREME") for flag in analysis.risk_flags):
        return WAIT
    return StrategyVote(vote.direction, min(100.0, (vote.edge_score + analysis.confidence) / 2), vote.expected_move_pct, vote.stop_atr, vote.reward_r, (*vote.reasons, "AI direction filter confirmed"))


def ai_signal(analysis: AIAnalysis, feature: LabFeature) -> StrategyVote:
    if analysis.direction is LabDirection.WAIT or analysis.confidence < 72 or any(flag.startswith("EXTREME") for flag in analysis.risk_flags):
        return WAIT
    return StrategyVote(analysis.direction, float(analysis.confidence), feature.atr_pct * 1.7 * 2.4, 1.7, 2.4, (analysis.reasoning_summary,))


def _primary_regime(feature: LabFeature) -> str:
    return feature.volatility_regime if feature.volatility_regime != "NORMAL_VOLATILITY" else feature.trend_regime
