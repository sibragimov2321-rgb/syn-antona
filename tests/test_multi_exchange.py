from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.backtest.core import Candle, StrategyAction
from app.backtest.exchange_comparison import ExchangeBacktestProfile, ExchangeComparisonBacktest
from app.db import Base, ExchangeAccountRecord
from app.domain.models import Side
from app.exchanges.accounts import ExchangeAccountManager, ExchangeCredentials, UnsafeExchangePermissions
from app.exchanges.adapters import ADAPTER_TYPES, BybitAdapter, create_adapter
from app.exchanges.base import InvalidOrderError, LiveTradingDisabledError
from app.exchanges.ccxt_transport import CcxtTransport
from app.exchanges.health import ExchangeHealthMonitor
from app.exchanges.market_data import ExchangeSelector, MarketDataRouter
from app.exchanges.models import (
    ExchangeBalance,
    ExchangeOrder,
    FeeSchedule,
    HealthReport,
    HealthStatus,
    InstrumentRules,
    MarketType,
    OrderBook,
    OrderRequest,
    OrderSide,
    OrderType,
    SelectionMode,
    Ticker,
    TradingPermissions,
    VenueQuote,
)
from app.exchanges.portfolio import ManagedPosition, PortfolioManager, build_dashboard
from app.risk.portfolio import ExchangeRiskLimits, GlobalPortfolioRiskManager, GlobalRiskLimits, ProposedExposure
from app.trading.multi_exchange import ExecutionRiskContext, MultiExchangeTradingEngine


class FakeTransport:
    def __init__(self, sandbox: bool = True, withdrawal: bool = False) -> None:
        self.sandbox = sandbox
        self.withdrawal = withdrawal
        self.websocket_connected = True
        self.calls = []

    async def connect(self) -> None:
        return None

    async def call(self, operation: str, **parameters):
        self.calls.append((operation, parameters))
        now = datetime.now(UTC)
        if operation == "get_server_time":
            return now
        if operation == "get_permissions":
            return TradingPermissions(True, True, self.withdrawal)
        if operation == "get_balance":
            return ExchangeBalance(Decimal("1000"), Decimal("900"))
        if operation == "get_symbols":
            return ["BTCUSDT", "ETHUSDT"]
        if operation == "get_ticker":
            return Ticker("bybit", "BTC/USDT", Decimal("99"), Decimal("101"), Decimal("100"), now)
        if operation == "get_orderbook":
            return OrderBook("bybit", "BTC/USDT", ((Decimal("99"), Decimal("10")),), ((Decimal("101"), Decimal("10")),), now)
        if operation == "get_funding_rate":
            return Decimal("0.0001")
        if operation == "get_open_interest":
            return Decimal("1000000")
        if operation == "get_instrument_rules":
            return InstrumentRules(Decimal("0.1"), Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("100"), Decimal("20"), 1, 3)
        if operation == "create_order":
            request = parameters["request"]
            return ExchangeOrder("bybit", request.account_id, "order-1", request.symbol, "FILLED", request.quantity, request.quantity, Decimal("100"))
        if operation in ("get_positions", "get_open_orders", "get_trade_history"):
            return []
        return None


def test_all_seven_adapters_and_capabilities_are_registered():
    expected = {"bybit", "binance", "okx", "bitget", "kucoin", "gateio", "kraken"}
    assert expected <= set(ADAPTER_TYPES)
    for exchange in expected:
        adapter = create_adapter(exchange)
        assert adapter.capabilities.spot
        assert adapter.capabilities.websocket
        assert adapter.capabilities.supports_market(MarketType.PERPETUAL)
    assert create_adapter("bybit").rate_limit is not create_adapter("bybit").rate_limit


@pytest.mark.asyncio
async def test_ccxt_transport_constructs_all_seven_without_private_calls():
    transports = [CcxtTransport(exchange) for exchange in ("bybit", "binance", "okx", "bitget", "kucoin", "gateio", "kraken")]
    try:
        assert [transport.client.id for transport in transports] == ["bybit", "binance", "okx", "bitget", "kucoin", "gate", "kraken"]
        assert all(not transport.sandbox for transport in transports)
    finally:
        for transport in transports:
            await transport.close()


def test_exchange_symbol_normalization_and_formatting():
    assert create_adapter("bybit").normalize_symbol("BTCUSDT") == "BTC/USDT"
    assert create_adapter("okx").normalize_symbol("BTC-USDT-SWAP") == "BTC/USDT"
    assert create_adapter("kraken").normalize_symbol("XBT/USDT") == "BTC/USDT"
    assert create_adapter("okx").format_symbol("BTC/USDT") == "BTC-USDT-SWAP"
    assert create_adapter("kucoin").format_symbol("BTC/USDT") == "XBTUSDTM"
    assert create_adapter("gateio").format_symbol("BTC/USDT") == "BTC_USDT"


