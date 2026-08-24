import csv
import json
import random
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from math import sqrt
from pathlib import Path
from statistics import mean
from uuid import uuid4

from app.domain.models import RiskProfile, Side, TradeIntent
from app.risk.manager import RiskManager
from app.trading.positions import PositionManager


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class MarketRegime(StrEnum):
    BULL = "BULL"
    BEAR = "BEAR"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"


@dataclass(frozen=True)
class StrategyAction:
    side: Side
    stop_loss: Decimal
    take_profit: Decimal
    signal_score: int = 0
    regime: str = MarketRegime.SIDEWAYS
    volatility_pct: Decimal = Decimal()
    spread_pct: Decimal = Decimal()
    context: dict = field(default_factory=dict)
    break_even_enabled: bool = False
    trailing_distance: Decimal | None = None
    entry_price_override: Decimal | None = None
    entry_fee_rate: Decimal | None = None


@dataclass(frozen=True)
class BacktestTrade:
    side: Side
    entry_time: datetime
    exit_time: datetime
    entry: Decimal
    exit: Decimal
    quantity: Decimal
    pnl: Decimal
    fees: Decimal
    reason: str
    signal_score: int = 0
    regime: str = MarketRegime.SIDEWAYS
    risk_amount: Decimal = Decimal()
    slippage_cost: Decimal = Decimal()
    context: dict = field(default_factory=dict)
    funding: Decimal = Decimal()
    spread_cost: Decimal = Decimal()

    @property
    def r_multiple(self) -> Decimal:
        return self.pnl / self.risk_amount if self.risk_amount else Decimal()

    @property
    def turnover(self) -> Decimal:
        return (self.entry + self.exit) * self.quantity

    @property
    def pnl_before_costs(self) -> Decimal:
        return self.pnl + self.fees + self.slippage_cost + self.funding + self.spread_cost


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    balance: Decimal
    equity: Decimal
    drawdown: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class BacktestResult:
    run_id: str
    starting_balance: Decimal
    final_equity: Decimal
    trades: list[BacktestTrade]
    equity_curve: list[tuple[datetime, Decimal]]
    metrics: dict
    equity_points: list[EquityPoint] = field(default_factory=list)


def validate_candles(candles: list[Candle], interval_seconds: int | None = None) -> list[str]:
    errors: list[str] = []
    for index, candle in enumerate(candles):
        if (
            min(candle.open, candle.high, candle.low, candle.close) <= 0
            or candle.volume < 0
            or candle.low > min(candle.open, candle.close)
            or candle.high < max(candle.open, candle.close)
            or candle.low > candle.high
        ):
            errors.append(f"invalid OHLCV at {index}")
        if index and candle.timestamp <= candles[index - 1].timestamp:
            errors.append(f"duplicate or unordered candle at {index}")
        if interval_seconds and index:
            delta = (candle.timestamp - candles[index - 1].timestamp).total_seconds()
            if delta != interval_seconds:
                errors.append(f"missing candle gap at {index}")
    return errors


