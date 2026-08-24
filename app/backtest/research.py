from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from app.backtest.core import (
    BacktestResult,
    Candle,
    MarketRegime,
    StrategyAction,
)
from app.backtest.orchestrator import SIGNAL_TIMEFRAMES, WARMUP_CANDLES
from app.backtest.persistence import TIMEFRAME_SECONDS
from app.domain.models import Decision, Side
from app.market.indicators import ema, snapshot
from app.signals.engine import MarketFrame, SignalEngine


@dataclass(frozen=True)
class ResearchFeature:
    timestamp: datetime
    price: Decimal
    decision: Decision
    signal_score: int
    proposed_stop: Decimal | None
    proposed_target: Decimal | None
    rsi: Decimal
    macd: Decimal
    atr: Decimal
    atr_pct: Decimal
    ema_50: Decimal
    ema_200: Decimal
    adx: Decimal
    volume_ratio: Decimal
    breakout: str
    trend_regime: str
    volatility_regime: str
    primary_regime: str
    timeframe_alignment: int

    def context(self) -> dict:
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()}


@dataclass(frozen=True)
class StrategyConfig:
    version: str
    min_score: int = 70
    adx_min: Decimal = Decimal()
    volume_ratio_min: Decimal = Decimal()
    require_breakout: bool = False
    direction_filter: bool = False
    allow_sideways: bool = True
    min_atr_pct: Decimal = Decimal()
    max_atr_pct: Decimal = Decimal("1")
    atr_stop_multiplier: Decimal = Decimal("1.5")
    reward_r: Decimal = Decimal("2")
    cost_coverage: Decimal = Decimal()
    baseline: bool = False

    def to_dict(self) -> dict:
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()}


class HistoricalFeatureBuilder:
    """Precomputes the current technical/MTF signal once per closed 5m candle."""

    def __init__(self) -> None:
        self.signal_engine = SignalEngine()

    def build(self, histories: dict[str, list[Candle]], start: datetime, end: datetime) -> dict[datetime, ResearchFeature]:
        prepared = {timeframe: self._prepare_frames(timeframe, candles) for timeframe, candles in histories.items()}
        close_times = {timeframe: [item[0] for item in values] for timeframe, values in prepared.items()}
        features: dict[datetime, ResearchFeature] = {}
        for close_time, frame, candle_index in prepared["5m"]:
            candle_time = close_time - timedelta(minutes=5)
            if not start <= candle_time < end:
                continue
            frames = {"5M": frame}
            aligned = 1
            for timeframe in ("15m", "1h", "4h"):
                index = bisect_right(close_times[timeframe], close_time) - 1
                if index < 0:
                    break
                frames[SIGNAL_TIMEFRAMES[timeframe]] = prepared[timeframe][index][1]
                aligned += 1
            if aligned < 4:
                continue
            history = histories["5m"][candle_index-WARMUP_CANDLES+1:candle_index+1]
            signal = self.signal_engine.confirm_multi_timeframe("HISTORICAL", frames)
            indicator = frame.indicators
            closes = [candle.close for candle in history]
            ema_200 = ema(closes, 200)
            trend_regime = _trend_regime(indicator.ema_50, ema_200)
            volatility_regime = _volatility_regime(indicator.atr_14 / frame.price)
            primary = volatility_regime if volatility_regime != "NORMAL_VOLATILITY" else trend_regime
            frame_directions = [self.signal_engine.analyze("HISTORICAL", value).decision for value in frames.values()]
            directional = [item for item in frame_directions if item is not Decision.WAIT]
            alignment = max(directional.count(Decision.LONG), directional.count(Decision.SHORT), 0)
            features[candle_time] = ResearchFeature(
                candle_time, frame.price, signal.decision, signal.signal_score,
                signal.proposed_stop_loss, signal.proposed_take_profit,
                indicator.rsi_14, indicator.macd, indicator.atr_14,
                indicator.atr_14 / frame.price, indicator.ema_50, ema_200,
                _adx(history), _volume_ratio(history), _breakout(history),
                trend_regime, volatility_regime, primary, alignment,
            )
        return features

    def _prepare_frames(self, timeframe: str, candles: list[Candle]) -> list[tuple[datetime, MarketFrame, int]]:
        output = []
        duration = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
        for index in range(WARMUP_CANDLES - 1, len(candles)):
            history = candles[index - WARMUP_CANDLES + 1 : index + 1]
            indicators = snapshot(
                [candle.high for candle in history],
                [candle.low for candle in history],
                [candle.close for candle in history],
            )
            output.append((candles[index].timestamp + duration, MarketFrame(SIGNAL_TIMEFRAMES[timeframe],candles[index].close,indicators),index))
        return output


