from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.backtest.core import Candle, StrategyAction
from app.db import Base
from app.domain.models import Side
from app.shadow.engine import PROTOCOL_ID, ProspectiveShadowEngine
from app.shadow.market import PublicLiveMarketData
from app.shadow.models import InstrumentConstraints, LiveMarketSnapshot
from app.shadow.report import build_report, statistical_validation
from app.shadow.repository import ShadowRepository, shadow_metrics
from app.strategy_lab.phase4g import FROZEN_CONFIG_HASH


class Notifier:
    def __init__(self) -> None:
        self.opens = []
        self.closes = []

    async def opened(self, trade) -> None:
        self.opens.append(trade)

    async def closed(self, trade, values) -> None:
        self.closes.append((trade, values))


class Market:
    def __init__(self, candles=None) -> None:
        self.candles = candles or []

    async def closed_candles(self, exchange, symbol, start, end):
        return [candle for candle in self.candles if start <= candle.timestamp < end]


def _repository() -> ShadowRepository:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return ShadowRepository(sessionmaker(bind=engine, expire_on_commit=False))


def _history(end: datetime) -> list[Candle]:
    start = end - timedelta(hours=300)
    return [
        Candle(
            start + timedelta(hours=index),
            Decimal("100"),
            Decimal("101"),
            Decimal("99"),
            Decimal("100"),
            Decimal("1000"),
        )
        for index in range(300)
    ]


def _snapshot(timestamp: datetime, bid="100", ask="100.1") -> LiveMarketSnapshot:
    return LiveMarketSnapshot(
        "binance",
        "BTC/USDT",
        Decimal(bid),
        Decimal(ask),
        (Decimal(bid) + Decimal(ask)) / 2,
        ((Decimal(bid), Decimal("10")),),
        ((Decimal(ask), Decimal("10")),),
        timestamp,
        timestamp,
        InstrumentConstraints(
            Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")
        ),
    )


def _engine(locked_at: datetime, repository=None, market=None):
    repository = repository or _repository()
    notifier = Notifier()
    return ProspectiveShadowEngine(
        {
            "strategy_config_hash": FROZEN_CONFIG_HASH,
            "locked_at": locked_at.isoformat(),
            "exchanges": ["binance"],
            "assets": ["BTC/USDT"],
        },
        repository,
        market or Market(),
        {("binance", "BTC/USDT"): _history(locked_at.replace(minute=0, second=0, microsecond=0))},
        notifier,
    ), repository, notifier


def _decision(decision_id: str, timestamp: datetime) -> dict:
    return {
        "id": decision_id,
        "protocol_id": PROTOCOL_ID,
        "exchange": "binance",
        "symbol": "BTC/USDT",
        "candle_open_time": timestamp,
        "signal_timestamp": timestamp + timedelta(hours=1),
        "decision": "LONG",
        "signal_score": 90,
        "decision_price": Decimal("100"),
        "observed_bid": Decimal("100"),
        "observed_ask": Decimal("100.1"),
        "observed_spread": Decimal("0.1"),
        "risk_status": "ALLOW",
        "risk_reason": "Approved",
        "strategy_hash": FROZEN_CONFIG_HASH,
        "context_json": "{}",
    }


def test_public_market_gateway_exposes_no_order_method() -> None:
    assert not hasattr(PublicLiveMarketData, "create_order")
    assert not hasattr(PublicLiveMarketData, "cancel_order")


def test_engine_rejects_changed_strategy_hash() -> None:
    with pytest.raises(RuntimeError, match="hash mismatch"):
        ProspectiveShadowEngine(
            {"strategy_config_hash": "changed", "locked_at": datetime.now(UTC).isoformat()},
            _repository(),
            Market(),
            {},
            Notifier(),
        )