def test_precision_limits_and_minimums():
    rules = InstrumentRules(Decimal("0.1"), Decimal("0.001"), Decimal("0.01"), Decimal("5"), Decimal("2"), Decimal("10"))
    assert BybitAdapter.normalize_quantity(Decimal("1.2349"), rules) == Decimal("1.234")
    assert BybitAdapter.normalize_price(Decimal("100.06"), rules) == Decimal("100.1")
    with pytest.raises(InvalidOrderError, match="minimum"):
        BybitAdapter.normalize_quantity(Decimal("0.009"), rules)
    request = OrderRequest("a", "BTC/USDT", MarketType.PERPETUAL, OrderSide.BUY, OrderType.LIMIT, Decimal("0.01"), Decimal("100"), Decimal("11"))
    with pytest.raises(InvalidOrderError, match="leverage"):
        BybitAdapter.validate_order(request, rules)


@pytest.mark.asyncio
async def test_real_order_transport_is_always_blocked():
    adapter = BybitAdapter(FakeTransport(sandbox=False))
    request = OrderRequest("a", "BTC/USDT", MarketType.PERPETUAL, OrderSide.BUY, OrderType.MARKET, Decimal("0.1"))
    with pytest.raises(LiveTradingDisabledError):
        await adapter.create_order(request)


@pytest.mark.asyncio
async def test_sandbox_order_is_normalized_before_transport():
    transport = FakeTransport()
    adapter = BybitAdapter(transport)
    request = OrderRequest("a", "BTC/USDT", MarketType.PERPETUAL, OrderSide.BUY, OrderType.LIMIT, Decimal("0.1239"), Decimal("100.06"), Decimal("2"))
    order = await adapter.create_order(request)
    sent = next(parameters["request"] for operation, parameters in transport.calls if operation == "create_order")
    assert order.status == "FILLED"
    assert sent.quantity == Decimal("0.123")
    assert sent.price == Decimal("100.1")


@pytest.mark.asyncio
async def test_account_credentials_are_encrypted_masked_and_withdrawal_is_blocked():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    manager = ExchangeAccountManager(Fernet.generate_key().decode(), sessions)
    view = manager.add_account(1, "bybit", "primary", ExchangeCredentials("visible-api-key", "super-secret"))
    assert "visible-api-key" not in view.masked_api_key
    with sessions() as session:
        record = session.get(ExchangeAccountRecord, view.id)
        assert "visible-api-key" not in record.encrypted_api_key
        assert "super-secret" not in record.encrypted_secret
    assert manager.credentials_for(1, view.id).secret == "super-secret"
    await manager.verify_account(1, view.id, BybitAdapter(FakeTransport(withdrawal=True)))
    with pytest.raises(UnsafeExchangePermissions, match="Withdrawal"):
        manager.assert_autotrading_safe(1, view.id)


@pytest.mark.asyncio
async def test_market_context_is_bound_to_exchange_account():
    router = MarketDataRouter()
    router.register("account-a", BybitAdapter(FakeTransport()))
    context = await router.get_context("bybit", "account-a", "BTCUSDT")
    assert context.exchange == "bybit"
    assert context.account_id == "account-a"
    assert context.symbol == "BTC/USDT"
    assert context.ticker.last == Decimal("100")


@pytest.mark.asyncio
async def test_single_trading_engine_uses_bound_adapter_risk_and_duplicate_protection():
    router = MarketDataRouter()
    adapter = BybitAdapter(FakeTransport())
    router.register("account-a", adapter)
    market = await router.get_context("bybit", "account-a", "BTC/USDT")
    engine = MultiExchangeTradingEngine(router, ExchangeHealthMonitor(), PortfolioManager())
    request = OrderRequest("account-a", "BTC/USDT", MarketType.PERPETUAL, OrderSide.BUY, OrderType.MARKET, Decimal("0.1"), leverage=Decimal("1"), client_order_id="signal-1")
    risk = ExecutionRiskContext({("bybit", "account-a"): Decimal("1000")}, {})
    order = await engine.execute(market, request, Decimal("95"), risk)
    assert order.order_id == "order-1"
    with pytest.raises(PermissionError, match="Duplicate"):
        await engine.execute(market, request, Decimal("95"), risk)


def _quote(exchange: str, spread: str, liquidity: str, fees: str, slippage: str, funding: str, health: HealthStatus = HealthStatus.HEALTHY, latency: str = "10") -> VenueQuote:
    now = datetime.now(UTC)
    half = Decimal(spread) / 2
    ticker = Ticker(exchange, "BTC/USDT", Decimal("100") - half, Decimal("100") + half, Decimal("100"), now)
    report = HealthReport(exchange, health, now, Decimal(latency))
    return VenueQuote(exchange, exchange + "-account", ticker, Decimal(liquidity), Decimal(slippage), FeeSchedule(Decimal(), Decimal(fees)), Decimal(funding), report)


