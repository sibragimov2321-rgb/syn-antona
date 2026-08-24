from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from statistics import mean

from app.backtest.core import BacktestEngine, BacktestResult, Candle, StrategyAction
from app.backtest.costs import BacktestCostProfile, PHASE4E_SPOT_PROFILES
from app.backtest.orchestrator import LOW_RISK
from app.domain.models import Side
from app.strategy_lab.features import LabFeature, _series
from app.strategy_lab.strategies import LabDirection, MeanReversionStrategy

TIMEFRAMES_4E = ("5m", "15m", "1h")
WARMUP_4E = 300


class ExecutionMode(StrEnum):
    TAKER_ONLY = "TAKER_ONLY"
    MAKER_PREFERRED = "MAKER_PREFERRED"
    HYBRID_EXECUTION = "HYBRID_EXECUTION"


@dataclass(frozen=True)
class CostAwareConfig:
    name: str
    timeframe: str
    cost_buffer: Decimal
    min_expected_move_pct: Decimal
    min_bollinger_deviation: Decimal = Decimal("1.8")
    min_distance_atr: Decimal = Decimal("1.2")
    rsi_extreme: Decimal = Decimal("30")
    min_relative_volume: Decimal = Decimal()
    max_atr_percentile: Decimal = Decimal("70")
    max_trend_strength: Decimal = Decimal("999")
    reversal_confirmation: bool = False
    cooldown_minutes: int = 0
    execution: ExecutionMode = ExecutionMode.TAKER_ONLY

    @property
    def identifier(self) -> str:
        return f"{self.timeframe}:{self.name}:{self.execution.value}"

    def to_dict(self) -> dict:
        values = asdict(self)
        values["execution"] = self.execution.value
        return values


@dataclass(frozen=True)
class Phase4EAsset:
    symbol: str
    candles: dict[str, list[Candle]]
    features: dict[str, dict[datetime, LabFeature]]
    train: dict[str, list[Candle]]
    validation: dict[str, list[Candle]]
    holdout: dict[str, list[Candle]]


@dataclass
class PendingLimit:
    action: StrategyAction
    limit_price: Decimal
    placed_at: datetime
    bars_remaining: int
    relative_volume: float


def frozen_candidate_grid() -> tuple[CostAwareConfig, ...]:
    minimum_moves = {"5m": Decimal("0.0030"), "15m": Decimal("0.0045"), "1h": Decimal("0.0075")}
    candidates = []
    for timeframe in TIMEFRAMES_4E:
        move = minimum_moves[timeframe]
        candidates.extend(
            [
                CostAwareConfig("COST_BUFFER_1_25", timeframe, Decimal("1.25"), move),
                CostAwareConfig("COST_BUFFER_1_50", timeframe, Decimal("1.50"), move),
                CostAwareConfig("COST_BUFFER_2_00", timeframe, Decimal("2.00"), move),
                CostAwareConfig("DEVIATION_2_2", timeframe, Decimal("1.50"), move, Decimal("2.2"), Decimal("1.5")),
                CostAwareConfig("RARE_LARGE_MOVE", timeframe, Decimal("1.50"), move * Decimal("1.5"), Decimal("2.6"), Decimal("1.8"), Decimal("27")),
                CostAwareConfig("STRONG_CONFIRMATION", timeframe, Decimal("1.50"), move, Decimal("2.2"), Decimal("1.5"), Decimal("28"), Decimal("0.8"), reversal_confirmation=True),
                CostAwareConfig("LOW_VOL_REGIME", timeframe, Decimal("1.50"), move, Decimal("2.2"), Decimal("1.5"), max_atr_percentile=Decimal("50"), max_trend_strength=Decimal("1.5")),
                CostAwareConfig("COOLDOWN_60", timeframe, Decimal("1.50"), move, Decimal("2.2"), Decimal("1.5"), cooldown_minutes=60),
            ]
        )
    return tuple(candidates)


