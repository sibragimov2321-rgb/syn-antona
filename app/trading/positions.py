from dataclasses import replace
from decimal import Decimal

from app.domain.models import ClosedPosition, Position, Side
from app.trading.paper_broker import PaperBroker


class PositionManager:
    """Applies protective exits only; stops never move farther from the market."""

    def on_price(self, broker: PaperBroker, symbol: str, price: Decimal) -> list[ClosedPosition]:
        broker.mark_price(symbol, price)
        closed: list[ClosedPosition] = []
        for position in list(broker.positions):
            if position.symbol != symbol:
                continue
            if self._stop_hit(position, price):
                closed.append(broker.close_market(position.trade_id, position.stop_loss, "STOP_LOSS"))
                continue
            if self._target_hit(position, price):
                closed.append(broker.close_market(position.trade_id, position.take_profit, "TAKE_PROFIT"))
                continue
            self._advance_protection(broker, position, price)
        return closed

    @staticmethod
    def resolve_candle_exit(
        side: Side,
        stop_loss: Decimal,
        take_profit: Decimal,
        high: Decimal,
        low: Decimal,
    ) -> tuple[Decimal, str] | None:
        """Resolve OHLC ambiguity conservatively: Stop Loss takes precedence over Take Profit."""
        stop_hit = low <= stop_loss if side is Side.LONG else high >= stop_loss
        target_hit = high >= take_profit if side is Side.LONG else low <= take_profit
        if stop_hit:
            return stop_loss, "STOP_LOSS"
        if target_hit:
            return take_profit, "TAKE_PROFIT"
        return None

    @staticmethod
    def _stop_hit(position: Position, price: Decimal) -> bool:
        return price <= position.stop_loss if position.side is Side.LONG else price >= position.stop_loss

    @staticmethod
    def _target_hit(position: Position, price: Decimal) -> bool:
        return price >= position.take_profit if position.side is Side.LONG else price <= position.take_profit

    @staticmethod
    def _advance_protection(broker: PaperBroker, position: Position, price: Decimal) -> None:
        high = max(position.high_watermark or position.entry_price, price)
        low = min(position.low_watermark or position.entry_price, price)
        protected = replace(position, high_watermark=high, low_watermark=low)
        one_r = position.initial_risk
        if position.break_even_enabled:
            reached = price >= position.entry_price + one_r if position.side is Side.LONG else price <= position.entry_price - one_r
            if reached:
                stop = max(position.stop_loss, position.entry_price) if position.side is Side.LONG else min(position.stop_loss, position.entry_price)
                protected = replace(protected, stop_loss=stop)
        if position.trailing_distance:
            candidate = high - position.trailing_distance if position.side is Side.LONG else low + position.trailing_distance
            stop = max(protected.stop_loss, candidate) if position.side is Side.LONG else min(protected.stop_loss, candidate)
            protected = replace(protected, stop_loss=stop)
        broker.replace_position(protected)
