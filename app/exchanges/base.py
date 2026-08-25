from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Protocol

from app.backtest.core import Candle
from app.exchanges.models import (
    ExchangeBalance,
    ExchangeCapabilities,
    ExchangeOrder,
    ExchangePosition,
    ExchangeTrade,
    HealthReport,
    InstrumentRules,
    MarketType,
    OrderBook,
    OrderRequest,
    Ticker,
    TradingPermissions,
)


class ExchangeError(RuntimeError):
    pass


class UnsupportedCapabilityError(ExchangeError):
    pass


class LiveTradingDisabledError(ExchangeError):
    pass


class InvalidOrderError(ExchangeError):
    pass


class ExchangeTransport(Protocol):
    sandbox: bool

    async def connect(self) -> None: ...
    async def call(self, operation: str, **parameters: Any) -> Any: ...


class ExchangeAdapter(ABC):
    """Only exchange boundary used by trading and market-data services."""

    name: str
    capabilities: ExchangeCapabilities

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def health_check(self) -> HealthReport: ...

    @abstractmethod
    async def get_server_time(self) -> datetime: ...

    @abstractmethod
    async def get_balance(self) -> ExchangeBalance: ...

    @abstractmethod
    async def get_equity(self) -> Decimal: ...

    @abstractmethod
    async def get_symbols(self, market_type: MarketType = MarketType.PERPETUAL) -> list[str]: ...

    @abstractmethod
    async def get_ticker(self, symbol: str, market_type: MarketType = MarketType.PERPETUAL) -> Ticker: ...

    @abstractmethod
    async def get_orderbook(self, symbol: str, market_type: MarketType = MarketType.PERPETUAL, depth: int = 20) -> OrderBook: ...

    @abstractmethod
    async def get_ohlcv(self, symbol: str, timeframe: str, start: datetime | None = None, end: datetime | None = None, limit: int = 200, market_type: MarketType = MarketType.PERPETUAL) -> list[Candle]: ...

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> Decimal: ...

    @abstractmethod
    async def get_open_interest(self, symbol: str) -> Decimal: ...

    @abstractmethod
    async def get_positions(self) -> list[ExchangePosition]: ...

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]: ...

    @abstractmethod
    async def create_order(self, request: OrderRequest) -> ExchangeOrder: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> ExchangeOrder: ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: str | None = None) -> list[ExchangeOrder]: ...

    @abstractmethod
    async def close_position(self, position_id: str, symbol: str) -> ExchangeOrder: ...

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: Decimal) -> None: ...

    @abstractmethod
    async def set_stop_loss(self, position_id: str, symbol: str, price: Decimal) -> None: ...

    @abstractmethod
    async def set_take_profit(self, position_id: str, symbol: str, price: Decimal) -> None: ...

    @abstractmethod
    async def get_order_status(self, order_id: str, symbol: str) -> ExchangeOrder: ...

    @abstractmethod
    async def get_trade_history(self, symbol: str | None = None, limit: int = 100) -> list[ExchangeTrade]: ...

    @abstractmethod
    async def get_instrument_rules(self, symbol: str, market_type: MarketType = MarketType.PERPETUAL) -> InstrumentRules: ...

    @abstractmethod
    async def get_permissions(self) -> TradingPermissions: ...

    @abstractmethod
    def normalize_symbol(self, symbol: str) -> str: ...

    @abstractmethod
    def format_symbol(self, symbol: str, market_type: MarketType = MarketType.PERPETUAL) -> str: ...

    @staticmethod
    def normalize_quantity(quantity: Decimal, rules: InstrumentRules) -> Decimal:
        if quantity <= 0:
            raise InvalidOrderError("Quantity must be positive")
        normalized = (quantity / rules.quantity_step).to_integral_value(rounding=ROUND_DOWN) * rules.quantity_step
        if normalized < rules.minimum_quantity:
            raise InvalidOrderError("Quantity is below exchange minimum")
        if rules.maximum_quantity is not None and normalized > rules.maximum_quantity:
            raise InvalidOrderError("Quantity exceeds exchange maximum")
        return normalized

    @staticmethod
    def normalize_price(price: Decimal, rules: InstrumentRules) -> Decimal:
        if price <= 0:
            raise InvalidOrderError("Price must be positive")
        return (price / rules.tick_size).to_integral_value(rounding=ROUND_HALF_UP) * rules.tick_size

    @classmethod
    def validate_order(cls, request: OrderRequest, rules: InstrumentRules) -> OrderRequest:
        if request.leverage <= 0:
            raise InvalidOrderError("Leverage must be positive")
        if request.leverage > rules.maximum_leverage:
            raise InvalidOrderError("Requested leverage exceeds exchange maximum")
        quantity = cls.normalize_quantity(request.quantity, rules)
        price = cls.normalize_price(request.price, rules) if request.price is not None else None
        if price is not None and quantity * price < rules.minimum_notional:
            raise InvalidOrderError("Order notional is below exchange minimum")
        return OrderRequest(**{**request.__dict__, "quantity": quantity, "price": price})


class ExchangeReadAdapter(ABC):
    """Legacy Phase 1 read contract retained for backwards compatibility."""

    @abstractmethod
    async def balance(self) -> ExchangeBalance: ...

    @abstractmethod
    async def symbols(self) -> list[str]: ...

    @abstractmethod
    async def candles(self, symbol: str, timeframe: str, limit: int = 200): ...