class BacktestEngine:
    """Candle execution engine. A strategy only receives history closed at decision time."""

    def __init__(
        self,
        fee_rate: Decimal = Decimal("0.0006"),
        slippage: Decimal = Decimal("0.0002"),
        funding_rate: Decimal = Decimal(),
        risk_manager: RiskManager | None = None,
        *,
        spread: Decimal = Decimal(),
        market_type: str = "spot",
        funding_interval_hours: int = 8,
    ) -> None:
        """Create a market-order backtester.

        ``fee_rate`` is the taker rate charged once on each filled leg. ``spread``
        is the full bid/ask spread; a market fill crosses half of it per leg.
        A positive perpetual funding rate means LONG pays and SHORT receives.
        """
        if min(fee_rate, slippage, spread) < 0:
            raise ValueError("Execution costs cannot be negative")
        if market_type == "spot" and funding_rate:
            raise ValueError("Spot datasets must use zero funding")
        if funding_interval_hours <= 0:
            raise ValueError("Funding interval must be positive")
        self.fee_rate = fee_rate
        self.slippage = slippage
        self.funding_rate = funding_rate
        self.spread = spread
        self.market_type = market_type
        self.funding_interval = timedelta(hours=funding_interval_hours)
        self.risk_manager = risk_manager or RiskManager()

    def run(self, candles: list[Candle], strategy, starting_balance: Decimal = Decimal("1000"), risk_pct: Decimal = Decimal("0.005"), risk_profile: RiskProfile | None = None, retain_equity: bool = True, leverage: Decimal = Decimal("1")) -> BacktestResult:
        if not candles: raise ValueError("Backtest requires candles")
        interval_seconds = int((candles[1].timestamp - candles[0].timestamp).total_seconds()) if len(candles) > 1 else None
        if errors := validate_candles(candles, interval_seconds): raise ValueError("; ".join(errors))
        if leverage <= 0:
            raise ValueError("Leverage must be positive")
        profile = risk_profile or RiskProfile(risk_per_trade_pct=risk_pct)
        bar_duration = candles[1].timestamp - candles[0].timestamp if len(candles) > 1 else timedelta()
        balance, open_trade, trades, curve, equity_points = starting_balance, None, [], [], []
        peak_equity = starting_balance
        daily_pnl: dict[date, Decimal] = {}; consecutive_losses = 0; last_loss_at = None
        for index, candle in enumerate(candles):
            event_time = candle.timestamp + bar_duration
            if open_trade:
                closed = self._maybe_close(open_trade, candle, event_time)
                if closed:
                    balance += closed.pnl; trades.append(closed)
                    daily_pnl[event_time.date()] = daily_pnl.get(event_time.date(), Decimal()) + closed.pnl
                    consecutive_losses = consecutive_losses + 1 if closed.pnl < 0 else 0
                    last_loss_at = event_time if closed.pnl < 0 else None; open_trade = None
                else:
                    self._advance_protection(open_trade, candle)
            if open_trade is None:
                action = self._normalize_action(strategy(_CandleHistoryView(candles, index + 1)))
                if action:
                    cooldown = bool(last_loss_at and event_time < last_loss_at + timedelta(minutes=profile.cooldown_minutes))
                    losses = 0 if last_loss_at and not cooldown else consecutive_losses
                    intent = TradeIntent(f"bt-{index}",0,"BACKTEST",action.side,candle.close,action.stop_loss,action.take_profit,balance,daily_pnl.get(event_time.date(),Decimal()),0,losses,leverage,balance,action.volatility_pct,action.spread_pct,cooldown)
                    approval = self.risk_manager.approve(intent, profile)
                    if approval.approved:
                        fill = action.entry_price_override or self._entry_fill(candle.close, action.side)
                        per_unit_risk = abs(fill-action.stop_loss)
                        quantity = min(
                            approval.quantity,
                            approval.risk_amount/per_unit_risk,
                            profile.max_position_notional/fill,
                            balance*leverage/fill,
                        ).quantize(Decimal("0.000001"), ROUND_DOWN)
                        if quantity > 0:
                            actual_risk=per_unit_risk*quantity
                            maker_entry = action.entry_price_override is not None
                            open_trade = dict(side=action.side,entry=fill,entry_reference=candle.close,quantity=quantity,stop=action.stop_loss,target=action.take_profit,opened=event_time,score=action.signal_score,regime=action.regime,risk=actual_risk,context=action.context,initial_unit_risk=per_unit_risk,break_even=action.break_even_enabled,trailing=action.trailing_distance,high_watermark=fill,low_watermark=fill,entry_fee_rate=action.entry_fee_rate if action.entry_fee_rate is not None else self.fee_rate,entry_slippage=Decimal() if maker_entry else candle.close*quantity*self.slippage,entry_spread=Decimal() if maker_entry else candle.close*quantity*self.spread/Decimal("2"))
            unrealized = self._unrealized(open_trade, candle.close, event_time)
            equity = balance + unrealized
            peak_equity = max(peak_equity, equity)
            curve.append((event_time, equity))
            equity_points.append(EquityPoint(event_time,balance,equity,peak_equity-equity,balance-starting_balance,unrealized))
        if open_trade:
            final_time = candles[-1].timestamp + bar_duration
            closed = self._close(open_trade,candles[-1].close,final_time,"END_OF_DATA")
            balance += closed.pnl; trades.append(closed); curve[-1] = (final_time,balance)
            peak_equity=max(peak_equity,balance)
            equity_points[-1]=EquityPoint(final_time,balance,balance,peak_equity-balance,balance-starting_balance,Decimal())
        calculated = calculate_metrics(trades,curve,starting_balance)
        return BacktestResult(uuid4().hex,starting_balance,balance,trades,curve if retain_equity else [],calculated,equity_points if retain_equity else [])

    @staticmethod
    def _normalize_action(raw) -> StrategyAction | None:
        if raw is None: return None
        if isinstance(raw, StrategyAction): return raw
        side, stop, target = raw
        return StrategyAction(side, stop, target)

    def _entry_fill(self, price: Decimal, side: Side) -> Decimal:
        adverse = self.slippage + self.spread / Decimal("2")
        return price * (Decimal("1") + adverse if side is Side.LONG else Decimal("1") - adverse)

    def _maybe_close(self, trade: dict, candle: Candle, timestamp: datetime) -> BacktestTrade | None:
        resolved=PositionManager.resolve_candle_exit(
            trade["side"],trade["stop"],trade["target"],candle.high,candle.low
        )
        if not resolved: return None
        price,reason=resolved
        return self._close(trade,price,timestamp,reason)

    def _close(self, trade: dict, requested: Decimal, timestamp: datetime, reason: str) -> BacktestTrade:
        side=trade["side"]
        adverse = self.slippage + self.spread / Decimal("2")
        exit_price=requested*(Decimal("1")-adverse if side is Side.LONG else Decimal("1")+adverse)
        gross=(exit_price-trade["entry"])*trade["quantity"]*(1 if side is Side.LONG else -1)
        entry_fee=trade["entry"]*trade["quantity"]*trade["entry_fee_rate"]
        exit_fee=exit_price*trade["quantity"]*self.fee_rate
        fees=entry_fee+exit_fee
        funding=self._funding_cost(trade,timestamp)
        slippage_cost=trade["entry_slippage"]+requested*trade["quantity"]*self.slippage
        spread_cost=trade["entry_spread"]+requested*trade["quantity"]*self.spread/Decimal("2")
        return BacktestTrade(side,trade["opened"],timestamp,trade["entry"],exit_price,trade["quantity"],gross-fees-funding,fees,reason,trade["score"],trade["regime"],trade["risk"],slippage_cost,trade["context"],funding,spread_cost)

    def _unrealized(self, trade: dict | None, price: Decimal, timestamp: datetime) -> Decimal:
        if not trade: return Decimal()
        pnl=(price-trade["entry"])*trade["quantity"]
        gross = pnl if trade["side"] is Side.LONG else -pnl
        entry_fee = trade["entry"] * trade["quantity"] * trade["entry_fee_rate"]
        return gross - entry_fee - self._funding_cost(trade, timestamp)

    def _funding_cost(self, trade: dict, timestamp: datetime) -> Decimal:
        if not self.funding_rate or timestamp <= trade["opened"]:
            return Decimal()
        interval_seconds = int(self.funding_interval.total_seconds())
        opened_epoch = int(trade["opened"].timestamp())
        exit_epoch = int(timestamp.timestamp())
        settlements = exit_epoch // interval_seconds - opened_epoch // interval_seconds
        if opened_epoch % interval_seconds == 0:
            settlements = max(0, settlements)
        if settlements <= 0:
            return Decimal()
        direction = Decimal("1") if trade["side"] is Side.LONG else Decimal("-1")
        return trade["entry"] * trade["quantity"] * self.funding_rate * settlements * direction

    @staticmethod
    def _advance_protection(trade: dict, candle: Candle) -> None:
        """Advance protection for the next candle only; never widen a stop."""
        trade["high_watermark"] = max(trade["high_watermark"], candle.high)
        trade["low_watermark"] = min(trade["low_watermark"], candle.low)
        side = trade["side"]
        if trade["break_even"]:
            reached = (
                trade["high_watermark"] >= trade["entry"] + trade["initial_unit_risk"]
                if side is Side.LONG
                else trade["low_watermark"] <= trade["entry"] - trade["initial_unit_risk"]
            )
            if reached:
                trade["stop"] = max(trade["stop"], trade["entry"]) if side is Side.LONG else min(trade["stop"], trade["entry"])
        if trade["trailing"] is not None:
            candidate = (
                trade["high_watermark"] - trade["trailing"]
                if side is Side.LONG
                else trade["low_watermark"] + trade["trailing"]
            )
            trade["stop"] = max(trade["stop"], candidate) if side is Side.LONG else min(trade["stop"], candidate)


