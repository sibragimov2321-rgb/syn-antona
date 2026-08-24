from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.domain.models import ClosedPosition, Position, RiskProfile, Side, TradeIntent
from app.risk.manager import RiskManager
from app.trading.paper import DuplicateTradeError


@dataclass(frozen=True)
class PaperAccount:
    balance: Decimal
    equity: Decimal
    available_balance: Decimal
    used_margin: Decimal
    unrealized_pnl: Decimal


class PaperBroker:
    """In-memory exchange simulator. It cannot communicate with a real exchange."""

    def __init__(
        self,
        initial_balance: Decimal = Decimal("10000"),
        risk_manager: RiskManager | None = None,
        fee_rate: Decimal = Decimal("0.0006"),
        slippage_rate: Decimal = Decimal("0.0002"),
    ) -> None:
        self._balance = initial_balance
        self._risk_manager = risk_manager or RiskManager()
        self._fee_rate = fee_rate
        self._slippage_rate = slippage_rate
        self._positions: dict[str, Position] = {}
        self._last_prices: dict[str, Decimal] = {}
        self._executed_trade_ids: set[str] = set()

    @property
    def positions(self) -> tuple[Position, ...]:
        return tuple(self._positions.values())

    def last_price(self, symbol: str) -> Decimal:
        return self._last_prices[symbol]

    def account(self) -> PaperAccount:
        unrealized = sum((self.unrealized_pnl(position) for position in self._positions.values()), Decimal())
        equity = self._balance + unrealized
        used_margin = sum(
            (position.entry_price * position.quantity / position.leverage for position in self._positions.values()),
            Decimal(),
        )
        return PaperAccount(self._balance, equity, equity - used_margin, used_margin, unrealized)

    def open_market(self, intent: TradeIntent, profile: RiskProfile) -> Position:
        if intent.trade_id in self._executed_trade_ids:
            raise DuplicateTradeError(f"Trade {intent.trade_id} was already executed")
        current = self.account()
        complete_intent = TradeIntent(
            **{**intent.__dict__, "equity": current.equity, "available_balance": current.available_balance}
        )
        approval = self._risk_manager.approve(complete_intent, profile)
        if not approval.approved:
            raise PermissionError(approval.reason)
        entry = self._fill_price(intent.entry, intent.side, entering=True)
        margin = entry * approval.quantity / intent.requested_leverage
        fee = (entry * approval.quantity * self._fee_rate).quantize(Decimal("0.01"))
        if margin + fee > current.available_balance:
            raise PermissionError("Insufficient available balance for margin and commission")
        position = Position(
            intent.trade_id, intent.user_id, intent.symbol, intent.side, approval.quantity, entry,
            intent.stop_loss, intent.take_profit, intent.requested_leverage, fee, datetime.now(UTC),
            abs(entry - intent.stop_loss), high_watermark=entry, low_watermark=entry,
        )
        self._balance -= fee
        self._positions[position.trade_id] = position
        self._executed_trade_ids.add(position.trade_id)
        self._last_prices[position.symbol] = entry
        return position

    def mark_price(self, symbol: str, price: Decimal) -> None:
        if price <= 0:
            raise ValueError("Market price must be positive")
        self._last_prices[symbol] = price

    def unrealized_pnl(self, position: Position) -> Decimal:
        price = self._last_prices.get(position.symbol, position.entry_price)
        difference = price - position.entry_price
        return (difference if position.side is Side.LONG else -difference) * position.quantity

    def close_market(self, trade_id: str, price: Decimal, reason: str) -> ClosedPosition:
        position = self._positions.pop(trade_id)
        exit_price = self._fill_price(price, position.side, entering=False)
        gross_pnl = (exit_price - position.entry_price) * position.quantity
        if position.side is Side.SHORT:
            gross_pnl = -gross_pnl
        exit_fee = (exit_price * position.quantity * self._fee_rate).quantize(Decimal("0.01"))
        realized = gross_pnl - position.entry_fee - exit_fee
        self._balance += gross_pnl - exit_fee
        self._last_prices[position.symbol] = exit_price
        return ClosedPosition(position, exit_price, exit_fee, realized, reason, datetime.now(UTC))

    def replace_position(self, position: Position) -> None:
        if position.trade_id not in self._positions:
            raise KeyError(position.trade_id)
        self._positions[position.trade_id] = position

    def close_all(self, price_by_symbol: dict[str, Decimal], reason: str = "EMERGENCY_CLOSE") -> list[ClosedPosition]:
        return [
            self.close_market(position.trade_id, price_by_symbol[position.symbol], reason)
            for position in list(self._positions.values())
        ]

    def _fill_price(self, price: Decimal, side: Side, entering: bool) -> Decimal:
        adverse_for_long = (side is Side.LONG) == entering
        modifier = Decimal("1") + self._slippage_rate if adverse_for_long else Decimal("1") - self._slippage_rate
        return (price * modifier).quantize(Decimal("0.01"))