def build_features(candles: list[Candle], start: datetime, end: datetime) -> dict[datetime, LabFeature]:
    values = _series(candles)
    output = {}
    for index in range(WARMUP_4E - 1, len(candles)):
        candle = candles[index]
        if not start <= candle.timestamp < end:
            continue
        price = values["close"][index]
        atr = values["atr"][index]
        ema50 = values["ema50"][index]
        ema200 = values["ema200"][index]
        separation = (ema50 - ema200) / ema200 if ema200 else 0.0
        trend_regime = "BULL" if separation >= 0.003 else "BEAR" if separation <= -0.003 else "SIDEWAYS"
        percentile = values["atr_percentile"][index]
        volatility_regime = "HIGH_VOLATILITY" if percentile >= 80 else "LOW_VOLATILITY" if percentile <= 20 else "NORMAL_VOLATILITY"
        output[candle.timestamp] = LabFeature(
            candle.timestamp,
            price,
            values["rsi"][index],
            values["adx"][index],
            atr,
            atr / price if price else 0.0,
            percentile,
            values["relative_volume"][index],
            values["vwap"][index],
            values["bollinger_width"][index],
            values["bollinger_z"][index],
            values["ema20"][index],
            ema50,
            ema200,
            abs(ema50 - ema200) / atr if atr else 0.0,
            values["momentum"][index],
            values["momentum_acceleration"][index],
            (price - ema50) / atr if atr else 0.0,
            volatility_regime,
            trend_regime,
            values["breakout"][index],
            0,
            None,
            None,
            None,
        )
    return output