@pytest.mark.asyncio
async def test_pre_lock_closed_candle_is_never_saved_or_decided() -> None:
    locked = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    candle = Candle(locked.replace(hour=23, day=31, month=12, year=2025, minute=0), Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1"))
    engine, repository, _ = _engine(locked, market=Market([candle]))
    await engine._process_candles(_snapshot(locked.replace(hour=1, minute=5)))
    assert repository.first_candle(PROTOCOL_ID) is None
    assert repository.decisions_count(PROTOCOL_ID) == 0


@pytest.mark.asyncio
async def test_first_post_lock_closed_candle_is_saved_and_wait_recorded() -> None:
    locked = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    candle = Candle(locked.replace(minute=0), Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1"))
    engine, repository, _ = _engine(locked, market=Market([candle]))
    await engine._process_candles(_snapshot(locked.replace(hour=1, minute=5)))
    assert repository.first_candle(PROTOCOL_ID).candle_close_time.replace(tzinfo=UTC) == datetime(2026, 1, 1, 1, tzinfo=UTC)
    assert repository.decisions_count(PROTOCOL_ID) == 1
    assert repository.decisions_count(PROTOCOL_ID, signals_only=True) == 0


def test_shadow_order_uses_observed_ask_slippage_risk_and_precision() -> None:
    locked = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    engine, _, _ = _engine(locked)
    action = StrategyAction(Side.LONG, Decimal("98"), Decimal("106"), 90, volatility_pct=Decimal("0.01"))
    trade = engine._shadow_order(_snapshot(locked), action, "decision", Candle(locked, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1")))
    assert trade is not None
    assert trade["entry_price"] > Decimal("100.1")
    assert trade["quantity"] % Decimal("0.001") == 0
    assert trade["entry_fee"] > 0
    assert trade["entry_spread_cost"] > 0
    assert trade["strategy_hash"] == FROZEN_CONFIG_HASH


def test_exchange_minimum_can_reject_shadow_order() -> None:
    locked = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    engine, _, _ = _engine(locked)
    snapshot = _snapshot(locked)
    snapshot = LiveMarketSnapshot(*snapshot.__dict__.values())
    object.__setattr__(snapshot, "constraints", InstrumentConstraints(Decimal("1"), Decimal("10"), Decimal("10000"), Decimal("0.01")))
    action = StrategyAction(Side.LONG, Decimal("98"), Decimal("106"), 90, volatility_pct=Decimal("0.01"))
    assert engine._shadow_order(snapshot, action, "decision", Candle(locked, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1"))) is None
    assert "minimum" in engine._last_risk_reason.lower()


@pytest.mark.asyncio
async def test_quote_trigger_closes_shadow_trade_with_explicit_costs() -> None:
    locked = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    engine, repository, notifier = _engine(locked)
    action = StrategyAction(Side.LONG, Decimal("98"), Decimal("106"), 90, volatility_pct=Decimal("0.01"))
    trade = engine._shadow_order(_snapshot(locked), action, "decision", Candle(locked, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1")))
    repository.record_decision(_decision("decision", locked), trade)
    await engine._track_position(_snapshot(locked + timedelta(hours=2), "106.1", "106.2"))
    closed = repository.closed_trades(PROTOCOL_ID)
    assert len(closed) == 1
    assert closed[0].exit_reason == "TAKE_PROFIT"
    assert closed[0].exit_fee > 0
    assert closed[0].realized_pnl < closed[0].gross_pnl
    assert notifier.closes


def test_shadow_metrics_never_hide_costs() -> None:
    locked = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    engine, repository, _ = _engine(locked)
    action = StrategyAction(Side.LONG, Decimal("98"), Decimal("106"), 90, volatility_pct=Decimal("0.01"))
    trade = engine._shadow_order(_snapshot(locked), action, "decision", Candle(locked, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1")))
    repository.record_decision(_decision("decision", locked), trade)
    repository.close_trade(trade["id"], {"status": "CLOSED", "exit_reference": Decimal("106"), "exit_price": Decimal("105.9"), "exit_reason": "TP", "exit_fee": Decimal("0.1"), "exit_spread_cost": Decimal("0.05"), "exit_slippage_cost": Decimal("0.05"), "gross_pnl": Decimal("5"), "realized_pnl": Decimal("4"), "closed_at": locked + timedelta(hours=2)})
    metrics = shadow_metrics(repository.closed_trades(PROTOCOL_ID))
    assert metrics["gross_pnl"] == Decimal("5")
    assert metrics["fees"] > Decimal("0.1")
    assert metrics["spread_cost"] > Decimal("0.05")
    assert metrics["slippage"] > Decimal("0.05")
    assert metrics["net_pnl"] == Decimal("4")


def test_empty_prospective_sample_is_not_reported_as_an_edge() -> None:
    assert statistical_validation([])["adequacy"] == "INSUFFICIENT SAMPLE"

    protocol = {
        "strategy_config_hash": FROZEN_CONFIG_HASH,
        "strategy_version": "phase4g_volatility_expansion_1h_frozen_v1",
        "exchanges": ["binance"],
        "assets": ["BTC/USDT"],
    }

    class EmptyRepository:
        def protocol(self, protocol_id):
            return SimpleNamespace(
                locked_at=datetime.now(UTC),
                protocol_json=json.dumps(protocol),
                protocol_hash="protocol-hash",
            )

        def closed_trades(self, protocol_id):
            return []

        def prospective_candles(self, protocol_id, exchange, symbol):
            return []

    report = build_report(EmptyRepository(), preview=True)
    assert report["without_best_asset"]["removed"] is None
    assert report["without_best_asset"]["diagnosis"] == "INSUFFICIENT SAMPLE"
    assert report["without_best_exchange"]["removed"] is None
    assert report["without_best_exchange"]["diagnosis"] == "INSUFFICIENT SAMPLE"
