import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.market_discovery import (
    BybitAllMarketReader,
    HybridMarketDiscoveryService,
    MarketCandidate,
    MarketDiscoveryRepository,
    MarketDiscoverySnapshot,
    _eligible_market,
    build_market_prompt,
)
from app.ai.live_trader import AILiveRepository, format_ai_live_status_ru
from app.core.config import Settings
from app.db import AIMarketDiscoveryRuntimeRecord, Base
from app.exchanges.bybit_readonly import ReadResult


D = Decimal
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def instrument(symbol: str, *, status: str = "Trading") -> dict:
    return {
        "symbol": symbol,
        "status": status,
        "contractType": "LinearPerpetual",
        "settleCoin": "USDT",
        "lotSizeFilter": {
            "minOrderQty": "0.1",
            "qtyStep": "0.1",
            "minNotionalValue": "5",
        },
    }


def ticker(symbol: str, *, turnover: str = "100000000", spread: str = "0.01") -> dict:
    bid = D("100")
    return {
        "symbol": symbol,
        "bid1Price": str(bid),
        "ask1Price": str(bid + D(spread)),
        "turnover24h": turnover,
        "volume24h": "1000000",
    }


class FakeBybitClient:
    def __init__(self, fail_symbol: str | None = None) -> None:
        self.instrument_pages = 0
        self.closed = False
        self.fail_symbol = fail_symbol

    async def synchronize_time(self):
        return ReadResult({}, D("1"))

    async def public_get(self, path, params):
        if path == "/v5/market/instruments-info":
            self.instrument_pages += 1
            if not params.get("cursor"):
                return ReadResult(
                    {
                        "list": [instrument("BTCUSDT"), instrument("ATOMUSDT")],
                        "nextPageCursor": "page-2",
                    },
                    D("1"),
                )
            return ReadResult(
                {
                    "list": [
                        instrument("OPUSDT"),
                        instrument("ARBUSDT"),
                        instrument("BADUSDT", status="Settled"),
                    ],
                    "nextPageCursor": "",
                },
                D("1"),
            )
        if path == "/v5/market/tickers":
            return ReadResult(
                {
                    "list": [
                        ticker("BTCUSDT"),
                        ticker("ATOMUSDT", turnover="80000000"),
                        ticker("OPUSDT", turnover="70000000"),
                        ticker("ARBUSDT", turnover="60000000"),
                        ticker("BADUSDT"),
                    ]
                },
                D("1"),
            )
        if path == "/v5/market/kline":
            if params["symbol"] == self.fail_symbol:
                raise RuntimeError("fixture market is temporarily unavailable")
            minutes = {"5": 5, "15": 15, "60": 60}[str(params["interval"])]
            rows = []
            for index in range(80):
                opened = NOW - timedelta(minutes=minutes * (index + 1))
                close = D("100") + D(80 - index) / D("100")
                rows.append(
                    [
                        str(int(opened.timestamp() * 1000)),
                        str(close - D("0.1")),
                        str(close + D("0.2")),
                        str(close - D("0.2")),
                        str(close),
                        str(D("1000") + index),
                    ]
                )
            return ReadResult({"list": rows}, D("1"))
        raise AssertionError(path)

    async def close(self):
        self.closed = True


def candidate(symbol: str, score: str = "50") -> MarketCandidate:
    frame = {
        "last_closed_at": NOW.isoformat(),
        "trend": "BULLISH",
        "indicators": {
            "rsi_14": "60",
            "ema_9": "101",
            "ema_21": "100",
            "ema_50": "99",
            "macd": "1",
            "macd_signal": "0.5",
            "atr_14": "1",
            "bollinger_upper": "102",
            "bollinger_lower": "98",
            "realized_volatility_20": "0.01",
            "relative_volume_20": "1.5",
            "price_moves": {"12": "0.02"},
        },
        "candles": [[NOW.isoformat(), "1", "1", "1", "1", "1"]],
    }
    return MarketCandidate(
        symbol,
        D("100"),
        D("100.01"),
        D("0.0001"),
        D("100000000"),
        D("1000000"),
        D("5"),
        D(score),
        {"5m": frame, "15m": frame, "1h": frame},
    )


def snapshot(now: datetime = NOW, scores=("50", "50", "50")) -> MarketDiscoverySnapshot:
    return MarketDiscoverySnapshot(
        500,
        100,
        tuple(candidate(symbol, score) for symbol, score in zip(
            ("ATOMUSDT", "OPUSDT", "ARBUSDT"), scores, strict=True
        )),
        now,
    )


def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hybrid.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_all_market_filters_invalid_liquidity_spread_and_minimum_order() -> None:
    base = instrument("ATOMUSDT")
    assert _eligible_market(base, ticker("ATOMUSDT")) is not None
    assert _eligible_market(base, ticker("ATOMUSDT", turnover="1")) is None
    assert _eligible_market(base, ticker("ATOMUSDT", spread="1")) is None
    too_large = instrument("ATOMUSDT")
    too_large["lotSizeFilter"]["minNotionalValue"] = "20"
    assert _eligible_market(too_large, ticker("ATOMUSDT")) is None


