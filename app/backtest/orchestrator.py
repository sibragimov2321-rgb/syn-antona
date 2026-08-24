from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.backtest.core import (
    BacktestEngine,
    BacktestResult,
    Candle,
    MarketRegime,
    StrategyAction,
    buy_and_hold,
    monte_carlo,
    overfitting_warning,
    regime_statistics,
    signal_score_calibration,
    validation_status,
)
from app.backtest.persistence import (
    BacktestRepository,
    CachedHistoricalDataProvider,
    HistoricalCandleCache,
    TIMEFRAME_SECONDS,
)
from app.domain.models import Decision, RiskProfile, Side
from app.market.indicators import ema, snapshot
from app.signals.engine import MarketFrame, SignalEngine

WARMUP_CANDLES = 200
TIMEFRAMES = ("5m", "15m", "1h", "4h")
SIGNAL_TIMEFRAMES = {"5m": "5M", "15m": "15M", "1h": "1H", "4h": "4H"}

LOW_RISK = RiskProfile()
MEDIUM_RISK = RiskProfile(
    risk_per_trade_pct=Decimal("0.01"),
    max_daily_loss_pct=Decimal("0.03"),
    max_leverage=Decimal("3"),
    max_position_notional=Decimal("1000"),
)


@dataclass(frozen=True)
class HistoricalBacktestReport:
    result: BacktestResult
    walk_forward: dict[str, BacktestResult]
    overfitting_warning: bool
    regimes: dict[str, dict]
    score_calibration: dict[str, dict]
    monte_carlo: dict
    benchmark_final_equity: Decimal
    benchmark_return_pct: Decimal
    validation_status: str
    candle_counts: dict[str, int]
    started_at: datetime
    ended_at: datetime


class MultiTimeframeAligner:
    """Selects only candles whose closing timestamp is at/before the 5m decision time."""

    def __init__(self, candles: dict[str, list[Candle]]) -> None:
        self.candles = candles
        self.close_times = {
            timeframe: [
                candle.timestamp + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
                for candle in values
            ]
            for timeframe, values in candles.items()
        }

    def closed_history(self, timeframe: str, decision_time: datetime) -> list[Candle]:
        position = bisect_right(self.close_times[timeframe], decision_time)
        return self.candles[timeframe][:position]

    def frames(self, decision_time: datetime, base_history: list[Candle]) -> dict[str, MarketFrame] | None:
        histories = {"5m": base_history}
        for timeframe in ("15m", "1h", "4h"):
            histories[timeframe] = self.closed_history(timeframe, decision_time)
        if any(len(history) < WARMUP_CANDLES for history in histories.values()):
            return None
        return {
            SIGNAL_TIMEFRAMES[timeframe]: _market_frame(SIGNAL_TIMEFRAMES[timeframe], history)
            for timeframe, history in histories.items()
        }


