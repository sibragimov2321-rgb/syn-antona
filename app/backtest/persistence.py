from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.backtest.core import BacktestResult, BacktestTrade, Candle, EquityPoint, validate_candles
from app.db import (
    BacktestEquityRecord,
    BacktestMetricRecord,
    BacktestRunRecord,
    BacktestTradeRecord,
    HistoricalCandleRecord,
    MarketRegimeRecord,
    MonteCarloRecord,
    SessionLocal,
    StrategyExperimentRecord,
    StrategyVersionRecord,
)
from app.domain.models import Side

TIMEFRAME_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


class HistoricalCandleCache:
    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self.session_factory = session_factory

    def load(self, exchange: str, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Candle]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(HistoricalCandleRecord).where(
                    HistoricalCandleRecord.exchange == exchange,
                    HistoricalCandleRecord.symbol == symbol,
                    HistoricalCandleRecord.timeframe == timeframe,
                    HistoricalCandleRecord.timestamp >= start,
                    HistoricalCandleRecord.timestamp < end,
                ).order_by(HistoricalCandleRecord.timestamp)
            ).all()
        return [Candle(_utc(row.timestamp), row.open, row.high, row.low, row.close, row.volume) for row in rows]

    def missing_ranges(self, exchange: str, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        cached = {candle.timestamp for candle in self.load(exchange, symbol, timeframe, start, end)}
        step = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
        cursor = _floor_time(start, TIMEFRAME_SECONDS[timeframe])
        missing: list[tuple[datetime, datetime]] = []
        range_start: datetime | None = None
        while cursor < end:
            if cursor not in cached and range_start is None:
                range_start = cursor
            if cursor in cached and range_start is not None:
                missing.append((range_start, cursor))
                range_start = None
            cursor += step
        if range_start is not None:
            missing.append((range_start, end))
        return missing

    def save(self, exchange: str, symbol: str, timeframe: str, candles: list[Candle]) -> None:
        if not candles:
            return
        values = [dict(exchange=exchange, symbol=symbol, timeframe=timeframe, timestamp=item.timestamp, open=item.open, high=item.high, low=item.low, close=item.close, volume=item.volume) for item in candles]
        with self.session_factory.begin() as session:
            if session.bind.dialect.name == "postgresql":
                for offset in range(0, len(values), 500):
                    statement = pg_insert(HistoricalCandleRecord).values(values[offset:offset + 500]).on_conflict_do_nothing(constraint="uq_candle_key")
                    session.execute(statement)
            elif session.bind.dialect.name == "sqlite":
                for offset in range(0, len(values), 500):
                    statement = sqlite_insert(HistoricalCandleRecord).values(values[offset:offset + 500]).on_conflict_do_nothing(index_elements=["exchange", "symbol", "timeframe", "timestamp"])
                    session.execute(statement)
            else:
                for value in values:
                    exists = session.scalar(select(HistoricalCandleRecord.id).where(HistoricalCandleRecord.exchange==exchange,HistoricalCandleRecord.symbol==symbol,HistoricalCandleRecord.timeframe==timeframe,HistoricalCandleRecord.timestamp==value["timestamp"]))
                    if not exists: session.add(HistoricalCandleRecord(**value))


class CachedHistoricalDataProvider:
    def __init__(self, provider, cache: HistoricalCandleCache) -> None:
        self.provider, self.cache, self.name = provider, cache, provider.name

    async def fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Candle]:
        for missing_start, missing_end in self.cache.missing_ranges(self.name, symbol, timeframe, start, end):
            downloaded = await self.provider.fetch(symbol, timeframe, missing_start, missing_end)
            self.cache.save(self.name, symbol, timeframe, downloaded)
        candles = self.cache.load(self.name, symbol, timeframe, start, end)
        if errors := validate_candles(candles, TIMEFRAME_SECONDS[timeframe]):
            raise ValueError("Historical cache validation failed: " + "; ".join(errors))
        return candles


