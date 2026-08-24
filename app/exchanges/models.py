from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class MarketType(StrEnum):
    SPOT = "SPOT"
    FUTURES = "FUTURES"
    PERPETUAL = "PERPETUAL"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class AccountStatus(StrEnum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    WARNING = "WARNING"
    DISABLED = "DISABLED"


class SelectionMode(StrEnum):
    SINGLE_EXCHANGE = "SINGLE_EXCHANGE"
    MULTI_EXCHANGE = "MULTI_EXCHANGE"
    BEST_EXECUTION = "BEST_EXECUTION"


@dataclass(frozen=True)
class ExchangeCapabilities:
    spot: bool
    futures: bool
    perpetual: bool
    short: bool
    leverage: bool
    hedge_mode: bool
    funding: bool
    open_interest: bool
    stop_loss: bool
    take_profit: bool
    trailing_stop: bool
    websocket: bool

    def supports_market(self, market_type: MarketType) -> bool:
        return {
            MarketType.SPOT: self.spot,
            MarketType.FUTURES: self.futures,
            MarketType.PERPETUAL: self.perpetual,
        }[market_type]


@dataclass(frozen=True)
class InstrumentRules:
    tick_size: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal
    maximum_quantity: Decimal | None = None
    maximum_leverage: Decimal = Decimal("1")
    price_precision: int | None = None
    quantity_precision: int | None = None


@dataclass(frozen=True)
class ExchangeBalance:
    equity: Decimal
    available: Decimal
    currency: str = "USDT"


@dataclass(frozen=True)
class Ticker:
    exchange: str
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    timestamp: datetime

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> Decimal:
        midpoint = (self.ask + self.bid) / 2
        return self.spread / midpoint if midpoint else Decimal()


@dataclass(frozen=True)
class OrderBook:
    exchange: str
    symbol: str
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    timestamp: datetime


@dataclass(frozen=True)
class MarketContext:
    exchange: str
    account_id: str | None
    symbol: str
    market_type: MarketType
    ticker: Ticker | None = None
    orderbook: OrderBook | None = None
    funding_rate: Decimal | None = None
    open_interest: Decimal | None = None


@dataclass(frozen=True)
class OrderRequest:
    account_id: str
    symbol: str
    market_type: MarketType
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    price: Decimal | None = None
    leverage: Decimal = Decimal("1")
    client_order_id: str | None = None
    reduce_only: bool = False


@dataclass(frozen=True)
class ExchangeOrder:
    exchange: str
    account_id: str
    order_id: str
    symbol: str
    status: str
    quantity: Decimal
    filled_quantity: Decimal = Decimal()
    average_price: Decimal | None = None


@dataclass(frozen=True)
class ExchangePosition:
    exchange: str
    account_id: str
    position_id: str
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    leverage: Decimal
    unrealized_pnl: Decimal = Decimal()
    strategy_version: str = "unknown"


@dataclass(frozen=True)
class ExchangeTrade:
    exchange: str
    account_id: str
    trade_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    timestamp: datetime


@dataclass(frozen=True)
class TradingPermissions:
    read: bool = True
    trade: bool = False
    withdrawal: bool = False


@dataclass(frozen=True)
class HealthReport:
    exchange: str
    status: HealthStatus
    checked_at: datetime
    latency_ms: Decimal
    websocket_connected: bool = False
    market_data_age_seconds: Decimal | None = None
    time_drift_ms: Decimal | None = None
    rate_limited: bool = False
    order_errors: int = 0
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FeeSchedule:
    maker: Decimal
    taker: Decimal
    funding_estimate: Decimal = Decimal()


@dataclass(frozen=True)
class VenueQuote:
    exchange: str
    account_id: str
    ticker: Ticker
    available_liquidity: Decimal
    estimated_slippage_pct: Decimal
    fees: FeeSchedule
    funding_rate: Decimal
    health: HealthReport
