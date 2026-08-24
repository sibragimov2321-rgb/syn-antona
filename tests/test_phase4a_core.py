from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.backtest.core import (
    BacktestEngine,
    Candle,
    StrategyAction,
    monte_carlo,
    overfitting_warning,
    signal_score_calibration,
    validation_status,
)
from app.backtest.orchestrator import HistoricalBacktestOrchestrator, LOW_RISK, MultiTimeframeAligner
from app.backtest.orchestrator import _split_boundaries
from app.backtest.persistence import (
    BacktestRepository,
    CachedHistoricalDataProvider,
    HistoricalCandleCache,
)
from app.db import Base
from app.domain.models import RiskProfile, Side


def _candles(start: datetime, count: int, minutes: int, first_price: int = 100) -> list[Candle]:
    output = []
    for index in range(count):
        price = Decimal(first_price + index)
        output.append(Candle(start + timedelta(minutes=minutes * index), price, price + 1, price - 1, price, Decimal("10")))
    return output


def test_multi_timeframe_alignment_excludes_unclosed_candle() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    one_hour = _candles(start, 2, 60)
    aligner = MultiTimeframeAligner({"1h": one_hour})
    assert aligner.closed_history("1h", start + timedelta(minutes=59)) == []
    assert aligner.closed_history("1h", start + timedelta(hours=1)) == [one_hour[0]]


def test_warmup_returns_no_frames_before_ema200_history() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    histories = {
        "5m": _candles(start, 199, 5),
        "15m": _candles(start, 199, 15),
        "1h": _candles(start, 199, 60),
        "4h": _candles(start, 199, 240),
    }
    aligner = MultiTimeframeAligner(histories)
    decision = histories["4h"][-1].timestamp + timedelta(hours=4)
    assert aligner.frames(decision, histories["5m"]) is None


def test_full_historical_pipeline_generates_risk_managed_trade() -> None:
    anchor = datetime(2025, 2, 15, tzinfo=UTC)
    histories = {
        "4h": _candles(anchor - timedelta(hours=4 * 210), 210, 240),
        "1h": _candles(anchor - timedelta(hours=210), 210, 60),
        "15m": _candles(anchor - timedelta(minutes=15 * 210), 210, 15),
        "5m": _candles(anchor - timedelta(minutes=5 * 200), 210, 5),
    }
    base = histories["5m"][-10:]
    result, _ = HistoricalBacktestOrchestrator({})._execute_window(
        histories, base, base[0].timestamp, LOW_RISK, Decimal("1000")
    )
    assert result.trades
    assert result.trades[0].signal_score >= 70
    assert result.metrics["total_fees"] > 0
    assert result.metrics["slippage_cost"] > 0


def test_metrics_calibration_monte_carlo_and_validation_gate() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    candles = _candles(start, 5, 5)
    used = False
    def strategy(history):
        nonlocal used
        if used: return None
        used = True
        return StrategyAction(Side.LONG, history[-1].close - 1, history[-1].close + 2, 75, "BULL")
    result = BacktestEngine(slippage=Decimal()).run(candles, strategy)
    calibration = signal_score_calibration(result.trades)
    assert calibration["70-79"]["trades"] == 1
    simulation = monte_carlo(result.trades, Decimal("1000"), 1000)
    assert simulation["simulations"] == 1000
    assert {"expected_max_drawdown", "probability_dd_10", "probability_dd_20"} <= simulation.keys()
    assert validation_status(result) == "NEEDS_MORE_DATA"
    assert not overfitting_warning(result, result)


