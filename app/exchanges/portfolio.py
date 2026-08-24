from dataclasses import dataclass
from decimal import Decimal

from app.exchanges.models import ExchangePosition, HealthStatus


@dataclass(frozen=True)
class ManagedPosition:
    exchange: str
    account_id: str
    symbol: str
    position_id: str
    strategy_version: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    leverage: Decimal
    stop_loss: Decimal
    current_price: Decimal

    @property
    def notional(self) -> Decimal:
        return self.current_price * self.quantity

    @property
    def open_risk(self) -> Decimal:
        return abs(self.entry_price - self.stop_loss) * self.quantity

    @property
    def unrealized_pnl(self) -> Decimal:
        delta = self.current_price - self.entry_price
        return delta * self.quantity * (Decimal("1") if self.side == "LONG" else Decimal("-1"))


class PortfolioManager:
    """Keeps exchange positions independent and never migrates them between venues."""

    def __init__(self) -> None:
        self._positions: dict[tuple[str, str, str], ManagedPosition] = {}

    def add(self, position: ManagedPosition) -> None:
        key = (position.exchange, position.account_id, position.position_id)
        if key in self._positions:
            raise ValueError("Duplicate exchange position")
        self._positions[key] = position

    def remove(self, exchange: str, account_id: str, position_id: str) -> ManagedPosition:
        return self._positions.pop((exchange, account_id, position_id))

    def all(self) -> tuple[ManagedPosition, ...]:
        return tuple(self._positions.values())

    def by_account(self, exchange: str, account_id: str) -> tuple[ManagedPosition, ...]:
        return tuple(position for position in self._positions.values() if position.exchange == exchange and position.account_id == account_id)

    @staticmethod
    def failover_for_new_signal(preferred_exchange: str, health_by_exchange: dict[str, HealthStatus], fallback_order: list[str]) -> str:
        if health_by_exchange.get(preferred_exchange) is not HealthStatus.UNAVAILABLE:
            return preferred_exchange
        for exchange in fallback_order:
            if health_by_exchange.get(exchange) is HealthStatus.HEALTHY:
                return exchange
        raise RuntimeError("No healthy failover exchange for new signals")


@dataclass(frozen=True)
class PortfolioDashboard:
    total_equity: Decimal
    total_pnl: Decimal
    total_open_risk: Decimal
    equity_by_exchange: dict[str, Decimal]
    positions_by_exchange: dict[str, tuple[ManagedPosition, ...]]


def build_dashboard(equity_by_exchange: dict[str, Decimal], positions: tuple[ManagedPosition, ...]) -> PortfolioDashboard:
    grouped: dict[str, list[ManagedPosition]] = {}
    for position in positions:
        grouped.setdefault(position.exchange, []).append(position)
    return PortfolioDashboard(
        sum(equity_by_exchange.values(), Decimal()),
        sum((position.unrealized_pnl for position in positions), Decimal()),
        sum((position.open_risk for position in positions), Decimal()),
        dict(equity_by_exchange),
        {exchange: tuple(values) for exchange, values in grouped.items()},
    )


def from_exchange_position(position: ExchangePosition, stop_loss: Decimal, current_price: Decimal) -> ManagedPosition:
    return ManagedPosition(position.exchange, position.account_id, position.symbol, position.position_id, position.strategy_version, position.side, position.quantity, position.entry_price, position.leverage, stop_loss, current_price)