class CostAwareMeanReversion:
    stop_atr = Decimal("1.3")
    reward_r = Decimal("2")
    maker_trade_through = Decimal("0.0001")

    def __init__(self, symbol: str, config: CostAwareConfig, features: dict[datetime, LabFeature], costs: BacktestCostProfile) -> None:
        self.symbol = symbol
        self.config = config
        self.features = features
        self.costs = costs
        self.strategy = MeanReversionStrategy()
        self.pending: PendingLimit | None = None
        self.last_entry: datetime | None = None
        self.unfilled_limits = 0

    def __call__(self, history) -> StrategyAction | None:
        candle = history[-1]
        feature = self.features.get(candle.timestamp)
        if self.pending is not None:
            pending = self.pending
            if candle.timestamp > pending.placed_at and self._limit_filled(candle, pending):
                self.pending = None
                self.last_entry = candle.timestamp
                context = dict(pending.action.context)
                context.update({"execution": "MAKER_ENTRY_TAKER_EXIT", "maker_fill": True, "limit_placed_at": pending.placed_at})
                return replace(pending.action, entry_price_override=pending.limit_price, entry_fee_rate=self.costs.maker_fee, context=context)
            if candle.timestamp > pending.placed_at:
                pending.bars_remaining -= 1
                if pending.bars_remaining <= 0:
                    self.pending = None
                    self.unfilled_limits += 1
                    if self.config.execution is ExecutionMode.HYBRID_EXECUTION and feature is not None:
                        action = self._action(feature, maker_entry=False)
                        if action:
                            self.last_entry = candle.timestamp
                        return action
            return None
        if feature is None or self._cooldown(candle.timestamp):
            return None
        maker = self.config.execution is not ExecutionMode.TAKER_ONLY
        action = self._action(feature, maker_entry=maker)
        if action is None:
            return None
        if not maker:
            self.last_entry = candle.timestamp
            return action
        side = action.side
        price = Decimal(str(feature.price))
        limit = price * (Decimal("1") - self.costs.spread / Decimal("2") if side is Side.LONG else Decimal("1") + self.costs.spread / Decimal("2"))
        self.pending = PendingLimit(action, limit, candle.timestamp, 2 if self.config.execution is ExecutionMode.MAKER_PREFERRED else 1, feature.relative_volume)
        return None

    def _action(self, feature: LabFeature, maker_entry: bool) -> StrategyAction | None:
        vote = self.strategy.evaluate(feature)
        if vote.direction is LabDirection.WAIT or vote.edge_score < 68:
            return None
        absolute_z = abs(Decimal(str(feature.bollinger_z)))
        absolute_distance = abs(Decimal(str(feature.distance_from_ema)))
        if absolute_z < self.config.min_bollinger_deviation or absolute_distance < self.config.min_distance_atr:
            return None
        rsi = Decimal(str(feature.rsi))
        if vote.direction is LabDirection.LONG and rsi > self.config.rsi_extreme:
            return None
        if vote.direction is LabDirection.SHORT and rsi < Decimal("100") - self.config.rsi_extreme:
            return None
        if Decimal(str(feature.relative_volume)) < self.config.min_relative_volume:
            return None
        if Decimal(str(feature.atr_percentile)) > self.config.max_atr_percentile:
            return None
        if Decimal(str(feature.trend_strength)) > self.config.max_trend_strength:
            return None
        if self.config.reversal_confirmation:
            if vote.direction is LabDirection.LONG and feature.momentum_acceleration <= 0:
                return None
            if vote.direction is LabDirection.SHORT and feature.momentum_acceleration >= 0:
                return None
        price = Decimal(str(feature.price))
        atr = Decimal(str(feature.atr))
        expected_move = min(abs(price - Decimal(str(feature.ema_20))), atr * self.stop_atr * self.reward_r)
        if not price or expected_move / price < self.config.min_expected_move_pct:
            return None
        cost = self._estimated_cost(price, expected_move, vote.direction, maker_entry)
        expected_net = expected_move - cost["total"]
        if expected_move < cost["total"] * self.config.cost_buffer or expected_net <= 0:
            return None
        stop_distance = atr * self.stop_atr
        reward = stop_distance * self.reward_r
        side = Side.LONG if vote.direction is LabDirection.LONG else Side.SHORT
        stop = price - stop_distance if side is Side.LONG else price + stop_distance
        target = price + reward if side is Side.LONG else price - reward
        if min(stop, target) <= 0:
            return None
        context = feature.context()
        context.update(
            {
                "bollinger_z": feature.bollinger_z,
                "ema_20": feature.ema_20,
                "candidate": self.config.identifier,
                "expected_move": expected_move,
                "estimated_entry_fee": cost["entry_fee"],
                "estimated_exit_fee": cost["exit_fee"],
                "estimated_spread": cost["spread"],
                "estimated_slippage": cost["slippage"],
                "estimated_total_cost": cost["total"],
                "expected_net_edge": expected_net,
                "signal_score": int(vote.edge_score),
            }
        )
        regime = feature.volatility_regime if feature.volatility_regime != "NORMAL_VOLATILITY" else feature.trend_regime
        return StrategyAction(side, stop, target, int(vote.edge_score), regime, Decimal(str(feature.atr_pct)), self.costs.spread, context)

    def _estimated_cost(self, price: Decimal, expected_move: Decimal, direction: LabDirection, maker_entry: bool) -> dict[str, Decimal]:
        exit_price = price + expected_move if direction is LabDirection.LONG else max(Decimal("0.00000001"), price - expected_move)
        entry_fee = price * (self.costs.maker_fee if maker_entry else self.costs.taker_fee)
        exit_fee = exit_price * self.costs.taker_fee
        taker_legs = Decimal("1") if maker_entry else Decimal("2")
        average_price = (price + exit_price) / Decimal("2")
        spread = average_price * self.costs.spread / Decimal("2") * taker_legs
        slippage = average_price * self.costs.slippage * taker_legs
        total = entry_fee + exit_fee + spread + slippage
        return {"entry_fee": entry_fee, "exit_fee": exit_fee, "spread": spread, "slippage": slippage, "total": total}

    def _limit_filled(self, candle: Candle, pending: PendingLimit) -> bool:
        if pending.action.side is Side.LONG:
            traded_through = candle.low < pending.limit_price * (Decimal("1") - self.maker_trade_through)
        else:
            traded_through = candle.high > pending.limit_price * (Decimal("1") + self.maker_trade_through)
        if not traded_through or candle.volume <= 0:
            return False
        probability = min(0.65, max(0.25, 0.35 + min(pending.relative_volume, 2.0) * 0.15))
        digest = sha256(f"42|{self.symbol}|{pending.placed_at.isoformat()}|{pending.action.side}".encode()).digest()
        draw = int.from_bytes(digest[:8], "big") / (2**64 - 1)
        return draw < probability

    def _cooldown(self, timestamp: datetime) -> bool:
        return bool(self.last_entry and timestamp < self.last_entry + timedelta(minutes=self.config.cooldown_minutes))


