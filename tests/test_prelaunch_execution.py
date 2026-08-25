from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, ExecutionOrderRecord
from app.domain.models import RiskProfile, Side, TradeIntent
from app.exchanges.adapters import BybitAdapter
from app.exchanges.base import InvalidOrderError
from app.exchanges.health import ExchangeHealthMonitor
from app.exchanges.market_data import MarketDataRouter
from app.exchanges.models import (
    ExchangeBalance,
    ExchangeOrder,
    InstrumentRules,
    MarketType,
    OrderRequest,
    OrderSide,
    OrderType,
    Ticker,
)
from app.exchanges.portfolio import PortfolioManager
from app.trading.execution_store import OrderOutcomeUnknown, SqlExecutionOrderStore
from app.trading.multi_exchange import ExecutionRiskContext, MultiExchangeTradingEngine
from app.trading.paper_broker import PaperBroker


def _intent(side: Side, identifier: str) -> TradeIntent:
    return TradeIntent(
        trade_id=identifier,
        user_id=1,
        symbol="BTC/USDT",
        side=side,
        entry=Decimal("100"),
        stop_loss=Decimal("95") if side is Side.LONG else Decimal("105"),
        take_profit=Decimal("110") if side is Side.LONG else Decimal("90"),
        equity=Decimal("10000"),
        daily_realized_pnl=Decimal(),
        open_positions=0,
        consecutive_losses=0,
    )


def test_deterministic_paper_long_accounting() -> None:
    broker = PaperBroker(fee_rate=Decimal("0.001"), slippage_rate=Decimal("0.01"))
    position = broker.open_market(_intent(Side.LONG, "known-long"), RiskProfile())
    closed = broker.close_market(position.trade_id, Decimal("110"), "CONTROL")

    assert position.quantity == Decimal("5.000000")
    assert position.entry_price == Decimal("101.00")
    assert position.entry_fee == Decimal("0.50")
    assert closed.exit_price == Decimal("108.90")
    assert closed.exit_fee == Decimal("0.54")
    assert closed.realized_pnl == Decimal("38.46")
    assert broker.account().balance == Decimal("10038.46")


def test_deterministic_paper_short_accounting() -> None:
    broker = PaperBroker(fee_rate=Decimal("0.001"), slippage_rate=Decimal("0.01"))
    position = broker.open_market(_intent(Side.SHORT, "known-short"), RiskProfile())
    closed = broker.close_market(position.trade_id, Decimal("90"), "CONTROL")

    assert position.quantity == Decimal("5.000000")
    assert position.entry_price == Decimal("99.00")
    assert position.entry_fee == Decimal("0.50")
    assert closed.exit_price == Decimal("90.90")
    assert closed.exit_fee == Decimal("0.45")
    assert closed.realized_pnl == Decimal("39.55")
    assert broker.account().balance == Decimal("10039.55")


class SafetyTransport:
    sandbox = True
    websocket_connected = True

    def __init__(self, *, timeout: bool = False, ticker_age_seconds: int = 0) -> None:
        self.timeout = timeout
        self.ticker_age_seconds = ticker_age_seconds
        self.create_calls = 0

    async def connect(self) -> None:
        return None

    async def call(self, operation: str, **parameters):
        now = datetime.now(UTC)
        if operation == "get_server_time":
            return now
        if operation == "get_ticker":
            return Ticker(
                "bybit",
                "BTC/USDT",
                Decimal("99.9"),
                Decimal("100.1"),
                Decimal("100"),
                now - timedelta(seconds=self.ticker_age_seconds),
            )
        if operation == "get_instrument_rules":
            return InstrumentRules(
                Decimal("0.1"),
                Decimal("0.001"),
                Decimal("0.001"),
                Decimal("5"),
                Decimal("100"),
                Decimal("2"),
            )
        if operation == "create_order":
            self.create_calls += 1
            if self.timeout:
                raise TimeoutError("deterministic timeout")
            request = parameters["request"]
            return ExchangeOrder(
                "bybit",
                request.account_id,
                "venue-1",
                request.symbol,
                "FILLED",
                request.quantity,
                request.quantity,
                Decimal("100"),
                request.client_order_id,
            )
        if operation == "get_balance":
            return ExchangeBalance(Decimal("1000"), Decimal("1000"))
        if operation in {"get_positions", "get_open_orders", "get_trade_history"}:
            return []
        return None


def _database_store(tmp_path):
    database = create_engine(f"sqlite:///{tmp_path / 'execution.db'}")
    Base.metadata.create_all(database)
    sessions = sessionmaker(bind=database, expire_on_commit=False)
    return database, sessions, SqlExecutionOrderStore(sessions)


async def _engine(transport: SafetyTransport, store: SqlExecutionOrderStore):
    router = MarketDataRouter()
    adapter = BybitAdapter(transport)
    router.register("account-a", adapter)
    market = await router.get_context("bybit", "account-a", "BTC/USDT")
    engine = MultiExchangeTradingEngine(
        router,
        ExchangeHealthMonitor(),
        PortfolioManager(),
        order_store=store,
        require_persistent_store=True,
    )
    return engine, market


