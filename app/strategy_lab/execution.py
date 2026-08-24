from decimal import Decimal
from enum import StrEnum

from app.backtest.core import StrategyAction
from app.domain.models import Side
from app.strategy_lab.ai_provider import AIAnalysisProvider, ai_signal, strategy_with_ai_filter
from app.strategy_lab.features import LabFeature
from app.strategy_lab.strategies import LabDirection, LabStrategy, StrategyVote


class ResearchVariant(StrEnum):
    STRATEGY_ONLY = "A_STRATEGY_ONLY"
    STRATEGY_AI_FILTER = "B_STRATEGY_AI_FILTER"
    AI_SIGNAL_RISK = "C_AI_SIGNAL_RISK"


def action_for(strategy: LabStrategy, variant: ResearchVariant, feature: LabFeature, ai_provider: AIAnalysisProvider, fee_rate: Decimal, slippage: Decimal, minimum_edge: float = 68.0, cost_multiple: float = 2.0) -> StrategyAction | None:
    strategy_vote = strategy.evaluate(feature)
    analysis = ai_provider.analyze(feature)
    if variant is ResearchVariant.STRATEGY_ONLY:
        vote = strategy_vote
    elif variant is ResearchVariant.STRATEGY_AI_FILTER:
        vote = strategy_with_ai_filter(strategy_vote, analysis)
    else:
        vote = ai_signal(analysis, feature)
    return action_from_vote(vote, feature, fee_rate, slippage, minimum_edge, cost_multiple, variant.value)


def action_from_vote(vote: StrategyVote, feature: LabFeature, fee_rate: Decimal, slippage: Decimal, minimum_edge: float = 68.0, cost_multiple: float = 2.0, source: str = "DIRECT") -> StrategyAction | None:
    if vote.direction is LabDirection.WAIT or vote.edge_score < minimum_edge:
        return None
    round_trip_cost = float(fee_rate * 2 + slippage * 2)
    if vote.expected_move_pct <= round_trip_cost * cost_multiple:
        return None
    entry = Decimal(str(feature.price))
    distance = Decimal(str(feature.atr * vote.stop_atr))
    reward = distance * Decimal(str(vote.reward_r))
    if vote.direction is LabDirection.LONG:
        side, stop, target = Side.LONG, entry - distance, entry + reward
    else:
        side, stop, target = Side.SHORT, entry + distance, entry - reward
    if stop <= 0 or target <= 0:
        return None
    regime = feature.volatility_regime if feature.volatility_regime != "NORMAL_VOLATILITY" else feature.trend_regime
    context = feature.context()
    context.update({"strategy_reasons": vote.reasons, "edge_score": vote.edge_score, "variant": source})
    return StrategyAction(side, stop, target, int(vote.edge_score), regime, Decimal(str(feature.atr_pct)), Decimal(), context)
