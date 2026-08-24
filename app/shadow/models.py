from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.backtest.core import Candle


@dataclass(frozen=True)
class InstrumentConstraints:
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal
    price_step: Decimal


@dataclass(frozen=True)
class LiveMarketSnapshot:
    exchange: str
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    exchange_timestamp: datetime
    received_at: datetime
    constraints: InstrumentConstraints

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> Decimal:
        return self.spread / self.midpoint if self.midpoint else Decimal()


@dataclass(frozen=True)
class ClosedCandleObservation:
    exchange: str
    symbol: str
    candle: Candle
    close_time: datetime
    exchange_timestamp: datetime
    received_at: datetime
