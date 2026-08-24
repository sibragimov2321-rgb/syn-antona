from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Any

from app.backtest.core import Candle
from app.exchanges.base import (
    ExchangeAdapter,
    ExchangeError,
    ExchangeTransport,
    LiveTradingDisabledError,
    UnsupportedCapabilityError,
)
from app.exchanges.models import (
    ExchangeBalance,
    ExchangeCapabilities,
    ExchangeOrder,
    ExchangePosition,
    ExchangeTrade,
    FeeSchedule,
    HealthReport,
    HealthStatus,
    InstrumentRules,
    MarketType,
    OrderBook,
    OrderRequest,
    Ticker,
    TradingPermissions,
)
from app.exchanges.rate_limit import RateLimitManager


class UnavailableTransport:
    """Safe default: adapters need an explicit REST/WebSocket or sandbox transport."""

    sandbox = False

    async def connect(self) -> None:
        raise ExchangeError("No exchange transport configured")

    async def call(self, operation: str, **parameters: Any) -> Any:
        raise ExchangeError(f"No exchange transport configured for {operation}")


ALL = ExchangeCapabilities(True, True, True, True, True, True, True, True, True, True, True, True)
NO_HEDGE_TRAILING = ExchangeCapabilities(True, True, True, True, True, False, True, True, True, True, False, True)
NO_TRAILING = ExchangeCapabilities(True, True, True, True, True, True, True, True, True, True, False, True)