@pytest.mark.asyncio
async def test_all_market_scan_is_paginated_local_and_excludes_core() -> None:
    client = FakeBybitClient()
    result = await BybitAllMarketReader(client).scan(NOW)
    assert client.instrument_pages == 2
    assert result.scanned_symbols == 5
    assert result.eligible_symbols == 4
    assert len(result.candidates) == 3
    assert {item.symbol for item in result.candidates} == {
        "BTCUSDT", "ATOMUSDT", "OPUSDT"
    }
    assert all(
        len(frame["candles"]) <= 5
        for item in result.candidates
        for frame in item.timeframes.values()
    )


@pytest.mark.asyncio
async def test_one_unavailable_market_does_not_stop_the_market_scan() -> None:
    result = await BybitAllMarketReader(FakeBybitClient("ARBUSDT")).scan(NOW)
    assert result.scanned_symbols == 5
    assert len(result.candidates) == 3
    assert {item.symbol for item in result.candidates} == {
        "BTCUSDT", "ATOMUSDT", "OPUSDT"
    }


def test_restart_safe_thirty_minute_gate_and_strong_event(tmp_path) -> None:
    factory = sessions(tmp_path)
    first = MarketDiscoveryRepository(factory)
    first.initialize()
    assert first.claim_local_slot(NOW)
    assert not first.claim_local_slot(NOW)
    first.save_local(snapshot())
    assert first.claim_hermes(snapshot())
    first.record_hermes_http_call(NOW)

    restarted = MarketDiscoveryRepository(factory)
    restarted.initialize()
    assert not restarted.claim_hermes(snapshot(NOW + timedelta(minutes=10)))
    assert restarted.claim_hermes(snapshot(NOW + timedelta(minutes=31)))

    different = MarketDiscoverySnapshot(
        500,
        100,
        (
            candidate("INJUSDT", "90"),
            candidate("TIAUSDT", "90"),
            candidate("APTUSDT", "90"),
        ),
        NOW + timedelta(minutes=35),
    )
    assert restarted.claim_hermes(different)
    with factory() as session:
        row = session.get(AIMarketDiscoveryRuntimeRecord, "HYBRID_MARKET_DISCOVERY")
        assert row.hermes_calls_today == 1


class FakeProvider:
    def __init__(self) -> None:
        self.request_observer = None
        self.calls = 0

    async def complete_json(self, _prompt, _schema, *, expected_symbols):
        self.calls += 1
        if self.request_observer:
            self.request_observer(NOW)
        return {
            "decisions": [
                {
                    "symbol": symbol,
                    "action": "WAIT",
                    "confidence": 50,
                    "stop_loss": None,
                    "take_profit": None,
                    "reason": "No diagnostic setup",
                }
                for symbol in sorted(expected_symbols)
            ]
        }


class FakeReader:
    def __init__(self, value: MarketDiscoverySnapshot) -> None:
        self.value = value
        self.calls = 0

    async def scan(self, _now):
        self.calls += 1
        return self.value

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_service_uses_one_batched_diagnostic_call_and_never_executes(tmp_path) -> None:
    factory = sessions(tmp_path)
    repository = MarketDiscoveryRepository(factory)
    repository.initialize()
    provider = FakeProvider()
    reader = FakeReader(snapshot())
    service = HybridMarketDiscoveryService(repository, reader, provider)
    result = await service.cycle(NOW)
    assert result["status"] == "HERMES_COMPLETED"
    assert provider.calls == 1
    assert reader.calls == 1
    assert json.loads(build_market_prompt(snapshot()))["execution_allowed"] is False

    repeated = await service.cycle(NOW + timedelta(minutes=5))
    assert repeated["status"] == "LOCAL_ONLY"
    assert provider.calls == 1
    with factory() as session:
        row = session.get(AIMarketDiscoveryRuntimeRecord, "HYBRID_MARKET_DISCOVERY")
        assert row.hermes_calls_today == 1
        assert len(json.loads(row.last_decisions_json)["decisions"]) == 3


def test_telegram_status_reports_separate_core_and_market_counters(tmp_path) -> None:
    factory = sessions(tmp_path)
    now = datetime.now(UTC)
    core = AILiveRepository(factory)
    core.initialize(Settings())
    core.record_hermes_call()
    core.record_hermes_call()

    market = MarketDiscoveryRepository(factory)
    market.initialize()
    market.save_local(snapshot(now))
    market.record_hermes_http_call(now)

    rendered = format_ai_live_status_ru(core.status())
    assert "CORE AI SCAN: 8 / 5m" in rendered
    assert "MARKET LOCAL SCAN: 500 symbols / 5m" in rendered
    assert "MARKET TOP: 3" in rendered
    assert "CORE HERMES CALLS TODAY: 2" in rendered
    assert "MARKET HERMES CALLS TODAY: 1" in rendered