def scaled_costs(profile: BacktestCostProfile, multiple: Decimal) -> BacktestCostProfile:
    return replace(
        profile,
        maker_fee=profile.maker_fee * multiple,
        taker_fee=profile.taker_fee * multiple,
        slippage=profile.slippage * multiple,
        spread=profile.spread * multiple,
    )


def evaluate(symbol: str, candles: list[Candle], features: dict[datetime, LabFeature], config: CostAwareConfig, profile: BacktestCostProfile, starting_balance: Decimal = Decimal("1000")) -> tuple[BacktestResult, int]:
    strategy = CostAwareMeanReversion(symbol, config, features, profile)
    engine = BacktestEngine(fee_rate=profile.taker_fee, slippage=profile.slippage, spread=profile.spread, market_type=profile.market_type)
    result = engine.run(candles, strategy, starting_balance, risk_profile=LOW_RISK, retain_equity=False)
    return result, strategy.unfilled_limits


def aggregate(results: dict[str, BacktestResult]) -> dict:
    trades = [trade for result in results.values() for trade in result.trades]
    metrics = [result.metrics for result in results.values()]
    gross_profit = sum((Decimal(str(item["gross_profit_before_costs"])) for item in metrics), Decimal())
    gross_loss = sum((Decimal(str(item["gross_loss_before_costs"])) for item in metrics), Decimal())
    net_profit = sum((Decimal(str(item["gross_profit"])) for item in metrics), Decimal())
    net_loss = sum((Decimal(str(item["gross_loss"])) for item in metrics), Decimal())
    gross = sum((trade.pnl_before_costs for trade in trades), Decimal())
    net = sum((trade.pnl for trade in trades), Decimal())
    fees = sum((trade.fees for trade in trades), Decimal())
    spread = sum((trade.spread_cost for trade in trades), Decimal())
    slippage = sum((trade.slippage_cost for trade in trades), Decimal())
    holding = [Decimal(str((trade.exit_time - trade.entry_time).total_seconds())) for trade in trades]
    return {
        "trades": len(trades),
        "gross_pnl": gross,
        "fees": fees,
        "spread": spread,
        "slippage": slippage,
        "net_pnl": net,
        "gross_pf": gross_profit / abs(gross_loss) if gross_loss else (Decimal("Infinity") if gross_profit else Decimal()),
        "net_pf": net_profit / abs(net_loss) if net_loss else (Decimal("Infinity") if net_profit else Decimal()),
        "gross_expectancy": gross / len(trades) if trades else Decimal(),
        "net_expectancy": net / len(trades) if trades else Decimal(),
        "return_pct": net / (Decimal("1000") * len(results)) * Decimal("100") if results else Decimal(),
        "max_drawdown_pct": max((Decimal(str(item["max_drawdown_pct"])) for item in metrics), default=Decimal()),
        "turnover": sum((trade.turnover for trade in trades), Decimal()),
        "average_holding_seconds": sum(holding, Decimal()) / len(holding) if holding else Decimal(),
    }


def serialize_result(result: BacktestResult) -> dict:
    return {"trades": len(result.trades), "metrics": result.metrics}


def selection_score(summary: dict) -> float:
    if summary["trades"] < 15:
        return -1000 + summary["trades"]
    return min(float(summary["net_pf"]), 5) * 4 + float(summary["net_expectancy"]) * 2 + float(summary["return_pct"]) * 0.2 - float(summary["max_drawdown_pct"]) * 0.15