class UnifiedExchangeAdapter(ExchangeAdapter):
    name = "generic"
    capabilities = ALL
    fees = FeeSchedule(Decimal("0.0002"), Decimal("0.0006"))
    requests_per_second = 8.0

    def __init__(self, transport: ExchangeTransport | None = None) -> None:
        self.transport = transport or UnavailableTransport()
        self.rate_limit = RateLimitManager(self.requests_per_second)

    async def _call(self, operation: str, **parameters: Any) -> Any:
        return await self.rate_limit.execute(lambda: self.transport.call(operation, **parameters))

    async def connect(self) -> None:
        await self.transport.connect()

    async def health_check(self) -> HealthReport:
        started = perf_counter()
        reasons: list[str] = []
        try:
            server_time = await self.get_server_time()
            drift = abs(Decimal(str((datetime.now(UTC) - server_time).total_seconds() * 1000)))
            latency = Decimal(str((perf_counter() - started) * 1000))
            if drift > 5000:
                reasons.append("server time drift exceeds 5 seconds")
            if latency > 2000:
                reasons.append("API latency exceeds 2 seconds")
            status = HealthStatus.DEGRADED if reasons else HealthStatus.HEALTHY
            return HealthReport(self.name, status, datetime.now(UTC), latency, bool(getattr(self.transport, "websocket_connected", False)), time_drift_ms=drift, reasons=tuple(reasons))
        except Exception as error:
            latency = Decimal(str((perf_counter() - started) * 1000))
            return HealthReport(self.name, HealthStatus.UNAVAILABLE, datetime.now(UTC), latency, reasons=(str(error),))

    async def get_server_time(self) -> datetime:
        return await self._call("get_server_time")

    async def get_balance(self) -> ExchangeBalance:
        return await self._call("get_balance")

    async def get_equity(self) -> Decimal:
        return (await self.get_balance()).equity

    async def get_symbols(self, market_type: MarketType = MarketType.PERPETUAL) -> list[str]:
        self._require_market(market_type)
        symbols = await self._call("get_symbols", market_type=market_type)
        return sorted({self.normalize_symbol(symbol) for symbol in symbols})

    async def get_ticker(self, symbol: str, market_type: MarketType = MarketType.PERPETUAL) -> Ticker:
        self._require_market(market_type)
        return await self._call("get_ticker", symbol=self.format_symbol(symbol, market_type), market_type=market_type)

    async def get_orderbook(self, symbol: str, market_type: MarketType = MarketType.PERPETUAL, depth: int = 20) -> OrderBook:
        self._require_market(market_type)
        return await self._call("get_orderbook", symbol=self.format_symbol(symbol, market_type), market_type=market_type, depth=depth)

    async def get_ohlcv(self, symbol: str, timeframe: str, start: datetime | None = None, end: datetime | None = None, limit: int = 200, market_type: MarketType = MarketType.PERPETUAL) -> list[Candle]:
        self._require_market(market_type)
        return await self._call("get_ohlcv", symbol=self.format_symbol(symbol, market_type), market_type=market_type, timeframe=timeframe, start=start, end=end, limit=limit)

    async def get_funding_rate(self, symbol: str) -> Decimal:
        self._require_capability("funding")
        return await self._call("get_funding_rate", symbol=self.format_symbol(symbol))

    async def get_open_interest(self, symbol: str) -> Decimal:
        self._require_capability("open_interest")
        return await self._call("get_open_interest", symbol=self.format_symbol(symbol))

    async def get_positions(self) -> list[ExchangePosition]:
        return await self._call("get_positions")

    async def get_open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]:
        return await self._call("get_open_orders", symbol=self.format_symbol(symbol) if symbol else None)

    async def create_order(self, request: OrderRequest) -> ExchangeOrder:
        self._require_sandbox_execution()
        self._require_market(request.market_type)
        rules = await self.get_instrument_rules(request.symbol, request.market_type)
        normalized = self.validate_order(request, rules)
        if normalized.price is None:
            ticker = await self.get_ticker(request.symbol, request.market_type)
            if normalized.quantity * ticker.last < rules.minimum_notional:
                raise ExchangeError("Order notional is below exchange minimum")
        return await self._call("create_order", request=normalized, exchange_symbol=self.format_symbol(request.symbol, request.market_type))

    async def cancel_order(self, order_id: str, symbol: str) -> ExchangeOrder:
        self._require_sandbox_execution()
        return await self._call("cancel_order", order_id=order_id, symbol=self.format_symbol(symbol))

    async def cancel_all_orders(self, symbol: str | None = None) -> list[ExchangeOrder]:
        self._require_sandbox_execution()
        return await self._call("cancel_all_orders", symbol=self.format_symbol(symbol) if symbol else None)

    async def close_position(self, position_id: str, symbol: str) -> ExchangeOrder:
        self._require_sandbox_execution()
        return await self._call("close_position", position_id=position_id, symbol=self.format_symbol(symbol))

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        self._require_sandbox_execution()
        self._require_capability("leverage")
        rules = await self.get_instrument_rules(symbol)
        if leverage <= 0 or leverage > rules.maximum_leverage:
            raise ExchangeError("Leverage is outside instrument limits")
        await self._call("set_leverage", symbol=self.format_symbol(symbol), leverage=leverage)

    async def set_stop_loss(self, position_id: str, symbol: str, price: Decimal) -> None:
        self._require_sandbox_execution()
        self._require_capability("stop_loss")
        rules = await self.get_instrument_rules(symbol)
        await self._call("set_stop_loss", position_id=position_id, symbol=self.format_symbol(symbol), price=self.normalize_price(price, rules))

    async def set_take_profit(self, position_id: str, symbol: str, price: Decimal) -> None:
        self._require_sandbox_execution()
        self._require_capability("take_profit")
        rules = await self.get_instrument_rules(symbol)
        await self._call("set_take_profit", position_id=position_id, symbol=self.format_symbol(symbol), price=self.normalize_price(price, rules))

    async def get_order_status(self, order_id: str, symbol: str) -> ExchangeOrder:
        return await self._call("get_order_status", order_id=order_id, symbol=self.format_symbol(symbol))

    async def get_trade_history(self, symbol: str | None = None, limit: int = 100) -> list[ExchangeTrade]:
        return await self._call("get_trade_history", symbol=self.format_symbol(symbol) if symbol else None, limit=limit)

    async def get_instrument_rules(self, symbol: str, market_type: MarketType = MarketType.PERPETUAL) -> InstrumentRules:
        return await self._call("get_instrument_rules", symbol=self.format_symbol(symbol, market_type), market_type=market_type)

    async def get_permissions(self) -> TradingPermissions:
        return await self._call("get_permissions")

    def normalize_symbol(self, symbol: str) -> str:
        cleaned = symbol.upper().replace("-SWAP", "").replace("_", "/").replace("-", "/")
        cleaned = cleaned.replace("XBT", "BTC")
        if "/" not in cleaned:
            for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
                suffix = f"{quote}M" if cleaned.endswith(f"{quote}M") else quote
                if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
                    cleaned = f"{cleaned[:-len(suffix)]}/{quote}"
                    break
        return cleaned

    def format_symbol(self, symbol: str, market_type: MarketType = MarketType.PERPETUAL) -> str:
        return self.normalize_symbol(symbol).replace("/", "")

    def _require_market(self, market_type: MarketType) -> None:
        if not self.capabilities.supports_market(market_type):
            raise UnsupportedCapabilityError(f"{self.name} does not support {market_type}")

    def _require_capability(self, name: str) -> None:
        if not getattr(self.capabilities, name):
            raise UnsupportedCapabilityError(f"{self.name} does not support {name}")

    def _require_sandbox_execution(self) -> None:
        if not getattr(self.transport, "sandbox", False):
            raise LiveTradingDisabledError("Real exchange order execution is disabled; use an explicit sandbox transport")