class HistoricalBacktestOrchestrator:
    def __init__(self, providers: dict[str, object], repository: BacktestRepository | None = None) -> None:
        cache = HistoricalCandleCache()
        self.providers = {
            name: provider if isinstance(provider,CachedHistoricalDataProvider) else CachedHistoricalDataProvider(provider,cache)
            for name,provider in providers.items()
        }
        self.repository = repository
        self.signal_engine = SignalEngine()

    async def run(
        self,
        exchange: str,
        symbol: str,
        start: datetime,
        end: datetime,
        starting_balance: Decimal = Decimal("1000"),
        risk_profile: RiskProfile = LOW_RISK,
        persist: bool = True,
    ) -> HistoricalBacktestReport:
        provider = self.providers[exchange]
        histories: dict[str, list[Candle]] = {}
        for timeframe in TIMEFRAMES:
            warmup = timedelta(seconds=TIMEFRAME_SECONDS[timeframe] * WARMUP_CANDLES)
            histories[timeframe] = await provider.fetch(symbol, timeframe, start - warmup, end)
        base = [candle for candle in histories["5m"] if start <= candle.timestamp < end]
        if not base:
            raise ValueError("No closed 5m candles in requested period")
        result, regime_timeline = self._execute_window(histories, base, start, risk_profile, starting_balance)
        boundaries = _split_boundaries(base)
        walk = {}
        for name, partition in boundaries.items():
            partition_result, _ = self._execute_window(
                histories, partition, partition[0].timestamp, risk_profile, starting_balance
            )
            walk[name] = partition_result
        warning = overfitting_warning(walk["train"], walk["out_of_sample"])
        status = validation_status(result, walk["out_of_sample"])
        simulation = monte_carlo(result.trades, starting_balance, 1000)
        benchmark = buy_and_hold(base, starting_balance)
        report = HistoricalBacktestReport(
            result=result,
            walk_forward=walk,
            overfitting_warning=warning,
            regimes=regime_statistics(result.trades, starting_balance),
            score_calibration=signal_score_calibration(result.trades),
            monte_carlo=simulation,
            benchmark_final_equity=benchmark,
            benchmark_return_pct=(benchmark - starting_balance) / starting_balance * 100,
            validation_status=status,
            candle_counts={timeframe: len(values) for timeframe, values in histories.items()},
            started_at=base[0].timestamp,
            ended_at=base[-1].timestamp,
        )
        if persist and self.repository:
            sections={f"walk_forward.{name}":value.metrics for name,value in walk.items()}
            sections.update({f"regime.{name}":values for name,values in report.regimes.items()})
            sections.update({f"score.{name}":values for name,values in report.score_calibration.items()})
            sections["benchmark"]={"final_equity":benchmark,"return_pct":report.benchmark_return_pct}
            self.repository.save(
                result, exchange, symbol, "LOW" if risk_profile == LOW_RISK else "CUSTOM",
                status, regime_timeline, simulation, sections,
            )
        return report

    def _execute_window(self, histories: dict[str, list[Candle]], base: list[Candle], trading_start: datetime, risk_profile: RiskProfile, starting_balance: Decimal) -> tuple[BacktestResult, dict[datetime, str]]:
        aligner = MultiTimeframeAligner(histories)
        warmup_5m = [candle for candle in histories["5m"] if candle.timestamp < base[0].timestamp][-WARMUP_CANDLES:]
        regimes: dict[datetime, str] = {}

        def production_strategy(window: list[Candle]) -> StrategyAction | None:
            current = window[-1]
            if current.timestamp < trading_start:
                return None
            base_history = warmup_5m + window
            decision_time = current.timestamp + timedelta(minutes=5)
            frames = aligner.frames(decision_time, base_history)
            if frames is None:
                return None
            signal = self.signal_engine.confirm_multi_timeframe("".join(symbol_part for symbol_part in ["HISTORICAL"]), frames)
            regime = classify_regime(base_history)
            regimes[current.timestamp] = regime.value
            if signal.decision is Decision.WAIT:
                return None
            return StrategyAction(
                side=Side(signal.decision),
                stop_loss=signal.proposed_stop_loss,
                take_profit=signal.proposed_take_profit,
                signal_score=signal.signal_score,
                regime=regime.value,
                volatility_pct=frames["5M"].indicators.atr_14 / frames["5M"].price,
            )

        result = BacktestEngine().run(base, production_strategy, starting_balance, risk_profile=risk_profile)
        return result, regimes


def _market_frame(timeframe: str, history: list[Candle]) -> MarketFrame:
    selected = history[-WARMUP_CANDLES:]
    indicators = snapshot(
        [candle.high for candle in selected],
        [candle.low for candle in selected],
        [candle.close for candle in selected],
    )
    return MarketFrame(timeframe, selected[-1].close, indicators)


def classify_regime(history: list[Candle]) -> MarketRegime:
    closes = [candle.close for candle in history[-WARMUP_CANDLES:]]
    indicators = snapshot(
        [candle.high for candle in history[-WARMUP_CANDLES:]],
        [candle.low for candle in history[-WARMUP_CANDLES:]],
        closes,
    )
    volatility = indicators.atr_14 / closes[-1]
    if volatility >= Decimal("0.02"):
        return MarketRegime.HIGH_VOLATILITY
    if volatility <= Decimal("0.004"):
        return MarketRegime.LOW_VOLATILITY
    ema_50, ema_200 = ema(closes, 50), ema(closes, 200)
    separation = (ema_50 - ema_200) / ema_200
    if separation >= Decimal("0.005"):
        return MarketRegime.BULL
    if separation <= Decimal("-0.005"):
        return MarketRegime.BEAR
    return MarketRegime.SIDEWAYS


def _split_boundaries(candles: list[Candle]) -> dict[str, list[Candle]]:
    first, second = int(len(candles) * Decimal("0.6")), int(len(candles) * Decimal("0.8"))
    partitions = {
        "train": candles[:first],
        "validation": candles[first:second],
        "out_of_sample": candles[second:],
    }
    if any(not partition for partition in partitions.values()):
        raise ValueError("Not enough candles for walk-forward split")
    return partitions