def validation_pass(summary: dict) -> bool:
    return summary["trades"] >= 15 and summary["net_pf"] > 1 and summary["net_expectancy"] > 0 and summary["return_pct"] > 0


def attribution(trades, minimum_group: int = 5) -> dict:
    dimensions = {
        "signal_score": lambda trade: _numeric_bucket(float(trade.signal_score), (70, 80, 90)),
        "volatility": lambda trade: trade.context.get("volatility_regime", "UNKNOWN"),
        "atr_percentile": lambda trade: _numeric_bucket(float(trade.context.get("atr_percentile", 0)), (20, 40, 60, 80)),
        "distance_from_mean": lambda trade: _absolute_bucket(float(trade.context.get("distance_from_ema", 0)), (1.5, 2, 3)),
        "rsi": lambda trade: _numeric_bucket(float(trade.context.get("rsi", 0)), (20, 30, 70, 80)),
        "bollinger_deviation": lambda trade: _absolute_bucket(float(trade.context.get("bollinger_z", 0)), (2, 2.5, 3)),
        "volume": lambda trade: _numeric_bucket(float(trade.context.get("relative_volume", 0)), (0.5, 1, 1.5, 2)),
        "trend_strength": lambda trade: _numeric_bucket(float(trade.context.get("trend_strength", 0)), (0.5, 1, 2, 3)),
        "side": lambda trade: trade.side.value,
        "market_regime": lambda trade: trade.regime,
        "hour_utc": lambda trade: str(trade.entry_time.hour),
        "holding_time": lambda trade: _numeric_bucket((trade.exit_time - trade.entry_time).total_seconds() / 3600, (0.5, 1, 2, 4, 8)),
    }
    output = {}
    for name, classifier in dimensions.items():
        groups = {}
        for trade in trades:
            groups.setdefault(str(classifier(trade)), []).append(trade)
        output[name] = {}
        for label, selected in groups.items():
            gross = sum((trade.pnl_before_costs for trade in selected), Decimal())
            net = sum((trade.pnl for trade in selected), Decimal())
            output[name][label] = {
                "trades": len(selected),
                "gross_expectancy": gross / len(selected),
                "net_expectancy": net / len(selected),
                "negative_net_group": len(selected) >= minimum_group and net < 0,
            }
    return output


def persistent_negative_groups(train: dict, validation: dict) -> dict[str, list[str]]:
    output = {}
    for dimension, groups in train.items():
        persistent = []
        for label, train_values in groups.items():
            validation_values = validation.get(dimension, {}).get(label)
            if train_values["negative_net_group"] and validation_values and validation_values["negative_net_group"]:
                persistent.append(label)
        output[dimension] = persistent
    return output


def _numeric_bucket(value: float, boundaries: tuple[float, ...]) -> str:
    lower = float("-inf")
    for upper in boundaries:
        if value < upper:
            return f"{lower:g}..{upper:g}"
        lower = upper
    return f"{lower:g}..inf"


def _absolute_bucket(value: float, boundaries: tuple[float, ...]) -> str:
    return _numeric_bucket(abs(value), boundaries)


def result_table_row(name: str, config: CostAwareConfig, split: str, results: dict[str, BacktestResult], unfilled: int = 0) -> dict:
    summary = aggregate(results)
    return {
        "candidate": name,
        "timeframe": config.timeframe,
        "execution": config.execution.value,
        "split": split,
        **summary,
        "unfilled_limits": unfilled,
        "BTC_result": serialize_result(results["BTC/USDT"]),
        "ETH_result": serialize_result(results["ETH/USDT"]),
        "SOL_result": serialize_result(results["SOL/USDT"]),
    }


def average(values) -> float:
    return mean(values) if values else 0.0


DEFAULT_PROFILE = PHASE4E_SPOT_PROFILES["bybit"]