class _CandleHistoryView(Sequence[Candle]):
    """Read-only prefix view: strategies see no future candles without O(n) copies."""

    def __init__(self, candles: list[Candle], stop: int) -> None:
        self._candles = candles
        self._stop = stop

    def __len__(self) -> int:
        return self._stop

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(self._stop)
            return self._candles[start:stop:step]
        normalized = index + self._stop if index < 0 else index
        if normalized < 0 or normalized >= self._stop:
            raise IndexError(index)
        return self._candles[normalized]

    def __add__(self, other):
        return self._candles[:self._stop] + list(other)

    def __radd__(self, other):
        return list(other) + self._candles[:self._stop]


def calculate_metrics(trades: list[BacktestTrade], curve: list[tuple[datetime, Decimal]], start: Decimal) -> dict:
    pnls=[trade.pnl for trade in trades]; wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<0]
    gp=sum(wins,Decimal()); gl=sum(losses,Decimal()); net=sum(pnls,Decimal()); peak=start; draw=Decimal(); draw_pct=Decimal()
    for _,equity in curve:
        peak=max(peak,equity); draw=max(draw,peak-equity)
        draw_pct=max(draw_pct,(peak-equity)/peak*100 if peak else Decimal())
    returns=[float(p/start) for p in pnls] if start else []; avg=mean(returns) if returns else 0.0
    variance=mean([(value-avg)**2 for value in returns]) if returns else 0.0
    downside=mean([min(0.0,value)**2 for value in returns]) if returns else 0.0
    r_values=[trade.r_multiple for trade in trades]; win_streak,loss_streak=_streaks(pnls)
    holding=[Decimal(str((t.exit_time-t.entry_time).total_seconds())) for t in trades]
    long=[t for t in trades if t.side is Side.LONG]; short=[t for t in trades if t.side is Side.SHORT]
    total_fees=sum((t.fees for t in trades),Decimal()); slippage_cost=sum((t.slippage_cost for t in trades),Decimal())
    total_funding=sum((t.funding for t in trades),Decimal()); spread_cost=sum((t.spread_cost for t in trades),Decimal())
    gross_values=[t.pnl_before_costs for t in trades]
    gross_before_profit=sum((p for p in gross_values if p>0),Decimal()); gross_before_loss=sum((p for p in gross_values if p<0),Decimal())
    gross_pf=gross_before_profit/abs(gross_before_loss) if gross_before_loss else (Decimal("Infinity") if gross_before_profit else Decimal())
    return {"total_trades":len(trades),"wins":len(wins),"losses":len(losses),"win_rate":Decimal(len(wins)*100)/len(trades) if trades else Decimal(),"net_pnl":net,"return_pct":net/start*100 if start else Decimal(),"gross_profit":gp,"gross_loss":gl,"gross_pnl_before_costs":sum(gross_values,Decimal()),"gross_profit_before_costs":gross_before_profit,"gross_loss_before_costs":gross_before_loss,"gross_profit_factor":gross_pf,"profit_factor":gp/abs(gl) if gl else (Decimal("Infinity") if gp else Decimal()),"expectancy":net/len(trades) if trades else Decimal(),"average_win":gp/len(wins) if wins else Decimal(),"average_loss":gl/len(losses) if losses else Decimal(),"average_r":sum(r_values,Decimal())/len(r_values) if r_values else Decimal(),"max_drawdown":draw,"max_drawdown_pct":draw_pct,"recovery_factor":net/draw if draw else Decimal(),"sharpe":Decimal(str(avg/sqrt(variance)*sqrt(len(trades)))) if variance else Decimal(),"sortino":Decimal(str(avg/sqrt(downside)*sqrt(len(trades)))) if downside else Decimal(),"longest_winning_streak":win_streak,"longest_losing_streak":loss_streak,"average_holding_seconds":sum(holding,Decimal())/len(holding) if holding else Decimal(),"total_fees":total_fees,"slippage_cost":slippage_cost,"funding":total_funding,"spread_cost":spread_cost,"total_costs":total_fees+slippage_cost+total_funding+spread_cost,"total_turnover":sum((t.turnover for t in trades),Decimal()),"average_turnover":sum((t.turnover for t in trades),Decimal())/len(trades) if trades else Decimal(),"average_fee":total_fees/len(trades) if trades else Decimal(),"average_cost_per_trade":(total_fees+slippage_cost+total_funding+spread_cost)/len(trades) if trades else Decimal(),"long_trades":len(long),"long_wins":sum(t.pnl>0 for t in long),"long_pnl":sum((t.pnl for t in long),Decimal()),"long_win_rate":Decimal(sum(t.pnl>0 for t in long)*100)/len(long) if long else Decimal(),"short_trades":len(short),"short_wins":sum(t.pnl>0 for t in short),"short_pnl":sum((t.pnl for t in short),Decimal()),"short_win_rate":Decimal(sum(t.pnl>0 for t in short)*100)/len(short) if short else Decimal()}