def action_for(config: StrategyConfig, feature: ResearchFeature, fee_rate: Decimal, slippage: Decimal) -> StrategyAction | None:
    if feature.decision is Decision.WAIT or feature.signal_score < config.min_score:
        return None
    if feature.adx < config.adx_min or feature.volume_ratio < config.volume_ratio_min:
        return None
    if not config.min_atr_pct <= feature.atr_pct <= config.max_atr_pct:
        return None
    if not config.allow_sideways and feature.trend_regime == MarketRegime.SIDEWAYS:
        return None
    if config.direction_filter and (
        (feature.decision is Decision.LONG and feature.trend_regime != MarketRegime.BULL)
        or (feature.decision is Decision.SHORT and feature.trend_regime != MarketRegime.BEAR)
    ):
        return None
    expected_breakout = "UP" if feature.decision is Decision.LONG else "DOWN"
    if config.require_breakout and feature.breakout != expected_breakout:
        return None
    if config.baseline:
        stop, target = feature.proposed_stop, feature.proposed_target
    else:
        distance = feature.atr * config.atr_stop_multiplier
        stop = feature.price - distance if feature.decision is Decision.LONG else feature.price + distance
        target = feature.price + distance * config.reward_r if feature.decision is Decision.LONG else feature.price - distance * config.reward_r
    round_trip_cost = feature.price * (fee_rate * 2 + slippage * 2)
    potential_reward = abs(target - feature.price)
    if potential_reward < round_trip_cost * config.cost_coverage:
        return None
    return StrategyAction(
        Side(feature.decision),stop,target,feature.signal_score,feature.primary_regime,
        feature.atr_pct,Decimal(),feature.context(),
    )


def strategy_from_features(config: StrategyConfig, features: dict[datetime,ResearchFeature], fee_rate: Decimal, slippage: Decimal):
    def strategy(history: list[Candle]) -> StrategyAction | None:
        feature = features.get(history[-1].timestamp)
        return action_for(config,feature,fee_rate,slippage) if feature else None
    return strategy


def diagnose(result: BacktestResult) -> dict:
    dimensions = {
        "direction": lambda trade: trade.side.value,
        "hour_utc": lambda trade: f"{trade.entry_time.hour:02d}",
        "regime": lambda trade: trade.regime,
        "signal_score": lambda trade: _bucket(Decimal(trade.signal_score),(70,80,90)),
        "rsi": lambda trade: _bucket(Decimal(trade.context.get("rsi",0)),(30,40,50,60,70)),
        "macd": lambda trade: "POSITIVE" if Decimal(trade.context.get("macd",0))>0 else "NEGATIVE",
        "atr_pct": lambda trade: _bucket(Decimal(trade.context.get("atr_pct",0))*100,(0.4,0.8,1.2,2.0)),
        "adx": lambda trade: _bucket(Decimal(trade.context.get("adx",0)),(15,20,25,30)),
        "volume_ratio": lambda trade: _bucket(Decimal(trade.context.get("volume_ratio",0)),(0.8,1.0,1.2,1.5)),
        "trend_regime": lambda trade: str(trade.context.get("trend_regime","UNKNOWN")),
        "timeframe_alignment": lambda trade: str(trade.context.get("timeframe_alignment",0)),
        "breakout": lambda trade: str(trade.context.get("breakout","NONE")),
    }
    return {name:_group_stats(result.trades,key) for name,key in dimensions.items()}


def cost_diagnostics(result: BacktestResult) -> dict:
    metrics=result.metrics
    return {"gross_pnl_before_fees_slippage":metrics["gross_pnl_before_costs"],"net_pnl":metrics["net_pnl"],"fees":metrics["total_fees"],"slippage":metrics["slippage_cost"],"average_fee":metrics["average_fee"],"average_turnover":metrics["average_turnover"],"total_turnover":metrics["total_turnover"],"trades":metrics["total_trades"]}