class BacktestRepository:
    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self.session_factory = session_factory

    def save(self, result: BacktestResult, exchange: str, symbol: str, risk_profile: str, validation: str, regimes: dict[datetime,str], monte_carlo: dict[str,Decimal], metric_sections: dict[str,dict] | None = None, strategy_version: str = "baseline_v1") -> None:
        with self.session_factory.begin() as session:
            session.add(BacktestRunRecord(id=result.run_id,exchange=exchange,symbol=symbol,timeframe="5m",started_at=result.equity_curve[0][0],ended_at=result.equity_curve[-1][0],starting_balance=result.starting_balance,final_equity=result.final_equity,risk_profile=risk_profile,validation_status=validation,strategy_version=strategy_version))
            session.add_all([BacktestTradeRecord(run_id=result.run_id,side=t.side,entry_time=t.entry_time,exit_time=t.exit_time,entry=t.entry,exit=t.exit,quantity=t.quantity,pnl=t.pnl,fees=t.fees,slippage_cost=t.slippage_cost,funding=t.funding,spread_cost=t.spread_cost,risk_amount=t.risk_amount,signal_score=t.signal_score,regime=t.regime,reason=t.reason,context_json=json.dumps(t.context,default=str,sort_keys=True)) for t in result.trades])
            session.add_all([BacktestEquityRecord(run_id=result.run_id,timestamp=p.timestamp,balance=p.balance,equity=p.equity,drawdown=p.drawdown,realized_pnl=p.realized_pnl,unrealized_pnl=p.unrealized_pnl) for p in result.equity_points])
            sections={"overall":result.metrics,**(metric_sections or {})}
            session.add_all([BacktestMetricRecord(run_id=result.run_id,section=section,name=name,value=str(value)) for section,values in sections.items() for name,value in values.items()])
            session.add_all([MarketRegimeRecord(run_id=result.run_id,timestamp=timestamp,regime=regime) for timestamp,regime in regimes.items()])
            session.add(MonteCarloRecord(run_id=result.run_id,simulations=int(monte_carlo["simulations"]),median_final_equity=monte_carlo["median_final_equity"],worst_5pct=monte_carlo["worst_5pct"],best_5pct=monte_carlo["best_5pct"],expected_max_drawdown=monte_carlo["expected_max_drawdown"],probability_dd_10=monte_carlo["probability_dd_10"],probability_dd_20=monte_carlo["probability_dd_20"]))

    def load_run(self, run_id: str) -> BacktestRunRecord | None:
        with self.session_factory() as session:
            record=session.get(BacktestRunRecord,run_id)
            if record: session.expunge(record)
            return record

    def load_result(self, run_id: str) -> BacktestResult | None:
        with self.session_factory() as session:
            run=session.get(BacktestRunRecord,run_id)
            if not run: return None
            trade_rows=session.scalars(select(BacktestTradeRecord).where(BacktestTradeRecord.run_id==run_id).order_by(BacktestTradeRecord.id)).all()
            point_rows=session.scalars(select(BacktestEquityRecord).where(BacktestEquityRecord.run_id==run_id).order_by(BacktestEquityRecord.timestamp)).all()
            metric_rows=session.scalars(select(BacktestMetricRecord).where(BacktestMetricRecord.run_id==run_id,BacktestMetricRecord.section=="overall")).all()
            trades=[BacktestTrade(side=Side(row.side),entry_time=_utc(row.entry_time),exit_time=_utc(row.exit_time),entry=row.entry,exit=row.exit,quantity=row.quantity,pnl=row.pnl,fees=row.fees,reason=row.reason,signal_score=row.signal_score,regime=row.regime,risk_amount=row.risk_amount,slippage_cost=row.slippage_cost,context=json.loads(row.context_json),funding=row.funding,spread_cost=row.spread_cost) for row in trade_rows]
            points=[EquityPoint(_utc(row.timestamp),row.balance,row.equity,row.drawdown,row.realized_pnl,row.unrealized_pnl) for row in point_rows]
            return BacktestResult(run.id,run.starting_balance,run.final_equity,trades,[(point.timestamp,point.equity) for point in points],{row.name:_parse_metric(row.value) for row in metric_rows},points)

    def register_strategy(self, version: str, config: dict) -> None:
        serialized=json.dumps(config,sort_keys=True,separators=(",",":"),default=str)
        digest=sha256(serialized.encode()).hexdigest()
        with self.session_factory.begin() as session:
            existing=session.get(StrategyVersionRecord,version)
            if existing and existing.config_hash != digest: raise ValueError("Strategy version is immutable")
            if not existing: session.add(StrategyVersionRecord(version=version,config_json=serialized,config_hash=digest))

    def save_experiment(self, version: str, symbol: str, split: str, costs: str, metrics: dict, selected: bool = False) -> str:
        identifier=uuid4().hex
        with self.session_factory.begin() as session:
            session.add(StrategyExperimentRecord(id=identifier,strategy_version=version,symbol=symbol,data_split=split,cost_scenario=costs,metrics_json=json.dumps(metrics,default=str,sort_keys=True),selected=int(selected)))
        return identifier


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _floor_time(value: datetime, seconds: int) -> datetime:
    timestamp=int(value.timestamp()); return datetime.fromtimestamp(timestamp-timestamp%seconds,UTC)


def _parse_metric(value: str):
    if value in {"Infinity","-Infinity"}: return Decimal(value)
    try: return Decimal(value)
    except Exception: return value