def metrics(trades, curve, start): return calculate_metrics(trades,curve,start)


def _streaks(pnls: list[Decimal]) -> tuple[int,int]:
    bw=bl=cw=cl=0
    for pnl in pnls:
        cw=cw+1 if pnl>0 else 0; cl=cl+1 if pnl<0 else 0; bw=max(bw,cw); bl=max(bl,cl)
    return bw,bl


def regime_statistics(trades: list[BacktestTrade], start: Decimal) -> dict[str,dict]:
    return {regime.value:calculate_metrics(selected:=[t for t in trades if t.regime==regime.value],_curve_from_trades(selected,start),start) for regime in MarketRegime}


def signal_score_calibration(trades: list[BacktestTrade]) -> dict[str,dict]:
    output={}
    for label,low,high in (("60-69",60,69),("70-79",70,79),("80-89",80,89),("90-100",90,100)):
        selected=[t for t in trades if low<=t.signal_score<=high]; wins=[t for t in selected if t.pnl>0]; gp=sum((t.pnl for t in wins),Decimal()); gl=sum((t.pnl for t in selected if t.pnl<0),Decimal())
        output[label]={"trades":len(selected),"win_rate":Decimal(len(wins)*100)/len(selected) if selected else Decimal(),"net_pnl":sum((t.pnl for t in selected),Decimal()),"profit_factor":gp/abs(gl) if gl else (Decimal("Infinity") if gp else Decimal()),"average_r":sum((t.r_multiple for t in selected),Decimal())/len(selected) if selected else Decimal()}
    return output