def _group_stats(trades, key) -> dict:
    groups={}
    for trade in trades: groups.setdefault(key(trade),[]).append(trade)
    output={}
    for label,selected in groups.items():
        wins=[t for t in selected if t.pnl>0]; gp=sum((t.pnl for t in wins),Decimal()); gl=sum((t.pnl for t in selected if t.pnl<0),Decimal())
        output[label]={"trades":len(selected),"win_rate":Decimal(len(wins)*100)/len(selected),"net_pnl":sum((t.pnl for t in selected),Decimal()),"profit_factor":gp/abs(gl) if gl else Decimal("Infinity"),"expectancy":sum((t.pnl for t in selected),Decimal())/len(selected),"average_r":sum((t.r_multiple for t in selected),Decimal())/len(selected)}
    return output


def _bucket(value: Decimal, edges: tuple) -> str:
    previous=Decimal("-Infinity")
    for edge in edges:
        edge_value=Decimal(str(edge))
        if value<edge_value: return f"{previous}:{edge_value}"
        previous=edge_value
    return f"{previous}:Infinity"


def _trend_regime(ema_50: Decimal, ema_200: Decimal) -> str:
    separation=(ema_50-ema_200)/ema_200
    if separation>=Decimal("0.003"): return MarketRegime.BULL
    if separation<=Decimal("-0.003"): return MarketRegime.BEAR
    return MarketRegime.SIDEWAYS


def _volatility_regime(atr_pct: Decimal) -> str:
    if atr_pct>=Decimal("0.02"): return MarketRegime.HIGH_VOLATILITY
    if atr_pct<=Decimal("0.004"): return MarketRegime.LOW_VOLATILITY
    return "NORMAL_VOLATILITY"


def _volume_ratio(history: list[Candle]) -> Decimal:
    previous=history[-21:-1]
    average=sum((candle.volume for candle in previous),Decimal())/len(previous)
    return history[-1].volume/average if average else Decimal()


def _breakout(history: list[Candle]) -> str:
    previous=history[-21:-1]; current=history[-1]
    if current.close>max(candle.high for candle in previous): return "UP"
    if current.close<min(candle.low for candle in previous): return "DOWN"
    return "NONE"


def _adx(history: list[Candle], period: int = 14) -> Decimal:
    selected=history[-(period+1):]; plus=[]; minus=[]; true_ranges=[]
    for previous,current in zip(selected[:-1],selected[1:],strict=True):
        up=current.high-previous.high; down=previous.low-current.low
        plus.append(up if up>down and up>0 else Decimal())
        minus.append(down if down>up and down>0 else Decimal())
        true_ranges.append(max(current.high-current.low,abs(current.high-previous.close),abs(current.low-previous.close)))
    atr=sum(true_ranges,Decimal())
    if not atr: return Decimal()
    plus_di=Decimal("100")*sum(plus,Decimal())/atr; minus_di=Decimal("100")*sum(minus,Decimal())/atr
    return Decimal("100")*abs(plus_di-minus_di)/(plus_di+minus_di) if plus_di+minus_di else Decimal()


def candidate_configs() -> tuple[StrategyConfig,...]:
    return (
        StrategyConfig("baseline_v1",baseline=True),
        StrategyConfig("trend_v2_s75_a15",75,Decimal("15"),direction_filter=True,allow_sideways=False,cost_coverage=Decimal("2")),
        StrategyConfig("trend_v2_s80_a20",80,Decimal("20"),direction_filter=True,allow_sideways=False,cost_coverage=Decimal("2")),
        StrategyConfig("momentum_v2_breakout",75,Decimal("15"),Decimal("1.05"),True,False,True,Decimal("0.002"),Decimal("0.025"),Decimal("1.5"),Decimal("2"),Decimal("2")),
        StrategyConfig("regime_v2",75,Decimal("18"),Decimal("0.9"),False,True,False,Decimal("0.003"),Decimal("0.02"),Decimal("1.75"),Decimal("2"),Decimal("2.5")),
        StrategyConfig("hybrid_v3_a",78,Decimal("18"),Decimal("1.0"),False,True,False,Decimal("0.003"),Decimal("0.02"),Decimal("1.75"),Decimal("2.25"),Decimal("3")),
        StrategyConfig("hybrid_v3_b",80,Decimal("22"),Decimal("1.05"),False,True,False,Decimal("0.003"),Decimal("0.018"),Decimal("2"),Decimal("2.5"),Decimal("3")),
        StrategyConfig("hybrid_v3_breakout",78,Decimal("18"),Decimal("1.05"),True,True,False,Decimal("0.003"),Decimal("0.02"),Decimal("2"),Decimal("2.5"),Decimal("3")),
    )