def test_best_execution_uses_total_execution_quality_and_health():
    selector = ExchangeSelector()
    cheap = _quote("okx", "0.02", "100000", "0.0004", "0.0001", "0")
    bad_price = _quote("bybit", "0.01", "1", "0.002", "0.005", "0.001")
    unavailable = _quote("binance", "0", "1000000", "0", "0", "0", HealthStatus.UNAVAILABLE)
    assert selector.select(SelectionMode.BEST_EXECUTION, [bad_price, cheap, unavailable])[0].exchange == "okx"


def _position(exchange: str, account: str, position_id: str, symbol: str = "BTC/USDT", side: str = "LONG") -> ManagedPosition:
    return ManagedPosition(exchange, account, symbol, position_id, "v1", side, Decimal("1"), Decimal("100"), Decimal("1"), Decimal("95"), Decimal("100"))


def test_positions_are_independent_and_failover_only_selects_new_venue():
    portfolio = PortfolioManager()
    portfolio.add(_position("bybit", "a", "p1"))
    portfolio.add(_position("binance", "b", "p1", side="SHORT"))
    assert len(portfolio.all()) == 2
    assert portfolio.remove("bybit", "a", "p1").exchange == "bybit"
    assert portfolio.all()[0].exchange == "binance"
    chosen = portfolio.failover_for_new_signal("bybit", {"bybit": HealthStatus.UNAVAILABLE, "okx": HealthStatus.HEALTHY}, ["okx"])
    assert chosen == "okx"
    assert portfolio.all()[0].exchange == "binance"


def test_global_risk_rejects_duplicate_correlated_major_exposure():
    manager = GlobalPortfolioRiskManager()
    positions = (_position("bybit", "a", "p1"),)
    proposal = ProposedExposure("binance", "b", "ETH/USDT", "LONG", Decimal("250"), Decimal("1"), Decimal("5"))
    decision = manager.approve(
        proposal,
        positions,
        {("bybit", "a"): Decimal("1000"), ("binance", "b"): Decimal("1000")},
        {},
        HealthStatus.HEALTHY,
        ExchangeRiskLimits(Decimal("1"), 5, Decimal("0.05")),
        GlobalRiskLimits(Decimal("1"), Decimal("5"), Decimal("1"), Decimal("0.1"), Decimal("0.15")),
    )
    assert not decision.approved
    assert "Correlated" in decision.reason


def test_global_risk_rejects_unavailable_and_daily_loss():
    manager = GlobalPortfolioRiskManager()
    proposal = ProposedExposure("bybit", "a", "BTC/USDT", "LONG", Decimal("10"), Decimal("1"), Decimal("1"))
    equity = {("bybit", "a"): Decimal("1000")}
    assert not manager.approve(proposal, (), equity, {}, HealthStatus.UNAVAILABLE).approved
    decision = manager.approve(proposal, (), equity, {("bybit", "a"): Decimal("-20")}, HealthStatus.HEALTHY)
    assert not decision.approved
    assert "daily loss" in decision.reason


def test_portfolio_dashboard_aggregates_accounts_without_merging_positions():
    positions = (_position("bybit", "a", "p1"), _position("binance", "b", "p2", "ETH/USDT"))
    dashboard = build_dashboard({"bybit": Decimal("1000"), "binance": Decimal("900")}, positions)
    assert dashboard.total_equity == Decimal("1900")
    assert dashboard.total_open_risk == Decimal("10")
    assert set(dashboard.positions_by_exchange) == {"bybit", "binance"}


def test_exchange_comparison_uses_each_exchange_cost_profile():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [
        Candle(start, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("10")),
        Candle(start + timedelta(minutes=5), Decimal("100"), Decimal("104"), Decimal("99"), Decimal("103"), Decimal("10")),
    ]

    def strategy_factory(_exchange):
        return lambda history: StrategyAction(Side.LONG, Decimal("99"), Decimal("103")) if len(history) == 1 else None

    comparison = ExchangeComparisonBacktest()
    results = comparison.run(
        "v1",
        {"bybit": candles, "binance": candles},
        {
            "bybit": ExchangeBacktestProfile("bybit", FeeSchedule(Decimal(), Decimal("0.001")), Decimal()),
            "binance": ExchangeBacktestProfile("binance", FeeSchedule(Decimal(), Decimal()), Decimal()),
        },
        strategy_factory,
    )
    assert results["binance"].metrics["net_pnl"] > results["bybit"].metrics["net_pnl"]