class BybitAdapter(UnifiedExchangeAdapter):
    name = "bybit"
    capabilities = ALL
    fees = FeeSchedule(Decimal("0.0002"), Decimal("0.00055"))
    requests_per_second = 10


class BinanceAdapter(UnifiedExchangeAdapter):
    name = "binance"
    capabilities = ALL
    fees = FeeSchedule(Decimal("0.0002"), Decimal("0.0005"))
    requests_per_second = 10


class OKXAdapter(UnifiedExchangeAdapter):
    name = "okx"
    capabilities = ALL
    fees = FeeSchedule(Decimal("0.0002"), Decimal("0.0005"))

    def format_symbol(self, symbol: str, market_type: MarketType = MarketType.PERPETUAL) -> str:
        normalized = self.normalize_symbol(symbol).replace("/", "-")
        return f"{normalized}-SWAP" if market_type is MarketType.PERPETUAL else normalized


class BitgetAdapter(UnifiedExchangeAdapter):
    name = "bitget"
    capabilities = ALL
    fees = FeeSchedule(Decimal("0.0002"), Decimal("0.0006"))


class KuCoinAdapter(UnifiedExchangeAdapter):
    name = "kucoin"
    capabilities = NO_HEDGE_TRAILING
    fees = FeeSchedule(Decimal("0.0002"), Decimal("0.0006"))

    def format_symbol(self, symbol: str, market_type: MarketType = MarketType.PERPETUAL) -> str:
        base, quote = self.normalize_symbol(symbol).split("/")
        if market_type in (MarketType.FUTURES, MarketType.PERPETUAL):
            return f"{'XBT' if base == 'BTC' else base}{quote}M"
        return f"{base}-{quote}"


class GateIOAdapter(UnifiedExchangeAdapter):
    name = "gateio"
    capabilities = NO_TRAILING
    fees = FeeSchedule(Decimal("0.0002"), Decimal("0.0005"))

    def format_symbol(self, symbol: str, market_type: MarketType = MarketType.PERPETUAL) -> str:
        return self.normalize_symbol(symbol).replace("/", "_")


class KrakenAdapter(UnifiedExchangeAdapter):
    name = "kraken"
    capabilities = NO_HEDGE_TRAILING
    fees = FeeSchedule(Decimal("0.0002"), Decimal("0.0005"))

    def format_symbol(self, symbol: str, market_type: MarketType = MarketType.PERPETUAL) -> str:
        base, quote = self.normalize_symbol(symbol).split("/")
        base = "XBT" if base == "BTC" else base
        if market_type is MarketType.PERPETUAL:
            return f"PF_{base}{quote}"
        return f"{base}/{quote}"


ADAPTER_TYPES = {
    "bybit": BybitAdapter,
    "binance": BinanceAdapter,
    "okx": OKXAdapter,
    "bitget": BitgetAdapter,
    "kucoin": KuCoinAdapter,
    "gateio": GateIOAdapter,
    "gate.io": GateIOAdapter,
    "kraken": KrakenAdapter,
}


def create_adapter(exchange: str, transport: ExchangeTransport | None = None) -> UnifiedExchangeAdapter:
    try:
        adapter_type = ADAPTER_TYPES[exchange.lower()]
    except KeyError as error:
        raise ValueError(f"Unsupported exchange: {exchange}") from error
    return adapter_type(transport)