def monte_carlo(trades: list[BacktestTrade], starting: Decimal, simulations: int = 1000, seed: int | None = 42) -> dict:
    rng=random.Random(seed); values=[t.pnl for t in trades]; finals=[]; drawdowns=[]
    for _ in range(simulations):
        equity=peak=starting; max_drawdown=Decimal()
        for pnl in (rng.choices(values,k=len(values)) if values else []): equity+=pnl; peak=max(peak,equity); max_drawdown=max(max_drawdown,peak-equity)
        finals.append(equity); drawdowns.append(max_drawdown)
    finals.sort()
    return {"simulations":simulations,"median_final_equity":_percentile(finals,.5),"worst_5pct":_percentile(finals,.05),"best_5pct":_percentile(finals,.95),"expected_max_drawdown":sum(drawdowns,Decimal())/simulations,"probability_losing_capital":Decimal(sum(value<starting for value in finals))/simulations,"probability_dd_10":Decimal(sum(d>starting*Decimal("0.10") for d in drawdowns))/simulations,"probability_dd_20":Decimal(sum(d>starting*Decimal("0.20") for d in drawdowns))/simulations}


def _percentile(values: list[Decimal], q: float) -> Decimal: return values[min(len(values)-1,int((len(values)-1)*q))] if values else Decimal()


def walk_forward(candles: list[Candle], engine: BacktestEngine, strategy, start: Decimal, risk_profile: RiskProfile | None = None) -> dict[str,BacktestResult]:
    first,second=int(len(candles)*.6),int(len(candles)*.8); parts={"train":candles[:first],"validation":candles[first:second],"out_of_sample":candles[second:]}
    return {name:engine.run(part,strategy,start,risk_profile=risk_profile) for name,part in parts.items()}


def overfitting_warning(train: BacktestResult, oos: BacktestResult) -> bool:
    return train.metrics["return_pct"]>0 and (oos.metrics["return_pct"]<0 or oos.metrics["return_pct"]<train.metrics["return_pct"]*Decimal("0.25"))


def buy_and_hold(candles: list[Candle], starting: Decimal) -> Decimal:
    if not candles: raise ValueError("Benchmark requires candles")
    return starting*candles[-1].close/candles[0].close


def validation_status(result: BacktestResult, oos: BacktestResult | None = None) -> str:
    m=result.metrics
    if m["total_trades"]<30: return "NEEDS_MORE_DATA"
    if m["expectancy"]<=0 or m["profit_factor"]<=1 or m["max_drawdown_pct"]>30 or (oos and (oos.metrics["expectancy"]<=0 or oos.metrics["return_pct"]<=0)): return "FAILED"
    if m["max_drawdown_pct"]<=20 and oos: return "PROMISING"
    return "WEAK"


def export(result: BacktestResult, path: Path, format: str = "csv") -> None:
    if format=="json": path.write_text(json.dumps(asdict(result),default=str),encoding="utf-8"); return
    with path.open("w",newline="",encoding="utf-8") as file:
        writer=csv.DictWriter(file,fieldnames=BacktestTrade.__dataclass_fields__); writer.writeheader(); writer.writerows(asdict(t) for t in result.trades)


def _curve_from_trades(trades: list[BacktestTrade], start: Decimal) -> list[tuple[datetime,Decimal]]:
    equity=start; curve=[]
    for trade in trades: equity+=trade.pnl; curve.append((trade.exit_time,equity))
    return curve or [(datetime.now(UTC),start)]
