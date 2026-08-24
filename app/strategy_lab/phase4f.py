from bisect import bisect_right
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from math import erfc, log, sqrt

from app.backtest.core import BacktestEngine, BacktestResult, Candle, StrategyAction
from app.backtest.costs import BacktestCostProfile, PHASE4E_SPOT_PROFILES
from app.backtest.orchestrator import LOW_RISK
from app.backtest.persistence import TIMEFRAME_SECONDS
from app.domain.models import Side
from app.strategy_lab.features import LabFeature
from app.strategy_lab.phase4e import build_features
from app.strategy_lab.strategies import LabDirection, MeanReversionStrategy, StrategyVote, WAIT

TIMEFRAMES_4F = ("5m", "15m", "1h", "4h")
PURGE_DAYS = 7
IMPACT_PER_LEG = Decimal("0.00005")
SAFETY_MARGIN_MULTIPLE = Decimal("0.50")
MINIMUM_SIGNAL_SCORE = 75


class Regime4F(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    BREAKOUT = "BREAKOUT"


@dataclass(frozen=True)
class Hypothesis:
    family: str
    timeframe: str
    rationale: str

    @property
    def identifier(self) -> str:
        return f"{self.family}:{self.timeframe}:v1"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SplitBoundaries:
    start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    walk_forward_start: datetime
    walk_forward_end: datetime
    holdout_start: datetime
    end: datetime


@dataclass(frozen=True)
class Phase4FAsset:
    symbol: str
    candles: dict[str, list[Candle]]
    features: dict[str, dict[datetime, LabFeature]]
    train: dict[str, list[Candle]]
    validation: dict[str, list[Candle]]
    walk_forward: dict[str, list[Candle]]
    holdout: dict[str, list[Candle]]


RATIONALES = {
    "TREND_FOLLOWING": "Persistent information diffusion and risk-premium flows can extend established directional moves.",
    "BREAKOUT": "Price leaving a well-observed range with participation can trigger stops and delayed positioning.",
    "MOMENTUM": "Short-horizon continuation may persist when return acceleration is confirmed by participation.",
    "VOLATILITY_EXPANSION": "Volatility clustering can make the first confirmed expansion persist beyond its initial bar.",
    "PULLBACK_IN_TREND": "Temporary liquidity-driven retracements inside a strong trend can revert toward trend direction.",
    "MEAN_REVERSION_BASELINE": "Temporary inventory imbalance in range regimes may mean-revert; retained only as a control.",
    "MULTI_TIMEFRAME_TREND": "Alignment across independently closed horizons may reduce false single-timeframe trends.",
    "REGIME_ADAPTIVE": "Different return-generating mechanisms may dominate in trend, range, and expansion regimes.",
    "VOLUME_VOL_CONFIRMATION": "Unusually high participation plus volatility can identify genuine demand/supply imbalance.",
}


def frozen_hypotheses() -> tuple[Hypothesis, ...]:
    return tuple(Hypothesis(family, timeframe, rationale) for family, rationale in RATIONALES.items() for timeframe in TIMEFRAMES_4F)


def make_boundaries(start: datetime, end: datetime) -> SplitBoundaries:
    duration = end - start
    train_end = start + duration * 0.50
    validation_end = start + duration * 0.70
    walk_end = start + duration * 0.90
    purge = timedelta(days=PURGE_DAYS)
    return SplitBoundaries(start, train_end, train_end + purge, validation_end, validation_end + purge, walk_end, walk_end + purge, end)


def build_aligned_features(histories: dict[str, list[Candle]], start: datetime, end: datetime) -> dict[str, dict[datetime, LabFeature]]:
    features = {timeframe: build_features(candles, start, end) for timeframe, candles in histories.items()}
    ordered = {timeframe: sorted(values) for timeframe, values in features.items()}
    closed_times = {
        timeframe: [value + timedelta(seconds=TIMEFRAME_SECONDS[timeframe]) for value in timestamps]
        for timeframe, timestamps in ordered.items()
    }
    for timeframe in TIMEFRAMES_4F:
        duration = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
        higher = TIMEFRAMES_4F[TIMEFRAMES_4F.index(timeframe) + 1 :]
        updated = {}
        for timestamp, feature in features[timeframe].items():
            decision_time = timestamp + duration
            alignment = _trend_vote(feature)
            valid = True
            for higher_timeframe in higher:
                index = bisect_right(closed_times[higher_timeframe], decision_time) - 1
                if index < 0:
                    valid = False
                    break
                alignment += _trend_vote(features[higher_timeframe][ordered[higher_timeframe][index]])
            if valid:
                updated[timestamp] = replace(feature, mtf_alignment=alignment)
        features[timeframe] = updated
    return features


def classify_regime(feature: LabFeature) -> Regime4F:
    if feature.breakout and feature.atr_percentile >= 60:
        return Regime4F.BREAKOUT
    if feature.atr_percentile >= 80:
        return Regime4F.HIGH_VOLATILITY
    if feature.atr_percentile <= 20:
        return Regime4F.LOW_VOLATILITY
    if feature.adx >= 22 and feature.ema_20 > feature.ema_50 and feature.momentum > 0:
        return Regime4F.TREND_UP
    if feature.adx >= 22 and feature.ema_20 < feature.ema_50 and feature.momentum < 0:
        return Regime4F.TREND_DOWN
    return Regime4F.RANGE


class EconomicHypothesisStrategy:
    def __init__(self, hypothesis: Hypothesis, features: dict[datetime, LabFeature], costs: BacktestCostProfile, cost_multiple: Decimal = Decimal("1")) -> None:
        self.hypothesis = hypothesis
        self.features = features
        self.cost_multiple = cost_multiple
        self.costs = replace(
            costs,
            maker_fee=costs.maker_fee * cost_multiple,
            taker_fee=costs.taker_fee * cost_multiple,
            spread=costs.spread * cost_multiple,
            slippage=costs.slippage * cost_multiple,
        )
        self.mean_reversion = MeanReversionStrategy()

    def __call__(self, history) -> StrategyAction | None:
        feature = self.features.get(history[-1].timestamp)
        if feature is None:
            return None
        regime = classify_regime(feature)
        vote = self._vote(feature, regime)
        if vote.direction is LabDirection.WAIT or vote.edge_score < MINIMUM_SIGNAL_SCORE:
            return None
        price = Decimal(str(feature.price))
        atr = Decimal(str(feature.atr))
        stop_distance = atr * Decimal(str(vote.stop_atr))
        reward_distance = stop_distance * Decimal(str(vote.reward_r))
        expected_atr = Decimal(str(_expected_atr(self.hypothesis.family)))
        expected_move = min(reward_distance, atr * expected_atr)
        side = Side.LONG if vote.direction is LabDirection.LONG else Side.SHORT
        expected_exit = price + expected_move if side is Side.LONG else max(Decimal("0.00000001"), price - expected_move)
        entry_fee = price * self.costs.taker_fee
        exit_fee = expected_exit * self.costs.taker_fee
        spread = (price + expected_exit) * self.costs.spread / Decimal("2")
        slippage = (price + expected_exit) * self.costs.slippage
        market_impact = (price + expected_exit) * IMPACT_PER_LEG * self.cost_multiple
        funding = Decimal()
        total_cost = entry_fee + exit_fee + spread + slippage + market_impact + funding
        safety_margin = total_cost * SAFETY_MARGIN_MULTIPLE
        expected_net_edge = expected_move - total_cost
        if expected_move <= total_cost + safety_margin or expected_net_edge <= 0:
            return None
        stop = price - stop_distance if side is Side.LONG else price + stop_distance
        target = price + reward_distance if side is Side.LONG else price - reward_distance
        if min(stop, target) <= 0:
            return None
        context = feature.context()
        context.update(
            {
                "family": self.hypothesis.family,
                "economic_rationale": self.hypothesis.rationale,
                "market_regime": regime.value,
                "expected_edge": expected_move,
                "estimated_entry_fee": entry_fee,
                "estimated_exit_fee": exit_fee,
                "estimated_spread": spread,
                "estimated_slippage": slippage,
                "estimated_market_impact": market_impact,
                "estimated_funding": funding,
                "estimated_round_trip_cost": total_cost,
                "safety_margin": safety_margin,
                "expected_net_edge": expected_net_edge,
            }
        )
        return StrategyAction(side, stop, target, int(vote.edge_score), regime.value, Decimal(str(feature.atr_pct)), self.costs.spread, context)

    def _vote(self, feature: LabFeature, regime: Regime4F) -> StrategyVote:
        family = self.hypothesis.family
        if family == "TREND_FOLLOWING":
            return _trend_vote_signal(feature, regime)
        if family == "BREAKOUT":
            return _breakout_vote(feature, regime)
        if family == "MOMENTUM":
            return _momentum_vote(feature, regime)
        if family == "VOLATILITY_EXPANSION":
            return _volatility_vote(feature, regime)
        if family == "PULLBACK_IN_TREND":
            return _pullback_vote(feature, regime)
        if family == "MEAN_REVERSION_BASELINE":
            return self.mean_reversion.evaluate(feature) if regime in {Regime4F.RANGE, Regime4F.LOW_VOLATILITY} else WAIT
        if family == "MULTI_TIMEFRAME_TREND":
            return _mtf_vote(feature, regime, self.hypothesis.timeframe)
        if family == "REGIME_ADAPTIVE":
            if regime in {Regime4F.TREND_UP, Regime4F.TREND_DOWN}:
                return _trend_vote_signal(feature, regime)
            if regime in {Regime4F.BREAKOUT, Regime4F.HIGH_VOLATILITY}:
                return _volatility_vote(feature, regime)
            return self.mean_reversion.evaluate(feature)
        if family == "VOLUME_VOL_CONFIRMATION":
            return _volume_vote(feature, regime)
        return WAIT


def evaluate_hypothesis(symbol: str, hypothesis: Hypothesis, candles: list[Candle], features: dict[datetime, LabFeature], profile: BacktestCostProfile, cost_multiple: Decimal = Decimal("1")) -> BacktestResult:
    strategy = EconomicHypothesisStrategy(hypothesis, features, profile, cost_multiple)
    costs = strategy.costs
    engine = BacktestEngine(fee_rate=costs.taker_fee, slippage=costs.slippage + IMPACT_PER_LEG * cost_multiple, spread=costs.spread, market_type="spot")
    return engine.run(candles, strategy, Decimal("1000"), risk_profile=LOW_RISK, retain_equity=False)


def summarize(results: dict[str, BacktestResult]) -> dict:
    trades = [trade for result in results.values() for trade in result.trades]
    gross_profit = sum((trade.pnl_before_costs for trade in trades if trade.pnl_before_costs > 0), Decimal())
    gross_loss = sum((trade.pnl_before_costs for trade in trades if trade.pnl_before_costs < 0), Decimal())
    net_profit = sum((trade.pnl for trade in trades if trade.pnl > 0), Decimal())
    net_loss = sum((trade.pnl for trade in trades if trade.pnl < 0), Decimal())
    gross = sum((trade.pnl_before_costs for trade in trades), Decimal())
    net = sum((trade.pnl for trade in trades), Decimal())
    fees = sum((trade.fees for trade in trades), Decimal())
    spread = sum((trade.spread_cost for trade in trades), Decimal())
    slippage = sum((trade.slippage_cost for trade in trades), Decimal())
    funding = sum((trade.funding for trade in trades), Decimal())
    wins = sum(trade.pnl > 0 for trade in trades)
    holding = [Decimal(str((trade.exit_time - trade.entry_time).total_seconds())) for trade in trades]
    sharpes = [Decimal(str(result.metrics["sharpe"])) for result in results.values()]
    sortinos = [Decimal(str(result.metrics["sortino"])) for result in results.values()]
    return {
        "trades": len(trades),
        "win_rate": Decimal(wins * 100) / len(trades) if trades else Decimal(),
        "gross_pnl": gross,
        "net_pnl": net,
        "fees": fees,
        "spread": spread,
        "slippage": slippage,
        "funding": funding,
        "net_pf": net_profit / abs(net_loss) if net_loss else (Decimal("Infinity") if net_profit else Decimal()),
        "gross_pf": gross_profit / abs(gross_loss) if gross_loss else (Decimal("Infinity") if gross_profit else Decimal()),
        "expectancy": net / len(trades) if trades else Decimal(),
        "sharpe": sum(sharpes, Decimal()) / len(sharpes) if sharpes else Decimal(),
        "sortino": sum(sortinos, Decimal()) / len(sortinos) if sortinos else Decimal(),
        "max_drawdown_pct": max((Decimal(str(result.metrics["max_drawdown_pct"])) for result in results.values()), default=Decimal()),
        "turnover": sum((trade.turnover for trade in trades), Decimal()),
        "average_holding_seconds": sum(holding, Decimal()) / len(holding) if holding else Decimal(),
        "regimes": regime_results(trades),
    }


def regime_results(trades) -> dict:
    output = {}
    for regime in Regime4F:
        selected = [trade for trade in trades if trade.regime == regime.value]
        net = sum((trade.pnl for trade in selected), Decimal())
        wins = sum(trade.pnl > 0 for trade in selected)
        gp = sum((trade.pnl for trade in selected if trade.pnl > 0), Decimal())
        gl = sum((trade.pnl for trade in selected if trade.pnl < 0), Decimal())
        output[regime.value] = {
            "trades": len(selected),
            "win_rate": Decimal(wins * 100) / len(selected) if selected else Decimal(),
            "net_pnl": net,
            "expectancy": net / len(selected) if selected else Decimal(),
            "net_pf": gp / abs(gl) if gl else (Decimal("Infinity") if gp else Decimal()),
        }
    return output


def multiple_testing_adjustment(summary: dict, experiments: int) -> dict:
    sharpe = float(summary["sharpe"])
    probability = 0.5 * erfc(sharpe / sqrt(2))
    adjusted = min(1.0, probability * experiments)
    penalty = sqrt(2 * log(max(experiments, 2)))
    return {"raw_one_sided_p": probability, "bonferroni_p": adjusted, "deflated_sharpe": sharpe - penalty, "experiments": experiments}


def train_score(summary: dict, correction: dict) -> float:
    if summary["trades"] < 20:
        return -1000 + summary["trades"]
    return min(float(summary["net_pf"]), 4) * 3 + float(summary["expectancy"]) + float(summary["net_pnl"]) * 0.01 - float(summary["max_drawdown_pct"]) * 0.1 + min(correction["deflated_sharpe"], 2)


def confirmation_pass(summary: dict, minimum_pf: Decimal = Decimal("1.05")) -> bool:
    return summary["trades"] >= 20 and summary["net_pf"] > minimum_pf and summary["expectancy"] > 0 and summary["net_pnl"] > 0


def _trend_vote(feature: LabFeature) -> int:
    return 1 if feature.ema_20 > feature.ema_50 and feature.momentum > 0 else -1 if feature.ema_20 < feature.ema_50 and feature.momentum < 0 else 0


def _trend_vote_signal(feature: LabFeature, regime: Regime4F) -> StrategyVote:
    if regime is Regime4F.TREND_UP and feature.price > feature.ema_20 and feature.adx >= 24:
        return StrategyVote(LabDirection.LONG, min(95, 68 + feature.adx * 0.45), feature.atr_pct * 4.5, 1.8, 2.5, ("directional persistence",))
    if regime is Regime4F.TREND_DOWN and feature.price < feature.ema_20 and feature.adx >= 24:
        return StrategyVote(LabDirection.SHORT, min(95, 68 + feature.adx * 0.45), feature.atr_pct * 4.5, 1.8, 2.5, ("directional persistence",))
    return WAIT


def _breakout_vote(feature: LabFeature, regime: Regime4F) -> StrategyVote:
    if regime is not Regime4F.BREAKOUT or feature.relative_volume < 1.2:
        return WAIT
    direction = LabDirection.LONG if feature.breakout > 0 else LabDirection.SHORT
    return StrategyVote(direction, min(96, 68 + feature.relative_volume * 7 + feature.atr_percentile * 0.12), feature.atr_pct * 4.48, 1.6, 2.8, ("range exit with participation",))


def _momentum_vote(feature: LabFeature, regime: Regime4F) -> StrategyVote:
    if regime not in {Regime4F.TREND_UP, Regime4F.TREND_DOWN, Regime4F.HIGH_VOLATILITY} or feature.relative_volume < 1:
        return WAIT
    if feature.momentum > 0.004 and feature.momentum_acceleration > 0 and feature.rsi < 78:
        return StrategyVote(LabDirection.LONG, min(94, 68 + feature.momentum * 1500 + feature.relative_volume * 4), feature.atr_pct * 4.08, 1.7, 2.4, ("return acceleration",))
    if feature.momentum < -0.004 and feature.momentum_acceleration < 0 and feature.rsi > 22:
        return StrategyVote(LabDirection.SHORT, min(94, 68 + abs(feature.momentum) * 1500 + feature.relative_volume * 4), feature.atr_pct * 4.08, 1.7, 2.4, ("return acceleration",))
    return WAIT


def _volatility_vote(feature: LabFeature, regime: Regime4F) -> StrategyVote:
    if regime not in {Regime4F.BREAKOUT, Regime4F.HIGH_VOLATILITY} or feature.atr_percentile < 75 or feature.bollinger_width < 0.008:
        return WAIT
    if feature.breakout > 0 and feature.momentum_acceleration > 0:
        return StrategyVote(LabDirection.LONG, min(96, 70 + feature.atr_percentile * 0.2), feature.atr_pct * 6, 2, 3, ("volatility clustering",))
    if feature.breakout < 0 and feature.momentum_acceleration < 0:
        return StrategyVote(LabDirection.SHORT, min(96, 70 + feature.atr_percentile * 0.2), feature.atr_pct * 6, 2, 3, ("volatility clustering",))
    return WAIT


def _pullback_vote(feature: LabFeature, regime: Regime4F) -> StrategyVote:
    price = feature.price
    if regime is Regime4F.TREND_UP and feature.ema_50 <= price <= feature.ema_20 * 1.003 and 40 <= feature.rsi <= 55 and feature.momentum_acceleration > 0:
        return StrategyVote(LabDirection.LONG, min(92, 72 + feature.adx * 0.35), feature.atr_pct * 2.99, 1.3, 2.3, ("trend pullback",))
    if regime is Regime4F.TREND_DOWN and feature.ema_20 * 0.997 <= price <= feature.ema_50 and 45 <= feature.rsi <= 60 and feature.momentum_acceleration < 0:
        return StrategyVote(LabDirection.SHORT, min(92, 72 + feature.adx * 0.35), feature.atr_pct * 2.99, 1.3, 2.3, ("trend pullback",))
    return WAIT


def _mtf_vote(feature: LabFeature, regime: Regime4F, timeframe: str) -> StrategyVote:
    required = len(TIMEFRAMES_4F) - TIMEFRAMES_4F.index(timeframe)
    if regime is Regime4F.TREND_UP and feature.mtf_alignment >= required and feature.adx >= 20:
        return StrategyVote(LabDirection.LONG, min(96, 70 + feature.adx * 0.4 + required * 2), feature.atr_pct * 4.86, 1.8, 2.7, ("closed horizon alignment",))
    if regime is Regime4F.TREND_DOWN and feature.mtf_alignment <= -required and feature.adx >= 20:
        return StrategyVote(LabDirection.SHORT, min(96, 70 + feature.adx * 0.4 + required * 2), feature.atr_pct * 4.86, 1.8, 2.7, ("closed horizon alignment",))
    return WAIT


def _volume_vote(feature: LabFeature, regime: Regime4F) -> StrategyVote:
    if feature.relative_volume < 1.5 or feature.atr_percentile < 60:
        return WAIT
    if feature.momentum > 0.003 and feature.momentum_acceleration > 0 and feature.rsi < 75:
        return StrategyVote(LabDirection.LONG, min(95, 68 + feature.relative_volume * 8 + feature.atr_percentile * 0.08), feature.atr_pct * 3.75, 1.5, 2.5, ("participation imbalance",))
    if feature.momentum < -0.003 and feature.momentum_acceleration < 0 and feature.rsi > 25:
        return StrategyVote(LabDirection.SHORT, min(95, 68 + feature.relative_volume * 8 + feature.atr_percentile * 0.08), feature.atr_pct * 3.75, 1.5, 2.5, ("participation imbalance",))
    return WAIT


def _expected_atr(family: str) -> float:
    return {
        "TREND_FOLLOWING": 2.0,
        "BREAKOUT": 2.4,
        "MOMENTUM": 2.0,
        "VOLATILITY_EXPANSION": 2.8,
        "PULLBACK_IN_TREND": 1.6,
        "MEAN_REVERSION_BASELINE": 1.8,
        "MULTI_TIMEFRAME_TREND": 2.2,
        "REGIME_ADAPTIVE": 2.0,
        "VOLUME_VOL_CONFIRMATION": 2.1,
    }[family]


BINANCE_PROFILE = PHASE4E_SPOT_PROFILES["binance"]