def _request(identifier: str = "signal-closed-candle-1", **overrides) -> OrderRequest:
    values = {
        "account_id": "account-a",
        "symbol": "BTC/USDT",
        "market_type": MarketType.PERPETUAL,
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "quantity": Decimal("0.1"),
        "leverage": Decimal("1"),
        "client_order_id": identifier,
    }
    values.update(overrides)
    return OrderRequest(**values)


def _risk(**overrides) -> ExecutionRiskContext:
    values = {
        "equity_by_account": {("bybit", "account-a"): Decimal("1000")},
        "daily_pnl_by_account": {},
        "available_by_account": {("bybit", "account-a"): Decimal("900")},
        "allowed_symbols": frozenset({"BTC/USDT"}),
    }
    values.update(overrides)
    return ExecutionRiskContext(**values)


@pytest.mark.asyncio
async def test_persistent_client_id_blocks_duplicate_after_process_restart(tmp_path) -> None:
    _, sessions, store = _database_store(tmp_path)
    transport = SafetyTransport()
    first, market = await _engine(transport, store)
    request = _request()
    assert (await first.execute(market, request, Decimal("95"), _risk())).status == "FILLED"

    restarted, restarted_market = await _engine(transport, SqlExecutionOrderStore(sessions))
    with pytest.raises(PermissionError, match="Duplicate"):
        await restarted.execute(restarted_market, request, Decimal("95"), _risk())
    assert transport.create_calls == 1


@pytest.mark.asyncio
async def test_timeout_is_unknown_and_never_automatically_resent(tmp_path) -> None:
    database, sessions, store = _database_store(tmp_path)
    transport = SafetyTransport(timeout=True)
    engine, market = await _engine(transport, store)
    request = _request("timeout-order")

    with pytest.raises(OrderOutcomeUnknown, match="automatic retry"):
        await engine.execute(market, request, Decimal("95"), _risk())
    with pytest.raises(OrderOutcomeUnknown, match="reconcile"):
        await engine.execute(market, request, Decimal("95"), _risk())
    assert transport.create_calls == 1
    with sessionmaker(bind=database)() as session:
        assert session.query(ExecutionOrderRecord).one().status == "UNKNOWN"


@pytest.mark.asyncio
async def test_stale_data_kill_switch_and_loss_cooldown_block_before_order(tmp_path) -> None:
    _, _, store = _database_store(tmp_path)
    stale_transport = SafetyTransport(ticker_age_seconds=31)
    engine, stale_market = await _engine(stale_transport, store)
    with pytest.raises(PermissionError, match="STALE DATA"):
        await engine.execute(stale_market, _request("stale"), Decimal("95"), _risk())

    transport = SafetyTransport()
    engine, market = await _engine(transport, store)
    with pytest.raises(PermissionError, match="kill switch"):
        await engine.execute(
            market, _request("killed"), Decimal("95"), _risk(kill_switch_active=True)
        )
    with pytest.raises(PermissionError, match="Consecutive-loss"):
        await engine.execute(
            market,
            _request("losses"),
            Decimal("95"),
            _risk(consecutive_losses_by_account={("bybit", "account-a"): 3}),
        )
    assert transport.create_calls == 0


@pytest.mark.asyncio
async def test_hard_notional_risk_margin_and_symbol_limits(tmp_path) -> None:
    _, _, store = _database_store(tmp_path)
    transport = SafetyTransport()
    engine, market = await _engine(transport, store)
    cases = [
        (_request("notional", quantity=Decimal("6")), Decimal("99"), _risk(), "notional"),
        (_request("risk", quantity=Decimal("2")), Decimal("95"), _risk(), "per-trade"),
        (
            _request("margin", quantity=Decimal("2")),
            Decimal("99"),
            _risk(available_by_account={("bybit", "account-a"): Decimal("100")}),
            "margin",
        ),
        (
            _request("symbol", symbol="ETH/USDT"),
            Decimal("95"),
            _risk(),
            "allowlist",
        ),
    ]
    for request, stop, risk, message in cases:
        altered_market = market
        if request.symbol != market.symbol:
            altered_market = type(market)(
                market.exchange,
                market.account_id,
                request.symbol,
                market.market_type,
                market.ticker,
                market.orderbook,
                market.funding_rate,
                market.open_interest,
            )
        with pytest.raises(PermissionError, match=message):
            await engine.execute(altered_market, request, stop, risk)
    assert transport.create_calls == 0


def test_instrument_filters_reject_invalid_leverage_and_round_safely() -> None:
    rules = InstrumentRules(
        Decimal("0.1"),
        Decimal("0.001"),
        Decimal("0.01"),
        Decimal("5"),
        Decimal("2"),
        Decimal("2"),
    )
    with pytest.raises(InvalidOrderError, match="positive"):
        BybitAdapter.validate_order(_request(leverage=Decimal("0")), rules)
    normalized = BybitAdapter.validate_order(
        _request(
            order_type=OrderType.LIMIT,
            quantity=Decimal("1.2349"),
            price=Decimal("100.06"),
        ),
        rules,
    )
    assert normalized.quantity == Decimal("1.234")
    assert normalized.price == Decimal("100.1")