def test_short_execution_uses_current_equity_and_adverse_slippage() -> None:
    started = datetime(2025, 1, 1, tzinfo=UTC)
    candles = [Candle(started+timedelta(minutes=5*i),Decimal("100"),Decimal("101"),Decimal("99"),Decimal("100"),Decimal("10")) for i in range(6)]
    calls = 0
    def strategy(history):
        nonlocal calls
        calls += 1
        if calls in {1, 3}:
            price = history[-1].close
            return StrategyAction(Side.SHORT, price + 1, price - 2, 80, "BEAR")
        return None
    result = BacktestEngine(slippage=Decimal("0.001")).run(
        candles, strategy, risk_profile=RiskProfile(cooldown_minutes=0)
    )
    assert result.trades
    assert all(trade.side is Side.SHORT for trade in result.trades)
    assert result.trades[0].entry < candles[0].close
    assert len(result.trades) == 2
    assert result.trades[1].quantity < result.trades[0].quantity
    assert len({trade.entry_time for trade in result.trades}) == len(result.trades)
    assert result.metrics["total_fees"] > 0
    assert result.metrics["slippage_cost"] > 0


def test_walk_forward_boundaries_are_disjoint() -> None:
    values = _candles(datetime(2025, 1, 1, tzinfo=UTC), 100, 5)
    split = _split_boundaries(values)
    assert len(split["train"]) == 60
    assert len(split["validation"]) == 20
    assert len(split["out_of_sample"]) == 20
    assert split["train"][-1].timestamp < split["validation"][0].timestamp
    assert split["validation"][-1].timestamp < split["out_of_sample"][0].timestamp


def test_take_profit_execution_is_not_replaced_by_stop() -> None:
    started = datetime(2025, 1, 1, tzinfo=UTC)
    candles = [
        Candle(started,Decimal("100"),Decimal("101"),Decimal("100"),Decimal("100"),Decimal("10")),
        Candle(started+timedelta(minutes=5),Decimal("100"),Decimal("103"),Decimal("100"),Decimal("102"),Decimal("10")),
    ]
    used = False
    def strategy(_):
        nonlocal used
        if used: return None
        used = True
        return StrategyAction(Side.LONG,Decimal("99"),Decimal("102"),80,"BULL")
    result = BacktestEngine(slippage=Decimal()).run(candles,strategy)
    assert result.trades[0].reason == "TAKE_PROFIT"
    assert result.trades[0].pnl > 0


@pytest.mark.asyncio
async def test_cached_provider_downloads_only_missing_range(tmp_path) -> None:
    database = tmp_path / "cache.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine,expire_on_commit=False)
    started = datetime(2025,1,1,tzinfo=UTC)
    existing = _candles(started,2,5)
    cache = HistoricalCandleCache(sessions)
    cache.save("fake","BTC/USDT","5m",existing)
    class FakeProvider:
        name = "fake"
        calls = []
        async def fetch(self,symbol,timeframe,start,end):
            self.calls.append((start,end))
            return _candles(start,1,5,102)
    raw = FakeProvider()
    provider = CachedHistoricalDataProvider(raw,cache)
    loaded = await provider.fetch("BTC/USDT","5m",started,started+timedelta(minutes=15))
    assert len(loaded) == 3
    assert raw.calls == [(started+timedelta(minutes=10),started+timedelta(minutes=15))]
    await provider.fetch("BTC/USDT","5m",started,started+timedelta(minutes=15))
    assert len(raw.calls) == 1


def test_cache_and_saved_backtest_survive_new_repository(tmp_path) -> None:
    database = tmp_path / "phase4a.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    cache = HistoricalCandleCache(sessions)
    candles = _candles(datetime(2025, 1, 1, tzinfo=UTC), 3, 5)
    cache.save("bybit", "BTC/USDT", "5m", candles)
    assert len(HistoricalCandleCache(sessions).load("bybit", "BTC/USDT", "5m", candles[0].timestamp, candles[-1].timestamp + timedelta(minutes=5))) == 3
    result = BacktestEngine(slippage=Decimal()).run(candles, lambda _: None)
    simulation = monte_carlo(result.trades, Decimal("1000"), 1000)
    BacktestRepository(sessions).save(result, "bybit", "BTC/USDT", "LOW", "NEEDS_MORE_DATA", {}, simulation)
    loaded = BacktestRepository(sessions).load_result(result.run_id)
    assert loaded and loaded.final_equity == Decimal("1000")
    assert len(loaded.equity_points) == len(candles)
